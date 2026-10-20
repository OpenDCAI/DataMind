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

The plugin and official Codex SDK integration use the same data layer and tools.
Neither launches an internal StoreAgent or RetrieveAgent. Those older APIs remain
available for compatibility; `agent.backend=sdk` there means **Claude Agent SDK**,
not the official Codex SDK used by the new integration.

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
key. Embedding models are separate from the main Codex model. Legacy model-backed
retrieval strategies such as `multi_query` require the compatibility runtime;
use `simple` or `hybrid` with direct tools.

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

## Compatibility and migration

New plugin tools replace `datamind_ask/store/graph_query/workspace_ask`: the main
Codex agent now calls direct tools and writes the final answer itself. Use
operation_begin plus workspace_publish for coordinated updates instead of the
old model-extracting workspace_update MCP tool.

`datamind.agent.build_datamind(Settings())`, the existing CLI, FastAPI/UI and
native / Claude SDK loops remain legacy compatibility entry points. They still
require the configured DataMind model key and may invoke StoreAgent/RetrieveAgent.
They are not the new Codex SDK architecture. Existing provider, benchmark and
recovery APIs remain available. See [legacy runtime](./docs/LEGACY_RUNTIME.md).

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
