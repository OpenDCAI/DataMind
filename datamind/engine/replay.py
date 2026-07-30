"""Offline replay using protected source-result artifacts."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict

from datamind.dataops import (
    ResultEnvelope,
    plan_to_dict,
    validate_plan,
)
from datamind.kernel import (
    ExecutionTrace,
    ReplayError,
    TraceEventKind,
    Usage,
)
from datamind.ports import ReplayArtifactStore, TraceStore

from .fingerprint import fingerprint
from .runtime import (
    execute_dataflow_operation,
    is_dataflow_operation,
    select_path,
)


class ReplayEngine:
    """Re-run deterministic Core operations without live source access."""

    def __init__(
        self,
        *,
        trace_store: TraceStore,
        artifact_store: ReplayArtifactStore,
    ) -> None:
        self._trace_store = trace_store
        self._artifact_store = artifact_store

    async def replay(self, trace_id: str) -> ResultEnvelope[Any]:
        trace = await self._trace_store.get(trace_id)
        try:
            return await self._replay_completed_trace(trace_id, trace)
        except Exception as exc:
            await self._trace_store.append(
                trace_id,
                TraceEventKind.REPLAY_FAILED,
                details={
                    "error_type": "{}.{}".format(
                        type(exc).__module__,
                        type(exc).__qualname__,
                    ),
                    "error_fingerprint": fingerprint(str(exc)),
                },
            )
            raise

    async def _replay_completed_trace(
        self,
        trace_id: str,
        trace: ExecutionTrace,
    ) -> ResultEnvelope[Any]:
        completion = next(
            (
                event
                for event in reversed(trace.events)
                if event.kind is TraceEventKind.PLAN_COMPLETED
            ),
            None,
        )
        if completion is None:
            raise ReplayError(
                "trace {!r} has no completed execution".format(trace_id)
            )

        recorded_plan = await self._artifact_store.load_plan(trace_id)
        current_plan_fingerprint = fingerprint(
            plan_to_dict(recorded_plan.plan)
        )
        if current_plan_fingerprint != recorded_plan.fingerprint:
            raise ReplayError("recorded plan fingerprint mismatch")
        trace_plan_fingerprint = trace.events[0].details.get(
            "plan_fingerprint"
        )
        if trace_plan_fingerprint != recorded_plan.fingerprint:
            raise ReplayError("trace and plan artifact fingerprints differ")

        plan = recorded_plan.plan
        report = validate_plan(plan)
        report.require_valid()
        operations = {operation.op_id: operation for operation in plan.operations}
        results: Dict[str, ResultEnvelope[Any]] = {}
        total_usage = Usage()
        for op_id in report.topological_order:
            operation = operations[op_id]
            recorded = await self._artifact_store.load_result(
                trace_id,
                op_id,
            )
            if recorded.result.op_id != op_id:
                raise ReplayError(
                    "recorded result belongs to a different operation"
                )
            if fingerprint(recorded.result) != recorded.fingerprint:
                raise ReplayError(
                    "recorded result fingerprint mismatch for {!r}".format(
                        op_id
                    )
                )

            if is_dataflow_operation(operation):
                source_result = execute_dataflow_operation(
                    operation,
                    prior_results=results,
                )
                result = ResultEnvelope(
                    op_id=operation.op_id,
                    value=source_result.value,
                    result_kind=source_result.result_kind,
                    trace_id=trace_id,
                    evidence=source_result.evidence,
                    bindings=source_result.bindings,
                    provenance=source_result.provenance,
                    snapshots=source_result.snapshots,
                    usage=source_result.usage + Usage(actions=1),
                    warnings=source_result.warnings,
                    status=source_result.status,
                )
                if fingerprint(result) != recorded.fingerprint:
                    raise ReplayError(
                        "deterministic replay diverged at {!r}".format(op_id)
                    )
            else:
                result = recorded.result

            total_usage = total_usage + result.usage
            plan.budget.require(total_usage)
            results[op_id] = result

        final = results[plan.output.op_id]
        if plan.output.path:
            final = replace(
                final,
                value=select_path(final.value, plan.output.path),
            )
        final = replace(final, usage=total_usage)
        final_fingerprint = fingerprint(final)
        expected_fingerprint = completion.details.get("result_fingerprint")
        if final_fingerprint != expected_fingerprint:
            raise ReplayError("final replay result fingerprint mismatch")

        await self._trace_store.append(
            trace_id,
            TraceEventKind.REPLAY_COMPLETED,
            details={
                "result_fingerprint": final_fingerprint,
                "operation_count": len(plan.operations),
            },
        )
        return final


__all__ = ["ReplayEngine"]
