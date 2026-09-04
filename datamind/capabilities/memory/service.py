"""MemoryService — unifies short-term + long-term under one API.

Short-term (in-memory rolling window) is cheap and always-on.
Long-term (MemoryStore, v1.0) uses scope-typed storage:
  - global  : applies everywhere ("respond in Chinese")
  - profile : tenant/project boundary
  - session : single conversation

Recall takes (profile, session_id) and returns the union of three
scope-conditioned top-k retrievals. This is the v1.0 multi-tenant
isolation primitive — see `core/protocols.py::MemoryStore`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Sequence

from datamind.capabilities.embedding import build_embedding
from datamind.config import Settings
from datamind.core.logging import get_logger
from datamind.core.protocols import EmbeddingProvider, MemoryStore, TextModelClient
from datamind.core.registry import memory_registry

# Import providers so registry is populated on module load.
from . import providers  # noqa: F401
from .extractor import extract_facts
from .short_term import ShortTermMemory, Turn

_log = get_logger("memory.service")

Scope = Literal["global", "profile", "session"]
Kind = Literal["preference", "decision", "workflow", "summary", "skill", "fact"]


class MemoryService:
    """Wraps short-term + long-term memory; long-term is scope-typed.

    Most callers pass a `default_profile` (the active tenant) and optional
    `default_session_id` once at construction; per-call `scope` / `profile`
    / `session_id` overrides are still allowed.
    """

    def __init__(
        self,
        *,
        short_term: ShortTermMemory,
        long_term: MemoryStore,
        default_profile: str | None = None,
        llm_client: TextModelClient | None = None,
        llm_model: str | None = None,
    ) -> None:
        self.short_term = short_term
        self.long_term = long_term
        self._default_profile = default_profile
        self._llm = llm_client
        self._model = llm_model

    # ----------------------------------------------------------- short-term

    async def append_turn(self, session_id: str, role: str, content: str, **metadata: Any) -> None:
        await self.short_term.append(session_id, role, content, **metadata)

    async def recent_turns(self, session_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        return [t.to_dict() for t in await self.short_term.recent(session_id, limit=limit)]

    # ----------------------------------------------------------- long-term

    async def save(
        self,
        content: str,
        *,
        scope: Scope = "profile",
        profile: str | None = None,
        session_id: str | None = None,
        kind: Kind = "fact",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        return await self.long_term.save(
            content,
            scope=scope,
            profile=profile or (self._default_profile if scope == "profile" else None),
            session_id=session_id,
            kind=kind,
            metadata=metadata,
        )

    async def recall(
        self,
        query: str,
        *,
        profile: str | None = None,
        session_id: str | None = None,
        top_k: int = 8,
        kinds: Sequence[str] | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        hits = await self.long_term.recall(
            query,
            profile=profile or self._default_profile,
            session_id=session_id,
            top_k=top_k,
            kinds=kinds,
            include_archived=include_archived,
        )
        return [h.model_dump() for h in hits]

    async def forget(self, item_id: str, *, hard: bool = False) -> bool:
        return await self.long_term.forget(item_id, hard=hard)

    async def list_profiles(self) -> list[str]:
        return await self.long_term.list_profiles()

    # -------------------------------------------------------- fact extraction

    async def extract_and_save(
        self,
        *,
        user_turn: str,
        assistant_turn: str,
        scope: Scope = "session",
        profile: str | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        """Pull facts from a turn pair using the fallback LLM, persist them.

        Defaults to scope='session' because per-turn extractions are most
        valuable bounded to the current conversation; callers that want
        cross-session retention should pass scope='profile' or 'global'.
        """
        if not self._llm or not self._model:
            return []
        facts = await extract_facts(
            client=self._llm,
            model=self._model,
            user_turn=user_turn,
            assistant_turn=assistant_turn,
        )
        for f in facts:
            await self.save(
                f,
                scope=scope,
                profile=profile,
                session_id=session_id,
                kind="fact",
                metadata={"_source": "auto_extracted"},
            )
        return facts


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_memory_service(
    settings: Settings,
    *,
    llm_client: TextModelClient | None = None,
    embedding: EmbeddingProvider | None = None,
) -> MemoryService:
    # Short-term
    st = ShortTermMemory(max_turns=settings.memory.short_term_turns)

    # Long-term
    backend = settings.memory.backend
    kwargs: dict[str, Any] = {}
    if settings.memory.dsn:
        kwargs["dsn"] = settings.memory.dsn
    else:
        # Per-profile default file.
        kwargs["db_path"] = str(settings.data.storage_dir / "memory.db")

    # Embedding (optional — recall still works lexically without it)
    has_creds = bool(settings.embedding.api_key or settings.llm.api_key)
    if embedding is None and settings.memory.long_term_enabled and has_creds:
        try:
            embedding = build_embedding(settings.embedding, fallback_llm=settings.llm)
        except Exception as exc:  # noqa: BLE001
            _log.warning("memory_embedding_disabled", extra={"err": repr(exc)})
    kwargs["embedding"] = embedding

    long_term = memory_registry.create(backend, **kwargs)

    model = settings.llm.fallback_model or settings.llm.model if llm_client else None
    return MemoryService(
        short_term=st,
        long_term=long_term,
        default_profile=settings.data.profile,
        llm_client=llm_client,
        llm_model=model,
    )


__all__ = ["MemoryService", "build_memory_service", "Turn"]
