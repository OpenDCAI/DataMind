"""FastAPI HTTP server.

Endpoints:
    POST /api/chat           — stream a RetrieveAgent answer over SSE
    POST /api/store          — run StoreAgent and return its receipt summary
    POST /api/ask            — non-streaming convenience (full response JSON)
    GET  /api/health         — process liveness + config snapshot
    GET  /api/tools          — list registered tools (name/description/schema)
    POST /api/kb/reindex     — rebuild the KB index
    GET  /api/kb/documents   — list docs under the active profile
    GET  /api/memory/:ns     — peek into a memory namespace
    GET  /api/graph/stats    — graph stats

Streaming uses true SSE; each AgentEvent becomes one `data: {...}\n\n` frame.

Design:
- One DataMind system (StoreAgent + RetrieveAgent) per process, built at startup.
- No request-scoped globals — each agent is concurrency-safe because
  each call threads through its own `history=[]` parameter.
- Session identity comes from the `X-Session-Id` header (or cookie), not a
  server-side map, so horizontal scaling is trivial.
"""
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
from datamind.agent import DataMind, build_datamind
from datamind.config import Settings
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from datamind.core.logging import setup_logging


# -------------------------------------------------------------- lifespan


class AppState:
    """Container held on `app.state` — no module-level globals."""

    system: DataMind | None = None
    settings: Settings | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging("INFO")
    st = AppState()
    st.settings = Settings()
    st.settings.ensure_dirs()
    st.system = await build_datamind(st.settings)
    await st.system.warmup()
    app.state.datamind = st
    try:
        yield
    finally:
        if st.system is not None:
            await st.system.aclose()


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
    if st.system is None or st.settings is None:
        raise HTTPException(503, "Agent not ready")
    return st


def _session_id(x_session_id: str | None = Header(default=None)) -> str:
    return x_session_id or "default"


# --------------------------------------------------------------- models


class AskRequest(BaseModel):
    message: str = Field(..., min_length=1)
    history: list[dict] | None = None


class AskResponse(BaseModel):
    answer: str
    iterations: int
    stop_reason: str
    usage: dict
    evidence: list[dict] = Field(default_factory=list)
    receipts: list[dict] = Field(default_factory=list)
    surfaces_used: list[str] = Field(default_factory=list)
    tool_trace: list[dict] = Field(default_factory=list)


# ------------------------------------------------------------- endpoints


@app.get("/api/health")
async def health(st: AppState = Depends(_state)) -> dict:
    return {
        "status": "ok",
        "profile": st.settings.data.profile,
        "llm_protocol": st.settings.llm.protocol,
        "model": st.settings.llm.model,
        "embedding": st.settings.embedding.model,
        "db_dialect": st.settings.db.dialect,
        "retrieve_tools": len(st.system.retrieve.tools),
        "store_tools": len(st.system.store.tools),
        "revision": st.system.store.revision,
    }


@app.get("/api/tools")
async def list_tools(st: AppState = Depends(_state)) -> dict:
    out = []
    for role, registry in (
        ("retrieve", st.system.retrieve.tools),
        ("store", st.system.store.tools),
    ):
        for name in registry.names():
            spec = registry.get(name)
            out.append({
                "name": spec.name,
                "role": role,
                "surface": spec.metadata.get("surface"),
                "access": spec.metadata.get("access"),
                "description": spec.description,
                "group": spec.metadata.get("group"),
                "input_schema": spec.input_schema,
            })
    return {"tools": out, "count": len(out)}


@app.post("/api/ask", response_model=AskResponse)
async def ask(
    req: AskRequest,
    st: AppState = Depends(_state),
    session: str = Depends(_session_id),
) -> AskResponse:
    context = RequestContext(session_id=session, profile=st.settings.data.profile)
    with bind_context(context):
        result = await st.system.retrieve.loop.run_turn(
            user_message=req.message,
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
    )


@app.post("/api/store", response_model=AskResponse)
async def store(
    req: AskRequest,
    st: AppState = Depends(_state),
    session: str = Depends(_session_id),
) -> AskResponse:
    """Ask StoreAgent to route and persist data across the five surfaces."""
    context = RequestContext(session_id=session, profile=st.settings.data.profile)
    with bind_context(context):
        result = await st.system.store.loop.run_turn(
            user_message=req.message,
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
            async for event in st.system.retrieve.loop.stream_turn(
                user_message=req.message,
                history=req.history or [],
            ):
                payload = json.dumps(
                    {"type": event.type, **event.data},
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
    """Compatibility endpoint routed through StoreAgent's receipt boundary."""
    return await st.system.store.tools.get("kb_reindex").handler()


@app.get("/api/kb/documents")
async def kb_documents(st: AppState = Depends(_state)) -> dict:
    items = await st.system.services.kb.list_documents()
    return {"count": len(items), "items": items}


@app.get("/api/memory/{namespace}")
async def memory_peek(
    namespace: str,
    query: str = "",
    top_k: int = 10,
    st: AppState = Depends(_state),
) -> dict:
    """Inspect long-term memory.

    The path component is treated as a profile name (v0.3 scope='profile').
    For session-scoped peeks pass session_id explicitly via a query param if
    that becomes useful — the UI today only browses by tenant.
    """
    if not query:
        query = " "  # any string — recall ranks every row lexically if no embedding
    results = await st.system.services.memory.recall(query, profile=namespace, top_k=top_k)
    return {"profile": namespace, "query": query, "results": results}


@app.get("/api/graph/stats")
async def graph_stats(st: AppState = Depends(_state)) -> dict:
    return st.system.services.graph.stats()


# ----------------------------------------------------- file upload (ingest)


@app.post("/api/upload")
async def upload_file(
    request: Request,
    st: AppState = Depends(_state),
) -> dict:
    """Accept a multipart file upload and stash it in the profile's
    `uploads/` dir. Returns the saved path so the frontend can construct a
    follow-up StoreAgent request asking it to ingest the file.

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

    body = await upload.read()
    if len(body) > 25 * 1024 * 1024:
        raise HTTPException(413, f"file too large ({len(body)} bytes); cap is 25 MB")

    profile_dir = st.settings.data.data_dir
    uploads_dir = profile_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    target = uploads_dir / safe_name
    # Avoid silent overwrite when the upload is a different file with the
    # same name. Suffix with a content hash if collision.
    if target.exists() and target.read_bytes() != body:
        import hashlib
        suffix = hashlib.sha1(body).hexdigest()[:8]
        target = uploads_dir / f"{Path(safe_name).stem}-{suffix}{Path(safe_name).suffix}"
    target.write_bytes(body)

    return {
        "saved_to": str(target),
        "filename": target.name,
        "bytes": len(body),
        "content_type": getattr(upload, "content_type", None),
        # Help the frontend craft the follow-up prompt to the agent.
        "suggested_prompt_kb": f"帮我把刚上传的 {target.name} 加进知识库",
        "suggested_prompt_csv": f"把刚上传的 {target.name} 导入成数据表",
    }


# Expose an ASGI app name for uvicorn
__all__ = ["app"]
