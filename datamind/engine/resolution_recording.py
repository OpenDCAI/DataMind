"""Content-safe parent recording for bounded compile/execute attempts."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from datamind.dataops import DataPlan, ResultEnvelope, plan_to_dict
from datamind.kernel import (
    Budget,
    ExecutionFailure,
    ResolutionEventKind,
    Usage,
)
from datamind.ports import CompiledPlan, ResolutionTraceStore

from .fingerprint import fingerprint


class ResolutionRecorder:
    """Keep multi-plan orchestration separate from single-plan Trace."""

    def __init__(
        self,
        store: Optional[ResolutionTraceStore] = None,
    ) -> None:
        self._store = store

    @property
    def enabled(self) -> bool:
        return self._store is not None

    async def start(
        self,
        resolution_id: str,
        *,
        request_id: str,
        intent: str,
        budget: Budget,
        source_catalog: Any,
    ) -> None:
        if self._store is None:
            return
        await self._store.start_resolution(
            resolution_id,
            details={
                "request_id": request_id,
                "intent_fingerprint": fingerprint(intent),
                "budget": self._budget_details(budget),
                "source_catalog_fingerprint": fingerprint(source_catalog),
            },
        )

    async def start_attempt(
        self,
        resolution_id: str,
        *,
        attempt_number: int,
        trace_id: str,
        compiled: CompiledPlan,
    ) -> None:
        if self._store is None:
            return
        await self._store.append_resolution(
            resolution_id,
            ResolutionEventKind.PLAN_ATTEMPT_STARTED,
            attempt_number=attempt_number,
            trace_id=trace_id,
            details={
                "plan_id": compiled.plan.plan_id,
                "plan_fingerprint": fingerprint(
                    plan_to_dict(compiled.plan)
                ),
                "compilation_usage": self._usage_details(compiled.usage),
                "compilation_attempts": [
                    {
                        "number": item.number,
                        "model": item.model,
                        "usage": self._usage_details(item.usage),
                        "issue_codes": [
                            issue.code for issue in item.issues
                        ],
                        "response_id": item.response_id,
                        "output_fingerprint": item.output_fingerprint,
                    }
                    for item in compiled.attempts
                ],
            },
        )

    async def complete_attempt(
        self,
        resolution_id: str,
        *,
        attempt_number: int,
        trace_id: str,
        result: ResultEnvelope[Any],
    ) -> None:
        await self._append_attempt(
            resolution_id,
            ResolutionEventKind.PLAN_ATTEMPT_COMPLETED,
            attempt_number=attempt_number,
            trace_id=trace_id,
            details={
                "result_fingerprint": fingerprint(result),
                "execution_usage": self._usage_details(result.usage),
            },
        )

    async def fail_attempt(
        self,
        resolution_id: str,
        *,
        attempt_number: int,
        trace_id: str,
        failure: ExecutionFailure,
        will_replan: bool,
    ) -> None:
        await self._append_attempt(
            resolution_id,
            ResolutionEventKind.PLAN_ATTEMPT_FAILED,
            attempt_number=attempt_number,
            trace_id=trace_id,
            details={
                "failure": self._failure_details(failure),
                "will_replan": will_replan,
            },
        )

    async def complete(
        self,
        resolution_id: str,
        *,
        attempt_count: int,
        final_trace_id: str,
        usage: Usage,
    ) -> None:
        if self._store is None:
            return
        await self._store.append_resolution(
            resolution_id,
            ResolutionEventKind.RESOLUTION_COMPLETED,
            details={
                "attempt_count": attempt_count,
                "final_trace_id": final_trace_id,
                "usage": self._usage_details(usage),
            },
        )

    async def fail(
        self,
        resolution_id: str,
        error: Exception,
        *,
        attempt_count: int,
        usage: Usage,
    ) -> None:
        if self._store is None:
            return
        await self._store.append_resolution(
            resolution_id,
            ResolutionEventKind.RESOLUTION_FAILED,
            details={
                "attempt_count": attempt_count,
                "usage": self._usage_details(usage),
                "error_type": "{}.{}".format(
                    type(error).__module__,
                    type(error).__qualname__,
                ),
                "error_fingerprint": fingerprint(str(error)),
            },
        )

    async def _append_attempt(
        self,
        resolution_id: str,
        kind: ResolutionEventKind,
        *,
        attempt_number: int,
        trace_id: str,
        details: Mapping[str, Any],
    ) -> None:
        if self._store is not None:
            await self._store.append_resolution(
                resolution_id,
                kind,
                attempt_number=attempt_number,
                trace_id=trace_id,
                details=details,
            )

    @staticmethod
    def _failure_details(
        failure: ExecutionFailure,
    ) -> Mapping[str, Any]:
        return {
            "kind": failure.kind.value,
            "error_type": failure.error_type,
            "error_fingerprint": failure.error_fingerprint,
            "usage": ResolutionRecorder._usage_details(failure.usage),
            "failed_op_id": failure.failed_op_id,
            "operation": failure.operation,
            "source_id": failure.source_id,
            "completed_op_ids": list(failure.completed_op_ids),
            "recoverable": failure.recoverable,
        }

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


__all__ = ["ResolutionRecorder"]
