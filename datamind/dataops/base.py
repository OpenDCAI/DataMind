"""Typed operation protocol and references between DataPlan outputs."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    ClassVar,
    FrozenSet,
    Generic,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
)

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
    GRAPH_PATHS = "graph_paths"
    MEMORY_RECORDS = "memory_records"
    SKILL_SPECS = "skill_specs"
    SKILL_RESULT = "skill_result"
    MEMORY_MUTATION_PROPOSAL = "memory_mutation_proposal"
    MEMORY_MUTATION_RECEIPT = "memory_mutation_receipt"
    BINDING_SET = "binding_set"
    EVIDENCE_SET = "evidence_set"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class OperationSignature:
    """Nominal data-flow contract for one serializable operation type."""

    output_kind: ResultKind
    accepted_input_kinds: FrozenSet[ResultKind] = frozenset()
    min_inputs: int = 0
    max_inputs: Optional[int] = 0
    allow_input_paths: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.output_kind, ResultKind):
            raise KernelValidationError(
                "signature output_kind must be a ResultKind"
            )
        object.__setattr__(
            self,
            "accepted_input_kinds",
            frozenset(self.accepted_input_kinds),
        )
        if any(
            not isinstance(item, ResultKind)
            for item in self.accepted_input_kinds
        ):
            raise KernelValidationError(
                "signature input kinds must contain ResultKind values"
            )
        for name in ("min_inputs", "max_inputs"):
            value = getattr(self, name)
            if value is None and name == "max_inputs":
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise KernelValidationError(
                    "signature {} must be an integer".format(name)
                )
            if value < 0:
                raise KernelValidationError(
                    "signature {} cannot be negative".format(name)
                )
        if (
            self.max_inputs is not None
            and self.min_inputs > self.max_inputs
        ):
            raise KernelValidationError(
                "signature min_inputs cannot exceed max_inputs"
            )
        if not isinstance(self.allow_input_paths, bool):
            raise KernelValidationError(
                "signature allow_input_paths must be a boolean"
            )
        if self.max_inputs != 0 and not self.accepted_input_kinds:
            raise KernelValidationError(
                "operations with inputs must declare accepted input kinds"
            )

    def accepts_count(self, count: int) -> bool:
        return (
            count >= self.min_inputs
            and (self.max_inputs is None or count <= self.max_inputs)
        )

    def accepts_kind(self, kind: ResultKind) -> bool:
        return kind in self.accepted_input_kinds


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
    signature: ClassVar[OperationSignature]
    effect_level: ClassVar[EffectLevel]
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]]

    @property
    def effect(self) -> EffectSpec:
        ...


class OperationMixin:
    """Common behaviour shared by concrete frozen operation dataclasses."""

    operation: ClassVar[str] = "operation"
    output_kind: ClassVar[ResultKind] = ResultKind.EVIDENCE_SET
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=ResultKind.EVIDENCE_SET,
    )
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
        if not isinstance(self.signature, OperationSignature):
            raise KernelValidationError(
                "{} signature must be an OperationSignature".format(
                    self.operation
                )
            )
        if self.signature.output_kind is not self.output_kind:
            raise KernelValidationError(
                "{} signature output does not match output_kind".format(
                    self.operation
                )
            )
        if not self.signature.accepts_count(len(inputs)):
            maximum = (
                "unbounded"
                if self.signature.max_inputs is None
                else str(self.signature.max_inputs)
            )
            raise KernelValidationError(
                "{} expects {}..{} inputs, received {}".format(
                    self.operation,
                    self.signature.min_inputs,
                    maximum,
                    len(inputs),
                )
            )
        if (
            not self.signature.allow_input_paths
            and any(ref.path for ref in inputs)
        ):
            raise KernelValidationError(
                "{} does not accept nested input paths".format(
                    self.operation
                )
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


__all__ = [
    "DataOp",
    "OperationMixin",
    "OperationSignature",
    "OutputRef",
    "ResultKind",
]
