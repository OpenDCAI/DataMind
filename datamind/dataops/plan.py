"""Immutable, versioned, bounded DataPlan."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from datamind.kernel import Budget, EffectLevel, KernelValidationError, new_id

from .base import DataOp, OutputRef


@dataclass(frozen=True)
class DataPlan:
    """A finite DAG of typed data operations."""

    operations: Tuple[DataOp[Any], ...]
    output: OutputRef[Any]
    plan_id: str = field(default_factory=lambda: new_id("plan"))
    version: str = "1"
    max_effect: EffectLevel = EffectLevel.READ
    budget: Budget = field(default_factory=Budget)
    description: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))
        if not self.operations:
            raise KernelValidationError("data plan requires at least one operation")
        if not isinstance(self.output, OutputRef):
            raise KernelValidationError("plan output must be an OutputRef")
        if not isinstance(self.plan_id, str):
            raise KernelValidationError("plan_id must be a string")
        if not self.plan_id.strip():
            raise KernelValidationError("plan_id must be non-empty")
        if not isinstance(self.version, str):
            raise KernelValidationError("plan version must be a string")
        if not self.version.strip():
            raise KernelValidationError("plan version must be non-empty")
        if not isinstance(self.max_effect, EffectLevel):
            raise KernelValidationError(
                "plan max_effect must be an EffectLevel"
            )
        if not isinstance(self.budget, Budget):
            raise KernelValidationError("plan budget must be a Budget")
        if self.description is not None:
            if not isinstance(self.description, str):
                raise KernelValidationError(
                    "plan description must be a string"
                )
            if not self.description.strip():
                raise KernelValidationError("plan description cannot be blank")

    def operation(self, op_id: str) -> DataOp[Any]:
        for op in self.operations:
            if op.op_id == op_id:
                return op
        raise KeyError(op_id)
