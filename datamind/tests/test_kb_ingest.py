"""Tests for IngestService's KB methods (add / update / delete).

Parameterized over two axes:
  - ingest_mode: "direct" (per-file) or "cocoindex" (workspace-wide)
  - vector store: lancedb or chroma

Only CocoIndex-mode tests are marked `coco`; everything else is direct.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from datamind.capabilities.ingest.service import IngestService
from datamind.core.errors import CapabilityError


# ================================================================ fakes

class _Embedding:
    dimension = 4
    async def embed_texts(self, texts):
        if not texts:
            raise AssertionError("empty revisions must not call the embedding provider")
        return [self._embed(t) for t in texts]
    def _embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vals = [
            int.from_bytes(digest[i*4:(i+1)*4], "little") / 2**32
            for i in range(self.dimension)
        ]
        n = math.sqrt(sum(x * x for x in vals))
        return [x / n for x in vals]


class _KB:
    def __init__(self, vector_store):
        self.embedding = _Embedding()
        self.vector_store = vector_store
    async def record_incremental_ingest(self):
        pass


class _Model:
    async def generate_text(self, prompt, **kwargs):
        return "[]"


def _make_store(kind: str, storage_dir: Path, dimension: int = 4):
    if kind == "lancedb":
        from datamind.capabilities.kb.providers.lancedb_store import LanceDBVectorStore
        return LanceDBVectorStore(
            persist_dir=str(storage_dir / "lancedb"),
            collection_name="kb_default", dimension=dimension,
        )
    from datamind.capabilities.kb.providers.chroma_store import ChromaVectorStore
    return ChromaVectorStore(
        persist_dir=str(storage_dir / "chroma"),
        collection_name="kb_default", dimension=dimension,
    )


# ================================================================ fixtures

@pytest.fixture
def profile(tmp_path) -> Path:
    p = tmp_path / "profile"
    (p / "uploads").mkdir(parents=True)
    return p


def _ingest(tmp_path, profile, store, mode) -> IngestService:
    return IngestService(
        kb=_KB(vector_store=store), db=None, graph=None,
        llm_client=_Model(), llm_model="test",
        profile_data_dir=profile,
        chunk_size=512, chunk_overlap=64,
        ingest_mode=mode,
    )


@pytest.fixture
def direct(tmp_path, profile) -> IngestService:
    return _ingest(tmp_path, profile, _make_store("lancedb", tmp_path), "direct")


@pytest.fixture
def coco(tmp_path, profile) -> IngestService:
    return _ingest(tmp_path, profile, _make_store("lancedb", tmp_path), "cocoindex")


def _write(profile: Path, name: str, text: str) -> Path:
    p = profile / "uploads" / name
    p.write_text(text, encoding="utf-8")
    return p


# ================================================================ kb_add_file

@pytest.mark.asyncio
async def test_kb_add_file_indexes_and_is_idempotent(direct, profile):
    """Re-ingesting the same path replaces chunks — no duplicates."""
    src = _write(profile, "doc.txt", "first version")
    r = await direct.kb_add_file(path=str(src))
    assert r["chunks_added"] == 1

    src.write_text("second version", encoding="utf-8")
    r = await direct.kb_add_file(path=str(src))
    # Same table → 1 chunk still; count unchanged
    assert r["chunks_added"] == 1


@pytest.mark.asyncio
async def test_kb_add_file_empty_yields_no_chunks(direct, profile):
    src = _write(profile, "empty.txt", "")
    r = await direct.kb_add_file(path=str(src))
    assert r["chunks_added"] == 0


@pytest.mark.asyncio
async def test_kb_add_file_copy_false_leaves_external_file_alone(
    direct, profile, tmp_path,
):
    external = tmp_path / "external.txt"
    external.write_text("outside", encoding="utf-8")

    r = await direct.kb_add_file(path=str(external), copy_to_profile=False)
    assert r["chunks_added"] >= 1
    assert r["copied_to"] is None
    assert not (profile / "uploads" / external.name).exists()


@pytest.mark.asyncio
async def test_kb_add_file_duplicate_name_different_content(direct, tmp_path):
    """Same filename, different content → hash suffix avoids clobbering."""
    a = tmp_path / "a" / "note.txt"; a.parent.mkdir()
    b = tmp_path / "b" / "note.txt"; b.parent.mkdir()
    a.write_text("first version", encoding="utf-8")
    b.write_text("second version", encoding="utf-8")

    ra = await direct.kb_add_file(path=str(a))
    rb = await direct.kb_add_file(path=str(b))
    assert ra["copied_to"] != rb["copied_to"]
    assert rb["copied_to"].startswith("uploads/note-")


# ================================================================ kb_delete_file

@pytest.mark.asyncio
async def test_kb_delete_file_removes_chunks(direct, profile, tmp_path):
    src = _write(profile, "del.txt", "to be removed")
    await direct.kb_add_file(path=str(src))

    src.unlink()
    r = await direct.kb_delete_file(path=str(src))
    assert r["chunks_deleted"] >= 1


@pytest.mark.asyncio
async def test_kb_delete_file_rejects_outside_workspace(direct, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    outside.unlink()
    with pytest.raises(CapabilityError):
        await direct.kb_delete_file(path=str(outside))


# ================================================================ kb_add_text

@pytest.mark.asyncio
async def test_kb_add_text_persists_and_indexes(direct, profile):
    r = await direct.kb_add_text(text="hello world", source="note.md")
    assert r["persisted"] is True
    assert r["chunks_added"] >= 1
    target = profile / "notes" / "note.md"
    assert target.read_text(encoding="utf-8").strip() == "hello world"


@pytest.mark.asyncio
async def test_kb_add_text_persist_false(direct, profile):
    r = await direct.kb_add_text(text="ephemeral", source="tmp.md", persist=False)
    assert r["persisted"] is False
    assert not (profile / "notes" / "tmp.md").exists()


@pytest.mark.asyncio
async def test_kb_add_text_rejects_path(direct):
    with pytest.raises(CapabilityError):
        await direct.kb_add_text(text="x", source="subdir/file.md")


@pytest.mark.asyncio
async def test_kb_add_text_cocoindex_requires_persist(coco):
    with pytest.raises(CapabilityError):
        await coco.kb_add_text(text="hello", persist=False)


# ================================================================ kb_add_path

@pytest.mark.asyncio
async def test_kb_add_path_single_file(direct, profile):
    src = _write(profile, "single.txt", "content")
    r = await direct.kb_add_path(path=str(src))
    assert r["files_processed"] == 1
    assert r["chunks_added"] >= 1


@pytest.mark.asyncio
async def test_kb_add_path_directory_recursive(direct, profile):
    uploads = profile / "uploads"
    (uploads / "a.txt").write_text("a", encoding="utf-8")
    nested = uploads / "nested"; nested.mkdir()
    (nested / "b.txt").write_text("b", encoding="utf-8")
    r = await direct.kb_add_path(path=str(uploads), recursive=True)
    assert r["files_processed"] == 2


@pytest.mark.asyncio
async def test_kb_add_path_skips_unsupported_and_continues(direct, profile):
    uploads = profile / "uploads"
    (uploads / "ok.txt").write_text("hello", encoding="utf-8")
    (uploads / "skip.bin").write_bytes(b"\x00")
    (uploads / "broken.pdf").write_bytes(b"not a pdf")

    r = await direct.kb_add_path(path=str(uploads))
    assert r["files_processed"] == 1
    assert r["skipped_count"] >= 2


@pytest.mark.asyncio
async def test_coco_detects_file_removal(coco, profile):
    src = _write(profile, "del.txt", "will be removed")
    await coco.kb_add_file(path=str(src))

    src.unlink()
    r = await coco.kb_update_workspace()
    assert r["chunks_deleted"] > 0


@pytest.mark.asyncio
async def test_coco_update_workspace_noop(coco, profile):
    src = _write(profile, "doc.txt", "stable")
    await coco.kb_add_file(path=str(src))

    r = await coco.kb_update_workspace()
    assert r["chunks_added"] == 0
    assert r["msg"]["Update"] == []