"""Port contracts between the deterministic engine and source adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Mapping, Protocol, Tuple, TypeVar

from datamind.dataops import (
    BindingSet,
    DataOp,
    Evidence,
    ResultKind,
    ResultStatus,
)
from datamind.kernel import (
    ExecutionContext,
    KernelValidationError,
    Provenance,
    SnapshotRef,
    SourceDescriptor,
    SourceKind,
    SourceRef,
    Usage,
)

T = TypeVar("T")


@dataclass(frozen=True)
class SourceResult(Generic[T]):
    """Adapter result before the engine adds operation and trace identity."""

    value: T
    result_kind: ResultKind
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
                "source result_kind must be a ResultKind"
            )
        if not isinstance(self.status, ResultStatus):
            raise KernelValidationError(
                "source result status must be a ResultStatus"
            )
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not isinstance(self.bindings, BindingSet):
            raise KernelValidationError(
                "source bindings must be a BindingSet"
            )
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if any(not isinstance(item, Evidence) for item in self.evidence):
            raise KernelValidationError(
                "source evidence must contain Evidence values"
            )
        if any(
            not isinstance(item, Provenance)
            for item in self.provenance
        ):
            raise KernelValidationError(
                "source provenance must contain Provenance values"
            )
        if any(
            not isinstance(item, SnapshotRef)
            for item in self.snapshots
        ):
            raise KernelValidationError(
                "source snapshots must contain SnapshotRef values"
            )
        if not isinstance(self.usage, Usage):
            raise KernelValidationError("source usage must be Usage")
        evidence_ids_in_order = tuple(
            item.evidence_id for item in self.evidence
        )
        if len(set(evidence_ids_in_order)) != len(evidence_ids_in_order):
            raise KernelValidationError(
                "source evidence ids cannot contain duplicates"
            )
        evidence_ids = set(evidence_ids_in_order)
        referenced_ids = {
            evidence_id
            for row in self.bindings.rows
            for evidence_id in row.evidence_ids
        }
        unknown_ids = sorted(referenced_ids - evidence_ids)
        if unknown_ids:
            raise KernelValidationError(
                "source bindings reference unknown evidence ids: {}".format(
                    unknown_ids
                )
            )
        if any(
            not isinstance(warning, str) or not warning.strip()
            for warning in self.warnings
        ):
            raise KernelValidationError(
                "source result warnings must be non-empty strings"
            )
        if self.status is ResultStatus.PARTIAL and not self.warnings:
            raise KernelValidationError(
                "partial source results must explain their degradation"
            )


class DataSource(Protocol):
    """A source adapter capable of executing declared DataOps."""

    @property
    def descriptor(self) -> SourceDescriptor:
        ...

    async def execute(
        self,
        operation: DataOp[Any],
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        ...


class SourceCatalogPort(Protocol):
    """Read surface consumed by the engine; implementations remain injectable."""

    def get(self, source: SourceRef) -> DataSource:
        ...

    def describe(self, source: SourceRef) -> SourceDescriptor:
        ...

    def discover(
        self,
        kinds: Tuple[SourceKind, ...] = (),
    ) -> Tuple[SourceDescriptor, ...]:
        ...

    def descriptors(self) -> Mapping[str, SourceDescriptor]:
        ...


__all__ = [
    "DataSource",
    "SourceCatalogPort",
    "SourceResult",
]
