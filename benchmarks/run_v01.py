"""Run the canonical DataMind-Bench v0.1 acceptance suite."""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Optional, Sequence, Tuple

from .diagnostics import diagnostic_report, task_catalog
from .runner import BenchmarkRunner, BenchmarkSuiteResult
from .schema import BenchmarkValidationError, RunnerMode, TaskSpec
from .v0 import default_registry, load_v01_tasks


def _selected_tasks(
    task_id: Optional[str],
) -> Tuple[Tuple[int, TaskSpec], ...]:
    indexed = tuple(enumerate(load_v01_tasks(), start=1))
    if task_id is None:
        return indexed
    selected = tuple(
        (index, task)
        for index, task in indexed
        if task.task_id == task_id
    )
    if not selected:
        raise BenchmarkValidationError(
            "unknown DataMind-Bench v0.1 task {!r}".format(task_id)
        )
    return selected


async def run_v01(
    task_id: Optional[str] = None,
) -> BenchmarkSuiteResult:
    runner = BenchmarkRunner(default_registry())
    runs = []
    for index, task in _selected_tasks(task_id):
        mode = (
            RunnerMode.ORACLE_PLAN
            if RunnerMode.ORACLE_PLAN in task.supported_modes
            else RunnerMode.SCRIPTED_COMPILER
        )
        runs.append(
            await runner.run(
                task,
                mode=mode,
                run_id="v01-{:03d}".format(index),
            )
        )
    return BenchmarkSuiteResult(tuple(runs))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run or inspect the canonical DataMind-Bench v0.1 suite."
        )
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_tasks",
        help="list task contracts without executing them",
    )
    parser.add_argument(
        "--task",
        metavar="TASK_ID",
        help="execute exactly one task by its stable task_id",
    )
    parser.add_argument(
        "--show-plan",
        action="store_true",
        help="show the actual DataPlan attempts captured by the run",
    )
    parser.add_argument(
        "--show-trace",
        action="store_true",
        help="show captured content-safe execution events",
    )
    parser.add_argument(
        "--show-result",
        action="store_true",
        help="show a content-safe ResultEnvelope summary",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    show_diagnostics = any(
        (args.show_plan, args.show_trace, args.show_result)
    )
    if args.list_tasks:
        if args.task is not None or show_diagnostics:
            parser.error("--list cannot be combined with execution options")
        print(
            json.dumps(
                task_catalog(load_v01_tasks()),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if show_diagnostics and args.task is None:
        parser.error("diagnostic views require --task TASK_ID")
    try:
        suite = asyncio.run(run_v01(args.task))
    except BenchmarkValidationError as error:
        parser.error(str(error))
    payload = suite.summary()
    if show_diagnostics:
        payload["diagnostics"] = [
            diagnostic_report(
                run,
                show_plan=args.show_plan,
                show_trace=args.show_trace,
                show_result=args.show_result,
            )
            for run in suite.runs
        ]
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if suite.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
