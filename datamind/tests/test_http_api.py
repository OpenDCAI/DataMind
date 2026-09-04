"""HTTP API end-to-end contracts using a deterministic in-process system."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from datamind.agent.base import AgentEvent
from datamind.config import Settings
from datamind.core.tools import ToolRegistry, ToolSpec
from datamind.server import AppState, app


class _FakeLoop:
    def __init__(self, role: str) -> None:
        self.role = role

    async def run_turn(self, *, user_message: str, history=None, final_contract=None):
        if self.role == "store":
            return {
                "answer": "stored",
                "iterations": 1,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 1},
                "receipts": [{"receipt_id": "http-receipt", "revision": 1}],
                "surfaces_used": ["memory"],
                "tool_trace": [{"name": "memory_save", "access": "write"}],
            }
        return {
            "answer": f"answer for {user_message}",
            "iterations": 1,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 4, "output_tokens": 2},
            "evidence": [{
                "surface": "kb",
                "source_id": "demo.md",
                "locator": {"source": "demo.md"},
                "content": "demo evidence",
                "score": 1.0,
            }],
            "surfaces_used": ["kb"],
            "tool_trace": [{"name": "kb_search", "access": "read"}],
        }

    async def stream_turn(self, *, user_message: str, history=None, final_contract=None):
        yield AgentEvent("text", {"delta": "streamed answer"})
        yield AgentEvent("done", {
            "iterations": 1,
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 2},
        })


class _FakeKB:
    async def list_documents(self):
        return [{"source": "demo.md", "chunks": 1}]


class _FakeMemory:
    async def recall(self, query: str, *, profile: str, top_k: int):
        return [{"content": "remembered", "profile": profile, "score": 1.0}][:top_k]


class _FakeGraph:
    def stats(self):
        return {"nodes": 2, "edges": 1}


def _registry(role: str) -> ToolRegistry:
    registry = ToolRegistry()

    async def _tool(**kwargs):
        return {"ok": True, **kwargs}

    if role == "retrieve":
        registry.add(ToolSpec(
            name="kb_search",
            description="Search the demo knowledge base.",
            input_schema={"type": "object", "properties": {}},
            handler=_tool,
            metadata={"surface": "kb", "access": "read", "group": "kb"},
        ))
    else:
        registry.add(ToolSpec(
            name="kb_reindex",
            description="Reindex the demo knowledge base.",
            input_schema={"type": "object", "properties": {}},
            handler=_tool,
            metadata={"surface": "kb", "access": "write", "group": "kb"},
        ))
    return registry


@pytest.fixture
def configured_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(llm={"api_key": "http-test"})
    settings.data.base_dir = tmp_path
    settings.data.profile = "http_e2e"
    settings.ensure_dirs()

    retrieve = SimpleNamespace(
        loop=_FakeLoop("retrieve"),
        tools=_registry("retrieve"),
    )
    store = SimpleNamespace(
        loop=_FakeLoop("store"),
        tools=_registry("store"),
        revision=1,
    )
    system = SimpleNamespace(
        retrieve=retrieve,
        store=store,
        services=SimpleNamespace(
            kb=_FakeKB(),
            memory=_FakeMemory(),
            graph=_FakeGraph(),
        ),
    )
    state = AppState()
    state.settings = settings
    state.system = system
    monkeypatch.setattr(app.state, "datamind", state, raising=False)
    return app, settings


@pytest.mark.asyncio
async def test_http_api_routes_round_trip(configured_app):
    test_app, settings = configured_app
    transport = httpx.ASGITransport(app=test_app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["retrieve_tools"] == 1
        assert health.json()["store_tools"] == 1

        tools = await client.get("/api/tools")
        assert tools.status_code == 200
        assert tools.json()["count"] == 2
        assert {item["role"] for item in tools.json()["tools"]} == {"retrieve", "store"}

        ask = await client.post(
            "/api/ask",
            headers={"X-Session-Id": "session-http"},
            json={"message": "What is in the demo?"},
        )
        assert ask.status_code == 200
        ask_body = ask.json()
        assert ask_body["answer"].startswith("answer for")
        assert ask_body["evidence"][0]["surface"] == "kb"

        store = await client.post(
            "/api/store",
            json={"message": "Remember this demo"},
        )
        assert store.status_code == 200
        assert store.json()["receipts"][0]["receipt_id"] == "http-receipt"

        chat = await client.post("/api/chat", json={"message": "stream this"})
        assert chat.status_code == 200
        assert chat.headers["content-type"].startswith("text/event-stream")
        assert '"type": "text"' in chat.text
        assert '"type": "done"' in chat.text

        upload = await client.post(
            "/api/upload",
            files={"file": ("note.md", b"hello from http", "text/markdown")},
        )
        assert upload.status_code == 200
        upload_body = upload.json()
        saved_to = Path(upload_body["saved_to"])
        assert saved_to == settings.data.data_dir / "uploads" / "note.md"
        assert saved_to.read_text(encoding="utf-8") == "hello from http"

        docs = await client.get("/api/kb/documents")
        assert docs.json() == {"count": 1, "items": [{"source": "demo.md", "chunks": 1}]}
        reindex = await client.post("/api/kb/reindex")
        assert reindex.json()["ok"] is True

        memory = await client.get("/api/memory/customer-a", params={"query": "demo"})
        assert memory.json()["profile"] == "customer-a"
        assert memory.json()["results"][0]["content"] == "remembered"

        graph = await client.get("/api/graph/stats")
        assert graph.json() == {"nodes": 2, "edges": 1}
