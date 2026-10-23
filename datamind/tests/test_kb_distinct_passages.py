"""Duplicate locations must not crowd different evidence out of top-k."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from datamind.capabilities.kb.service import KBService
from datamind.config import RetrievalConfig
from datamind.core.protocols import RetrievedChunk


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [False, True])
async def test_dedup_keeps_sources_filters_and_historical_version(historical):
    class Retriever:
        seen = None
        async def aretrieve(self, query, **kwargs):
            self.seen = kwargs
            return [RetrievedChunk(id=str(i), text="same record" if i < 3 else "different record",
                score=1 - i / 10, source=f"path/{i}.md",
                metadata={"source_id": str(i), "source_version": "sha256:old"}) for i in range(4)]
        aretrieve_at_version = aretrieve
    class Store:
        name = "lancedb"
        async def list_versions(self):
            return [{"version": 7}]
    retriever = Retriever()
    kb = KBService(embedding=SimpleNamespace(), vector_store=Store(), retriever=retriever,
                   data_dir=Path("."), retrieval_cfg=RetrievalConfig())
    search = kb.search_at_version if historical else kb.search
    results = await search("record", top_k=2, filters={"tenant": "a"}, **({"version": 7} if historical else {}))
    assert [r["text"] for r in results] == ["same record", "different record"]
    assert [r["source"] for r in results[0]["source_locations"]] == ["path/0.md", "path/1.md", "path/2.md"]
    assert retriever.seen["filters"] == {"tenant": "a"}
    if historical:
        assert retriever.seen["version"] == 7
    # The MCP's 50-result cap must not become a breaking limit on Python callers.
    results = await search("record", top_k=75, **({"version": 7} if historical else {}))
    assert len(results) == 2 and retriever.seen["top_k"] >= 75
