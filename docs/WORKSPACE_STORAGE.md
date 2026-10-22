# Workspace storage and history (2.1)

A Workspace revision is a small catalog entry and manifest binding all selected
surfaces to one immutable state. Keeping history does not require another full
copy of every store. History retention and storage layout are separate choices.

## Enable shared incremental storage

```sh
pip install -e ".[versioning,dolt]"
# Install the Dolt 2.5.0 executable separately and put it on PATH.
export DATAMIND__WORKSPACE__STORAGE_MODE=incremental
export DATAMIND__KB__VECTOR_STORE=lancedb
export DATAMIND__DB__DIALECT=dolt
```

Supported/tested native packages: LanceDB 0.40.x, pylance 9.x, PyArrow >=19,
SQLAlchemy and PyMySQL (the `dolt` extra). Catalog/graph use Python SQLite;
cross-process locks use filelock. KB also needs an embedding provider.
Graph-only incremental workspaces do not require LanceDB or Dolt. Selected KB/DB
surfaces require those native providers. An external DB DSN is rejected: Workspace
owns a private local Dolt repository and never resets an application's database.

`storage_mode=auto` preserves the layout of an existing head. For a new Workspace
it chooses incremental when KB=LanceDB and DB=Dolt are configured, otherwise the
legacy snapshot layout. `snapshot` retains the legacy SQLite/NetworkX and local
Chroma/LanceDB implementation. Providers are never switched silently.

## What is stored

| Component | Shared data | What a revision references |
|---|---|---|
| Raw evidence | Immutable files keyed by SHA-256 and format | Object path, original URI and source hash |
| KB | One private LanceDB table per embedding configuration; immutable vector cache | Fixed table version and chunk fingerprints |
| DB | One private Dolt repository; unchanged table trees copied by reference | Fixed commit and table hashes |
| Graph | Indexed SQLite assertions grouped by immutable source artifact | Source artifact IDs visible to this revision |
| Build output | Immutable source artifacts with parsed tables, chunks and provenance | Processing fingerprint and source artifact path |

Unchanged sources do not need copying, embedding, parsing or graph extraction.
A changed document reuses identical text embeddings and updates its KB rows and
source graph assertions. A removed source disappears from the next manifest;
other origins supporting the same graph edge survive. Codex supplies graph
assertions for new or changed documents, including explicit empty lists.

CSV/TSV/XLSX imports stream canonical rows into table objects. Unchanged tables
are reused. Changed tables are currently replaced as a unit; Dolt shares matching
internal data where possible. This release does **not** promise row-level upserts
or constant update cost for a giant changed CSV. All imported columns preserve
TEXT semantics (NULL and empty strings remain distinct). Convert legacy XLS first.

Dolt snapshot commits share an empty initial parent rather than forming a chain
of Workspace snapshots. Table trees are copied from the selected base commit.
The Workspace catalog preserves logical parentage. Removing a snapshot tag thus
allows ordinary Dolt GC to reclaim it without retaining all prior commits.

Manifests still contain a complete source/reference inventory, and builds still
scan/hash source files to detect changes. Metadata and scanning cost therefore grow
with workspace size; incremental storage does not mean every operation is O(delta).
Changing embedding configuration creates a separate index; existing readers require
the original configuration and credentials. Model/API secrets are never saved in
manifests. This coordinator supports processes on one host with local storage,
not distributed locking, a remote graph service, or multi-host transactions.
Dolt SQL sessions are currently serialized and start a local server per operation;
enterprise high-throughput storage needs a persistent server adapter separately.

## Retention choices

New workspaces default to `latest`. Existing catalogs with revisions default to
`all`, preserving prior history. An explicitly saved API/CLI/UI policy takes
precedence over environment defaults and survives restart. Configure through CLI, the UI's Workspace history
selector, HTTP, MCP, Python or `DATAMIND__WORKSPACE__RETENTION`.

```sh
datamind workspace-status --usage
datamind workspace-policy latest       # no historical rollback after cleanup
datamind workspace-policy recent --keep-last 10
datamind workspace-policy all
datamind workspace-protect revision-...  # retain a particular published version
```

Setting a policy alone does not delete history. Automatic cleanup runs after
successful publication (`AUTO_GC=true`); latest/recent also compact native history.
Active readers and protected versions are retained in addition to policy roots.
Latest means minimal retained *history*, not literally zero transient storage:
build staging, active readers and provider housekeeping still take space.

```sh
datamind workspace-gc                  # preview only
datamind workspace-gc --apply          # remove unreferenced objects/version bindings
datamind workspace-gc --apply --compact # also clean Lance history, run Dolt GC, vacuum graph
```

GC marks unreachable revisions PRUNED before removing files/provider versions;
interrupted cleanup is retryable. Pruned revisions cannot be read or rolled back.
Shared data is removed only when no retained revision references it. Cleanup errors
do not undo a successful publication and are reported in build metrics; retry GC.
`reclaimed_bytes` counts removed immutable files and revision directories. Native
provider cleanup reports separately; allocated file size and filesystem free space
may differ. Non-compacting GC can leave provider history physically allocated.

## Consistency, leases and rollback

Builds serialize under an OS writer lock, prepare shared immutable artifacts and
native versions, then publish one manifest using revision+epoch CAS in SQLite.
Failures leave the old head readable. Completed artifacts/embeddings can be reused
when a failed build restarts. A dead incremental builder is recognized after the
writer lock is released; GC can discard its unreachable output. The private Dolt server is identified by its absolute configuration path; after a
parent crash, the next locked operation stops that orphan before reopening the
repository. Unrelated Dolt processes are left alone. Legacy builds are
conservatively deferred for explicit recovery instead of guessed dead.

An MCP read operation atomically acquires the head and a durable reader lease.
Every business tool call reads that revision's bindings, even if another process
publishes between calls. Calls renew the lease; `datamind_operation_close` releases
it. Default TTL is 1800 seconds; an expired operation errors and must be restarted,
never silently switches versions. Python `Workspace.reader` and the Codex host also
run heartbeats and release leases on exit/cancellation. Crashed readers expire.

```python
from datamind.config import DataSettings
from datamind.workspace import Workspace

workspace = Workspace(DataSettings())
workspace.configure_retention('recent', keep_last=10)
# graph_sources are prepared externally by the main Codex agent.
revision = await workspace.update('/absolute/sources', graph_sources=graph_sources)
async with workspace.reader(revision['workspace_revision']) as reader:
    print(await reader.call('graph_schema'))
workspace.rollback(old_revision, expected_revision=workspace.current_revision)
```

Rollback changes only the catalog head, increments the epoch and preserves fixed
bindings. The next build uses the rolled-back head as its base; it does not reuse
an incompatible provider's mutable latest state. Protect rollback targets if a
latest/recent cleanup might otherwise remove them.

## Migrate an existing Workspace

1. Back up the storage directory and stop legacy snapshot writers.
2. Configure the native providers above, keeping the original embedding configuration.
3. Run `datamind workspace-migrate` (or `Workspace.migrate()` / MCP `workspace_migrate`).
4. Validate pinned reads, choose a history policy, preview GC, then apply if desired.

Migration reads the current revision's frozen evidence, stored embeddings,
source-attributed graph and SQLite rows; it needs no original files or model calls.
It publishes a new V2 revision. Existing IDs keep their V1 contents and remain
readable until retention allows pruning; migration never silently deletes them.
Only the current head is migrated, not every historical snapshot. Keeping V1
history therefore keeps its full snapshots. Tables without traceable source
identity or incompatible embeddings fail explicitly rather than guessing.
Independent live data is not migrated into a Workspace by this command.

## API entry points

MCP tools (with `datamind_` prefix): `workspace_history`, `workspace_retention`,
`workspace_gc`, `workspace_rollback`, `workspace_protect`, `workspace_migrate`.
Mutations require a write operation. GC defaults to `dry_run=true`.
HTTP exposes history, retention, GC, rollback and migration under `/api/workspace`;
see [API contracts](STABLE_API.md). These share the existing trusted-host HTTP
boundary; enterprise deployments must apply their own tenant/auth policy.

The same data layer serves the personal Codex plugin and the Codex SDK host.
Tests cover native fixed-version reads, table/object reuse, active/expired leases,
rollback ABA, failed native publication, process death before publication,
source-scoped graph retraction, migration and provider cleanup. Passing tests
supports these contracts; it cannot guarantee all backend scales or model answers.
