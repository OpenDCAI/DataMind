"""Deterministic DataOp execution against injected source ports."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Optional, Tuple

from datamind.dataops import (
    ApplyMutation,
    DataPlan,
    Describe,
    Discover,
    OutputRef,
    ProposeMutation,
    Recall,
    ResultEnvelope,
    ResultKind,
    validate_plan,
)
from datamind.kernel import (
    Budget,
    BudgetExceeded,
    DeadlineExceeded,
    EffectLevel,
    EffectPolicyError,
    ExecutionContext,
    ExecutionError,
    ExecutionFailure,
    ExecutionFailureKind,
    KernelValidationError,
    PlanValidationError,
    ReplayError,
    ScopePolicyError,
    SnapshotUnavailableError,
    SourceExecutionError,
    TraceError,
    Usage,
    require_effect_allowed,
    utc_now,
)
from datamind.ports import (
    ReplayArtifactStore,
    SnapshotSource,
    SourceCatalogPort,
    SourceResult,
    TraceStore,
)

from .fingerprint import fingerprint
from .recording import ExecutionRecorder
from .bindings import resolve_bound_operation
from .runtime import (
    execute_dataflow_operation,
    is_dataflow_operation,
    select_path,
)


class _ExecutionInterrupted(Exception):
    def __init__(
        self,
        cause: Exception,
        *,
        completed_op_ids: Tuple[str, ...] = (),
        usage: Usage = Usage(),
        failed_op_id: Optional[str] = None,
    ) -> None:
        self.cause = cause
        self.completed_op_ids = completed_op_ids
        self.usage = usage
        self.failed_op_id = failed_op_id
        super().__init__(str(cause))


@dataclass(frozen=True)
class ExecutionAttemptOutcome:
    """Internal orchestration result preserving failure usage and cause."""

    result: Optional[ResultEnvelope[Any]] = None
    failure: Optional[ExecutionFailure] = None
    error: Optional[Exception] = None

    def __post_init__(self) -> None:
        succeeded = self.result is not None
        failed = self.failure is not None and self.error is not None
        if succeeded == failed:
            raise ExecutionError(
                "execution attempt must contain one result or one failure"
            )

    @property
    def successful(self) -> bool:
        return self.result is not None

    @property
    def usage(self) -> Usage:
        if self.result is not None:
            return self.result.usage
        assert self.failure is not None
        return self.failure.usage


class Executor:
    """Validate, schedule, and execute plans without model calls."""

    def __init__(
        self,
        catalog: SourceCatalogPort,
        *,
        trace_store: Optional[TraceStore] = None,
        artifact_store: Optional[ReplayArtifactStore] = None,
        max_parallelism: int = 4,
    ) -> None:
        if (
            isinstance(max_parallelism, bool)
            or not isinstance(max_parallelism, int)
            or max_parallelism <= 0
        ):
            raise ExecutionError(
                "max_parallelism must be a positive integer"
            )
        self._catalog = catalog
        self._max_parallelism = max_parallelism
        self._recorder = ExecutionRecorder(
            trace_store=trace_store,
            artifact_store=artifact_store,
        )

    async def execute(
        self,
        operation_or_plan: Any,
        *,
        context: ExecutionContext,
    ) -> ResultEnvelope[Any]:
        plan = (
            operation_or_plan
            if isinstance(operation_or_plan, DataPlan)
            else self._single_operation_plan(operation_or_plan)
        )
        outcome = await self.execute_attempt(plan, context=context)
        if outcome.result is not None:
            return outcome.result
        assert outcome.error is not None
        raise outcome.error

    async def execute_attempt(
        self,
        plan: DataPlan,
        *,
        context: ExecutionContext,
    ) -> ExecutionAttemptOutcome:
        """Execute one plan while retaining sanitized failure observations."""

        if not isinstance(plan, DataPlan):
            raise ExecutionError(
                "execute_attempt() expects a DataPlan"
            )
        trace_started = False
        try:
            trace_started = await self._recorder.start_plan(
                plan,
                context=context,
            )
            result = await self._execute_plan(plan, context=context)
            await self._recorder.complete_plan(
                context.trace_id,
                plan,
                result,
            )
            return ExecutionAttemptOutcome(result=result)
        except _ExecutionInterrupted as interrupted:
            error = interrupted.cause
            if trace_started:
                await self._recorder.fail_plan(context.trace_id, error)
            return ExecutionAttemptOutcome(
                failure=self._failure_from(
                    plan,
                    error,
                    completed_op_ids=interrupted.completed_op_ids,
                    usage=interrupted.usage,
                    failed_op_id=interrupted.failed_op_id,
                ),
                error=error,
            )
        except Exception as error:
            if trace_started:
                await self._recorder.fail_plan(context.trace_id, error)
            return ExecutionAttemptOutcome(
                failure=self._failure_from(plan, error),
                error=error,
            )

    async def replay(self, trace_id: str) -> ResultEnvelope[Any]:
        """Replay a completed trace without consulting live source adapters."""

        trace_store, artifact_store = self._recorder.replay_stores
        if trace_store is None or artifact_store is None:
            raise ReplayError(
                "replay requires both TraceStore and ReplayArtifactStore"
            )
        from .replay import ReplayEngine

        replay_engine = ReplayEngine(
            trace_store=trace_store,
            artifact_store=artifact_store,
        )
        return await replay_engine.replay(trace_id)

    def _single_operation_plan(self, operation: Any) -> DataPlan:
        try:
            op_id = operation.op_id
            effect_level = operation.effect.level
        except AttributeError as exc:
            raise ExecutionError(
                "execute() expects a DataOp or DataPlan"
            ) from exc
        return DataPlan(
            operations=(operation,),
            output=OutputRef(op_id),
            max_effect=effect_level,
            budget=Budget(),
            description="direct operation",
        )

    async def _execute_plan(
        self,
        plan: DataPlan,
        *,
        context: ExecutionContext,
    ) -> ResultEnvelope[Any]:
        results: Dict[str, ResultEnvelope[Any]] = {}
        total_usage = Usage()
        try:
            report = validate_plan(
                plan,
                sources=self._catalog.descriptors(),
            )
            report.require_valid()
            await self._preflight(plan, context=context)
            await self._recorder.plan_validated(
                context.trace_id,
                topological_order=report.topological_order,
                static_actions=len(plan.operations),
                max_parallelism=self._max_parallelism,
            )

            operations = {
                operation.op_id: operation
                for operation in plan.operations
            }
            remaining = list(report.topological_order)
            while remaining:
                self._require_before_deadline(context)
                batch = self._ready_batch(
                    remaining,
                    operations=operations,
                    results=results,
                )
                for op_id in batch:
                    await self._recorder.start_operation(
                        context.trace_id,
                        operations[op_id],
                        context=context,
                    )
                outcomes = await asyncio.gather(
                    *(
                        self._execute_one(
                            operations[op_id],
                            prior_results=results,
                            context=context,
                        )
                        for op_id in batch
                    ),
                    return_exceptions=True,
                )
                failures = []
                for op_id, outcome in zip(batch, outcomes):
                    if isinstance(outcome, Exception):
                        total_usage = total_usage + Usage(actions=1)
                        await self._recorder.fail_operation(
                            context.trace_id,
                            op_id,
                            outcome,
                        )
                        failures.append((op_id, outcome))
                        continue
                    result = outcome
                    total_usage = total_usage + result.usage
                    await self._recorder.complete_operation(
                        context.trace_id,
                        result,
                    )
                    results[op_id] = result

                failed_op_id = failures[0][0] if failures else None
                try:
                    plan.budget.require(total_usage)
                    context.budget.require(total_usage)
                except Exception as budget_error:
                    raise _ExecutionInterrupted(
                        budget_error,
                        completed_op_ids=tuple(results),
                        usage=total_usage,
                        failed_op_id=failed_op_id,
                    ) from budget_error
                if failures:
                    raise _ExecutionInterrupted(
                        failures[0][1],
                        completed_op_ids=tuple(results),
                        usage=total_usage,
                        failed_op_id=failed_op_id,
                    ) from failures[0][1]
                completed = set(batch)
                remaining = [
                    op_id for op_id in remaining if op_id not in completed
                ]

            final = results[plan.output.op_id]
            if plan.output.path:
                final = replace(
                    final,
                    value=select_path(final.value, plan.output.path),
                )
            return replace(final, usage=total_usage)
        except _ExecutionInterrupted:
            raise
        except Exception as error:
            raise _ExecutionInterrupted(
                error,
                completed_op_ids=tuple(results),
                usage=total_usage,
            ) from error

    def _ready_batch(
        self,
        remaining: list,
        *,
        operations: Mapping[str, Any],
        results: Mapping[str, ResultEnvelope[Any]],
    ) -> Tuple[str, ...]:
        ready = tuple(
            op_id
            for op_id in remaining
            if all(
                ref.op_id in results
                for ref in operations[op_id].inputs
            )
        )
        if not ready:
            raise ExecutionError(
                "validated plan has no schedulable operation"
            )
        first = operations[ready[0]]
        if first.effect.level > EffectLevel.READ:
            return (ready[0],)
        batch = []
        for op_id in ready:
            operation = operations[op_id]
            if operation.effect.level > EffectLevel.READ:
                break
            batch.append(op_id)
            if len(batch) >= self._max_parallelism:
                break
        return tuple(batch)

    async def _preflight(
        self,
        plan: DataPlan,
        *,
        context: ExecutionContext,
    ) -> None:
        self._require_before_deadline(context)
        context.budget.require(Usage(actions=len(plan.operations)))
        for operation in plan.operations:
            require_effect_allowed(
                operation.effect,
                max_level=context.max_effect,
                approvals=context.approvals,
                allowed_resources=context.allowed_resources,
            )
            if isinstance(operation, Recall):
                context.require_readable_scopes(operation.scopes)
            elif isinstance(operation, ProposeMutation):
                context.require_readable_scopes(operation.scopes)
                context.require_writable_scopes(operation.scopes)
                context.bind_memory_origin(
                    scope=operation.draft.scope,
                    approval_key=operation.draft.approval_key,
                )
            elif isinstance(operation, ApplyMutation):
                context.require_writable_scopes(operation.scopes)
        checked_sources = set()
        for operation in plan.operations:
            if (
                operation.source is None
                or isinstance(operation, Describe)
                or operation.source.source_id in checked_sources
            ):
                continue
            checked_sources.add(operation.source.source_id)
            pinned = context.snapshots.get(operation.source)
            if pinned is None:
                continue
            adapter = self._catalog.get(operation.source)
            if not isinstance(adapter, SnapshotSource):
                raise SnapshotUnavailableError(
                    "source {!r} does not support pinned execution".format(
                        operation.source.source_id
                    )
                )
            if not await adapter.has_snapshot(pinned):
                raise SnapshotUnavailableError(
                    "source {!r} cannot serve pinned snapshot {!r}".format(
                        operation.source.source_id,
                        pinned.version,
                    )
                )

    async def _execute_one(
        self,
        operation: Any,
        *,
        prior_results: Mapping[str, ResultEnvelope[Any]],
        context: ExecutionContext,
    ) -> ResultEnvelope[Any]:
        if isinstance(operation, Discover):
            descriptors = self._catalog.discover(operation.kinds)
            if context.allowed_resources:
                descriptors = tuple(
                    item
                    for item in descriptors
                    if item.ref.source_id in context.allowed_resources
                )
            source_result = SourceResult(
                value=descriptors,
                result_kind=ResultKind.SOURCE_LIST,
            )
        elif isinstance(operation, Describe):
            source_result = SourceResult(
                value=self._catalog.describe(operation.source),
                result_kind=ResultKind.SOURCE_DESCRIPTOR,
            )
        elif is_dataflow_operation(operation):
            source_result = execute_dataflow_operation(
                operation,
                prior_results=prior_results,
            )
        else:
            source_result = await self._execute_source(
                resolve_bound_operation(
                    operation,
                    prior_results=prior_results,
                ),
                context=context,
            )

        if not isinstance(source_result, SourceResult):
            raise ExecutionError(
                "source {!r} returned {}, expected SourceResult".format(
                    (
                        operation.source.source_id
                        if operation.source is not None
                        else "core"
                    ),
                    type(source_result).__name__,
                )
            )
        if source_result.result_kind is not operation.output_kind:
            raise ExecutionError(
                "operation {!r} declared {} but source returned {}".format(
                    operation.operation,
                    operation.output_kind.value,
                    source_result.result_kind.value,
                )
            )
        return ResultEnvelope(
            op_id=operation.op_id,
            value=source_result.value,
            result_kind=source_result.result_kind,
            trace_id=context.trace_id,
            evidence=source_result.evidence,
            bindings=source_result.bindings,
            provenance=source_result.provenance,
            snapshots=source_result.snapshots,
            usage=source_result.usage + Usage(actions=1),
            warnings=source_result.warnings,
            status=source_result.status,
        )

    async def _execute_source(
        self,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        if operation.source is None:
            raise ExecutionError(
                "operation {!r} has no Core executor or source".format(
                    operation.operation
                )
            )
        adapter = self._catalog.get(operation.source)
        try:
            result = await adapter.execute(operation, context=context)
        except ExecutionError:
            raise
        except Exception as exc:
            raise SourceExecutionError(
                "source {!r} failed operation {!r}: {}".format(
                    operation.source.source_id,
                    operation.operation,
                    exc,
                )
            ) from exc
        pinned = context.snapshots.get(operation.source)
        if pinned is not None and not any(
            item.same_version_as(pinned) for item in result.snapshots
        ):
            raise SnapshotUnavailableError(
                "source {!r} did not execute against pinned snapshot "
                "{!r}".format(
                    operation.source.source_id,
                    pinned.version,
                )
            )
        return result

    @staticmethod
    def _require_before_deadline(context: ExecutionContext) -> None:
        if context.deadline is not None and utc_now() >= context.deadline:
            raise DeadlineExceeded("execution deadline has expired")

    @staticmethod
    def _failure_kind(error: Exception) -> ExecutionFailureKind:
        if isinstance(error, SourceExecutionError):
            return ExecutionFailureKind.SOURCE
        if isinstance(error, SnapshotUnavailableError):
            return ExecutionFailureKind.SNAPSHOT
        if isinstance(error, BudgetExceeded):
            return ExecutionFailureKind.BUDGET
        if isinstance(error, (EffectPolicyError, ScopePolicyError)):
            return ExecutionFailureKind.POLICY
        if isinstance(error, (PlanValidationError, KernelValidationError)):
            return ExecutionFailureKind.PLAN
        if isinstance(error, DeadlineExceeded):
            return ExecutionFailureKind.DEADLINE
        if isinstance(error, TraceError):
            return ExecutionFailureKind.INFRASTRUCTURE
        return ExecutionFailureKind.EXECUTION

    @classmethod
    def _failure_from(
        cls,
        plan: DataPlan,
        error: Exception,
        *,
        completed_op_ids: Tuple[str, ...] = (),
        usage: Usage = Usage(),
        failed_op_id: Optional[str] = None,
    ) -> ExecutionFailure:
        operation = None
        source_id = None
        if failed_op_id is not None:
            try:
                failed_operation = plan.operation(failed_op_id)
            except KeyError:
                failed_operation = None
            if failed_operation is not None:
                operation = failed_operation.operation
                if failed_operation.source is not None:
                    source_id = failed_operation.source.source_id
        kind = cls._failure_kind(error)
        return ExecutionFailure(
            kind=kind,
            error_type="{}.{}".format(
                type(error).__module__,
                type(error).__qualname__,
            ),
            error_fingerprint=fingerprint(str(error)),
            usage=usage,
            failed_op_id=failed_op_id,
            operation=operation,
            source_id=source_id,
            completed_op_ids=completed_op_ids,
            recoverable=kind is ExecutionFailureKind.SOURCE,
        )


__all__ = ["ExecutionAttemptOutcome", "Executor"]
