"""Static effect semantics for reads, governed writes, and destructive work."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import FrozenSet, Optional, Tuple

from .errors import EffectPolicyError, KernelValidationError
from .types import SourceRef


class EffectLevel(IntEnum):
    PURE = 0
    READ = 10
    INTERNAL_WRITE = 20
    EXTERNAL_WRITE = 30
    DESTRUCTIVE = 40


@dataclass(frozen=True)
class EffectSpec:
    """Risk and recovery properties declared by a DataOp."""

    level: EffectLevel
    resource: Optional[SourceRef] = None
    reversible: bool = True
    requires_approval: bool = False
    approval_key: Optional[str] = None
    idempotency_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.level, EffectLevel):
            raise KernelValidationError("effect level must be an EffectLevel")
        if self.resource is not None and not isinstance(self.resource, SourceRef):
            raise KernelValidationError("effect resource must be a SourceRef")
        for name in ("reversible", "requires_approval"):
            if not isinstance(getattr(self, name), bool):
                raise KernelValidationError(
                    "{} must be a boolean".format(name)
                )
        for name in ("approval_key", "idempotency_key"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str):
                    raise KernelValidationError(
                        "{} must be a string".format(name)
                    )
                if not value.strip():
                    raise KernelValidationError(
                        "{} cannot be blank".format(name)
                    )
        if self.level is EffectLevel.DESTRUCTIVE and not self.requires_approval:
            raise KernelValidationError(
                "destructive effects must explicitly require approval"
            )
        if self.requires_approval and not self.approval_key:
            raise KernelValidationError(
                "approval_key is required when requires_approval is true"
            )

    @property
    def is_write(self) -> bool:
        return self.level >= EffectLevel.INTERNAL_WRITE


def effect_violations(
    effect: EffectSpec,
    *,
    max_level: EffectLevel,
    approvals: FrozenSet[str] = frozenset(),
    allowed_resources: FrozenSet[str] = frozenset(),
) -> Tuple[str, ...]:
    """Return deterministic preflight violations for an effect."""

    violations = []
    if effect.level > max_level:
        violations.append(
            "effect {} exceeds allowed level {}".format(
                effect.level.name, max_level.name
            )
        )
    if (
        allowed_resources
        and effect.resource is not None
        and effect.resource.source_id not in allowed_resources
    ):
        violations.append(
            "resource {!r} is not allowed".format(effect.resource.source_id)
        )
    if (
        effect.level >= EffectLevel.EXTERNAL_WRITE
        and not effect.idempotency_key
    ):
        violations.append("external writes require an idempotency key")
    if (
        effect.requires_approval
        and effect.approval_key not in approvals
    ):
        violations.append(
            "missing approval {!r}".format(effect.approval_key)
        )
    return tuple(violations)


def require_effect_allowed(
    effect: EffectSpec,
    *,
    max_level: EffectLevel,
    approvals: FrozenSet[str] = frozenset(),
    allowed_resources: FrozenSet[str] = frozenset(),
) -> None:
    violations = effect_violations(
        effect,
        max_level=max_level,
        approvals=approvals,
        allowed_resources=allowed_resources,
    )
    if violations:
        raise EffectPolicyError(violations)
