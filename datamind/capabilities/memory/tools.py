"""Memory tools exposed to the agent (v1.0 scope-typed).

Tools accept an optional `scope` ∈ {global, profile, session}. The active
profile and session_id come from `RequestContext` (injected by the agent
loop) so the agent rarely needs to specify them explicitly.

Backward compatibility: callers that don't pass `scope` get scope='profile'
(the most common case for v0.2 namespace='profile:...' style usage), with
`profile` taken from RequestContext via the closure built in `build_memory_tools`.
"""
from __future__ import annotations

from typing import Any

from datamind.core.context import RequestContext
from datamind.core.logging import current_context
from datamind.core.tools import ToolSpec, tool_provider_registry

from .service import MemoryService


def build_memory_tools(
    memory: MemoryService,
    *,
    request_context: RequestContext | None = None,
) -> list[ToolSpec]:
    """Build memory tools bound to the given request context.

    The agent loop creates one ``MemoryService`` (long-lived) and
    re-builds tools per request with a fresh ``RequestContext`` so that
    `profile` / `session_id` stay accurate across concurrent requests.
    """

    def _ctx() -> tuple[str | None, str | None]:
        context = request_context or current_context()
        if context is None:
            return (None, None)
        return (context.profile, context.session_id)

    async def _save(
        content: str,
        scope: str = "profile",
        kind: str = "fact",
        profile: str | None = None,
        session_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        ctx_profile, ctx_session = _ctx()
        effective_profile = profile or ctx_profile if scope == "profile" else profile
        effective_session = session_id or ctx_session if scope == "session" else session_id
        item_id = await memory.save(
            content,
            scope=scope,  # type: ignore[arg-type]
            profile=effective_profile,
            session_id=effective_session,
            kind=kind,  # type: ignore[arg-type]
            metadata=metadata,
        )
        return {"id": item_id, "scope": scope, "kind": kind}

    async def _recall(
        query: str,
        top_k: int = 8,
        scope_filter: list[str] | None = None,
        kinds: list[str] | None = None,
        profile: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        ctx_profile, ctx_session = _ctx()
        # `scope_filter` lets callers limit to e.g. ["global","profile"];
        # absent ⇒ all three scopes are merged.
        eff_profile = profile if profile is not None else ctx_profile
        eff_session = session_id if session_id is not None else ctx_session
        if scope_filter:
            if "session" not in scope_filter:
                eff_session = None
            if "profile" not in scope_filter:
                eff_profile = None
        hits = await memory.recall(
            query,
            profile=eff_profile,
            session_id=eff_session,
            top_k=top_k,
            kinds=kinds,
        )
        return {"query": query, "count": len(hits), "results": hits}

    async def _forget(item_id: str, hard: bool = False) -> dict:
        ok = await memory.forget(item_id, hard=hard)
        return {"id": item_id, "deleted": ok, "hard": hard}

    async def _list_profiles() -> dict:
        return {"profiles": await memory.list_profiles()}

    return [
        ToolSpec(
            name="memory_save",
            description=(
                "Persist a durable fact/preference/decision to long-term memory. "
                "Choose scope: 'global' for system-wide preferences, 'profile' for "
                "tenant-scoped facts (default), 'session' for ephemeral context. "
                "The current profile and session are auto-injected — pass them "
                "explicitly only when you need to cross boundaries on purpose."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "What to remember."},
                    "scope": {
                        "type": "string",
                        "enum": ["global", "profile", "session"],
                        "default": "profile",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["preference", "decision", "workflow", "summary", "skill", "fact"],
                        "default": "fact",
                    },
                    "profile": {"type": "string", "description": "Override the active profile."},
                    "session_id": {"type": "string", "description": "Override the active session."},
                    "metadata": {"type": "object", "additionalProperties": True},
                },
                "required": ["content"],
            },
            handler=_save,
            metadata={"group": "memory", "surface": "memory", "access": "write"},
        ),
        ToolSpec(
            name="memory_recall",
            description=(
                "Look up relevant memories by semantic similarity, automatically "
                "merging session-, profile-, and global-scope items. Use this when "
                "the user references prior context or stable preferences. "
                "Optional `scope_filter` restricts which scopes contribute (e.g. "
                "['profile','global'] to ignore session memories)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                    "scope_filter": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["global", "profile", "session"]},
                    },
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional kind filter (preference/decision/workflow/...).",
                    },
                    "profile": {"type": "string"},
                    "session_id": {"type": "string"},
                },
                "required": ["query"],
            },
            handler=_recall,
            metadata={"group": "memory", "surface": "memory", "access": "read"},
        ),
        ToolSpec(
            name="memory_forget",
            description=(
                "Soft-delete a memory item by id (it becomes status='archived' "
                "and stops appearing in recall). Pass `hard=true` to remove the "
                "row outright. Obtain ids from memory_recall first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "hard": {"type": "boolean", "default": False},
                },
                "required": ["item_id"],
            },
            handler=_forget,
            metadata={
                "group": "memory",
                "surface": "memory",
                "access": "write",
                "destructive": True,
            },
        ),
        ToolSpec(
            name="memory_list_profiles",
            description="List every profile (tenant) that currently has stored memories.",
            input_schema={"type": "object", "properties": {}},
            handler=_list_profiles,
            metadata={"group": "memory", "surface": "memory", "access": "read"},
        ),
    ]


@tool_provider_registry.register("memory")
class _MemoryToolProvider:
    def build(self, **services: Any) -> list[ToolSpec]:
        m = services.get("memory_service")
        if m is None:
            raise ValueError("memory tool provider requires 'memory_service'")
        ctx = services.get("request_context")
        return build_memory_tools(m, request_context=ctx)


__all__ = ["build_memory_tools"]
