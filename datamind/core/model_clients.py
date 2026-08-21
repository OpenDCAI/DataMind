"""Protocol-neutral model clients.

The rest of DataMind never calls ``messages.create`` or an OpenAI endpoint
directly.  Both outer tool loops and internal text generation share these
clients, which prevents a gateway from working for the agent but failing in
NL2SQL, multi-query retrieval, memory, or ingest.
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any, AsyncIterator

import httpx
from anthropic import AsyncAnthropic

from datamind.config import LLMConfig

from .errors import ConfigError, ExternalServiceError
from .logging import current_context
from .protocols import ModelResponse, ModelStreamEvent, ModelUsage


def _record_nested_usage(response: ModelResponse) -> None:
    context = current_context()
    if context is None:
        return
    context.extra.setdefault("nested_model_usage", []).append({
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "resolved_model": response.resolved_model,
    })


def _retry_after(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in content or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            out.append({"type": "text", "text": getattr(block, "text", "")})
        elif kind == "tool_use":
            out.append({
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": dict(getattr(block, "input", None) or {}),
            })
    return out


class AnthropicModelClient:
    protocol = "anthropic"

    def __init__(self, client: Any, *, default_model: str | None = None) -> None:
        self._client = client
        self._default_model = default_model
        self._closed = False

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        tool_choice: str | None = None,
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if tool_choice == "none":
            # Anthropic has no literal "none" choice. Omitting tools is the
            # portable and enforceable equivalent.
            kwargs.pop("tools", None)
        raw = await self._client.messages.create(**kwargs)
        usage = getattr(raw, "usage", None)
        return ModelResponse(
            content=_anthropic_blocks(getattr(raw, "content", [])),
            stop_reason=str(getattr(raw, "stop_reason", "end_turn")),
            usage=ModelUsage(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                cache_create_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            ),
            resolved_model=getattr(raw, "model", None) or model,
        )

    async def generate_text(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        response = await self.complete(
            model=model or self._default_model or "",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        _record_nested_usage(response)
        return "".join(
            str(block.get("text", ""))
            for block in response.content
            if block.get("type") == "text"
        ).strip()

    async def stream(self, **kwargs: Any) -> AsyncIterator[ModelStreamEvent]:
        request = dict(kwargs)
        if request.pop("tool_choice", None) == "none":
            request["tools"] = None
        request = {k: v for k, v in request.items() if v is not None}
        async with self._client.messages.stream(**request) as stream:
            async for delta in stream.text_stream:
                if delta:
                    yield ModelStreamEvent(type="text", delta=delta)
            raw = await stream.get_final_message()
        usage = getattr(raw, "usage", None)
        yield ModelStreamEvent(
            type="done",
            response=ModelResponse(
                content=_anthropic_blocks(getattr(raw, "content", [])),
                stop_reason=str(getattr(raw, "stop_reason", "end_turn")),
                usage=ModelUsage(
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                    cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                    cache_create_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
                ),
                resolved_model=getattr(raw, "model", None) or request.get("model"),
            ),
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._client, "close", None) or getattr(self._client, "aclose", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result


def _openai_messages(
    messages: list[dict[str, Any]], system: str | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            tool_calls = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                })
            item: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                item["tool_calls"] = tool_calls
            out.append(item)
            continue
        # Anthropic groups all tool results into one user message; OpenAI
        # requires one role=tool message per call.
        emitted = False
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                text_parts.append(str(block))
            elif block.get("type") == "tool_result":
                out.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": str(block.get("content", "")),
                })
                emitted = True
            elif block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
        if text_parts or not emitted:
            out.append({"role": role, "content": "".join(text_parts)})
    return out


def _openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


def _parse_tool_args(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"__datamind_parse_error__": f"malformed tool arguments: {exc.msg}"}
    if not isinstance(parsed, dict):
        return {"__datamind_parse_error__": "tool arguments must be a JSON object"}
    return parsed


class OpenAIChatCompletionsModelClient:
    protocol = "openai_chat_completions"

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        default_model: str | None = None,
        timeout_s: float = 60.0,
        connect_timeout_s: float = 10.0,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        base = api_base.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        self._url = f"{base}/chat/completions"
        self._default_model = default_model
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        self._closed = False

    def _payload(self, **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": kwargs["model"],
            "messages": _openai_messages(kwargs["messages"], kwargs.get("system")),
            "max_tokens": kwargs.get("max_tokens", 4096),
            "temperature": kwargs.get("temperature", 1.0),
        }
        tools = _openai_tools(kwargs.get("tools"))
        if tools and kwargs.get("tool_choice") != "none":
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = await self._client.post(self._url, json=payload)
                if response.status_code not in {408, 409, 429} and response.status_code < 500:
                    response.raise_for_status()
                    return response
                last = ExternalServiceError(
                    "llm", f"HTTP {response.status_code}: {response.text[:200]}",
                    status_code=response.status_code,
                )
            except httpx.HTTPStatusError as exc:
                # Authentication, malformed payloads and other ordinary 4xx
                # are deterministic and must fail fast.
                raise ExternalServiceError(
                    "llm", f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                    status_code=exc.response.status_code, cause=exc,
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
            if attempt >= self._max_retries:
                break
            delay = _retry_after(response)
            if delay is None:
                delay = self._backoff_base_s * (2**attempt) + random.uniform(0, self._backoff_base_s)
            await asyncio.sleep(delay)
        raise ExternalServiceError("llm", f"{type(last).__name__}: {last}", cause=last)

    @staticmethod
    def _response(body: dict[str, Any], requested_model: str) -> ModelResponse:
        choices = body.get("choices") or []
        if not choices:
            raise ExternalServiceError("llm", "response contained no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        blocks: list[dict[str, Any]] = []
        if message.get("content"):
            blocks.append({"type": "text", "text": str(message["content"])})
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            blocks.append({
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": str(fn.get("name") or ""),
                "input": _parse_tool_args(str(fn.get("arguments") or "")),
            })
        usage = body.get("usage") or {}
        reason = str(choice.get("finish_reason") or "stop")
        return ModelResponse(
            content=blocks,
            stop_reason="tool_use" if reason == "tool_calls" else reason,
            usage=ModelUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
            resolved_model=str(body.get("model") or requested_model),
        )

    async def complete(self, **kwargs: Any) -> ModelResponse:
        payload = self._payload(**kwargs)
        response = await self._post(payload)
        try:
            body = response.json()
        except ValueError as exc:
            raise ExternalServiceError("llm", "response was not valid JSON", cause=exc) from exc
        return self._response(body, str(kwargs["model"]))

    async def generate_text(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        response = await self.complete(
            model=model or self._default_model or "",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        _record_nested_usage(response)
        return "".join(
            str(block.get("text", ""))
            for block in response.content
            if block.get("type") == "text"
        ).strip()

    async def stream(self, **kwargs: Any) -> AsyncIterator[ModelStreamEvent]:
        payload = self._payload(**kwargs)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        content: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] = {}
        resolved_model: str | None = None
        finish_reason = "stop"
        emitted = False
        for attempt in range(self._max_retries + 1):
            retry_error: Exception | None = None
            retry_delay: float | None = None
            try:
                async with self._client.stream("POST", self._url, json=payload) as response:
                    if response.status_code >= 400:
                        raw = (await response.aread()).decode("utf-8", errors="replace")
                        error = ExternalServiceError(
                            "llm", f"HTTP {response.status_code}: {raw[:200]}",
                            status_code=response.status_code,
                        )
                        if response.status_code in {408, 409, 429} or response.status_code >= 500:
                            retry_error = error
                            retry_delay = _retry_after(response)
                        else:
                            raise error
                    else:
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError as exc:
                                raise ExternalServiceError("llm", "malformed SSE JSON", cause=exc) from exc
                            resolved_model = chunk.get("model") or resolved_model
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                            for choice in chunk.get("choices") or []:
                                finish_reason = choice.get("finish_reason") or finish_reason
                                delta = choice.get("delta") or {}
                                if delta.get("content"):
                                    text = str(delta["content"])
                                    content.append(text)
                                    emitted = True
                                    yield ModelStreamEvent(type="text", delta=text)
                                for call in delta.get("tool_calls") or []:
                                    emitted = True
                                    index = int(call.get("index") or 0)
                                    state = calls.setdefault(
                                        index, {"id": "", "name": "", "arguments": ""}
                                    )
                                    if call.get("id"):
                                        state["id"] += str(call["id"])
                                    fn = call.get("function") or {}
                                    state["name"] += str(fn.get("name") or "")
                                    state["arguments"] += str(fn.get("arguments") or "")
                        break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if emitted:
                    raise ExternalServiceError("llm", "stream interrupted after output", cause=exc) from exc
                retry_error = exc
            if retry_error is None:
                break
            if attempt >= self._max_retries:
                raise ExternalServiceError(
                    "llm", f"{type(retry_error).__name__}: {retry_error}", cause=retry_error,
                )
            if retry_delay is None:
                retry_delay = self._backoff_base_s * (2**attempt) + random.uniform(
                    0, self._backoff_base_s
                )
            await asyncio.sleep(retry_delay)
        blocks: list[dict[str, Any]] = []
        if content:
            blocks.append({"type": "text", "text": "".join(content)})
        for index in sorted(calls):
            state = calls[index]
            blocks.append({
                "type": "tool_use", "id": state["id"], "name": state["name"],
                "input": _parse_tool_args(state["arguments"]),
            })
        yield ModelStreamEvent(
            type="done",
            response=ModelResponse(
                content=blocks,
                stop_reason="tool_use" if finish_reason == "tool_calls" else str(finish_reason),
                usage=ModelUsage(
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                ),
                resolved_model=resolved_model or str(kwargs["model"]),
            ),
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()


def build_model_client(cfg: LLMConfig, *, protocol: str | None = None):
    selected = protocol or cfg.protocol
    if selected == "anthropic":
        raw = AsyncAnthropic(
            base_url=str(cfg.api_base),
            api_key=cfg.api_key.get_secret_value(),
            timeout=httpx.Timeout(cfg.timeout_s, connect=cfg.connect_timeout_s),
            max_retries=cfg.max_retries,
        )
        return AnthropicModelClient(raw, default_model=cfg.model)
    if selected == "openai_chat_completions":
        return OpenAIChatCompletionsModelClient(
            api_key=cfg.api_key.get_secret_value(),
            api_base=str(cfg.api_base),
            default_model=cfg.model,
            timeout_s=cfg.timeout_s,
            connect_timeout_s=cfg.connect_timeout_s,
            max_retries=cfg.max_retries,
            backoff_base_s=cfg.backoff_base_s,
        )
    raise ConfigError(f"Unsupported LLM protocol: {selected!r}")


__all__ = [
    "AnthropicModelClient",
    "OpenAIChatCompletionsModelClient",
    "build_model_client",
]
