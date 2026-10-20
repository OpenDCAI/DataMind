# Public API (v2.x)

DataMind 2.0 removes `build_datamind`, StoreAgent/RetrieveAgent and the
Claude SDK/CCR product runtime. See [migration notes](LEGACY_RUNTIME.md).
The supported architecture is Codex over a shared data layer.

## Direct Python data plane

```python
from datamind.config import DataSettings
from datamind.data_plane import build_data_plane

async def run():
    data = await build_data_plane(DataSettings(), enable={"graph", "memory", "skills"})
    try:
        await data.warmup()
        receipt = await data.call("memory_save", {"content": "Weekly reports use Chinese."})
        result = await data.call("memory_recall", {"query": "report language"}, read_only=True)
        print(receipt, result)
    finally:
        await data.aclose()
```

`DataSettings` and `Settings` name the same typed settings model.
`DataPlane.call(name, arguments, context=None, read_only=False)` validates the
schema, profile and hooks. Business write tools attach receipts. Selecting
surfaces controls which services initialize. No conversation client is created;
KB still requires a configured embedding provider. An explicit `text_client`
parameter is reserved for custom capability pipelines and benchmark fixtures.

## Codex hosting

```python
from datamind.config import DataSettings
from datamind.host import CodexHost

async def run():
    host = CodexHost(DataSettings())
    try:
        written = await host.ingest("Remember: weekly reports use Chinese.")
        result = await host.query("What language should weekly reports use?")
        print(written["receipts"], result["answer"])
    finally:
        await host.aclose()
```

`CodexHost` is a transport adapter around the official SDK, not another agent
implementation. `query` binds read-only access; `ingest` permits explicit
writes. `stream` yields `text`, `tool_use`, `tool_result`, and `done` dictionaries.
Completion includes `answer`, `receipts`, `evidence`, `tool_trace`,
`surfaces_used`, `iterations`, `stop_reason`, `usage`, `request_id`,
`workspace_revision` and `recovery`. The iteration count represents observed
data/tool calls, not internal model reasoning iterations.

Actual MCP operations supply request-scoped traces and receipts, including
when SDK Code Mode wraps calls inside a generic execution event. Evidence is
best-effort normalized provenance. Receipts contain `stored`, `unchanged` or
`failed` per-surface outcomes; an ID alone does not mean success.

`CodexSession` is the lower-level official-SDK bridge. Local plugin use does
not require `CodexHost` or a second model key. Gateway and approval settings are
documented in [deployment](CODEX_GATEWAY_DEPLOYMENT.md).

## Workspace snapshots

`Workspace.prepare` inventories sources; `Workspace.publish` builds and atomically
publishes selected KB/DB/Graph snapshots from prepared, source-hashed inputs.
`Workspace.reader(revision=...)` selects immutable historical snapshots;
`enable` may select a subset of that manifest's surfaces. `reader.call` enforces
read-only access. Query hosting selects the current head at request admission;
without a published head, it reads independent live stores. Direct writes do
not silently modify snapshots. Memory/Skills are not one atomic business snapshot.

## HTTP API

```json
{"message": "...", "history": []}
```

| Route | Contract |
|---|---|
| `GET /api/health` | Codex backend/model, profile, package version, tool count, receipt revision and published workspace revision |
| `GET /api/tools` | Tools with access, surface, description and schema; role fields retain read/write labels for UI compatibility |
| `POST /api/ask` | Same Codex host with read-only access; optional request field `revision` |
| `POST /api/store` | Same Codex host with write permission and checked receipt results |
| `POST /api/chat` | SSE frames: `text`, `tool_use`, `tool_result`, `error`, `done`; chat uses read-only access |
| `POST /api/upload` | Saved file metadata and suggested import request; upload does not ingest |

Ask/store responses include answer, evidence, surfaces, trace, receipts,
request ID, workspace revision, recovery and usage. Inspection/reindex routes
remain available; KB routes require a configured provider. Memory inspection
is limited to the active profile. `X-Session-Id` scopes conversation and is not
an authentication credential. See [security boundaries](SECURITY_BOUNDARIES.md).

## Graph traversal

NetworkX traversal distinguishes exact stored edge sequences, including
parallel relations and different origins. Breadth-first expansion is bounded
by `max_hops` and `max_results`; relation filters apply at every hop, and paths
do not repeat nodes. Deterministic expansion and score sorting do not promise
a global top-k result. Sources and edge properties remain attached to paths.

## Compatibility policy

Minor releases may add fields and tools; consumers should ignore unknown JSON
fields and SSE events. Major releases may remove public symbols with migration
notes. Provider internals, prompt wording and SDK-specific events are extension
points. Core contracts include `DataSurface`, `ToolAccess`, `SourceRef`,
`SurfaceWriteResult`, `IngestReceipt`, `Evidence` and `InferenceResult`.
