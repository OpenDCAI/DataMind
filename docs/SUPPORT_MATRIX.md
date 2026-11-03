# Codex host and data-provider support

DataMind 2.0 has one shared data plane and one main-agent choice: Codex.

| Entry point | Main runtime | Installation | Data path |
|---|---|---|---|
| Personal local plugin | User's Codex | Plugin installer and base package | Direct MCP tools |
| Python SDK integration | Official `openai-codex` | `pip install 'datamind[codex]'` | `CodexSession` + MCP |
| CLI / HTTP / browser UI | Official `openai-codex` | Same Codex extra | `CodexHost` + MCP |
| Deterministic Python operations | None | Base package plus selected providers | `build_data_plane` / `Workspace` |

The Codex extra currently pins the supported SDK minor version. Authentication,
model access, tool approvals and gateway compatibility are integration concerns;
see [gateway deployment](CODEX_GATEWAY_DEPLOYMENT.md). A successful offline CI
run cannot guarantee availability of a third-party model service.

## Shared capabilities

All Codex entry points use the same validated data tools, operation tokens,
write receipts, path/SQL/audit hooks and workspace snapshots. Query hosting uses
read-only MCP access; write hosting permits business reads and explicit writes.
Codex performs extraction, SQL generation, planning and answer synthesis. The
data layer does not create a second conversation-model client.

KB requires embeddings. SQL/Graph/Skills/Memory can work without embeddings.
SQLite and NetworkX/JSON are local defaults; optional database and versioned
providers retain their own dependencies and deployment boundaries. The current
workspace coordinator uses local, single-host storage. SDK concurrency limits
do not turn it into a distributed coordinator or a multi-tenant service.

## Automated validation

GitHub CI runs Python 3.11/3.12 regression suites, installed-wheel verification,
optional versioning providers, real MySQL/Dolt integration, and an official-SDK
job for offline host/MCP contracts. Required integration jobs reject skips.
Model gateway calls and local embedding downloads are separate live deployment
checks. Native/OpenAI loop primitives remain for controlled benchmark baselines;
they are not selectable CLI/HTTP product backends.
