# Migrating to the unified Codex host (2.0)

The older two-agent product runtime has been removed. This is a breaking Python
API change; it is not a delayed rollout or an automatic fallback mechanism.

| Previous entry | Replacement |
| --- | --- |
| `build_datamind(Settings())` plus `query` / `ingest` | `CodexHost(DataSettings())` plus `query` / `ingest` |
| `system.retrieve.tools.get(...).handler(...)` | `data.call(name, arguments, read_only=True)` |
| `system.store.tools.get(...).handler(...)` | `data.call(name, arguments)` with hooks and business-write receipts |
| StoreAgent / RetrieveAgent role prompts | one main Codex agent plus ingestion/update/retrieval Skills |
| `agent.backend=native/sdk` and Claude SDK/CCR | `DATAMIND__CODEX__*` and the official `openai-codex` SDK |
| model-extracting Workspace builds | Codex-prepared graph_sources, then `Workspace.update` or MCP prepare/publish |

```python
from datamind.config import DataSettings
from datamind.host import CodexHost

async def run():
    host = CodexHost(DataSettings())
    try:
        return await host.query('Summarize the workspace with evidence.')
    finally:
        await host.aclose()
```

Root launchers `python main.py` (chat by default) and `python server.py` also
delegate to the shared host; `uvicorn server:app` exports the same FastAPI app.
The old v0.1 endpoint/event shapes are not retained: update clients to the
[current HTTP contract](STABLE_API.md). Existing local data is not deleted or
automatically republished by this migration.

The existing package CLI commands and HTTP ask/store/chat endpoints remain, backed by
that same host. The browser UI retains upload, evidence and receipt flows.
Graph/SQL/Skills/Memory data operations and `datamind info` do not require a
conversation model key. Natural-language commands require the Codex SDK/login
or an explicitly configured Responses-compatible gateway.

Read requests bind a Workspace revision at admission and reject write operations
below the prompt boundary. No published snapshot means an explicitly live read
scope. Data writes and immutable snapshot publication remain separate. Source
checkpoints, failed-build isolation, pinned raw evidence and rollback are retained.

Generic native-loop classes remain under `datamind.agent` solely as benchmark
primitives; product services do not use them. Existing capability utilities may
receive an explicit text client for custom pipelines or tests; the data builder
never constructs a conversation client on its own.
