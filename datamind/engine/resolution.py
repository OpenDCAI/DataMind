"""Public result of compile-then-execute request resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from datamind.dataops import (
    ContextPack,
    DataPlan,
    ResultEnvelope,
)
from datamind.kernel import (
    ExecutionFailure,
    KernelValidationError,
    MemoryMutationProposal,
    Usage,
)
from datamind.ports import CompilationAttempt


@dataclass(frozen=True)
class PlanAttempt:
    """One compiled plan, its child Trace, usage, and optional failure."""

    number: int
    trace_id: str
    plan: DataPlan
    compilation_attempts: Tuple[CompilationAttempt, ...]
    compilation_usage: Usage
    execution_usage: Usage
    failure: Optional[ExecutionFailure] = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.number, bool)
            or not isinstance(self.number, int)
            or self.number <= 0
        ):
            raise KernelValidationError(
                "plan attempt number must be positive"
            )
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise KernelValidationError(
                "plan attempt trace_id must be non-empty"
            )
        if not isinstance(self.plan, DataPlan):
            raise KernelValidationError(
                "plan attempt requires a DataPlan"
            )
        object.__setattr__(
            self,
            "compilation_attempts",
            tuple(self.compilation_attempts),
        )
        if not self.compilation_attempts:
            raise KernelValidationError(
                "plan attempt requires compilation attempts"
            )
        if any(
            not isinstance(item, CompilationAttempt)
            for item in self.compilation_attempts
        ):
            raise KernelValidationError(
                "plan attempt compilation_attempts are invalid"
            )
        if not self.compilation_attempts[-1].successful:
            raise KernelValidationError(
                "plan attempt compilation must end successfully"
            )
        for name in ("compilation_usage", "execution_usage"):
            if not isinstance(getattr(self, name), Usage):
                raise KernelValidationError(
                    "plan attempt {} must be Usage".format(name)
                )
        observed_compilation_usage = Usage()
        for item in self.compilation_attempts:
            observed_compilation_usage = (
                observed_compilation_usage + item.usage
            )
        if self.compilation_usage != observed_compilation_usage:
            raise KernelValidationError(
                "plan attempt compilation usage must equal its attempts"
            )
        if self.failure is not None:
            if not isinstance(self.failure, ExecutionFailure):
                raise KernelValidationError(
                    "plan attempt failure must be ExecutionFailure"
                )
            if self.failure.usage != self.execution_usage:
                raise KernelValidationError(
                    "plan attempt failure usage must match execution usage"
                )

    @property
    def successful(self) -> bool:
        return self.failure is None

    @property
    def usage(self) -> Usage:
        return self.compilation_usage + self.execution_usage


@dataclass(frozen=True)
class Resolution:
    """Final result plus every bounded plan attempt under one parent id."""

    resolution_id: str
    request_id: str
    plan_attempts: Tuple[PlanAttempt, ...]
    result: ResultEnvelope[Any]
    usage: Usage
    proposed_mutations: Tuple[MemoryMutationProposal, ...] = field(
        default=()
    )

    def __post_init__(self) -> None:
        for name in ("resolution_id", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "resolution {} must be non-empty".format(name)
                )
        object.__setattr__(
            self,
            "plan_attempts",
            tuple(self.plan_attempts),
        )
        if not self.plan_attempts or len(self.plan_attempts) > 2:
            raise KernelValidationError(
                "resolution requires one or two plan attempts"
            )
        if any(
            not isinstance(item, PlanAttempt)
            for item in self.plan_attempts
        ):
            raise KernelValidationError(
                "resolution plan_attempts must contain PlanAttempt values"
            )
        expected_numbers = tuple(range(1, len(self.plan_attempts) + 1))
        if tuple(item.number for item in self.plan_attempts) != expected_numbers:
            raise KernelValidationError(
                "resolution plan attempt numbers must be contiguous"
            )
        if any(item.successful for item in self.plan_attempts[:-1]):
            raise KernelValidationError(
                "only the final resolution plan attempt may succeed"
            )
        if any(
            not item.failure.recoverable
            for item in self.plan_attempts[:-1]
            if item.failure is not None
        ):
            raise KernelValidationError(
                "only recoverable failures may precede another plan attempt"
            )
        if not self.plan_attempts[-1].successful:
            raise KernelValidationError(
                "successful resolution requires a successful final attempt"
            )
        if not isinstance(self.result, ResultEnvelope):
            raise KernelValidationError(
                "resolution result must be a ResultEnvelope"
            )
        if self.result.trace_id != self.plan_attempts[-1].trace_id:
            raise KernelValidationError(
                "resolution result must belong to the final child trace"
            )
        if self.result.usage != self.plan_attempts[-1].execution_usage:
            raise KernelValidationError(
                "resolution result usage must match the final plan attempt"
            )
        if not isinstance(self.usage, Usage):
            raise KernelValidationError(
                "resolution usage must be Usage"
            )
        observed_usage = Usage()
        for item in self.plan_attempts:
            observed_usage = observed_usage + item.usage
        if self.usage != observed_usage:
            raise KernelValidationError(
                "resolution usage must equal all plan-attempt usage"
            )
        object.__setattr__(
            self,
            "proposed_mutations",
            tuple(self.proposed_mutations),
        )
        if any(
            not isinstance(item, MemoryMutationProposal)
            for item in self.proposed_mutations
        ):
            raise KernelValidationError(
                "resolution proposals must contain "
                "MemoryMutationProposal values"
            )

    @property
    def final_attempt(self) -> PlanAttempt:
        return self.plan_attempts[-1]

    @property
    def plan(self) -> DataPlan:
        return self.final_attempt.plan

    @property
    def compilation_attempts(self) -> Tuple[CompilationAttempt, ...]:
        return tuple(
            compiler_attempt
            for plan_attempt in self.plan_attempts
            for compiler_attempt in plan_attempt.compilation_attempts
        )

    @property
    def compilation_usage(self) -> Usage:
        total = Usage()
        for item in self.plan_attempts:
            total = total + item.compilation_usage
        return total


def collect_proposed_mutations(
    value: Any,
) -> Tuple[MemoryMutationProposal, ...]:
    """Collect explicit proposals from a final native result structure."""

    found = []
    seen = set()

    def visit(item: Any) -> None:
        if isinstance(item, MemoryMutationProposal):
            if item.proposal_id not in seen:
                seen.add(item.proposal_id)
                found.append(item)
            return
        if isinstance(item, ContextPack):
            for context_item in item.items:
                visit(context_item.value)
            return
        if isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, (tuple, list)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(found)


__all__ = ["PlanAttempt", "Resolution", "collect_proposed_mutations"]
