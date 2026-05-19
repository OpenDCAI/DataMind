"""Hook framework for sandboxed tool execution (v0.3 / Phase 8).

The agent loop (`NativeAgentLoop` / `SdkAgentLoop`) dispatches every tool
call through a single chokepoint. v0.2 had two void callbacks
(`on_tool_start`, `on_tool_end`) that could observe but not steer; v0.3
upgrades that seam to a richer `Hook` protocol that returns one of four
decisions per call:

    Allow                   — the tool runs as proposed (default)
    Deny(reason)            — the tool is blocked; agent sees an error
    AskUser(prompt, ...)    — surface a structured `requires_confirmation`
                              tool_result so the human-in-the-loop is the
                              model itself; the agent must re-issue the
                              call with explicit consent fields to pass
    Rewrite(new_args)       — replace args before the tool runs
                              (e.g. force `read_only=True`, append a LIMIT
                              clause, pin a tenant filter)

`HookChain` runs hooks in registration order; the first non-`Allow`
decision wins. Post-hooks run in registration order regardless of pre
decisions (so audit log gets every attempt, including denied ones).

Built-in hooks live in `datamind/capabilities/hooks/` (Phase 8b/c/d):
    - DestructiveSqlHook     — parses SQL, AskUser on DROP/DELETE/UPDATE
    - PathAllowlistHook      — refuses paths outside the profile data dir
    - AuditLogHook           — appends every tool call to audit.jsonl

Design notes:
- `HookDecision` is a discriminated union of frozen dataclasses, NOT an
  enum — Deny/AskUser/Rewrite carry payloads. Pattern-match with
  `isinstance` (or 3.10+ structural match) at the call site.
- `pre_tool_use` is async because real hooks may need DB lookups
  (per-tenant policy) or sqlglot parses; we don't pre-judge.
- The chain itself is stateless — state belongs in individual hooks.
- `to_callbacks(chain)` returns the legacy `(OnToolStart, OnToolEnd)`
  pair so older code (tests, demo scripts) keeps working without
  re-plumbing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .context import RequestContext
from .logging import current_context, get_logger

_log = get_logger("hooks")


# ============================================================ HookDecision


@dataclass(frozen=True)
class Allow:
    """Allow the tool call to proceed unmodified."""

    pass


@dataclass(frozen=True)
class Deny:
    """Block the tool call. The agent receives `reason` as the error message.

    Use for hard-no policies: paths outside the allow-list, blanket
    `DROP DATABASE`, calls with malformed args.
    """

    reason: str


@dataclass(frozen=True)
class AskUser:
    """Block the tool call until the agent re-issues it with consent.

    The hook returns a structured tool_result containing `prompt` and any
    `details` the model should surface to the human. The agent is
    instructed (via system prompt) to translate human assent into a
    follow-up call carrying the consent fields named in `confirm_args`.

    Example: DestructiveSqlHook returns
        AskUser(
            prompt="Confirm destructive SQL: DELETE FROM orders WHERE...",
            details={"op": "DELETE", "row_estimate": 42},
            confirm_args={"confirm_destructive": True},
        )
    The agent then asks the human, and on yes re-issues
        db_query_sql(sql="...", confirm_destructive=True)
    The hook sees `confirm_destructive=True` and returns Allow().
    """

    prompt: str
    details: dict[str, Any] = field(default_factory=dict)
    confirm_args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rewrite:
    """Allow the tool call but with mutated arguments.

    Use for normalisation: append `LIMIT 1000` to unbounded SQL, force
    `read_only=True`, add a tenant filter the agent forgot.
    """

    new_args: dict[str, Any]


HookDecision = Allow | Deny | AskUser | Rewrite


# ============================================================ Hook Protocol


@runtime_checkable
class Hook(Protocol):
    """A pre/post-tool-use interceptor.

    Implementations must be async even if the body is synchronous —
    consistency with the loop's await sites avoids special-casing.
    """

    name: str

    async def pre_tool_use(
        self,
        ctx: RequestContext,
        tool_name: str,
        args: dict[str, Any],
    ) -> HookDecision:
        """Decide whether the tool may run. Default-implementing hooks
        that don't care about a particular tool should return `Allow()`.
        """
        ...

    async def post_tool_use(
        self,
        ctx: RequestContext,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        error: Exception | None,
    ) -> None:
        """Observe the outcome. Used for audit log, metrics, traces.

        Returning a value is meaningless — post hooks cannot steer
        execution after the fact.
        """
        ...


# ============================================================ HookChain


class HookChain:
    """Ordered collection of Hooks. First non-Allow decision wins.

    Construction:
        chain = HookChain([
            PathAllowlistHook(roots=[...]),
            DestructiveSqlHook(),
            AuditLogHook(audit_path=...),
        ])
    """

    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self._hooks: list[Hook] = list(hooks or [])

    def add(self, hook: Hook) -> None:
        self._hooks.append(hook)

    def names(self) -> list[str]:
        return [h.name for h in self._hooks]

    def __len__(self) -> int:
        return len(self._hooks)

    def __bool__(self) -> bool:  # for `if chain:` checks
        return bool(self._hooks)

    async def pre(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> HookDecision:
        """Run pre-hooks in order. Return the first non-Allow decision,
        or Allow() if everyone allows. Rewrites are composable: a Rewrite
        from hook N replaces args for hook N+1's input.
        """
        ctx = current_context() or RequestContext.new()
        cur_args = dict(args)
        for h in self._hooks:
            try:
                decision = await h.pre_tool_use(ctx, tool_name, cur_args)
            except Exception as exc:  # noqa: BLE001 — hook bug must not crash agent
                _log.warning(
                    "hook_pre_failed",
                    extra={"hook": h.name, "tool": tool_name, "err": repr(exc)},
                )
                continue
            if isinstance(decision, Allow):
                continue
            if isinstance(decision, Rewrite):
                cur_args = dict(decision.new_args)
                continue  # keep going — later hooks see the rewritten args
            # Deny / AskUser short-circuit.
            return decision
        # If any rewrite happened, return final Rewrite; else Allow.
        if cur_args != args:
            return Rewrite(new_args=cur_args)
        return Allow()

    async def post(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        error: Exception | None,
    ) -> None:
        """Run post-hooks in registration order. Errors in one hook do
        not prevent later hooks from running.
        """
        ctx = current_context() or RequestContext.new()
        for h in self._hooks:
            try:
                await h.post_tool_use(ctx, tool_name, args, result, error)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "hook_post_failed",
                    extra={"hook": h.name, "tool": tool_name, "err": repr(exc)},
                )


# ============================================================ Adapters


# Legacy callback signatures retained for back-compat with existing call sites
# (tests, hello_agent demos). The agent loop now consumes HookChain directly,
# but anything written against OnToolStart / OnToolEnd still works.
OnToolStart = Callable[[str, dict], Awaitable[None]]
OnToolEnd = Callable[[str, dict, Any, Exception | None], Awaitable[None]]


def to_callbacks(chain: HookChain) -> tuple[OnToolStart, OnToolEnd]:
    """Wrap a HookChain into the legacy `(on_tool_start, on_tool_end)` pair.

    The pre callback degrades the rich HookDecision to fire-and-forget:
        - Allow / Rewrite → noop (the chain has already mutated args
          in place if rewriting was needed; this wrapper is for tests
          that don't care about decision routing)
        - Deny / AskUser → raise HookBlocked(reason)
    Use cases that need rewrite/askuser routing must consume HookChain
    directly via `NativeAgentLoop(hooks=chain, ...)`.
    """

    async def _start(tool_name: str, args: dict) -> None:
        decision = await chain.pre(tool_name, args)
        if isinstance(decision, Deny):
            raise HookBlocked(decision.reason)
        if isinstance(decision, AskUser):
            raise HookBlocked(f"awaiting user confirmation: {decision.prompt}")
        # Allow / Rewrite — observe-only adapter, ignore the rewrite

    async def _end(
        tool_name: str, args: dict, result: Any, error: Exception | None
    ) -> None:
        await chain.post(tool_name, args, result, error)

    return _start, _end


class HookBlocked(Exception):
    """Raised by the legacy adapter to abort a tool call. The agent loop
    converts this to an `is_error` tool_result so the model can recover.

    Code consuming HookChain directly does NOT use this — it inspects the
    HookDecision and produces a structured tool_result.
    """

    pass


__all__ = [
    "Allow",
    "AskUser",
    "Deny",
    "Hook",
    "HookBlocked",
    "HookChain",
    "HookDecision",
    "OnToolEnd",
    "OnToolStart",
    "Rewrite",
    "to_callbacks",
]
