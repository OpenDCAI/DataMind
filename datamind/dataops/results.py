"""Native result values plus normalized evidence and execution metadata."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Optional, Tuple, TypeVar

from datamind.kernel import (
    JsonObject,
    KernelValidationError,
    MemoryConflict,
    MemoryRecord,
    Provenance,
    SnapshotRef,
    SourceKind,
    Usage,
    freeze_json_object,
    new_id,
)

from .base import OutputRef, ResultKind

T = TypeVar("T")


class ResultStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ContextItem:
    """A structure-preserving value selected from an upstream result."""

    ref: OutputRef[Any]
    value: Any


@dataclass(frozen=True)
class ContextPack:
    """Composed native values plus stable references to normalized evidence."""

    strategy: str
    items: Tuple[ContextItem, ...]
    evidence_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, str) or not self.strategy.strip():
            raise KernelValidationError(
                "context pack strategy must be non-empty"
            )
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if any(not isinstance(item, ContextItem) for item in self.items):
            raise KernelValidationError(
                "context pack items must contain ContextItem values"
            )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.evidence_ids
        ):
            raise KernelValidationError(
                "context pack evidence ids must be non-empty strings"
            )


@dataclass(frozen=True)
class MemoryRecallResult:
    """Native Recall value with conflicts kept separate from memory content."""

    records: Tuple[MemoryRecord, ...]
    conflicts: Tuple[MemoryConflict, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "conflicts", tuple(self.conflicts))
        if any(not isinstance(item, MemoryRecord) for item in self.records):
            raise KernelValidationError(
                "memory recall records must contain MemoryRecord values"
            )
        if any(
            not isinstance(item, MemoryConflict)
            for item in self.conflicts
        ):
            raise KernelValidationError(
                "memory recall conflicts must contain MemoryConflict values"
            )
        record_ids = tuple(item.memory_id for item in self.records)
        if len(set(record_ids)) != len(record_ids):
            raise KernelValidationError(
                "memory recall records must have unique ids"
            )
        returned = set(record_ids)
        if any(
            not set(conflict.record_ids).issubset(returned)
            for conflict in self.conflicts
        ):
            raise KernelValidationError(
                "memory conflicts must reference returned records"
            )


@dataclass(frozen=True)
class Evidence:
    """Normalized view consumed by composition and downstream agents."""

    kind: SourceKind
    content: str
    provenance: Provenance
    evidence_id: str = field(default_factory=lambda: new_id("evidence"))
    score: Optional[float] = None
    metadata: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKind):
            raise KernelValidationError("evidence kind must be a SourceKind")
        if not isinstance(self.provenance, Provenance):
            raise KernelValidationError(
                "evidence provenance must be Provenance"
            )
        if not isinstance(self.evidence_id, str):
            raise KernelValidationError("evidence_id must be a string")
        if not self.evidence_id.strip():
            raise KernelValidationError("evidence_id must be non-empty")
        if not isinstance(self.content, str):
            raise KernelValidationError("evidence content must be a string")
        if not self.content.strip():
            raise KernelValidationError("evidence content must be non-empty")
        if self.kind != self.provenance.source.kind:
            raise KernelValidationError(
                "evidence kind must match provenance source kind"
            )
        if self.score is not None and not math.isfinite(self.score):
            raise KernelValidationError("evidence score must be finite")
        object.__setattr__(
            self, "metadata", freeze_json_object(self.metadata)
        )


@dataclass(frozen=True)
class ResultEnvelope(Generic[T]):
    """Unified execution result without flattening the native value."""

    op_id: str
    value: T
    result_kind: ResultKind
    trace_id: str
    evidence: Tuple[Evidence, ...] = ()
    provenance: Tuple[Provenance, ...] = ()
    snapshots: Tuple[SnapshotRef, ...] = ()
    usage: Usage = field(default_factory=Usage)
    warnings: Tuple[str, ...] = ()
    status: ResultStatus = ResultStatus.OK

    def __post_init__(self) -> None:
        if not isinstance(self.result_kind, ResultKind):
            raise KernelValidationError(
                "result_kind must be a ResultKind"
            )
        if not isinstance(self.status, ResultStatus):
            raise KernelValidationError("status must be a ResultStatus")
        if not isinstance(self.op_id, str):
            raise KernelValidationError("result op_id must be a string")
        if not self.op_id.strip():
            raise KernelValidationError("result op_id must be non-empty")
        if not isinstance(self.trace_id, str):
            raise KernelValidationError("result trace_id must be a string")
        if not self.trace_id.strip():
            raise KernelValidationError("result trace_id must be non-empty")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if any(
            not isinstance(warning, str) or not warning.strip()
            for warning in self.warnings
        ):
            raise KernelValidationError("result warnings cannot be blank")
        if self.status is ResultStatus.PARTIAL and not self.warnings:
            raise KernelValidationError(
                "partial results must explain the degradation in warnings"
            )
