"""Dependency-free domain kernel for DataMind Core 1.0."""
from .budget import Budget, Usage
from .context import ExecutionContext
from .effects import (
    EffectLevel,
    EffectSpec,
    effect_violations,
    require_effect_allowed,
)
from .errors import (
    BudgetExceeded,
    EffectPolicyError,
    ExecutionError,
    KernelError,
    KernelValidationError,
    PlanValidationError,
    ReplayError,
    SerializationError,
    SourceExecutionError,
    TraceConflictError,
    TraceError,
    TraceNotFoundError,
)
from .provenance import Provenance
from .source import SourceDescriptor
from .trace import ExecutionTrace, TraceEvent, TraceEventKind
from .types import (
    JsonObject,
    JsonScalar,
    JsonValue,
    SnapshotRef,
    SourceKind,
    SourceRef,
    freeze_json,
    freeze_json_object,
    new_id,
    thaw_json,
    utc_now,
)

__all__ = [
    "Budget",
    "BudgetExceeded",
    "EffectLevel",
    "EffectPolicyError",
    "EffectSpec",
    "ExecutionContext",
    "ExecutionError",
    "ExecutionTrace",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "KernelError",
    "KernelValidationError",
    "PlanValidationError",
    "Provenance",
    "ReplayError",
    "SerializationError",
    "SnapshotRef",
    "SourceDescriptor",
    "SourceExecutionError",
    "SourceKind",
    "SourceRef",
    "TraceConflictError",
    "TraceError",
    "TraceEvent",
    "TraceEventKind",
    "TraceNotFoundError",
    "Usage",
    "effect_violations",
    "freeze_json",
    "freeze_json_object",
    "new_id",
    "require_effect_allowed",
    "thaw_json",
    "utc_now",
]
