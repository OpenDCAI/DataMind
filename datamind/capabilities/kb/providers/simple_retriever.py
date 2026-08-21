"""Simple vector retriever — just top-k cosine similarity.

This is the baseline every other strategy should beat. Keep it minimal.
"""
from __future__ import annotations

from typing import Any

from datamind.core.logging import get_logger
from datamind.core.protocols import EmbeddingProvider, RetrievedChunk, VectorStore
from datamind.core.registry import retriever_registry
from datamind.capabilities.kb.filters import validate_metadata_filter

_log = get_logger("retriever.simple")


@retriever_registry.register("simple")
class SimpleRetriever:
    """Embed the query, query the vector store, return top-k."""

    def __init__(
        self,
        *,
        vector_store: VectorStore,
        embedding: EmbeddingProvider,
    ) -> None:
        self._store = vector_store
        self._embed = embedding

    async def aretrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        validate_metadata_filter(filters)
        vec = await self._embed.embed_query(query)
        chunks = await self._store.query(vec, top_k=top_k, where=filters)
        _log.info(
            "retrieved",
            extra={"query_len": len(query), "top_k": top_k, "hits": len(chunks)},
        )
        return chunks
