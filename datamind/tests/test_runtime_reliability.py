"""Regression contracts for runtime reliability and repository guarantees."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from datamind.agent import OpenAICompatibleAgentLoop, build_datamind
from datamind.agent.base import AgentLoopConfig
from datamind.capabilities.db.service import DBService
from datamind.capabilities.embedding.providers.openai_compatible import (
    OpenAICompatibleEmbedding,
)
from datamind.capabilities.graph.providers.networkx_store import NetworkXGraphStore
from datamind.capabilities.graph.service import GraphService
from datamind.capabilities.kb.providers.hybrid_retriever import HybridRetriever
from datamind.capabilities.kb.service import KBService
from datamind.config import DBConfig, RetrievalConfig, Settings
from datamind.core.errors import ConfigError, ExternalServiceError
from datamind.core.model_clients import OpenAIChatCompletionsModelClient
from datamind.core.protocols import QueryResult, RetrievedChunk, TableSchema
from datamind.core.tools import ToolRegistry, ToolSpec
from benchmark.evaluate import exact_match, load_results
from benchmark.run import _load_completed, run_benchmark


@pytest.mark.asyncio
async def test_openai_loop_and_internal_text_share_chat_completions_protocol():
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("tools"):
            return httpx.Response(200, json={
                "model": "resolved-openai-model",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call-1", "type": "function",
                            "function": {"name": "echo", "arguments": '{"value":"ok"}'},
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            })
        return httpx.Response(200, json={
            "model": "resolved-openai-model",
            "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIChatCompletionsModelClient(
        api_key="test", api_base="https://example.test",
        default_model="requested", client=http,
    )
    registry = ToolRegistry()

    async def echo(value: str) -> dict[str, str]:
        return {"echo": value}

    registry.add(ToolSpec(
        name="echo", description="echo",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=echo, metadata={"surface": "kb", "access": "read"},
    ))
    loop = OpenAICompatibleAgentLoop(
        client=client, tools=registry, config=AgentLoopConfig(model="requested"),
    )
    result = await loop.run_turn(user_message="use echo")
    generated = await client.generate_text("internal generation", model="requested")
    await client.aclose()

    assert result["answer"] == "done"
    assert result["usage"]["resolved_models"] == ["resolved-openai-model"]
    assert generated == "done"
    assert all("messages" in payload for payload in requests)
    assert any(
        any(message.get("role") == "tool" for message in payload["messages"])
        for payload in requests
    )


@pytest.mark.asyncio
async def test_openai_streaming_emits_real_deltas_and_normalized_final_response():
    chunks = [
        {
            "model": "resolved-stream-model",
            "choices": [{"finish_reason": None, "delta": {"content": "hel"}}],
        },
        {
            "model": "resolved-stream-model",
            "choices": [{"finish_reason": "stop", "delta": {"content": "lo"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
    ]
    wire = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=wire.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )
    ))
    client = OpenAIChatCompletionsModelClient(
        api_key="test", api_base="https://example.test", client=http,
    )
    events = [
        event async for event in client.stream(
            model="requested", messages=[{"role": "user", "content": "hi"}],
        )
    ]
    await client.aclose()
    assert [event.delta for event in events if event.type == "text"] == ["hel", "lo"]
    final = events[-1].response
    assert final is not None
    assert final.content == [{"type": "text", "text": "hello"}]
    assert final.resolved_model == "resolved-stream-model"


@pytest.mark.asyncio
async def test_openai_stream_retries_429_before_emitting_output():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="busy", headers={"retry-after": "0"})
        wire = (
            'data: {"model":"m","choices":[{"finish_reason":"stop",'
            '"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        )
        return httpx.Response(200, content=wire.encode(), headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIChatCompletionsModelClient(
        api_key="test", api_base="https://example.test", client=http,
        max_retries=1, backoff_base_s=0.001,
    )
    events = [event async for event in client.stream(model="m", messages=[])]
    await client.aclose()
    assert calls == 2
    assert [event.delta for event in events if event.type == "text"] == ["ok"]


@pytest.mark.asyncio
async def test_embedding_reorders_and_validates_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [
                {"index": 1, "embedding": [3.0, 4.0]},
                {"index": 0, "embedding": [1.0, 2.0]},
            ]
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedding = OpenAICompatibleEmbedding(
        api_key="test", model="custom", dimension=2, client=http,
    )
    assert await embedding.embed_texts(["a", "b"]) == [[1.0, 2.0], [3.0, 4.0]]
    await embedding.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"index": 0, "embedding": [1.0, 2.0]}]},
        {"data": [
            {"index": 0, "embedding": [1.0, 2.0]},
            {"index": 1, "embedding": [1.0, float("nan")]},
        ]},
    ],
)
async def test_embedding_rejects_count_and_non_finite_values(body):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=json.dumps(body).encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        )
    )
    embedding = OpenAICompatibleEmbedding(
        api_key="test", model="custom", dimension=2, client=http, max_retries=0,
    )
    with pytest.raises(ExternalServiceError):
        await embedding.embed_texts(["a", "b"])
    await embedding.aclose()


@pytest.mark.asyncio
async def test_embedding_auth_failure_is_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="invalid key")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedding = OpenAICompatibleEmbedding(
        api_key="bad", model="custom", dimension=2, client=http, max_retries=5,
    )
    with pytest.raises(ExternalServiceError) as error:
        await embedding.embed_query("x")
    assert error.value.status_code == 401
    assert calls == 1
    await embedding.aclose()


class _FilteredStore:
    dimension = 2

    def __init__(self):
        self.rows = [
            ("outside", "needle needle needle", {"tenant": "b"}),
            ("inside", "needle", {"tenant": "a"}),
        ]

    async def query(self, embedding, *, top_k=5, where=None):
        return [
            RetrievedChunk(id=rid, text=text, source=None, metadata=meta, score=1.0)
            for rid, text, meta in self.rows if not where or meta["tenant"] == where["tenant"]
        ][:top_k]

    async def get_all_texts(self):
        return self.rows


class _Embedding:
    name = "fake"
    dimension = 2

    async def embed_query(self, query):
        return [1.0, 0.0]

    async def embed_texts(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_hybrid_filter_applies_before_bm25_fusion():
    retriever = HybridRetriever(vector_store=_FilteredStore(), embedding=_Embedding())
    hits = await retriever.aretrieve("needle", filters={"tenant": "a"}, top_k=5)
    assert [hit.id for hit in hits] == ["inside"]
    with pytest.raises(ConfigError):
        await retriever.aretrieve("needle", filters={"tenant": {"$regex": "a"}})


class _Dialect:
    name = "fake"

    def __init__(self):
        self.described: list[str] = []

    async def list_tables(self, engine):
        return ["a", "b", "c"]

    async def describe(self, engine, table):
        self.described.append(table)
        return TableSchema(name=table, columns=[])

    async def execute_readonly(self, engine, sql, *, row_limit=1000, timeout_s=10):
        return QueryResult(columns=["n"], rows=[[1]])


class _TextClient:
    protocol = "fake"

    async def generate_text(self, prompt, **kwargs):
        return "SELECT 1 AS n"

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_nl2sql_describes_only_requested_tables_and_caches_schema():
    dialect = _Dialect()
    db = DBService(
        dialect=dialect, engine=object(), db_cfg=DBConfig(),
        llm_client=_TextClient(), llm_model="m",
    )
    await db.query_nl("count", tables=["a", "c"])
    await db.query_nl("count again", tables=["a", "c"])
    assert dialect.described == ["a", "c"]


@pytest.mark.asyncio
async def test_graph_profile_reload_reconciles_deleted_edges(tmp_path: Path):
    data = tmp_path / "data"
    triplets = data / "triplets"
    triplets.mkdir(parents=True)
    source = triplets / "graph.jsonl"
    source.write_text(
        json.dumps({"subject": "A", "relation": "r1", "object": "B"}) + "\n" +
        json.dumps({"subject": "A", "relation": "r2", "object": "C"}) + "\n",
        encoding="utf-8",
    )
    store = NetworkXGraphStore(persist_path=tmp_path / "graph.json")
    service = GraphService(store=store, data_dir=data, storage_dir=tmp_path)
    await service.load_from_profile()
    assert store.stats()["edges"] == 2

    source.write_text(
        json.dumps({"subject": "A", "relation": "r1", "object": "B"}) + "\n",
        encoding="utf-8",
    )
    await service.load_from_profile()
    edges = await service.neighbors("A", direction="out", relation_filter=["r1"], limit=1)
    assert store.stats()["edges"] == 1
    assert edges[0]["relation"] == "r1"
    assert edges[0]["properties"]["profile_source"] == "graph.jsonl:1"


@pytest.mark.asyncio
async def test_empty_and_graph_only_builds_are_lazy_and_close_idempotently(tmp_path: Path):
    settings = Settings(llm={"api_key": "test"})
    settings.data.base_dir = tmp_path
    empty = await build_datamind(settings, enable=set())
    assert len(empty.retrieve.tools) == len(empty.store.tools) == 0
    assert empty.services.embedding is empty.services.kb is None
    await empty.aclose()
    await empty.aclose()

    graph = await build_datamind(settings, enable={"graph"})
    assert graph.services.graph is not None
    assert graph.services.embedding is graph.services.kb is None
    await graph.aclose()


class _StagingStore:
    dimension = 2

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.discarded = False

    def create_staging_store(self):
        return _StagingStore()

    async def add(self, ids, texts, embeddings, metadatas=None):
        self.rows.update(dict(zip(ids, texts)))

    async def count(self):
        return len(self.rows)

    async def reset(self):
        self.rows.clear()

    async def activate_staging(self, staging, *, metadata=None):
        self.rows = dict(staging.rows)

    async def discard(self):
        self.discarded = True


class _FailingEmbedding(_Embedding):
    async def embed_texts(self, texts):
        raise RuntimeError("embedding failed")


@pytest.mark.asyncio
async def test_failed_staging_reindex_preserves_old_index(tmp_path: Path):
    (tmp_path / "doc.md").write_text("new corpus", encoding="utf-8")
    store = _StagingStore({"old": "old corpus"})
    service = KBService(
        embedding=_FailingEmbedding(), vector_store=store,
        retriever=object(), data_dir=tmp_path, retrieval_cfg=RetrievalConfig(),
    )
    with pytest.raises(RuntimeError, match="embedding failed"):
        await service.reindex()
    assert store.rows == {"old": "old corpus"}


@pytest.mark.asyncio
async def test_benchmark_checkpoints_and_resumes_without_duplicate_rows(tmp_path: Path):
    class _System:
        def __init__(self):
            self.calls: list[str] = []

        async def query(self, question, *, final_contract=None):
            self.calls.append(question)
            return {
                "answer": question.upper(), "usage": {}, "iterations": 1,
                "stop_reason": "stop", "surfaces_used": [],
                "tool_trace": [], "evidence": [],
            }

    artifact = tmp_path / "run.jsonl"
    system = _System()
    items = [
        {"task_id": "1", "question": "one"},
        {"task_id": "2", "question": "two"},
    ]
    await run_benchmark(
        items, system=system, output=artifact, run_id="run-a", concurrency=2,
        task_retries=0,
    )
    completed = _load_completed(artifact, "run-a")
    await run_benchmark(
        items, system=system, output=artifact, run_id="run-a", concurrency=2,
        task_retries=0, completed=completed,
    )

    rows = load_results(str(artifact))
    assert system.calls == ["one", "two"] or sorted(system.calls) == ["one", "two"]
    assert len(rows) == 2
    assert completed == {"1", "2"}
    assert exact_match("50", "5") is False
