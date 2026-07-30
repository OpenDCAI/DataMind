"""Serializable, deliberately limited bindings from prior result fields."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from datamind.kernel import KernelValidationError

from .base import OutputRef


class BindingCardinality(str, Enum):
    SINGLE = "single"
    COLLECT = "collect"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ValueBinding:
    """Read one field from an upstream BindingSet at execution time."""

    ref: OutputRef
    field: str
    cardinality: BindingCardinality = BindingCardinality.SINGLE
    max_items: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.ref, OutputRef):
            raise KernelValidationError(
                "value binding ref must be an OutputRef"
            )
        if self.ref.path:
            raise KernelValidationError(
                "value bindings address BindingSet fields, not value paths"
            )
        if not isinstance(self.field, str) or not self.field.strip():
            raise KernelValidationError(
                "value binding field must be a non-empty string"
            )
        if not isinstance(self.cardinality, BindingCardinality):
            raise KernelValidationError(
                "value binding cardinality must be a BindingCardinality"
            )
        if isinstance(self.max_items, bool) or not isinstance(
            self.max_items, int
        ):
            raise KernelValidationError(
                "value binding max_items must be an integer"
            )
        if self.max_items <= 0:
            raise KernelValidationError(
                "value binding max_items must be positive"
            )
        if (
            self.cardinality is BindingCardinality.SINGLE
            and self.max_items != 1
        ):
            raise KernelValidationError(
                "single value bindings must use max_items=1"
            )

    @classmethod
    def single(cls, ref: OutputRef, field: str) -> "ValueBinding":
        return cls(
            ref=ref,
            field=field,
            cardinality=BindingCardinality.SINGLE,
            max_items=1,
        )

    @classmethod
    def collect(
        cls,
        ref: OutputRef,
        field: str,
        *,
        max_items: int,
    ) -> "ValueBinding":
        return cls(
            ref=ref,
            field=field,
            cardinality=BindingCardinality.COLLECT,
            max_items=max_items,
        )


@dataclass(frozen=True)
class ArgumentBinding:
    """Bind one named Skill argument from a prior BindingSet."""

    argument: str
    value: ValueBinding

    def __post_init__(self) -> None:
        if not isinstance(self.argument, str) or not self.argument.strip():
            raise KernelValidationError(
                "argument binding name must be a non-empty string"
            )
        if not isinstance(self.value, ValueBinding):
            raise KernelValidationError(
                "argument binding value must be a ValueBinding"
            )


__all__ = [
    "ArgumentBinding",
    "BindingCardinality",
    "ValueBinding",
]
