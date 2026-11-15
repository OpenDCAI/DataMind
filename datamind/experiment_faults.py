"""Opt-in benchmark faults at the real provider boundary, shared across MCP calls.

Only a trusted experiment host supplies this environment plan. There is no model
or document-facing tool for enabling faults. Normal execution does not create state.
"""
import asyncio
import contextvars
import json
import math
import os
import random
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import OperationalError

from datamind.core.errors import ConfigError
from datamind.core.logging import current_context


def install(kb):
    raw=os.environ.get('DATAMIND_TEST_READ_FAULT')
    if not raw or kb is None:return
    plan=json.loads(raw)
    # DB faults are installed at the shared read executor, not on KB providers.
    if plan.get('component') == 'db':return
    if plan['component'] not in {'embedding','vector'}:
        raise ValueError('Unsupported experiment component')
    probability=float(plan['probability'])
    if not 0<=probability<=1:raise ValueError('Invalid fault probability')
    path=Path(plan['state']);path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS fault_state(singleton INTEGER PRIMARY KEY, started REAL, attempts INTEGER, injected INTEGER)')
        db.execute('INSERT OR IGNORE INTO fault_state VALUES(1,?,0,0)',(time.monotonic(),))
    provider=kb.embedding if plan['component']=='embedding' else kb.vector_store
    method='embed_query' if plan['component']=='embedding' else 'query'
    original=getattr(provider,method)
    async def faulty(*args,**kwargs):
        with sqlite3.connect(path,timeout=30) as db:
            db.execute('BEGIN IMMEDIATE')
            started,attempts,injected=db.execute('SELECT started,attempts,injected FROM fault_state WHERE singleton=1').fetchone()
            fail=(time.monotonic()-started<float(plan.get('outage_ms',0))/1000 or
                  random.Random(f"{plan['seed']}:{plan['task']}:{attempts}").random()<probability)
            db.execute('UPDATE fault_state SET attempts=?,injected=? WHERE singleton=1',(attempts+1,injected+int(fail)))
        if fail:
            await asyncio.sleep(float(plan.get('delay_ms',0))/1000)
            raise TimeoutError('injected experiment provider outage')
        return await original(*args,**kwargs)
    setattr(provider,method,faulty)


def counts(path):
    if not Path(path).exists():return {'attempts':0,'injected':0}
    with sqlite3.connect(path) as db:
        attempts,injected=db.execute('SELECT attempts,injected FROM fault_state WHERE singleton=1').fetchone()
    return {'attempts':attempts,'injected':injected}


class ExperimentFaultError(ConfigError):
    """Invalid experiment setup or state failure; never a retryable DB outage."""


_SQLITE_ERRORS = {
    "sqlite_busy": (sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
    "sqlite_locked": (sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED"),
    "sqlite_error": (sqlite3.SQLITE_ERROR, "SQLITE_ERROR"),
}


@dataclass(frozen=True)
class _DBPlan:
    state: Path
    error: str
    probability: float
    outage_ms: float
    delay_ms: float
    seed: str
    task: str


_DB_PLAN: contextvars.ContextVar[_DBPlan | None] = contextvars.ContextVar("db_fault_plan", default=None)


def _parse_db_plan(plan):
    try:
        if plan.get("component") != "db":
            raise ValueError("component must be db")
        error = plan.get("error", "sqlite_busy")
        if error not in _SQLITE_ERRORS:
            raise ValueError("unsupported DB fault; expected sqlite_busy, sqlite_locked or sqlite_error")
        probability = float(plan["probability"])
        outage = float(plan.get("outage_ms", 0))
        delay = float(plan.get("delay_ms", 0))
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("probability must be finite and between 0 and 1")
        if not all(math.isfinite(v) and v >= 0 for v in (outage, delay)):
            raise ValueError("outage_ms and delay_ms must be finite and nonnegative")
        if not isinstance(plan["state"], str) or not plan["state"]:
            raise ValueError("state must be a nonempty path")
        return _DBPlan(Path(plan["state"]).expanduser().resolve(), error, probability,
                       outage, delay, str(plan["seed"]), str(plan["task"]))
    except (KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
        raise ExperimentFaultError("Invalid DB fault plan: " + str(exc)) from exc


@contextmanager
def db_faults(plan):
    """Enable a plan for this query/task only, including its retries and workers.

    No provider is monkey-patched, so recreating a service cannot double-install
    the fault. The shared state file preserves the schedule across MCP processes.
    """
    token = _DB_PLAN.set(_parse_db_plan(plan))
    try:
        yield
    finally:
        _DB_PLAN.reset(token)


def _db_plan():
    plan = _DB_PLAN.get()
    if plan is not None:
        return plan
    # Child MCP processes receive the trusted host's environment. Activate only
    # inside a pinned workspace read, never while preparing/building a revision.
    context = current_context()
    raw = os.environ.get("DATAMIND_TEST_READ_FAULT")
    if not raw or context is None or not context.extra.get("workspace_revision"):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ExperimentFaultError("Invalid read fault JSON") from exc
    if not isinstance(value, dict):
        raise ExperimentFaultError("Read fault plan must be an object")
    return _parse_db_plan(value) if value.get("component") == "db" else None


@contextmanager
def _db_state(path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bound contention in the experiment store separately from the target DB.
        connection = sqlite3.connect(path, timeout=0.1)
        try:
            with connection:
                yield connection
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        raise ExperimentFaultError("DB fault state is unavailable") from exc


def _db_attempt(plan):
    with _db_state(plan.state) as state:
        state.execute("BEGIN IMMEDIATE")
        state.execute("CREATE TABLE IF NOT EXISTS db_fault_state ("
                      "singleton INTEGER PRIMARY KEY, started REAL, attempts INTEGER, "
                      "injected INTEGER, executed INTEGER)")
        state.execute("INSERT OR IGNORE INTO db_fault_state VALUES (1,?,0,0,0)", (time.monotonic(),))
        started, attempts = state.execute(
            "SELECT started,attempts FROM db_fault_state WHERE singleton=1"
        ).fetchone()
        fail = (time.monotonic() - started < plan.outage_ms / 1000 or
                random.Random(f"{plan.seed}:{plan.task}:{attempts}").random() < plan.probability)
        state.execute("UPDATE db_fault_state SET attempts=attempts+1,injected=injected+? WHERE singleton=1",
                      (int(fail),))
    return fail


async def before_db_read(engine):
    """Called after SQL validation and inside the query's timeout boundary."""
    plan = _db_plan()
    if plan is None:
        return None
    if engine.dialect.name != "sqlite":
        raise ExperimentFaultError("DB fault injection currently supports SQLite only")
    database = engine.url.database
    if database and database != ":memory:" and Path(database).resolve() == plan.state:
        raise ExperimentFaultError("Fault state must be separate from the target database")
    fail = await asyncio.to_thread(_db_attempt, plan)
    if fail:
        await asyncio.sleep(plan.delay_ms / 1000)
        code, name = _SQLITE_ERRORS[plan.error]
        original = sqlite3.OperationalError("injected experiment DB fault")
        original.sqlite_errorcode = code
        original.sqlite_errorname = name
        error = OperationalError(None, None, original)
        error.datamind_fault_injected = True
        raise error
    return plan.state


def record_db_execution(state_path):
    """Count entries to conn.execute, not merely attempts or successful results."""
    if state_path is not None:
        with _db_state(state_path) as state:
            state.execute("UPDATE db_fault_state SET executed=executed+1 WHERE singleton=1")


def db_counts(path):
    path = Path(path)
    if not path.exists():
        return {"attempts": 0, "injected": 0, "executed": 0}
    with _db_state(path) as state:
        row = state.execute("SELECT attempts,injected,executed FROM db_fault_state WHERE singleton=1").fetchone()
    if row is None:
        raise ExperimentFaultError("DB fault state has no counter row")
    return dict(zip(("attempts", "injected", "executed"), row))
