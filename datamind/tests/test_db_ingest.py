"""Tests for IngestService's DB methods (import / update / delete).

Parameterized over SQLite / MySQL / Dolt. MySQL and Dolt are skipped
when their prerequisites are missing (env var / dolt binary).
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, text

from datamind.capabilities.db import DBService
from datamind.capabilities.ingest.service import IngestService
from datamind.config import DBConfig
from datamind.core.errors import CapabilityError


# ================================================================ helpers

class _Embedding:
    dimension = 4
    async def embed_texts(self, texts):
        return [[0.0] * self.dimension for _ in texts]


class _KB:
    def __init__(self, tmp_path):
        self.embedding = _Embedding()
    async def record_incremental_ingest(self):
        pass


class _Model:
    async def generate_text(self, prompt, **kwargs):
        return "[]"


def _write_csv(path: Path, header, rows, delimiter: str = ",") -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)
    return path


def _fetch_all(engine, sql: str):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def _rows(engine, sql: str) -> list[tuple]:
    return [tuple(r) for r in _fetch_all(engine, sql)]


def _can_connect_mysql(host, port, user, password) -> bool:
    try:
        import pymysql
        c = pymysql.connect(host=host, port=port, user=user, password=password,
                            connect_timeout=2)
        c.close()
        return True
    except Exception:
        return False


# ================================================================ Dolt server

def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


@pytest.fixture(scope="session")
def dolt_server():
    host, port = "127.0.0.1", 3307
    if _port_open(host, port):
        yield host, port
        return

    dolt_bin = shutil.which("dolt")
    if dolt_bin is None:
        pytest.skip("dolt binary not on PATH")

    tmpdir = tempfile.mkdtemp(prefix="dolt_db_ingest_")
    subprocess.run([dolt_bin, "init", "--name", "T", "--email", "t@e.com"],
                   cwd=tmpdir, check=True, capture_output=True)
    log = open(Path(tmpdir) / "dolt.log", "wb")
    proc = subprocess.Popen([dolt_bin, "sql-server", "--port", str(port),
                             "--host", host],
                            cwd=tmpdir, stdout=log, stderr=log)
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


# ================================================================ backend fixtures

@pytest.fixture(params=["sqlite", "mysql", "dolt"])
def backend(request):
    return request.param


@pytest.fixture
def dsn(backend, request, tmp_path):
    """Yield a DSN. MySQL/Dolt get a fresh uuid-named database per test."""
    if backend == "sqlite":
        yield None
        return

    if backend == "mysql":
        url = os.environ.get("DATAMIND_TEST_MYSQL_DSN")
        if not url:
            pytest.skip("set DATAMIND_TEST_MYSQL_DSN to run MySQL tests")
        parsed = urlparse(url)
        user = parsed.username or "root"
        password = parsed.password or ""
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 3306
        if not _can_connect_mysql(host, port, user, password):
            pytest.skip(f"MySQL not reachable at {host}:{port}")
    else:
        host, port = request.getfixturevalue("dolt_server")
        user, password = "root", ""

    db_name = f"datamind_test_{uuid.uuid4().hex[:8]}"
    admin_url = f"mysql+pymysql://{user}:{password}@{host}:{port}/"

    admin = create_engine(admin_url, future=True)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE DATABASE `{db_name}`"))
    admin.dispose()

    try:
        yield f"mysql+pymysql://{user}:{password}@{host}:{port}/{db_name}"
    finally:
        admin = create_engine(admin_url, future=True)
        try:
            with admin.begin() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS `{db_name}`"))
        finally:
            admin.dispose()


@pytest.fixture
def db_service(backend, dsn, tmp_path) -> DBService:
    if backend == "dolt":
        from datamind.capabilities.db.providers.dolt import DoltDialect
        dialect = DoltDialect()
        engine = dialect.build_engine(dsn, storage_dir=str(tmp_path))
    elif backend == "sqlite":
        from datamind.capabilities.db.providers.sqlite import SQLiteDialect
        dialect = SQLiteDialect()
        engine = dialect.build_engine(None, default_path=str(tmp_path / "demo.db"))
    else:
        from datamind.capabilities.db.providers.mysql import MySQLDialect
        dialect = MySQLDialect()
        engine = dialect.build_engine(dsn)

    yield DBService(dialect=dialect, engine=engine, db_cfg=DBConfig(),
                    llm_client=_Model(), llm_model="test")
    engine.dispose()
    aclose = getattr(dialect, "aclose", None)
    if callable(aclose):
        aclose()


@pytest.fixture
def profile(tmp_path) -> Path:
    p = tmp_path / "profile"
    (p / "uploads").mkdir(parents=True)
    return p


@pytest.fixture
def service(db_service, tmp_path, profile) -> IngestService:
    return IngestService(
        kb=_KB(tmp_path), db=db_service, graph=None,
        llm_client=_Model(), llm_model="test",
        profile_data_dir=profile,
        chunk_size=512, chunk_overlap=64,
        ingest_mode="direct",
    )


# ================================================================ _read_from_csv

def test_read_from_csv_headers_and_rows(service, tmp_path):
    p = _write_csv(tmp_path / "a.csv", ["id", "name"], [["1", "alice"]])
    cols, rows = service._read_from_csv(p)
    assert cols == ["id", "name", "_source_file"]
    assert rows == [{"id": "1", "name": "alice", "_source_file": str(p)}]


def test_read_from_csv_empty_raises(service, tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    with pytest.raises(CapabilityError):
        service._read_from_csv(p)


def test_read_from_csv_header_only(service, tmp_path):
    p = _write_csv(tmp_path / "h.csv", ["a", "b"], [])
    cols, rows = service._read_from_csv(p)
    assert cols == ["a", "b", "_source_file"]
    assert rows == []


def test_read_from_csv_ragged_rows(service, tmp_path):
    p = tmp_path / "ragged.csv"
    p.write_text("a,b,c\n1,2\n3,4,5,6\n", encoding="utf-8")
    _, rows = service._read_from_csv(p)
    assert rows[0]["c"] == ""       # short → padded
    assert rows[1]["c"] == "5"      # long  → truncated


def test_read_from_csv_sanitizes_headers(service, tmp_path):
    """Bad / duplicate / reserved headers get stable fallbacks."""
    p = tmp_path / "bad.csv"
    p.write_text("1bad,good name,id,id,_source_file\nx,y,z,w,v\n",
                 encoding="utf-8")
    cols, _ = service._read_from_csv(p)
    assert len(set(cols)) == len(cols)                    # no duplicates
    assert cols[0] == "col_1" and cols[1] == "col_2"      # invalid → fallback
    assert any(c.startswith("_source_file") for c in cols)


# ================================================================ _read_from_records

def test_read_from_records_basic(service):
    cols, rows = service._read_from_records(
        records=[{"a": 1, "b": "x"}], source_file=None,
    )
    assert cols == ["a", "b", "_source_file"]
    assert rows[0] == {"a": "1", "b": "x", "_source_file": "inline_records"}


def test_read_from_records_nested_json(service):
    _, rows = service._read_from_records(
        records=[{"tags": ["a", "b"], "meta": {"k": 1}}], source_file=None,
    )
    assert json.loads(rows[0]["tags"]) == ["a", "b"]
    assert json.loads(rows[0]["meta"]) == {"k": 1}


def test_read_from_records_empty_raises(service):
    with pytest.raises(CapabilityError):
        service._read_from_records(records=[], source_file=None)


def test_read_from_records_case_insensitive_columns(service):
    """Name / name must not both become distinct columns."""
    cols, _ = service._read_from_records(
        records=[{"Name": 1, "name": 2}], source_file=None,
    )
    assert len(set(c.casefold() for c in cols)) == len(cols)


# ================================================================ db_import_csv

@pytest.mark.asyncio
async def test_db_import_csv_append(service, backend, tmp_path, db_service):
    p = _write_csv(tmp_path / "a.csv", ["id", "name"], [["1", "a"], ["2", "b"]])
    result = await service.db_import_csv(path=str(p), table="t1")

    assert result["rows_inserted"] == 2
    assert result["columns"] == ["id", "name", "_source_file"]
    if backend == "dolt":
        assert result["commit_hash"]
    else:
        assert result["commit_hash"] is None

    assert _rows(db_service.engine, "SELECT id, name FROM t1 ORDER BY id") == \
        [("1", "a"), ("2", "b")]


@pytest.mark.asyncio
async def test_db_import_csv_replace(service, tmp_path, db_service):
    p1 = _write_csv(tmp_path / "a.csv", ["id"], [["1"], ["2"]])
    await service.db_import_csv(path=str(p1), table="t1")

    p2 = _write_csv(tmp_path / "b.csv", ["id"], [["9"]])
    await service.db_import_csv(path=str(p2), table="t1", if_exists="replace")

    assert _rows(db_service.engine, "SELECT id FROM t1") == [("9",)]


@pytest.mark.asyncio
async def test_db_import_csv_add_column_on_reimport(service, tmp_path, db_service):
    """Re-importing with more columns adds them; old rows get NULL."""
    await service.db_import_csv(
        path=str(_write_csv(tmp_path / "a.csv", ["id"], [["1"]])), table="t1",
    )
    await service.db_import_csv(
        path=str(_write_csv(tmp_path / "b.csv", ["id", "extra"], [["2", "x"]])),
        table="t1",
    )
    assert _rows(db_service.engine, "SELECT id, extra FROM t1 ORDER BY id") == \
        [("1", None), ("2", "x")]


@pytest.mark.asyncio
async def test_db_import_csv_rejects_invalid_table(service, tmp_path):
    p = _write_csv(tmp_path / "a.csv", ["id"], [["1"]])
    with pytest.raises(CapabilityError):
        await service.db_import_csv(path=str(p), table="bad table!")


@pytest.mark.asyncio
async def test_db_import_csv_rejects_missing_file(service, tmp_path):
    with pytest.raises(CapabilityError):
        await service.db_import_csv(path=str(tmp_path / "missing.csv"), table="t1")


@pytest.mark.asyncio
async def test_db_import_csv_fail_if_exists(service, tmp_path):
    p = _write_csv(tmp_path / "a.csv", ["id"], [["1"]])
    await service.db_import_csv(path=str(p), table="t1")
    with pytest.raises(CapabilityError):
        await service.db_import_csv(path=str(p), table="t1", if_exists="fail")


@pytest.mark.asyncio
async def test_db_import_csv_header_only(service, tmp_path):
    p = _write_csv(tmp_path / "h.csv", ["a", "b"], [])
    assert (await service.db_import_csv(path=str(p), table="t_empty"))["rows_inserted"] == 0


# ================================================================ db_import_records

@pytest.mark.asyncio
async def test_db_import_records_basic(service, backend, db_service):
    result = await service.db_import_records(
        table="notes",
        records=[{"id": "1", "text": "hi"}, {"id": "2", "text": "world"}],
    )
    assert result["rows_inserted"] == 2
    assert result["source"] == "inline_records"

    assert _rows(db_service.engine, "SELECT id, text FROM notes ORDER BY id") == \
        [("1", "hi"), ("2", "world")]


@pytest.mark.asyncio
async def test_db_import_records_replace(service, db_service):
    await service.db_import_records(table="t", records=[{"a": "1"}], if_exists="replace")
    await service.db_import_records(table="t", records=[{"a": "9"}], if_exists="replace")
    assert _rows(db_service.engine, "SELECT a FROM t") == [("9",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("records", [[], ["not-a-dict"]])
async def test_db_import_records_rejects_bad_input(service, records):
    with pytest.raises(CapabilityError):
        await service.db_import_records(table="t", records=records)


# ================================================================ db_import_path

@pytest.mark.asyncio
async def test_db_import_path_csv_and_tsv(service, tmp_path, db_service):
    csv_p = _write_csv(tmp_path / "d.csv", ["id"], [["1"]])
    tsv_p = _write_csv(tmp_path / "d.tsv", ["id"], [["2"]], delimiter="\t")

    r1 = await service.db_import_path(path=str(csv_p))
    r2 = await service.db_import_path(path=str(tsv_p))
    assert r1["tables"][0]["rows_inserted"] == 1
    assert r2["tables"][0]["rows_inserted"] == 1


@pytest.mark.asyncio
async def test_db_import_path_rejects_unsupported(service, tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00")
    with pytest.raises(CapabilityError):
        await service.db_import_path(path=str(p))


@pytest.mark.asyncio
@pytest.mark.parametrize("maker", [
    lambda p: p.write_bytes(b"this is not a zip"),
    lambda p: zipfile.ZipFile(p, "w").writestr("dummy.txt", "x"),
    lambda p: p.write_bytes(b""),
])
async def test_db_import_path_handles_corrupt_xlsx(service, tmp_path, maker):
    """Malformed / empty / zero-byte XLSX must surface as CapabilityError."""
    p = tmp_path / "bad.xlsx"
    maker(p)
    with pytest.raises(CapabilityError):
        await service.db_import_path(path=str(p))


# ================================================================ db_update_csv

@pytest.mark.asyncio
async def test_db_update_csv_replaces(service, tmp_path, db_service):
    p = _write_csv(tmp_path / "a.csv", ["id", "v"], [["1", "a"], ["2", "b"]])
    await service.db_import_csv(path=str(p), table="t1")

    _write_csv(p, ["id", "v"], [["1", "A"], ["3", "c"]])
    result = await service.db_update_csv(path=str(p), table="t1")

    assert result["rows_new"] == 2
    assert _rows(db_service.engine, "SELECT id, v FROM t1 ORDER BY id") == \
        [("1", "A"), ("3", "c")]


@pytest.mark.asyncio
async def test_db_update_csv_rejects_unknown_table(service, tmp_path):
    p = _write_csv(tmp_path / "a.csv", ["id"], [["1"]])
    with pytest.raises(CapabilityError):
        await service.db_update_csv(path=str(p), table="never_created")


@pytest.mark.asyncio
async def test_db_update_csv_rejects_no_matching(service, tmp_path):
    p = _write_csv(tmp_path / "a.csv", ["id"], [["1"]])
    with pytest.raises(CapabilityError):
        await service.db_update_csv(path=str(p))


@pytest.mark.asyncio
async def test_db_update_csv_rejects_ambiguous(service, tmp_path):
    p = _write_csv(tmp_path / "a.csv", ["id"], [["1"]])
    await service.db_import_csv(path=str(p), table="t_a")
    await service.db_import_csv(path=str(p), table="t_b")
    with pytest.raises(CapabilityError):
        await service.db_update_csv(path=str(p))


# ================================================================ db_update_records

@pytest.mark.asyncio
async def test_db_update_records_replaces(service, tmp_path, db_service):
    src = tmp_path / "a.csv"
    src.write_text("x", encoding="utf-8")
    await service.db_import_records(
        table="t", records=[{"id": "1", "v": "a"}], source_file=str(src),
    )
    result = await service.db_update_records(
        table="t",
        records=[{"id": "1", "v": "A"}, {"id": "2", "v": "B"}],
        path=str(src),
    )
    assert result["rows_new"] == 2
    assert _rows(db_service.engine, "SELECT id, v FROM t ORDER BY id") == \
        [("1", "A"), ("2", "B")]


# ================================================================ db_delete_path

@pytest.mark.asyncio
async def test_db_delete_path_removes_file_and_rows(
    service, backend, tmp_path, db_service,
):
    p = _write_csv(tmp_path / "a.csv", ["id"], [["1"], ["2"]])
    await service.db_import_csv(path=str(p), table="t1")

    result = await service.db_delete_path(path=str(p))

    assert not p.exists()
    assert result["rows_deleted"] == 2
    assert result["rows_deleted_by_table"] == {"t1": 2}
    assert result["status"] == ("deleted and committed" if backend == "dolt" else "deleted")
    assert _fetch_all(db_service.engine, "SELECT COUNT(*) FROM t1")[0][0] == 0


@pytest.mark.asyncio
async def test_db_delete_path_no_match(service, tmp_path):
    p = _write_csv(tmp_path / "a.csv", ["id"], [["1"]])
    result = await service.db_delete_path(path=str(p))
    assert result["rows_deleted"] == 0
    assert result["status"] == "unchanged"
    assert not p.exists()


@pytest.mark.asyncio
async def test_db_delete_path_rejects_directory(service, tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(CapabilityError):
        await service.db_delete_path(path=str(d))


@pytest.mark.asyncio
async def test_db_delete_path_spans_multiple_tables(service, tmp_path, db_service):
    p = _write_csv(tmp_path / "a.csv", ["id"], [["1"]])
    await service.db_import_csv(path=str(p), table="t_a")
    await service.db_import_csv(path=str(p), table="t_b")

    result = await service.db_delete_path(path=str(p))
    assert result["rows_deleted"] == 2
    assert result["rows_deleted_by_table"] == {"t_a": 1, "t_b": 1}


@pytest.mark.asyncio
async def test_db_delete_path_only_matching_source(service, tmp_path, db_service):
    p1 = _write_csv(tmp_path / "a.csv", ["id"], [["1"]])
    p2 = _write_csv(tmp_path / "b.csv", ["id"], [["2"]])
    await service.db_import_csv(path=str(p1), table="t1")
    await service.db_import_csv(path=str(p2), table="t1")

    await service.db_delete_path(path=str(p1))
    assert _rows(db_service.engine, "SELECT id FROM t1") == [("2",)]


# ================================================================ _get_candidate_tables

@pytest.mark.asyncio
async def test_get_candidate_tables_none(service, db_service):
    t = await service._get_candidate_tables(
        db_service.engine, db_service.dialect, "/nope.csv",
    )
    assert t == []


@pytest.mark.asyncio
async def test_get_candidate_tables_rejects_missing_explicit(service, db_service):
    with pytest.raises(CapabilityError):
        await service._get_candidate_tables(
            db_service.engine, db_service.dialect, "/x", table="missing",
        )


@pytest.mark.asyncio
async def test_get_candidate_tables_finds_import(service, tmp_path, db_service):
    p = _write_csv(tmp_path / "a.csv", ["id"], [["1"]])
    await service.db_import_csv(path=str(p), table="t1")
    t = await service._get_candidate_tables(
        db_service.engine, db_service.dialect, str(p),
    )
    assert t == ["t1"]