"""Run the canonical DataMind-Bench v0.1 acceptance suite."""
from __future__ import annotations

import asyncio
import json

from .runner import BenchmarkRunner, BenchmarkSuiteResult
from .schema import RunnerMode
from .v0 import default_registry, load_v01_tasks


async def run_v01() -> BenchmarkSuiteResult:
    runner = BenchmarkRunner(default_registry())
    runs = []
    for index, task in enumerate(load_v01_tasks(), start=1):
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


def main() -> int:
    suite = asyncio.run(run_v01())
    print(
        json.dumps(
            suite.summary(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if suite.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
