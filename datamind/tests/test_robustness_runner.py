"""Exercise the public experiment CLI, including a real child process exit."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def run_cli(*args):
    result = subprocess.run([sys.executable, "-m", "benchmark.robustness", *args],
        cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode()


def test_query_experiment_pairs_policies_and_labels_fixture_results(tmp_path):
    output = tmp_path / "query"
    run_cli("query", "--fixture", "--output", str(output),
            "--failure-probabilities", "0", "1", "--repeats", "1")
    manifest = json.loads((output / "manifest.json").read_text())
    summary = json.loads((output / "summary.json").read_text())
    assert manifest["result_kind"] == "fixture_smoke" and manifest["code_hash"]
    groups = {(g["policy"], g["failure_probability"]): g for g in summary["groups"]}
    assert groups["none", 1]["completion_rate"] == 0
    assert groups["retry", 1]["completion_rate"] == 0
    assert groups["fallback", 1]["source_hit_rate"] == 1
    assert groups["fallback", 1]["exact_answer_match_rate"] is None
    rows = [json.loads(line) for line in (output / "rows.jsonl").read_text().splitlines()]
    assert len(rows) == 12
    assert len({r["workspace_revision"] for r in rows}) == 1
    degraded = [r for r in rows if r["policy"] == "fallback" and r["failure_probability"] == 1]
    assert all(r["result"]["retrieval"]["degraded"] for r in degraded)


def test_process_exit_recovery_experiment_matches_cold_content(tmp_path):
    output = tmp_path / "build"
    run_cli("build", "--fixture", "--after", "2", "--output", str(output))
    result = json.loads((output / "summary.json").read_text())
    assert result["fixture"] and result["logical_content_equal"]
    assert result["resume_metrics"]["sources_reused"] == 2
    assert result["cold_metrics"]["sources_reused"] == 0
    assert result["resume_metrics"]["graph_extraction_calls"] < result["cold_metrics"]["graph_extraction_calls"]
    assert result["resume_metrics"]["materialization_ms"] > 0


@pytest.mark.parametrize("error,retry_attempts", [
    ("sqlite_busy", 2), ("sqlite_locked", 1), ("sqlite_error", 1),
])
def test_db_experiment_executes_sql_and_distinguishes_attempts_from_execution(tmp_path, error, retry_attempts):
    output = tmp_path / "db"
    run_cli("query", "--fixture", "--surfaces", "db", "--execution", "sql",
            "--component", "db", "--db-error", error, "--output", str(output),
            "--failure-probabilities", "0", "1", "--repeats", "1")
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["result_kind"] == "fixture_smoke"
    assert manifest["harness"]["db_error"] == error
    rows = [json.loads(line) for line in (output / "rows.jsonl").read_text().splitlines()]
    assert len(rows) == 8
    assert {row["policy"] for row in rows} == {"none", "retry"}
    assert len({row["workspace_revision"] for row in rows}) == 1
    for row in rows:
        assert row["fault_origin"] == "simulated_exception"
        assert row["source_hit"] is None and row["exact_answer_match"] is None
        if row["failure_probability"] == 0:
            assert row["outcome"] == "ok" and row["result_match"] is True
            assert row["component_attempts"] == row["sql_executions"] == 1
            assert row["injected_faults"] == row["recovery"]["retries"] == 0
        else:
            attempts = retry_attempts if row["policy"] == "retry" else 1
            assert row["outcome"] == "error" and row["result_match"] is False
            assert row["component_attempts"] == row["injected_faults"] == attempts
            assert row["sql_executions"] == 0
            assert row["recovery"]["retries"] == attempts - 1
    summary = json.loads((output / "summary.json").read_text())
    for group in summary["groups"]:
        expected = 1 if group["failure_probability"] == 0 else 0
        assert group["completion_rate"] == group["result_match_rate"] == expected


def test_db_experiment_scores_explicit_sql_tasks_without_labels(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text('{"task_id":"q","sql":"SELECT 42"}\n')
    output = tmp_path / "db"
    run_cli("query", "--fixture", "--surfaces", "db", "--execution", "sql",
            "--component", "db", "--questions", str(tasks), "--output", str(output),
            "--failure-probabilities", "0", "--repeats", "1")
    rows = [json.loads(line) for line in (output / "rows.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert all(row["result"]["rows"] == [[42]] and row["result_match"] is None for row in rows)


@pytest.mark.parametrize("extra", [
    ("--component", "db"),
    ("--execution", "sql"),
    ("--component", "db", "--execution", "sql", "--surfaces", "kb"),
    ("--outage-ms", "nan"),
])
def test_invalid_db_experiment_options_fail_before_build(tmp_path, extra):
    output = tmp_path / "db"
    result = subprocess.run([sys.executable, "-m", "benchmark.robustness", "query", "--fixture",
                             "--output", str(output), *extra], cwd=REPO, capture_output=True, timeout=20)
    assert result.returncode != 0
    assert not output.exists()
