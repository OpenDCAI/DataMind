"""Single-host workspace retention and durable reader leases.

All reference changes and GC run under one OS lock. Files are immutable;
publication is still a compare-and-swap in the catalog, not a multi-store commit.
"""

from __future__ import annotations
import json
import shutil
import time
import uuid
from contextlib import contextmanager
from filelock import FileLock
from datamind.core.errors import CapabilityError


class WorkspaceCatalog:
    def _init_catalog(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leases (
                    id TEXT PRIMARY KEY, revision TEXT NOT NULL, expires REAL NOT NULL, kind TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS leases_revision ON leases(revision);
                CREATE TABLE IF NOT EXISTS protected_versions (revision TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS incremental_builds (revision TEXT PRIMARY KEY);
            """)
            legacy = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] > 0
            db.execute(
                "INSERT OR IGNORE INTO workspace_meta VALUES (?,?)",
                (
                    "retention",
                    json.dumps(
                        {
                            "mode": "all" if legacy else "latest",
                            "keep_last": self.settings.workspace.keep_last,
                        }
                    ),
                ),
            )

    def _lock(self):
        return FileLock(
            str(self.root / "references.lock"),
            timeout=self.settings.workspace.lock_timeout_s,
        )

    @property
    def retention(self):
        with self._connect() as db:
            value = json.loads(
                db.execute(
                    "SELECT value FROM workspace_meta WHERE key='retention'"
                ).fetchone()[0]
            )
        cfg = self.settings.workspace
        if not value.get("explicit") and cfg.retention != "auto":
            return {"mode": cfg.retention, "keep_last": cfg.keep_last}
        return {k: value[k] for k in ("mode", "keep_last")}

    def configure_retention(self, mode, keep_last=10):
        if (
            mode not in {"latest", "recent", "all"}
            or not isinstance(keep_last, int)
            or isinstance(keep_last, bool)
            or keep_last < 1
        ):
            raise ValueError(
                "retention must be latest, recent, or all; keep_last must be positive"
            )
        value = {"mode": mode, "keep_last": keep_last}
        with self._lock(), self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO workspace_meta VALUES (?,?)",
                ("retention", json.dumps({**value, "explicit": True})),
            )
        # An explicit API policy takes effect on this instance too.
        self.settings.workspace.retention = "auto"
        return value

    def storage_usage(self):
        sizes = {}
        for name in (
            "objects",
            "vectors",
            "tables",
            "artifacts",
            "shared",
            "revisions",
            "checkpoints",
        ):
            sizes[name] = sum(
                p.stat().st_size for p in (self.root / name).rglob("*") if p.is_file()
            )
        graph = self.root / "graph.sqlite3"
        sizes["graph"] = graph.stat().st_size if graph.exists() else 0
        return {
            "bytes": sum(sizes.values()),
            "components": sizes,
            "note": "Allocated file sizes; database free pages and filesystem compression may differ.",
        }

    async def keepalive(self, token):
        import asyncio

        while token:
            await asyncio.sleep(min(30, self.settings.workspace.lease_ttl_s / 3))
            self.renew_lease(token)

    def history(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,parent,state,error FROM revisions ORDER BY rowid DESC"
            ).fetchall()
        return {
            "current_revision": self.current_revision,
            "retention": self.retention,
            "versions": [
                dict(zip(("revision", "parent", "state", "error"), r)) for r in rows
            ],
        }

    def acquire_lease(self, revision=None, *, kind="reader"):
        with self._lock(), self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if revision is None:
                revision = db.execute(
                    "SELECT revision FROM head WHERE singleton=1"
                ).fetchone()[0]
            if revision is None:
                return None, None
            row = db.execute(
                "SELECT state FROM revisions WHERE id=?", (revision,)
            ).fetchone()
            if row is None or row[0] not in (
                {"BUILDING"} if kind == "build" else {"PUBLISHED"}
            ):
                raise CapabilityError(
                    "workspace", "revision is unavailable or has been pruned"
                )
            token = uuid.uuid4().hex
            db.execute(
                "INSERT INTO leases VALUES (?,?,?,?)",
                (
                    token,
                    revision,
                    time.time() + self.settings.workspace.lease_ttl_s,
                    kind,
                ),
            )
        return token, revision

    def renew_lease(self, token):
        with self._lock(), self._connect() as db:
            row = db.execute(
                "SELECT revision,expires FROM leases WHERE id=?", (token,)
            ).fetchone()
            if row is None or row[1] <= time.time():
                raise CapabilityError(
                    "workspace", "workspace lease expired; start a new operation"
                )
            db.execute(
                "UPDATE leases SET expires=? WHERE id=?",
                (time.time() + self.settings.workspace.lease_ttl_s, token),
            )
        return row[0]

    def release_lease(self, token):
        if token:
            with self._lock(), self._connect() as db:
                db.execute("DELETE FROM leases WHERE id=?", (token,))

    @contextmanager
    def pinned(self, revision=None):
        token, revision = self.acquire_lease(revision)
        try:
            yield revision
        finally:
            self.release_lease(token)

    def protect(self, revision, enabled=True):
        with self._lock():
            self.manifest(revision)
            with self._connect() as db:
                if enabled:
                    db.execute(
                        "INSERT OR IGNORE INTO protected_versions VALUES (?)",
                        (revision,),
                    )
                else:
                    db.execute(
                        "DELETE FROM protected_versions WHERE revision=?", (revision,)
                    )
        return {"revision": revision, "protected": enabled}

    async def collect(self, *, dry_run=True, compact=False):
        """Mark/sweep immutable objects, retaining active readers and builders.

        Provider compaction is separate and must honor all retained bindings.
        A failed cleanup is retryable; tombstoned versions never become readable.
        """
        from filelock import AsyncFileLock

        # Same acquisition order as incremental builds: writer, then references.
        async with AsyncFileLock(
            str(self.root / "writer.lock"),
            timeout=self.settings.workspace.lock_timeout_s,
        ):
            with self._lock():
                now = time.time()
                with self._connect() as db:
                    rows = db.execute(
                        "SELECT id,state FROM revisions ORDER BY rowid DESC"
                    ).fetchall()
                    leases = db.execute(
                        "SELECT revision FROM leases WHERE expires>?", (now,)
                    ).fetchall()
                    active = {r[0] for r in leases}
                    roots = {
                        r[0]
                        for r in db.execute("SELECT revision FROM protected_versions")
                    }
                    head = db.execute(
                        "SELECT revision FROM head WHERE singleton=1"
                    ).fetchone()[0]
                    if head:
                        roots.add(head)
                    roots |= active
                    published = [r for r, state in rows if state == "PUBLISHED"]
                    policy = self.retention
                    if policy["mode"] == "all":
                        roots.update(published)
                    elif policy["mode"] == "recent":
                        roots.update(published[: policy["keep_last"]])
                    # Old builders have no heartbeat protocol. Never remove them.
                    building = set()
                    native_builds = {
                        r[0]
                        for r in db.execute("SELECT revision FROM incremental_builds")
                    }
                    for r, state in rows:
                        if state != "BUILDING":
                            continue
                        marker = self.root / "revisions" / r / "build.json"
                        if r in native_builds or (
                            marker.exists()
                            and json.loads(marker.read_text()).get("format_version")
                            == 2
                        ):
                            if not dry_run:
                                db.execute(
                                    "UPDATE revisions SET state='FAILED',error='interrupted incremental build' WHERE id=?",
                                    (r,),
                                )
                        else:
                            building.add(r)
                    if building:
                        return {
                            "dry_run": dry_run,
                            "deferred": "build in progress or requires recovery",
                            "retention": policy,
                            "retained_revisions": sorted(roots),
                            "pruned_revisions": [],
                            "reclaimed_bytes": 0,
                        }
                    candidates = [
                        r
                        for r, state in rows
                        if (
                            state in {"PUBLISHED", "PRUNED", "FAILED"}
                            or (state == "BUILDING" and r not in building)
                        )
                        and r not in roots
                    ]
                    retained = []
                    for r in roots:
                        path = self.root / "revisions" / r / "manifest.json"
                        if path.is_file():
                            retained.append(json.loads(path.read_text()))
                    referenced = set()
                    for manifest in retained:
                        for item in manifest["sources"]:
                            if manifest.get("format_version") == 2:
                                referenced.add(item["snapshot"])
                                referenced.add(item["artifact"])
                                artifact = self.root / item["artifact"]
                                if artifact.exists():
                                    value = json.loads(artifact.read_text())
                                    referenced.update(
                                        c["vector_ref"] for c in value.get("chunks", [])
                                    )
                                    referenced.update(
                                        t["path"] for t in value.get("tables", [])
                                    )
                            elif item.get("checkpoint"):
                                referenced.add(item["checkpoint"])
                                artifact = self.root / item["checkpoint"]
                                if artifact.exists():
                                    referenced.update(t["path"] for t in json.loads(artifact.read_text()).get("tables", [])
                                                      if isinstance(t, dict) and "path" in t)
                    files = [
                        p
                        for prefix in (".source-", ".table-", ".migrate-table-")
                        for p in self.root.glob(prefix + "*")
                        if p.is_file()
                    ]
                    for folder in ("objects", "artifacts", "vectors", "tables"):
                        for path in (self.root / folder).glob("*/*"):
                            if (
                                path.is_file()
                                and path.relative_to(self.root).as_posix()
                                not in referenced
                            ):
                                files.append(path)
                    # V1 caches without explicit references remain until migrated.
                    if retained and all(m.get("format_version") == 2 for m in retained):
                        files.extend(
                            p for p in (self.root / "checkpoints").glob("*.json")
                        )
                    size = sum(p.stat().st_size for p in files)
                    directories = [self.root / "revisions" / r for r in candidates]
                    size += sum(
                        p.stat().st_size
                        for d in directories
                        for p in d.rglob("*")
                        if p.is_file()
                    )
                    result = {
                        "dry_run": dry_run,
                        "retention": policy,
                        "retained_revisions": sorted(roots),
                        "pruned_revisions": candidates,
                        "reclaimable_bytes": size,
                        "reclaimed_bytes": 0,
                    }
                    if dry_run:
                        return result
                    db.executemany(
                        "UPDATE revisions SET state='PRUNED' WHERE id=?",
                        [(r,) for r in candidates],
                    )
                    db.execute("DELETE FROM leases WHERE expires<=?", (now,))
            # Native versions are removed only after catalog invalidation, so
            # interrupted cleanup cannot leave a readable dangling manifest.
            from datamind.workspace_native import prune_native

            result["providers"] = await prune_native(self, retained, compact=compact)
            from datamind.workspace_graph import prune_graph

            from datamind.workspace_native import blocking

            await blocking(prune_graph, self.root, retained, compact=compact)
            for path in files:
                path.unlink(missing_ok=True)
            for directory in directories:
                if directory.exists():
                    shutil.rmtree(directory)
            result["reclaimed_bytes"] = size
            return result
