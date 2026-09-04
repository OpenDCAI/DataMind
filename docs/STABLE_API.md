# Stable API (v1.x)

This document is the compatibility promise for DataMind v1.0.0 and later
minor/patch releases. The stable baseline is the native backend with local
profile storage. SDK/CCR and custom provider implementations are supported
integration points, but their vendor-specific setup is outside the Python API
promise; see the [support matrix](./SUPPORT_MATRIX.md).

## Python facade

The preferred entry point is `build_datamind`:

```python
from datamind.agent import build_datamind
from datamind.config import Settings


async def run() -> None:
    settings = Settings()  # reads DATAMIND__* environment variables
    system = await build_datamind(settings)
    try:
        receipt = await system.ingest("Remember that weekly reports use Chinese.")
        result = await system.query("What language should weekly reports use?")
        print(receipt["receipts"])
        print(result["answer"])
    finally:
        await system.aclose()
```

`DataMind` also supports the async context-manager form:

```python
system = await build_datamind(settings)
async with system:
    result = await system.query("...")
```

### Stable facade methods

| Symbol | Stability | Contract |
|---|---|---|
| `datamind.agent.build_datamind(settings, enable=None)` | Stable | Builds one `DataMind` facade with shared services and separate Store/Retrieve registries. `enable` may contain `kb`, `db`, `graph`, `skills`, and `memory`. |
| `DataMind.ingest(message, history=None)` | Stable | Runs StoreAgent and returns the StoreAgent result, including `answer`, `receipts`, `tool_trace`, `surfaces_used`, `iterations`, `stop_reason`, and `usage`. |
| `DataMind.query(message, history=None, final_contract=None)` | Stable | Runs RetrieveAgent and returns `answer`, `evidence`, `surfaces_used`, `tool_trace`, `iterations`, `stop_reason`, and `usage`. |
| `DataMind.warmup()` | Stable | Loads profile skills/graph and returns warmup counts plus hook names. |
| `DataMind.aclose()` | Stable | Idempotently closes shared clients, engines, stores, and providers. |
| `datamind.config.Settings` | Stable | Pydantic settings model populated from `DATAMIND__*` variables or `.env.datamind`. |

The `StoreAgent` and `RetrieveAgent` objects are available as
`system.store_agent` / `system.retrieve_agent` (or the shorter
`system.store` / `system.retrieve` properties). Their public `store`, `query`,
and `warmup` methods are stable convenience methods; loop constructors and
private attributes are not.

## Result shapes

The facade returns JSON-serialisable dictionaries so it can be used from a
CLI, HTTP server, or worker without importing a provider model. The important
fields are:

### RetrieveAgent result

```json
{
  "answer": "...",
  "evidence": [
    {
      "surface": "kb",
      "source_id": "file:...",
      "locator": {"source": "handbook.md", "chunk_id": "..."},
      "content": "...",
      "score": 0.82
    }
  ],
  "surfaces_used": ["kb", "db"],
  "tool_trace": [],
  "iterations": 1,
  "stop_reason": "end_turn",
  "usage": {}
}
```

`evidence` is best-effort normalized provenance. Consumers must still enforce
their own authorization and source retention policy; an evidence object is not
a new trust boundary.

### StoreAgent result

```json
{
  "answer": "Stored 1 item.",
  "receipts": [
    {
      "receipt_id": "...",
      "profile": "default",
      "revision": 3,
      "source": {"source_id": "memory:...", "kind": "memory"},
      "results": [
        {"surface": "memory", "operation": "save", "status": "stored", "items_written": 1}
      ]
    }
  ],
  "surfaces_used": ["memory"],
  "tool_trace": [],
  "iterations": 1,
  "stop_reason": "end_turn",
  "usage": {}
}
```

Receipt status is one of `stored`, `unchanged`, or `failed`. A receipt records
what DataMind observed and attempted; it is not a two-phase commit with an
external database or sink.

## HTTP API contract

The bundled FastAPI app is a thin transport over the same facade. Request body
for `/api/ask` and `/api/store`:

```json
{"message": "...", "history": []}
```

| Route | Stable response |
|---|---|
| `GET /api/health` | `status`, active profile, LLM protocol/model, enabled tool counts, and StoreAgent revision |
| `GET /api/tools` | `{ "tools": [...], "count": N }`; each item includes role, surface, access, description, group, and input schema |
| `POST /api/ask` | `AskResponse` containing the RetrieveAgent result fields |
| `POST /api/store` | `AskResponse` containing the StoreAgent result fields, including receipts |
| `POST /api/chat` | `text/event-stream`; each frame is `{ "type": ..., ... }` with `text`, `tool_use`, `tool_result`, `error`, or `done` events |
| `POST /api/upload` | Saved filename/path metadata and suggested StoreAgent prompts; upload alone does not ingest data |

`/api/kb/reindex`, `/api/kb/documents`, `/api/memory/{namespace}`, and
`/api/graph/stats` are inspection/compatibility routes. They are useful for a
private deployment but should receive the same authentication and authorization
as the primary routes. See [public deployment security boundaries](./SECURITY_BOUNDARIES.md).

## Contracts and enums

The provider-neutral models in `datamind.core.contracts` are stable names for
typed integrations:

- `DataSurface`: `kb`, `db`, `graph`, `skills`, `memory`
- `ToolAccess`: `read`, `write`, `utility`
- `SourceRef`: content-stable source identity and checksum
- `SurfaceWriteResult`: one write operation and its status
- `IngestReceipt`: auditable StoreAgent receipt
- `Evidence`: normalized read provenance
- `InferenceResult`: provider-neutral retrieval result model

The concrete provider classes, loop implementation modules, prompt wording,
tool descriptions, and private service fields may evolve within the v1.x line.
Depend on the facade and contracts above instead of importing underscored
helpers or reaching into a provider's engine.

## Compatibility policy

- Patch releases fix bugs and improve diagnostics without changing the stable
  result fields or role boundary.
- Minor releases may add optional fields, tools, providers, or routes. Consumers
  should ignore unknown JSON fields and unknown SSE event types.
- Major releases may change stable contracts and require migration notes.
- The StoreAgent write/read separation and RetrieveAgent read-only guarantee are
  security-relevant API behavior, not prompt conventions.
