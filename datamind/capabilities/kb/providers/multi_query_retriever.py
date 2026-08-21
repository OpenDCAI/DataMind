"""Multi-query retriever.

Uses the LLM to rewrite the user's question into N semantically-diverse
sub-queries, retrieves top-k for each, then merges with dedup. This helps
when the user's phrasing doesn't match the corpus vocabulary.

We use the same protocol-neutral client as the agent loop. The model is
configurable — small/cheap is fine for rewriting.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from datamind.core.logging import get_logger
from datamind.core.protocols import EmbeddingProvider, RetrievedChunk, TextModelClient, VectorStore
from datamind.core.registry import retriever_registry

_log = get_logger("retriever.multi_query")

_REWRITE_PROMPT = """You rewrite user questions into {n} alternative search queries \
that would retrieve relevant documents. Each alternative should approach the question from \
a different angle (synonyms, related concepts, narrower/broader scope). Respond with a JSON \
array of strings only, no other text.

User question: {query}"""


@retriever_registry.register("multi_query")
class MultiQueryRetriever:
    def __init__(
        self,
        *,
        vector_store: VectorStore,
        embedding: EmbeddingProvider,
        llm_client: TextModelClient,
        llm_model: str,
        num_queries: int = 3,
    ) -> None:
        self._store = vector_store
        self._embed = embedding
        self._llm = llm_client
        self._model = llm_model
        self._n = num_queries

    async def aretrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        subqueries = await self._rewrite(query)
        subqueries = [query, *subqueries][: self._n + 1]

        # Fan out: embed + query for each subquery in parallel.
        vecs = await self._embed.embed_texts(subqueries)
        results = await asyncio.gather(
            *(
                self._store.query(v, top_k=top_k, where=filters)
                for v in vecs
            )
        )
        merged = self._merge(results)
        _log.info(
            "retrieved",
            extra={
                "subqueries": subqueries,
                "top_k": top_k,
                "merged": len(merged),
            },
        )
        return merged[:top_k]

    # -------------------------------------------------------------- private

    async def _rewrite(self, query: str) -> list[str]:
        prompt = _REWRITE_PROMPT.format(n=self._n, query=query)
        try:
            text = (await self._llm.generate_text(
                prompt, model=self._model, max_tokens=512, temperature=0.0,
            )).strip()
            # Accept both a raw JSON array and a fenced block.
            m = re.search(r"\[.*\]", text, re.DOTALL)
            raw = m.group(0) if m else text
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(s) for s in parsed if isinstance(s, (str, int, float))][: self._n]
        except Exception as exc:  # noqa: BLE001 — fall back to original query
            _log.warning("rewrite_failed", extra={"err": repr(exc)})
        return []

    @staticmethod
    def _merge(results: list[list[RetrievedChunk]]) -> list[RetrievedChunk]:
        """Dedup by id, keep highest score."""
        by_id: dict[str, RetrievedChunk] = {}
        for group in results:
            for ch in group:
                prev = by_id.get(ch.id)
                if prev is None or ch.score > prev.score:
                    by_id[ch.id] = ch
        return sorted(by_id.values(), key=lambda c: c.score, reverse=True)
