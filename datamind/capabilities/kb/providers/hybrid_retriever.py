"""Hybrid retriever: BM25 (lexical) + dense (semantic), fused with RRF.

Reciprocal Rank Fusion is simple, hyperparameter-free enough for a default,
and consistently beats either pure-vector or pure-BM25 on most corpora.

We keep the BM25 index in memory. It's rebuilt on startup and can be
refreshed manually via `rebuild_lexical()`. For corpora up to ~1M chunks
this is fine; past that you'd swap in an external BM25 (e.g. Tantivy).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from rank_bm25 import BM25Okapi  # type: ignore

from datamind.core.logging import get_logger
from datamind.core.protocols import EmbeddingProvider, RetrievedChunk, VectorStore
from datamind.core.registry import retriever_registry
from datamind.core.recovery import recover_read
from datamind.capabilities.kb.filters import matches_metadata, validate_metadata_filter

_log = get_logger("retriever.hybrid")


def _tokenize(text: str) -> list[str]:
    """Simple CJK-aware tokeniser: split on non-word runs, keep CJK chars.

    A real deployment would use jieba / lindera / the embedding model's
    tokenizer. This default is good enough for a mixed zh/en corpus and
    has zero extra deps.
    """
    # Lowercase + split on runs of whitespace / punct.
    toks = re.findall(r"[\w一-鿿]+", text.lower())
    # For CJK runs longer than 1, also emit unigrams — helps BM25 on zh.
    out: list[str] = []
    for t in toks:
        out.append(t)
        if re.match(r"[一-鿿]{2,}$", t):
            out.extend(list(t))
    return out


@retriever_registry.register("hybrid")
class HybridRetriever:
    """Vector search + BM25 with Reciprocal Rank Fusion."""

    def __init__(
        self,
        *,
        vector_store: VectorStore,
        embedding: EmbeddingProvider,
        rrf_k: int = 60,
        vector_weight: float = 1.0,
        bm25_weight: float = 1.0,
        candidate_multiplier: int = 3,
        vector_failure_fallback: str = "error",
        vector_retries: int = 0,
        vector_timeout_s: float | None = None,
    ) -> None:
        self._store = vector_store
        self._embed = embedding
        self._rrf_k = rrf_k
        self._vw = vector_weight
        self._bw = bm25_weight
        self._cm = candidate_multiplier
        if vector_failure_fallback not in {"error", "bm25"}:
            raise ValueError("vector_failure_fallback must be error or bm25")
        if not 0 <= vector_retries <= 5:
            raise ValueError("vector_retries must be between 0 and 5")
        if vector_timeout_s is not None and vector_timeout_s <= 0:
            raise ValueError("vector_timeout_s must be positive")
        self._fallback = vector_failure_fallback
        self._vector_retries = vector_retries
        self._vector_timeout = vector_timeout_s
        # Lazy-built on first call; rebuildable.
        self._bm25: BM25Okapi | None = None
        self._ids: list[str] = []
        self._texts: list[str] = []
        self._metas: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def _ensure_lexical(self) -> None:
        if self._bm25 is not None:
            return
        async with self._lock:
            if self._bm25 is not None:
                return
            records = await self._store.get_all_texts()
            if not records:
                self._bm25 = BM25Okapi([[""]])  # empty-safe
                self._ids, self._texts, self._metas = [], [], []
                return
            self._ids = [r[0] for r in records]
            self._texts = [r[1] for r in records]
            self._metas = [r[2] for r in records]
            tokenised = [_tokenize(t) for t in self._texts]
            self._bm25 = BM25Okapi(tokenised)
            _log.info("bm25_built", extra={"docs": len(self._ids)})

    async def _ensure_lexical_at_version(
        self, version: int | None = None,
    ) -> tuple[BM25Okapi, list[str], list[str], list[dict[str, Any]]]:
        async with self._lock:
            records = await self._store.get_all_texts(version=version)
            if not records:
                _bm25 = BM25Okapi([[""]])  # empty-safe
                _ids, _texts, _metas = [], [], []
            else:
                _ids = [r[0] for r in records]
                _texts = [r[1] for r in records]
                _metas = [r[2] for r in records]
                tokenised = [_tokenize(t) for t in _texts]
                _bm25 = BM25Okapi(tokenised)
            return _bm25, _ids, _texts, _metas

    async def rebuild_lexical(self) -> None:
        """Force a rebuild after adding documents to the vector store."""
        async with self._lock:
            self._bm25 = None
        await self._ensure_lexical()

    async def aretrieve(
        self, query: str, *, top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        return await self._retrieve(query, top_k=top_k, filters=filters, version=None)

    async def aretrieve_at_version(
        self, query: str, *, top_k: int = 5,
        filters: dict[str, Any] | None = None, version: int | None = None,
    ) -> list[RetrievedChunk]:
        return await self._retrieve(query, top_k=top_k, filters=filters, version=version)

    async def _retrieve(self, query, *, top_k, filters, version):
        validate_metadata_filter(filters)
        if top_k < 1:
            raise ValueError("top_k must be positive")
        if version is None:
            await self._ensure_lexical()
            bm25, ids, texts, metas = self._bm25, self._ids, self._texts, self._metas
        else:
            # Lexical and vector reads must use exactly the same historical view.
            bm25, ids, texts, metas = await self._ensure_lexical_at_version(version)
        k_inner = top_k * self._cm
        stage = "kb.embedding"

        async def vector_search():
            nonlocal stage
            stage = "kb.embedding"
            vec = await self._embed.embed_query(query)
            stage = "kb.vector_query"
            kwargs = {"top_k": k_inner, "where": filters}
            if version is not None:
                kwargs["version"] = version
            return await self._store.query(vec, **kwargs)

        def rank_lexical():
            if bm25 is not None and ids:
                scores = bm25.get_scores(_tokenize(query))
                eligible = [i for i in range(len(scores)) if matches_metadata(metas[i], filters)]
                order = sorted(eligible, key=lambda i: -scores[i])[:k_inner]
                return {
                    ids[i]: (rank, RetrievedChunk(
                        id=ids[i], text=texts[i], score=float(scores[i]),
                        source=metas[i].get("source"), metadata=metas[i],
                    )) for rank, i in enumerate(order) if scores[i] > 0
                }
            else:
                return {}

        fallback_ranked = {}
        async def lexical_fallback():
            nonlocal fallback_ranked
            fallback_ranked = rank_lexical()
            return None  # Distinct from a successful vector search returning [].

        vec_hits = await recover_read(
            vector_search, component=lambda: stage, retries=self._vector_retries,
            attempt_timeout_s=self._vector_timeout,
            fallback=lexical_fallback if self._fallback == "bm25" else None,
            fallback_method="bm25",
        )
        degraded = vec_hits is None
        vec_ranked = {ch.id: (rank, ch) for rank, ch in enumerate(vec_hits or [])}
        bm25_ranked = fallback_ranked if degraded else rank_lexical()
        fused: dict[str, tuple[float, RetrievedChunk]] = {}
        # Lexical fallback remains useful even if lexical fusion was disabled.
        lexical_weight = 1.0 if degraded else self._bw
        for ranked, weight in ((vec_ranked, self._vw), (bm25_ranked, lexical_weight)):
            for cid, (rank, ch) in ranked.items():
                score = weight / (self._rrf_k + rank + 1)
                if cid in fused:
                    previous, old = fused[cid]
                    fused[cid] = (previous + score, old)
                else:
                    fused[cid] = (score, ch)
        out = [RetrievedChunk(
            id=ch.id, text=ch.text, score=float(score), source=ch.source,
            metadata={**ch.metadata, **({"degraded": True, "retrieval_method": "bm25"} if degraded else {})},
        ) for score, ch in sorted(fused.values(), key=lambda item: -item[0])[:top_k]]
        _log.info("retrieved", extra={"top_k": top_k, "vec_hits": len(vec_ranked),
                  "bm25_hits": len(bm25_ranked), "fused": len(out),
                  "version": version, "degraded": degraded})
        return out
