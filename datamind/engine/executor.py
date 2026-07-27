"""Deterministic DataOp execution against injected source ports."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Mapping, Optional

from datamind.dataops import (
    Compose,
    DataPlan,
    Describe,
    Discover,
    OutputRef,
    ResultEnvelope,
    ResultKind,
    validate_plan,
)
from datamind.kernel import (
    Budget,
    ExecutionContext,
    ExecutionError,
    ReplayError,
    SourceExecutionError,
    Usage,
    require_effect_allowed,
    utc_now,
)
from datamind.ports import (
    ReplayArtifactStore,
    SourceCatalogPort,
    SourceResult,
    TraceStore,
)

from .recording import ExecutionRecorder
from .runtime import compose_results, select_path


class Executor:
    """Validate, schedule, and execute plans without model calls."""

    def __init__(
        self,
        catalog: SourceCatalogPort,
        *,
        trace_store: Optional[TraceStore] = None,
        artifact_store: Optional[ReplayArtifactStore] = None,
    ) -> None:
        self._catalog = catalog
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
            return result
        except Exception as exc:
            if trace_started:
                await self._recorder.fail_plan(context.trace_id, exc)
            raise

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
        report = validate_plan(
            plan,
            sources=self._catalog.descriptors(),
        )
        report.require_valid()
        self._preflight(plan, context=context)
        await self._recorder.plan_validated(
            context.trace_id,
            topological_order=report.topological_order,
            static_actions=len(plan.operations),
        )

        operations = {operation.op_id: operation for operation in plan.operations}
        results: Dict[str, ResultEnvelope[Any]] = {}
        total_usage = Usage()
        for op_id in report.topological_order:
            self._require_before_deadline(context)
            operation = operations[op_id]
            await self._recorder.start_operation(
                context.trace_id,
                operation,
            )
            try:
                result = await self._execute_one(
                    operation,
                    prior_results=results,
                    context=context,
                )
                total_usage = total_usage + result.usage
                plan.budget.require(total_usage)
                context.budget.require(total_usage)
                await self._recorder.complete_operation(
                    context.trace_id,
                    result,
                )
                results[op_id] = result
            except Exception as exc:
                await self._recorder.fail_operation(
                    context.trace_id,
                    op_id,
                    exc,
                )
                raise

        final = results[plan.output.op_id]
        if plan.output.path:
            final = replace(
                final,
                value=select_path(final.value, plan.output.path),
            )
        return replace(final, usage=total_usage)

    def _preflight(
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

    async def _execute_one(
        self,
        operation: Any,
        *,
        prior_results: Mapping[str, ResultEnvelope[Any]],
        context: ExecutionContext,
    ) -> ResultEnvelope[Any]:
        if isinstance(operation, Discover):
            source_result = SourceResult(
                value=self._catalog.discover(operation.kinds),
                result_kind=ResultKind.SOURCE_LIST,
            )
        elif isinstance(operation, Describe):
            source_result = SourceResult(
                value=self._catalog.describe(operation.source),
                result_kind=ResultKind.SOURCE_DESCRIPTOR,
            )
        elif isinstance(operation, Compose):
            source_result = compose_results(
                operation,
                prior_results=prior_results,
            )
        else:
            source_result = await self._execute_source(
                operation,
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
            return await adapter.execute(operation, context=context)
        except SourceExecutionError:
            raise
        except Exception as exc:
            raise SourceExecutionError(
                "source {!r} failed operation {!r}: {}".format(
                    operation.source.source_id,
                    operation.operation,
                    exc,
                )
            ) from exc

    @staticmethod
    def _require_before_deadline(context: ExecutionContext) -> None:
        if context.deadline is not None and utc_now() >= context.deadline:
            raise ExecutionError("execution deadline has expired")
