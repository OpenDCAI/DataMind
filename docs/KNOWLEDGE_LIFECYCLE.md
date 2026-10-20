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
