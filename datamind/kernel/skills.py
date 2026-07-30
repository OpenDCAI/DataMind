"""Typed, governed Skill identities and provider-independent results."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Tuple

from .effects import EffectLevel, EffectSpec
from .errors import KernelValidationError
from .types import (
    JsonObject,
    JsonValue,
    SourceRef,
    freeze_json,
    freeze_json_object,
    thaw_json,
)

_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillKind(str, Enum):
    INSTRUCTION = "instruction"
    EXECUTABLE = "executable"
    HYBRID = "hybrid"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class SkillRef:
    """Immutable identity of one exact Skill artifact."""

    name: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        for name in ("name", "version", "digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "skill {} must be a non-empty string".format(name)
                )
        if len(self.digest) != 64 or any(
            item not in "0123456789abcdef" for item in self.digest
        ):
            raise KernelValidationError(
                "skill digest must be a lowercase SHA-256 hex digest"
            )
        if (
            len(self.name) > 64
            or _SKILL_NAME_PATTERN.fullmatch(self.name) is None
        ):
            raise KernelValidationError(
                "skill name must follow the Agent Skills naming convention"
            )


@dataclass(frozen=True)
class SkillSpec:
    """Portable Skill data plus a trusted, governed execution policy."""

    name: str
    version: str
    description: str
    instructions: str
    kind: SkillKind = SkillKind.INSTRUCTION
    input_schema: JsonObject = field(
        default_factory=lambda: freeze_json_object(
            {"type": "object", "properties": {}}
        )
    )
    output_schema: JsonObject = field(
        default_factory=lambda: freeze_json_object(
            {"type": "object", "properties": {}}
        )
    )
    effect_level: EffectLevel = EffectLevel.PURE
    reversible: bool = True
    requires_approval: bool = False
    compatibility: str = ""
    allowed_tools: Tuple[str, ...] = ()
    resource_refs: Tuple[str, ...] = ()
    metadata: JsonObject = field(default_factory=freeze_json_object)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("name", "version", "description", "instructions"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "skill {} must be a non-empty string".format(name)
                )
        if (
            len(self.name) > 64
            or _SKILL_NAME_PATTERN.fullmatch(self.name) is None
        ):
            raise KernelValidationError(
                "skill name must follow the Agent Skills naming convention"
            )
        if len(self.description) > 1024:
            raise KernelValidationError(
                "skill description cannot exceed 1024 characters"
            )
        if not isinstance(self.kind, SkillKind):
            raise KernelValidationError("skill kind must be a SkillKind")
        if not isinstance(self.effect_level, EffectLevel):
            raise KernelValidationError(
                "skill effect_level must be an EffectLevel"
            )
        for name in ("reversible", "requires_approval"):
            if not isinstance(getattr(self, name), bool):
                raise KernelValidationError(
                    "skill {} must be a boolean".format(name)
                )
        if (
            self.effect_level is EffectLevel.DESTRUCTIVE
            and not self.requires_approval
        ):
            raise KernelValidationError(
                "destructive skills must require approval"
            )
        if self.kind is SkillKind.INSTRUCTION and (
            self.effect_level is not EffectLevel.PURE
            or self.requires_approval
        ):
            raise KernelValidationError(
                "instruction-only skills cannot declare execution effects"
            )
        if not isinstance(self.compatibility, str):
            raise KernelValidationError(
                "skill compatibility must be a string"
            )
        if len(self.compatibility) > 500:
            raise KernelValidationError(
                "skill compatibility cannot exceed 500 characters"
            )
        for name in ("allowed_tools", "resource_refs"):
            values = tuple(getattr(self, name))
            object.__setattr__(self, name, values)
            if any(
                not isinstance(item, str) or not item.strip()
                for item in values
            ):
                raise KernelValidationError(
                    "skill {} must contain non-empty strings".format(name)
                )
            if len(set(values)) != len(values):
                raise KernelValidationError(
                    "skill {} cannot contain duplicates".format(name)
                )
        object.__setattr__(
            self,
            "input_schema",
            freeze_json_object(self.input_schema),
        )
        object.__setattr__(
            self,
            "output_schema",
            freeze_json_object(self.output_schema),
        )
        for name in ("input_schema", "output_schema"):
            schema = getattr(self, name)
            if schema.get("type") != "object":
                raise KernelValidationError(
                    "skill {} must describe a JSON object".format(name)
                )
            properties = schema.get("properties", {})
            if not isinstance(properties, Mapping):
                raise KernelValidationError(
                    "skill {} properties must be an object".format(name)
                )
        object.__setattr__(
            self,
            "metadata",
            freeze_json_object(self.metadata),
        )
        canonical = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "instructions": self.instructions,
            "kind": self.kind.value,
            "input_schema": thaw_json(self.input_schema),
            "output_schema": thaw_json(self.output_schema),
            "effect_level": self.effect_level.name,
            "reversible": self.reversible,
            "requires_approval": self.requires_approval,
            "compatibility": self.compatibility,
            "allowed_tools": list(self.allowed_tools),
            "resource_refs": list(self.resource_refs),
            "metadata": thaw_json(self.metadata),
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        object.__setattr__(
            self,
            "digest",
            hashlib.sha256(encoded).hexdigest(),
        )

    @property
    def ref(self) -> SkillRef:
        return SkillRef(
            name=self.name,
            version=self.version,
            digest=self.digest,
        )

    @property
    def is_executable(self) -> bool:
        return self.kind in (SkillKind.EXECUTABLE, SkillKind.HYBRID)

    def invocation_effect(
        self,
        *,
        source: SourceRef,
        approval_key: str = "",
        idempotency_key: str = "",
    ) -> EffectSpec:
        """Build the operation effect from trusted registry policy."""

        return EffectSpec(
            level=self.effect_level,
            resource=source,
            reversible=self.reversible,
            requires_approval=self.requires_approval,
            approval_key=approval_key or None,
            idempotency_key=idempotency_key or None,
        )


@dataclass(frozen=True)
class SkillMatch:
    spec: SkillSpec
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.spec, SkillSpec):
            raise KernelValidationError(
                "skill match spec must be a SkillSpec"
            )
        if isinstance(self.score, bool) or not isinstance(
            self.score, (int, float)
        ):
            raise KernelValidationError(
                "skill match score must be numeric"
            )
        if self.score < 0:
            raise KernelValidationError(
                "skill match score cannot be negative"
            )
        if not math.isfinite(float(self.score)):
            raise KernelValidationError(
                "skill match score must be finite"
            )
        object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True)
class SkillResolution:
    query: str
    matches: Tuple[SkillMatch, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise KernelValidationError(
                "skill resolution query must be non-empty"
            )
        object.__setattr__(self, "matches", tuple(self.matches))
        if any(not isinstance(item, SkillMatch) for item in self.matches):
            raise KernelValidationError(
                "skill resolution matches must contain SkillMatch values"
            )
        refs = tuple(item.spec.ref for item in self.matches)
        if len(set(refs)) != len(refs):
            raise KernelValidationError(
                "skill resolution cannot contain duplicate SkillRefs"
            )


@dataclass(frozen=True)
class SkillInvocationResult:
    skill: SkillRef
    output: JsonValue
    effect_level: EffectLevel = EffectLevel.PURE
    idempotency_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.skill, SkillRef):
            raise KernelValidationError(
                "skill invocation skill must be a SkillRef"
            )
        object.__setattr__(self, "output", freeze_json(self.output))
        if not isinstance(self.effect_level, EffectLevel):
            raise KernelValidationError(
                "skill result effect_level must be an EffectLevel"
            )
        if self.idempotency_key is not None and (
            not isinstance(self.idempotency_key, str)
            or not self.idempotency_key.strip()
        ):
            raise KernelValidationError(
                "skill result idempotency_key cannot be blank"
            )


__all__ = [
    "SkillInvocationResult",
    "SkillKind",
    "SkillMatch",
    "SkillRef",
    "SkillResolution",
    "SkillSpec",
]
