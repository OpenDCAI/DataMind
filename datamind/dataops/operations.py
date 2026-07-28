"""Initial, deliberately small DataOps instruction set."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, FrozenSet, Optional, Tuple, Type, Union

from datamind.kernel import (
    EffectLevel,
    JsonObject,
    KernelValidationError,
    MemoryKind,
    ScopeRef,
    SourceKind,
    SourceRef,
    freeze_json_object,
    new_id,
    require_aware,
)

from .base import OperationMixin, OutputRef, ResultKind


@dataclass(frozen=True)
class Discover(OperationMixin):
    """List registered sources, optionally filtered by surface kind."""

    kinds: Tuple[SourceKind, ...] = ()
    op_id: str = field(default_factory=lambda: new_id("discover"))
    inputs: Tuple[OutputRef[Any], ...] = field(default=(), init=False)
    source: ClassVar[None] = None

    operation: ClassVar[str] = "discover"
    output_kind: ClassVar[ResultKind] = ResultKind.SOURCE_LIST
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
class Compose(OperationMixin):
    """Combine prior typed results into a normalized evidence set."""

    inputs: Tuple[OutputRef[Any], ...]
    strategy: str = "evidence_union"
    op_id: str = field(default_factory=lambda: new_id("compose"))
    source: ClassVar[None] = None

    operation: ClassVar[str] = "compose"
    output_kind: ClassVar[ResultKind] = ResultKind.EVIDENCE_SET
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


InitialDataOp = Union[Discover, Describe, Search, Query, Recall, Compose]
INITIAL_DATA_OP_TYPES: Tuple[Type[OperationMixin], ...] = (
    Discover,
    Describe,
    Search,
    Query,
    Recall,
    Compose,
)

__all__ = [
    "Compose",
    "Describe",
    "Discover",
    "INITIAL_DATA_OP_TYPES",
    "InitialDataOp",
    "Query",
    "Recall",
    "Search",
]
