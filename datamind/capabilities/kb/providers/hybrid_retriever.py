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
    ) -> None:
        self._store = vector_store
        self._embed = embedding
        self._rrf_k = rrf_k
        self._vw = vector_weight
        self._bw = bm25_weight
        self._cm = candidate_multiplier
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

    async def rebuild_lexical(self) -> None:
        """Force a rebuild after adding documents to the vector store."""
        async with self._lock:
            self._bm25 = None
        await self._ensure_lexical()

    async def aretrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        validate_metadata_filter(filters)
        await self._ensure_lexical()
        k_inner = top_k * self._cm

        # Vector side
        vec = await self._embed.embed_query(query)
        vec_hits = await self._store.query(vec, top_k=k_inner, where=filters)
        vec_ranked = {ch.id: (rank, ch) for rank, ch in enumerate(vec_hits)}

        # BM25 must use the exact same candidate scope as the vector branch.
        # Filtering after fusion would allow an out-of-scope lexical hit to
        # displace a valid vector candidate.
        if self._bm25 is not None and self._ids:
            scores = self._bm25.get_scores(_tokenize(query))
            eligible = [
                i for i in range(len(scores))
                if matches_metadata(self._metas[i], filters)
            ]
            order = sorted(eligible, key=lambda i: -scores[i])[:k_inner]
            bm25_ranked = {
                self._ids[i]: (
                    rank,
                    RetrievedChunk(
                        id=self._ids[i],
                        text=self._texts[i],
                        score=float(scores[i]),
                        source=self._metas[i].get("source"),
                        metadata=self._metas[i],
                    ),
                )
                for rank, i in enumerate(order)
                if scores[i] > 0
            }
        else:
            bm25_ranked = {}

        # Reciprocal Rank Fusion
        fused: dict[str, tuple[float, RetrievedChunk]] = {}
        for idmap, weight in ((vec_ranked, self._vw), (bm25_ranked, self._bw)):
            for cid, (rank, ch) in idmap.items():
                contrib = weight / (self._rrf_k + rank + 1)
                if cid in fused:
                    prev_score, prev_ch = fused[cid]
                    fused[cid] = (prev_score + contrib, prev_ch)
                else:
                    fused[cid] = (contrib, ch)

        top = sorted(fused.values(), key=lambda sc: -sc[0])[:top_k]
        # Preserve fused score so callers can reason about it.
        out = [
            RetrievedChunk(
                id=ch.id,
                text=ch.text,
                score=float(score),
                source=ch.source,
                metadata=ch.metadata,
            )
            for score, ch in top
        ]
        _log.info(
            "retrieved",
            extra={
                "top_k": top_k,
                "vec_hits": len(vec_ranked),
                "bm25_hits": len(bm25_ranked),
                "fused": len(out),
            },
        )
        return out
