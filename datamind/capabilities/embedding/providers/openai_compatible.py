"""OpenAI-compatible embedding provider.

Works with:
- OpenAI (api.openai.com/v1)
- SiliconFlow, DeepSeek, Moonshot, 智谱 etc. — any provider that speaks the
  POST /v1/embeddings JSON shape `{"model": ..., "input": [...]}`.
- A custom gateway may expose this endpoint alongside its LLM endpoint so one
  deployment can drive both LLM and embeddings.

We use httpx directly rather than the `openai` package: fewer deps, easier
to retry, and the Anthropic-style gateway sometimes has quirks around error
shapes that are simpler to handle raw.
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import Any, Sequence

import httpx

from datamind.core.errors import ExternalServiceError
from datamind.core.logging import get_logger
from datamind.core.registry import embedding_registry

_log = get_logger("embedding.openai_compatible")

# Published dimensions for the most common models. If the runtime sees a
# different length we trust the server and update.
_KNOWN_DIMS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "BAAI/bge-large-zh-v1.5": 1024,
    "BAAI/bge-m3": 1024,
}


@embedding_registry.register("openai_compatible")
@embedding_registry.register("openai")
class OpenAICompatibleEmbedding:
    """Call any /v1/embeddings endpoint."""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_base: str = "https://api.openai.com/v1",
        dimension: int | None = None,
        batch_size: int = 32,
        timeout_s: float = 30.0,
        connect_timeout_s: float = 10.0,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
        max_batch_tokens: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        # Normalise the base URL so `/v1` always appears exactly once before
        # `/embeddings`. This lets users point at either
        # "https://api.openai.com/v1" or the gateway root "http://host:3888".
        base = api_base.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        self._api_base = base
        self._batch_size = batch_size
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._max_batch_tokens = max_batch_tokens
        # Resolve dimension: explicit arg > known default > probe on first call.
        self.dimension = dimension or _KNOWN_DIMS.get(model, 0)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OpenAICompatibleEmbedding":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # --------------------------------------------------------------- public

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        all_vecs: list[list[float]] = []
        # Batch for the API call — many providers cap input size.
        batches: list[list[str]] = []
        current: list[str] = []
        estimated_tokens = 0
        for text in texts:
            item_tokens = max(1, (len(text) + 3) // 4)
            if current and (
                len(current) >= self._batch_size
                or (
                    self._max_batch_tokens is not None
                    and estimated_tokens + item_tokens > self._max_batch_tokens
                )
            ):
                batches.append(current)
                current = []
                estimated_tokens = 0
            current.append(text)
            estimated_tokens += item_tokens
        if current:
            batches.append(current)
        for batch in batches:
            vecs = await self._call(batch)
            all_vecs.extend(vecs)
        return all_vecs

    async def embed_query(self, query: str) -> list[float]:
        vecs = await self._call([query])
        return vecs[0]

    # -------------------------------------------------------------- private

    async def _call(self, inputs: list[str]) -> list[list[float]]:
        url = f"{self._api_base}/embeddings"
        payload = {"model": self._model, "input": inputs}

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            resp: httpx.Response | None = None
            try:
                resp = await self._client.post(url, json=payload)
                if resp.status_code >= 500 or resp.status_code in {408, 409, 429}:
                    # retriable
                    raise ExternalServiceError(
                        "embedding",
                        f"HTTP {resp.status_code}: {resp.text[:200]}",
                        status_code=resp.status_code,
                    )
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data") or []
                if not data:
                    raise ExternalServiceError(
                        "embedding",
                        f"empty data in response: {body!r}",
                    )
                if len(data) != len(inputs):
                    raise ExternalServiceError(
                        "embedding",
                        f"response count mismatch: expected {len(inputs)}, got {len(data)}",
                    )
                try:
                    ordered = sorted(data, key=lambda row: int(row["index"]))
                    indices = [int(row["index"]) for row in ordered]
                    vecs = [list(row["embedding"]) for row in ordered]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ExternalServiceError(
                        "embedding", "response rows require integer index and embedding",
                        cause=exc,
                    ) from exc
                if indices != list(range(len(inputs))):
                    raise ExternalServiceError(
                        "embedding", f"response indices are incomplete or duplicated: {indices}",
                    )
                dimensions = {len(vec) for vec in vecs}
                if len(dimensions) != 1 or not dimensions or next(iter(dimensions)) <= 0:
                    raise ExternalServiceError(
                        "embedding", f"inconsistent vector dimensions: {sorted(dimensions)}",
                    )
                actual_dimension = next(iter(dimensions))
                if self.dimension and actual_dimension != self.dimension:
                    raise ExternalServiceError(
                        "embedding",
                        f"dimension mismatch for {self._model}: expected {self.dimension}, got {actual_dimension}",
                    )
                if any(
                    not isinstance(value, (int, float)) or not math.isfinite(float(value))
                    for vec in vecs for value in vec
                ):
                    raise ExternalServiceError("embedding", "response contains non-finite vector values")
                # Auto-detect dimension on first successful call.
                if not self.dimension and vecs:
                    self.dimension = actual_dimension
                    _log.info(
                        "embedding_dimension_detected",
                        extra={"model": self._model, "dim": self.dimension},
                    )
                return vecs
            except httpx.HTTPStatusError as exc:
                # Ordinary 4xx (especially 401/403) are deterministic.
                raise ExternalServiceError(
                    "embedding",
                    f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                    status_code=exc.response.status_code,
                    cause=exc,
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError, ExternalServiceError) as exc:
                last_exc = exc
                retriable = isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) or (
                    isinstance(exc, ExternalServiceError)
                    and getattr(exc, "status_code", None) in {408, 409, 429, 500, 502, 503, 504}
                )
                if not retriable:
                    raise
                if attempt < self._max_retries:
                    retry_after = None
                    if resp is not None and resp.headers.get("retry-after"):
                        try:
                            retry_after = max(0.0, float(resp.headers["retry-after"]))
                        except ValueError:
                            retry_after = None
                    delay = retry_after
                    if delay is None:
                        delay = self._backoff_base_s * (2**attempt) + random.uniform(
                            0, self._backoff_base_s
                        )
                    await asyncio.sleep(delay)
                    continue
                break

        raise ExternalServiceError(
            "embedding",
            f"{type(last_exc).__name__}: {last_exc}",
            cause=last_exc,
        )
