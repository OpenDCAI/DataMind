"""Batch SQL error/recovery checks; no external database or model is contacted.

    python -m benchmark.sql_errors --output /tmp/new-sql-errors

SQLite scenarios execute real local SELECTs. MySQL/PostgreSQL scenarios use
actual optional driver exceptions and a synthetic successful return value;
their results are exception-contract checks, never server integration results.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import math
import platform
import random
import sqlite3
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from sqlalchemy import create_mock_engine, event, text
from sqlalchemy.exc import DBAPIError

from datamind.capabilities.db.providers.sqlite import SQLiteDialect
from datamind.capabilities.db.service import DBService
from datamind.config import DBConfig
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from datamind.core.recovery import is_transient, recover_read, recovery_summary
from datamind.experiment_db_errors import DB_FAULTS, FaultDriverUnavailable, build_db_fault
from datamind.experiment_faults import before_db_read, db_counts, db_faults

REPO = Path(__file__).resolve().parents[1]


def error_code(error):
    raw = error.orig if isinstance(error, DBAPIError) else error
    return (getattr(raw, "sqlite_errorcode", None) or getattr(raw, "sqlstate", None)
            or getattr(raw, "pgcode", None) or getattr(raw, "errno", None)
            or (raw.args[0] if raw.args and isinstance(raw.args[0], int) else None))


def expected_schedule(seed, task, probability, retries):
    """Independent oracle for probability-only schedules (no timing assumptions)."""
    for attempt in range(retries + 1):
        if random.Random(f"{seed}:{task}:{attempt}").random() >= probability:
            return attempt + 1, True
    return retries + 1, False


async def run_simulated(args, output, service, name, wrapping, policy, probability, repeat):
    spec = DB_FAULTS[name]
    base = {"backend": spec.backend, "scenario": name, "wrapping": wrapping,
            "policy": policy, "probability": probability, "repeat": repeat,
            "fault_origin": "simulated_exception", "expected_code": spec.code,
            "expected_retryable": spec.retryable,
            "execution_kind": "sqlite_query" if spec.backend == "sqlite" else "driver_contract_simulation"}
    try:
        sample = build_db_fault(name, wrapped=wrapping == "sqlalchemy")
    except FaultDriverUnavailable:
        return {**base, "status": "skipped", "skip_reason": "optional_driver_missing",
                "check_passed": None, "sql_executions": None}
    classified = is_transient(sample)
    budget = args.retries if policy == "retry" else 0
    task = f"{name}:{repeat}"
    state = output / "fault-states" / f"{name}.{wrapping}.{policy}.{probability}.{repeat}.sqlite3"
    plan = {"component": "db", "error": name, "probability": probability,
            "seed": args.seed, "task": task, "state": str(state),
            "outage_ms": args.outage_ms, "delay_ms": args.fault_delay_ms}
    target = (service.engine if spec.backend == "sqlite" else
              create_mock_engine(f"{spec.backend}://", lambda *a, **k: None))
    observed = []

    async def operation():
        try:
            if spec.backend == "sqlite":
                return (await service.query_sql("SELECT value FROM numbers")).rows
            await before_db_read(target)
            return [[42]]  # contract-only success; no SQL/server execution
        except Exception as error:
            if wrapping == "raw" and isinstance(error, DBAPIError):
                error = error.orig
            observed.append(error)
            raise error from None

    context = RequestContext.new()
    context.extra["query_deadline"] = time.monotonic() + args.deadline_s
    started = time.monotonic()
    result = None
    failure = None
    with bind_context(context), db_faults(plan):
        try:
            result = await recover_read(operation, component="db_query_sql", retries=budget)
        except Exception as error:
            failure = error
    actual = db_counts(state)
    passed = (classified == spec.retryable and error_code(sample) == spec.code
              and actual["attempts"] <= budget + 1)
    if args.outage_ms == 0 and args.fault_delay_ms == 0:
        expected_attempts, expected_success = expected_schedule(
            args.seed, task, probability, budget if spec.retryable else 0,
        )
        passed &= actual["attempts"] == expected_attempts and (failure is None) == expected_success
    if failure is None:
        passed &= result == [[42]]
    return {**base, "status": "completed", "check_passed": bool(passed),
            "classified_retryable": classified, "observed_codes": [error_code(e) for e in observed],
            "attempts": actual["attempts"], "injected": actual["injected"],
            "sql_executions": actual["executed"] if spec.backend == "sqlite" else None,
            "operation_succeeded": failure is None,
            "result_match": result == [[42]] if spec.backend == "sqlite" else None,
            "final_error_type": type(failure).__name__ if failure else None,
            "recovery": recovery_summary(context.extra.get("recovery_events", [])),
            "latency_ms": (time.monotonic() - started) * 1000}


async def run_native(args, service, name, wrapping, policy):
    queries = {
        "native_syntax": "SELECT FROM numbers",
        "native_missing_table": "SELECT value FROM missing_table",
        "native_missing_column": "SELECT absent FROM numbers",
    }
    locked = name.startswith("real_busy")
    retryable = locked
    budget = args.retries if policy == "retry" else 0
    blocker = None
    calls = 0
    executions = 0
    errors = []

    def count_execution(conn, cursor, statement, parameters, context, executemany):
        nonlocal executions
        if statement.startswith("SELECT * FROM ("):
            executions += 1

    event.listen(service.engine, "before_cursor_execute", count_execution)
    if locked:
        blocker = sqlite3.connect(service.engine.url.database, isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")

    async def operation():
        nonlocal calls
        calls += 1
        try:
            return (await service.query_sql(queries.get(name, "SELECT value FROM numbers"))).rows
        except Exception as error:
            if name == "real_busy_release" and error_code(error) == sqlite3.SQLITE_BUSY:
                blocker.rollback()
            if wrapping == "raw" and isinstance(error, DBAPIError):
                error = error.orig
            errors.append(error)
            raise error from None

    context = RequestContext.new()
    context.extra["query_deadline"] = time.monotonic() + args.deadline_s
    failure = None
    result = None
    started = time.monotonic()
    try:
        with bind_context(context):
            try:
                result = await recover_read(operation, component="db_query_sql", retries=budget)
            except Exception as error:
                failure = error
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
        event.remove(service.engine, "before_cursor_execute", count_execution)
    expected_success = name == "real_busy_release" and budget > 0
    expected_attempts = min(2, budget + 1) if name == "real_busy_release" else budget + 1 if locked else 1
    expected_code = sqlite3.SQLITE_BUSY if locked else sqlite3.SQLITE_ERROR
    passed = (bool(errors) and all(error_code(error) == expected_code for error in errors)
              and is_transient(errors[0]) == retryable
              and calls == expected_attempts and (failure is None) == expected_success
              and (not expected_success or result == [[42]]))
    return {"backend": "sqlite", "scenario": name, "wrapping": wrapping, "policy": policy,
            "probability": None, "repeat": 0, "fault_origin": "real_lock" if locked else "native_sql_error",
            "execution_kind": "sqlite_query", "expected_code": 5 if locked else 1,
            "expected_retryable": retryable, "classified_retryable": is_transient(errors[0]) if errors else None,
            "observed_codes": [error_code(e) for e in errors], "status": "completed",
            "check_passed": passed, "attempts": calls, "injected": 0, "sql_executions": executions,
            "operation_succeeded": failure is None, "result_match": result == [[42]],
            "final_error_type": type(failure).__name__ if failure else None,
            "recovery": recovery_summary(context.extra.get("recovery_events", [])),
            "latency_ms": (time.monotonic() - started) * 1000}


def write_reports(output, rows, args):
    groups = defaultdict(list)
    for row in rows:
        groups[row["backend"], row["scenario"], row["wrapping"]].append(row)
    lines = ["# SQL error and recovery checks", "",
             "SQLite runs real local queries. MySQL/PostgreSQL use driver exceptions only; no server is contacted.",
             "A passed check means behavior matched expectations, including expected query failures.", "",
             "| Backend | Scenario | Wrapping | Passed | Failed | Skipped |",
             "|---|---|---|---:|---:|---:|"]
    for (backend, name, wrapping), batch in groups.items():
        lines.append(f"| {backend} | {name} | {wrapping} | "
                     f"{sum(r['check_passed'] is True for r in batch)} | "
                     f"{sum(r['check_passed'] is False for r in batch)} | "
                     f"{sum(r['status'] == 'skipped' for r in batch)} |")
    (output / "REPORT.md").write_text("\n".join(lines) + "\n")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (output / "results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()})
    summary = {"rows": len(rows), "passed": sum(r["check_passed"] is True for r in rows),
               "failed": sum(r["check_passed"] is False for r in rows),
               "skipped": sum(r["status"] == "skipped" for r in rows),
               "live_mysql_postgres_servers": False}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    versions = {}
    for package in ("sqlalchemy", "pymysql", "psycopg"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    manifest = {"result_kind": "sql_error_mechanism_checks", "arguments": vars(args),
                "git_commit": commit, "versions": versions, "sqlite_version": sqlite3.sqlite_version,
                "python": platform.python_version(),
                "code_hash": hashlib.sha256(json.dumps([
                    (str(path.relative_to(REPO)), hashlib.sha256(path.read_bytes()).hexdigest())
                    for package in ("datamind", "benchmark")
                    for path in sorted((REPO / package).rglob("*.py"))
                ]).encode()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return summary


async def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    dialect = SQLiteDialect()
    engine = dialect.build_engine(None, default_path=output / "target.sqlite3", connect_args={"timeout": 0.005})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE numbers (value INTEGER)"))
        connection.execute(text("INSERT INTO numbers VALUES (42)"))
    service = DBService(dialect=dialect, engine=engine, db_cfg=DBConfig(query_timeout_s=args.deadline_s))
    names = args.errors or [name for name, spec in DB_FAULTS.items()
                            if args.backend == "all" or spec.backend == args.backend]
    forms = ("raw", "sqlalchemy") if args.wrapping == "both" else (args.wrapping,)
    rows = []

    def append(row):
        rows.append(row)
        with (output / "rows.jsonl").open("a") as handle:
            handle.write(json.dumps(row) + "\n")

    try:
        for name in names:
            for wrapping in forms:
                for policy in ("none", "retry"):
                    for probability in args.failure_probabilities:
                        for repeat in range(args.repeats):
                            append(await run_simulated(args, output, service, name, wrapping, policy, probability, repeat))
        if args.backend in {"sqlite", "all"} and not args.skip_native:
            for name in ("native_syntax", "native_missing_table", "native_missing_column", "real_busy_release", "real_busy_persistent"):
                for wrapping in forms:
                    for policy in ("none", "retry"):
                        append(await run_native(args, service, name, wrapping, policy))
    finally:
        engine.dispose()
    summary = write_reports(output, rows, args)
    print(json.dumps({"output": str(output), **summary}))
    return 1 if summary["failed"] or (args.require_drivers and summary["skipped"]) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="new experiment directory")
    parser.add_argument("--backend", choices=["all", "sqlite", "mysql", "postgresql"], default="all")
    parser.add_argument("--errors", nargs="+", choices=sorted(DB_FAULTS))
    parser.add_argument("--wrapping", choices=["both", "raw", "sqlalchemy"], default="both")
    parser.add_argument("--failure-probabilities", nargs="+", type=float, default=[0, 0.3, 1])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--outage-ms", type=float, default=0)
    parser.add_argument("--fault-delay-ms", type=float, default=0)
    parser.add_argument("--deadline-s", type=float, default=5)
    parser.add_argument("--skip-native", action="store_true")
    parser.add_argument("--require-drivers", action="store_true", help="fail if optional driver scenarios were skipped")
    args = parser.parse_args(argv)
    if (any(not math.isfinite(p) or not 0 <= p <= 1 for p in args.failure_probabilities)
            or not 0 <= args.retries <= 5 or args.repeats < 1
            or not all(math.isfinite(v) and v >= 0 for v in (args.outage_ms, args.fault_delay_ms))
            or not math.isfinite(args.deadline_s) or args.deadline_s <= 0):
        parser.error("invalid probability, retry budget, repeat count or duration")
    if args.errors:
        args.errors = list(dict.fromkeys(args.errors))
        if args.backend != "all" and any(DB_FAULTS[name].backend != args.backend for name in args.errors):
            parser.error("--errors must match --backend")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
