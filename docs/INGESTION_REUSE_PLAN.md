# Reusable ingestion and evidence exports

Status: implementation and validation on the unmerged optional-data-access branch.
No merge or production rollout until the user reviews experimental results.

## Problem and scope

Workspace-Bench traces show SQL/asset reads followed by another Excel parse for
analysis and cell evidence. A cached file is useful only if subsequent code can
consume it and resolve its original cells. Codex remains the sole main agent.

1. Share canonical parsed table objects between snapshot and incremental builds.
   Preserve physical SQL columns, original headers, source row numbers and column
   letters. Attach conservative header interpretation; never infer business units
   or silently convert values. Preserve title/notes in the raw representation.
2. Accept optional source-hash-bound table mappings during publication. Codex can
   specify header/data row bounds and field names/units. Validate before publishing;
   retain mappings for unchanged content and invalidate them after source changes.
   Include interpretation changes in source cache keys, independently per source.
3. Add one bounded batch export tool for parsed tables/text. Write JSONL/text plus
   a manifest into a profile-scoped local cache and return compact file handles.
   Exported rows retain source row numbers; the manifest maps fields to original
   columns and pins source hashes/revision. Reuse content-addressed files across
   calls/revisions. Do not send whole datasets through the model context.
4. Keep existing tools/old snapshots readable. Advertise actual SQL dialect and
   mappings in the catalog. Export requires a shared filesystem with agent code;
   remote deployments must mount the configured export root. No new graph backend,
   nested agent, mandatory retrieval path, hosted transfer service or model parser.

## Validation

- Test short title rows, headers below titles, notes, blank/duplicate/non-ASCII
  headers, explicit mappings, invalid/stale mappings, bounds and original cells.
- Test new publish versus open readers/rollback, unchanged reuse, mapping-only
  changes, snapshot and real Dolt, old artifacts, path safety and cache corruption.
- Test MCP schemas and actual official SDK discovery; run existing CI checks.
- First verify the mechanism on task 386 without benchmark answers/rubrics at
  ingestion: independent native values and source cells must match exported data,
  then execute analysis directly from the export with original parsing disabled.
- Before claiming end-to-end benefit, freeze code and protocol, compare the same
  tasks/budgets with fresh task caches, separate querying from judging, retain
  build/query/judge usage and all failed runs. Previously inspected tasks are
  development evidence, not a held-out superiority claim. Keep PR unmerged.

## Limits

Single-row header suggestions cannot establish complex business meaning. Ambiguous
or multi-section sheets require explicit mappings or raw inspection. SQL aggregate
lineage is not inferred automatically: retain the SQL expression, selected source
tables and row ranges when generating evidence. JSONL preserves existing source
string/NULL semantics; numeric/date conversion belongs to explicit analysis code.
