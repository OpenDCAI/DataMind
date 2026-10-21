"""Explicit V1 -> V2 migration from frozen evidence, without model calls.

Old revision IDs remain readable using their original format. The migrated head
is a new revision; retention is never silently changed during migration.
"""

from __future__ import annotations
import csv
import hashlib
import json
import os
import sqlite3
import struct
import tempfile
from pathlib import Path
from datamind.core.errors import ConfigError, CapabilityError
from datamind.core.protocols import GraphTriple
from datamind.capabilities.ingest.formats import DOCUMENT_EXTS, TABLE_EXTS
from datamind.workspace_incremental import digest, encoded


async def migrate(workspace):
    from filelock import AsyncFileLock

    async with AsyncFileLock(
        str(workspace.root / "writer.lock"),
        timeout=workspace.settings.workspace.lock_timeout_s,
    ):
        result = await _migrate(workspace)
    if workspace.settings.workspace.auto_gc and not result.get("unchanged"):
        try:
            result["metrics"]["retention_cleanup"] = await workspace.collect(
                dry_run=False, compact=workspace.retention["mode"] != "all"
            )
        except Exception as exc:
            result["metrics"]["cleanup_error"] = type(exc).__name__
    return result


async def _migrate(workspace):
    from datamind.workspace import Workspace

    token, revision = workspace.acquire_lease()
    if revision is None:
        raise ConfigError("No published Workspace to migrate")
    try:
        old = workspace.manifest(revision)
        with workspace._connect() as db:
            expected_parent = db.execute(
                "SELECT revision,epoch FROM head WHERE singleton=1"
            ).fetchone()
        if expected_parent[0] != revision:
            raise CapabilityError("workspace", "publication conflict during migration")
        if old.get("format_version") == 2:
            return {"unchanged": True, "workspace_revision": revision}
        selected = set(old["surfaces"])
        if "kb" in selected and workspace.settings.kb.vector_store != "lancedb":
            raise ConfigError("Configure kb.vector_store=lancedb for migration")
        if "db" in selected and workspace.settings.db.dialect != "dolt":
            raise ConfigError("Configure db.dialect=dolt for migration")
        prepared = {
            s["uri"]: {
                "chunks": [],
                "tables": [],
                "graph_artifact": None,
                "truncated": s["uri"] in old.get("truncated_sources", []),
            }
            for s in old["sources"]
        }
        snapshots = {
            s["uri"]: workspace._settings_for(revision).data.base_dir / s["snapshot"]
            for s in old["sources"]
        }
        # Validate copied source bytes. Never read mutable originals, which may
        # have changed or been deleted since the old revision was published.
        for uri, path in snapshots.items():
            sha = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    sha.update(block)
            expected = next(s["version"] for s in old["sources"] if s["uri"] == uri)
            if "sha256:" + sha.hexdigest() != expected:
                raise CapabilityError("workspace", "legacy evidence checksum mismatch")
        graph = {uri: [] for uri in prepared}
        async with workspace.reader(revision) as reader:
            services = reader._system.services
            if "kb" in selected:
                if services.kb._compatibility_error:
                    raise ConfigError(services.kb._compatibility_error)
                store = services.kb.vector_store
                if store.name == "chroma":
                    result = store._collection.get(
                        include=["documents", "metadatas", "embeddings"]
                    )
                    values = zip(
                        result["ids"],
                        result["documents"],
                        result["metadatas"],
                        result["embeddings"],
                    )
                elif store.name == "lancedb":
                    rows = store._collection.search(None).to_list()
                    values = (
                        (
                            r["id"],
                            r["document"],
                            json.loads(r["metadata"]),
                            r["embedding"],
                        )
                        for r in rows
                    )
                else:
                    raise ConfigError(
                        "Legacy vector provider cannot export stored embeddings"
                    )
                for key, text, meta, vector in values:
                    uri = meta.get("source")
                    if uri not in prepared:
                        raise ConfigError("Legacy KB source is absent from manifest")
                    vector = list(vector)
                    ref_key = digest(
                        encoded(
                            [
                                workspace.settings.embedding.model_dump(
                                    mode="json", exclude={"api_key"}
                                ),
                                text,
                            ]
                        )
                    )
                    ref = "vectors/" + ref_key[:2] + "/" + ref_key + ".f32"
                    path = workspace.root / ref
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not path.exists():
                        path.write_bytes(struct.pack("<" + "f" * len(vector), *vector))
                    prepared[uri]["chunks"].append(
                        {"id": key, "text": text, "metadata": meta, "vector_ref": ref}
                    )
            if "graph" in selected:
                store = services.graph.store
                for source, target, data in store._g.edges(data=True):
                    edge = store._edge(source, target, data)
                    uri = edge.properties.get("source")
                    if uri not in graph:
                        raise ConfigError("Legacy graph source is absent from manifest")
                    triple = GraphTriple(
                        subject=source,
                        object=target,
                        relation=edge.relation,
                        subject_type=store._g.nodes[source].get("type", "entity"),
                        object_type=store._g.nodes[target].get("type", "entity"),
                        confidence=edge.weight,
                        source=uri,
                        evidence=edge.evidence,
                        properties={
                            k: v for k, v in edge.properties.items() if k != "source"
                        },
                    )
                    graph[uri].append(triple.model_dump(mode="json"))
            if "db" in selected:
                dbpath = services.db.engine.url.database
                connection = sqlite3.connect(
                    "file:" + str(Path(dbpath).resolve()) + "?mode=ro", uri=True
                )
                try:
                    for (name,) in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall():
                        quoted = '"' + name.replace('"', '""') + '"'
                        cursor = connection.execute("SELECT * FROM " + quoted)
                        columns = [c[0] for c in cursor.description]
                        if "_source_file" not in columns:
                            raise ConfigError("Legacy table has no source identity")
                        first = cursor.fetchone()
                        if first is None:
                            candidates = []
                            for source in old["sources"]:
                                checkpoint = source.get("checkpoint")
                                if (
                                    checkpoint
                                    and (workspace.root / checkpoint).is_file()
                                ):
                                    value = json.loads(
                                        (workspace.root / checkpoint).read_text()
                                    )
                                    if any(
                                        "ws_"
                                        + digest((source["source_id"] + t[0]).encode())[
                                            :24
                                        ]
                                        == name
                                        for t in value.get("tables", [])
                                    ):
                                        candidates.append(source["uri"])
                            if len(candidates) != 1:
                                raise ConfigError(
                                    "Cannot identify source of empty legacy table; republish it before migration"
                                )
                            uri = candidates[0]
                        else:
                            uri = first[columns.index("_source_file")]
                        if uri not in prepared:
                            raise ConfigError(
                                "Legacy DB source is absent from manifest"
                            )
                        fd, temp = tempfile.mkstemp(
                            dir=workspace.root, prefix=".migrate-table-"
                        )
                        try:
                            count = 0
                            with os.fdopen(
                                fd, "w", encoding="utf-8", newline=""
                            ) as handle:
                                writer = csv.writer(handle)
                                writer.writerow(columns)
                                rows = [first] if first is not None else []
                                while rows:
                                    for row in rows:
                                        if row[columns.index("_source_file")] != uri:
                                            raise ConfigError(
                                                "Legacy table mixes source identities"
                                            )
                                        writer.writerow(
                                            [
                                                json.dumps(v, ensure_ascii=False)
                                                for v in row
                                            ]
                                        )
                                        count += 1
                                    rows = cursor.fetchmany(1000)
                            sha = hashlib.sha256()
                            with open(temp, "rb") as handle:
                                for block in iter(
                                    lambda: handle.read(1024 * 1024), b""
                                ):
                                    sha.update(block)
                            key = sha.hexdigest()
                            dest = workspace.root / "tables" / key[:2] / (key + ".csv")
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if not dest.exists():
                                os.replace(temp, dest)
                            prepared[uri]["tables"].append(
                                {
                                    "name": name,
                                    "hash": key,
                                    "path": dest.relative_to(workspace.root).as_posix(),
                                    "columns": columns,
                                    "rows": count,
                                    "sheet": name,
                                }
                            )
                        finally:
                            Path(temp).unlink(missing_ok=True)
                finally:
                    connection.close()
        worker = Workspace(workspace.settings, builder=workspace._builder)
        worker.settings.workspace.storage_mode = "incremental"
        worker.settings.workspace.auto_gc = False
        worker._writer_held = True
        worker._expected_parent = expected_parent
        inventory = [
            {k: s[k] for k in ("uri", "relative_path", "version")}
            for s in old["sources"]
        ]
        worker._inventory = lambda path: list(inventory)
        worker._migration_sources = snapshots
        worker._migration_prepared = prepared
        assertions = [
            {
                "uri": s["uri"],
                "version": s["version"],
                "triples": graph[s["uri"]],
                "truncated": prepared[s["uri"]]["truncated"],
            }
            for s in old["sources"]
            if Path(s["uri"]).suffix.lower() in (DOCUMENT_EXTS - TABLE_EXTS)
        ]
        result = await worker.update(
            old["source_root"], surfaces=selected, graph_sources=assertions
        )
        return {**result, "migrated_from": revision}
    finally:
        workspace.release_lease(token)
