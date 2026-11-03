"""Exercise the public experiment CLI, including a real child process exit."""
import json
import os
import subprocess
import sys
from pathlib import Path

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
