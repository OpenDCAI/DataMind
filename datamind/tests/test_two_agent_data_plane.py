"""Two-agent contracts and StoreAgent receipt behaviour."""
from __future__ import annotations

from pathlib import Path

import pytest

from datamind.capabilities.db.providers.sqlite import SQLiteDialect
from datamind.capabilities.db.service import DBService
from datamind.capabilities.ingest.ledger import IngestLedger, with_receipts
from datamind.capabilities.ingest.service import IngestService
from datamind.capabilities.memory.tools import build_memory_tools
from datamind.capabilities.skills.service import SkillsService
from datamind.config import DBConfig
from datamind.core.context import RequestContext
from datamind.core.contracts import IngestReceipt, SourceRef
from datamind.core.logging import bind_context
from datamind.core.tools import ToolRegistry, ToolSpec


def test_source_ref_is_content_stable(tmp_path: Path):
    source = tmp_path / "doc.md"
    source.write_text("same content", encoding="utf-8")
    first = SourceRef.from_file(source)
    second = SourceRef.from_file(source)
    assert first.source_id == second.source_id
    assert first.checksum == second.checksum


@pytest.mark.asyncio
async def test_store_tool_emits_receipt_and_deduplicates(tmp_path: Path):
    calls = 0

    async def _save(content: str) -> dict:
        nonlocal calls
        calls += 1
        return {"id": "m1", "content": content}

    raw = ToolRegistry()
    raw.add(ToolSpec(
        name="memory_save",
        description="save",
        input_schema={"type": "object", "properties": {"content": {"type": "string"}}},
        handler=_save,
        metadata={"surface": "memory", "access": "write"},
    ))
    wrapped = with_receipts(
        raw,
        IngestLedger(storage_dir=tmp_path / "storage", profile="test"),
    )

    first_raw = await wrapped.get("memory_save").handler(content="remember me")
    second_raw = await wrapped.get("memory_save").handler(content="remember me")
    first = IngestReceipt.model_validate(first_raw)
    second = IngestReceipt.model_validate(second_raw)

    assert calls == 1
    assert first.revision == 1
    assert first.results[0].status == "stored"
    assert second.revision == 1
    assert second.results[0].status == "unchanged"


@pytest.mark.asyncio
async def test_store_failure_is_retained_as_receipt(tmp_path: Path):
    async def _fail() -> dict:
        raise ValueError("bad input")

    raw = ToolRegistry()
    raw.add(ToolSpec(
        name="graph_upsert_triples",
        description="write graph",
        input_schema={"type": "object", "properties": {}},
        handler=_fail,
        metadata={"surface": "graph", "access": "write"},
    ))
    wrapped = with_receipts(
        raw,
        IngestLedger(storage_dir=tmp_path / "storage", profile="test"),
    )
    receipt = IngestReceipt.model_validate(
        await wrapped.get("graph_upsert_triples").handler()
    )
    assert receipt.revision == 0
    assert receipt.results[0].status == "failed"
    assert "ValueError" in (receipt.results[0].error or "")


@pytest.mark.asyncio
async def test_reindex_deduplication_tracks_source_directory_changes(tmp_path: Path):
    calls = 0
    source_dir = tmp_path / "profile"
    source_dir.mkdir()
    document = source_dir / "doc.md"
    document.write_text("version one", encoding="utf-8")

    async def _reindex() -> dict:
        nonlocal calls
        calls += 1
        return {"total_embedded": 1}

    raw = ToolRegistry()
    raw.add(ToolSpec(
        name="kb_reindex",
        description="reindex",
        input_schema={"type": "object", "properties": {}},
        handler=_reindex,
        metadata={
            "surface": "kb",
            "access": "write",
            "source_path": str(source_dir),
        },
    ))
    wrapped = with_receipts(
        raw,
        IngestLedger(storage_dir=tmp_path / "storage", profile="test"),
    )

    first = IngestReceipt.model_validate(await wrapped.get("kb_reindex").handler())
    duplicate = IngestReceipt.model_validate(await wrapped.get("kb_reindex").handler())
    document.write_text("version two", encoding="utf-8")
    changed = IngestReceipt.model_validate(await wrapped.get("kb_reindex").handler())

    assert calls == 2
    assert first.revision == duplicate.revision == 1
    assert duplicate.results[0].status == "unchanged"
    assert changed.revision == 2
    assert changed.results[0].status == "stored"


@pytest.mark.asyncio
async def test_memory_tools_take_profile_and_session_from_bound_context():
    captured: dict = {}

    class _Memory:
        async def save(self, content: str, **kwargs):
            captured.update({"content": content, **kwargs})
            return "memory-1"

        async def recall(self, *args, **kwargs):
            return []

        async def forget(self, *args, **kwargs):
            return True

        async def list_profiles(self):
            return []

    tools = ToolRegistry()
    tools.extend(build_memory_tools(_Memory()))  # type: ignore[arg-type]
    context = RequestContext(session_id="session-7", profile="tenant-a")
    with bind_context(context):
        result = await tools.get("memory_save").handler(
            content="temporary note",
            scope="session",
        )

    assert result["id"] == "memory-1"
    assert captured["profile"] is None
    assert captured["session_id"] == "session-7"


@pytest.mark.asyncio
async def test_store_can_write_after_retrieve_marks_sqlite_connection_read_only(
    tmp_path: Path,
):
    dialect = SQLiteDialect()
    engine = dialect.build_engine(None, default_path=str(tmp_path / "shared.db"))
    db = DBService(dialect=dialect, engine=engine, db_cfg=DBConfig())
    ingest = object.__new__(IngestService)
    ingest._db = db

    await db.query_sql("SELECT 1 AS ready")
    stored = await ingest.db_import_records(
        table="events",
        records=[{"name": "created"}],
        if_exists="replace",
    )
    retrieved = await db.query_sql("SELECT name FROM events")

    assert stored["rows_inserted"] == 1
    assert retrieved.rows == [["created"]]


@pytest.mark.asyncio
async def test_profile_skill_upsert_is_immediately_readable(tmp_path: Path):
    base = tmp_path / "base"
    profile = tmp_path / "profile"
    service = SkillsService(
        skills_dir=base,
        profile_skills_dir=profile,
        embedding=None,
        vector_store=None,
    )
    result = await service.upsert(
        name="incident-response",
        description="Handle incidents",
        body="# Steps\n\n1. Triage\n2. Mitigate",
        keywords=["incident", "triage"],
    )
    loaded = service.get("incident-response")
    assert result["name"] == "incident-response"
    assert loaded["found"] is True
    assert "Triage" in loaded["body"]
    assert str(profile) in loaded["path"]
