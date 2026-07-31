"""Executable acceptance tests for the repository-level benchmark."""
from __future__ import annotations

import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path

from benchmarks import (
    BenchmarkRunner,
    BenchmarkValidationError,
    PlanConstraints,
    RunnerMode,
    TaskSpec,
)
from benchmarks.run_v01 import run_v01
from benchmarks.v0 import default_registry, load_v01_tasks


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


if __name__ == "__main__":
    unittest.main()
