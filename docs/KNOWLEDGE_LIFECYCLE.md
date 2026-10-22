# Knowledge lifecycle

Both the enterprise Agent SDK deployment and the personal Codex deployment use
this knowledge contract. They can use different storage and scheduling providers.

## Graph content and evidence

The KB keeps document passages; the DB keeps typed records and exact values; the
KG connects entities, business relationships, and source artifacts. Prefer a small
application-specific relation vocabulary (`relation_types` on graph ingest).
Do not infer `depends_on` from a filename mention or schema similarity: those are
`mentions` and `schema_overlap` candidate links, not proven dependencies.

Entity IDs remain caller-provided strings for compatibility. Resolve aliases
using explicit domain identifiers; do not merge people by name alone. Extracted
triples can carry subject/object types. Explicit triples can carry evidence.

Each `GraphEvidence` records a logical `source_id`, immutable `source_version`,
locator in extracted text, method (`explicit`, `extracted`, or `inferred`), and
processing version. A file-derived logical ID is based on its canonical URI;
a rename is currently a new identity. A content hash is a version, not identity.
For binary documents, character offsets refer to extracted text, not file bytes.
A graph edge supports multiple evidence records; independent document origins
also remain separate parallel edges. A path is a retrieval aid, not proof of
causality. Return source evidence alongside paths and retrieve the matching source
revision before composing a factual answer.

## Document replacement

Extract all bounded document parts before replacing any of that document's old
edges. Empty documents and a valid `[]` retract old document support. Malformed
model output or a failed later part leaves the previous graph revision intact.
Shrinking documents replaces all old parts, including old `#part=N` origins.
Directory re-ingest retracts missing managed documents inside the scanned scope;
explicit/runtime and other-directory relationships are retained.

`truncated` in per-file results means the extraction budget may have limited
coverage. Absence of a relationship in a bounded graph is not evidence that the
relationship does not exist. Model extraction quality still requires evaluation.

## Delivery order and validation

1. Graph content, evidence, complete-document replacement, and bounded access.
2. KB/Graph maintenance, immutable workspace publication and pinned queries;
   integrate and repair PR #28's optional KB/DB version providers.
3. Reusable ingestion Skills over these validated tools, with receipt checks.

Research validation follows the experiment plan: construction quality/cost;
edit/delete and unaffected questions; publication between tool calls; failed
extraction, interrupted builds, retries after unacknowledged writes. Do not label
an in-memory NetworkX store a concurrent multi-process enterprise backend.

## Published workspace API

Codex inventories sources using `datamind_workspace_prepare(path)`, reads new or
changed documents, then submits `datamind_workspace_publish(path, surfaces,
graph_sources=[{uri, version, triples}, ...])` using a write operation. Each input
version must match the inventory SHA-256. Include empty triples when no relations
were found. Snapshot mode requires all documents; incremental mode reuses unchanged
source assertions. Source deletions disappear from the new inventory automatically.

Python callers use `Workspace.update(..., graph_sources=...)` and
`async with workspace.reader(revision) as reader`, then `reader.call(...)` for direct
pinned reads. Query planning stays with the main Codex agent. Legacy internally
generative `workspace_update`/`workspace_ask` are not MCP tools.

The catalog publishes one manifest with fixed KB, DB and graph bindings. A revision
and epoch compare-and-swap rejects stale builds, including rollback ABA. All reads
within an operation stay on that manifest; original source URIs resolve to immutable
stored bytes. A failed build never changes the published head.

See [Workspace storage](WORKSPACE_STORAGE.md) for source reuse, native version
bindings, history retention, reader leases, cleanup and explicit legacy migration.
Direct live imports remain independent; they do not update published revisions.
