"""Typed memory identity, scope, time, and lineage values."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .errors import KernelValidationError
from .provenance import Provenance
from .types import JsonObject, freeze_json_object, require_aware


class ScopeKind(str, Enum):
    """Built-in scope classes; no hierarchy is implied between them."""

    SESSION = "session"
    PRINCIPAL = "principal"
    WORKSPACE = "workspace"
    ORGANIZATION = "organization"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ScopeRef:
    """Explicit identity of one independently authorized memory scope."""

    kind: ScopeKind
    scope_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScopeKind):
            raise KernelValidationError("scope kind must be a ScopeKind")
        if not isinstance(self.scope_id, str) or not self.scope_id.strip():
            raise KernelValidationError(
                "scope_id must be a non-empty string"
            )


class MemoryKind(str, Enum):
    """Semantic memory kinds; executable Skills remain a separate surface."""

    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    EXPERIENCE = "experience"
    PROCEDURE = "procedure"
    SUMMARY = "summary"

    def __str__(self) -> str:
        return self.value


class MemoryOriginChannel(str, Enum):
    """System-bound channel through which a memory entered the data plane."""

    IMPORTED = "imported"
    USER_EXPLICIT = "user_explicit"
    AGENT_INFERRED = "agent_inferred"
    TOOL_DERIVED = "tool_derived"
    POLICY_COMPACTION = "policy_compaction"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MemoryOrigin:
    """Auditable write authority without embedding principal identity."""

    channel: MemoryOriginChannel = MemoryOriginChannel.IMPORTED
    trace_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.channel, MemoryOriginChannel):
            raise KernelValidationError(
                "memory origin channel must be a MemoryOriginChannel"
            )
        if self.trace_id is not None:
            if (
                not isinstance(self.trace_id, str)
                or not self.trace_id.strip()
            ):
                raise KernelValidationError(
                    "memory origin trace_id must be a non-empty string"
                )
        if (
            self.channel is not MemoryOriginChannel.IMPORTED
            and self.trace_id is None
        ):
            raise KernelValidationError(
                "runtime memory origins require a trace_id"
            )


class MemoryLinkKind(str, Enum):
    """Auditable relations between memory records, not a general graph API."""

    SUPERSEDES = "supersedes"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DERIVED_FROM = "derived_from"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MemoryLink:
    kind: MemoryLinkKind
    target_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MemoryLinkKind):
            raise KernelValidationError(
                "memory link kind must be a MemoryLinkKind"
            )
        if not isinstance(self.target_id, str) or not self.target_id.strip():
            raise KernelValidationError(
                "memory link target_id must be a non-empty string"
            )


@dataclass(frozen=True)
class EvidenceRef:
    """Content-free link from a semantic memory to supporting evidence."""

    evidence_id: str
    provenance: Provenance

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence_id, str)
            or not self.evidence_id.strip()
        ):
            raise KernelValidationError(
                "evidence ref id must be a non-empty string"
            )
        if not isinstance(self.provenance, Provenance):
            raise KernelValidationError(
                "evidence ref provenance must be Provenance"
            )


@dataclass(frozen=True)
class MemoryRecord:
    """One immutable semantic assertion in a bi-temporal memory history."""

    memory_id: str
    kind: MemoryKind
    scope: ScopeRef
    content: str
    recorded_from: datetime
    recorded_to: Optional[datetime] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    origin: MemoryOrigin = field(default_factory=MemoryOrigin)
    mutation_id: Optional[str] = None
    evidence: Tuple[EvidenceRef, ...] = ()
    links: Tuple[MemoryLink, ...] = ()
    metadata: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        if not isinstance(self.memory_id, str) or not self.memory_id.strip():
            raise KernelValidationError(
                "memory_id must be a non-empty string"
            )
        if not isinstance(self.kind, MemoryKind):
            raise KernelValidationError("memory kind must be a MemoryKind")
        if not isinstance(self.scope, ScopeRef):
            raise KernelValidationError("memory scope must be a ScopeRef")
        if not isinstance(self.content, str) or not self.content.strip():
            raise KernelValidationError(
                "memory content must be a non-empty string"
            )
        if not isinstance(self.recorded_from, datetime):
            raise KernelValidationError(
                "recorded_from must be a datetime"
            )
        require_aware(self.recorded_from, "recorded_from")
        if self.recorded_to is not None:
            if not isinstance(self.recorded_to, datetime):
                raise KernelValidationError(
                    "recorded_to must be a datetime"
                )
            require_aware(self.recorded_to, "recorded_to")
            if self.recorded_from >= self.recorded_to:
                raise KernelValidationError(
                    "recorded interval must be half-open and non-empty"
                )
        for name in ("valid_from", "valid_to"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, datetime):
                    raise KernelValidationError(
                        "{} must be a datetime".format(name)
                    )
                require_aware(value, name)
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_from >= self.valid_to
        ):
            raise KernelValidationError(
                "valid interval must be half-open and non-empty"
            )
        if not isinstance(self.origin, MemoryOrigin):
            raise KernelValidationError(
                "memory origin must be a MemoryOrigin"
            )
        if self.mutation_id is not None:
            if (
                not isinstance(self.mutation_id, str)
                or not self.mutation_id.strip()
            ):
                raise KernelValidationError(
                    "memory mutation_id must be a non-empty string"
                )
        if (
            self.origin.channel is not MemoryOriginChannel.IMPORTED
            and self.mutation_id is None
        ):
            raise KernelValidationError(
                "runtime memory records require a mutation_id"
            )

        object.__setattr__(self, "evidence", tuple(self.evidence))
        if any(not isinstance(item, EvidenceRef) for item in self.evidence):
            raise KernelValidationError(
                "memory evidence must contain EvidenceRef values"
            )
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise KernelValidationError(
                "memory evidence cannot contain duplicate ids"
            )

        object.__setattr__(self, "links", tuple(self.links))
        if any(not isinstance(item, MemoryLink) for item in self.links):
            raise KernelValidationError(
                "memory links must contain MemoryLink values"
            )
        if len(set(self.links)) != len(self.links):
            raise KernelValidationError(
                "memory links cannot contain duplicates"
            )
        if any(item.target_id == self.memory_id for item in self.links):
            raise KernelValidationError("memory cannot link to itself")
        if (
            sum(
                item.kind is MemoryLinkKind.SUPERSEDES
                for item in self.links
            )
            > 1
        ):
            raise KernelValidationError(
                "memory can supersede at most one prior record"
            )
        object.__setattr__(
            self,
            "metadata",
            freeze_json_object(self.metadata),
        )

    def is_visible_at(
        self,
        *,
        valid_at: datetime,
        known_at: datetime,
    ) -> bool:
        """Return whether this assertion belongs to one bi-temporal slice."""

        if not isinstance(valid_at, datetime):
            raise KernelValidationError("valid_at must be a datetime")
        if not isinstance(known_at, datetime):
            raise KernelValidationError("known_at must be a datetime")
        require_aware(valid_at, "valid_at")
        require_aware(known_at, "known_at")
        transaction_visible = (
            self.recorded_from <= known_at
            and (
                self.recorded_to is None
                or known_at < self.recorded_to
            )
        )
        reality_visible = (
            (self.valid_from is None or self.valid_from <= valid_at)
            and (self.valid_to is None or valid_at < self.valid_to)
        )
        return transaction_visible and reality_visible


@dataclass(frozen=True)
class MemoryConflict:
    """An explicit contradiction among records returned by Recall."""

    record_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_ids", tuple(self.record_ids))
        if len(self.record_ids) < 2:
            raise KernelValidationError(
                "memory conflict requires at least two records"
            )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.record_ids
        ):
            raise KernelValidationError(
                "memory conflict ids must be non-empty strings"
            )
        if len(set(self.record_ids)) != len(self.record_ids):
            raise KernelValidationError(
                "memory conflict ids must be unique"
            )


__all__ = [
    "EvidenceRef",
    "MemoryConflict",
    "MemoryKind",
    "MemoryLink",
    "MemoryLinkKind",
    "MemoryOrigin",
    "MemoryOriginChannel",
    "MemoryRecord",
    "ScopeKind",
    "ScopeRef",
]
