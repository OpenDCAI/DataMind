"""Short-term (per-session) rolling memory.

Simple in-memory FIFO buffer keyed by session id. Not a MemoryStore (no
semantic recall) — this layer just preserves the last N turns verbatim so
the agent has immediate context during a multi-turn conversation.

Thread/async safety: an asyncio.Lock per session guards mutations. Since
each request builds its own context via RequestContext, callers typically
own the lock implicitly. We still guard to be safe under concurrent tool
calls within a single session.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Turn:
    role: str  # "user" | "assistant" | "system"
    content: str
    ts: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "ts": self.ts,
            "metadata": self.metadata,
        }


class ShortTermMemory:
    """Session-keyed rolling window of Turns."""

    def __init__(self, *, max_turns: int = 20) -> None:
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
            raise ValueError("max_turns must be a positive integer")
        self._max = max_turns
        self._buffers: dict[str, deque[Turn]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def append(self, session_id: str, role: str, content: str, **metadata: Any) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if role not in {"user", "assistant", "system"}:
            raise ValueError("role must be user, assistant, or system")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        async with self._lock(session_id):
            buf = self._buffers.setdefault(session_id, deque(maxlen=self._max))
            buf.append(Turn(role=role, content=content, metadata=dict(metadata)))

    async def recent(self, session_id: str, *, limit: int | None = None) -> list[Turn]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer")
        async with self._lock(session_id):
            buf = self._buffers.get(session_id)
            if not buf:
                return []
            items = list(buf)
            if limit == 0:
                return []
            if limit is not None:
                items = items[-limit:]
            return items

    async def clear(self, session_id: str) -> int:
        async with self._lock(session_id):
            buf = self._buffers.pop(session_id, None)
            self._locks.pop(session_id, None)
            return len(buf) if buf else 0

    def sessions(self) -> list[str]:
        return list(self._buffers)
