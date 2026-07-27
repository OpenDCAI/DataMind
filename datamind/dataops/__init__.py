"""Typed, serializable DataOps instruction set for DataMind Core 1.0."""
from .base import DataOp, OperationMixin, OutputRef, ResultKind
from .operations import (
    INITIAL_DATA_OP_TYPES,
    Compose,
    Describe,
    Discover,
    InitialDataOp,
    Query,
    Search,
)
from .plan import DataPlan
from .results import (
    ContextItem,
    ContextPack,
    Evidence,
    ResultEnvelope,
    ResultStatus,
)
from .serde import (
    DATA_PLAN_SCHEMA,
    DATA_PLAN_VERSION,
    operation_from_dict,
    operation_to_dict,
    plan_from_dict,
    plan_from_json,
    plan_to_dict,
    plan_to_json,
)
from .validation import (
    PlanValidationIssue,
    PlanValidationReport,
    require_valid_plan,
    validate_plan,
)

__all__ = [
    "Compose",
    "ContextItem",
    "ContextPack",
    "DATA_PLAN_SCHEMA",
    "DATA_PLAN_VERSION",
    "DataOp",
    "DataPlan",
    "Describe",
    "Discover",
    "Evidence",
    "INITIAL_DATA_OP_TYPES",
    "InitialDataOp",
    "OperationMixin",
    "OutputRef",
    "PlanValidationIssue",
    "PlanValidationReport",
    "Query",
    "ResultEnvelope",
    "ResultKind",
    "ResultStatus",
    "Search",
    "operation_from_dict",
    "operation_to_dict",
    "plan_from_dict",
    "plan_from_json",
    "plan_to_dict",
    "plan_to_json",
    "require_valid_plan",
    "validate_plan",
]
