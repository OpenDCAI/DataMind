"""Deterministic DataOp/DataPlan execution against injected source ports."""
from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Any, Dict, Mapping, Sequence, Tuple

from datamind.dataops import (
    Compose,
    ContextItem,
    ContextPack,
    DataPlan,
    Describe,
    Discover,
    OutputRef,
    ResultEnvelope,
    ResultKind,
    ResultStatus,
    validate_plan,
)
from datamind.kernel import (
    Budget,
    ExecutionContext,
    ExecutionError,
    Provenance,
    SnapshotRef,
    SourceExecutionError,
    Usage,
    require_effect_allowed,
    utc_now,
)
from datamind.ports import SourceCatalogPort, SourceResult


class Executor:
    """Execute validated plans without model calls or implicit registration."""

    def __init__(self, catalog: SourceCatalogPort) -> None:
        self._catalog = catalog

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
        return await self._execute_plan(plan, context=context)

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

        operations = {operation.op_id: operation for operation in plan.operations}
        results: Dict[str, ResultEnvelope[Any]] = {}
        total_usage = Usage()
        for op_id in report.topological_order:
            self._require_before_deadline(context)
            operation = operations[op_id]
            result = await self._execute_one(
                operation,
                prior_results=results,
                context=context,
            )
            total_usage = total_usage + result.usage
            plan.budget.require(total_usage)
            context.budget.require(total_usage)
            results[op_id] = result

        final = results[plan.output.op_id]
        if plan.output.path:
            selected = self._select_path(final.value, plan.output.path)
            final = replace(final, value=selected)
        return replace(final, usage=total_usage)

    def _preflight(
        self,
        plan: DataPlan,
        *,
        context: ExecutionContext,
    ) -> None:
        self._require_before_deadline(context)
        static_usage = Usage(actions=len(plan.operations))
        context.budget.require(static_usage)
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
            source_result = self._compose(
                operation,
                prior_results=prior_results,
            )
        else:
            if operation.source is None:
                raise ExecutionError(
                    "operation {!r} has no Core executor or source".format(
                        operation.operation
                    )
                )
            adapter = self._catalog.get(operation.source)
            try:
                source_result = await adapter.execute(
                    operation,
                    context=context,
                )
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

    def _compose(
        self,
        operation: Compose,
        *,
        prior_results: Mapping[str, ResultEnvelope[Any]],
    ) -> SourceResult[ContextPack]:
        items = []
        evidence = []
        provenance = []
        snapshots = []
        warnings = []
        partial = False
        seen_evidence = set()
        seen_provenance = set()
        seen_snapshots = set()

        for ref in operation.inputs:
            upstream = prior_results.get(ref.op_id)
            if upstream is None:
                raise ExecutionError(
                    "compose input {!r} has not been executed".format(ref.op_id)
                )
            selected = self._select_path(upstream.value, ref.path)
            items.append(ContextItem(ref=ref, value=selected))
            partial = partial or upstream.status is ResultStatus.PARTIAL
            warnings.extend(upstream.warnings)

            for item in upstream.evidence:
                if item.evidence_id not in seen_evidence:
                    seen_evidence.add(item.evidence_id)
                    evidence.append(item)
            for item in upstream.provenance:
                key = self._provenance_key(item)
                if key not in seen_provenance:
                    seen_provenance.add(key)
                    provenance.append(item)
            for item in upstream.snapshots:
                key = self._snapshot_key(item)
                if key not in seen_snapshots:
                    seen_snapshots.add(key)
                    snapshots.append(item)

        context_pack = ContextPack(
            strategy=operation.strategy,
            items=tuple(items),
            evidence_ids=tuple(item.evidence_id for item in evidence),
        )
        return SourceResult(
            value=context_pack,
            result_kind=ResultKind.EVIDENCE_SET,
            evidence=tuple(evidence),
            provenance=tuple(provenance),
            snapshots=tuple(snapshots),
            warnings=tuple(warnings),
            status=ResultStatus.PARTIAL if partial else ResultStatus.OK,
        )

    @staticmethod
    def _select_path(value: Any, path: Sequence[Any]) -> Any:
        selected = value
        for part in path:
            if isinstance(selected, Mapping):
                try:
                    selected = selected[part]
                except KeyError as exc:
                    raise ExecutionError(
                        "output path key {!r} does not exist".format(part)
                    ) from exc
            elif (
                isinstance(selected, Sequence)
                and not isinstance(selected, (str, bytes, bytearray))
                and isinstance(part, int)
            ):
                try:
                    selected = selected[part]
                except IndexError as exc:
                    raise ExecutionError(
                        "output path index {} is out of range".format(part)
                    ) from exc
            elif is_dataclass(selected) and isinstance(part, str):
                field_names = {item.name for item in fields(selected)}
                if part not in field_names:
                    raise ExecutionError(
                        "output path field {!r} does not exist".format(part)
                    )
                selected = getattr(selected, part)
            else:
                raise ExecutionError(
                    "cannot apply output path item {!r} to {}".format(
                        part,
                        type(selected).__name__,
                    )
                )
        return selected

    @staticmethod
    def _provenance_key(item: Provenance) -> Tuple[Any, ...]:
        return (
            item.source,
            item.locator,
            item.snapshot,
            item.valid_from,
            item.valid_to,
        )

    @staticmethod
    def _snapshot_key(item: SnapshotRef) -> Tuple[Any, ...]:
        return (item.source, item.version, item.checksum)

    @staticmethod
    def _require_before_deadline(context: ExecutionContext) -> None:
        if context.deadline is not None and utc_now() >= context.deadline:
            raise ExecutionError("execution deadline has expired")
