"""Native result values plus normalized evidence and execution metadata."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Mapping, Optional, Tuple, TypeVar

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
class BindingRow:
    """One flat, JSON-safe record with references to supporting evidence."""

    values: JsonObject
    evidence_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "values",
            freeze_json_object(self.values),
        )
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if any(
            not isinstance(name, str) or not name.strip()
            for name in self.values
        ):
            raise KernelValidationError(
                "binding row field names must be non-empty strings"
            )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.evidence_ids
        ):
            raise KernelValidationError(
                "binding evidence ids must be non-empty strings"
            )
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise KernelValidationError(
                "binding evidence ids cannot contain duplicates"
            )


@dataclass(frozen=True)
class BindingSet:
    """Deterministic relational view kept alongside a native result."""

    fields: Tuple[str, ...] = ()
    rows: Tuple[BindingRow, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "rows", tuple(self.rows))
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.fields
        ):
            raise KernelValidationError(
                "binding fields must be non-empty strings"
            )
        if len(set(self.fields)) != len(self.fields):
            raise KernelValidationError(
                "binding fields cannot contain duplicates"
            )
        if any(not isinstance(item, BindingRow) for item in self.rows):
            raise KernelValidationError(
                "binding rows must contain BindingRow values"
            )
        if self.rows and not self.fields:
            raise KernelValidationError(
                "non-empty binding rows require at least one field"
            )
        expected = frozenset(self.fields)
        for row in self.rows:
            actual = frozenset(row.values)
            if actual != expected:
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                raise KernelValidationError(
                    "binding row schema mismatch; missing={}, extra={}".format(
                        missing,
                        extra,
                    )
                )

    def __len__(self) -> int:
        return len(self.rows)

    @classmethod
    def from_values(
        cls,
        fields: Tuple[str, ...],
        rows: Tuple[Mapping[str, Any], ...],
        evidence_ids: Tuple[Tuple[str, ...], ...] = (),
    ) -> "BindingSet":
        """Build a strict BindingSet while preserving explicit field order."""

        fields = tuple(fields)
        rows = tuple(rows)
        if evidence_ids:
            evidence_ids = tuple(tuple(item) for item in evidence_ids)
            if len(evidence_ids) != len(rows):
                raise KernelValidationError(
                    "binding evidence rows must align with values"
                )
        else:
            evidence_ids = tuple(() for _ in rows)
        return cls(
            fields=fields,
            rows=tuple(
                BindingRow(
                    values={field: row[field] for field in fields},
                    evidence_ids=row_evidence,
                )
                for row, row_evidence in zip(rows, evidence_ids)
            ),
        )


@dataclass(frozen=True)
class EvidenceSet:
    """Ordered evidence identities produced by a deterministic fuse."""

    strategy: str
    evidence_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, str) or not self.strategy.strip():
            raise KernelValidationError(
                "evidence set strategy must be non-empty"
            )
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.evidence_ids
        ):
            raise KernelValidationError(
                "evidence set ids must be non-empty strings"
            )
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise KernelValidationError(
                "evidence set ids cannot contain duplicates"
            )


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
    bindings: BindingSet = field(default_factory=BindingSet)
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
        if any(not isinstance(item, Evidence) for item in self.evidence):
            raise KernelValidationError(
                "result evidence must contain Evidence values"
            )
        evidence_ids_in_order = tuple(
            item.evidence_id for item in self.evidence
        )
        if len(set(evidence_ids_in_order)) != len(evidence_ids_in_order):
            raise KernelValidationError(
                "result evidence ids cannot contain duplicates"
            )
        if not isinstance(self.bindings, BindingSet):
            raise KernelValidationError(
                "result bindings must be a BindingSet"
            )
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if any(
            not isinstance(warning, str) or not warning.strip()
            for warning in self.warnings
        ):
            raise KernelValidationError("result warnings cannot be blank")
        evidence_ids = set(evidence_ids_in_order)
        referenced_ids = {
            evidence_id
            for row in self.bindings.rows
            for evidence_id in row.evidence_ids
        }
        unknown_ids = sorted(referenced_ids - evidence_ids)
        if unknown_ids:
            raise KernelValidationError(
                "result bindings reference unknown evidence ids: {}".format(
                    unknown_ids
                )
            )
        if self.status is ResultStatus.PARTIAL and not self.warnings:
            raise KernelValidationError(
                "partial results must explain the degradation in warnings"
            )


__all__ = [
    "BindingRow",
    "BindingSet",
    "ContextItem",
    "ContextPack",
    "Evidence",
    "EvidenceSet",
    "MemoryRecallResult",
    "ResultEnvelope",
    "ResultStatus",
]
