"""Native vendor-neutral tool-use loop.

This is the default backend (`DATAMIND__AGENT__BACKEND=native`). It consumes
the common model-client contract and therefore supports Anthropic Messages or
OpenAI Chat Completions without changing tool execution.

    1. Send [history + user_message] with tools=[...] to /v1/messages
    2. If stop_reason == "tool_use":
         - For each tool_use block:
             - Run the hook chain (PreToolUse) — Deny / AskUser / Rewrite
               are surfaced as structured tool_results; Allow / Rewrite
               proceed to the handler
             - Invoke ToolRegistry[name].handler(**input_or_rewritten)
             - Run the hook chain (PostToolUse) — audit log, metrics
             - Append tool_result block
         - Loop to step 1 with the tool_result(s) appended as user message
    3. Otherwise: emit the final assistant text and return

Both `run_turn` (non-streaming) and `stream_turn` (async generator of
`AgentEvent`s) are exposed; the server uses `stream_turn` for SSE.

Hook seam (Phase 8): the loop accepts an optional `HookChain` that runs
on every tool dispatch. The legacy `on_tool_start` / `on_tool_end` void
callbacks are kept for back-compat (tests, hello_agent) and run alongside
the chain when both are provided.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any, AsyncIterator

from datamind.core.contracts import ToolAccess
from datamind.core.hooks import (
    AskUser,
    Allow,
    Deny,
    HookChain,
    Rewrite,
)
from datamind.core.logging import current_context, get_logger
from datamind.core.model_clients import AnthropicModelClient
from datamind.core.protocols import ModelResponse
from datamind.core.tools import ToolRegistry

from .base import AgentEvent, AgentLoopConfig, OnToolEnd, OnToolStart

_log = get_logger("agent.loop.native")


class NativeAgentLoop:
    """One instance per (client, tools) pair; safe to share across requests."""

    def __init__(
        self,
        *,
        client: Any,
        tools: ToolRegistry,
        config: AgentLoopConfig,
        on_tool_start: OnToolStart | None = None,
        on_tool_end: OnToolEnd | None = None,
        hooks: HookChain | None = None,
    ) -> None:
        self._client = client
        # Public constructors historically accepted AsyncAnthropic directly.
        # Keep that API while running the loop through the neutral contract.
        self._model_client = (
            client if callable(getattr(client, "complete", None))
            else AnthropicModelClient(client, default_model=config.model)
        )
        self._tools = tools
        self._cfg = config
        self._on_tool_start = on_tool_start
        self._on_tool_end = on_tool_end
        self._hooks = hooks

    # ----------------------------------------------------------- helpers

    async def _dispatch_tool(
        self, name: str, tool_input: dict
    ) -> tuple[Any, Exception | None, dict[str, Any] | None]:
        """Run the full pre-hook → handler → post-hook pipeline.

        Returns `(result, error, hook_outcome)`:
            result        — handler return, or a structured dict for
                            Deny/AskUser. None on handler error.
            error         — exception from handler (post-hooks see this).
            hook_outcome  — None for normal Allow/Rewrite paths;
                            {"kind": "denied"|"asks_user", ...} when the
                            pre-hook chain short-circuited the call.
        """
        if "__datamind_parse_error__" in tool_input:
            return None, ValueError(str(tool_input["__datamind_parse_error__"])), None
        try:
            spec = self._tools.get(name)
        except Exception as exc:  # unknown tool
            return None, exc, None

        effective_args = dict(tool_input)
        hook_outcome: dict[str, Any] | None = None

        # ---- Pre hooks (HookChain) -----------------------------------
        if self._hooks:
            decision = await self._hooks.pre(name, effective_args)
            if isinstance(decision, Deny):
                _log.info(
                    "hook_denied",
                    extra={"tool": name, "reason": decision.reason},
                )
                hook_outcome = {
                    "kind": "denied",
                    "tool": name,
                    "reason": decision.reason,
                    "message": (
                        f"Tool call '{name}' was denied by a policy hook: "
                        f"{decision.reason}"
                    ),
                }
                # Run post-hooks so audit captures the denial.
                await self._hooks.post(name, effective_args, hook_outcome, None)
                return hook_outcome, None, hook_outcome
            if isinstance(decision, AskUser):
                _log.info(
                    "hook_asks_user",
                    extra={"tool": name, "prompt": decision.prompt[:200]},
                )
                hook_outcome = {
                    "kind": "asks_user",
                    "tool": name,
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
                await self._hooks.post(name, effective_args, hook_outcome, None)
                return hook_outcome, None, hook_outcome
            if isinstance(decision, Rewrite):
                effective_args = decision.new_args
            # Allow → fall through unchanged

        # ---- Legacy callback (back-compat) ---------------------------
        if self._on_tool_start:
            try:
                await self._on_tool_start(name, effective_args)
            except Exception as exc:  # hook failure is non-fatal
                _log.warning("on_tool_start_failed", extra={"err": repr(exc)})

        # ---- Tool handler --------------------------------------------
        try:
            result = await spec.handler(**effective_args)
            err: Exception | None = None
        except Exception as exc:  # noqa: BLE001
            result = None
            err = exc

        # ---- Post hooks ----------------------------------------------
        if self._hooks:
            await self._hooks.post(name, effective_args, result, err)
        if self._on_tool_end:
            try:
                await self._on_tool_end(name, effective_args, result, err)
            except Exception as exc:
                _log.warning("on_tool_end_failed", extra={"err": repr(exc)})
        return result, err, None

    @staticmethod
    def _block_to_dict(block: Any) -> dict[str, Any]:
        """Normalise Anthropic response blocks into plain dicts for history."""
        if isinstance(block, dict):
            return dict(block)
        t = getattr(block, "type", None)
        if t == "text":
            return {"type": "text", "text": block.text}
        if t == "tool_use":
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": dict(block.input or {}),
            }
        # Best effort — anything else we keep as JSON-stringified blob.
        try:
            return dict(block.to_dict())  # type: ignore[attr-defined]
        except AttributeError:
            return {"type": t or "unknown", "data": str(block)}

    @staticmethod
    def _bounded_value(value: Any, *, max_rows: int) -> tuple[Any, bool, int | None]:
        """Project large row/result arrays before they enter model context."""
        if isinstance(value, dict):
            projected: dict[str, Any] = {}
            was_truncated = False
            total_count: int | None = None
            for key, item in value.items():
                if key in {"rows", "results", "paths", "edges", "items", "entities"} and isinstance(item, list):
                    total_count = max(total_count or 0, len(item))
                    projected[key] = item[:max_rows]
                    if len(item) > max_rows:
                        was_truncated = True
                else:
                    projected[key] = item
            if was_truncated:
                projected["truncated"] = True
                projected.setdefault("total_count", total_count)
                projected.setdefault("next_cursor", max_rows)
            return projected, was_truncated, total_count
        if isinstance(value, list) and len(value) > max_rows:
            return value[:max_rows], True, len(value)
        return value, False, None

    def _tool_result_block(self, tool_use_id: str, result: Any, err: Exception | None) -> dict:
        if err is None:
            result, structured_truncated, total_count = self._bounded_value(
                result, max_rows=self._cfg.max_tool_result_rows
            )
            # Stringify unless already a string — the API accepts both but
            # models read plain text better.
            if isinstance(result, str):
                content = result
            else:
                try:
                    content = json.dumps(result, ensure_ascii=False)
                except (TypeError, ValueError):
                    content = str(result)
            if len(content) > self._cfg.max_tool_result_chars:
                omitted = len(content) - self._cfg.max_tool_result_chars
                content = (
                    content[: self._cfg.max_tool_result_chars]
                    + f"\n…[tool result truncated; {omitted} chars omitted]"
                )
                structured_truncated = True
            block = {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
            }
            if structured_truncated:
                block["_datamind_truncated"] = True
                if total_count is not None:
                    block["_datamind_total_count"] = total_count
            return block
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": True,
            "content": f"{type(err).__name__}: {err}",
        }

    def _trace_and_evidence(
        self,
        *,
        name: str,
        tool_input: dict[str, Any],
        result: Any,
        error: Exception | None,
        outcome: dict[str, Any] | None = None,
        latency_ms: float = 0.0,
        duplicate: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Normalize one dispatch for inference observability."""
        try:
            spec = self._tools.get(name)
        except Exception:
            return ({"name": name, "input": tool_input, "is_error": True}, [])
        surface = spec.metadata.get("surface")
        receipt_failed = self._result_failed(result)
        trace = {
            "name": name,
            "surface": surface,
            "access": spec.metadata.get("access", "read"),
            "input": tool_input,
            "is_error": error is not None or outcome is not None or receipt_failed,
            "latency_ms": round(latency_ms, 3),
            "result_size_chars": len(self._safe_json(result)),
            "duplicate": duplicate,
        }
        if error is not None:
            trace.update({
                "error_type": type(error).__name__,
                "error_code": getattr(error, "status_code", None),
                "message": self._redact_message(str(error))[:500],
            })
        if outcome is not None:
            trace["hook_outcome"] = outcome.get("kind")
        if (
            error is not None
            or outcome is not None
            or spec.access != ToolAccess.READ
            or surface is None
        ):
            return trace, []

        evidence: list[dict[str, Any]] = []
        if surface == "kb" and isinstance(result, dict):
            for item in result.get("results", []):
                if not isinstance(item, dict):
                    continue
                evidence.append({
                    "surface": surface,
                    "source_id": item.get("source"),
                    "locator": {"source": item.get("source"), "chunk_id": item.get("id")},
                    "content": item.get("text"),
                    "score": item.get("score"),
                })
            if not evidence:
                evidence.append({
                    "surface": surface,
                    "source_id": result.get("source"),
                    "locator": dict(tool_input),
                    "content": result,
                    "score": None,
                })
        elif surface == "memory" and isinstance(result, dict):
            for item in result.get("results", []):
                if not isinstance(item, dict):
                    continue
                evidence.append({
                    "surface": surface,
                    "source_id": item.get("id"),
                    "locator": {
                        "scope": item.get("scope"),
                        "profile": item.get("profile"),
                        "session_id": item.get("session_id"),
                    },
                    "content": item.get("content"),
                    "score": item.get("score"),
                })
        elif surface == "db" and isinstance(result, dict):
            # Listing catalog names alone is not evidence that a table was
            # actually used. Describe/query calls retain precise locators.
            if name != "db_list_tables":
                sql = result.get("sql") or tool_input.get("sql")
                tables = tool_input.get("tables") or []
                if tool_input.get("table"):
                    tables = [tool_input["table"]]
                evidence.append({
                    "surface": "db",
                    "source_id": tables[0] if len(tables) == 1 else None,
                    "locator": {
                        "tables": tables,
                        "sql": sql,
                        "columns": result.get("columns"),
                    },
                    "content": {
                        "columns": result.get("columns"),
                        "rows": (result.get("rows") or [])[: self._cfg.max_tool_result_rows],
                    },
                    "score": None,
                })
        elif surface == "graph" and isinstance(result, dict):
            if name != "graph_search_entities" or result.get("entities"):
                evidence.append({
                    "surface": "graph",
                    "source_id": result.get("start") or result.get("entity"),
                    "locator": {
                        "start": result.get("start") or result.get("entity"),
                        "relation_filter": tool_input.get("relation_filter"),
                        "paths": result.get("paths"),
                        "edges": result.get("edges"),
                    },
                    "content": None,
                    "score": None,
                })
        else:
            source_id = None
            locator = dict(tool_input)
            if isinstance(result, dict):
                source_id = result.get("source") or result.get("path") or result.get("name")
            evidence.append({
                "surface": surface,
                "source_id": source_id,
                "locator": locator,
                "content": result,
                "score": None,
            })
        return trace, evidence

    @staticmethod
    def _safe_json(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _redact_message(value: str) -> str:
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
        return value

    @staticmethod
    def _call_key(name: str, tool_input: dict[str, Any]) -> str:
        raw = json.dumps([name, tool_input], sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _complete(
        self,
        *,
        conv: list[dict[str, Any]],
        allow_tools: bool,
        system_prompt: str,
    ) -> ModelResponse:
        return await self._model_client.complete(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            temperature=self._cfg.temperature,
            system=system_prompt or None,
            tools=self._tools.as_anthropic_tools() if allow_tools and len(self._tools) else None,
            tool_choice=None if allow_tools else "none",
            messages=conv,
        )

    @staticmethod
    def _contract_prompt(contract: dict[str, Any] | None) -> str:
        if not contract:
            return ""
        rules = ["Final answer contract (caller supplied; do not call tools while formatting):"]
        if contract.get("language"):
            rules.append(f"- language: {contract['language']}")
        if contract.get("markdown") is False:
            rules.append("- no Markdown")
        if contract.get("type"):
            rules.append(f"- output type: {contract['type']}")
        if contract.get("json_schema"):
            rules.append("- output valid JSON matching: " + json.dumps(contract["json_schema"], ensure_ascii=False))
        if contract.get("max_length"):
            rules.append(f"- maximum characters: {int(contract['max_length'])}")
        return "\n".join(rules)

    @staticmethod
    def _validate_contract(answer: str, contract: dict[str, Any] | None) -> bool:
        if not contract:
            return True
        if contract.get("max_length") and len(answer) > int(contract["max_length"]):
            return False
        kind = contract.get("type")
        if kind == "number":
            try:
                float(answer.strip())
            except ValueError:
                return False
        schema = contract.get("json_schema")
        if schema or kind in {"json", "array", "object"}:
            try:
                parsed = json.loads(answer)
            except json.JSONDecodeError:
                return False
            expected = (schema or {}).get("type") or kind
            if expected == "array" and not isinstance(parsed, list):
                return False
            if expected == "object" and not isinstance(parsed, dict):
                return False
        return True

    @staticmethod
    def _dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            key = json.dumps(
                [item.get("surface"), item.get("source_id"), item.get("locator")],
                sort_keys=True, ensure_ascii=False, default=str,
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    @staticmethod
    def _best_effort_answer(evidence: list[dict[str, Any]]) -> str:
        """Non-sentinel fallback for a non-compliant/timeout provider."""
        if not evidence:
            return "未能在当前预算内形成可靠答案。"
        compact = []
        for item in evidence[:5]:
            content = item.get("content")
            if content is None:
                content = item.get("locator")
            compact.append(content)
        return "基于已收集证据：" + json.dumps(compact, ensure_ascii=False, default=str)[:4000]

    @staticmethod
    def _usage_dict(
        input_tokens: int,
        output_tokens: int,
        cache_read: int,
        cache_create: int,
        tool_calls: int,
        resolved_models: list[str],
    ) -> dict[str, Any]:
        context = current_context()
        nested = (
            list(context.extra.get("nested_model_usage", []))
            if context is not None else []
        )
        nested_input = sum(int(item.get("input_tokens", 0) or 0) for item in nested)
        nested_output = sum(int(item.get("output_tokens", 0) or 0) for item in nested)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_input_tokens": input_tokens + nested_input,
            "total_output_tokens": output_tokens + nested_output,
            "cache_read": cache_read,
            "cache_create": cache_create,
            "tool_calls": tool_calls,
            "resolved_models": list(resolved_models),
            "nested_model_calls": nested,
        }

    @staticmethod
    def _result_failed(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        results = result.get("results")
        return isinstance(results, list) and any(
            isinstance(item, dict) and item.get("status") == "failed"
            for item in results
        )

    # ---------------------------------------------------------------- API

    async def run_turn(
        self,
        *,
        user_message: str,
        history: list[dict] | None = None,
        final_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one user turn to completion. Returns {answer, history, usage}."""
        conv: list[dict] = list(history or [])
        conv.append({"role": "user", "content": user_message})
        bound_context = current_context()
        if bound_context is not None:
            bound_context.extra["nested_model_usage"] = []
        contract_prompt = self._contract_prompt(final_contract)
        system_prompt = self._cfg.system_prompt
        if contract_prompt:
            system_prompt = f"{system_prompt}\n\n{contract_prompt}".strip()

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_create = 0
        tool_trace: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        surfaces_used: list[str] = []
        resolved_models: list[str] = []
        successful_calls: dict[str, Any] = {}
        tool_call_count = 0
        started = time.monotonic()

        for iteration in range(self._cfg.max_tool_turns):
            elapsed = time.monotonic() - started
            if elapsed >= self._cfg.wall_clock_timeout_s:
                allow_tools = False
            else:
                allow_tools = (
                    iteration < self._cfg.max_tool_turns - 1
                    and tool_call_count < self._cfg.max_tool_calls
                    and total_input < self._cfg.max_input_tokens
                )
            remaining = max(0.1, self._cfg.wall_clock_timeout_s - elapsed)
            try:
                async with asyncio.timeout(remaining):
                    resp = await self._complete(
                        conv=conv,
                        allow_tools=allow_tools,
                        system_prompt=system_prompt,
                    )
            except TimeoutError:
                return {
                    "answer": self._best_effort_answer(evidence),
                    "history": conv,
                    "iterations": iteration,
                    "stop_reason": "wall_clock_timeout",
                    "usage": self._usage_dict(
                        total_input, total_output, total_cache_read, total_cache_create,
                        tool_call_count, resolved_models,
                    ),
                    "tool_trace": tool_trace,
                    "evidence": evidence,
                    "receipts": receipts,
                    "surfaces_used": surfaces_used,
                }
            total_input += resp.usage.input_tokens
            total_output += resp.usage.output_tokens
            total_cache_read += resp.usage.cache_read_tokens
            total_cache_create += resp.usage.cache_create_tokens
            if resp.resolved_model and resp.resolved_model not in resolved_models:
                resolved_models.append(resp.resolved_model)

            assistant_blocks = [self._block_to_dict(b) for b in resp.content]
            conv.append({"role": "assistant", "content": assistant_blocks})

            if resp.stop_reason != "tool_use" or not allow_tools:
                text = "".join(b["text"] for b in assistant_blocks if b.get("type") == "text")
                stop_reason = resp.stop_reason if resp.stop_reason != "tool_use" else "max_iterations"
                # One no-tool formatting repair is allowed, and it never gets
                # access to evidence tools or expected-answer content.
                if text and not self._validate_contract(text, final_contract):
                    conv.append({
                        "role": "user",
                        "content": "Reformat the previous answer to satisfy the final answer contract. "
                                   "Do not add new factual claims.",
                    })
                    repair = await self._complete(
                        conv=conv, allow_tools=False, system_prompt=system_prompt,
                    )
                    total_input += repair.usage.input_tokens
                    total_output += repair.usage.output_tokens
                    if repair.resolved_model and repair.resolved_model not in resolved_models:
                        resolved_models.append(repair.resolved_model)
                    repair_blocks = [self._block_to_dict(b) for b in repair.content]
                    conv.append({"role": "assistant", "content": repair_blocks})
                    candidate = "".join(
                        b["text"] for b in repair_blocks if b.get("type") == "text"
                    )
                    if candidate:
                        text = candidate
                        stop_reason = "contract_repaired"
                if not text:
                    text = self._best_effort_answer(evidence)
                return {
                    "answer": text,
                    "history": conv,
                    "iterations": iteration + 1,
                    "stop_reason": stop_reason,
                    "usage": self._usage_dict(
                        total_input, total_output, total_cache_read, total_cache_create,
                        tool_call_count, resolved_models,
                    ),
                    "tool_trace": tool_trace,
                    "evidence": self._dedupe_evidence(evidence),
                    "receipts": receipts,
                    "surfaces_used": surfaces_used,
                }

            # Dispatch independent read calls concurrently. Identical successful
            # calls are reused instead of repeatedly hitting the same service.
            tool_results: list[dict] = []
            blocks = [b for b in assistant_blocks if b.get("type") == "tool_use"]
            pending: dict[str, asyncio.Task] = {}
            for b in blocks:
                key = self._call_key(b["name"], b["input"])
                if key in successful_calls or key in pending:
                    continue
                if tool_call_count + len(pending) >= self._cfg.max_tool_calls:
                    continue

                async def _timed_dispatch(block=b):
                    call_started = time.monotonic()
                    result = await self._dispatch_tool(block["name"], block["input"])
                    return (*result, (time.monotonic() - call_started) * 1000)

                pending[key] = asyncio.create_task(_timed_dispatch())
            completed = dict(zip(pending, await asyncio.gather(*pending.values()))) if pending else {}

            for b in blocks:
                key = self._call_key(b["name"], b["input"])
                duplicate = key in successful_calls
                if duplicate:
                    result, err, outcome, latency_ms = successful_calls[key], None, None, 0.0
                elif key in completed:
                    result, err, outcome, latency_ms = completed[key]
                    tool_call_count += 1
                    if err is None and outcome is None:
                        successful_calls[key] = result
                else:
                    result = None
                    err = RuntimeError("tool-call budget exhausted")
                    outcome = None
                    latency_ms = 0.0
                trace, found = self._trace_and_evidence(
                    name=b["name"],
                    tool_input=b["input"],
                    result=result,
                    error=err,
                    outcome=outcome,
                    latency_ms=latency_ms,
                    duplicate=duplicate,
                )
                tool_trace.append(trace)
                evidence.extend(found)
                ctx = current_context()
                if ctx is not None:
                    ctx.extra.setdefault("raw_tool_results", []).append({
                        "tool": b["name"], "input": b["input"], "result": result,
                        "error": str(err) if err else None,
                    })
                if isinstance(result, dict) and result.get("receipt_id"):
                    receipts.append(result)
                surface = trace.get("surface")
                if surface and surface not in surfaces_used:
                    surfaces_used.append(surface)
                tool_results.append(self._tool_result_block(b["id"], result, err))
            conv.append({"role": "user", "content": tool_results})

        # Hit the iteration cap.
        return {
            "answer": self._best_effort_answer(evidence),
            "history": conv,
            "iterations": self._cfg.max_tool_turns,
            "stop_reason": "max_iterations",
            "usage": self._usage_dict(
                total_input, total_output, total_cache_read, total_cache_create,
                tool_call_count, resolved_models,
            ),
            "tool_trace": tool_trace,
            "evidence": self._dedupe_evidence(evidence),
            "receipts": receipts,
            "surfaces_used": surfaces_used,
        }

    async def stream_turn(
        self,
        *,
        user_message: str,
        history: list[dict] | None = None,
        final_contract: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Like run_turn but yields AgentEvents as they happen.

        Streams token deltas from the assistant, emits tool_use and
        tool_result events, then a final 'done'.
        """
        conv: list[dict] = list(history or [])
        conv.append({"role": "user", "content": user_message})
        bound_context = current_context()
        if bound_context is not None:
            bound_context.extra["nested_model_usage"] = []
        tool_trace: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        surfaces_used: list[str] = []
        resolved_models: list[str] = []
        successful_calls: dict[str, Any] = {}
        total_input = total_output = total_cache_read = total_cache_create = 0
        tool_call_count = 0
        system_prompt = self._cfg.system_prompt
        contract_prompt = self._contract_prompt(final_contract)
        if contract_prompt:
            system_prompt = f"{system_prompt}\n\n{contract_prompt}".strip()

        for iteration in range(self._cfg.max_tool_turns):
            allow_tools = (
                iteration < self._cfg.max_tool_turns - 1
                and tool_call_count < self._cfg.max_tool_calls
                and total_input < self._cfg.max_input_tokens
            )
            final: ModelResponse | None = None
            async for model_event in self._model_client.stream(
                model=self._cfg.model,
                max_tokens=self._cfg.max_tokens,
                temperature=self._cfg.temperature,
                system=system_prompt or None,
                tools=self._tools.as_anthropic_tools() if allow_tools and len(self._tools) else None,
                tool_choice=None if allow_tools else "none",
                messages=conv,
            ):
                if model_event.type == "text" and model_event.delta:
                    yield AgentEvent(type="text", data={"delta": model_event.delta})
                elif model_event.type == "done":
                    final = model_event.response
            if final is None:
                yield AgentEvent(type="error", data={"message": "model stream ended without a final response"})
                return

            total_input += final.usage.input_tokens
            total_output += final.usage.output_tokens
            total_cache_read += final.usage.cache_read_tokens
            total_cache_create += final.usage.cache_create_tokens
            if final.resolved_model and final.resolved_model not in resolved_models:
                resolved_models.append(final.resolved_model)

            assistant_blocks = [self._block_to_dict(b) for b in final.content]
            conv.append({"role": "assistant", "content": assistant_blocks})

            if final.stop_reason != "tool_use" or not allow_tools:
                if final.stop_reason == "tool_use":
                    fallback = self._best_effort_answer(evidence)
                    yield AgentEvent(type="text", data={"delta": fallback})
                yield AgentEvent(
                    type="done",
                    data={
                        "iterations": iteration + 1,
                        "stop_reason": final.stop_reason if final.stop_reason != "tool_use" else "max_iterations",
                        "usage": self._usage_dict(
                            total_input, total_output, total_cache_read, total_cache_create,
                            tool_call_count, resolved_models,
                        ),
                        "tool_trace": tool_trace,
                        "evidence": self._dedupe_evidence(evidence),
                        "receipts": receipts,
                        "surfaces_used": surfaces_used,
                    },
                )
                return

            tool_results: list[dict] = []
            blocks = [b for b in assistant_blocks if b.get("type") == "tool_use"]
            for b in blocks:
                yield AgentEvent(
                    type="tool_use",
                    data={"name": b["name"], "input": b["input"], "id": b["id"]},
                )
            pending: dict[str, asyncio.Task] = {}
            for b in blocks:
                key = self._call_key(b["name"], b["input"])
                if key in successful_calls or key in pending:
                    continue
                if tool_call_count + len(pending) >= self._cfg.max_tool_calls:
                    continue

                async def _timed_dispatch(block=b):
                    call_started = time.monotonic()
                    dispatched = await self._dispatch_tool(block["name"], block["input"])
                    return (*dispatched, (time.monotonic() - call_started) * 1000)

                pending[key] = asyncio.create_task(_timed_dispatch())
            completed = dict(zip(pending, await asyncio.gather(*pending.values()))) if pending else {}
            for b in blocks:
                key = self._call_key(b["name"], b["input"])
                duplicate = key in successful_calls
                if duplicate:
                    result, err, outcome, latency_ms = successful_calls[key], None, None, 0.0
                elif key in completed:
                    result, err, outcome, latency_ms = completed[key]
                    tool_call_count += 1
                    if err is None and outcome is None:
                        successful_calls[key] = result
                else:
                    result, err, outcome, latency_ms = (
                        None, RuntimeError("tool-call budget exhausted"), None, 0.0
                    )
                trace, found = self._trace_and_evidence(
                    name=b["name"],
                    tool_input=b["input"],
                    result=result,
                    error=err,
                    outcome=outcome,
                    latency_ms=latency_ms,
                    duplicate=duplicate,
                )
                tool_trace.append(trace)
                evidence.extend(found)
                if isinstance(result, dict) and result.get("receipt_id"):
                    receipts.append(result)
                surface = trace.get("surface")
                if surface and surface not in surfaces_used:
                    surfaces_used.append(surface)
                tr = self._tool_result_block(b["id"], result, err)
                tool_results.append(tr)
                # If a hook short-circuited (denied / asks_user), surface it
                # as a separate event so the frontend can render a modal /
                # confirmation UI instead of treating it like a normal error.
                if outcome and outcome.get("kind") == "asks_user":
                    yield AgentEvent(
                        type="hook_asks_user",
                        data={
                            "tool": outcome["tool"],
                            "tool_use_id": b["id"],
                            "prompt": outcome["prompt"],
                            "details": outcome["details"],
                            "confirm_args": outcome["confirm_args"],
                        },
                    )
                elif outcome and outcome.get("kind") == "denied":
                    yield AgentEvent(
                        type="hook_denied",
                        data={
                            "tool": outcome["tool"],
                            "tool_use_id": b["id"],
                            "reason": outcome["reason"],
                        },
                    )
                yield AgentEvent(
                    type="tool_result",
                    data={
                        "name": b["name"],
                        "is_error": bool(err) or self._result_failed(result),
                        "preview": tr["content"][:500] if isinstance(tr["content"], str) else None,
                    },
                )
            conv.append({"role": "user", "content": tool_results})

        yield AgentEvent(
            type="done",
            data={
                "stop_reason": "max_iterations",
                "iterations": self._cfg.max_tool_turns,
                "usage": self._usage_dict(
                    total_input, total_output, total_cache_read, total_cache_create,
                    tool_call_count, resolved_models,
                ),
                "tool_trace": tool_trace,
                "evidence": self._dedupe_evidence(evidence),
                "receipts": receipts,
                "surfaces_used": surfaces_used,
            },
        )


__all__ = ["NativeAgentLoop"]
