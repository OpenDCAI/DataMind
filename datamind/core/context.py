"""Request-scoped context.

Replaces the legacy global `AppState` / `_state` singletons. Every request
(HTTP chat, CLI turn, benchmark sample) builds its own `RequestContext`,
passes it explicitly to whichever code needs it, and drops it when done.

What goes in here:
- identity fields (session_id, user_id)
- runtime knobs that change per-request but not per-tool-call (profile,
  trace_id, permission_mode)
- `extra` dict for ad-hoc attachments (e.g. "last retrieved images" that
  the old code stashed on the global AppState).

What does NOT go in here: model clients, DB engines, indices. Those are
long-lived and live in their own module-level singletons or are created
per-capability by the MCP servers.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RequestContext:
    """Per-request execution context. Build one, pass it around, discard it."""

    session_id: str
    profile: str = "default"
    user_id: str | None = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    # Ad-hoc request-scoped state. Prefer named fields for anything recurring;
    # this is the escape hatch.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, *, profile: str = "default", user_id: str | None = None) -> "RequestContext":
        """Create a context with a fresh session_id."""
        return cls(
            session_id=uuid.uuid4().hex,
            profile=profile,
            user_id=user_id,
        )
