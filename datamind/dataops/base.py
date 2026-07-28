"""Typed operation protocol and references between DataPlan outputs."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, FrozenSet, Generic, Optional, Protocol, Tuple, TypeVar

from datamind.kernel import (
    EffectLevel,
    EffectSpec,
    KernelValidationError,
    SourceKind,
    SourceRef,
)

T = TypeVar("T")


class ResultKind(str, Enum):
    SOURCE_LIST = "source_list"
    SOURCE_DESCRIPTOR = "source_descriptor"
    DOCUMENT_HITS = "document_hits"
    TABLE = "table"
    MEMORY_RECORDS = "memory_records"
    EVIDENCE_SET = "evidence_set"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class OutputRef(Generic[T]):
    """Reference to a prior operation result or a nested path within it."""

    op_id: str
    path: Tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.op_id, str):
            raise KernelValidationError("output reference op_id must be a string")
        if not self.op_id.strip():
            raise KernelValidationError("output reference op_id must be non-empty")
        object.__setattr__(self, "path", tuple(self.path))
        for item in self.path:
            if not isinstance(item, (str, int)):
                raise KernelValidationError(
                    "output reference path items must be strings or integers"
                )


class DataOp(Protocol, Generic[T]):
    """Serializable description of data work; never contains a handler."""

    op_id: str
    source: Optional[SourceRef]
    inputs: Tuple[OutputRef[Any], ...]
    operation: ClassVar[str]
    output_kind: ClassVar[ResultKind]
    effect_level: ClassVar[EffectLevel]
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]]

    @property
    def effect(self) -> EffectSpec:
        ...


class OperationMixin:
    """Common behaviour shared by concrete frozen operation dataclasses."""

    operation: ClassVar[str] = "operation"
    output_kind: ClassVar[ResultKind] = ResultKind.EVIDENCE_SET
    effect_level: ClassVar[EffectLevel] = EffectLevel.PURE
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset()

    op_id: str
    source: Optional[SourceRef]
    inputs: Tuple[OutputRef[Any], ...]

    def _validate_common(self) -> None:
        if not isinstance(self.op_id, str):
            raise KernelValidationError("operation op_id must be a string")
        if not self.op_id.strip():
            raise KernelValidationError("operation op_id must be non-empty")
        inputs = tuple(self.inputs)
        object.__setattr__(self, "inputs", inputs)
        if any(not isinstance(ref, OutputRef) for ref in inputs):
            raise KernelValidationError(
                "operation inputs must contain OutputRef values"
            )
        if self.source is not None and not isinstance(self.source, SourceRef):
            raise KernelValidationError(
                "operation source must be a SourceRef"
            )
        if len(set(inputs)) != len(inputs):
            raise KernelValidationError(
                "operation inputs cannot contain duplicate output references"
            )
        if self.allowed_source_kinds:
            if self.source is None:
                raise KernelValidationError(
                    "{} requires a source".format(self.operation)
                )
            if self.source.kind not in self.allowed_source_kinds:
                allowed = ", ".join(
                    sorted(kind.value for kind in self.allowed_source_kinds)
                )
                raise KernelValidationError(
                    "{} does not support source kind {}; expected {}".format(
                        self.operation, self.source.kind.value, allowed
                    )
                )
        elif self.source is not None:
            raise KernelValidationError(
                "{} is source-independent".format(self.operation)
            )

    @property
    def effect(self) -> EffectSpec:
        return EffectSpec(level=self.effect_level, resource=self.source)
