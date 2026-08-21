"""KB service: wires EmbeddingProvider + VectorStore + Retriever from Settings.

This is the thing agent code / MCP servers / tool handlers grab. It's
intentionally stateful but scoped to a single profile + collection, so
swapping profiles means building a new service (cheap — chroma is
persistent).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from datamind.capabilities.embedding import build_embedding
from datamind.config import LLMConfig, RetrievalConfig, Settings
from datamind.core.errors import ConfigError
from datamind.core.logging import get_logger
from datamind.core.protocols import EmbeddingProvider, Retriever, TextModelClient, VectorStore
from datamind.core.registry import retriever_registry, vector_store_registry

# Importing providers populates the registries.
from . import providers  # noqa: F401
from .indexer import (
    build_index,
    build_index_manifest,
    corpus_fingerprint,
    list_documents,
    write_manifest_atomic,
)

_log = get_logger("kb.service")


class KBService:
    """Bundle of (embedding, vector store, retriever) scoped to one profile."""

    def __init__(
        self,
        *,
        embedding: EmbeddingProvider,
        vector_store: VectorStore,
        retriever: Retriever,
        data_dir: Path,
        retrieval_cfg: RetrievalConfig,
        manifest_path: Path | None = None,
        manifest_base: dict[str, Any] | None = None,
    ) -> None:
        self.embedding = embedding
        self.vector_store = vector_store
        self.retriever = retriever
        self.data_dir = data_dir
        self.retrieval_cfg = retrieval_cfg
        self.manifest_path = manifest_path
        self._manifest_base = manifest_base or {}
        self._compatibility_error: str | None = None
        if manifest_path and manifest_path.is_file():
            try:
                actual = json.loads(manifest_path.read_text(encoding="utf-8"))
                for key, expected in self._manifest_base.items():
                    if actual.get(key) != expected:
                        self._compatibility_error = (
                            f"index fingerprint mismatch for {key}: "
                            f"stored={actual.get(key)!r}, configured={expected!r}; run explicit reindex"
                        )
                        break
            except (OSError, json.JSONDecodeError) as exc:
                self._compatibility_error = f"invalid KB index manifest: {exc}; run explicit reindex"
        elif manifest_path and int(getattr(vector_store, "existing_count", 0) or 0) > 0:
            self._compatibility_error = (
                "existing KB index has no compatibility manifest; run explicit reindex"
            )

    # ------------------------------------------------------------ behaviour

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if self._compatibility_error:
            raise ConfigError(self._compatibility_error)
        k = top_k or self.retrieval_cfg.top_k
        chunks = await self.retriever.aretrieve(query, top_k=k, filters=filters)
        return [c.model_dump() for c in chunks]

    async def count(self) -> int:
        return await self.vector_store.count()

    async def reindex(self) -> dict[str, Any]:
        staging_factory = getattr(self.vector_store, "create_staging_store", None)
        staging = staging_factory() if callable(staging_factory) else None
        target = staging or self.vector_store
        activated = False
        staged_manifest_path: Path | None = None
        try:
            if staging is None:
                # Non-staging third-party providers retain their historical
                # behaviour; the built-in Chroma path is transactional.
                await target.reset()
            stats = await build_index(
                data_dir=self.data_dir,
                vector_store=target,
                embedding=self.embedding,
                chunk_size=self.retrieval_cfg.chunk_size,
                chunk_overlap=self.retrieval_cfg.chunk_overlap,
            )
            manifest = build_index_manifest(
                data_dir=self.data_dir,
                embedding_provider=str(self._manifest_base.get("embedding_provider", self.embedding.name)),
                embedding_model=str(self._manifest_base.get("embedding_model", "unknown")),
                dimension=int(self.embedding.dimension),
                chunk_size=self.retrieval_cfg.chunk_size,
                chunk_overlap=self.retrieval_cfg.chunk_overlap,
            )
            if self.manifest_path:
                staged_manifest_path = self.manifest_path.with_name(
                    f".{self.manifest_path.name}.staging"
                )
                write_manifest_atomic(staged_manifest_path, manifest)
            if staging is not None:
                await self.vector_store.activate_staging(
                    staging, metadata={"datamind:fingerprint": manifest["fingerprint"]},
                )
                activated = True
            if self.manifest_path and staged_manifest_path:
                os.replace(staged_manifest_path, self.manifest_path)
            self._manifest_base = {
                key: manifest[key]
                for key in ("embedding_provider", "embedding_model", "dimension", "chunk_size", "chunk_overlap")
            }
            self._compatibility_error = None
        except Exception:
            if staging is not None and not activated:
                discard = getattr(staging, "discard", None)
                if callable(discard):
                    try:
                        await discard()
                    except Exception:  # noqa: BLE001 - keep the build error
                        pass
            if staged_manifest_path and staged_manifest_path.exists():
                staged_manifest_path.unlink(missing_ok=True)
            raise
        # If the retriever keeps a lexical cache (hybrid), nudge it.
        rebuild = getattr(self.retriever, "rebuild_lexical", None)
        if callable(rebuild):
            await rebuild()
        return stats

    async def aclose(self) -> None:
        close = getattr(self.vector_store, "aclose", None)
        if callable(close):
            await close()

    async def list_documents(self) -> list[dict[str, Any]]:
        return await list_documents(self.data_dir)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_kb_service(
    settings: Settings,
    *,
    llm_client: TextModelClient | None = None,
    collection_name: str = "kb_default",
    embedding: EmbeddingProvider | None = None,
) -> KBService:
    """Build a KBService from the top-level Settings.

    `llm_client` is only consulted by the multi_query strategy; callers
    that don't use it can pass None.
    """
    embedding = embedding or build_embedding(settings.embedding, fallback_llm=settings.llm)
    storage_dir = settings.data.storage_dir
    storage_dir.mkdir(parents=True, exist_ok=True)
    vector_store = vector_store_registry.create(
        "chroma",
        persist_dir=str(storage_dir / "chroma"),
        collection_name=collection_name,
        dimension=embedding.dimension,
    )

    strategy = settings.retrieval.strategy
    if settings.retrieval.rerank:
        raise ConfigError(
            "retrieval.rerank=true is not implemented; disable it instead of assuming reranking is active"
        )
    if strategy == "simple":
        retriever = retriever_registry.create(
            "simple", vector_store=vector_store, embedding=embedding,
        )
    elif strategy == "multi_query":
        if llm_client is None:
            raise ConfigError(
                "multi_query retriever needs an llm_client; pass one or "
                "use another strategy via DATAMIND__RETRIEVAL__STRATEGY."
            )
        retriever = retriever_registry.create(
            "multi_query",
            vector_store=vector_store,
            embedding=embedding,
            llm_client=llm_client,
            llm_model=settings.llm.fallback_model or settings.llm.model,
        )
    elif strategy == "hybrid":
        retriever = retriever_registry.create(
            "hybrid", vector_store=vector_store, embedding=embedding,
        )
    else:
        raise ConfigError(
            f"Unknown retrieval strategy '{strategy}'. "
            f"Known: {retriever_registry.known()}"
        )

    return KBService(
        embedding=embedding,
        vector_store=vector_store,
        retriever=retriever,
        data_dir=settings.data.data_dir,
        retrieval_cfg=settings.retrieval,
        manifest_path=storage_dir / "kb_index_manifest.json",
        manifest_base={
            "embedding_provider": settings.embedding.provider,
            "embedding_model": settings.embedding.model,
            "dimension": int(embedding.dimension),
            "chunk_size": settings.retrieval.chunk_size,
            "chunk_overlap": settings.retrieval.chunk_overlap,
            "corpus_hash": corpus_fingerprint(settings.data.data_dir),
        },
    )


__all__ = ["KBService", "build_kb_service"]
