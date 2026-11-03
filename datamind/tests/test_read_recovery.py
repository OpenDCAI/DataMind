"""Degradation must preserve scope/version and must not hide deterministic errors."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from datamind.capabilities.kb.providers.hybrid_retriever import HybridRetriever
from datamind.capabilities.kb.tools import build_kb_tools
from datamind.core.context import RequestContext
from datamind.core.errors import ExternalServiceError
from datamind.core.logging import bind_context
from datamind.core.recovery import recover_read, recovery_summary


class Embedding:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def embed_query(self, query):
        self.calls += 1
        if self.error:
            raise self.error
        return [1., 0.]


class Store:
    def __init__(self, error=None):
        self.error = error
        self.versions = []
        self.rows = {
            1: [("old", "needle old value", {"tenant": "a", "source": "a.md"})],
            2: [("new", "needle new value", {"tenant": "a", "source": "a.md"})],
        }
        for rows in self.rows.values():
            rows.extend((str(i), f"filler unrelated document {i}", {"tenant": "b"}) for i in range(5))

    async def get_all_texts(self, version=None):
        self.versions.append(version)
        return self.rows[version or 2]

    async def query(self, vec, **kwargs):
        if self.error:
            raise self.error
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["embedding", "vector"])
async def test_transient_failure_falls_back_after_bounded_retries(stage):
    embed = Embedding(TimeoutError() if stage == "embedding" else None)
    store = Store(TimeoutError() if stage == "vector" else None)
    retriever = HybridRetriever(vector_store=store, embedding=embed,
        vector_failure_fallback="bm25", vector_retries=1)
    ctx = RequestContext.new()
    with bind_context(ctx):
        hits = await retriever.aretrieve("needle", filters={"tenant": "a"})
    assert [h.id for h in hits] == ["new"]
    assert hits[0].metadata["degraded"] is True
    assert embed.calls == 2
    summary = recovery_summary(ctx.extra["recovery_events"])
    assert summary["retries"] == 1 and summary["fallbacks"] == 1
    assert summary["events"][0]["component"] == f"kb.{'embedding' if stage == 'embedding' else 'vector_query'}"


@pytest.mark.asyncio
async def test_historical_fallback_never_reads_current_lexical_data():
    store = Store()
    retriever = HybridRetriever(vector_store=store, embedding=Embedding(TimeoutError()),
                                vector_failure_fallback="bm25")
    old, new = await asyncio.gather(
        retriever.aretrieve_at_version("needle", version=1),
        retriever.aretrieve_at_version("needle", version=2),
    )
    assert old[0].id == "old" and new[0].id == "new"
    assert sorted(store.versions) == [1, 2]
    assert await retriever.aretrieve_at_version("needle", version=1, filters={"tenant": "b"}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    ValueError("bad input"), ExternalServiceError("embedding", "auth", status_code=401),
    ExternalServiceError("embedding", "malformed response"), FileNotFoundError("missing index"),
])
async def test_deterministic_errors_are_not_retried_or_hidden(error):
    embed = Embedding(error)
    retriever = HybridRetriever(vector_store=Store(), embedding=embed,
                                vector_failure_fallback="bm25", vector_retries=2)
    with pytest.raises(type(error)):
        await retriever.aretrieve("needle")
    assert embed.calls == 1


@pytest.mark.asyncio
async def test_disabled_fallback_propagates_and_nested_retry_budget_is_shared():
    embed = Embedding(TimeoutError())
    retriever = HybridRetriever(vector_store=Store(), embedding=embed, vector_retries=3)
    ctx = RequestContext.new()
    with bind_context(ctx), pytest.raises(TimeoutError):
        await recover_read(lambda: retriever.aretrieve("needle"), component="kb_search", retries=1)
    assert embed.calls == 2  # Not (outer retries + 1) * (inner retries + 1).
    assert recovery_summary(ctx.extra["recovery_events"])["retries"] == 1


@pytest.mark.asyncio
async def test_successful_empty_vector_result_is_not_a_fault():
    retriever = HybridRetriever(vector_store=Store(), embedding=Embedding(), vector_failure_fallback="bm25")
    hits = await retriever.aretrieve("needle")
    assert hits and "degraded" not in hits[0].metadata


@pytest.mark.asyncio
async def test_empty_fallback_is_observable_in_tool_result():
    retriever = HybridRetriever(vector_store=Store(), embedding=Embedding(TimeoutError()),
                                vector_failure_fallback="bm25")

    async def search(query, **kwargs):
        return [h.model_dump() for h in await retriever.aretrieve(query, **kwargs)]

    tool = next(t for t in build_kb_tools(SimpleNamespace(
        search=search, data_dir=".", vector_store=SimpleNamespace(name="test"),
    )) if t.name == "kb_search")
    result = await tool.handler(query="no_matching_term")
    assert result["results"] == []
    assert result["retrieval"]["degraded"] is True
    assert result["retrieval"]["events"][0]["method"] == "bm25"


@pytest.mark.asyncio
async def test_unavailable_lexical_source_is_not_silently_empty():
    store = Store()
    async def unavailable(**kwargs):
        raise TimeoutError("text storage offline")
    store.get_all_texts = unavailable
    retriever = HybridRetriever(vector_store=store, embedding=Embedding(TimeoutError()),
                                vector_failure_fallback="bm25")
    with pytest.raises(TimeoutError):
        await retriever.aretrieve("needle")


@pytest.mark.asyncio
async def test_attempt_timeout_can_fallback_but_query_deadline_cannot():
    embed = Embedding()
    async def slow(query):
        await asyncio.sleep(.1)
        return [1., 0.]
    embed.embed_query = slow
    retriever = HybridRetriever(vector_store=Store(), embedding=embed,
                                vector_failure_fallback="bm25", vector_timeout_s=.005)
    assert (await retriever.aretrieve("needle"))[0].metadata["degraded"]
    ctx = RequestContext.new()
    ctx.extra["query_deadline"] = time.monotonic() + .005
    with bind_context(ctx), pytest.raises(TimeoutError):
        await retriever.aretrieve("needle")
    assert not recovery_summary(ctx.extra["recovery_events"])["degraded"]


@pytest.mark.asyncio
async def test_cancellation_never_triggers_fallback():
    embed = Embedding(asyncio.CancelledError())
    retriever = HybridRetriever(vector_store=Store(), embedding=embed, vector_failure_fallback="bm25")
    with pytest.raises(asyncio.CancelledError):
        await retriever.aretrieve("needle")


@pytest.mark.asyncio
async def test_request_recovery_records_do_not_leak():
    retriever = HybridRetriever(vector_store=Store(), embedding=Embedding(TimeoutError()),
                                vector_failure_fallback="bm25")
    async def run():
        ctx = RequestContext.new()
        with bind_context(ctx):
            await retriever.aretrieve("needle")
        return ctx.extra["recovery_events"]
    first, second = await asyncio.gather(run(), run())
    assert first is not second and len(first) == len(second) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 1])
async def test_http_provider_does_not_multiply_outer_retry_budget(retries):
    import httpx
    from datamind.capabilities.embedding.providers.openai_compatible import OpenAICompatibleEmbedding
    calls = 0
    def response(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="offline")
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        embed = OpenAICompatibleEmbedding(api_key="test", model="test", dimension=2,
                                         max_retries=3, client=client)
        with pytest.raises(ExternalServiceError):
            await recover_read(lambda: embed.embed_query("query"), component="test",
                               retries=retries, manage_provider_retries=True)
    assert calls == retries + 1


@pytest.mark.asyncio
async def test_unmanaged_http_provider_retains_existing_retry_behavior_and_usage():
    import httpx
    from datamind.capabilities.embedding.providers.openai_compatible import OpenAICompatibleEmbedding
    calls = 0
    def response(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="offline")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1., 0.]}],
                                        "usage": {"total_tokens": 7}})
    ctx = RequestContext.new()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        embed = OpenAICompatibleEmbedding(api_key="test", model="test", dimension=2,
            max_retries=1, backoff_base_s=.001, client=client)
        with bind_context(ctx):
            assert await embed.embed_query("query") == [1., 0.]
    assert calls == 2
    assert ctx.extra["embedding_usage"] == [{"model": "test", "total_tokens": 7}]
