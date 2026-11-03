# DataSpace input adaptation

## Default evaluation flow

`run_task(phase="run")` now gives the question and task-local file inventory to
one CodexHost write-enabled session. The main model chooses DataMind tools on
demand; it need not build every file or publish KB/Graph/DB. `surfaces` limits
available tools, not required build targets. Direct `db_import_path` accepts
table JSON with typed cells and registers SQLite read-only. It imports only the
selected file and does not embed it. Empty JSON records without a schema fail
explicitly in this direct import. Video frames/OCR/ASR may be used independently
of publication. Final exports must belong to this task profile and host request,
be complete, and pass SHA-256 validation. No gold/schema is supplied to the model.

Explicit `phase="build"`/`phase="query"` and the synthetic `sql=` verifier retain
the versioned workspace path. The complete-workspace mapping and coverage checks
described below apply to that explicit workflow, not the default evaluation.
An external benchmark runner is responsible for providing the official
execution isolation, task deadline and scoring. This entry does not bundle
that infrastructure or provide an operating-system sandbox.


The explicit `python -m datamind.dataspace` test entry opts into three rules:
structured `{ "table": "...", "records": [...] }` JSON is a table input, and the
SQL result row cap is disabled, and standalone SQLite inputs are registered
by reference without copying their files or rows. Ordinary DataMind configuration keeps JSON
as a document and `db.row_limit=1000`. No process-wide environment variables
are changed by the test entry. Chinese column names and SQLite file ingestion
are available in ordinary workspaces too.

DataSpace inputs are assumed immutable for an evaluation run. Initial ingestion
records source fingerprints for provenance and cache identity, but build completion
and queries do not rehash sources to detect version changes. The runner checks
input-path coverage without another fingerprint pass. Task/profile boundaries,
read-only SQL, bundle allowlists and final prediction integrity checks remain enabled.

## Data flow

| Input | General behavior | DataSpace test behavior |
| --- | --- | --- |
| CSV / TSV / XLSX | DB tables; retain Unicode column names and positional original-name mappings | Same |
| SQLite `.sqlite` / `.sqlite3` | Consistent native SQLite snapshot, registered in DB | Read-only registration of the original standalone file; no file or row copy |
| JSON table envelope | Existing document ingestion | Complete typed DB rows, schema cards in KB, and long text cells in KB |
| Other JSON / Markdown / PDF | Existing document ingestion | Same |
| MP4 | Existing video bundle, KB and Graph workflow | Same; automatic image viewing is unchanged |

For structured JSON and SQLite, selecting Graph adds deterministic file-to-table,
table-to-column and declared foreign-key relationships. Model-supplied semantic
assertions remain optional for these structured sources. A matching field name
alone is not treated as a foreign key. Ordinary CSV graph routing is unchanged.

DataSpace JSON is read in streaming passes. Values remain numbers, strings or
NULL, rather than being converted uniformly to text. Numeric-looking strings
such as `"000021"` stay strings. Duplicate rows remain duplicate. Nested JSON
cells and integers outside signed 64-bit range fail explicitly; they are not
silently flattened or rounded. An empty `records` array still gets a schema
card and an empty managed table with provenance columns, without inventing
business columns. JSON text cells of at least 160 characters are split into KB
chunks with source/table/column/row locators; short values remain in DB. KB is
not a copy of every numeric cell, and no model invents semantic relationships
as part of the deterministic adapter. Codex may supply additional explicitly
evidenced relationships through the existing `graph_sources` interface.

## Column and source discovery

`db_list_databases` discovers registered SQLite IDs, source versions, storage
mode and original table names. Pass `database_id` to `db_list_tables`,
`db_describe_table`, `db_query_sql`, `db_query_nl` or `db_export_sql` to use a
native source. Omitting it selects managed CSV/JSON tables, including
source-scoped `ws_...` names. `db_describe_table(table).source_metadata` exposes the original source
file, table name (`sheet`), column mapping, types, original declared types,
primary keys, foreign keys and row count when present. The workspace manifest
contains the same catalog. Direct CSV imports also persist their original
column mapping. Each mapping entry is positional:

```json
{"ordinal": 1, "original_name": "收入（万元）", "name": "收入（万元）"}
```

Headers are trimmed and kept as quoted Unicode identifiers. Blank, duplicate,
control-character or reserved `_source_file` headers receive a stable fallback;
long names are limited to 64 characters and collisions are disambiguated. The
original header remains in `column_mapping`, including duplicate originals.
Use dialect-appropriate quotes, e.g. SQLite `SELECT "收入（万元）" FROM "ws_..."`.
CSV/XLSX values retain the existing TEXT/NULL import semantics. JSON has a
synthetic `_source_row` (or a nonconflicting variant) to locate long-text KB
hits. `_source_file` identifies the source for managed tables. Native SQLite keeps
its exact columns; provenance lives in the registry and Graph evidence.

## Native SQLite registration

With the standard policy, Workspace builds and direct `db_import_path` create
consistent SQLite snapshots with SQLite's backup API, including committed WAL data. Native schema
objects, views, indexes, constraints, collations and trigger definitions remain
in the file. Triggers are not replayed into a different database. Original
scalar types, BLOBs, NULLs and row multiplicity are preserved. No per-table CSV
cache or managed table copy is generated for SQLite.

The DataSpace policy instead registers the original absolute file path in both
Workspace builds and direct imports. It
requires an independently readable database with no nonempty WAL or rollback
journal. The registry stores database ID, source URI/version, SHA-256, workspace
revision, storage ownership, full schema objects and column/foreign-key metadata.
DataSpace native queries and KB/Graph reads do not validate source versions or
checksums again. Keep inputs immutable for the whole run;
read-only connections alone do not prevent a different process changing files.
The test runner does not provide an operating-system sandbox.

Ordinary native queries retain before/after validation of the selected snapshots.
A bounded process-local cache reuses successful hashes while device/inode, size,
nanosecond modification/change times and WAL/journal state match. Changed metadata
invalidates the check; unrelated KB/Graph tools do not scan databases. This cache
is not a substitute for OS-level immutability against a privileged writer.

Native connections use read-only SQLite URIs, query-only mode, SQL guards and a
query deadline. Views are queried directly. Available built-in virtual modules
such as FTS5 work without flattening; a missing custom module is reported as a
schema/query error rather than silently skipping the table. Extensions are not
loaded automatically. `.db` remains unregistered because its format is ambiguous.

For joins across registered SQLite files, supply `database_ids` to
`db_query_sql`/`db_export_sql`. Refer to an attached source as
`"database_id"."original_table"`. These IDs resolve only through the current
registry; the model cannot attach arbitrary paths. Omitting the primary
`database_id` joins these sources with the managed SQLite CSV/JSON database.
SQLite's limit of ten attached databases per query applies. Joining native
SQLite with a managed Dolt database in one statement is not implemented; query
those providers separately. Native SQLite itself does not get converted to Dolt.

The manifest retains source-scoped table catalog keys for evidence identity;
for native tables use its `database_id` and `native_table` fields in SQL tools,
not the catalog key as a physical table name. KB schema cards and Graph locators
include the routing ID. Only schema and explicit declared relationships are
extracted automatically; views are not treated as inferred foreign keys.

Use versioned Workspace publication for coordinated updates and retained
snapshots. Referenced DataSpace revisions do not retain historical source bytes:
changing a source during evaluation is outside the immutable-input contract and
is no longer detected by reference reads. Rebuild for a new input set.
GC never owns or deletes referenced inputs. Normal snapshot revisions remain
readable after the original file changes or disappears. Existing pre-native
revisions remain readable through Workspace; rebuild old DataSpace test
workspaces to use this new registration path.

Direct `db_import_path` keeps native names (no `table_prefix`). For an already
registered source use `if_exists="replace"`; append does not concatenate SQLite
databases. `db_delete_path` unregisters a native source without deleting its
original file. Publication replaces the registry entry as a unit, so deleted tables
cannot linger from the old file. `db_update_path` remains the CSV/Excel interface.

## Setup and execution

From the repository root, in your activated Python environment:

```bash
python -m pip install -e '.[dataspace]'
```

The `dataspace` extra adds `ijson`; regular SQLite/CSV ingestion needs no new
optional parser. Use a separate runtime root for tests. Each task receives a
separate `dataspace_task_N` profile, workspace and agent working directory.
Only `input/task_N/context` is registered as the input root. The agent working
directory is in the runtime profile, not the original dataset. The test entry
reads the question from that task's `task.json`; it never loads `output/`,
reference answers or evaluation configurations. The model prompt also forbids
other tasks and Memory. This is application-level task separation, not an OS
security boundary against an adversarial model with unrestricted filesystem tools.

A DB-only build validates ingestion without model/API calls:

```bash
python -m datamind.dataspace \
  --dataset-root /path/to/DataSpace-Benchmark \
  --runtime-root /path/to/dataspace-runtime \
  --task task_1 --phase build --surfaces db
```

The default on-demand run uses the existing Codex host. Configure embeddings
if KB indexing or search is needed, and the optional OCR/ASR providers for
video processing. Configure these as for ordinary DataMind; no credentials
are bundled by this change.

```bash
python -m datamind.dataspace \
  --dataset-root /path/to/DataSpace-Benchmark \
  --runtime-root /path/to/dataspace-runtime \
  --task task_1 --phase run
```

`--phase build` publishes inputs and `--phase query` solves against the published
revision. The default `--phase run` uses on-demand tools without requiring a
full Workspace build. With an explicit `--sql`, `--phase run` instead performs
the build and deterministic query workflow. A model build must publish a new revision
with the requested surfaces and complete current inventory; an old workspace
cannot silently stand in for a failed build. `--sql 'SELECT ...'` substitutes an
explicit read-only query for model solving and is useful for offline checks.
Use `--database-id` with `--sql` to select a native source. It is not a
benchmark prediction generated by the agent.

## Complete results

The task setting `db.row_limit=None` reaches both the host and MCP subprocess.
It applies to direct and generated SQL. Explicit SQL `LIMIT`, query timeouts,
read-only guards and revision restrictions remain in effect. Normal runs still
use the configured 1,000-row default. Long-running benchmark queries may need a
larger `DATAMIND__DB__QUERY_TIMEOUT_S`; disabling a row cap does not disable timeouts.

For final answers the test entry requires `db_export_sql`. It streams results
to `storage/dataspace_task_N/query_results/<result_id>.csv` and returns only a
small preview plus the full row count, hash and pinned revision. The runner
checks `complete`, `truncated`, revision and hash before creating:

```text
<runtime-root>/predictions/task_N/prediction.csv
```

This avoids mistaking a shortened model/tool preview for the full answer.
The model still chooses the SQL and is responsible for the question's columns,
ordering and values. This adaptation does not establish benchmark accuracy.

Direct filesystem ingestion does not use the web uploader, so its existing
25 MiB HTTP upload limit does not apply. The uploader itself is unchanged.
Existing published revisions stay readable; rebuilding creates a new revision
and applies the new column/schema rules without renaming old snapshots in place.

## Offline verification

```bash
python -m pytest -q datamind/tests plugins/datamind-context/tests
python scripts/verify_dataspace_inputs.py \
  --dataset-root /path/to/DataSpace-Benchmark \
  --runtime-root /path/to/dataspace-runtime \
  --task task_1
```

The verifier requires an existing DB build. It compares managed CSV/JSON rows
and native SQLite query results with the originals, verifies SQLite reference
ownership, and checks a 1,505-row export.
The additional tests cover policy isolation, Unicode/duplicate headers, typed
SQLite and JSON values, BLOBs/NULLs/empty tables, retained revisions, schema
Graph evidence, explicit migration, full CSV export, mocked Codex orchestration,
and rejection of builds that did not publish. The mocked orchestration test
checks integration contracts; it does not call a real model.
