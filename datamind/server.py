"""FastAPI adapter: Codex handles natural-language requests; DataMind owns data tools."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from datamind import __version__
from datamind.data_plane import DataPlane, build_data_plane
from datamind.host import CodexHost
from datamind.workspace import Workspace
from datamind.config import DataSettings
from datamind.config import Settings
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from datamind.core.logging import setup_logging


# -------------------------------------------------------------- lifespan


class AppState:
    """Container held on `app.state` — no module-level globals."""

    data: DataPlane | None = None
    host: CodexHost | None = None
    settings: Settings | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging("INFO")
    st = AppState()
    st.settings = DataSettings()
    st.settings.ensure_dirs()
    active = {'graph', 'db', 'skills', 'memory'}
    if st.settings.embedding.api_key or st.settings.embedding.provider == 'huggingface':
        active.add('kb')
    st.data = await build_data_plane(st.settings, enable=active)
    await st.data.warmup()
    st.host = CodexHost(st.settings)
    app.state.datamind = st
    try:
        yield
    finally:
        if st.host is not None:
            await st.host.aclose()
        if st.data is not None:
            await st.data.aclose()


app = FastAPI(title="DataMind", version=__version__, lifespan=_lifespan)

# CORS: permissive by default — tighten in production via env or reverse proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files (frontend) ────────────────────────────────────────────────
# Resolution order:
#   1. Repo-level static/ (developer running from a checkout)
#   2. Package-bundled datamind/static/ (pip-install users)
# Whichever exists first wins, so editable installs and wheel installs both
# Just Work. The bundled copy ships with the wheel so `pip install datamind`
# users get the browser UI without cloning the repo.
def _resolve_static_dir() -> Path | None:
    repo_static = Path(__file__).resolve().parent.parent / "static"
    pkg_static = Path(__file__).resolve().parent / "static"
    for cand in (repo_static, pkg_static):
        if (cand / "app.html").is_file():
            return cand
    return None


_STATIC_DIR = _resolve_static_dir()
if _STATIC_DIR is not None:
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def _root() -> FileResponse:
        target = _STATIC_DIR / "app.html"
        if not target.is_file():
            raise HTTPException(404, "frontend not bundled")
        return FileResponse(target)


# ---------------------------------------------------------------- deps


def _state(request: Request) -> AppState:
    st: AppState = request.app.state.datamind
    if st.data is None or st.host is None or st.settings is None:
        raise HTTPException(503, "Agent not ready")
    return st


def _session_id(x_session_id: str | None = Header(default=None)) -> str:
    return x_session_id or "default"


# --------------------------------------------------------------- models


class AskRequest(BaseModel):
    message: str = Field(..., min_length=1)
    history: list[dict] | None = None
    revision: str | None = Field(default=None,min_length=1,max_length=128)


class AskResponse(BaseModel):
    answer: str
    iterations: int
    stop_reason: str
    usage: dict
    evidence: list[dict] = Field(default_factory=list)
    receipts: list[dict] = Field(default_factory=list)
    surfaces_used: list[str] = Field(default_factory=list)
    tool_trace: list[dict] = Field(default_factory=list)
    workspace_revision: str | None = None
    request_id: str | None = None
    recovery: dict = Field(default_factory=dict)


# ------------------------------------------------------------- endpoints


@app.get('/api/health')
async def health(st: AppState = Depends(_state)) -> dict:
    return {'workspace_revision': Workspace(st.settings).current_revision, 'status': 'ok', 'backend': 'codex', 'model': st.settings.codex.model,
            'profile': st.settings.data.profile, 'version': __version__,
            'tools': len(st.data.tools), 'revision': st.data.revision,
            'kb_available': st.data.services.kb is not None,
            'embedding': st.settings.embedding.model, 'db_dialect': st.settings.db.dialect}


@app.get('/api/tools')
async def list_tools(st: AppState = Depends(_state)) -> dict:
    out = [{'name': spec.name, 'role': spec.access.value, 'surface': spec.metadata.get('surface'),
            'access': spec.access.value, 'description': spec.description,
            'group': spec.metadata.get('group'), 'input_schema': spec.input_schema}
           for spec in (st.data.tools.get(name) for name in st.data.tools.names())]
    return {'tools': out, 'count': len(out)}


@app.post("/api/ask", response_model=AskResponse)
async def ask(
    req: AskRequest,
    st: AppState = Depends(_state),
    session: str = Depends(_session_id),
) -> AskResponse:
    context = RequestContext(session_id=session, profile=st.settings.data.profile)
    with bind_context(context):
        result = await st.host.query(
            req.message,
            history=req.history or [],
            workspace_revision=req.revision,
        )
    return AskResponse(
        answer=result["answer"],
        iterations=result["iterations"],
        stop_reason=result["stop_reason"],
        usage=result["usage"],
        evidence=result.get("evidence", []),
        receipts=result.get("receipts", []),
        surfaces_used=result.get("surfaces_used", []),
        tool_trace=result.get("tool_trace", []),
        workspace_revision=result.get("workspace_revision"),
        request_id=result.get("request_id"), recovery=result.get("recovery", {}),
    )


@app.post("/api/store", response_model=AskResponse)
async def store(
    req: AskRequest,
    st: AppState = Depends(_state),
    session: str = Depends(_session_id),
) -> AskResponse:
    """Ask the main Codex agent to persist data through the shared data tools."""
    if req.revision is not None:
        raise HTTPException(400, 'Published revisions are read-only; write to live data and publish a new revision')
    context = RequestContext(session_id=session, profile=st.settings.data.profile)
    with bind_context(context):
        result = await st.host.ingest(
            req.message,
            history=req.history or [],
        )
    return AskResponse(
        answer=result["answer"],
        iterations=result["iterations"],
        stop_reason=result["stop_reason"],
        usage=result["usage"],
        evidence=result.get("evidence", []),
        receipts=result.get("receipts", []),
        surfaces_used=result.get("surfaces_used", []),
        tool_trace=result.get("tool_trace", []),
        workspace_revision=result.get("workspace_revision"),
        request_id=result.get("request_id"), recovery=result.get("recovery", {}),
    )


@app.post("/api/chat")
async def chat(
    req: AskRequest,
    st: AppState = Depends(_state),
    session: str = Depends(_session_id),
):
    async def stream() -> AsyncIterator[bytes]:
        context = RequestContext(session_id=session, profile=st.settings.data.profile)
        with bind_context(context):
            try:
                async for event in st.host.stream(
                    req.message,
                    history=req.history or [],
                    workspace_revision=req.revision,
                ):
                    payload = json.dumps(
                        event,
                        ensure_ascii=False,
                    )
                    yield f"data: {payload}\n\n".encode("utf-8")
            except Exception as exc:  # noqa: BLE001 - convert provider errors to SSE
                payload = json.dumps(
                    {"type": "error", "message": f"stream failed: {type(exc).__name__}"},
                    ensure_ascii=False,
                )
                yield f"data: {payload}\n\n".encode("utf-8")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/kb/reindex")
async def kb_reindex(st: AppState = Depends(_state)) -> dict:
    """Explicit data-tool endpoint with hooks and a write receipt."""
    if st.data.services.kb is None:
        raise HTTPException(503,"Configure an embedding provider before rebuilding KB")
    context = RequestContext.new(profile=st.settings.data.profile)
    with bind_context(context):
        return await st.data.call("kb_reindex")


@app.get("/api/kb/documents")
async def kb_documents(st: AppState = Depends(_state)) -> dict:
    workspace=Workspace(st.settings)
    revision=workspace.current_revision
    if revision and 'kb' in workspace.manifest(revision)['surfaces']:
        async with workspace.reader(revision,enable={'kb'}) as reader:
            items=await reader._system.services.kb.list_documents()
    else:
        if st.data.services.kb is None:
            raise HTTPException(503, 'Configure an embedding provider before using KB tools')
        items = await st.data.services.kb.list_documents()
    return {"count": len(items), "items": items}


@app.get("/api/memory/{namespace}")
async def memory_peek(
    namespace: str,
    query: str = "",
    top_k: int = 10,
    st: AppState = Depends(_state),
) -> dict:
    """Inspect long-term memory.

    The path component is treated as a profile name (v1.0 scope='profile').
    For session-scoped peeks pass session_id explicitly via a query param if
    that becomes useful — the UI today only browses by tenant.
    """
    if namespace != st.settings.data.profile:
        raise HTTPException(403, 'This server is bound to a different profile')
    if not query:
        query = " "  # any string — recall ranks every row lexically if no embedding
    results = await st.data.services.memory.recall(query, profile=namespace, top_k=top_k)
    return {"profile": namespace, "query": query, "results": results}


@app.get("/api/graph/stats")
async def graph_stats(st: AppState = Depends(_state)) -> dict:
    workspace=Workspace(st.settings)
    revision=workspace.current_revision
    if revision and 'graph' in workspace.manifest(revision)['surfaces']:
        async with workspace.reader(revision,enable={'graph'}) as reader:
            return {**reader._system.services.graph.stats(),'workspace_revision':revision}
    return st.data.services.graph.stats()


# ----------------------------------------------------- file upload (ingest)


@app.post("/api/upload")
async def upload_file(
    request: Request,
    st: AppState = Depends(_state),
) -> dict:
    """Accept a multipart file upload and stash it in the profile's
    `uploads/` dir. Returns the saved path so the frontend can construct a
    follow-up Codex request asking it to ingest the file.

    We deliberately don't auto-ingest here — the agent decides what to do
    with the file (KB chunk? CSV import? graph triples?) based on the
    user's request. Auto-ingest would surprise users who just want to see
    the file before deciding.

    Caps:
        - 25 MB per file (rough; tightened in production via reverse proxy)
        - Path traversal blocked: only the basename is honoured
    """
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(400, "no 'file' field in multipart body")

    raw_name = (upload.filename or "upload.bin").strip()
    # Strip any directory components — only basename is allowed.
    safe_name = Path(raw_name).name or "upload.bin"
    if safe_name in {".",".."}:
        safe_name="upload.bin"

    body = await upload.read()
    if len(body) > 25 * 1024 * 1024:
        raise HTTPException(413, f"file too large ({len(body)} bytes); cap is 25 MB")

    profile_dir = st.settings.data.data_dir
    uploads_dir = profile_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    target = uploads_dir / safe_name
    if not target.resolve().is_relative_to(uploads_dir.resolve()):
        raise HTTPException(403,"upload path escapes the profile directory")
    # A same-name upload represents a new revision of the same user document.
    # Keep its stable path so re-ingestion can replace KB chunks and graph
    # edges that came from the previous revision.
    replaced = target.exists() and target.read_bytes() != body
    target.write_bytes(body)

    return {
        "saved_to": str(target),
        "filename": target.name,
        "replaced": replaced,
        "bytes": len(body),
        "content_type": getattr(upload, "content_type", None),
        # Help the frontend craft the follow-up prompt to the agent.
        "suggested_prompt_kb": f"帮我把刚上传的 {target.name} 加进知识库",
        "suggested_prompt_csv": f"把刚上传的 {target.name} 导入成数据表",
    }


# Expose an ASGI app name for uvicorn
__all__ = ["app"]
