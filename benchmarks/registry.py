"""Explicit registries for trusted benchmark fixtures and programs."""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Mapping, Tuple

from .environment import BenchmarkEnvironment
from .schema import BenchmarkValidationError, TaskSpec

EnvironmentFactory = Callable[[], BenchmarkEnvironment]
Workload = Callable[..., Any]
ScriptFactory = Callable[
    [BenchmarkEnvironment, TaskSpec],
    Tuple[Mapping[str, Any], ...],
]


class BenchmarkRegistry:
    """No import-side-effect discovery and no executable code in task JSON."""

    def __init__(self) -> None:
        self._fixtures: Dict[str, EnvironmentFactory] = {}
        self._workloads: Dict[str, Workload] = {}
        self._scripts: Dict[str, ScriptFactory] = {}

    @staticmethod
    def _add(
        target: Dict[str, Any],
        name: str,
        value: Any,
        *,
        kind: str,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise BenchmarkValidationError(
                "{} name must be non-empty".format(kind)
            )
        if not callable(value):
            raise BenchmarkValidationError(
                "{} {!r} must be callable".format(kind, name)
            )
        if name in target:
            raise BenchmarkValidationError(
                "{} {!r} is already registered".format(kind, name)
            )
        target[name] = value

    def register_fixture(
        self,
        name: str,
        factory: EnvironmentFactory,
    ) -> None:
        self._add(self._fixtures, name, factory, kind="fixture")

    def register_workload(self, name: str, workload: Workload) -> None:
        self._add(self._workloads, name, workload, kind="workload")

    def register_script(
        self,
        name: str,
        factory: ScriptFactory,
    ) -> None:
        self._add(self._scripts, name, factory, kind="script")

    def environment(self, name: str) -> BenchmarkEnvironment:
        try:
            environment = self._fixtures[name]()
        except KeyError as exc:
            raise BenchmarkValidationError(
                "unknown fixture {!r}".format(name)
            ) from exc
        if not isinstance(environment, BenchmarkEnvironment):
            raise BenchmarkValidationError(
                "fixture {!r} did not return BenchmarkEnvironment".format(
                    name
                )
            )
        return environment

    def workload(self, name: str) -> Workload:
        try:
            return self._workloads[name]
        except KeyError as exc:
            raise BenchmarkValidationError(
                "unknown workload {!r}".format(name)
            ) from exc

    def script(
        self,
        name: str,
        environment: BenchmarkEnvironment,
        task: TaskSpec,
    ) -> Tuple[Mapping[str, Any], ...]:
        try:
            values = tuple(self._scripts[name](environment, task))
        except KeyError as exc:
            raise BenchmarkValidationError(
                "unknown script {!r}".format(name)
            ) from exc
        if not values or any(
            not isinstance(item, Mapping) for item in values
        ):
            raise BenchmarkValidationError(
                "script {!r} must return JSON objects".format(name)
            )
        return values

    def validate_tasks(self, tasks: Iterable[TaskSpec]) -> None:
        for task in tasks:
            if task.fixture_id not in self._fixtures:
                raise BenchmarkValidationError(
                    "task {!r} references unknown fixture {!r}".format(
                        task.task_id,
                        task.fixture_id,
                    )
                )
            if task.workload_id not in self._workloads:
                raise BenchmarkValidationError(
                    "task {!r} references unknown workload {!r}".format(
                        task.task_id,
                        task.workload_id,
                    )
                )
            if task.script_id is not None and task.script_id not in self._scripts:
                raise BenchmarkValidationError(
                    "task {!r} references unknown script {!r}".format(
                        task.task_id,
                        task.script_id,
                    )
                )
