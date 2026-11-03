# Query intent and final answer columns

DataMind's MCP SQL tools require an explicit query intent in every profile,
including ordinary Codex hosting and standalone MCP/plugin use. The main model
interprets the user request; the data layer checks SQL output mechanically.
There is no extra planning model, benchmark-specific column rule, or reference
answer access.

## Tool workflow

1. Inspect source/table metadata as needed. Metadata tools need no intent.
2. For intermediate SQL, pass `intent` with `purpose` (`explore` or `verify`),
   `question` (the current subquestion), and `return_columns` (exact ordered SQL
   output labels). Intermediate evidence may include additional fields.
3. Before final-answer SQL, call `db_answer_schema` with `question`, `columns`
   (`name` and `requirement` for each), and `row_shape` (`records` or `scalar`).
   Distinguish requested outputs from fields used to filter, join, group or sort.
   For example, a count of employees in Support requests a count, not a department
   column. A full-table request can explicitly request every column.
4. Use the returned `schema_id` with `purpose: answer` in query/export intent.
   `return_columns` must exactly match that schema. Use explicit SELECT outputs
   and AS aliases for expressions. `COUNT(*) AS count` is supported; final
   `SELECT *` is rejected. Exploration stars are checked after cursor expansion.
5. `db_export_sql` returns a complete managed CSV with `answer_validated`,
   `answer_schema_id`, `answer_schema_revision`, and the query intent in its
   existing metadata. Check completeness before consuming it as an answer.

MCP names have the `datamind_` prefix. No separate planning round is added to each
query; intent travels with the existing tool call. Declaring the final answer is
one additional tool call, with further calls only if it needs revision.

An inconsistent projection fails before SQL execution when statically resolvable.
Cursor columns are checked before row fetching/CSV writing as well. Errors are
returned through the existing MCP error path so the same agent can correct the
query. No automatic column deletion, deduplication, sorting, or unit conversion
is performed. Row limits and query deadlines retain their existing behavior;
truncated final answers are rejected. `scalar` requires exactly one data row.

## Scope and revisions

Definitions persist in `storage/query_results/answer_schemas.sqlite3`, beside
managed query results. They are request data, not runtime logs. The key is the
profile plus host request ID; standalone MCP uses its operation ID. Different
concurrent requests and profiles cannot reuse each other's definitions.

Repeated identical declarations are idempotent. Changing the question, columns,
or row shape requires `revision_reason` explaining the user-requirement change;
it creates a new version and invalidates previous final exports. Do not broaden
a definition merely to silence a SQL mismatch.

The reusable final-file validator is:

```python
from datamind.capabilities.db.query_contract import validate_answer_export

metadata = validate_answer_export(
    result_dir, result_id, profile=profile, scope=request_id,
)
```

It checks the current request-scoped schema, complete/untruncated status, file
hash, exact header and column order, every row's width, and row count. DataSpace's
prediction copier calls this function; its scorer and reference answers do not
participate in defining the schema.

## Python compatibility and limits

### Final validation before closing a host session

`CodexHost.query`, `ingest`, and `stream` locally validate `final_contract` (JSON
Schema) before emitting `done`. A read-only `final_validator` callback may also
check application-specific artifacts. Managed SQL CSV consumers can reuse:

```python
from datamind.capabilities.db.query_contract import (
    MANAGED_ANSWER_SCHEMA, ManagedAnswerExportValidator,
)

result = await host.ingest(
    question,
    final_contract=MANAGED_ANSWER_SCHEMA,
    final_validator=ManagedAnswerExportValidator(
        settings.data.storage_dir / 'query_results', settings.data.profile,
    ),
)
```

The validator receives parsed JSON (or raw text when no schema is supplied),
plus `request_id` and `workspace_revision` keyword arguments. Return `None` on
success, or raise `ValueError`/`CapabilityError` with a corrective description.
Async validators are supported; synchronous validators run in a worker thread.
They must not write data: cancellation cannot forcibly stop a synchronous thread.
Unexpected programmer errors and cancellation propagate instead of being hidden
behind model retries.

Managed validation checks exact ID format, file/metadata existence, request and
profile ownership, current answer-schema version, workspace revision, complete
untruncated status, bytes/hash, column headers and row shape. Missing/corrupt
artifacts are failures, not successful submissions. A `BLOCKED: ...` string is
not an export ID. Artifact validation uses the schema database in read-only mode.

On failure the host keeps the same SDK session, thread, request and operation
alive, supplies the rejection plus bounded recent failed-tool previews, and lets
the existing main model correct its tool arguments/output. It does not rerun
successful writes itself or create another planner. At most **three host
continuations total**, shared with outstanding-tool recovery, are allowed;
all remain within the original session deadline. DataSpace's outer 1800-second
whole-task deadline is unchanged. Exhaustion raises `FinalAnswerContractError`;
there is no invalid `done` or fabricated prediction. Streaming text is tentative
until `done` passes validation.

Events include `final_validation_failed`, `final_recovery`, and
`final_validation_passed`; completion recovery metadata records
`final_validation_retries` and `host_continuations`. Diagnostics use the configured
log directory. Ordinary free-form prose without a contract remains compatible;
this does not prove semantic correctness of an answer.

### Mistyped database identifiers

Native SQLite tools reject unknown `database_id`/`database_ids` before accessing
data. Errors include the supplied invalid IDs, exact available IDs with their
source URIs in the current profile/operation, total count and truncation indicator.
The preview is limited to 20 databases; `db_list_databases` returns the full list.
Copy a complete ID without shortening it. Correct the arguments instead of
reimporting a file. There is no fuzzy automatic selection or cross-profile lookup.
The same error details are retained in MCP responses and diagnostic logs.

### Existing Python calls

Low-level `DBService.query_sql/export_sql` and existing Python tool calls without
intent remain compatible. Python callers can opt in by supplying intent, and can
require it by reusing a `RequestContext` whose `extra` contains
`query_contract_required=True`. Bind the same context or pass it to each
`DataPlane.call`; without a host/operation ID the context's stable `trace_id`
provides the scope. This allows related calls to share one answer schema.

KB/Graph search returns evidence with provenance rather than a final SQL table;
these existing results are preserved. When evidence is turned into a final table,
the same SQL export contract applies. Free-form prose is not a validated CSV.
The contract guarantees conformity to the model-declared columns, not semantic
correctness: choosing the wrong source, entity identifier, filtering condition,
or answer definition can still produce a wrong answer. A column declaration is
not a substitute for understanding the user's question.
