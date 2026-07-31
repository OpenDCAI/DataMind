"""Content-safe summaries of one failed deterministic execution attempt."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from .budget import Usage
from .errors import KernelValidationError


class ExecutionFailureKind(str, Enum):
    """Stable failure classes used by policy and bounded replanning."""

    SOURCE = "source"
    SNAPSHOT = "snapshot"
    BUDGET = "budget"
    POLICY = "policy"
    PLAN = "plan"
    DEADLINE = "deadline"
    EXECUTION = "execution"
    INFRASTRUCTURE = "infrastructure"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ExecutionFailure:
    """Sanitized failure facts; raw provider errors never cross this boundary."""

    kind: ExecutionFailureKind
    error_type: str
    error_fingerprint: str
    usage: Usage = field(default_factory=Usage)
    failed_op_id: Optional[str] = None
    operation: Optional[str] = None
    source_id: Optional[str] = None
    completed_op_ids: Tuple[str, ...] = ()
    recoverable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExecutionFailureKind):
            raise KernelValidationError(
                "execution failure kind must be ExecutionFailureKind"
            )
        for name in ("error_type", "error_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "execution failure {} must be non-empty".format(name)
                )
        for name in ("failed_op_id", "operation", "source_id"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise KernelValidationError(
                        "execution failure {} must be non-empty".format(
                            name
                        )
                    )
        if not isinstance(self.usage, Usage):
            raise KernelValidationError(
                "execution failure usage must be Usage"
            )
        object.__setattr__(
            self,
            "completed_op_ids",
            tuple(self.completed_op_ids),
        )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.completed_op_ids
        ):
            raise KernelValidationError(
                "completed_op_ids must contain non-empty strings"
            )
        if len(set(self.completed_op_ids)) != len(self.completed_op_ids):
            raise KernelValidationError(
                "completed_op_ids cannot contain duplicates"
            )
        if not isinstance(self.recoverable, bool):
            raise KernelValidationError(
                "execution failure recoverable must be boolean"
            )
        if self.recoverable and self.kind is not ExecutionFailureKind.SOURCE:
            raise KernelValidationError(
                "only source failures may be marked recoverable"
            )


__all__ = ["ExecutionFailure", "ExecutionFailureKind"]
