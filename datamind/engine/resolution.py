"""Public result of compile-then-execute request resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple

from datamind.dataops import (
    ContextPack,
    DataPlan,
    ResultEnvelope,
)
from datamind.kernel import (
    KernelValidationError,
    MemoryMutationProposal,
    Usage,
)
from datamind.ports import CompilationAttempt


@dataclass(frozen=True)
class Resolution:
    """Validated plan, execution result, and content-safe compiler record."""

    request_id: str
    plan: DataPlan
    result: ResultEnvelope[Any]
    compilation_attempts: Tuple[CompilationAttempt, ...]
    compilation_usage: Usage
    usage: Usage
    proposed_mutations: Tuple[MemoryMutationProposal, ...] = field(
        default=()
    )

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise KernelValidationError(
                "resolution request_id must be non-empty"
            )
        if not isinstance(self.plan, DataPlan):
            raise KernelValidationError(
                "resolution plan must be a DataPlan"
            )
        if not isinstance(self.result, ResultEnvelope):
            raise KernelValidationError(
                "resolution result must be a ResultEnvelope"
            )
        object.__setattr__(
            self,
            "compilation_attempts",
            tuple(self.compilation_attempts),
        )
        if not self.compilation_attempts:
            raise KernelValidationError(
                "resolution requires compilation attempts"
            )
        if any(
            not isinstance(item, CompilationAttempt)
            for item in self.compilation_attempts
        ):
            raise KernelValidationError(
                "resolution attempts must contain CompilationAttempt values"
            )
        for name in ("compilation_usage", "usage"):
            if not isinstance(getattr(self, name), Usage):
                raise KernelValidationError(
                    "resolution {} must be Usage".format(name)
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


__all__ = ["Resolution", "collect_proposed_mutations"]
