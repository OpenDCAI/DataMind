"""Initial, deliberately small DataOps instruction set."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, FrozenSet, Optional, Tuple, Type, Union

from datamind.kernel import (
    EffectLevel,
    EffectSpec,
    JsonObject,
    KernelValidationError,
    MemoryKind,
    MemoryMutationDraft,
    MemoryMutationProposal,
    ScopeRef,
    SourceKind,
    SourceRef,
    freeze_json_object,
    new_id,
    require_aware,
)

from .base import (
    OperationMixin,
    OperationSignature,
    OutputRef,
    ResultKind,
)


_ALL_RESULT_KINDS = frozenset(ResultKind)
_BINDING_CAPABLE_RESULT_KINDS = frozenset(
    (
        ResultKind.DOCUMENT_HITS,
        ResultKind.TABLE,
        ResultKind.GRAPH_PATHS,
        ResultKind.MEMORY_RECORDS,
        ResultKind.SKILL_SPECS,
        ResultKind.SKILL_RESULT,
        ResultKind.BINDING_SET,
    )
)
_EVIDENCE_CAPABLE_RESULT_KINDS = frozenset(
    (
        ResultKind.DOCUMENT_HITS,
        ResultKind.TABLE,
        ResultKind.GRAPH_PATHS,
        ResultKind.MEMORY_RECORDS,
        ResultKind.SKILL_SPECS,
        ResultKind.SKILL_RESULT,
        ResultKind.BINDING_SET,
        ResultKind.EVIDENCE_SET,
    )
)


@dataclass(frozen=True)
class Discover(OperationMixin):
    """List registered sources, optionally filtered by surface kind."""

    kinds: Tuple[SourceKind, ...] = ()
    op_id: str = field(default_factory=lambda: new_id("discover"))
    inputs: Tuple[OutputRef[Any], ...] = field(default=(), init=False)
    source: ClassVar[None] = None

    operation: ClassVar[str] = "discover"
    output_kind: ClassVar[ResultKind] = ResultKind.SOURCE_LIST
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.PURE
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kinds", tuple(self.kinds))
        if any(not isinstance(kind, SourceKind) for kind in self.kinds):
            raise KernelValidationError(
                "discover kinds must contain SourceKind values"
            )
        if len(set(self.kinds)) != len(self.kinds):
            raise KernelValidationError("discover kinds cannot contain duplicates")
        self._validate_common()


@dataclass(frozen=True)
class Describe(OperationMixin):
    """Describe a source's schema, capabilities, and current version."""

    source: SourceRef
    op_id: str = field(default_factory=lambda: new_id("describe"))
    inputs: Tuple[OutputRef[Any], ...] = field(default=(), init=False)

    operation: ClassVar[str] = "describe"
    output_kind: ClassVar[ResultKind] = ResultKind.SOURCE_DESCRIPTOR
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.READ
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset(SourceKind)

    def __post_init__(self) -> None:
        self._validate_common()


@dataclass(frozen=True)
class Search(OperationMixin):
    """Retrieve ranked evidence from a document/KB source."""

    source: SourceRef
    query: str
    limit: int = 10
    filters: JsonObject = field(default_factory=freeze_json_object)
    op_id: str = field(default_factory=lambda: new_id("search"))
    inputs: Tuple[OutputRef[Any], ...] = field(default=(), init=False)

    operation: ClassVar[str] = "search"
    output_kind: ClassVar[ResultKind] = ResultKind.DOCUMENT_HITS
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.READ
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset(
        (SourceKind.DOCUMENT,)
    )

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise KernelValidationError("search query must be a string")
        if not self.query.strip():
            raise KernelValidationError("search query must be non-empty")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise KernelValidationError("search limit must be an integer")
        if self.limit <= 0:
            raise KernelValidationError("search limit must be positive")
        object.__setattr__(self, "filters", freeze_json_object(self.filters))
        self._validate_common()


@dataclass(frozen=True)
class Query(OperationMixin):
    """Run a source-native read query, initially SQL over table sources."""

    source: SourceRef
    statement: str
    language: str = "sql"
    parameters: JsonObject = field(default_factory=freeze_json_object)
    op_id: str = field(default_factory=lambda: new_id("query"))
    inputs: Tuple[OutputRef[Any], ...] = field(default=(), init=False)

    operation: ClassVar[str] = "query"
    output_kind: ClassVar[ResultKind] = ResultKind.TABLE
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.READ
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset(
        (SourceKind.TABLE,)
    )

    def __post_init__(self) -> None:
        if not isinstance(self.statement, str):
            raise KernelValidationError("query statement must be a string")
        if not self.statement.strip():
            raise KernelValidationError("query statement must be non-empty")
        if not isinstance(self.language, str):
            raise KernelValidationError("query language must be a string")
        if not self.language.strip():
            raise KernelValidationError("query language must be non-empty")
        object.__setattr__(
            self, "parameters", freeze_json_object(self.parameters)
        )
        self._validate_common()


@dataclass(frozen=True)
class Recall(OperationMixin):
    """Recall typed memory records from explicit scopes and time slices."""

    source: SourceRef
    query: str
    scopes: Tuple[ScopeRef, ...]
    kinds: Tuple[MemoryKind, ...] = ()
    valid_at: Optional[datetime] = None
    known_at: Optional[datetime] = None
    limit: int = 10
    op_id: str = field(default_factory=lambda: new_id("recall"))
    inputs: Tuple[OutputRef[Any], ...] = field(default=(), init=False)

    operation: ClassVar[str] = "recall"
    output_kind: ClassVar[ResultKind] = ResultKind.MEMORY_RECORDS
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.READ
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset(
        (SourceKind.MEMORY,)
    )

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise KernelValidationError(
                "recall query must be a non-empty string"
            )
        object.__setattr__(self, "scopes", tuple(self.scopes))
        if not self.scopes:
            raise KernelValidationError(
                "recall requires at least one explicit scope"
            )
        if any(not isinstance(item, ScopeRef) for item in self.scopes):
            raise KernelValidationError(
                "recall scopes must contain ScopeRef values"
            )
        if len(set(self.scopes)) != len(self.scopes):
            raise KernelValidationError(
                "recall scopes cannot contain duplicates"
            )
        object.__setattr__(self, "kinds", tuple(self.kinds))
        if any(not isinstance(item, MemoryKind) for item in self.kinds):
            raise KernelValidationError(
                "recall kinds must contain MemoryKind values"
            )
        if len(set(self.kinds)) != len(self.kinds):
            raise KernelValidationError(
                "recall kinds cannot contain duplicates"
            )
        for name in ("valid_at", "known_at"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, datetime):
                    raise KernelValidationError(
                        "recall {} must be a datetime".format(name)
                    )
                require_aware(value, "recall {}".format(name))
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise KernelValidationError(
                "recall limit must be an integer"
            )
        if self.limit <= 0:
            raise KernelValidationError(
                "recall limit must be positive"
            )
        self._validate_common()


@dataclass(frozen=True)
class ProposeMutation(OperationMixin):
    """Validate untrusted Memory intent without changing authoritative state."""

    source: SourceRef
    draft: MemoryMutationDraft
    op_id: str = field(default_factory=lambda: new_id("propose_mutation"))
    inputs: Tuple[OutputRef[Any], ...] = field(default=(), init=False)

    operation: ClassVar[str] = "propose_mutation"
    output_kind: ClassVar[ResultKind] = (
        ResultKind.MEMORY_MUTATION_PROPOSAL
    )
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.READ
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset(
        (SourceKind.MEMORY,)
    )

    def __post_init__(self) -> None:
        if not isinstance(self.draft, MemoryMutationDraft):
            raise KernelValidationError(
                "propose_mutation draft must be a MemoryMutationDraft"
            )
        self._validate_common()

    @property
    def scopes(self) -> Tuple[ScopeRef, ...]:
        return (self.draft.scope,)


@dataclass(frozen=True)
class ApplyMutation(OperationMixin):
    """Atomically apply one validated, snapshot-bound Memory proposal."""

    source: SourceRef
    proposal: MemoryMutationProposal
    op_id: str = field(default_factory=lambda: new_id("apply_mutation"))
    inputs: Tuple[OutputRef[Any], ...] = field(default=(), init=False)

    operation: ClassVar[str] = "apply_mutation"
    output_kind: ClassVar[ResultKind] = ResultKind.MEMORY_MUTATION_RECEIPT
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.INTERNAL_WRITE
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset(
        (SourceKind.MEMORY,)
    )

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, MemoryMutationProposal):
            raise KernelValidationError(
                "apply_mutation proposal must be a MemoryMutationProposal"
            )
        if self.proposal.source != self.source:
            raise KernelValidationError(
                "apply_mutation source must match its proposal"
            )
        self._validate_common()

    @property
    def scopes(self) -> Tuple[ScopeRef, ...]:
        return (self.proposal.draft.scope,)

    @property
    def effect(self) -> EffectSpec:
        return EffectSpec(
            level=self.effect_level,
            resource=self.source,
            reversible=True,
            requires_approval=self.proposal.requires_approval,
            approval_key=self.proposal.draft.approval_key,
            idempotency_key=self.proposal.draft.idempotency_key,
        )


@dataclass(frozen=True)
class Compose(OperationMixin):
    """Combine prior typed results into a normalized evidence set."""

    inputs: Tuple[OutputRef[Any], ...]
    strategy: str = "evidence_union"
    op_id: str = field(default_factory=lambda: new_id("compose"))
    source: ClassVar[None] = None

    operation: ClassVar[str] = "compose"
    output_kind: ClassVar[ResultKind] = ResultKind.EVIDENCE_SET
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
        accepted_input_kinds=_ALL_RESULT_KINDS,
        min_inputs=1,
        max_inputs=None,
        allow_input_paths=True,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.PURE
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        if not self.inputs:
            raise KernelValidationError("compose requires at least one input")
        if not isinstance(self.strategy, str):
            raise KernelValidationError("compose strategy must be a string")
        if not self.strategy.strip():
            raise KernelValidationError("compose strategy must be non-empty")
        self._validate_common()


class ComparisonOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    IN = "in"
    CONTAINS = "contains"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class BindingPredicate:
    """Serializable deterministic predicate over one flat binding field."""

    field: str
    operator: ComparisonOperator
    value: Any

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not self.field.strip():
            raise KernelValidationError(
                "binding predicate field must be non-empty"
            )
        if not isinstance(self.operator, ComparisonOperator):
            raise KernelValidationError(
                "binding predicate operator must be a ComparisonOperator"
            )
        object.__setattr__(self, "value", freeze_json_object(
            {"value": self.value}
        )["value"])


@dataclass(frozen=True)
class Project(OperationMixin):
    """Select and reorder fields from an upstream normalized BindingSet."""

    inputs: Tuple[OutputRef[Any], ...]
    fields: Tuple[str, ...]
    op_id: str = field(default_factory=lambda: new_id("project"))
    source: ClassVar[None] = None

    operation: ClassVar[str] = "project"
    output_kind: ClassVar[ResultKind] = ResultKind.BINDING_SET
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
        accepted_input_kinds=_BINDING_CAPABLE_RESULT_KINDS,
        min_inputs=1,
        max_inputs=1,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.PURE
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "fields", tuple(self.fields))
        if not self.fields:
            raise KernelValidationError(
                "project requires at least one field"
            )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.fields
        ):
            raise KernelValidationError(
                "project fields must be non-empty strings"
            )
        if len(set(self.fields)) != len(self.fields):
            raise KernelValidationError(
                "project fields cannot contain duplicates"
            )
        self._validate_common()


@dataclass(frozen=True)
class Filter(OperationMixin):
    """Filter a BindingSet using one deterministic scalar predicate."""

    inputs: Tuple[OutputRef[Any], ...]
    predicate: BindingPredicate
    op_id: str = field(default_factory=lambda: new_id("filter"))
    source: ClassVar[None] = None

    operation: ClassVar[str] = "filter"
    output_kind: ClassVar[ResultKind] = ResultKind.BINDING_SET
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
        accepted_input_kinds=frozenset((ResultKind.BINDING_SET,)),
        min_inputs=1,
        max_inputs=1,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.PURE
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        if not isinstance(self.predicate, BindingPredicate):
            raise KernelValidationError(
                "filter predicate must be a BindingPredicate"
            )
        self._validate_common()


@dataclass(frozen=True)
class Join(OperationMixin):
    """Perform a deterministic inner join over explicit binding keys."""

    inputs: Tuple[OutputRef[Any], ...]
    left_on: Tuple[str, ...]
    right_on: Tuple[str, ...]
    left_alias: str = "left"
    right_alias: str = "right"
    op_id: str = field(default_factory=lambda: new_id("join"))
    source: ClassVar[None] = None

    operation: ClassVar[str] = "join"
    output_kind: ClassVar[ResultKind] = ResultKind.BINDING_SET
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
        accepted_input_kinds=frozenset((ResultKind.BINDING_SET,)),
        min_inputs=2,
        max_inputs=2,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.PURE
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "left_on", tuple(self.left_on))
        object.__setattr__(self, "right_on", tuple(self.right_on))
        if not self.left_on or not self.right_on:
            raise KernelValidationError(
                "join requires explicit left and right keys"
            )
        if len(self.left_on) != len(self.right_on):
            raise KernelValidationError(
                "join left and right key counts must match"
            )
        for name, values in (
            ("left_on", self.left_on),
            ("right_on", self.right_on),
        ):
            if any(
                not isinstance(item, str) or not item.strip()
                for item in values
            ):
                raise KernelValidationError(
                    "join {} must contain non-empty strings".format(name)
                )
            if len(set(values)) != len(values):
                raise KernelValidationError(
                    "join {} cannot contain duplicates".format(name)
                )
        for name in ("left_alias", "right_alias"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "join {} must be non-empty".format(name)
                )
            if "." in value:
                raise KernelValidationError(
                    "join aliases cannot contain dots"
                )
        if self.left_alias == self.right_alias:
            raise KernelValidationError(
                "join aliases must be distinct"
            )
        self._validate_common()


@dataclass(frozen=True)
class Fuse(OperationMixin):
    """Rank and deduplicate normalized evidence across upstream results."""

    inputs: Tuple[OutputRef[Any], ...]
    strategy: str = "rrf"
    limit: int = 20
    rank_constant: int = 60
    op_id: str = field(default_factory=lambda: new_id("fuse"))
    source: ClassVar[None] = None

    operation: ClassVar[str] = "fuse"
    output_kind: ClassVar[ResultKind] = ResultKind.EVIDENCE_SET
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
        accepted_input_kinds=_EVIDENCE_CAPABLE_RESULT_KINDS,
        min_inputs=1,
        max_inputs=None,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.PURE
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        if self.strategy != "rrf":
            raise KernelValidationError(
                "fuse currently supports only the 'rrf' strategy"
            )
        for name in ("limit", "rank_constant"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise KernelValidationError(
                    "fuse {} must be an integer".format(name)
                )
            if value <= 0:
                raise KernelValidationError(
                    "fuse {} must be positive".format(name)
                )
        self._validate_common()


InitialDataOp = Union[
    Discover,
    Describe,
    Search,
    Query,
    Recall,
    ProposeMutation,
    ApplyMutation,
    Project,
    Filter,
    Join,
    Fuse,
    Compose,
]
INITIAL_DATA_OP_TYPES: Tuple[Type[OperationMixin], ...] = (
    Discover,
    Describe,
    Search,
    Query,
    Recall,
    ProposeMutation,
    ApplyMutation,
    Project,
    Filter,
    Join,
    Fuse,
    Compose,
)

__all__ = [
    "ApplyMutation",
    "BindingPredicate",
    "ComparisonOperator",
    "Compose",
    "Describe",
    "Discover",
    "Filter",
    "Fuse",
    "INITIAL_DATA_OP_TYPES",
    "InitialDataOp",
    "Join",
    "Project",
    "ProposeMutation",
    "Query",
    "Recall",
    "Search",
]
