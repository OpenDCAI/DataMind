from pathlib import Path

import pytest

from datamind.capabilities.ingest.service import IngestService
from datamind.capabilities.ingest.ledger import IngestLedger, with_receipts
from datamind.capabilities.ingest.tools import build_ingest_tools
from datamind.core.tools import ToolRegistry


class _Embedding:
    async def embed_texts(self, texts):
        return [[float(len(text))] for text in texts]


class _VectorStore:
    def __init__(self):
        self.items = {}

    async def add(self, ids, texts, embeddings, metadatas=None):
        for chunk_id, text, metadata in zip(ids, texts, metadatas or []):
            self.items[chunk_id] = (text, dict(metadata))

    async def delete(self, ids):
        for chunk_id in ids:
            self.items.pop(chunk_id, None)

    async def get_all_texts(self):
        return [
            (chunk_id, text, metadata)
            for chunk_id, (text, metadata) in self.items.items()
        ]


class _PartiallyFailingStore(_VectorStore):
    async def add(self, ids, texts, embeddings, metadatas=None):
        first = True
        for chunk_id, text, metadata in zip(ids, texts, metadatas or []):
            if first:
                self.items[chunk_id] = (text, dict(metadata))
                first = False
                continue
            raise RuntimeError("simulated chunk write failure")


class _KB:
    def __init__(self):
        self.embedding = _Embedding()
        self.vector_store = _VectorStore()

    async def record_incremental_ingest(self):
        return None


class _FailingKB(_KB):
    def __init__(self):
        self.embedding = _Embedding()
        self.vector_store = _PartiallyFailingStore()


class _Model:
    async def generate_text(self, prompt, **kwargs):
        return "[]"


@pytest.mark.asyncio
async def test_reingesting_same_path_replaces_old_kb_chunks(tmp_path: Path):
    profile = tmp_path / "profile"
    uploads = profile / "uploads"
    uploads.mkdir(parents=True)
    source = uploads / "release_note.txt"
    kb = _KB()
    service = IngestService(
        kb=kb,
        db=None,
        graph=None,
        llm_client=_Model(),
        llm_model="test",
        profile_data_dir=profile,
        chunk_size=512,
        chunk_overlap=64,
    )

    source.write_text("负责人：林悦\n验收日期：2026年11月18日", encoding="utf-8")
    await service.kb_add_file(path=str(source))
    assert [item[0] for item in kb.vector_store.items.values()] == [
        "负责人：林悦\n验收日期：2026年11月18日"
    ]

    source.write_text("负责人：周宁\n验收日期：2026年12月2日", encoding="utf-8")
    await service.kb_add_file(path=str(source))

    assert [item[0] for item in kb.vector_store.items.values()] == [
        "负责人：周宁\n验收日期：2026年12月2日"
    ]


@pytest.mark.asyncio
async def test_partial_chunk_failure_returns_failed_receipt_without_new_chunks(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.mkdir()
    service = IngestService(
        kb=_FailingKB(), db=None, graph=None, llm_client=_Model(), llm_model="test",
        profile_data_dir=profile, chunk_size=4, chunk_overlap=0,
        allowed_roots=[tmp_path],
    )
    raw = ToolRegistry()
    raw.extend(build_ingest_tools(service))
    tools = with_receipts(
        raw,
        IngestLedger(storage_dir=tmp_path / "ledger", profile="test"),
    )

    receipt = await tools.get("kb_add_text").handler(
        text="abcdefgh", source="partial.txt", persist=False,
    )

    assert receipt["results"][0]["status"] == "failed"
    assert service._kb.vector_store.items == {}
