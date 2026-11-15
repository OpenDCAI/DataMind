"""Database code contracts plus recovery against real, independently locked SQLite.

MySQL/PostgreSQL exceptions below model optional drivers' public attributes;
those tests do not claim to exercise live MySQL/PostgreSQL servers.
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout
from sqlalchemy.pool import NullPool

from datamind.capabilities.db.providers.sqlite import SQLiteDialect
from datamind.capabilities.db.service import DBService
from datamind.config import DBConfig, Settings
from datamind.core.context import RequestContext
from datamind.core.errors import CapabilityError, ConfigError
from datamind.core.logging import bind_context
from datamind.core.recovery import is_transient, recover_read
from datamind.workspace import Workspace


def driver_error(module, *args, **attrs):
    error_type = type("DriverError", (Exception,), {"__module__": module})
    error = error_type(*args)
    for name, value in attrs.items():
        setattr(error, name, value)
    return error


def sqlite_error(code):
    error = sqlite3.OperationalError("message is deliberately not classified")
    if code is not None:
        error.sqlite_errorcode = code
    return error


def wrap(error):
    return OperationalError("SELECT 42", {}, error)


@pytest.mark.parametrize("code,expected", [
    (5, True), (261, True), (517, True), (773, True),
    (6, False), (262, False), (1, False), (8, False), (9, False),
    (11, False), (14, False), (19, False), (999, False), (None, False),
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_sqlite_codes(code, expected, wrapped):
    error = sqlite_error(code)
    assert is_transient(wrap(error) if wrapped else error) is expected


@pytest.mark.parametrize("module,field", [
    ("pymysql.err", "args"), ("MySQLdb", "args"),
    ("mysql.connector.errors", "errno"),
])
@pytest.mark.parametrize("code,expected", [
    (1205, True), (1213, True), (2006, True), (2013, True),
    (1045, False), (1044, False), (1049, False), (1064, False),
    (1146, False), (1054, False), (1062, False), (9999, False), (None, False),
])
def test_mysql_driver_code_contracts(module, field, code, expected):
    args = (code, "driver error") if field == "args" else ("driver error",)
    error = driver_error(module, *args, **({"errno": code} if field == "errno" else {}))
    assert is_transient(error) is expected
    assert is_transient(wrap(error)) is expected


@pytest.mark.parametrize("module,field", [("psycopg.errors", "sqlstate"), ("psycopg2.errors", "pgcode")])
@pytest.mark.parametrize("code,expected", [
    ("08000", True), ("08001", True), ("08003", True), ("08006", True),
    ("40001", True), ("40P01", True), ("57P03", True),
    ("08P01", False), ("57014", False), ("28P01", False), ("28000", False),
    ("42501", False), ("42601", False), ("42P01", False),
    ("23505", False), ("99999", False), (None, False),
])
def test_postgres_driver_code_contracts(module, field, code, expected):
    error = driver_error(module, "driver error", **{field: code})
    assert is_transient(error) is expected
    assert is_transient(wrap(error)) is expected


def test_driver_subclasses_keep_code_rules_and_override_generic_oserror():
    base = type("DriverError", (OSError,), {"__module__": "pymysql.err"})

    class ApplicationError(base):
        pass

    assert is_transient(ApplicationError(1213, "deadlock"))
    assert not is_transient(ApplicationError(1045, "authentication failed"))


@pytest.mark.parametrize("error", [ConnectionError(), TimeoutError(), PoolTimeout()])
def test_sqlalchemy_orig_only_transient_error(error):
    outer = wrap(error)
    assert outer.__cause__ is None
    assert is_transient(outer)


@pytest.mark.parametrize("error", [sqlite_error(1), driver_error("pymysql.err", 1045)])
def test_explicit_database_rejection_precedes_incidental_network_cause(error):
    error.__cause__ = ConnectionError()
    outer = wrap(error)
    outer.__cause__ = TimeoutError()
    assert not is_transient(outer)


@pytest.mark.parametrize("outer_type", [RuntimeError, CapabilityError])
def test_database_error_through_cause(outer_type):
    outer = outer_type("db", "failed") if outer_type is CapabilityError else outer_type("failed")
    outer.__cause__ = wrap(sqlite_error(5))
    assert is_transient(outer)


@pytest.mark.parametrize("outer_type", [ValueError, ConfigError, PermissionError, FileNotFoundError])
def test_explicit_outer_rejection_still_wins(outer_type):
    outer = outer_type("invalid operation")
    outer.__cause__ = wrap(sqlite_error(5))
    assert not is_transient(outer)


def test_unknown_codes_messages_and_unrelated_attributes_are_not_retry_signals():
    assert not is_transient(sqlite3.OperationalError("database is locked"))
    error = RuntimeError("database is locked, timeout, connection lost")
    error.orig = ConnectionError()
    error.errno = 1213
    error.sqlstate = "40001"
    assert not is_transient(error)
    assert not is_transient(wrap(RuntimeError("unknown driver error")))


def test_exception_graph_cycles_terminate_and_do_not_hide_another_branch():
    outer = wrap(sqlite_error(5))
    outer.__cause__ = outer
    assert is_transient(outer)
    outer.orig = outer
    assert not is_transient(outer)
    outer.__cause__ = ConnectionError()
    assert is_transient(outer)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected_calls", [
    (driver_error("pymysql.err", 1213, "deadlock"), 2),
    (driver_error("psycopg.errors", sqlstate="40001"), 2),
    (driver_error("pymysql.err", 1045, "authentication failed"), 1),
    (driver_error("psycopg2.errors", pgcode="57014"), 1),
])
async def test_driver_codes_control_actual_recovery_loop(error, expected_calls):
    calls = 0
    original = wrap(error)

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise original
        return 42

    if expected_calls == 2:
        assert await recover_read(operation, component="db_query_sql", retries=2) == 42
    else:
        with pytest.raises(OperationalError) as caught:
            await recover_read(operation, component="db_query_sql", retries=2)
        assert caught.value is original
    assert calls == expected_calls


@pytest.mark.asyncio
async def test_nested_database_retries_share_budget():
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise wrap(driver_error("pymysql.err", 1213, "deadlock"))

    with pytest.raises(OperationalError):
        await recover_read(
            lambda: recover_read(operation, component="db.inner", retries=5),
            component="db.outer", retries=1,
        )
    assert calls == 2


@pytest.fixture
def locked_database(tmp_path):
    path = tmp_path / "locked.db"
    dialect = SQLiteDialect()
    engine = dialect.build_engine(
        None, default_path=path, connect_args={"timeout": 0.005}, poolclass=NullPool,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE numbers (value INTEGER)"))
        connection.execute(text("INSERT INTO numbers VALUES (42)"))
    service = DBService(dialect=dialect, engine=engine, db_cfg=DBConfig())
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        yield service, blocker
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_real_sqlite_busy_recovers_after_other_connection_unlocks(locked_database):
    service, blocker = locked_database
    errors = []
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        try:
            return await service.query_sql("SELECT value FROM numbers")
        except OperationalError as error:
            errors.append(error)
            # Release the actual exclusive lock only after the read has failed.
            blocker.rollback()
            raise

    ctx = RequestContext.new()
    with bind_context(ctx):
        result = await recover_read(operation, component="db_query_sql", retries=2)
    assert result.rows == [[42]]
    assert calls == 2
    assert len(errors) == 1
    assert errors[0].orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
    event, = ctx.extra["recovery_events"]
    assert (event["attempts"], event["retries"], event["outcome"]) == (2, 1, "ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 2])
async def test_real_sqlite_busy_stops_at_retry_budget(locked_database, retries):
    service, _ = locked_database
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return await service.query_sql("SELECT value FROM numbers")

    with pytest.raises(OperationalError) as caught:
        await recover_read(operation, component="db_query_sql", retries=retries)
    assert caught.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
    assert calls == retries + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", ["SELECT value FROM absent_table", "SELECT * FRM numbers"])
async def test_real_sqlite_sql_errors_are_not_retried(locked_database, sql):
    service, blocker = locked_database
    blocker.rollback()
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return await service.query_sql(sql)

    with pytest.raises(OperationalError) as caught:
        await recover_read(operation, component="db_query_sql", retries=2)
    assert caught.value.orig.sqlite_errorcode == sqlite3.SQLITE_ERROR
    assert calls == 1


@pytest.mark.asyncio
async def test_workspace_db_tool_recovers_real_busy_on_pinned_revision(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "numbers.csv").write_text("value\n42\n")
    settings = Settings(
        llm={"api_key": "test", "model": "test"},
        data={"base_dir": tmp_path / "runtime"},
        workspace={"retention": "all"},
    )
    workspace = Workspace(settings)
    manifest = await workspace.update(str(source), surfaces=("db",))
    async with workspace.reader(read_retries=1) as reader:
        service = reader._system.services.db
        table, = await service.list_tables()
        service.engine.dispose()

        @event.listens_for(service.engine, "connect")
        def set_busy_timeout(connection, _):
            connection.execute("PRAGMA busy_timeout=5")

        original = service.query_sql
        calls = 0
        blocker = sqlite3.connect(service.engine.url.database, isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")

        async def query(sql):
            nonlocal calls
            calls += 1
            try:
                return await original(sql)
            except OperationalError as error:
                assert error.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
                blocker.rollback()
                raise

        monkeypatch.setattr(service, "query_sql", query)
        ctx = RequestContext.new()
        try:
            with bind_context(ctx):
                result = await reader.call("db_query_sql", sql=f'SELECT value FROM "{table}"')
        finally:
            blocker.rollback()
            blocker.close()
        assert str(result["rows"][0][0]) == "42"
        assert calls == 2
        assert reader.revision == manifest["workspace_revision"]
        record, = ctx.extra["recovery_events"]
        assert record["retries"] == 1
        assert record["workspace_revision"] == reader.revision
