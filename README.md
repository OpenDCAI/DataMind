# DataMind

**The shared workspace data layer for Codex.** Codex reasons, ingests and retrieves;
DataMind stores data, executes tools, and returns evidence and write receipts.

[中文](./README_zh.md) · [Codex plugin](./plugins/datamind-context/) ·
[CI](https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml) ·
[Documentation](https://opendcai.github.io/DataMind-Doc/)

```text
Local Codex (personal)         Codex SDK (enterprise host)
          └──────── main Codex agent ────────┘
                       Skills + MCP
                            │
            shared DataMind data tools
          KB · SQL · Graph · Skills · Memory
        evidence · receipts · workspace revisions
```

The plugin, official Codex SDK integration, CLI, HTTP API and browser UI share
the same data layer. Codex is the main agent for ingestion and retrieval.
DataMind contains no StoreAgent/RetrieveAgent implementation or Claude SDK agent
backend. Generic native loop primitives remain only for deterministic baseline
experiments; product entry points do not assemble them.

## Five surfaces

- **KB:** document chunks and semantic search; requires an embedding provider.
- **SQL:** exact records, joins and aggregation; Codex generates read-only SQL.
- **Graph:** typed entities and supported relationships. Relation names are
  extensible strings. Codex extracts triples; DataMind validates and stores them.
  `graph_schema` → `graph_search_entities` → `graph_neighbors` / `graph_traverse`
  supports discovery and bounded exploration. NetworkX + JSON remains the local backend.
- **Skills:** versioned reusable procedures, loaded on demand and recorded in write receipts.
- **Memory:** durable user facts and preferences, with profile/session scope.

Direct Graph, SQL, Skills and Memory tools do not need a DataMind conversation-model
key. Embedding models are separate from the main Codex model. Use `simple` or `hybrid` with direct tools. An explicitly supplied capability
text client can support custom pipelines without adding an agent loop.

## Quick start

### Personal: local Codex plugin

```bash
python -m pip install -e .
./scripts/install_codex_plugin.sh
```

In Codex, ask DataMind to ingest a folder or answer from an existing workspace.
The plugin Skill guides Codex to select procedures with `skill_get`, inspect
sources, extract relationships, invoke data tools and check receipts.

For KB, configure a dedicated embedding endpoint; example for OpenAI embeddings:

```bash
export DATAMIND__EMBEDDING__API_KEY=...
export DATAMIND__EMBEDDING__API_BASE=https://api.openai.com/v1
export DATAMIND__EMBEDDING__MODEL=text-embedding-3-small
```

The standalone MCP transport is also installed in the wheel:

```bash
python -m datamind.mcp
```

### Enterprise integration: official Codex SDK

```bash
python -m pip install -e '.[codex]'
```

```python
from datamind.codex import CodexSession

async def run():
    async with CodexSession(
        workspace="/srv/workspaces/team-a",
        data_root="/srv/datamind",
        profile="team-a",  # selected by the authenticated host
    ) as session:
        result = await session.run("Read the ingestion Skill and import this workspace.")
        return result.final_response
```

Authenticate Codex in the host environment. The integration uses the official
`openai-codex` Python SDK and inherits the configured model unless explicitly
overridden. Each session owns its Codex thread and profile-bound MCP subprocess.
The application owns authentication, tenant authorization, scheduling, budgets
and cancellation. A caller-supplied profile is not authorization.

SDK concurrency does not turn NetworkX JSON into a concurrent remote graph store.
Use local live graph writes with one writer; the workspace coordinator supports
concurrent builds through compare-and-swap publication on one host. Distributed
storage coordination remains separate work.

### Python: direct data tools

```python
from datamind.config import DataSettings
from datamind.data_plane import build_data_plane

async def graph_lookup():
    data = await build_data_plane(DataSettings(), enable={"graph"})
    try:
        return await data.call("graph_neighbors", {"entity": "Project A"})
    finally:
        await data.aclose()
```

`DataPlane.call` validates arguments, applies hooks and records write receipts.
Main-agent planning, semantic extraction and answer synthesis stay outside DataMind.

For custom endpoints and unattended tool authorization, see
[Codex gateway deployment](./docs/CODEX_GATEWAY_DEPLOYMENT.md).

## Dynamic workspaces and consistent reads

1. Start `datamind_operation_begin(mode="write")` and fetch `workspace-ingest`.
2. `datamind_workspace_prepare(path)` returns a source inventory with SHA-256 versions.
3. Codex reads documents and submits `datamind_workspace_publish(path, surfaces,
   graph_sources=[{uri, version, triples}, ...])`. Include an explicit empty list
   for each document without relations. Set `truncated=true` when appropriate.
4. DataMind validates source versions, materializes the selected surfaces privately,
   reuses completed source checkpoints, then publishes one complete revision.
   Failed or conflicting publications preserve the previous head.
5. Start `datamind_operation_begin(mode="read")` to pin the published revision.
   Reuse its `operation_id` across KB, SQL, graph and raw-evidence calls. Concurrent
   publication does not move that reader. Start another operation to see newer data.

Every tool after operation_begin requires the operation_id. Fetching a Skill
persists its version/hash for subsequent receipts in that operation. Skills and
Memory stay live profile context; they are outside the business-data snapshot.

The local coordinator supports SQLite, NetworkX, and local Chroma/LanceDB.
Snapshots are full materializations with per-source compute reuse; this does
not promise constant storage or incremental physical writes. Retained revisions
can be inspected or rolled back with the Python `Workspace` API.

Independent `kb_add_path`, `db_import_path`, `graph_replace_source` and explicit
triple writes remain available. They do not modify published snapshots. For graph
source replacement, Codex submits triples plus the expected source hash; validation
happens before retracting old support. `graph_forget_source` removes only that
source's managed support and preserves the original file and other evidence.

## CLI, HTTP and UI

```bash
pip install -e '.[codex]'
datamind info                       # no model startup
datamind ask "Who owns Project A?"   # Codex with read-only data access
datamind store "Import this file"    # same Codex host, authorized writes
datamind chat                       # /store permits writes, /new resets context
python -m uvicorn datamind.server:app --host 127.0.0.1 --port 8000
```

The UI and `/api/ask`, `/api/store`, `/api/chat` call the same Codex host. Read
requests pin the published revision at admission, and tools enforce that binding
across calls. With no published workspace, a read request explicitly uses live
stores. The API's optional `revision` selects a retained historical snapshot.
Source uploads, direct KB administration, graph status and scoped Memory reads
remain available. Receipt IDs, request IDs and data-tool evidence are returned
from actual data calls, including when Codex uses code mode.

## Migration to 2.0

The old `datamind.agent.build_datamind`, StoreAgent/RetrieveAgent classes, role
prompts, and Claude SDK/CCR agent adapter have been removed. Use `CodexHost` for
natural-language requests and `build_data_plane` for direct data operations.
`DATAMIND__CODEX__*` configures the main agent; the old native/sdk backend switch
no longer selects a product runtime. See [2.0 migration](./docs/LEGACY_RUNTIME.md).

The plugin's direct-tool workflow, source checkpoints, immutable revisions,
source-scoped graph replacement and rollback remain the same. Prepared graph
assertions are supplied by Codex; direct Workspace builds do not implicitly
start a model. Optional explicit text utility clients remain supported for
custom pipelines and deterministic experiment fixtures.

## Verify

```bash
pip install -e '.[dev,codex]'
pytest -q datamind/tests plugins/datamind-context/tests
python -m datamind.scripts.verify_sqlite_demo
```

GitHub CI covers Python 3.11/3.12, local versioning/recovery, real MySQL/Dolt,
installed wheels, direct MCP tool execution and official Codex SDK transport.
SDK tests perform an offline app-server handshake and mocked orchestration;
paid model reasoning is not part of CI. Passing tests establishes covered
contracts, not correctness for every possible model response or deployment.

[Benchmark notes](./docs/BENCHMARK_RUNNER.md) · [Changelog](./CHANGELOG.md)

DataMind is released under the [Apache License 2.0](./LICENSE).
