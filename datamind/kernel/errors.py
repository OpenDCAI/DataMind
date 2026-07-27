"""Errors owned by the dependency-free DataMind kernel."""
from __future__ import annotations

from typing import Iterable, Tuple


class KernelError(Exception):
    """Base class for deterministic Core failures."""


class KernelValidationError(KernelError, ValueError):
    """A kernel value violates an invariant."""


class EffectPolicyError(KernelError, PermissionError):
    """An operation effect is not permitted by the execution context."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations: Tuple[str, ...] = tuple(violations)
        super().__init__("; ".join(self.violations) or "effect policy denied")


class BudgetExceeded(KernelError):
    """Observed or statically known usage exceeds its budget."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations: Tuple[str, ...] = tuple(violations)
        super().__init__("; ".join(self.violations) or "budget exceeded")


class PlanValidationError(KernelError, ValueError):
    """A DataPlan cannot be executed safely."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations: Tuple[str, ...] = tuple(violations)
        super().__init__("; ".join(self.violations) or "invalid data plan")


class SerializationError(KernelError, ValueError):
    """A versioned Core object cannot be encoded or decoded."""


class ExecutionError(KernelError):
    """Base class for future deterministic executor failures."""


class SourceExecutionError(ExecutionError):
    """A source adapter failed while executing a DataOp."""


class TraceError(KernelError):
    """Base error for trace recording and lookup."""


class TraceConflictError(TraceError):
    """A trace or artifact identity has already been recorded."""


class TraceNotFoundError(TraceError, LookupError):
    """A requested trace or replay artifact does not exist."""


class ReplayError(TraceError):
    """A recorded execution cannot be replayed equivalently."""
