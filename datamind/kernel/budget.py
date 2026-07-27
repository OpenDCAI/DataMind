"""Token, latency, monetary, and action budgets."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple, Union

from .errors import BudgetExceeded, KernelValidationError

DecimalLike = Union[Decimal, int, float, str]


def _decimal(value: DecimalLike) -> Decimal:
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise KernelValidationError(
            "cost value must be decimal-compatible"
        ) from exc
    if not converted.is_finite():
        raise KernelValidationError("cost value must be finite")
    return converted


def _require_non_negative_integer(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KernelValidationError(
            "{} must be an integer".format(field_name)
        )
    if value < 0:
        raise KernelValidationError(
            "{} cannot be negative".format(field_name)
        )


@dataclass(frozen=True)
class Usage:
    tokens: int = 0
    latency_ms: int = 0
    cost_usd: Decimal = Decimal("0")
    actions: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "cost_usd", _decimal(self.cost_usd))
        for name in ("tokens", "latency_ms", "actions"):
            _require_non_negative_integer(getattr(self, name), name)
        if self.cost_usd < 0:
            raise KernelValidationError("usage cost cannot be negative")

    def __add__(self, other: "Usage") -> "Usage":
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            tokens=self.tokens + other.tokens,
            latency_ms=self.latency_ms + other.latency_ms,
            cost_usd=self.cost_usd + other.cost_usd,
            actions=self.actions + other.actions,
        )


@dataclass(frozen=True)
class Budget:
    max_tokens: Optional[int] = None
    max_latency_ms: Optional[int] = None
    max_cost_usd: Optional[Decimal] = None
    max_actions: Optional[int] = None

    def __post_init__(self) -> None:
        if self.max_cost_usd is not None:
            object.__setattr__(self, "max_cost_usd", _decimal(self.max_cost_usd))
        for name in ("max_tokens", "max_latency_ms", "max_actions"):
            value = getattr(self, name)
            if value is not None:
                _require_non_negative_integer(value, name)
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise KernelValidationError("max_cost_usd cannot be negative")

    def violations(self, usage: Usage) -> Tuple[str, ...]:
        violations = []
        if self.max_tokens is not None and usage.tokens > self.max_tokens:
            violations.append(
                "tokens {} exceed {}".format(usage.tokens, self.max_tokens)
            )
        if (
            self.max_latency_ms is not None
            and usage.latency_ms > self.max_latency_ms
        ):
            violations.append(
                "latency_ms {} exceeds {}".format(
                    usage.latency_ms, self.max_latency_ms
                )
            )
        if self.max_cost_usd is not None and usage.cost_usd > self.max_cost_usd:
            violations.append(
                "cost_usd {} exceeds {}".format(
                    usage.cost_usd, self.max_cost_usd
                )
            )
        if self.max_actions is not None and usage.actions > self.max_actions:
            violations.append(
                "actions {} exceed {}".format(usage.actions, self.max_actions)
            )
        return tuple(violations)

    def allows(self, usage: Usage) -> bool:
        return not self.violations(usage)

    def require(self, usage: Usage) -> None:
        violations = self.violations(usage)
        if violations:
            raise BudgetExceeded(violations)
