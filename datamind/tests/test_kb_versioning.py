"""Versioning tests for the KB stack.

    IngestService  →  KBService  →  LanceDBVectorStore

Only the embedding provider, retriever, and text model are faked.
Data is injected via `ingest.kb_add_file` — the only path that populates
the KB version list.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

pytest.importorskip("lancedb")

from datamind.capabilities.ingest.service import IngestService
from datamind.capabilities.kb.providers.lancedb_store import LanceDBVectorStore
from datamind.capabilities.kb.service import KBService
from datamind.config import RetrievalConfig
from datamind.core.errors import CapabilityError


# ================================================================ fakes

class _Embedding:
    name = "fake"
    dimension = 4

    async def embed_texts(self, texts):
        if not texts:
            raise AssertionError("empty revisions must not call the embedding provider")
        return [self._embed(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [
            int.from_bytes(digest[i * 4:(i + 1) * 4], "little", signed=False) / 2**32
            for i in range(self.dimension)
        ]
        norm = math.sqrt(sum(x * x for x in values))
        return [x / norm for x in values]


class _Model:
    async def generate_text(self, prompt, **kwargs):
        return "[]"


class _VersionedRetriever:
    """Thin retriever: embed → store.query (with optional version)."""

    def __init__(self, store: LanceDBVectorStore, embed: _Embedding):
        self._store = store
        self._embed = embed

    async def aretrieve(self, query, *, top_k=5, filters=None):
        vec = await self._embed.embed_query(query)
        return await self._store.query(vec, top_k=top_k, where=filters)

    async def aretrieve_at_version(self, query, *, top_k=5, filters=None, version=None):
        vec = await self._embed.embed_query(query)
        return await self._store.query(
            vec, top_k=top_k, where=filters, version=version,
        )


# ================================================================ fixtures

@pytest.fixture
def profile(tmp_path: Path) -> Path:
    p = tmp_path / "profile"
    (p / "uploads").mkdir(parents=True)
    return p


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def embedding() -> _Embedding:
    return _Embedding()


@pytest.fixture
def store(tmp_path: Path) -> LanceDBVectorStore:
    return LanceDBVectorStore(
        persist_dir=tmp_path / "lancedb",
        collection_name="kb",
        dimension=4,
    )


@pytest.fixture
def kb(store, embedding, data_dir) -> KBService:
    return KBService(
        embedding=embedding,
        vector_store=store,
        retriever=_VersionedRetriever(store, embedding),
        data_dir=data_dir,
        retrieval_cfg=RetrievalConfig(),
        manifest_path=None,
        manifest_base=None,
    )


@pytest.fixture
def ingest(kb, profile) -> IngestService:
    return IngestService(
        kb=kb, db=None, graph=None,
        llm_client=_Model(), llm_model="test",
        profile_data_dir=profile,
        chunk_size=512, chunk_overlap=64,
        ingest_mode="direct",
    )


# ================================================================ helpers

def _emb(i: float) -> list[float]:
    return [float(i)] * 4


def _write(profile: Path, name: str, text: str) -> Path:
    p = profile / "uploads" / name
    p.write_text(text, encoding="utf-8")
    return p


def _max_version(store: LanceDBVectorStore) -> int:
    return max(v["version"] for v in store._collection.list_versions())


async def _add_file(ingest: IngestService, profile: Path, name: str, text: str) -> int:
    src = _write(profile, name, text)
    result = await ingest.kb_add_file(path=str(src))
    assert result.get("version") is not None, f"no version recorded: {result}"
    return int(result["version"])


# ================================================================
# Store 层版本原语
# ================================================================

@pytest.mark.asyncio
async def test_store_write_bumps_latest_version(store):
    v0 = store.latest_version
    await store.add(
        ids=["a"], texts=["hello"],
        embeddings=[_emb(1)], metadatas=[{"source": "x.txt"}],
    )
    assert store.latest_version > v0


@pytest.mark.asyncio
async def test_store_rollback_restores_snapshot(store):
    await store.add(
        ids=["a", "b"], texts=["one", "two"],
        embeddings=[_emb(1), _emb(2)],
        metadatas=[{"source": "x.txt"}, {"source": "y.txt"}],
    )
    v_before = _max_version(store)

    await store.delete(["a", "b"])
    assert await store.count() == 0

    result = await store.rollback(v_before)
    assert result["count"] == 2
    assert await store.count() == 2


@pytest.mark.asyncio
async def test_store_rollback_appends_new_version(store):
    await store.add(
        ids=["a"], texts=["x"],
        embeddings=[_emb(1)], metadatas=[{"source": "x.txt"}],
    )
    v1 = _max_version(store)

    await store.delete(["a"])
    v2 = _max_version(store)

    await store.rollback(v1)
    v3 = _max_version(store)

    # rollback created a new version; history untouched
    assert v3 > v2
    assert len(store._collection.list_versions()) >= 3


@pytest.mark.asyncio
async def test_add_version_computes_diff(store):
    await store.add(
        ids=["a", "b"], texts=["one", "two"],
        embeddings=[_emb(1), _emb(2)],
        metadatas=[{"source": "x.txt"}, {"source": "y.txt"}],
    )
    v1 = _max_version(store)

    await store.delete(["b"])
    await store.add(
        ids=["c"], texts=["three"],
        embeddings=[_emb(3)], metadatas=[{"source": "z.txt"}],
    )
    v2 = _max_version(store)

    info = await store.add_version(v2, previous_version=v1)
    assert info is not None
    assert info["version"] == v2
    assert any("y.txt" in s for s in info["msg"]["Delete"])
    assert any("z.txt" in s for s in info["msg"]["Add"])
    assert info["chunks_added"] >= 1
    assert info["chunks_deleted"] >= 1


# ================================================================
# KBService 层版本方法
# ================================================================

@pytest.mark.asyncio
async def test_list_versions_starts_empty(kb):
    assert await kb.list_versions() == []


@pytest.mark.asyncio
async def test_ingest_registers_one_version_per_file(ingest, kb, profile):
    await _add_file(ingest, profile, "a.txt", "alpha")
    assert len(await kb.list_versions()) == 1

    await _add_file(ingest, profile, "b.txt", "beta")
    assert len(await kb.list_versions()) == 2


@pytest.mark.asyncio
async def test_search_at_version_sees_snapshot(ingest, kb, profile):
    """An older version sees only the data that existed then."""
    v1 = await _add_file(ingest, profile, "a.txt", "first doc")
    await _add_file(ingest, profile, "b.txt", "second doc")

    # Current: 2 chunks
    assert await kb.count() == 2

    # v1: only the first file's chunk
    hits = await kb.search_at_version("first doc", top_k=10, version=v1)
    assert len(hits) == 1
    assert "first doc" in hits[0]["text"]


@pytest.mark.asyncio
async def test_search_at_version_respects_filters(ingest, kb, profile):
    await _add_file(ingest, profile, "a.txt", "aaa")
    v = await _add_file(ingest, profile, "b.txt", "bbb")

    hits = await kb.search_at_version(
        "bbb", top_k=10, version=v,
        filters={"source_file": str(profile / "uploads" / "b.txt")},
    )
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_search_at_version_rejects_invalid_version(kb):
    # Non-int
    with pytest.raises(CapabilityError):
        await kb.search_at_version("q", version="3")
    # bool is a subclass of int — must be rejected
    with pytest.raises(CapabilityError):
        await kb.search_at_version("q", version=True)
    # Non-positive
    with pytest.raises(CapabilityError):
        await kb.search_at_version("q", version=0)
    # Unknown
    with pytest.raises(CapabilityError):
        await kb.search_at_version("q", version=99999)


@pytest.mark.asyncio
async def test_rollback_restores_ingested_data(ingest, kb, profile):
    src = _write(profile, "a.txt", "restore me")
    r = await ingest.kb_add_file(path=str(src))
    v1 = int(r["version"])
    assert await kb.count() == 1

    src.unlink()
    await ingest.kb_delete_file(path=str(src))
    assert await kb.count() == 0

    result = await kb.rollback(v1)
    assert result["status"] == "ok"
    assert result["rollback_version"] == v1
    assert result["count"] >= 1
    assert result["new_version"] > v1


@pytest.mark.asyncio
async def test_rollback_preserves_history(ingest, kb, profile):
    v1 = await _add_file(ingest, profile, "a.txt", "one")
    await _add_file(ingest, profile, "b.txt", "two")
    await _add_file(ingest, profile, "c.txt", "three")

    n_before = len(await kb.list_versions())
    await kb.rollback(v1)
    n_after = len(await kb.list_versions())
    assert n_after == n_before + 1


@pytest.mark.asyncio
async def test_rollback_then_search_sees_target(ingest, kb, profile):
    src = _write(profile, "a.txt", "restore me")
    r = await ingest.kb_add_file(path=str(src))
    v1 = int(r["version"])

    src.unlink()
    await ingest.kb_delete_file(path=str(src))
    assert await kb.count() == 0

    await kb.rollback(v1)
    assert len(await kb.search("restore me", top_k=5)) >= 1


@pytest.mark.asyncio
async def test_rollback_rejects_unknown_version(ingest, kb, profile):
    await _add_file(ingest, profile, "a.txt", "x")
    with pytest.raises(CapabilityError):
        await kb.rollback(99999)


# ================================================================
# 契约:只有 ingest 路径追踪版本
# ================================================================

@pytest.mark.asyncio
async def test_store_add_does_not_register_version(kb):
    """Direct store.add is NOT tracked by KBService."""
    before = len(await kb.list_versions())
    await kb.vector_store.add(
        ids=["x"], texts=["direct write"],
        embeddings=[[0.0, 0.0, 0.0, 1.0]],
        metadatas=[{"source": "direct.txt"}],
    )
    assert len(await kb.list_versions()) == before


@pytest.mark.asyncio
async def test_ingest_path_registers_version(ingest, kb, profile):
    before = len(await kb.list_versions())
    await _add_file(ingest, profile, "a.txt", "alpha")
    assert len(await kb.list_versions()) > before


# ================================================================
# 版本化查询的纯函数
# ================================================================

def test_dict_to_where_basic(store):
    assert store._dict_to_where({}) == ""
    assert store._dict_to_where({"id": "x"}) == "id = 'x'"
    assert "json_get_int(metadata, 'priority') = 1" in store._dict_to_where({"priority": 1})
    assert "IN ('a.txt', 'b.txt')" in store._dict_to_where({"source": ["a.txt", "b.txt"]})


def test_dict_to_where_rejects_bad_field_name(store):
    with pytest.raises(ValueError):
        store._dict_to_where({"bad field": "x"})


def test_escape_for_datafusion(store):
    """DataFusion uses SQL-standard quote-doubling, not backslash escapes."""
    assert store._escape("it's") == "it''s"
    assert store._escape(r"a\b") == r"a\b"


def test_parse_metadata(store):
    assert store._parse_metadata(None) == {}
    assert store._parse_metadata('{"a": 1}') == {"a": 1}
    assert store._parse_metadata("[1,2,3]") == {}     # not a dict