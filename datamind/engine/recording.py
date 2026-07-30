"""Trace and replay-artifact recording isolated from execution scheduling."""
from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from datamind.dataops import DataPlan, ResultEnvelope, plan_to_dict
from datamind.kernel import (
    Budget,
    ExecutionContext,
    TraceEventKind,
    Usage,
)
from datamind.ports import (
    RecordedPlan,
    RecordedResult,
    ReplayArtifactStore,
    TraceStore,
)

from .fingerprint import fingerprint


class ExecutionRecorder:
    """Record execution metadata only when a store is explicitly configured."""

    def __init__(
        self,
        *,
        trace_store: Optional[TraceStore] = None,
        artifact_store: Optional[ReplayArtifactStore] = None,
    ) -> None:
        self._trace_store = trace_store
        self._artifact_store = artifact_store

    @property
    def enabled(self) -> bool:
        return (
            self._trace_store is not None
            or self._artifact_store is not None
        )

    @property
    def replay_stores(
        self,
    ) -> Tuple[Optional[TraceStore], Optional[ReplayArtifactStore]]:
        return self._trace_store, self._artifact_store

    async def start_plan(
        self,
        plan: DataPlan,
        *,
        context: ExecutionContext,
    ) -> bool:
        if not self.enabled:
            return False
        plan_fingerprint = fingerprint(plan_to_dict(plan))
        trace_started = False
        try:
            if self._trace_store is not None:
                await self._trace_store.start(
                    context.trace_id,
                    details={
                        "plan_id": plan.plan_id,
                        "plan_version": plan.version,
                        "plan_fingerprint": plan_fingerprint,
                        "operation_count": len(plan.operations),
                        "plan_max_effect": plan.max_effect.name,
                        "context_max_effect": context.max_effect.name,
                        "context_budget": self._budget_details(
                            context.budget
                        ),
                        "snapshot_set_fingerprint": fingerprint(
                            context.snapshots
                        ),
                        "snapshot_pins": [
                            {
                                "source_id": item.source.source_id,
                                "source_kind": item.source.kind.value,
                                "version": item.version,
                                "checksum": item.checksum,
                            }
                            for item in context.snapshots.snapshots
                        ],
                        "request_id": context.request_id,
                        "profile_fingerprint": fingerprint(context.profile),
                        "session_fingerprint": (
                            fingerprint(context.session_id)
                            if context.session_id is not None
                            else None
                        ),
                        "user_fingerprint": (
                            fingerprint(context.user_id)
                            if context.user_id is not None
                            else None
                        ),
                        "readable_scope_fingerprints": sorted(
                            fingerprint(item)
                            for item in context.readable_scopes
                        ),
                        "writable_scope_fingerprints": sorted(
                            fingerprint(item)
                            for item in context.writable_scopes
                        ),
                    },
                )
                trace_started = True
            if self._artifact_store is not None:
                await self._artifact_store.save_plan(
                    context.trace_id,
                    RecordedPlan(
                        plan=plan,
                        fingerprint=plan_fingerprint,
                    ),
                )
        except Exception as exc:
            if trace_started:
                await self.fail_plan(context.trace_id, exc)
            raise
        return trace_started

    async def plan_validated(
        self,
        trace_id: str,
        *,
        topological_order: Tuple[str, ...],
        static_actions: int,
        max_parallelism: int,
    ) -> None:
        await self._append(
            trace_id,
            TraceEventKind.PLAN_VALIDATED,
            details={
                "topological_order": list(topological_order),
                "static_actions": static_actions,
                "max_parallelism": max_parallelism,
            },
        )

    async def start_operation(
        self,
        trace_id: str,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> None:
        scopes = tuple(getattr(operation, "scopes", ()))
        proposal = getattr(operation, "proposal", None)
        draft = getattr(operation, "draft", None)
        if draft is None and proposal is not None:
            draft = proposal.draft
        origin_channel = (
            proposal.origin.channel.value
            if proposal is not None
            else (
                context.memory_origin.value
                if draft is not None
                and context.memory_origin is not None
                else None
            )
        )
        await self._append(
            trace_id,
            TraceEventKind.OP_STARTED,
            op_id=operation.op_id,
            details={
                "operation": operation.operation,
                "effect": operation.effect.level.name,
                "source_id": (
                    operation.source.source_id
                    if operation.source is not None
                    else None
                ),
                "inputs": [ref.op_id for ref in operation.inputs],
                "scope_fingerprints": sorted(
                    fingerprint(item) for item in scopes
                ),
                "memory_origin_channel": origin_channel,
                "memory_proposal_fingerprint": (
                    fingerprint(proposal)
                    if proposal is not None
                    else None
                ),
                "idempotency_key_fingerprint": (
                    fingerprint(draft.idempotency_key)
                    if draft is not None
                    else None
                ),
                "valid_at": (
                    operation.valid_at.isoformat()
                    if getattr(operation, "valid_at", None) is not None
                    else None
                ),
                "known_at": (
                    operation.known_at.isoformat()
                    if getattr(operation, "known_at", None) is not None
                    else None
                ),
            },
        )

    async def complete_operation(
        self,
        trace_id: str,
        result: ResultEnvelope[Any],
    ) -> None:
        if not self.enabled:
            return
        result_fingerprint = fingerprint(result)
        if self._artifact_store is not None:
            await self._artifact_store.save_result(
                trace_id,
                result.op_id,
                RecordedResult(
                    result=result,
                    fingerprint=result_fingerprint,
                ),
            )
        await self._append(
            trace_id,
            TraceEventKind.OP_COMPLETED,
            op_id=result.op_id,
            details={
                "result_kind": result.result_kind.value,
                "result_fingerprint": result_fingerprint,
                "status": result.status.value,
                "usage": self._usage_details(result.usage),
                "evidence_count": len(result.evidence),
                "binding_field_count": len(result.bindings.fields),
                "binding_row_count": len(result.bindings.rows),
                "evidence_ids": [
                    item.evidence_id for item in result.evidence
                ],
                "snapshots": [
                    {
                        "source_id": item.source.source_id,
                        "source_kind": item.source.kind.value,
                        "version": item.version,
                        "checksum": item.checksum,
                    }
                    for item in result.snapshots
                ],
                "warning_count": len(result.warnings),
            },
        )

    async def fail_operation(
        self,
        trace_id: str,
        op_id: str,
        error: Exception,
    ) -> None:
        await self._append(
            trace_id,
            TraceEventKind.OP_FAILED,
            op_id=op_id,
            details=self._error_details(error),
        )

    async def complete_plan(
        self,
        trace_id: str,
        plan: DataPlan,
        result: ResultEnvelope[Any],
    ) -> None:
        if self._trace_store is None:
            return
        await self._append(
            trace_id,
            TraceEventKind.PLAN_COMPLETED,
            details={
                "final_op_id": plan.output.op_id,
                "result_fingerprint": fingerprint(result),
                "usage": self._usage_details(result.usage),
                "status": result.status.value,
            },
        )

    async def fail_plan(self, trace_id: str, error: Exception) -> None:
        await self._append(
            trace_id,
            TraceEventKind.PLAN_FAILED,
            details=self._error_details(error),
        )

    async def _append(
        self,
        trace_id: str,
        kind: TraceEventKind,
        *,
        op_id: Optional[str] = None,
        details: Mapping[str, Any],
    ) -> None:
        if self._trace_store is not None:
            await self._trace_store.append(
                trace_id,
                kind,
                op_id=op_id,
                details=details,
            )

    @staticmethod
    def _usage_details(usage: Usage) -> Mapping[str, Any]:
        return {
            "tokens": usage.tokens,
            "latency_ms": usage.latency_ms,
            "cost_usd": str(usage.cost_usd),
            "actions": usage.actions,
        }

    @staticmethod
    def _budget_details(budget: Budget) -> Mapping[str, Any]:
        return {
            "max_tokens": budget.max_tokens,
            "max_latency_ms": budget.max_latency_ms,
            "max_cost_usd": (
                str(budget.max_cost_usd)
                if budget.max_cost_usd is not None
                else None
            ),
            "max_actions": budget.max_actions,
        }

    @staticmethod
    def _error_details(error: Exception) -> Mapping[str, Any]:
        return {
            "error_type": "{}.{}".format(
                type(error).__module__,
                type(error).__qualname__,
            ),
            "error_fingerprint": fingerprint(str(error)),
        }


__all__ = ["ExecutionRecorder"]
