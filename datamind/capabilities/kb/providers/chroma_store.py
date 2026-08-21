"""Chroma-backed vector store provider."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Sequence

from datamind.core.logging import get_logger
from datamind.core.protocols import RetrievedChunk
from datamind.core.registry import vector_store_registry

_log = get_logger("vector_store.chroma")


@vector_store_registry.register("chroma")
class ChromaVectorStore:
    """Persistent Chroma collection, one per (profile, collection) tuple."""

    def __init__(
        self,
        *,
        persist_dir: str | Path,
        collection_name: str,
        dimension: int,
    ) -> None:
        import chromadb  # type: ignore

        self.dimension = dimension
        self._persist_dir = Path(persist_dir)
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._collection_name = collection_name
        self._client = chromadb.PersistentClient(path=str(self._persist_dir))
        # We supply our own embeddings — disable the default model download.
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            embedding_function=None,  # type: ignore[arg-type]
            metadata={"hnsw:space": "cosine"},
        )
        self.existing_count = int(self._collection.count())
        _log.info(
            "chroma_collection_ready",
            extra={
                "collection": collection_name,
                "path": str(self._persist_dir),
                "count": self._collection.count(),
            },
        )

    # --------------------------------------------------------- public (async)

    async def add(
        self,
        ids: Sequence[str],
        texts: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        if not ids:
            return
        metas = list(metadatas) if metadatas else [{} for _ in ids]
        # Chroma rejects empty dicts in some versions — replace with sentinel.
        metas = [m if m else {"_": 1} for m in metas]
        await asyncio.to_thread(
            self._collection.upsert,
            ids=list(ids),
            documents=list(texts),
            embeddings=[list(v) for v in embeddings],
            metadatas=metas,
        )
        self.existing_count = int(self._collection.count())

    async def query(
        self,
        embedding: Sequence[float],
        *,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        result = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=[list(embedding)],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]

        out: list[RetrievedChunk] = []
        for i, (cid, doc, meta, dist) in enumerate(zip(ids, docs, metas, dists)):
            meta = dict(meta or {})
            # Cosine distance -> cosine similarity (higher is better)
            score = float(1.0 - dist) if dist is not None else 0.0
            source = meta.pop("_source", None) or meta.pop("source", None)
            out.append(
                RetrievedChunk(
                    id=str(cid),
                    text=doc or "",
                    score=score,
                    source=source,
                    metadata={k: v for k, v in meta.items() if k != "_"},
                )
            )
        return out

    async def count(self) -> int:
        return await asyncio.to_thread(self._collection.count)

    async def delete(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        await asyncio.to_thread(self._collection.delete, ids=list(ids))
        self.existing_count = int(self._collection.count())

    async def reset(self) -> None:
        await asyncio.to_thread(
            self._client.delete_collection, name=self._collection_name
        )
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            embedding_function=None,  # type: ignore[arg-type]
            metadata={"hnsw:space": "cosine"},
        )
        self.existing_count = 0

    def create_staging_store(self) -> "ChromaVectorStore":
        return ChromaVectorStore(
            persist_dir=self._persist_dir,
            collection_name=f"{self._collection_name}__staging_{uuid.uuid4().hex[:10]}",
            dimension=self.dimension,
        )

    async def discard(self) -> None:
        """Delete this collection; used for failed staging builds."""
        await asyncio.to_thread(
            self._client.delete_collection, name=self._collection.name
        )

    async def activate_staging(
        self,
        staging: "ChromaVectorStore",
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Atomically swap a fully-built staging collection into service.

        Chroma has no transaction spanning collections, so names are swapped
        with a rollback guard.  The old collection is deleted only after the
        staging collection has acquired the canonical name.
        """
        if staging._persist_dir.resolve() != self._persist_dir.resolve():
            raise ValueError("staging collection must use the same Chroma database")
        main_name = self._collection_name
        backup_name = f"{main_name}__backup_{uuid.uuid4().hex[:10]}"
        old = self._collection
        new = staging._collection
        await asyncio.to_thread(old.modify, name=backup_name)
        try:
            # Distance configuration is immutable in Chroma. Renaming keeps
            # the staging collection's cosine configuration; user metadata
            # must not attempt to restate/change it during the swap.
            await asyncio.to_thread(
                new.modify, name=main_name, metadata=(metadata or None)
            )
        except Exception:
            await asyncio.to_thread(old.modify, name=main_name)
            raise
        self._collection = new
        self.existing_count = int(new.count())
        staging._collection = new
        await asyncio.to_thread(self._client.delete_collection, name=backup_name)

    async def aclose(self) -> None:
        # chromadb.PersistentClient owns no documented async close method.
        # Keep the lifecycle contract explicit for interchangeable providers.
        return None

    async def get_all_texts(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Enumerate (id, text, metadata) — used by lexical retrievers."""
        result = await asyncio.to_thread(
            self._collection.get, include=["documents", "metadatas"]
        )
        ids = result.get("ids") or []
        docs = result.get("documents") or []
        metas = result.get("metadatas") or []
        out: list[tuple[str, str, dict[str, Any]]] = []
        for cid, doc, meta in zip(ids, docs, metas):
            meta = dict(meta or {})
            meta.pop("_", None)
            out.append((str(cid), doc or "", meta))
        return out
