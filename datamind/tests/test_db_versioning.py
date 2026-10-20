"""Tests for DBService version-management methods (Dolt backend).

Data is injected through IngestService — the only path that calls
`commit_version` and produces a new Dolt commit. The versioned read
methods on DBService are then exercised against those commits.

Only Dolt supports versioning, so this file requires a local
`dolt sql-server` on 127.0.0.1:3307.
"""
from __future__ import annotations

import csv
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from datamind.capabilities.db.providers.dolt import DoltDialect
from datamind.capabilities.db.service import DBService
from datamind.capabilities.ingest.service import IngestService
from datamind.config import DBConfig
from datamind.core.errors import CapabilityError


# ================================================================ Dolt server

def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


@pytest.fixture(scope="session")
def dolt_server():
    """Reuse a running dolt sql-server, or start one for the session."""
    host, port = "127.0.0.1", 3307

    if _port_open(host, port):
        yield host, port
        return

    dolt_bin = shutil.which("dolt")
    if dolt_bin is None:
        pytest.skip("dolt binary not on PATH")

    tmpdir = tempfile.mkdtemp(prefix="dolt_db_svc_")
    subprocess.run(
        [dolt_bin, "init", "--name", "Test", "--email", "t@e.com"],
        cwd=tmpdir, check=True, capture_output=True,
    )
    log = open(Path(tmpdir) / "dolt.log", "wb")
    proc = subprocess.Popen(
        [dolt_bin, "sql-server", "--port", str(port), "--host", host],
        cwd=tmpdir, stdout=log, stderr=log,
    )
    for _ in range(60):
        time.sleep(0.5)
        if _port_open(host, port):
            break
    else:
        proc.terminate()
        shutil.rmtree(tmpdir, ignore_errors=True)
        pytest.skip("dolt sql-server did not start")

    yield host, port

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


# ================================================================ fixtures

@pytest.fixture
def profile(tmp_path: Path) -> Path:
    p = tmp_path / "profile"
    (p / "uploads").mkdir(parents=True)
    return p


@pytest.fixture
def db_service(dolt_server, tmp_path) -> DBService:
    """Fresh Dolt repo per test (unique db name + storage dir)."""
    host, port = dolt_server
    db_name = f"test_{uuid.uuid4().hex[:8]}"
    dsn = f"mysql+pymysql://root:@{host}:{port}/{db_name}"

    dialect = DoltDialect()
    engine = dialect.build_engine(dsn, storage_dir=str(tmp_path / "dolt"))
    yield DBService(dialect=dialect, engine=engine, db_cfg=DBConfig())
    engine.dispose()
    aclose = getattr(dialect, "aclose", None)
    if callable(aclose):
        aclose()


@pytest.fixture
def ingest(db_service, profile) -> IngestService:
    return IngestService(
        kb=None, db=db_service, graph=None,
        llm_client=None, llm_model="test",
        profile_data_dir=profile,
        chunk_size=512, chunk_overlap=64,
        ingest_mode="direct",
    )


# ================================================================ helpers

def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


async def _import(ingest, profile, name, header, rows, table="t1"):
    """Import a CSV and return the resulting commit hash."""
    p = _write_csv(profile / "uploads" / name, header, rows)
    result = await ingest.db_import_csv(path=str(p), table=table)
    return result["commit_hash"]


async def _user_commit(db_service, index_from_oldest=0) -> str:
    """Return the Nth user commit (0 = earliest), skipping Dolt's init commit.

    `list_versions` returns newest-first, so we reverse and skip the
    'Initialize data repository' entry.
    """
    versions = await db_service.list_versions()
    user_versions = [
        v for v in reversed(versions)
        if not (v.get("message") or "").startswith("Initialize")
    ]
    return user_versions[index_from_oldest]["commit"]


# ================================================================
# list_versions
# ================================================================

@pytest.mark.asyncio
async def test_list_versions_grows_after_import(ingest, db_service, profile):
    before = len(await db_service.list_versions())

    await _import(ingest, profile, "a.csv", ["id", "name"], [["1", "alice"]])

    versions = await db_service.list_versions()
    assert len(versions) > before
    # Entry shape
    assert {"commit", "message"} <= versions[0].keys()
    assert any("t1" in (v["message"] or "") for v in versions)


# ================================================================
# Snapshot isolation
# ================================================================

@pytest.mark.asyncio
async def test_list_tables_at_version_excludes_newer_tables(
    ingest, db_service, profile,
):
    await _import(ingest, profile, "a.csv", ["id"], [["1"]], table="old_table")
    v1 = await _user_commit(db_service)

    await _import(ingest, profile, "b.csv", ["id"], [["2"]], table="new_table")

    tables_v1 = await db_service.list_tables_at_version(version=v1)
    assert "old_table" in tables_v1
    assert "new_table" not in tables_v1


@pytest.mark.asyncio
async def test_describe_at_version_excludes_newer_columns(
    ingest, db_service, profile,
):
    await _import(ingest, profile, "a.csv", ["id"], [["1"]])
    v1 = await _user_commit(db_service)

    # Second import adds a column
    await _import(ingest, profile, "b.csv", ["id", "extra"], [["2", "x"]])

    schema_v1 = await db_service.describe_at_version("t1", version=v1)
    names = {c.name for c in schema_v1.columns}
    assert "id" in names
    assert "extra" not in names


@pytest.mark.asyncio
async def test_query_sql_at_version_sees_old_data(ingest, db_service, profile):
    await _import(ingest, profile, "a.csv", ["id"], [["1"]])
    v1 = await _user_commit(db_service)

    _write_csv(profile / "uploads" / "a.csv", ["id"], [["999"]])
    await ingest.db_update_csv(path=str(profile / "uploads" / "a.csv"), table="t1")

    # Current sees 999
    now = {r[0] for r in (await db_service.query_sql("SELECT id FROM t1")).rows}
    assert now == {"999"}

    # v1 sees 1
    v1_ids = {
        r[0] for r in (
            await db_service.query_sql_at_version("SELECT id FROM t1", version=v1)
        ).rows
    }
    assert v1_ids == {"1"}


# ================================================================
# rollback
# ================================================================

@pytest.mark.asyncio
async def test_rollback_restores_data_and_preserves_history(
    ingest, db_service, profile,
):
    """After rollback to v1, data == v1 and no commit was lost."""
    await _import(ingest, profile, "a.csv", ["id"], [["1"]])
    v1 = await _user_commit(db_service)

    _write_csv(profile / "uploads" / "a.csv", ["id"], [["999"]])
    await ingest.db_update_csv(path=str(profile / "uploads" / "a.csv"), table="t1")

    n_before = len(await db_service.list_versions())
    await db_service.rollback(version=v1)

    # Data is back to v1
    ids = {r[0] for r in (await db_service.query_sql("SELECT id FROM t1")).rows}
    assert ids == {"1"}

    # History preserved — DOLT_REVERT appends rather than rewinding
    n_after = len(await db_service.list_versions())
    assert n_after > n_before


# ================================================================
# Guards
# ================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "list_tables_at_version",
    "describe_at_version",
    "query_sql_at_version",
    "rollback",
])
async def test_versioned_methods_require_dolt(method):
    class _NotDolt:
        name = "sqlite"

    db = DBService(dialect=_NotDolt(), engine=object(), db_cfg=DBConfig())
    args = {
        "list_tables_at_version": ("v",),
        "describe_at_version": ("t", "v"),
        "query_sql_at_version": ("SELECT 1", "v"),
        "rollback": ("v",),
    }[method]
    with pytest.raises(CapabilityError):
        await getattr(db, method)(*args)
