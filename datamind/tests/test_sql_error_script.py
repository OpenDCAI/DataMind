"""Public SQL error script reports actual behavior and explicit driver skips."""
import csv
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_mock_engine
from sqlalchemy.exc import DBAPIError

from benchmark import sql_errors
from datamind.core.recovery import is_transient
from datamind.experiment_db_errors import DB_FAULTS, FaultDriverUnavailable, build_db_fault
from datamind.experiment_faults import ExperimentFaultError, before_db_read, db_faults

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", list(DB_FAULTS))
@pytest.mark.parametrize("wrapped", [False, True])
def test_real_driver_classes_match_catalog_labels(name, wrapped):
    spec = DB_FAULTS[name]
    if spec.backend != "sqlite":
        pytest.importorskip("pymysql" if spec.backend == "mysql" else "psycopg")
    error = build_db_fault(name, wrapped=wrapped)
    assert isinstance(error, DBAPIError) is wrapped
    assert sql_errors.error_code(error) == spec.code
    assert is_transient(error) is spec.retryable
    assert error.datamind_fault_injected is True
    raw = error.orig if wrapped else error
    module = type(raw).__module__
    assert module.startswith({"sqlite": "sqlite3", "mysql": "pymysql", "postgresql": "psycopg"}[spec.backend])


def test_sqlite_script_writes_all_reports_and_runs_real_errors(tmp_path):
    output = tmp_path / "sql-errors"
    result = subprocess.run([
        sys.executable, "-m", "benchmark.sql_errors", "--backend", "sqlite",
        "--failure-probabilities", "0", "1", "--output", str(output),
    ], cwd=REPO, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary == {"rows": 100, "passed": 100, "failed": 0, "skipped": 0,
                       "live_mysql_postgres_servers": False}
    rows = [json.loads(line) for line in (output / "rows.jsonl").read_text().splitlines()]
    with (output / "results.csv").open() as handle:
        assert len(list(csv.DictReader(handle))) == len(rows)
    assert "native_missing_column" in (output / "REPORT.md").read_text()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["code_hash"] and manifest["result_kind"] == "sql_error_mechanism_checks"
    for row in rows:
        if row["scenario"] == "real_busy_release" and row["policy"] == "retry":
            assert row["operation_succeeded"] and row["attempts"] == 2
            assert row["sql_executions"] == 2 and row["injected"] == 0
        if row["scenario"] == "native_syntax":
            assert not row["operation_succeeded"] and row["check_passed"]
            assert row["observed_codes"] == [1]
    # Existing reports and inputs are never overwritten.
    again = subprocess.run([sys.executable, "-m", "benchmark.sql_errors", "--output", str(output)],
                           cwd=REPO, capture_output=True, timeout=20)
    assert again.returncode != 0
    assert json.loads((output / "summary.json").read_text()) == summary


@pytest.mark.parametrize("backend,name,driver", [
    ("mysql", "mysql_deadlock", "pymysql"),
    ("postgresql", "postgres_serialization", "psycopg"),
    ("postgresql", "postgres_admin_shutdown", "psycopg"),
    ("postgresql", "postgres_lock_not_available", "psycopg"),
])
def test_serverless_driver_checks_do_not_claim_sql_execution(tmp_path, backend, name, driver):
    pytest.importorskip(driver)
    output = tmp_path / backend
    assert sql_errors.main(["--backend", backend, "--errors", name, "--require-drivers",
                            "--failure-probabilities", "0", "1", "--output", str(output)]) == 0
    rows = [json.loads(line) for line in (output / "rows.jsonl").read_text().splitlines()]
    assert len(rows) == 8 and all(row["check_passed"] for row in rows)
    assert all(row["execution_kind"] == "driver_contract_simulation"
               and row["sql_executions"] is None and row["result_match"] is None for row in rows)
    if name in {"postgres_admin_shutdown", "postgres_lock_not_available"}:
        for row in rows:
            assert row["classified_retryable"] is False
            if row["probability"] == 1:
                assert row["observed_codes"] == [DB_FAULTS[name].code]
                assert row["attempts"] == 1
                assert row["recovery"]["retries"] == 0
                assert not row["operation_succeeded"]


@pytest.mark.parametrize("required,exit_code", [(False, 0), (True, 1)])
def test_missing_optional_driver_is_an_explicit_skip(tmp_path, monkeypatch, required, exit_code):
    original = importlib.import_module

    def missing_driver(name, *args, **kwargs):
        if name == "pymysql":
            raise ModuleNotFoundError("driver unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", missing_driver)
    with pytest.raises(FaultDriverUnavailable):
        build_db_fault("mysql_deadlock")
    output = tmp_path / "missing"
    args = ["--backend", "mysql", "--errors", "mysql_deadlock", "--wrapping", "raw",
            "--failure-probabilities", "1", "--output", str(output)]
    assert sql_errors.main(args + (["--require-drivers"] if required else [])) == exit_code
    summary = json.loads((output / "summary.json").read_text())
    assert summary["passed"] == summary["failed"] == 0 and summary["skipped"] == 2


def test_bad_classification_fails_the_script_instead_of_passing_expected_sql_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(sql_errors, "is_transient", lambda error: True)
    output = tmp_path / "bad-classifier"
    assert sql_errors.main(["--backend", "sqlite", "--errors", "sqlite_locked", "--wrapping", "raw",
                            "--failure-probabilities", "1", "--skip-native", "--output", str(output)]) == 1
    summary = json.loads((output / "summary.json").read_text())
    assert summary["failed"] == 2


@pytest.mark.asyncio
async def test_provider_driver_setup_failure_is_not_a_target_database_outage(tmp_path, monkeypatch):
    original = importlib.import_module

    def missing_driver(name, *args, **kwargs):
        if name == "pymysql":
            raise ModuleNotFoundError("driver unavailable")
        return original(name, *args, **kwargs)

    engine = create_mock_engine("mysql://", lambda *a, **k: None)
    monkeypatch.setattr(importlib, "import_module", missing_driver)
    plan = {"component": "db", "error": "mysql_deadlock", "probability": 1,
            "seed": 7, "task": "q", "state": str(tmp_path / "state.db")}
    with db_faults(plan), pytest.raises(ExperimentFaultError) as caught:
        await before_db_read(engine)
    assert isinstance(caught.value.__cause__, FaultDriverUnavailable)
    assert not is_transient(caught.value)


@pytest.mark.parametrize("extra", [
    ["--failure-probabilities", "nan"], ["--retries", "6"], ["--fault-delay-ms", "-1"],
    ["--backend", "sqlite", "--errors", "mysql_deadlock"],
])
def test_invalid_cli_does_not_create_output(tmp_path, extra):
    output = tmp_path / "invalid"
    with pytest.raises(SystemExit) as caught:
        sql_errors.main(["--output", str(output), *extra])
    assert caught.value.code == 2
    assert not output.exists()
