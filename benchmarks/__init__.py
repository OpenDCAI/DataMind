"""Executable acceptance benchmark for the typed DataMind runtime."""

from .environment import BenchmarkEnvironment
from .faults import FaultInjectingSource, FaultRule
from .oracle import AssertionVerdict, OracleRegistry
from .registry import BenchmarkRegistry
from .runner import (
    BenchmarkRun,
    BenchmarkRunner,
    BenchmarkSuiteResult,
    RunObservation,
    WorkloadResult,
)
from .schema import (
    BENCHMARK_TASK_SCHEMA,
    BENCHMARK_TASK_VERSION,
    AssertionSpec,
    BenchmarkValidationError,
    ContextSpec,
    PlanConstraints,
    PrecedenceConstraint,
    ReplayExpectation,
    RunnerMode,
    TaskLayer,
    TaskSpec,
    load_task,
    load_tasks,
)

__all__ = [
    "AssertionSpec",
    "AssertionVerdict",
    "BENCHMARK_TASK_SCHEMA",
    "BENCHMARK_TASK_VERSION",
    "BenchmarkEnvironment",
    "BenchmarkRegistry",
    "BenchmarkRun",
    "BenchmarkRunner",
    "BenchmarkSuiteResult",
    "BenchmarkValidationError",
    "ContextSpec",
    "FaultInjectingSource",
    "FaultRule",
    "OracleRegistry",
    "PlanConstraints",
    "PrecedenceConstraint",
    "ReplayExpectation",
    "RunObservation",
    "RunnerMode",
    "TaskLayer",
    "TaskSpec",
    "WorkloadResult",
    "load_task",
    "load_tasks",
]
