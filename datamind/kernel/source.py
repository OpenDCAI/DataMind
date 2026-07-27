"""Descriptions of data sources exposed to DataPlan validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping, Optional

from .effects import EffectLevel
from .errors import KernelValidationError
from .types import JsonObject, SourceRef, freeze_json_object


@dataclass(frozen=True)
class SourceDescriptor:
    ref: SourceRef
    display_name: str
    capabilities: FrozenSet[str] = frozenset()
    max_effect: EffectLevel = EffectLevel.READ
    version: Optional[str] = None
    schema: JsonObject = field(default_factory=freeze_json_object)
    metadata: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        if not isinstance(self.ref, SourceRef):
            raise KernelValidationError("source ref must be a SourceRef")
        if not isinstance(self.display_name, str):
            raise KernelValidationError("source display_name must be a string")
        if not self.display_name.strip():
            raise KernelValidationError("source display_name must be non-empty")
        if not isinstance(self.max_effect, EffectLevel):
            raise KernelValidationError(
                "source max_effect must be an EffectLevel"
            )
        if isinstance(self.capabilities, (str, bytes)):
            raise KernelValidationError(
                "source capabilities must be a collection of operation names"
            )
        object.__setattr__(
            self,
            "capabilities",
            frozenset(str(item).strip() for item in self.capabilities),
        )
        if "" in self.capabilities:
            raise KernelValidationError("source capabilities cannot be blank")
        object.__setattr__(
            self,
            "schema",
            freeze_json_object(self.schema),
        )
        object.__setattr__(
            self,
            "metadata",
            freeze_json_object(self.metadata),
        )
        if self.version is not None:
            if not isinstance(self.version, str):
                raise KernelValidationError("source version must be a string")
            if not self.version.strip():
                raise KernelValidationError("source version cannot be blank")

    def supports(self, operation: str) -> bool:
        return operation in self.capabilities
