"""Executable acceptance tests for the repository-level benchmark."""
from __future__ import annotations

import ast
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from benchmarks import (
    BenchmarkRunner,
    BenchmarkValidationError,
    PlanConstraints,
    RunnerMode,
    TaskSpec,
    diagnostic_report,
)
from benchmarks.run_v01 import main, run_v01
from benchmarks.v0 import default_registry, load_v01_tasks
from datamind.adapters.audit import InMemoryTraceStore


class BenchmarkSchemaTests(unittest.TestCase):
    def test_v01_has_unique_versioned_non_executable_tasks(self) -> None:
        tasks = load_v01_tasks()

        self.assertEqual(len(tasks), 14)
        self.assertEqual(len({item.task_id for item in tasks}), 14)
        self.assertTrue(all(item.version == "0.1" for item in tasks))
        task_directory = (
            Path(__file__).resolve().parents[2]
            / "benchmarks"
            / "tasks"
            / "v0"
        )
        for path in task_directory.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("python", payload)
            self.assertNotIn("code", payload)
            self.assertNotIn("callable", payload)

    def test_unknown_task_fields_are_rejected(self) -> None:
        task = load_v01_tasks()[0]
        payload = {
            "schema": task.schema,
            "version": task.version,
            "task_id": task.task_id,
            "title": task.title,
            "layer": task.layer.value,
            "fixture_id": task.fixture_id,
            "workload_id": task.workload_id,
            "assertions": [
                {
                    "name": "result",
                    "oracle": "result_kind",
                    "params": {"expected": "document_hits"},
                }
            ],
            "python": "arbitrary()",
        }

        with self.assertRaises(BenchmarkValidationError):
            TaskSpec.from_dict(payload)

    def test_task_contract_has_lossless_json_codec(self) -> None:
        for task in load_v01_tasks():
            encoded = json.dumps(task.to_dict(), sort_keys=True)
            decoded = TaskSpec.from_dict(json.loads(encoded))

            self.assertEqual(decoded, task)

    def test_registry_rejects_unknown_contract_references(self) -> None:
        task = replace(
            load_v01_tasks()[0],
            fixture_id="not-registered",
        )

        with self.assertRaises(BenchmarkValidationError):
            default_registry().validate_tasks((task,))

    def test_runtime_never_imports_repository_benchmark(self) -> None:
        package = Path(__file__).resolve().parents[1]
        for path in package.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ]
            for node in imports:
                module = getattr(node, "module", "") or ""
                names = tuple(alias.name for alias in node.names)
                self.assertFalse(
                    module == "benchmarks"
                    or module.startswith("benchmarks.")
                    or any(
                        name == "benchmarks"
                        or name.startswith("benchmarks.")
                        for name in names
                    ),
                    "{} imports benchmarks".format(path),
                )


class BenchmarkRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_canonical_v01_tasks_pass(self) -> None:
        suite = await run_v01()

        self.assertEqual(len(suite.runs), 14)
        self.assertEqual(suite.passed, 14)
        self.assertEqual(suite.failed, 0)

    async def test_memory_task_is_isolated_across_repeated_runs(self) -> None:
        task = next(
            item
            for item in load_v01_tasks()
            if item.task_id == "state.memory_assert_read"
        )
        runner = BenchmarkRunner(default_registry())

        first = await runner.run(task, run_id="isolated-one")
        second = await runner.run(task, run_id="isolated-two")

        self.assertTrue(first.succeeded)
        self.assertTrue(second.succeeded)
        self.assertEqual(
            first.observation.result.value.records[0].content,
            second.observation.result.value.records[0].content,
        )

    async def test_semantically_wrong_plan_constraint_fails_outcome(
        self,
    ) -> None:
        task = load_v01_tasks()[0]
        task = replace(
            task,
            plan_constraints=PlanConstraints(
                required_operations=("query",),
            ),
        )
        run = await BenchmarkRunner(default_registry()).run(
            task,
            run_id="wrong-constraint",
        )

        self.assertFalse(run.succeeded)
        failed = {
            item.name
            for item in run.outcome.assertions
            if not item.passed
        }
        self.assertEqual(failed, {"contract.operations"})

    async def test_scripted_compiler_exercises_bounded_replanning(
        self,
    ) -> None:
        task = next(
            item
            for item in load_v01_tasks()
            if RunnerMode.SCRIPTED_COMPILER in item.supported_modes
        )
        run = await BenchmarkRunner(default_registry()).run(
            task,
            mode=RunnerMode.SCRIPTED_COMPILER,
            run_id="scripted-replan",
        )

        self.assertTrue(run.succeeded)
        self.assertEqual(len(run.observation.plans), 2)
        self.assertEqual(
            len(run.observation.resolution.plan_attempts),
            2,
        )
        self.assertIsNotNone(run.observation.replayed)

    async def test_live_mode_requires_an_injected_compiler(self) -> None:
        task = replace(
            load_v01_tasks()[0],
            supported_modes=(RunnerMode.LIVE_PLANNER,),
            request="Find the policy.",
        )

        with self.assertRaises(BenchmarkValidationError):
            await BenchmarkRunner(default_registry()).run(
                task,
                mode=RunnerMode.LIVE_PLANNER,
                run_id="missing-live-compiler",
            )

    async def test_summary_does_not_copy_native_results(self) -> None:
        task = load_v01_tasks()[0]
        run = await BenchmarkRunner(default_registry()).run(
            task,
            run_id="content-safe-summary",
        )
        serialized = json.dumps(run.summary(), sort_keys=True)

        self.assertNotIn("Travel reimbursement policy", serialized)
        self.assertNotIn("result_payload", serialized)

    async def test_observation_captures_traces_before_environment_close(
        self,
    ) -> None:
        task = load_v01_tasks()[0]
        run = await BenchmarkRunner(default_registry()).run(
            task,
            run_id="captured-trace",
        )

        self.assertEqual(
            tuple(
                trace.trace_id
                for trace in run.observation.execution_traces
            ),
            run.observation.trace_ids,
        )
        run.observation.environment.trace_store = InMemoryTraceStore()
        report = diagnostic_report(run, show_trace=True)

        self.assertEqual(
            len(report["traces"]["executions"]),
            len(run.observation.trace_ids),
        )
        self.assertEqual(report["traces"]["missing_trace_ids"], [])

    async def test_diagnostic_report_is_structural_and_content_safe(
        self,
    ) -> None:
        task = load_v01_tasks()[0]
        run = await BenchmarkRunner(default_registry()).run(
            task,
            run_id="diagnostic-summary",
        )

        report = diagnostic_report(
            run,
            show_plan=True,
            show_trace=True,
            show_result=True,
        )
        serialized = json.dumps(report, sort_keys=True)

        operation = report["plans"][0]["operations"][0]
        self.assertEqual(operation["operation"], "search")
        self.assertEqual(report["result"]["result_kind"], "document_hits")
        self.assertEqual(report["result"]["native_type"], "tuple")
        self.assertEqual(
            report["result"]["native_shape"],
            {
                "python_type": "tuple",
                "item_count": 1,
                "item_types": ["DocumentHit"],
            },
        )
        self.assertNotIn("Travel reimbursement policy", serialized)
        self.assertNotIn("travel reimbursement policy", serialized)

    async def test_single_task_selection_preserves_canonical_run_identity(
        self,
    ) -> None:
        suite = await run_v01("surface.graph_traverse")

        self.assertEqual(len(suite.runs), 1)
        self.assertEqual(suite.runs[0].task_id, "surface.graph_traverse")
        self.assertEqual(suite.runs[0].run_id, "v01-003")


class BenchmarkDiagnosticCliTests(unittest.TestCase):
    def test_list_describes_tasks_without_run_output(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(["--list"])
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(payload["tasks"]), 14)
        self.assertEqual(
            payload["tasks"][0]["task_id"],
            "surface.document_search",
        )
        self.assertNotIn("runs", payload)

    def test_single_task_can_project_plan_trace_and_result(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "--task",
                    "surface.document_search",
                    "--show-plan",
                    "--show-trace",
                    "--show-result",
                ]
            )
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["tasks"], 1)
        self.assertEqual(payload["passed"], 1)
        self.assertEqual(len(payload["diagnostics"]), 1)
        diagnostic = payload["diagnostics"][0]
        self.assertIn("plans", diagnostic)
        self.assertIn("traces", diagnostic)
        self.assertIn("result", diagnostic)

    def test_diagnostic_view_requires_task_isolation(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["--show-plan"])

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
