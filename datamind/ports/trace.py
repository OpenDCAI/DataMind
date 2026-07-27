"""Ports separating content-safe audit traces from replay artifacts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from datamind.dataops import DataPlan, ResultEnvelope
from datamind.kernel import (
    ExecutionTrace,
    KernelValidationError,
    TraceEvent,
    TraceEventKind,
)


@dataclass(frozen=True)
class RecordedPlan:
    plan: DataPlan
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan, DataPlan):
            raise KernelValidationError(
                "recorded plan must contain a DataPlan"
            )
        if not isinstance(self.fingerprint, str) or not self.fingerprint.strip():
            raise KernelValidationError(
                "recorded plan fingerprint must be non-empty"
            )


@dataclass(frozen=True)
class RecordedResult:
    result: ResultEnvelope[Any]
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.result, ResultEnvelope):
            raise KernelValidationError(
                "recorded result must contain a ResultEnvelope"
            )
        if not isinstance(self.fingerprint, str) or not self.fingerprint.strip():
            raise KernelValidationError(
                "recorded result fingerprint must be non-empty"
            )


class TraceStore(Protocol):
    """Append-only store for audit-safe trace events."""

    async def start(
        self,
        trace_id: str,
        *,
        details: Mapping[str, Any],
    ) -> TraceEvent:
        ...

    async def append(
        self,
        trace_id: str,
        kind: TraceEventKind,
        *,
        op_id: Optional[str] = None,
        details: Mapping[str, Any],
    ) -> TraceEvent:
        ...

    async def get(self, trace_id: str) -> ExecutionTrace:
        ...


class ReplayArtifactStore(Protocol):
    """Protected storage for plans and native results required by replay."""

    async def save_plan(
        self,
        trace_id: str,
        record: RecordedPlan,
    ) -> None:
        ...

    async def load_plan(self, trace_id: str) -> RecordedPlan:
        ...

    async def save_result(
        self,
        trace_id: str,
        op_id: str,
        record: RecordedResult,
    ) -> None:
        ...

    async def load_result(
        self,
        trace_id: str,
        op_id: str,
    ) -> RecordedResult:
        ...


__all__ = [
    "RecordedPlan",
    "RecordedResult",
    "ReplayArtifactStore",
    "TraceStore",
]
