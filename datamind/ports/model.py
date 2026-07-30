"""Provider-neutral structured model generation contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol

from datamind.kernel import (
    JsonObject,
    KernelValidationError,
    Usage,
    freeze_json_object,
)


class ModelError(Exception):
    """Base failure raised by a ModelPort implementation."""


class ModelInvocationError(ModelError):
    """The provider call could not be completed."""


class ModelOutputError(ModelError):
    """The provider returned no usable structured object."""

    def __init__(
        self,
        message: str,
        *,
        model: str = "unknown",
        usage: Usage = Usage(),
        response_id: Optional[str] = None,
    ) -> None:
        self.model = model
        self.usage = usage
        self.response_id = response_id
        super().__init__(message)


@dataclass(frozen=True)
class StructuredModelRequest:
    """One bounded request for a JSON object conforming to a schema."""

    instruction: str
    input_text: str
    output_schema: JsonObject
    schema_name: str = "structured_output"
    max_output_tokens: int = 4096
    temperature: float = 0.0

    def __post_init__(self) -> None:
        for name in ("instruction", "input_text", "schema_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "{} must be a non-empty string".format(name)
                )
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise KernelValidationError(
                "max_output_tokens must be a positive integer"
            )
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or self.temperature < 0
        ):
            raise KernelValidationError(
                "temperature must be a non-negative number"
            )
        object.__setattr__(
            self,
            "output_schema",
            freeze_json_object(self.output_schema),
        )


@dataclass(frozen=True)
class StructuredModelResponse:
    """Parsed provider output plus content-free call metadata."""

    output: JsonObject
    model: str
    usage: Usage = field(default_factory=Usage)
    response_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.output, Mapping):
            raise KernelValidationError(
                "structured model output must be a JSON object"
            )
        object.__setattr__(
            self,
            "output",
            freeze_json_object(self.output),
        )
        if not isinstance(self.model, str) or not self.model.strip():
            raise KernelValidationError(
                "structured model response requires a model identity"
            )
        if not isinstance(self.usage, Usage):
            raise KernelValidationError(
                "structured model response usage must be Usage"
            )
        if self.response_id is not None:
            if (
                not isinstance(self.response_id, str)
                or not self.response_id.strip()
            ):
                raise KernelValidationError(
                    "response_id must be a non-empty string"
                )


class ModelPort(Protocol):
    """Generate one structured object; never execute DataOps itself."""

    async def generate_structured(
        self,
        request: StructuredModelRequest,
    ) -> StructuredModelResponse:
        ...


__all__ = [
    "ModelError",
    "ModelInvocationError",
    "ModelOutputError",
    "ModelPort",
    "StructuredModelRequest",
    "StructuredModelResponse",
]
