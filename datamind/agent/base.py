"""Shared types between agent loop implementations.

Two concrete loops live alongside:
- `loop_native.py` — `NativeAgentLoop` on the `anthropic` SDK
- `loop_sdk.py`    — `SdkAgentLoop` on `claude-agent-sdk` (via CCR)

Both emit the same `AgentEvent` stream and speak `AgentLoopProtocol` so
the server / CLI / frontend are agnostic to which is active.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable


# ---------------------------------------------------------- hook signatures


# PreToolUse: called before a tool runs. Raise to abort, return to allow.
OnToolStart = Callable[[str, dict], Awaitable[None]]
# PostToolUse: called after — `error` is set if the tool raised.
OnToolEnd = Callable[[str, dict, Any, Exception | None], Awaitable[None]]


# ---------------------------------------------------------------- events ---


@dataclass
class AgentEvent:
    """Something observable during a turn.

    `type` is one of:
        "text"         — a text delta from the assistant (`data["delta"]`)
        "tool_use"     — the agent just invoked a tool
                         (`data["name"]`, `data["input"]`, `data["id"]`)
        "tool_result"  — the tool finished
                         (`data["name"]`, `data["is_error"]`,
                          `data["preview"]`  — truncated string)
        "error"        — unrecoverable loop error (`data["message"]`)
        "done"         — end of turn (`data["iterations"]`,
                         `data["stop_reason"]`, `data["usage"]`)
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------- loop config


@dataclass
class AgentLoopConfig:
    """Inputs to a concrete loop. Renamed from `AgentConfig` so it doesn't
    collide with `datamind.config.AgentConfig` (the Settings sub-model)."""

    model: str
    max_tokens: int = 4096
    temperature: float = 1.0
    system_prompt: str = ""
    max_tool_turns: int = 10  # hard cap on iterations to prevent runaways


# ---------------------------------------------------------------- protocol


@runtime_checkable
class AgentLoopProtocol(Protocol):
    """The contract every loop implementation must satisfy.

    Both `NativeAgentLoop` and `SdkAgentLoop` implement this. Agent assembly
    picks one based on `Settings.agent.backend`; everything downstream
    (server, CLI, frontend) talks through this protocol.
    """

    async def run_turn(
        self,
        *,
        user_message: str,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Run one user turn to completion.

        Returns a dict with at least:
            answer: str        — final assistant text
            history: list      — updated conversation history
            iterations: int    — number of API round-trips used
            stop_reason: str   — "end_turn" | "max_iterations" | ...
            usage: dict        — token counts (implementation-specific keys)
            tool_trace: list   — ordered tool calls made during the turn
            surfaces_used: list — data surfaces touched during the turn
            evidence: list     — normalized retrieval evidence (when available)
            receipts: list     — StoreAgent write receipts (when available)
        """
        ...

    def stream_turn(  # returns AsyncIterator, not coroutine — don't await
        self,
        *,
        user_message: str,
        history: list[dict] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Like run_turn but yields AgentEvents as they happen."""
        ...


__all__ = [
    "AgentEvent",
    "AgentLoopConfig",
    "AgentLoopProtocol",
    "OnToolStart",
    "OnToolEnd",
]
