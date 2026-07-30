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


class ScopePolicyError(KernelError, PermissionError):
    """A memory operation requested scopes unavailable to this context."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations: Tuple[str, ...] = tuple(violations)
        super().__init__("; ".join(self.violations) or "scope policy denied")


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


class PlanCompilationError(KernelError, ValueError):
    """A model could not produce a valid DataPlan within its attempt bound."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations: Tuple[str, ...] = tuple(violations)
        super().__init__(
            "; ".join(self.violations) or "data plan compilation failed"
        )


class UnsupportedPlanningError(KernelError):
    """A planning API was called without a configured plan compiler."""


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


class SnapshotUnavailableError(ExecutionError):
    """A source cannot serve the snapshot pinned by an execution."""


class MemoryMutationError(ExecutionError):
    """A governed Memory transition cannot be proposed or applied."""


class MemoryVersionConflictError(MemoryMutationError):
    """A Memory proposal is based on a snapshot that is no longer current."""


class MemoryIdempotencyConflictError(MemoryMutationError):
    """A Memory idempotency key was reused for different semantic intent."""


class SyncError(KernelError):
    """A versioned artifact change cannot be synchronized."""


class VersionConflictError(SyncError):
    """A change set was based on a source version that is no longer current."""


class ArtifactIntegrityError(SyncError):
    """Artifact content does not match its declared manifest."""


class ArtifactNotFoundError(SyncError, LookupError):
    """A manifest references an unavailable artifact."""


class IdempotencyConflictError(SyncError):
    """An idempotency key was reused for a different change set."""


class UnsupportedSyncError(SyncError):
    """A source or engine configuration does not support synchronization."""
