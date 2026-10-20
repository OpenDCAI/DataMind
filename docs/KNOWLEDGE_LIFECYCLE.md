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

```python
from datamind.config import Settings
from datamind.workspace import Workspace

workspace = Workspace(Settings())
published = await workspace.update(
    "/absolute/path/to/source", surfaces=["kb", "graph", "db"],
    relation_types=["owns", "depends_on", "requires", "cites"],
)
async with workspace.reader(published["workspace_revision"]) as reader:
    answer = await reader.query("Which projects depend on the changed procedure?")
    # Direct read tools also remain pinned, even if another process publishes.
    edges = await reader.call("graph_neighbors", entity="Project A")
```

`workspace_update` is also a StoreAgent tool in native and Agent SDK backends.
The Codex adapter exposes `datamind_workspace_update` and
`datamind_workspace_ask`. Legacy `datamind_use_folder` and normal live queries
retain their unversioned semantics; a workspace update publishes a separate
snapshot and never silently replaces those live stores.

Every revision contains copied source bytes, private capability storage, a
source inventory with stable URI-based IDs/content versions, snapshot bindings,
and a processing fingerprint. The SQLite catalog publishes by comparing the
current head with the build's starting head. Concurrent stale builds fail
publication rather than overwriting newer state. A reader owns one revision for
its lifetime; original source URIs in raw-evidence reads resolve to copied bytes.
Workspace inspection is also restricted to the pinned source copy.

To return to a retained complete generation:

```python
workspace.rollback(old_revision, expected_revision=workspace.current_revision)
```

Completed-source checkpoints include passages, vectors, extracted relationships,
and parsed tables. A new build after process termination reuses these checkpoints;
it does not rerun model calls for already completed sources. A failed source is
retried in full. Index materialization currently writes a new full snapshot: this
is file-granularity compute reuse, not dependency-aware sub-file maintenance or
constant-cost storage. Old snapshots/checkpoints are retained; automatic GC and
exactly-once external writes are not provided.

The shipped coordinator supports one-host concurrent readers/builders using local
NetworkX, SQLite, and Chroma/LanceDB storage. It rejects external DB DSNs and
unsupported graph providers before building. Multi-host scheduling, tenant
identity/authorization, distributed locks, remote snapshot providers, and service
capacity/load validation remain deployment work. They do not follow from selecting
the Agent SDK backend. Snapshot readers should be reused per request/session as
appropriate; this API currently opens capability services when a reader is opened.

## PR #28 integration

LanceDB versioning is opt-in with `pip install 'datamind[versioning]'` and
`DATAMIND__KB__VECTOR_STORE=lancedb`. CocoIndex additionally requires
`pip install 'datamind[cocoindex]'` and `DATAMIND__KB__INGEST_MODE=cocoindex`.
Personal installations keep Chroma/direct ingestion by default. Dolt requires
`pip install 'datamind[dolt]'` and a separately installed Dolt binary/service.

History records survive process restart; historical LanceDB reads use private
table handles and do not change the writer's checkout. Re-adding a chunk ID is an
upsert. External source copies are keyed by origin so editing one source replaces
its own managed copy rather than accumulating content-hash filenames. Dolt
historical SQL rewrites physical tables using an AST, covering joins and CTEs
without changing string literals. Dolt and external MySQL integration tests need
their services and must not be counted as validated when skipped.

## Ingestion Skills

`workspace-ingest` and `source-update` ship in both the checkout and wheel skill
catalogs. StoreAgent has explicitly allowed Skill lookup and input-inspection
reads; business KB/DB/Graph query tools remain RetrieveAgent-only. Skills guide
routing, schemas and receipt inspection; persistence invariants stay in tools.
A successfully fetched Skill contributes its name, declared version and content
hash to subsequent write receipts in that store turn. Fetching records procedural
context, not proof the model followed every instruction. Receipt context does not
leak into another store turn. Partial directory failures produce failed receipts
and are not cached as successful imports.

Pinned readers retry only transient I/O/timeouts and retryable external-service
errors, with a bounded `read_retries` budget (default 1, configurable from 0 to 5).
Retries use the same revision. After exhaustion the original error is returned to
the Agent, which can use another available evidence path; no answer is fabricated
and no latest-version fallback is made. The query response reports the retry
count. Application-level deadlines and workload evaluations remain the caller's
responsibility.

Inline graph text and structured assertions without supplied evidence retain
immutable input records under `triplets/evidence/`. These are excluded from KB
raw-document indexing. Explicit assertions are labeled as caller statements, not
independently verified facts. Logical file IDs now derive from canonical URI;
content hashes remain source versions. The receipt ledger migrates legacy JSON
state to SQLite, allocates unique revisions across workers, retains durable
receipts, and keeps the JSONL export for existing tooling. This does not make an
external append write and its receipt an exactly-once distributed transaction.

## Validation status

Local validation: 368 package tests passed, 86 skipped; 3 Codex-adapter tests
passed. The SQLite demo and wheel/resource checks passed. Tests use local storage
and deterministic model/embedding doubles. Coverage includes actual hard process
exit, source checkpoint reuse, parallel publication, publication/rollback races,
transient pinned reads, historical table reads, graph evidence replacement, and
Skill receipt isolation. Dolt/MySQL service tests were skipped when unavailable;
live SDK/model integrations, enterprise throughput, and research quality/cost
benchmarks have not been measured. Publication includes a monotonically increasing
epoch so an intervening publication and rollback cannot let a stale builder win.
