"""Stable contracts between Engine and the intelligence layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Tuple

from datamind.dataops import DataPlan
from datamind.kernel import (
    Budget,
    EffectLevel,
    ExecutionFailure,
    KernelValidationError,
    ScopeRef,
    SourceDescriptor,
    Usage,
)


@dataclass(frozen=True)
class ReplanningContext:
    """Trusted facts supplied for one complete replacement plan."""

    attempt_number: int
    previous_plan: DataPlan
    failure: ExecutionFailure

    def __post_init__(self) -> None:
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number < 2
        ):
            raise KernelValidationError(
                "replanning attempt_number must be at least two"
            )
        if not isinstance(self.previous_plan, DataPlan):
            raise KernelValidationError(
                "replanning previous_plan must be a DataPlan"
            )
        if not isinstance(self.failure, ExecutionFailure):
            raise KernelValidationError(
                "replanning failure must be an ExecutionFailure"
            )
        if not self.failure.recoverable:
            raise KernelValidationError(
                "replanning requires a recoverable execution failure"
            )


@dataclass(frozen=True)
class PlanningRequest:
    """Authorized, content-minimal input to natural-language compilation."""

    intent: str
    request_id: str
    sources: Tuple[SourceDescriptor, ...]
    readable_scopes: Tuple[ScopeRef, ...] = ()
    writable_scopes: Tuple[ScopeRef, ...] = ()
    max_effect: EffectLevel = EffectLevel.READ
    budget: Budget = field(default_factory=Budget)
    replanning: Optional[ReplanningContext] = None

    def __post_init__(self) -> None:
        for name in ("intent", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "{} must be a non-empty string".format(name)
                )
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(
            self,
            "readable_scopes",
            tuple(self.readable_scopes),
        )
        object.__setattr__(
            self,
            "writable_scopes",
            tuple(self.writable_scopes),
        )
        if any(
            not isinstance(item, SourceDescriptor)
            for item in self.sources
        ):
            raise KernelValidationError(
                "planning sources must contain SourceDescriptor values"
            )
        source_ids = tuple(item.ref.source_id for item in self.sources)
        if len(set(source_ids)) != len(source_ids):
            raise KernelValidationError(
                "planning sources cannot contain duplicate identities"
            )
        for name in ("readable_scopes", "writable_scopes"):
            values = getattr(self, name)
            if any(not isinstance(item, ScopeRef) for item in values):
                raise KernelValidationError(
                    "{} must contain ScopeRef values".format(name)
                )
            if len(set(values)) != len(values):
                raise KernelValidationError(
                    "{} cannot contain duplicates".format(name)
                )
        if not isinstance(self.max_effect, EffectLevel):
            raise KernelValidationError(
                "planning max_effect must be an EffectLevel"
            )
        if not isinstance(self.budget, Budget):
            raise KernelValidationError(
                "planning budget must be a Budget"
            )
        if (
            self.replanning is not None
            and not isinstance(self.replanning, ReplanningContext)
        ):
            raise KernelValidationError(
                "planning replanning must be a ReplanningContext"
            )


@dataclass(frozen=True)
class CompilationIssue:
    """One machine-readable compiler diagnostic."""

    code: str
    message: str
    op_id: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("code", "message"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "compilation issue {} must be non-empty".format(name)
                )
        if self.op_id is not None:
            if not isinstance(self.op_id, str) or not self.op_id.strip():
                raise KernelValidationError(
                    "compilation issue op_id must be non-empty"
                )

    def render(self) -> str:
        if self.op_id is None:
            return "{}: {}".format(self.code, self.message)
        return "{} [{}]: {}".format(self.code, self.op_id, self.message)


@dataclass(frozen=True)
class CompilationAttempt:
    """Content-safe record of one model/compiler attempt."""

    number: int
    model: str
    usage: Usage
    issues: Tuple[CompilationIssue, ...] = ()
    response_id: Optional[str] = None
    output_fingerprint: Optional[str] = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.number, bool)
            or not isinstance(self.number, int)
            or self.number <= 0
        ):
            raise KernelValidationError(
                "compilation attempt number must be positive"
            )
        if not isinstance(self.model, str) or not self.model.strip():
            raise KernelValidationError(
                "compilation attempt requires a model identity"
            )
        if not isinstance(self.usage, Usage):
            raise KernelValidationError(
                "compilation attempt usage must be Usage"
            )
        object.__setattr__(self, "issues", tuple(self.issues))
        if any(
            not isinstance(item, CompilationIssue)
            for item in self.issues
        ):
            raise KernelValidationError(
                "compilation issues must contain CompilationIssue values"
            )
        for name in ("response_id", "output_fingerprint"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise KernelValidationError(
                        "{} must be non-empty".format(name)
                    )

    @property
    def successful(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class CompiledPlan:
    """Validated plan and bounded compilation provenance."""

    plan: DataPlan
    attempts: Tuple[CompilationAttempt, ...]
    usage: Usage

    def __post_init__(self) -> None:
        if not isinstance(self.plan, DataPlan):
            raise KernelValidationError(
                "compiled plan requires a DataPlan"
            )
        object.__setattr__(self, "attempts", tuple(self.attempts))
        if not self.attempts:
            raise KernelValidationError(
                "compiled plan requires at least one attempt"
            )
        if any(
            not isinstance(item, CompilationAttempt)
            for item in self.attempts
        ):
            raise KernelValidationError(
                "compiled attempts must contain CompilationAttempt values"
            )
        if not self.attempts[-1].successful:
            raise KernelValidationError(
                "compiled plan must end in a successful attempt"
            )
        if not isinstance(self.usage, Usage):
            raise KernelValidationError(
                "compiled plan usage must be Usage"
            )


class PlanCompilerPort(Protocol):
    """Compile authorized natural language into a validated DataPlan."""

    async def compile(self, request: PlanningRequest) -> CompiledPlan:
        ...


__all__ = [
    "CompilationAttempt",
    "CompilationIssue",
    "CompiledPlan",
    "PlanCompilerPort",
    "PlanningRequest",
    "ReplanningContext",
]
