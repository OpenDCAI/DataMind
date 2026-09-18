from __future__ import annotations

import pytest

from datamind.capabilities.kb.providers.chroma_store import ChromaVectorStore
from datamind.core.errors import CapabilityError


@pytest.mark.asyncio
async def test_empty_vector_index_is_a_valid_empty_result(tmp_path):
    store = ChromaVectorStore(
        persist_dir=tmp_path, collection_name="empty-index", dimension=2,
    )

    assert await store.count() == 0
    assert await store.query([1.0, 0.0], top_k=5) == []


def test_corrupt_vector_index_is_not_reported_as_empty(tmp_path):
    (tmp_path / "chroma.sqlite3").write_bytes(b"not a sqlite database")

    with pytest.raises(CapabilityError, match="cannot open Chroma vector index"):
        ChromaVectorStore(
            persist_dir=tmp_path, collection_name="broken-index", dimension=2,
        )
