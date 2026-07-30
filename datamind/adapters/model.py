"""Reference adapters for the provider-neutral structured ModelPort."""
from __future__ import annotations

import time
from typing import Any, Iterable, List, Mapping, Union

from datamind.kernel import KernelValidationError, Usage, thaw_json
from datamind.ports import (
    ModelInvocationError,
    ModelOutputError,
    StructuredModelRequest,
    StructuredModelResponse,
)


class AnthropicStructuredModel:
    """Submit a forced, strict tool call through an Anthropic-style client."""

    def __init__(self, client: Any, *, model: str) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        messages = getattr(client, "messages", None)
        if messages is None or not callable(
            getattr(messages, "create", None)
        ):
            raise ValueError(
                "client must expose async messages.create()"
            )
        self._client = client
        self._model = model

    async def generate_structured(
        self,
        request: StructuredModelRequest,
    ) -> StructuredModelResponse:
        started = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=request.max_output_tokens,
                temperature=request.temperature,
                system=request.instruction,
                messages=[
                    {
                        "role": "user",
                        "content": request.input_text,
                    }
                ],
                tools=[
                    {
                        "name": request.schema_name,
                        "description": (
                            "Return the verified structured result."
                        ),
                        "input_schema": thaw_json(
                            request.output_schema
                        ),
                        "strict": True,
                    }
                ],
                tool_choice={
                    "type": "tool",
                    "name": request.schema_name,
                    "disable_parallel_tool_use": True,
                },
            )
        except Exception as exc:
            raise ModelInvocationError(
                "structured model request failed: {}".format(exc)
            ) from exc

        outputs = []
        for block in getattr(response, "content", ()):
            block_type = (
                block.get("type")
                if isinstance(block, Mapping)
                else getattr(block, "type", None)
            )
            if block_type != "tool_use":
                continue
            name = (
                block.get("name")
                if isinstance(block, Mapping)
                else getattr(block, "name", None)
            )
            if name != request.schema_name:
                continue
            value = (
                block.get("input")
                if isinstance(block, Mapping)
                else getattr(block, "input", None)
            )
            outputs.append(value)
        provider_usage = getattr(response, "usage", None)
        input_tokens = int(
            getattr(provider_usage, "input_tokens", 0) or 0
        )
        output_tokens = int(
            getattr(provider_usage, "output_tokens", 0) or 0
        )
        observed_usage = Usage(
            tokens=input_tokens + output_tokens,
            latency_ms=max(
                0,
                int((time.monotonic() - started) * 1000),
            ),
        )
        if len(outputs) != 1 or not isinstance(outputs[0], Mapping):
            raise ModelOutputError(
                "expected exactly one {!r} structured tool call".format(
                    request.schema_name
                ),
                model=str(getattr(response, "model", self._model)),
                response_id=getattr(response, "id", None),
                usage=observed_usage,
            )
        try:
            return StructuredModelResponse(
                output=outputs[0],
                model=str(getattr(response, "model", self._model)),
                response_id=getattr(response, "id", None),
                usage=observed_usage,
            )
        except KernelValidationError as exc:
            raise ModelOutputError(
                "provider output was not a JSON-compatible object",
                model=str(getattr(response, "model", self._model)),
                response_id=getattr(response, "id", None),
                usage=observed_usage,
            ) from exc


ScriptedItem = Union[StructuredModelResponse, Exception]


class ScriptedModel:
    """Deterministic model adapter for tests, examples, and benchmarks."""

    def __init__(self, responses: Iterable[ScriptedItem]) -> None:
        self._responses: List[ScriptedItem] = list(responses)
        self.requests: List[StructuredModelRequest] = []

    async def generate_structured(
        self,
        request: StructuredModelRequest,
    ) -> StructuredModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise ModelInvocationError(
                "scripted model has no remaining response"
            )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def remaining(self) -> int:
        return len(self._responses)


__all__ = [
    "AnthropicStructuredModel",
    "ScriptedItem",
    "ScriptedModel",
]
