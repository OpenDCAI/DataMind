"""Native LanceDB versions and Dolt commits for immutable Workspace bindings.

The shared stores are private to Workspace; direct live imports never write here.
Dolt snapshot commits have a common empty parent. Table trees are copied by
reference from the selected base commit; this makes pruned histories collectible
without keeping an ancestor chain to every old Workspace revision.
"""

from __future__ import annotations
import asyncio
import csv
import json
import os
import signal
import time
import socket
import subprocess
from contextlib import asynccontextmanager
from datetime import timedelta
from filelock import AsyncFileLock
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from datamind.core.errors import CapabilityError
from datamind.capabilities.db.providers.dolt import DoltDialect


async def completed(awaitable):
    # Cancellation must not release a store lock while its worker still writes.
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def blocking(function, *args, **kwargs):
    return await completed(asyncio.to_thread(function, *args, **kwargs))


class PinnedVectors:
    name = "lancedb"

    def __init__(self, store, version):
        self.store, self.version = store, version
        self.dimension = store.dimension
        self.existing_count = 0

    @property
    def latest_version(self):
        return self.version

    async def query(self, embedding, *, top_k=5, where=None, version=None):
        if version is not None and version != self.version:
            raise CapabilityError(
                "kb", "select a Workspace revision instead of another backend version"
            )
        return await completed(
            self.store.query(embedding, top_k=top_k, where=where, version=self.version)
        )

    async def count(self):
        def count():
            table = self.store._client.open_table(self.store._collection_name)
            table.checkout(self.version)
            return table.count_rows()

        return await blocking(count)

    async def get_all_texts(self):
        return await completed(self.store.get_all_texts(version=self.version))


class DoltRepository:
    def __init__(self, workspace):
        self.workspace = workspace
        self.path = workspace.root / "shared" / "dolt"
        self.path.mkdir(parents=True, exist_ok=True)

    def _reap_interrupted_server(self, config):
        # A killed parent cannot run Popen cleanup. The absolute, private config
        # path is an ownership marker; never signal unrelated Dolt processes.
        result = subprocess.run(
            ["ps", "-ww", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            command = parts[1].strip()
            marker = " sql-server --config " + str(config)
            if (
                not command.endswith(marker)
                or os.path.basename(command[: -len(marker)]) != "dolt"
            ):
                continue
            pid = int(parts[0])
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "stat="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
                if not state or state.startswith("Z"):
                    break
                time.sleep(0.05)
            else:
                # Confirm the ownership marker again before escalation.
                current = subprocess.run(
                    ["ps", "-ww", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
                if current == command:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @asynccontextmanager
    async def session(self, revision=None):
        async with AsyncFileLock(
            str(self.workspace.root / "dolt.lock"),
            timeout=self.workspace.settings.workspace.lock_timeout_s,
        ):
            dialect = DoltDialect()
            engine = None
            view_engine = None
            try:
                config = self.workspace.root / "shared" / "dolt-server.json"
                await blocking(self._reap_interrupted_server, config)
                # This local coordinator owns its repository. It never applies
                # checkout, reset or GC to an application's external database.
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                dsn = f"mysql+pymysql://root:@127.0.0.1:{port}/dolt"
                from datamind.workspace import _atomic_json

                _atomic_json(config, {"listener": {"host": "127.0.0.1", "port": port}})
                engine = await blocking(
                    dialect.build_engine,
                    dsn,
                    storage_dir=str(self.path),
                    server_config=config,
                )
                if revision:
                    revision = dialect.validate_version(revision)
                    url = make_url(dsn).set(database="dolt/" + revision)
                    view_engine = create_engine(url, pool_pre_ping=True)
                yield dialect, view_engine or engine
            finally:
                if view_engine is not None:
                    await blocking(view_engine.dispose)
                if engine is not None:
                    await blocking(engine.dispose)
                await blocking(dialect.aclose)

    async def build(self, base, tables, revision):
        async with self.session() as (dialect, root_engine):

            def sql(query, params=None):
                return dialect._run_sql(root_engine, query, params)

            initial = (await blocking(sql, "SELECT DOLT_HASHOF('main')"))[0][0]
            branch = "dm_build_" + revision.removeprefix("revision-")
            await blocking(
                sql,
                "CALL DOLT_BRANCH(:branch,:base)",
                {"branch": branch, "base": initial},
            )
            url = root_engine.url.set(database="dolt/" + branch)
            engine = create_engine(url, pool_pre_ping=True)
            old = base.get("tables", {}) if base else {}
            try:
                if base and old:
                    placeholders = ",".join(":t" + str(i) for i in range(len(old)))
                    params = {
                        "commit": base["version"],
                        **{"t" + str(i): name for i, name in enumerate(old)},
                    }
                    await blocking(
                        dialect._run_sql,
                        engine,
                        "CALL DOLT_CHECKOUT(:commit," + placeholders + ")",
                        params,
                    )

                def apply():
                    from itertools import islice

                    with engine.begin() as conn:
                        for name in old.keys() - tables.keys():
                            conn.execute(
                                text("DROP TABLE " + dialect._quote_ident(name))
                            )
                        for name, item in tables.items():
                            if old.get(name) == item["hash"]:
                                continue
                            q = dialect._quote_ident
                            if name in old:
                                conn.execute(text("DROP TABLE " + q(name)))
                            cols = item["columns"]
                            conn.execute(
                                text(
                                    "CREATE TABLE "
                                    + q(name)
                                    + " ("
                                    + ", ".join(q(c) + " TEXT" for c in cols)
                                    + ")"
                                )
                            )
                            statement = text(
                                "INSERT INTO "
                                + q(name)
                                + " ("
                                + ",".join(q(c) for c in cols)
                                + ") VALUES ("
                                + ",".join(":" + c for c in cols)
                                + ")"
                            )
                            with (self.workspace.root / item["path"]).open(
                                newline="", encoding="utf-8"
                            ) as handle:
                                reader = csv.reader(handle)
                                next(reader, None)
                                while batch := list(islice(reader, 1000)):
                                    # Canonical JSON cells preserve NULL vs empty
                                    # strings without interpreting user values.
                                    conn.execute(
                                        statement,
                                        [
                                            {
                                                c: json.loads(v)
                                                for c, v in zip(cols, row)
                                            }
                                            for row in batch
                                        ],
                                    )

                await blocking(apply)
                commit = await completed(
                    dialect.commit_version(engine, "Workspace " + revision)
                )
                if commit is None:
                    commit = (
                        await blocking(
                            dialect._run_sql, engine, "SELECT DOLT_HASHOF('HEAD')"
                        )
                    )[0][0]
                tag = "dm_" + commit
                existing = await blocking(
                    dialect._run_sql,
                    engine,
                    "SELECT tag_name FROM dolt_tags WHERE tag_name=:tag",
                    {"tag": tag},
                )
                if not existing:
                    await blocking(
                        dialect._run_sql,
                        engine,
                        "CALL DOLT_TAG(:tag,:commit)",
                        {"tag": tag, "commit": commit},
                    )
                return {
                    "provider": "dolt",
                    "version": commit,
                    "tables": {n: t["hash"] for n, t in tables.items()},
                    "path": "shared/dolt",
                }
            finally:
                await blocking(engine.dispose)
                # A retained tag, not this staging branch, owns the snapshot.
                await blocking(
                    sql, "CALL DOLT_BRANCH('-D',:branch)", {"branch": branch}
                )


class PinnedDB:
    def __init__(self, workspace, binding):
        self.repository = DoltRepository(workspace)
        self.binding = binding
        self.db_cfg = workspace.settings.db
        self.dialect = DoltDialect()

    def _validate(self, sql):
        from sqlglot import parse, exp
        from sqlglot.optimizer.scope import traverse_scope
        from sqlglot.errors import ParseError

        try:
            trees = parse(sql, read="mysql")
        except ParseError as exc:
            raise CapabilityError("db", "SQL is not a supported pinned SELECT") from exc
        if len(trees) != 1 or not isinstance(trees[0], exp.Query):
            raise CapabilityError("db", "Workspace reads require one SELECT")
        if trees[0].find(exp.Into) or trees[0].find(exp.Lock):
            raise CapabilityError(
                "db", "Workspace reads cannot write files or acquire locks"
            )
        for function in trees[0].find_all(exp.Anonymous):
            if function.name.lower().startswith("dolt_") or function.name.lower() in {
                "load_file",
                "get_lock",
                "release_lock",
            }:
                raise CapabilityError(
                    "db", "function is outside the pinned Workspace revision"
                )
        for scope in traverse_scope(trees[0]):
            for source in scope.sources.values():
                if isinstance(source, exp.Table):
                    if (
                        source.db
                        or source.catalog
                        or source.args.get("version")
                        or source.name not in self.binding["tables"]
                    ):
                        raise CapabilityError(
                            "db", "table is outside the pinned Workspace revision"
                        )

    async def list_tables(self):
        return sorted(self.binding["tables"])

    async def describe(self, table):
        if table not in self.binding["tables"]:
            raise CapabilityError(
                "db", "table is outside the pinned Workspace revision"
            )
        async with self.repository.session(self.binding["version"]) as (
            dialect,
            engine,
        ):
            return await completed(dialect.describe(engine, table))

    async def query_sql(self, sql):
        self._validate(sql)
        async with self.repository.session(self.binding["version"]) as (
            dialect,
            engine,
        ):
            return await dialect.execute_readonly(
                engine,
                sql,
                row_limit=self.db_cfg.row_limit,
                timeout_s=self.db_cfg.query_timeout_s,
            )

    def _check_version(self, version):
        if version != self.binding["version"]:
            raise CapabilityError(
                "db", "select a Workspace revision instead of another backend version"
            )

    async def list_versions(self):
        return {"versions": [{"commit": self.binding["version"]}], "pinned": True}

    async def list_tables_at_version(self, *, version):
        self._check_version(version)
        return await self.list_tables()

    async def describe_at_version(self, table, *, version):
        self._check_version(version)
        return await self.describe(table)

    async def query_sql_at_version(self, sql, *, version):
        self._check_version(version)
        return await self.query_sql(sql)

    async def aclose(self):
        pass


async def prune_native(workspace, manifests, *, compact=False):
    groups = {}
    for manifest in manifests:
        kb = manifest.get("snapshots", {}).get("kb", {})
        if manifest.get("format_version") == 2 and kb:
            groups.setdefault(kb["path"], set()).add(kb["version"])
    report = {"kb": [], "db": None, "compacted": compact}
    import shutil

    kbroot = workspace.root / "shared" / "kb"
    for path in kbroot.glob("*"):
        relative = path.relative_to(workspace.root).as_posix()
        if relative not in groups:
            shutil.rmtree(path)
            report["kb"].append({"path": relative, "removed": True})
            continue
        import lancedb

        table = lancedb.connect(path).open_table("workspace")
        keep = groups[relative]

        def clean():
            for version in keep:
                tag = "dm_workspace_" + str(version)
                if tag not in table.tags.list():
                    table.tags.create(tag, version)
            for tag in table.tags.list():
                if (
                    tag.startswith("dm_workspace_")
                    and int(tag.removeprefix("dm_workspace_")) not in keep
                ):
                    table.tags.delete(tag)
            # Tagged retained versions are protected by the storage engine.
            return (
                table.to_lance().cleanup_old_versions(
                    older_than=timedelta(0),
                    delete_unverified=False,
                    error_if_tagged_old_versions=False,
                )
                if compact
                else {"physical_gc_pending": True}
            )

        stats = await blocking(clean)
        report["kb"].append(
            {"path": relative, "retained_versions": sorted(keep), "cleanup": str(stats)}
        )
    path = workspace.root / "shared" / "dolt"
    if (path / ".dolt").is_dir():
        versions = {
            m["snapshots"]["db"]["version"]
            for m in manifests
            if m.get("format_version") == 2 and m.get("snapshots", {}).get("db")
        }
        repo = DoltRepository(workspace)
        async with repo.session() as (dialect, engine):
            rows = await blocking(
                dialect._run_sql, engine, "SELECT tag_name FROM dolt_tags"
            )
            for (tag,) in rows:
                if tag.startswith("dm_") and tag.removeprefix("dm_") not in versions:
                    await blocking(
                        dialect._run_sql,
                        engine,
                        "CALL DOLT_TAG('-d',:tag)",
                        {"tag": tag},
                    )
            rows = await blocking(
                dialect._run_sql, engine, "SELECT name FROM dolt_branches"
            )
            for (name,) in rows:
                if name.startswith("dm_build_"):
                    await blocking(
                        dialect._run_sql,
                        engine,
                        "CALL DOLT_BRANCH('-D',:name)",
                        {"name": name},
                    )
        # Ordinary GC now works: retained snapshots have no parent chain to
        # pruned Workspace commits, only to the common empty initial commit.
        if compact:
            async with AsyncFileLock(
                str(workspace.root / "dolt.lock"),
                timeout=workspace.settings.workspace.lock_timeout_s,
            ):

                def gc():
                    subprocess.run(
                        ["dolt", "gc"],
                        cwd=path,
                        check=True,
                        capture_output=True,
                        timeout=300,
                    )

                await blocking(gc)
        report["db"] = {
            "retained_versions": sorted(versions),
            "physical_gc_pending": not compact,
        }
    return report
