"""Execution-based runner producing append-only Outcome records."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Optional, Tuple

from datamind.adapters import ScriptedModel
from datamind.dataops import DataPlan, ResultEnvelope
from datamind.engine import Engine, Resolution
from datamind.intelligence import DataPlanCompiler
from datamind.kernel import (
    EffectLevel,
    ExecutionTrace,
    EvaluatorKind,
    OutcomeAssertion,
    OutcomeRecord,
    OutcomeTarget,
    OutcomeTargetKind,
    ReplayError,
    ResolutionTrace,
    TraceNotFoundError,
    Usage,
)
from datamind.ports import (
    PlanCompilerPort,
    StructuredModelResponse,
)

from .environment import BenchmarkEnvironment
from .oracle import AssertionVerdict, OracleRegistry
from .registry import BenchmarkRegistry
from .schema import (
    BenchmarkValidationError,
    PlanConstraints,
    ReplayExpectation,
    RunnerMode,
    TaskSpec,
)


@dataclass(frozen=True)
class WorkloadResult:
    """Result of one trusted workload, including multi-step state tasks."""

    plans: Tuple[DataPlan, ...] = ()
    result: Optional[ResultEnvelope[Any]] = None
    error: Optional[Exception] = None
    trace_ids: Tuple[str, ...] = ()
    replay_trace_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "plans", tuple(self.plans))
        object.__setattr__(self, "trace_ids", tuple(self.trace_ids))
        if any(not isinstance(item, DataPlan) for item in self.plans):
            raise BenchmarkValidationError(
                "workload plans must contain DataPlan values"
            )
        if self.result is not None and not isinstance(
            self.result, ResultEnvelope
        ):
            raise BenchmarkValidationError(
                "workload result must be ResultEnvelope"
            )
        if self.error is not None and not isinstance(self.error, Exception):
            raise BenchmarkValidationError(
                "workload error must be an Exception"
            )
        if self.result is not None and self.error is not None:
            raise BenchmarkValidationError(
                "workload cannot contain both result and error"
            )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.trace_ids
        ):
            raise BenchmarkValidationError(
                "workload trace_ids must be non-empty"
            )


@dataclass(frozen=True)
class RunObservation:
    """Immutable facts captured from one benchmark execution."""

    task: TaskSpec
    mode: RunnerMode
    environment: BenchmarkEnvironment
    plans: Tuple[DataPlan, ...]
    result: Optional[ResultEnvelope[Any]]
    resolution: Optional[Resolution]
    error: Optional[Exception]
    trace_ids: Tuple[str, ...]
    target_trace_id: Optional[str]
    execution_traces: Tuple[ExecutionTrace, ...] = ()
    resolution_trace: Optional[ResolutionTrace] = None
    replayed: Optional[ResultEnvelope[Any]] = None
    replay_error: Optional[Exception] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "plans", tuple(self.plans))
        object.__setattr__(self, "trace_ids", tuple(self.trace_ids))
        object.__setattr__(
            self,
            "execution_traces",
            tuple(self.execution_traces),
        )
        if any(
            not isinstance(item, ExecutionTrace)
            for item in self.execution_traces
        ):
            raise BenchmarkValidationError(
                "observation execution_traces must contain ExecutionTrace "
                "values"
            )
        if self.resolution_trace is not None and not isinstance(
            self.resolution_trace,
            ResolutionTrace,
        ):
            raise BenchmarkValidationError(
                "observation resolution_trace must be a ResolutionTrace"
            )

    def trace(self, trace_id: str) -> Optional[ExecutionTrace]:
        """Return a captured trace without consulting the live environment."""

        return next(
            (
                trace
                for trace in self.execution_traces
                if trace.trace_id == trace_id
            ),
            None,
        )


@dataclass(frozen=True)
class BenchmarkRun:
    run_id: str
    task_id: str
    mode: RunnerMode
    outcome: OutcomeRecord
    observation: RunObservation

    @property
    def succeeded(self) -> bool:
        return self.outcome.succeeded

    def summary(self) -> dict:
        """Return content-safe, JSON-serializable run metadata."""

        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "mode": self.mode.value,
            "outcome_id": self.outcome.outcome_id,
            "succeeded": self.succeeded,
            "assertions": [
                {
                    "name": item.name,
                    "passed": item.passed,
                    "score": (
                        str(item.score)
                        if item.score is not None
                        else None
                    ),
                }
                for item in self.outcome.assertions
            ],
        }


@dataclass(frozen=True)
class BenchmarkSuiteResult:
    runs: Tuple[BenchmarkRun, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "runs", tuple(self.runs))

    @property
    def passed(self) -> int:
        return sum(item.succeeded for item in self.runs)

    @property
    def failed(self) -> int:
        return len(self.runs) - self.passed

    @property
    def success_rate(self) -> Decimal:
        if not self.runs:
            return Decimal("0")
        return Decimal(self.passed) / Decimal(len(self.runs))

    def summary(self) -> dict:
        return {
            "tasks": len(self.runs),
            "passed": self.passed,
            "failed": self.failed,
            "success_rate": str(self.success_rate),
            "runs": [item.summary() for item in self.runs],
        }


class BenchmarkRunner:
    """Run deterministic workloads without adding logic to DataMind Core."""

    def __init__(
        self,
        registry: BenchmarkRegistry,
        *,
        oracles: Optional[OracleRegistry] = None,
        evaluator_version: str = "0.1",
    ) -> None:
        if not isinstance(registry, BenchmarkRegistry):
            raise BenchmarkValidationError(
                "runner registry must be BenchmarkRegistry"
            )
        if not isinstance(evaluator_version, str) or not evaluator_version.strip():
            raise BenchmarkValidationError(
                "evaluator_version must be non-empty"
            )
        self._registry = registry
        self._oracles = oracles or OracleRegistry()
        self._evaluator_version = evaluator_version

    async def run(
        self,
        task: TaskSpec,
        *,
        mode: RunnerMode = RunnerMode.ORACLE_PLAN,
        run_id: Optional[str] = None,
        compiler: Optional[PlanCompilerPort] = None,
    ) -> BenchmarkRun:
        if mode not in task.supported_modes:
            raise BenchmarkValidationError(
                "task {!r} does not support mode {!r}".format(
                    task.task_id,
                    mode.value,
                )
            )
        self._registry.validate_tasks((task,))
        self._oracles.validate(task.assertions)
        run_id = run_id or task.task_id
        if not isinstance(run_id, str) or not run_id.strip():
            raise BenchmarkValidationError("run_id must be non-empty")
        environment = self._registry.environment(task.fixture_id)
        engine = environment.engine()
        resolution = None
        resolution_id = None
        workload = None
        try:
            if mode is RunnerMode.ORACLE_PLAN:
                workload = await self._run_workload(
                    environment,
                    engine,
                    task,
                    run_id,
                )
            else:
                if mode is RunnerMode.SCRIPTED_COMPILER:
                    compiler = self._scripted_compiler(
                        environment,
                        task,
                    )
                elif compiler is None:
                    raise BenchmarkValidationError(
                        "live planner mode requires a compiler"
                    )
                engine = environment.engine(compiler=compiler)
                context = environment.context(task, run_id)
                resolution_id = context.trace_id
                try:
                    resolution = await engine.resolve(
                        task.request or "",
                        context=context,
                    )
                    workload = WorkloadResult(
                        plans=tuple(
                            item.plan
                            for item in resolution.plan_attempts
                        ),
                        result=resolution.result,
                        trace_ids=tuple(
                            item.trace_id
                            for item in resolution.plan_attempts
                        ),
                        replay_trace_id=(
                            resolution.final_attempt.trace_id
                        ),
                    )
                    resolution_id = resolution.resolution_id
                except Exception as error:
                    workload = WorkloadResult(
                        error=error,
                        replay_trace_id=context.trace_id,
                    )

            replayed = None
            replay_error = None
            if task.replay is not ReplayExpectation.SKIP:
                replay_id = workload.replay_trace_id
                if replay_id is None:
                    replay_error = ReplayError(
                        "workload did not expose a replay trace"
                    )
                else:
                    try:
                        replayed = await engine.replay(replay_id)
                    except Exception as error:
                        replay_error = error

            execution_traces = await self._capture_execution_traces(
                environment,
                workload.trace_ids,
            )
            resolution_trace = await self._capture_resolution_trace(
                environment,
                resolution_id,
            )

            observation = RunObservation(
                task=task,
                mode=mode,
                environment=environment,
                plans=workload.plans,
                result=workload.result,
                resolution=resolution,
                error=workload.error,
                trace_ids=workload.trace_ids,
                target_trace_id=workload.replay_trace_id,
                execution_traces=execution_traces,
                resolution_trace=resolution_trace,
                replayed=replayed,
                replay_error=replay_error,
            )
            assertions = list(
                self._contract_assertions(observation)
            )
            for spec in task.assertions:
                verdict = await self._oracles.evaluate(
                    observation,
                    spec,
                )
                assertions.append(
                    OutcomeAssertion(
                        name=spec.name,
                        passed=verdict.passed,
                        score=verdict.score,
                    )
                )
            target = (
                OutcomeTarget(
                    OutcomeTargetKind.RESOLUTION,
                    resolution.resolution_id,
                )
                if resolution is not None
                else OutcomeTarget(
                    OutcomeTargetKind.TRACE,
                    workload.replay_trace_id
                    or "benchmark-run-{}".format(run_id),
                )
            )
            outcome = OutcomeRecord(
                target=target,
                task_id=task.task_id,
                evaluator_kind=EvaluatorKind.PROGRAM,
                evaluator_name="datamind-bench",
                evaluator_version=self._evaluator_version,
                assertions=tuple(assertions),
                succeeded=all(item.passed for item in assertions),
                idempotency_key="{}:{}".format(run_id, task.task_id),
            )
            recorded = await environment.engine().record_outcome(outcome)
            return BenchmarkRun(
                run_id=run_id,
                task_id=task.task_id,
                mode=mode,
                outcome=recorded,
                observation=observation,
            )
        finally:
            environment.close()

    @staticmethod
    async def _capture_execution_traces(
        environment: BenchmarkEnvironment,
        trace_ids: Tuple[str, ...],
    ) -> Tuple[ExecutionTrace, ...]:
        """Snapshot content-safe execution traces before environment close."""

        captured = []
        seen = set()
        for trace_id in trace_ids:
            if trace_id in seen:
                continue
            seen.add(trace_id)
            try:
                captured.append(
                    await environment.trace_store.get(trace_id)
                )
            except TraceNotFoundError:
                continue
        return tuple(captured)

    @staticmethod
    async def _capture_resolution_trace(
        environment: BenchmarkEnvironment,
        resolution_id: Optional[str],
    ) -> Optional[ResolutionTrace]:
        """Snapshot the parent resolution trace when planning was used."""

        if resolution_id is None:
            return None
        try:
            return await environment.trace_store.get_resolution(
                resolution_id
            )
        except TraceNotFoundError:
            return None

    async def run_suite(
        self,
        tasks: Iterable[TaskSpec],
        *,
        mode: RunnerMode = RunnerMode.ORACLE_PLAN,
        compiler: Optional[PlanCompilerPort] = None,
        run_prefix: str = "suite",
    ) -> BenchmarkSuiteResult:
        values = tuple(tasks)
        self._registry.validate_tasks(values)
        runs = []
        for index, task in enumerate(values, start=1):
            runs.append(
                await self.run(
                    task,
                    mode=mode,
                    compiler=compiler,
                    run_id="{}-{:03d}".format(run_prefix, index),
                )
            )
        return BenchmarkSuiteResult(tuple(runs))

    async def _run_workload(
        self,
        environment: BenchmarkEnvironment,
        engine: Engine,
        task: TaskSpec,
        run_id: str,
    ) -> WorkloadResult:
        workload = self._registry.workload(task.workload_id)
        try:
            result = await workload(
                environment,
                engine,
                task,
                run_id,
            )
        except Exception as error:
            return WorkloadResult(error=error)
        if not isinstance(result, WorkloadResult):
            raise BenchmarkValidationError(
                "workload {!r} returned {}, expected WorkloadResult".format(
                    task.workload_id,
                    type(result).__name__,
                )
            )
        return result

    def _scripted_compiler(
        self,
        environment: BenchmarkEnvironment,
        task: TaskSpec,
    ) -> DataPlanCompiler:
        assert task.script_id is not None
        outputs = self._registry.script(
            task.script_id,
            environment,
            task,
        )
        responses = tuple(
            StructuredModelResponse(
                output=output,
                model="datamind-bench-scripted",
                response_id="{}-{}".format(task.task_id, index),
                usage=Usage(tokens=1),
            )
            for index, output in enumerate(outputs, start=1)
        )
        return DataPlanCompiler(ScriptedModel(responses))

    def _contract_assertions(
        self,
        observation: RunObservation,
    ) -> Tuple[OutcomeAssertion, ...]:
        task = observation.task
        execution_passed = self._execution_matches(
            observation.error,
            task.expected_error,
        )
        sources, operations, precedence, effects, actions = (
            _evaluate_plan_constraints(
                observation.plans,
                task.plan_constraints,
            )
        )
        if task.replay is ReplayExpectation.SKIP:
            replay_passed = True
        elif task.replay is ReplayExpectation.EQUIVALENT:
            replay_passed = (
                observation.result is not None
                and observation.replayed == observation.result
                and observation.replay_error is None
            )
        else:
            replay_passed = (
                observation.replayed is None
                and isinstance(observation.replay_error, ReplayError)
            )
        return tuple(
            OutcomeAssertion(
                name=name,
                passed=passed,
                score=1 if passed else 0,
            )
            for name, passed in (
                ("contract.execution", execution_passed),
                ("contract.sources", sources),
                ("contract.operations", operations),
                ("contract.precedence", precedence),
                ("contract.effects", effects),
                ("contract.actions", actions),
                ("contract.replay", replay_passed),
            )
        )

    @staticmethod
    def _execution_matches(
        error: Optional[Exception],
        expected_error: Optional[str],
    ) -> bool:
        if expected_error is None:
            return error is None
        if error is None:
            return False
        return any(
            item.__name__ == expected_error
            for item in type(error).__mro__
        )


def _evaluate_plan_constraints(
    plans: Tuple[DataPlan, ...],
    constraints: PlanConstraints,
) -> Tuple[bool, bool, bool, bool, bool]:
    operations = tuple(
        operation
        for plan in plans
        for operation in plan.operations
    )
    source_ids = {
        operation.source.source_id
        for operation in operations
        if operation.source is not None
    }
    source_kinds = {
        operation.source.kind
        for operation in operations
        if operation.source is not None
    }
    sources_passed = (
        set(constraints.required_source_ids) <= source_ids
        and set(constraints.required_source_kinds) <= source_kinds
    )
    operation_names = {item.operation for item in operations}
    operations_passed = set(
        constraints.required_operations
    ) <= operation_names
    if constraints.allowed_operations:
        operations_passed = operations_passed and (
            operation_names <= set(constraints.allowed_operations)
        )
    precedence_passed = all(
        _has_precedence(plans, item.before, item.after)
        for item in constraints.precedence
    )
    effects_passed = all(
        plan.max_effect <= constraints.max_effect
        and all(
            operation.effect.level <= constraints.max_effect
            for operation in plan.operations
        )
        for plan in plans
    )
    actions_passed = (
        constraints.max_actions is None
        or len(operations) <= constraints.max_actions
    )
    if not plans:
        sources_passed = (
            sources_passed
            and not constraints.required_source_ids
            and not constraints.required_source_kinds
        )
        operations_passed = (
            operations_passed
            and not constraints.required_operations
        )
        precedence_passed = (
            precedence_passed and not constraints.precedence
        )
    return (
        sources_passed,
        operations_passed,
        precedence_passed,
        effects_passed,
        actions_passed,
    )


def _has_precedence(
    plans: Tuple[DataPlan, ...],
    before: str,
    after: str,
) -> bool:
    for plan in plans:
        operations = {
            item.op_id: item for item in plan.operations
        }
        before_ids = {
            item.op_id
            for item in plan.operations
            if item.operation == before
        }
        after_ids = {
            item.op_id
            for item in plan.operations
            if item.operation == after
        }
        for after_id in after_ids:
            pending = [
                ref.op_id for ref in operations[after_id].inputs
            ]
            visited = set()
            while pending:
                current = pending.pop()
                if current in before_ids:
                    return True
                if current in visited or current not in operations:
                    continue
                visited.add(current)
                pending.extend(
                    ref.op_id for ref in operations[current].inputs
                )
    return False
