"""Content-safe external evaluation records for executions and resolutions."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional, Tuple, Union

from .errors import KernelValidationError
from .types import new_id, require_aware, utc_now

ScoreLike = Union[Decimal, int, float, str]


class OutcomeTargetKind(str, Enum):
    RESOLUTION = "resolution"
    TRACE = "trace"

    def __str__(self) -> str:
        return self.value


class EvaluatorKind(str, Enum):
    PROGRAM = "program"
    HUMAN = "human"
    MODEL = "model"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class OutcomeTarget:
    kind: OutcomeTargetKind
    target_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, OutcomeTargetKind):
            raise KernelValidationError(
                "outcome target kind must be OutcomeTargetKind"
            )
        if (
            not isinstance(self.target_id, str)
            or not self.target_id.strip()
        ):
            raise KernelValidationError(
                "outcome target_id must be non-empty"
            )


@dataclass(frozen=True)
class OutcomeAssertion:
    name: str
    passed: bool
    score: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise KernelValidationError(
                "outcome assertion name must be non-empty"
            )
        if not isinstance(self.passed, bool):
            raise KernelValidationError(
                "outcome assertion passed must be boolean"
            )
        if self.score is not None:
            try:
                score = (
                    self.score
                    if isinstance(self.score, Decimal)
                    else Decimal(str(self.score))
                )
            except (InvalidOperation, ValueError) as exc:
                raise KernelValidationError(
                    "outcome assertion score must be decimal-compatible"
                ) from exc
            if not score.is_finite() or score < 0 or score > 1:
                raise KernelValidationError(
                    "outcome assertion score must be between zero and one"
                )
            object.__setattr__(self, "score", score)


@dataclass(frozen=True)
class OutcomeRecord:
    """Append-only verdict whose payload contains no native task result."""

    target: OutcomeTarget
    task_id: str
    evaluator_kind: EvaluatorKind
    evaluator_name: str
    evaluator_version: str
    assertions: Tuple[OutcomeAssertion, ...]
    succeeded: bool
    idempotency_key: str
    outcome_id: str = field(default_factory=lambda: new_id("outcome"))
    observed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.target, OutcomeTarget):
            raise KernelValidationError(
                "outcome target must be an OutcomeTarget"
            )
        for name in (
            "task_id",
            "evaluator_name",
            "evaluator_version",
            "idempotency_key",
            "outcome_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "outcome {} must be non-empty".format(name)
                )
        if not isinstance(self.evaluator_kind, EvaluatorKind):
            raise KernelValidationError(
                "outcome evaluator_kind must be EvaluatorKind"
            )
        object.__setattr__(self, "assertions", tuple(self.assertions))
        if not self.assertions:
            raise KernelValidationError(
                "outcome requires at least one assertion"
            )
        if any(
            not isinstance(item, OutcomeAssertion)
            for item in self.assertions
        ):
            raise KernelValidationError(
                "outcome assertions must contain OutcomeAssertion values"
            )
        assertion_names = tuple(item.name for item in self.assertions)
        if len(set(assertion_names)) != len(assertion_names):
            raise KernelValidationError(
                "outcome assertion names cannot repeat"
            )
        if not isinstance(self.succeeded, bool):
            raise KernelValidationError(
                "outcome succeeded must be boolean"
            )
        if self.succeeded != all(
            item.passed for item in self.assertions
        ):
            raise KernelValidationError(
                "outcome succeeded must equal all assertion verdicts"
            )
        if not isinstance(self.observed_at, datetime):
            raise KernelValidationError(
                "outcome observed_at must be a datetime"
            )
        require_aware(self.observed_at, "outcome observed_at")

    def equivalent_to(self, other: "OutcomeRecord") -> bool:
        """Compare idempotent intent while ignoring generated identity/time."""

        if not isinstance(other, OutcomeRecord):
            return False
        return (
            self.target == other.target
            and self.task_id == other.task_id
            and self.evaluator_kind is other.evaluator_kind
            and self.evaluator_name == other.evaluator_name
            and self.evaluator_version == other.evaluator_version
            and self.assertions == other.assertions
            and self.succeeded is other.succeeded
            and self.idempotency_key == other.idempotency_key
        )


__all__ = [
    "EvaluatorKind",
    "OutcomeAssertion",
    "OutcomeRecord",
    "OutcomeTarget",
    "OutcomeTargetKind",
    "ScoreLike",
]
