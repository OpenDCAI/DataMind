"""Strict validation for OpenAI-compatible embedding response rows."""
from __future__ import annotations

import json

import httpx
import pytest

from datamind.capabilities.embedding.providers.openai_compatible import (
    OpenAICompatibleEmbedding,
)
from datamind.core.errors import ExternalServiceError


def _client(body, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            content=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "indices",
    [
        [0.9, 1.9],       # fractional values must not be truncated
        [True, 1],        # bool is an int subclass in Python, not a JSON index
        ["0", 1],         # numeric strings are not the documented JSON shape
        [-1, 0],
        [0, 0],
        [0, 2],
    ],
)
async def test_rejects_malformed_response_indices_without_retry(indices):
    calls = []
    body = {
        "data": [
            {"index": indices[0], "embedding": [1.0, 2.0]},
            {"index": indices[1], "embedding": [3.0, 4.0]},
        ],
    }
    client = _client(body, calls)
    embedding = OpenAICompatibleEmbedding(
        api_key="test", model="custom", dimension=2,
        client=client, max_retries=5,
    )
    with pytest.raises(ExternalServiceError):
        await embedding.embed_texts(["a", "b"])
    assert len(calls) == 1
    await embedding.aclose()


@pytest.mark.asyncio
async def test_rejects_boolean_vector_coordinates():
    calls = []
    client = _client(
        {"data": [{"index": 0, "embedding": [True, 0.0]}]}, calls,
    )
    embedding = OpenAICompatibleEmbedding(
        api_key="test", model="custom", dimension=2,
        client=client, max_retries=0,
    )
    with pytest.raises(ExternalServiceError):
        await embedding.embed_texts(["a"])
    await embedding.aclose()


@pytest.mark.asyncio
async def test_reorders_valid_integer_indices():
    calls = []
    client = _client(
        {"data": [
            {"index": 1, "embedding": [3.0, 4.0]},
            {"index": 0, "embedding": [1.0, 2.0]},
        ]},
        calls,
    )
    embedding = OpenAICompatibleEmbedding(
        api_key="test", model="custom", dimension=2,
        client=client,
    )
    assert await embedding.embed_texts(["a", "b"]) == [[1.0, 2.0], [3.0, 4.0]]
    await embedding.aclose()
