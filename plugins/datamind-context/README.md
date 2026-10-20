# DataMind Context for Codex

This directory is the official Codex adapter shipped with the DataMind repository.
It connects Codex to the current `datamind` v1 runtime over MCP; it does not
contain a second RAG engine, model client, or storage implementation.

## Install from a DataMind checkout

```bash
cd /path/to/DataMind
./scripts/install_codex_plugin.sh
```

The installer creates the Codex local marketplace entry and installs the current
DataMind package into the repository virtual environment. Edit the generated
`.env.datamind` file, or set the `DATAMIND__LLM__*` and
`DATAMIND__EMBEDDING__*` variables before first use.

## What the plugin exposes

- `datamind_ask` — query through RetrieveAgent and return evidence.
- `datamind_store` — write through StoreAgent and return receipts.
- `datamind_use_folder` — ingest a file or directory through the KB ingest tools.
- `datamind_graph_ingest` — build a graph from one or more text files with source provenance.
- `datamind_graph_build_lineage` — build a deterministic file-dependency graph from a workspace.
- `datamind_table_ingest` — import CSV/TSV or Excel sheets into the SQL surface.
- `datamind_build_start` / `datamind_build_freeze` / `datamind_build_verify` / `datamind_build_export` — manage reproducible workspace builds.
- `datamind_surface_ingest` — route documents, tables, and lineage into their appropriate surfaces.
- `datamind_raw_file_read` — read paginated source or extracted document evidence with SHA-256.
- `datamind_build_status` — inspect build state and verify frozen artifacts.
- `datamind_workspace_inspect` — inspect workspace files, hashes, and candidate surfaces before building.
- `datamind_rag_query` — perform direct vector search.
- `datamind_graph_query` — ask a relationship question using graph tools.
- `datamind_remember` — save durable memory.
- `datamind_list_profiles` and `datamind_status` — inspect local state.

For the complete tutorial, configuration reference, and deployment guidance,
see the [DataMind documentation site](https://opendcai.github.io/DataMind-Doc/).


PDF 解析支持可选的 MinerU API 优先适配；未安装或解析失败时自动回退到 `pypdf`。通过 `DATAMIND_MINERU=off` 可强制只使用 `pypdf`，`DATAMIND_MINERU_API_URL` 和 `DATAMIND_MINERU_API_TIMEOUT_S` 可指定 API 地址和超时。

For opt-in immutable workspace updates, use `datamind_workspace_update`, followed
by `datamind_workspace_ask`. The latter pins a published revision throughout the
question, including source evidence. Existing live-profile tools retain their
behavior. Snapshot storage currently supports local SQLite/NetworkX and
Chroma/LanceDB; see `docs/KNOWLEDGE_LIFECYCLE.md` in the repository for boundaries.
