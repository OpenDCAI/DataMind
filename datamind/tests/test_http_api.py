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


class _FailingStreamLoop(_FakeLoop):
    async def stream_turn(self, *, user_message: str, history=None, final_contract=None):
        yield AgentEvent("text", {"delta": "partial"})
        raise RuntimeError("provider unavailable")


class _FakeCodexHost:
    fail = False
    async def query(self, message, **kwargs):
        kwargs.pop('workspace_revision',None)
        return await _FakeLoop('retrieve').run_turn(user_message=message, **kwargs)
    async def ingest(self, message, **kwargs):
        return await _FakeLoop('store').run_turn(user_message=message, **kwargs)
    async def stream(self, message, **kwargs):
        yield {'type':'text','delta':'partial' if self.fail else 'streamed answer'}
        if self.fail:
            raise RuntimeError('provider unavailable')
        yield {'type':'done','answer':'streamed answer','iterations':1,'stop_reason':'end_turn','usage':{}}


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

    tools = _registry('retrieve')
    tools.extend([_registry('store').get('kb_reindex')])
    data = SimpleNamespace(tools=tools, revision=1,
        services=SimpleNamespace(kb=_FakeKB(),memory=_FakeMemory(),graph=_FakeGraph()))
    async def call(name):
        return await tools.get(name).handler()
    data.call=call
    state = AppState()
    state.settings = settings
    state.data = data
    state.host = _FakeCodexHost()
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
        assert health.json()["backend"] == "codex"
        assert health.json()["tools"] == 2

        tools = await client.get("/api/tools")
        assert tools.status_code == 200
        assert tools.json()["count"] == 2
        assert {item["role"] for item in tools.json()["tools"]} == {"read", "write"}

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
        assert upload_body["replaced"] is False

        replacement = await client.post(
            "/api/upload",
            files={"file": ("note.md", b"corrected content", "text/markdown")},
        )
        assert replacement.status_code == 200
        replacement_body = replacement.json()
        assert Path(replacement_body["saved_to"]) == saved_to
        assert replacement_body["filename"] == "note.md"
        assert replacement_body["replaced"] is True
        assert saved_to.read_text(encoding="utf-8") == "corrected content"

        docs = await client.get("/api/kb/documents")
        assert docs.json() == {"count": 1, "items": [{"source": "demo.md", "chunks": 1}]}
        reindex = await client.post("/api/kb/reindex")
        assert reindex.json()["ok"] is True

        memory = await client.get("/api/memory/http_e2e", params={"query": "demo"})
        assert memory.json()["profile"] == "http_e2e"
        assert memory.json()["results"][0]["content"] == "remembered"

        graph = await client.get("/api/graph/stats")
        assert graph.json() == {"nodes": 2, "edges": 1}


@pytest.mark.asyncio
async def test_chat_converts_stream_failure_to_sse_error(configured_app):
    test_app, _settings = configured_app
    test_app.state.datamind.host.fail = True
    transport = httpx.ASGITransport(app=test_app, raise_app_exceptions=True)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/chat", json={"message": "stream this"})

    assert response.status_code == 200
    assert '"type": "text"' in response.text
    assert '"type": "error"' in response.text
    assert '"message": "stream failed: RuntimeError"' in response.text


@pytest.mark.asyncio
async def test_workspace_policy_routes_validate_and_persist(configured_app):
    test_app,settings=configured_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app),base_url='http://test') as client:
        status=await client.get('/api/workspace?usage=true')
        assert status.json()['retention']['mode']=='latest'
        assert 'storage' in status.json()
        result=await client.post('/api/workspace/retention',json={'mode':'recent','keep_last':3})
        assert result.json()=={'mode':'recent','keep_last':3}
        assert (await client.get('/api/workspace')).json()['retention']['keep_last']==3
        assert (await client.post('/api/workspace/retention',json={'mode':'none'})).status_code==422
        assert (await client.post('/api/workspace/retention',json={'mode':'recent','keep_last':0})).status_code==422
        preview=await client.post('/api/workspace/gc',json={})
        assert preview.json()['dry_run'] is True
        missing=await client.post('/api/workspace/rollback',json={'revision':'unknown'})
        assert missing.status_code==409


@pytest.mark.asyncio
async def test_http_startup_with_private_workspace_dolt_needs_no_live_dsn(tmp_path,monkeypatch):
    from datamind import server
    cfg=Settings(_env_file=None,data={'base_dir':tmp_path},db={'dialect':'dolt'},
                 workspace={'storage_mode':'incremental'})
    class Host:
        def __init__(self,settings):pass
        async def aclose(self):pass
    monkeypatch.setattr(server,'DataSettings',lambda:cfg)
    monkeypatch.setattr(server,'CodexHost',Host)
    async with server._lifespan(server.app):
        state=server.app.state.datamind
        assert state.data.services.db is None
        assert 'workspace_publish' in state.data.tools
