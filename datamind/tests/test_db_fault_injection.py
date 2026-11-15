"""Opt-in simulated DB faults use real SQLite reads and the existing recovery loop."""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from datamind import experiment_faults as faults
from datamind.capabilities.db.providers.sqlite import SQLiteDialect
from datamind.capabilities.db.safeguard import DestructiveSQLError
from datamind.capabilities.db.service import DBService
from datamind.config import DBConfig, Settings
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from datamind.core.recovery import is_transient, recover_read
from datamind.workspace import Workspace


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("DATAMIND_TEST_READ_FAULT", raising=False)
    dialect = SQLiteDialect()
    engine = dialect.build_engine(None, default_path=tmp_path / "target.db")
    try:
        yield DBService(dialect=dialect, engine=engine, db_cfg=DBConfig())
    finally:
        engine.dispose()


def plan(tmp_path, **kwargs):
    return {"component": "db", "error": "sqlite_busy", "probability": 1,
            "seed": 7, "task": "q", "state": str(tmp_path / "faults.db"), **kwargs}


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code,retryable", [
    ("sqlite_busy", 5, True), ("sqlite_locked", 6, False), ("sqlite_error", 1, False),
])
async def test_codes_are_sqlalchemy_wrapped_and_control_retries(service, tmp_path, error, code, retryable):
    config = plan(tmp_path, error=error)
    with faults.db_faults(config), pytest.raises(OperationalError) as caught:
        await recover_read(lambda: service.query_sql("SELECT 42"), component="db_query_sql", retries=2)
    assert caught.value.orig.sqlite_errorcode == code
    assert caught.value.datamind_fault_injected is True
    assert caught.value.__cause__ is None  # exercises .orig, not just __cause__
    assert is_transient(caught.value) is retryable
    assert faults.db_counts(config["state"]) == {
        "attempts": 3 if retryable else 1, "injected": 3 if retryable else 1, "executed": 0,
    }


@pytest.mark.asyncio
async def test_outage_recovers_after_window_and_state_survives_new_service(service, tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(faults, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    config = plan(tmp_path, probability=0, outage_ms=100)

    async def operation():
        # A new service instance must consume the same persisted schedule.
        recreated = DBService(dialect=SQLiteDialect(), engine=service.engine, db_cfg=DBConfig())
        try:
            return await recreated.query_sql("SELECT 42")
        except OperationalError:
            clock[0] += 0.2
            raise

    with faults.db_faults(config):
        result = await recover_read(operation, component="db_query_sql", retries=1)
    assert result.rows == [[42]]
    assert faults.db_counts(config["state"]) == {"attempts": 2, "injected": 1, "executed": 1}
    # Leaving the scope restores normal execution even with a 100% fault plan.
    assert (await service.query_sql("SELECT 42")).rows == [[42]]
    assert faults.db_counts(config["state"])["attempts"] == 2


@pytest.mark.asyncio
async def test_probability_schedule_is_paired_and_not_reset_on_reentry(service, tmp_path):
    schedules = []
    for policy in ("none", "retry"):
        config = plan(tmp_path, state=str(tmp_path / f"{policy}.db"), probability=0.4)
        schedule = []
        for _ in range(12):
            with faults.db_faults(config):
                try:
                    await service.query_sql("SELECT 42")
                except OperationalError:
                    schedule.append(True)
                else:
                    schedule.append(False)
        schedules.append(schedule)
        assert faults.db_counts(config["state"]) == {
            "attempts": 12, "injected": sum(schedule), "executed": 12 - sum(schedule),
        }
    assert schedules[0] == schedules[1]
    assert any(schedules[0]) and not all(schedules[0])


@pytest.mark.asyncio
async def test_delay_is_inside_query_timeout_and_never_executes_sql(service, tmp_path):
    config = plan(tmp_path, delay_ms=1000)
    service.db_cfg.query_timeout_s = 0.02
    with faults.db_faults(config), pytest.raises(TimeoutError):
        await service.query_sql("SELECT 42")
    assert faults.db_counts(config["state"]) == {"attempts": 1, "injected": 1, "executed": 0}


@pytest.mark.asyncio
async def test_overall_deadline_does_not_retry_delayed_injection(service, tmp_path):
    config = plan(tmp_path, delay_ms=1000)
    context = RequestContext.new()
    context.extra["query_deadline"] = time.monotonic() + 0.05
    with bind_context(context), faults.db_faults(config), pytest.raises(TimeoutError):
        await recover_read(lambda: service.query_sql("SELECT 42"), component="db_query_sql", retries=2)
    record, = context.extra["recovery_events"]
    assert record["attempts"] == 1 and record["retries"] == 0
    assert faults.db_counts(config["state"])["executed"] == 0


@pytest.mark.asyncio
async def test_cancellation_during_injected_delay_never_executes_sql(service, tmp_path):
    config = plan(tmp_path, delay_ms=10000)
    with faults.db_faults(config):
        task = asyncio.create_task(service.query_sql("SELECT 42"))
        try:
            async with asyncio.timeout(2):
                while True:
                    try:
                        if faults.db_counts(config["state"])["injected"]:
                            break
                    except faults.ExperimentFaultError:
                        pass  # the first state transaction may still be initializing
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert faults.db_counts(config["state"])["executed"] == 0


@pytest.mark.asyncio
async def test_sql_validation_and_metadata_are_not_injected(service, tmp_path):
    config = plan(tmp_path)
    with faults.db_faults(config):
        with pytest.raises(DestructiveSQLError):
            await service.query_sql("DELETE FROM numbers")
        assert await service.list_tables() == []
    assert not Path(config["state"]).exists()


@pytest.mark.asyncio
async def test_execution_counter_includes_real_sql_errors(service, tmp_path):
    config = plan(tmp_path, probability=0)
    with faults.db_faults(config), pytest.raises(OperationalError) as caught:
        await service.query_sql("SELECT * FROM missing_table")
    assert not getattr(caught.value, "datamind_fault_injected", False)
    assert faults.db_counts(config["state"]) == {"attempts": 1, "injected": 0, "executed": 1}


@pytest.mark.asyncio
async def test_state_database_lock_is_not_a_target_db_fault_or_retried(service, tmp_path):
    config = plan(tmp_path, probability=0)
    with faults.db_faults(config):
        await service.query_sql("SELECT 42")
        blocker = sqlite3.connect(config["state"], isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            return await service.query_sql("SELECT 42")

        try:
            with pytest.raises(faults.ExperimentFaultError) as caught:
                await recover_read(operation, component="db_query_sql", retries=2)
            assert caught.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert not is_transient(caught.value)
            assert calls == 1
        finally:
            blocker.rollback()
            blocker.close()
    assert faults.db_counts(config["state"])["attempts"] == 1


@pytest.mark.parametrize("kwargs", [
    {"probability": -1}, {"probability": float("nan")}, {"probability": float("inf")},
    {"delay_ms": -1}, {"outage_ms": float("nan")}, {"error": "unknown"}, {"state": ""},
])
def test_invalid_plan_fails_before_creating_state(tmp_path, kwargs):
    with pytest.raises(faults.ExperimentFaultError):
        with faults.db_faults(plan(tmp_path, **kwargs)):
            pytest.fail("invalid plan was accepted")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_target_cannot_be_its_own_fault_state(service, tmp_path):
    with faults.db_faults(plan(tmp_path, state=service.engine.url.database)):
        with pytest.raises(faults.ExperimentFaultError):
            await service.query_sql("SELECT 42")


@pytest.mark.asyncio
async def test_non_sqlite_backend_is_rejected_instead_of_spoofed(tmp_path):
    with faults.db_faults(plan(tmp_path)):
        with pytest.raises(faults.ExperimentFaultError, match="SQLite only"):
            await faults.before_db_read(SimpleNamespace(dialect=SimpleNamespace(name="mysql")))
    assert not Path(plan(tmp_path)["state"]).exists()


def test_db_environment_plan_does_not_wrap_kb(tmp_path, monkeypatch):
    config = plan(tmp_path)
    monkeypatch.setenv("DATAMIND_TEST_READ_FAULT", json.dumps(config))
    faults.install(SimpleNamespace())  # no embedding or vector_store needed
    assert not Path(config["state"]).exists()


@pytest.mark.asyncio
async def test_concurrent_query_plans_are_isolated(service, tmp_path):
    async def query(name, probability):
        config = plan(tmp_path, probability=probability, state=str(tmp_path / f"{name}.db"))
        with faults.db_faults(config):
            await asyncio.sleep(0)
            return await service.query_sql("SELECT 42")

    failed, successful = await asyncio.gather(query("failed", 1), query("successful", 0), return_exceptions=True)
    assert isinstance(failed, OperationalError)
    assert successful.rows == [[42]]
    assert faults.db_counts(tmp_path / "failed.db")["injected"] == 1
    assert faults.db_counts(tmp_path / "successful.db")["executed"] == 1


@pytest.mark.asyncio
async def test_concurrent_attempts_are_counted_once(service, tmp_path):
    config = plan(tmp_path)
    with faults.db_faults(config):
        results = await asyncio.gather(*(service.query_sql("SELECT 42") for _ in range(8)), return_exceptions=True)
    assert all(isinstance(result, OperationalError) for result in results)
    assert faults.db_counts(config["state"]) == {"attempts": 8, "injected": 8, "executed": 0}


@pytest.mark.asyncio
async def test_env_plan_skips_build_and_applies_to_workspace_read(tmp_path, monkeypatch):
    config = plan(tmp_path)
    monkeypatch.setenv("DATAMIND_TEST_READ_FAULT", json.dumps(config))
    source = tmp_path / "source"
    source.mkdir()
    (source / "numbers.csv").write_text("value\n42\n")
    workspace = Workspace(Settings(llm={"api_key": "test", "model": "test"},
                                   data={"base_dir": tmp_path / "runtime"}))
    manifest = await workspace.update(str(source), surfaces=("db",))
    assert not Path(config["state"]).exists()
    async with workspace.reader(read_retries=1) as reader:
        table, = await reader._system.services.db.list_tables()
        with pytest.raises(OperationalError):
            await reader.call("db_query_sql", sql=f'SELECT value FROM "{table}"')
        assert reader.revision == manifest["workspace_revision"]
    assert faults.db_counts(config["state"]) == {"attempts": 2, "injected": 2, "executed": 0}


def test_env_schedule_survives_child_process_recreation(tmp_path):
    config = plan(tmp_path)
    script = '''
import asyncio, os
from datamind.capabilities.db.providers.sqlite import SQLiteDialect
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from sqlalchemy.exc import OperationalError
async def main():
    dialect=SQLiteDialect()
    engine=dialect.build_engine("sqlite:///:memory:")
    context=RequestContext.new()
    context.extra["workspace_revision"]="test-revision"
    try:
        with bind_context(context):
            try: await dialect.execute_readonly(engine,"SELECT 42")
            except OperationalError as error: assert error.orig.sqlite_errorcode == 5
            else: raise AssertionError("fault not injected")
    finally: engine.dispose()
asyncio.run(main())
'''
    for _ in range(2):
        subprocess.run([sys.executable, "-c", script], check=True, capture_output=True,
                       cwd=Path(__file__).resolve().parents[2], timeout=20,
                       env={**os.environ, "DATAMIND_TEST_READ_FAULT": json.dumps(config)})
    assert faults.db_counts(config["state"]) == {"attempts": 2, "injected": 2, "executed": 0}
