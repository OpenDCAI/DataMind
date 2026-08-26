"""Indexer / splitter tests (no network)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from datamind.capabilities.kb.indexer import (
    _split_text,
    build_index,
    list_documents,
)
from datamind.core.protocols import RetrievedChunk


# -------- minimal in-memory fakes so we don't need Chroma for unit tests ----


class _InMemoryStore:
    dimension = 4

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, dict[str, Any], list[float]]] = {}

    async def add(self, ids, texts, embeddings, metadatas=None):
        metas = list(metadatas or [{} for _ in ids])
        for cid, txt, vec, meta in zip(ids, texts, embeddings, metas):
            self._data[cid] = (txt, dict(meta), list(vec))

    async def query(self, embedding, *, top_k=5, where=None):
        # Not used by indexer tests
        return []

    async def count(self) -> int:
        return len(self._data)

    async def delete(self, ids):
        for i in ids:
            self._data.pop(i, None)

    async def reset(self):
        self._data.clear()

    async def get_all_texts(self):
        return [(cid, txt, meta) for cid, (txt, meta, _) in self._data.items()]


class _FakeEmbed:
    name = "fake"
    dimension = 4

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(len(t)), 0.0, 0.0, 0.0] for t in texts]

    async def embed_query(self, query: str) -> list[float]:
        return [float(len(query)), 0.0, 0.0, 0.0]


# --------------------------------------------------------------- splitter ---


def test_split_text_returns_whole_when_short():
    out = _split_text("hello", chunk_size=100, chunk_overlap=10)
    assert out == ["hello"]


def test_split_text_respects_paragraph_boundaries():
    text = "Para1 line.\n\nPara2 line.\n\nPara3 line."
    out = _split_text(text, chunk_size=20, chunk_overlap=0)
    assert all(len(c) <= 20 for c in out)
    joined = " ".join(out)
    assert "Para1" in joined and "Para3" in joined


def test_split_text_rejects_bad_overlap():
    with pytest.raises(ValueError):
        _split_text("abc", chunk_size=10, chunk_overlap=10)


def test_split_text_hard_cuts_long_paragraph():
    text = "x" * 1000
    out = _split_text(text, chunk_size=200, chunk_overlap=50)
    assert all(len(c) <= 200 for c in out)
    assert sum(len(c) for c in out) >= 1000  # overlap adds chars


# ---------------------------------------------------------------- build_index


@pytest.mark.asyncio
async def test_build_index_from_raw_markdown(tmp_path):
    data_dir = tmp_path / "profile"
    data_dir.mkdir()
    (data_dir / "doc1.md").write_text(
        "# Title\n\nFirst paragraph of a test document.\n\nSecond paragraph here.",
        encoding="utf-8",
    )

    store = _InMemoryStore()
    stats = await build_index(
        data_dir=data_dir,
        vector_store=store,
        embedding=_FakeEmbed(),
        chunk_size=128,
        chunk_overlap=16,
    )

    assert stats["raw_chunks"] >= 1
    assert stats["total_embedded"] == stats["raw_chunks"]
    assert await store.count() == stats["total_embedded"]


@pytest.mark.asyncio
async def test_build_index_assigns_unique_stable_ids_to_repeated_raw_chunks(tmp_path):
    data_dir = tmp_path / "profile"
    data_dir.mkdir()
    (data_dir / "repeated.md").write_text(
        "repeat\n\nrepeat", encoding="utf-8"
    )

    store = _InMemoryStore()
    stats = await build_index(
        data_dir=data_dir,
        vector_store=store,
        embedding=_FakeEmbed(),
        chunk_size=8,
        chunk_overlap=0,
    )
    first_ids = set(store._data)

    assert stats["raw_chunks"] == 2
    assert len(first_ids) == 2

    await build_index(
        data_dir=data_dir,
        vector_store=store,
        embedding=_FakeEmbed(),
        chunk_size=8,
        chunk_overlap=0,
    )
    assert set(store._data) == first_ids


@pytest.mark.asyncio
async def test_build_index_pre_chunked_jsonl(tmp_path):
    data_dir = tmp_path / "profile"
    (data_dir / "chunks").mkdir(parents=True)
    (data_dir / "chunks" / "a.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"id": "1", "text": "chunk one", "source": "a.md"}),
                json.dumps({"text": "chunk two", "source": "b.md"}),
                "",  # blank line skipped
                "not json",  # bad line skipped
            ]
        ),
        encoding="utf-8",
    )

    store = _InMemoryStore()
    stats = await build_index(
        data_dir=data_dir,
        vector_store=store,
        embedding=_FakeEmbed(),
        chunk_size=64,
        chunk_overlap=8,
    )
    assert stats["pre_chunked"] == 2
    assert await store.count() == 2


@pytest.mark.asyncio
async def test_list_documents(tmp_path):
    data_dir = tmp_path / "profile"
    (data_dir / "chunks").mkdir(parents=True)
    (data_dir / "chunks" / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (data_dir / "doc.md").write_text("hi", encoding="utf-8")

    docs = await list_documents(data_dir)
    kinds = {d["kind"] for d in docs}
    assert kinds == {"chunks", "docs"}


@pytest.mark.asyncio
async def test_build_index_empty_dir_is_noop(tmp_path):
    data_dir = tmp_path / "empty"
    data_dir.mkdir()
    store = _InMemoryStore()
    stats = await build_index(
        data_dir=data_dir,
        vector_store=store,
        embedding=_FakeEmbed(),
        chunk_size=128,
        chunk_overlap=16,
    )
    assert stats["total_embedded"] == 0
