"""A historical receipt cannot establish the current contents of a table."""
from pathlib import Path

import pytest

from datamind.capabilities.db.providers.sqlite import SQLiteDialect
from datamind.capabilities.db.service import DBService
from datamind.capabilities.ingest.ledger import IngestLedger, with_receipts
from datamind.capabilities.ingest.service import IngestService
from datamind.capabilities.ingest.tools import build_ingest_tools
from datamind.config import DBConfig
from datamind.core.tools import ToolRegistry


@pytest.fixture
def database(tmp_path):
    dialect = SQLiteDialect()
    engine = dialect.build_engine(None, default_path=str(tmp_path / "demo.db"))
    db = DBService(dialect=dialect, engine=engine, db_cfg=DBConfig())
    service = IngestService(
        kb=None, db=db, graph=None, llm_client=None, llm_model="test",
        profile_data_dir=tmp_path, chunk_size=512, chunk_overlap=64,
        allowed_roots=[tmp_path],
    )
    raw = ToolRegistry()
    raw.extend(build_ingest_tools(service))
    yield db, raw
    engine.dispose()


def arguments(tool, root: Path, amount, mode="replace"):
    if tool == "db_import_records":
        return dict(table="sales", records=[{"person": "Bob", "amount": amount}], if_exists=mode)
    source = root / "sales.csv"
    source.write_text(f"person,amount\nBob,{amount}\n", encoding="utf-8")
    if tool == "db_import_csv":
        return dict(path=str(source), table="sales", if_exists=mode)
    return dict(path=str(source), table_prefix="sales", if_exists=mode)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["db_import_records", "db_import_csv", "db_import_path"])
async def test_replace_restores_previous_value_across_ledger_reopen(database, tmp_path, tool):
    db, raw = database
    receipts = []
    for amount in (200, 999, 200, 200):
        # Also exercise persisted receipts loaded by a subsequent request/process.
        wrapped = with_receipts(raw, IngestLedger(storage_dir=tmp_path / "ledger", profile="test"))
        receipt = await wrapped.get(tool).handler(**arguments(tool, tmp_path, amount))
        receipts.append(receipt)
        rows = (await db.query_sql("SELECT person, amount FROM sales")).rows
        assert rows == [["Bob", str(amount)]]
        assert receipt["results"][0]["status"] == "stored"
    assert [r["revision"] for r in receipts] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_replace_ignores_existing_success_fingerprint(database, tmp_path):
    db, raw = database
    wrapped = with_receipts(raw, IngestLedger(storage_dir=tmp_path / "ledger", profile="test"))
    args = arguments("db_import_records", tmp_path, 200)
    await wrapped.get("db_import_records").handler(**args)
    # Simulate a change outside this ledger. Historical arguments still match.
    await raw.get("db_import_records").handler(**arguments("db_import_records", tmp_path, 999))
    receipt = await wrapped.get("db_import_records").handler(**args)
    assert receipt["results"][0]["status"] == "stored"
    assert (await db.query_sql("SELECT amount FROM sales")).rows == [["200"]]


@pytest.mark.asyncio
async def test_append_retry_remains_deduplicated(database, tmp_path):
    db, raw = database
    wrapped = with_receipts(raw, IngestLedger(storage_dir=tmp_path / "ledger", profile="test"))
    args = arguments("db_import_records", tmp_path, 200, mode="append")
    first = await wrapped.get("db_import_records").handler(**args)
    second = await wrapped.get("db_import_records").handler(**args)
    assert second["results"][0]["status"] == "unchanged"
    assert second["revision"] == first["revision"]
    assert (await db.query_sql("SELECT amount FROM sales")).rows == [["200"]]


@pytest.mark.asyncio
async def test_csv_import_keeps_duplicate_headers_as_distinct_columns(database, tmp_path):
    db, raw = database
    source = tmp_path / "duplicate-columns.csv"
    source.write_text("a,a\nfirst,second\n", encoding="utf-8")

    receipt = await raw.get("db_import_csv").handler(
        path=str(source), table="duplicate_columns", if_exists="replace"
    )

    assert receipt["columns"] == ["a", "col_2"]
    assert (await db.query_sql("SELECT * FROM duplicate_columns")).rows == [["first", "second"]]
