"""Agent loop backed by `claude-agent-sdk` (routed through CCR).

Architecture:
    DataMind (Python)
        └── claude-agent-sdk.query() / ClaudeSDKClient
            └── spawns `claude` CLI subprocess (stdio JSON-RPC)
                └── HTTP to ANTHROPIC_BASE_URL  (= CCR on localhost)
                    └── CCR translates Anthropic ↔ OpenAI protocol
                        └── HTTP to the real upstream (OpenAI-compat gateway)

DataMind's 23 tools bridge into the SDK via an in-process MCP server
built from our `ToolSpec` catalogue — each handler is wrapped so the
SDK sees the standard `@tool` shape but the business logic is
unchanged.

We expose the exact same event shape as `NativeAgentLoop`:
    type=text        → SDK AssistantMessage's TextBlock
    type=tool_use    → SDK AssistantMessage's ToolUseBlock
    type=tool_result → SDK UserMessage's ToolResultBlock (our MCP server's response)
    type=done        → SDK ResultMessage

So the server / CLI / frontend don't need to know which loop is active.

Why CCR in the middle: the SDK always speaks Anthropic's /v1/messages
protocol (hardwired in the `claude` CLI). Our upstream gateways only
speak OpenAI. CCR is a thin Node process that does the translation —
~20ms overhead per request, easily absorbed by model inference latency.

Session reuse: the SDK spawns a new `claude` subprocess per `query()`
call (costs ~5s init). For a server that handles many turns, we use
`ClaudeSDKClient` as a persistent session so that cost is paid once at
warmup and amortised across all subsequent turns.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from datamind.core.hooks import AskUser, Allow, Deny, HookChain, Rewrite
from datamind.core.logging import get_logger
from datamind.core.tools import ToolRegistry, ToolSpec

from .base import AgentEvent, AgentLoopConfig, OnToolEnd, OnToolStart

_log = get_logger("agent.loop.sdk")


# ----------------------------------------------------- ToolSpec → SDK tool


class _LegacyCallbackChain:
    """Adapts the old (on_tool_start, on_tool_end) void callbacks to the
    HookChain.pre/post surface that `_spec_to_sdk_tool` expects.

    pre() always returns Allow() — the legacy callbacks can observe but not
    steer — so this preserves pre-HookChain behaviour for any caller that
    still passes raw callbacks instead of a HookChain.
    """

    def __init__(self, on_start: OnToolStart | None, on_end: OnToolEnd | None) -> None:
        self._on_start = on_start
        self._on_end = on_end

    def __bool__(self) -> bool:  # truthy so `if hooks:` runs the wrapper
        return bool(self._on_start or self._on_end)

    async def pre(self, tool_name: str, args: dict[str, Any]):
        from datamind.core.hooks import Allow  # noqa: PLC0415

        if self._on_start:
            try:
                await self._on_start(tool_name, args)
            except Exception as exc:  # noqa: BLE001
                _log.warning("on_tool_start_failed", extra={"err": repr(exc), "tool": tool_name})
        return Allow()

    async def post(self, tool_name: str, args: dict[str, Any], result: Any, error: Exception | None) -> None:
        if self._on_end:
            try:
                await self._on_end(tool_name, args, result, error)
            except Exception as exc:  # noqa: BLE001
                _log.warning("on_tool_end_failed", extra={"err": repr(exc), "tool": tool_name})


def _spec_to_sdk_tool(spec: ToolSpec, hooks: HookChain | None = None):
    """Wrap a DataMind ToolSpec into an SDK SdkMcpTool.

    SDK expects:
        @tool(name, description, input_schema_dict)
        async def handler(args: dict) -> {"content": [...], "isError": bool}

    DataMind handlers are `async def handler(**kwargs) -> JSON-serialisable`.
    We splat `args`, stringify the result, and route errors into `isError`.

    Hook parity with NativeAgentLoop
    --------------------------------
    The SDK owns its outer control loop, but EVERY DataMind tool call still
    funnels through this wrapper — it is the one chokepoint we control. So
    we run the full `HookChain` here:

      - pre-hooks → Allow / Deny / AskUser / Rewrite
        * Deny     → return a structured tool_result, handler never runs
        * AskUser  → return a `requires_confirmation` tool_result so the
                     model surfaces it to the human; a follow-up call
                     carrying `confirm_args` (e.g. confirm_destructive=True)
                     passes the gate on the next turn
        * Rewrite  → run the handler with mutated args
        * Allow    → run unchanged
      - post-hooks → audit log etc. run for completed, denied, and
        ask-user calls (so audit.jsonl matches native exactly)

    This gives the `sdk` backend the same DestructiveSqlHook /
    PathAllowlistHook / AuditLogHook guarantees as `native`.
    """
    from claude_agent_sdk import tool  # local import — SDK is optional

    schema = spec.input_schema or {"type": "object", "properties": {}}

    def _as_text_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
        if isinstance(payload, (dict, list)):
            text = json.dumps(payload, ensure_ascii=False, default=str)
        else:
            text = str(payload)
        out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            out["isError"] = True
        return out

    @tool(spec.name, spec.description, schema)
    async def _wrapped(args: dict[str, Any]) -> dict[str, Any]:
        args = args or {}
        effective_args = dict(args)

        # ---- Pre-hook chain --------------------------------------------
        if hooks:
            decision = await hooks.pre(spec.name, effective_args)
            if isinstance(decision, Deny):
                outcome = {
                    "kind": "denied",
                    "tool": spec.name,
                    "reason": decision.reason,
                    "message": (
                        f"Tool call '{spec.name}' was denied by a policy "
                        f"hook: {decision.reason}"
                    ),
                }
                await hooks.post(spec.name, effective_args, outcome, None)
                _log.info("hook_denied", extra={"tool": spec.name})
                return _as_text_result(outcome, is_error=True)
            if isinstance(decision, AskUser):
                outcome = {
                    "kind": "asks_user",
                    "tool": spec.name,
                    "requires_confirmation": True,
                    "prompt": decision.prompt,
                    "details": decision.details,
                    "confirm_args": decision.confirm_args,
                    "message": (
                        "This call requires explicit user confirmation. "
                        "Show the prompt to the user, get their consent, "
                        "then re-issue the call merging in `confirm_args`."
                    ),
                }
                await hooks.post(spec.name, effective_args, outcome, None)
                _log.info("hook_asks_user", extra={"tool": spec.name})
                # Not an error — the model must read it and act.
                return _as_text_result(outcome)
            if isinstance(decision, Rewrite):
                effective_args = decision.new_args
            # Allow → fall through

        # ---- Handler ---------------------------------------------------
        try:
            result = await spec.handler(**effective_args)
            err: Exception | None = None
        except Exception as exc:  # noqa: BLE001
            result = None
            err = exc

        # ---- Post-hook chain -------------------------------------------
        if hooks:
            await hooks.post(spec.name, effective_args, result, err)

        if err is not None:
            return _as_text_result(
                f"[{type(err).__name__}] {err}", is_error=True
            )
        return _as_text_result(result)

    return _wrapped


# ------------------------------------------------------------------- loop


class SdkAgentLoop:
    """AgentLoopProtocol impl on top of claude-agent-sdk.

    Stateless across turns at the Python level — each run_turn / stream_turn
    call opens its own SDK `query()` scope. Turn-local conversation history
    is managed explicitly (just like NativeAgentLoop) so callers upstream
    (server, CLI) don't see a behavioural difference.
    """

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        config: AgentLoopConfig,
        ccr_base_url: str,
        ccr_api_key: str,
        hooks: HookChain | None = None,
        on_tool_start: OnToolStart | None = None,
        on_tool_end: OnToolEnd | None = None,
    ) -> None:
        # Import SDK lazily so native-only installs don't pay import cost.
        from claude_agent_sdk import create_sdk_mcp_server  # noqa: PLC0415

        self._cfg = config
        self._tools = tools
        self._ccr_base = ccr_base_url
        self._ccr_key = ccr_api_key
        # The HookChain runs inside each tool wrapper (the one chokepoint we
        # control under the SDK's own loop) — giving the sdk backend the same
        # Allow/Deny/AskUser/Rewrite + audit guarantees as native. The legacy
        # on_tool_start/on_tool_end callbacks are still accepted but, if a
        # HookChain is supplied, prefer it; otherwise fall back to wrapping
        # the callbacks into a chain so old call sites keep working.
        if hooks is None and (on_tool_start or on_tool_end):
            from datamind.core.hooks import HookChain as _HC  # noqa: PLC0415

            hooks = _LegacyCallbackChain(on_tool_start, on_tool_end)
        self._hooks = hooks

        # Build the in-process MCP server once — reuse across turns.
        sdk_tools = [
            _spec_to_sdk_tool(self._tools.get(n), hooks=self._hooks)
            for n in self._tools.names()
        ]
        self._mcp_server = create_sdk_mcp_server("datamind", tools=sdk_tools)
        self._allowed_tools = [f"mcp__datamind__{n}" for n in self._tools.names()]

    # ----------------------------------------------------------- helpers

    def _build_options(self):
        """ClaudeAgentOptions pre-filled with DataMind's tool catalogue.

        A fresh options object per turn: prompts can differ, history can
        differ, and we deliberately don't inherit SDK session state across
        independent /api/chat requests.
        """
        from claude_agent_sdk import ClaudeAgentOptions  # noqa: PLC0415

        return ClaudeAgentOptions(
            model=self._cfg.model,
            system_prompt=self._cfg.system_prompt or None,
            mcp_servers={"datamind": self._mcp_server},
            allowed_tools=self._allowed_tools,
            # Keep the SDK's built-in filesystem / bash tools off — we
            # don't want the agent to stray outside DataMind's catalogue.
            disallowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch"],
            permission_mode="bypassPermissions",
            max_turns=self._cfg.max_tool_turns,
            env={
                "ANTHROPIC_BASE_URL": self._ccr_base,
                "ANTHROPIC_API_KEY": self._ccr_key,
                "ANTHROPIC_AUTH_TOKEN": self._ccr_key,
                "DISABLE_TELEMETRY": "1",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_ERROR_REPORTING": "1",
                # Block any ambient HTTP proxy from hijacking localhost.
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "ALL_PROXY": "",
                "NO_PROXY": "127.0.0.1,localhost",
            },
            load_timeout_ms=30000,
        )

    @staticmethod
    def _short_tool_name(full: str) -> str:
        """Strip the SDK's `mcp__datamind__` prefix for user-visible events."""
        return full.removeprefix("mcp__datamind__")

    # ---------------------------------------------------------------- API

    async def run_turn(
        self,
        *,
        user_message: str,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Non-streaming turn. Collects every event, returns final answer.

        History handling: the SDK doesn't accept a pre-built conversation
        directly (that's one of its opinionated choices). To keep parity
        with NativeAgentLoop we stitch prior turns into the prompt by
        concatenation — good enough for our current use, and swappable
        once we adopt SDK session resume.
        """
        prompt = self._prompt_with_history(user_message, history)
        answer_parts: list[str] = []
        tool_calls: list[tuple[str, dict]] = []
        result_info: dict[str, Any] = {}

        from claude_agent_sdk import (  # noqa: PLC0415
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            query,
        )

        async for msg in query(prompt=prompt, options=self._build_options()):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        answer_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append((self._short_tool_name(block.name), block.input))
            elif isinstance(msg, ResultMessage):
                result_info = {
                    "subtype": msg.subtype,
                    "is_error": msg.is_error,
                    "duration_ms": msg.duration_ms,
                    "num_turns": getattr(msg, "num_turns", None),
                    "cost_usd": msg.total_cost_usd,
                }

        answer = "".join(answer_parts).strip()
        return {
            "answer": answer,
            # NOTE: SDK's own session history isn't easily serialisable
            # back to our dict shape; callers who need history for
            # multi-turn continuity should use run_turn's return value
            # as a black box (or switch to NativeAgentLoop).
            "history": (history or []) + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": answer},
            ],
            "iterations": result_info.get("num_turns") or len(tool_calls) + 1,
            "stop_reason": "end_turn" if not result_info.get("is_error") else "error",
            "usage": {
                "duration_ms": result_info.get("duration_ms"),
                "cost_usd": result_info.get("cost_usd"),
            },
        }

    async def stream_turn(
        self,
        *,
        user_message: str,
        history: list[dict] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Stream AgentEvents for one turn. Translates SDK messages on the fly."""
        prompt = self._prompt_with_history(user_message, history)

        from claude_agent_sdk import (  # noqa: PLC0415
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
            query,
        )

        # Track in-flight tool_use → tool_result pairing. The SDK reports
        # ToolUseBlock in an AssistantMessage and the matching result in a
        # subsequent UserMessage. We match by tool_use_id.
        pending_tools: dict[str, str] = {}  # tool_use_id -> short name

        iterations = 0
        async for msg in query(prompt=prompt, options=self._build_options()):
            if isinstance(msg, AssistantMessage):
                iterations += 1
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        if block.text:
                            yield AgentEvent(type="text", data={"delta": block.text})
                    elif isinstance(block, ToolUseBlock):
                        short = self._short_tool_name(block.name)
                        pending_tools[block.id] = short
                        yield AgentEvent(
                            type="tool_use",
                            data={"name": short, "input": block.input, "id": block.id},
                        )
            elif isinstance(msg, UserMessage):
                # UserMessage with a ToolResultBlock = our MCP server's reply.
                content = getattr(msg, "content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        short = pending_tools.pop(block.tool_use_id, "?")
                        raw = block.content
                        if isinstance(raw, list):
                            # SDK returns [{type:"text", text:"..."}]
                            preview = " ".join(
                                p.get("text", "") for p in raw if isinstance(p, dict)
                            )
                        else:
                            preview = str(raw)
                        yield AgentEvent(
                            type="tool_result",
                            data={
                                "name": short,
                                "is_error": bool(getattr(block, "is_error", False)),
                                "preview": preview[:500],
                            },
                        )
            elif isinstance(msg, ResultMessage):
                yield AgentEvent(
                    type="done",
                    data={
                        "iterations": getattr(msg, "num_turns", None) or iterations,
                        "stop_reason": "end_turn" if not msg.is_error else "error",
                        "usage": {
                            "duration_ms": msg.duration_ms,
                            "cost_usd": msg.total_cost_usd,
                        },
                    },
                )
            elif isinstance(msg, SystemMessage):
                # Surface retry/error notifications — useful for debugging CCR issues.
                if msg.subtype in ("api_error",):
                    yield AgentEvent(
                        type="error",
                        data={"message": str(getattr(msg, "data", msg.subtype))[:500]},
                    )

    # ---------------------------------------------------------------- util

    @staticmethod
    def _prompt_with_history(user_message: str, history: list[dict] | None) -> str:
        """Fold prior-turn history into a single prompt string.

        The SDK's `query()` takes a string prompt (or AsyncIterable of
        typed messages for advanced use). To keep parity with
        NativeAgentLoop without diving into SDK streaming-input mode, we
        serialise history as a short preamble. For most /api/chat
        requests history is empty anyway.
        """
        if not history:
            return user_message
        lines: list[str] = ["--- prior conversation ---"]
        for turn in history:
            role = turn.get("role", "?")
            content = turn.get("content", "")
            if isinstance(content, list):
                # Blocks — just concatenate any text parts.
                content = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            lines.append(f"[{role}] {content}")
        lines.append("--- current turn ---")
        lines.append(user_message)
        return "\n".join(lines)


__all__ = ["SdkAgentLoop"]
