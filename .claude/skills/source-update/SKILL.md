---
name: source-update
version: 1.2.0
description: Guide Codex to update or retract DataMind source data while preserving other evidence and published workspace consistency.
keywords: [update, delete, reingest, 更新, 删除, 同步, 版本]
---

# Source updates

Use a write operation and fetch this Skill with the same operation_id. MCP tools
have the `datamind_` prefix; Python DataPlane tools omit it.

- For a published workspace, prepare its original directory, read sources and
  extract relationships with Codex, then publish a complete source inventory via
  `workspace_publish`. Omitted/deleted files disappear from the new snapshot.
  In incremental mode, only new or changed documents need `{uri, version, triples}`, including empty
  triples; snapshot mode still needs every document. Hash mismatch, invalid assertions or a competing publication must
  retain the old head. Completed source checkpoints can be reused after restart.
- For independent live data, `kb_add_file` replaces the same document;
  `graph_replace_source(path, expected_version, triples)` replaces only that
  source's graph support. Extract triples yourself. Invalid input or a stale
  source hash must not retract existing support. Use `graph_forget_source` to
  retract a removed source without deleting its file or other sources' evidence.
- Use `db_update_path` for source-scoped table replacement. `db_delete_path` and
  `kb_delete_file` also remove a managed file: use them when deletion is requested.
  Never replace an entire shared table to change only one source.
- Check every receipt result. Successful KB refresh does not prove KG refresh.
  Published revisions and independent live stores are separate; live writes do
  not appear in snapshot readers. Start a new read operation after publication;
  existing read operations deliberately retain their original revision.

Use `workspace_rollback(revision, expected_revision)` for coordinated rollback
to a retained revision; latest retention may already have pruned older targets.
Close finished operations with `operation_close`. Per-surface
rollback does not roll back the workspace. Conflicting supported claims should
remain attributable; insertion order does not decide truth.
