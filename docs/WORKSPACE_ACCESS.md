# Optional retrieval and reusable evidence

The main Codex agent chooses whether the data layer helps. A file conversion or
small task with known paths can use native tools. Indexed discovery, SQL joins,
relationship queries and repeated access to parsed sources can use DataMind.
The plugin and official SDK host use the same instructions and data tools.

## Batch evidence for analysis code

New publications share canonical table artifacts between SQLite snapshots and
incremental Dolt storage. Raw values and physical SQL column names remain intact;
`_source_row` contains the original 1-based row as TEXT. The catalog advertises
the SQL dialect (`mysql` for Dolt), proposed/confirmed header/data bounds and field
labels. Header suggestions are conservative; multi-section sheets require review.
No business units or numeric/date conversions are guessed.

`workspace_publish` and `Workspace.update` accept optional `table_mappings`:

```python
table_mappings = [{
    "uri": "/workspace/sales.xlsx", "version": "sha256:<prepared SHA-256>",
    "sheets": [{"sheet": "Orders", "header_row": 3, "data_start_row": 4,
                "data_end_row": 102,
                "fields": [{"column": "col_2", "name": "Revenue", "unit": "USD"}]}],
}]
```

These describe a data view, without deleting titles, notes or raw rows. Mappings
are validated against the source hash, sheet, columns and row bounds. Unchanged
sources inherit mappings; content changes invalidate them. Explicit `sheets=[]`
clears a source's mappings. Mapping-only changes reuse parsing; they do not change
SQL table contents or force unrelated sources to recompute. A new build is needed
to add reliable cell lineage to older table artifacts; existing revisions remain
readable and legacy exports explicitly flag unavailable exact row lineage.

Use one batch call for related tables/text, rather than moving rows through the
model context and then parsing the original files again:

```python
bundle = await reader.call("workspace_export_bundle", requests=[
    {"path": "sales.xlsx"},  # all sheets; specify sheet/columns to narrow
    {"path": "meeting.md"},
])
import json
from pathlib import Path
manifest = json.loads(Path(bundle["manifest"]["path"]).read_text())
orders = next(item for item in manifest["items"] if item.get("sheet") == "Orders")
rows = [json.loads(line) for line in Path(orders["path"]).read_text().splitlines()]
# Reuse rows to calculate and generate the final artifacts.
# Each row's _source_row plus a field's source_column resolves e.g. C4.
```

MCP uses `datamind_workspace_export_bundle` with an `operation_id`. Exports contain
JSONL tables/text and a manifest with source hashes, workspace revision, field
keys, column letters and explicit units. Data view uses the interpreted header
and inclusive source row bounds; `view_mode="raw"` retains all stored rows after
row 1, which is separately retained as `raw_headers`. Ambiguous headers fall back
to row 1 and `needs_confirmation`; notes/blank rows are not silently removed.
SQL continues to contain raw rows: use confirmed bounds and numeric casts on
`_source_row` before aggregating. SQL aggregate-cell lineage is not automatic;
retain the expression and contributing tables/ranges when reporting evidence.

Default limits are 20 requested sources, 100 expanded assets, 100,000 rows and
32 MiB including the manifest; exceeding a limit fails rather than returning a
partial successful bundle. Files are content-addressed and reused across exports
and revisions when their bytes match. A corrupt cached output is regenerated.
No embedding/model/SQL call is needed for cached table/text exports.

The default cache is `<MCP cwd>/.datamind-cache/<profile>/exports`. Set the trusted
server environment variable `DATAMIND_EXPORT_ROOT` to relocate its profile-scoped
cache. This is a local file interface: agent code must share the filesystem or
mount the export root at the same path. It does not imply remote download support.
Export files are task/host-owned derived copies, not managed rollback targets;
remove that cache when the task finishes if persistent reuse is unnecessary.
Keep final deliverables separate. Source retention/GC still follows Workspace
leases and policy; an existing export is a copy, not a promise to retain a revision.

## Discovery and reads

Start `datamind_operation_begin(mode="read")` only when accessing DataMind.
The response contains `operation_id`, `revision`, `mode`, and `workspace` with
the source root, source count, selected surfaces and truncation count. It no
longer embeds the internal manifest. Python diagnostics can still explicitly
call `Workspace.manifest(revision)`.

Fetch `workspace-retrieve` by name when its procedure is needed. `skill_list` is
for discovery, not a required step before an already-known `skill_get`.

```python
from datamind.mcp import execute

op = await execute("datamind_operation_begin", {"mode": "read"})
token = op["operation_id"]
try:
    catalog = await execute("datamind_workspace_catalog", {
        "operation_id": token, "kind": "tables", "source": "sales.xlsx",
        "sheet": "Orders", "column": "amount", "limit": 10,
    })
    # Inspect the returned physical table name/column mapping before writing SQL.
    asset = await execute("datamind_workspace_read_asset", {
        "operation_id": token, "path": "sales.xlsx", "sheet": "Orders",
        "offset": 0, "limit": 20,
    })
finally:
    await execute("datamind_operation_close", {"operation_id": token})
```

`workspace_catalog(kind="sources")` searches published paths. `kind="tables"`
searches physical names, file paths, sheet names and columns. `query` uses
case-insensitive whitespace-separated terms; `source`, `sheet`, and `column`
are case-insensitive substring filters. Results include the source version and
workspace revision, with `total_count`, `next_offset` and `truncated`. Limits
are 1–100, default 20. Wide column lists show at most 50 entries; a column filter
puts matching columns first. Use `db_describe_table` for the complete SQL schema.

The catalog covers published sources, not all files on disk. An empty result
does not prove a raw file is absent. Same-named files at different paths retain
distinct identities. Do not silently mix current source files with historical
query results; use a pinned source read or disclose the changed evidence.

`workspace_read_asset` accepts a published URI or exact workspace-relative path.
For text, `offset`/`max_chars` count characters. For a sheet, `offset` counts data
rows, `limit` is 1–200, and `columns` selects SQL column names. Rows preserve the
import's stored TEXT/NULL representation; no unit or numeric meaning is inferred.
Responses include source version, pipeline hash and explicit truncation/next
offset fields. Discovery and parsed reads do not open an embedding or SQL backend.

New builds retain extracted text with their immutable source artifacts; table
reads reuse canonical table objects or legacy checkpoint records. Old text
artifacts without cached extraction fall back to parsing their frozen source
and return `reused=false`. No index rebuild is required just to read an older
revision. New incremental tables retain original header labels alongside SQL
column names; older artifacts may only retain SQL names. XLSX import reads actual
cells without trusting the workbook's declared dimension; original bytes and
hashes are preserved.

KB search overfetches a bounded candidate pool, then merges identical passage
text. Each result retains its primary source plus up to 20 duplicate source
locations and a truncation indicator. The count is for retrieved candidates,
not the entire corpus. Source/tenant metadata filters and historical versions
apply before merging. Different text or internal whitespace is not collapsed.

## Evaluation controls

`benchmark.workspace_protocol.run_recoverable_turn` consumes SDK events and
resumes transient failures in the same thread, with bounded retries inside one
wall-clock deadline. Use the same recovery settings for all methods. Attempts
and errors remain visible; exhausted infrastructure failures are identified
separately from task quality. Every request, including retries, still belongs
in usage accounting. The evaluation adapter must connect its event logger and
request-ID usage collector; the helper does not estimate missing token usage.

`source_audit` checks reference/workspace bytes and flags suspicious Python
syntax labels for manual review. Flags never rewrite official scores. Keep
metadata, rubrics and expected answers out of builders and task agents. Retain
official scores alongside validity flags and any separately defined human or
blind comparative assessment.

For a routing ablation compare raw Codex, optional DataMind, and a separately
labeled retrieval-guided condition over identical source bytes and budgets.
Use repeated paired trials and report completion, quality, uncached/cached/output
tokens, useful data calls, native operations and build cost separately. A small
smoke result establishes functionality, not general performance superiority.
