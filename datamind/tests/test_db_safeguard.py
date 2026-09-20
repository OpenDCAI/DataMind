"""Safeguard unit tests (no DB required)."""
from __future__ import annotations

import pytest

from datamind.capabilities.db.safeguard import (
    DestructiveSQLError,
    MultiStatementSQLError,
    contains_multiple_statements,
    ensure_row_limit,
    is_destructive_sql,
    leading_verb,
    strip_comments,
)
from datamind.capabilities.db.providers.sqlite import SQLiteDialect
from datamind.core.errors import CapabilityError


@pytest.mark.parametrize(
    "sql,verb",
    [
        ("SELECT * FROM t", "SELECT"),
        ("  select  1 ", "SELECT"),
        ("/* c */ SELECT 1", "SELECT"),
        ("-- comment\nDELETE FROM t", "DELETE"),
        ("  ", ""),
    ],
)
def test_leading_verb(sql, verb):
    assert leading_verb(sql) == verb


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT * FROM t", False),
        ("INSERT INTO t VALUES (1)", True),
        ("delete from t", True),
        ("DROP TABLE t", True),
        ("PRAGMA foreign_keys = ON", True),
        ("SELECT * INTO OUTFILE '/tmp/x' FROM t", True),
        ("SELECT * FROM (SELECT 1) _", False),
        ("   ", False),
    ],
)
def test_destructive_detection(sql, expected):
    assert is_destructive_sql(sql) is expected


def test_multi_statement_detection():
    assert contains_multiple_statements("SELECT 1; DELETE FROM t")
    assert contains_multiple_statements("SELECT 1; SELECT 2")
    assert not contains_multiple_statements("SELECT 1")
    assert not contains_multiple_statements("SELECT 1;")  # trailing semicolon OK
    assert not contains_multiple_statements("SELECT ';' FROM t")


def test_ensure_row_limit_wraps():
    out = ensure_row_limit("SELECT a FROM t ORDER BY a", row_limit=100)
    assert out.upper().startswith("SELECT * FROM ( ")
    assert "LIMIT 100" in out


def test_ensure_row_limit_strips_trailing_semicolon():
    out = ensure_row_limit("SELECT 1;", row_limit=10)
    assert ";" not in out.replace("LIMIT 10", "")


def test_strip_comments():
    assert strip_comments("SELECT /* x */ 1") == "SELECT  1"
    assert strip_comments("-- line\nSELECT 1") == "\nSELECT 1"


@pytest.mark.asyncio
@pytest.mark.parametrize("row_limit", [0, -1])
async def test_execute_readonly_rejects_nonpositive_row_limit(tmp_path, row_limit):
    dialect = SQLiteDialect()
    engine = dialect.build_engine(
        None, default_path=str(tmp_path / "db.sqlite")
    )

    with pytest.raises(CapabilityError, match="row_limit"):
        await dialect.execute_readonly(
            engine, "SELECT 1", row_limit=row_limit
        )
