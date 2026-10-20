---
name: datamind-context
description: Use DataMind from Codex to ingest local files, query RAG or graph knowledge, store facts, and inspect profiles.
---

# DataMind Context

Use this skill when the user asks Codex to use DataMind, ingest a local file or
folder, query indexed knowledge, ask about relationships, remember a preference,
or inspect profiles.

Prefer these tools:

- `datamind_raw_file_read` — read paginated source or extracted document evidence with SHA-256.
- `datamind_build_status` — inspect build state and verify frozen artifacts.
- `datamind_workspace_inspect` before building surfaces from a workspace.
- `datamind_use_folder` to add a file or directory.
- `datamind_graph_ingest` to build only the graph from text files, or pass
  `build_graph=true` to `datamind_use_folder` to build KB and Graph together.
- `datamind_graph_build_lineage` to build file dependencies and provenance from a workspace.
- `datamind_table_ingest` to import CSV/TSV or Excel sheets as queryable tables.
- Use `datamind_build_start`, then ingest/build surfaces, `datamind_build_freeze`, and `datamind_build_verify` before exporting a reusable build.
- Use `datamind_surface_ingest` for one workspace-level routing operation across KB, SQL, and lineage Graph.
- `datamind_ask` for an evidence-backed answer across DataMind surfaces.
- `datamind_rag_query` for direct document search.
- `datamind_graph_query` for relationship and multi-hop questions.
- `datamind_store` for conversational writes that should be routed by StoreAgent.
- `datamind_remember` for an explicit durable preference, decision, or fact.
- `datamind_list_profiles` and `datamind_status` to inspect local state.

The plugin is an adapter to the DataMind v1 runtime. Do not assume that it has
an independent database, index, or memory store. Keep user source files intact;
DataMind's ingest tools copy or index them according to the active profile and
path safety policy.


PDF 解析支持可选的 MinerU API 优先适配；未安装或解析失败时自动回退到 `pypdf`。通过 `DATAMIND_MINERU=off` 可强制只使用 `pypdf`，`DATAMIND_MINERU_API_URL` 和 `DATAMIND_MINERU_API_TIMEOUT_S` 可指定 API 地址和超时。

For a dynamic workspace requiring coherent cross-surface reads, use
`datamind_workspace_update` and then `datamind_workspace_ask`. Pass a returned
revision for repeatable historical questions. Legacy folder ingest does not
publish an atomic KB/Graph generation. The published snapshot path is currently
local-provider only; report unsupported-backend errors without switching storage.
