"""Shared immutable source objects plus native KB/DB snapshot bindings."""

from __future__ import annotations
import csv
import hashlib
import json
import os
import re
import struct
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from filelock import AsyncFileLock
from datamind.core.errors import CapabilityError, ConfigError
from datamind.core.protocols import GraphTriple, GraphEvidence
from datamind.core.logging import current_context
from datamind.capabilities.ingest.formats import (
    DOCUMENT_EXTS,
    TABLE_EXTS,
    extract_document,
)
from datamind.capabilities.kb.indexer import _hash, _split_text
from datamind.capabilities.ingest.video import VIDEO_EXTS
from datamind.capabilities.ingest.tables import SQLITE_EXTS, TABLE_PIPELINE, table_records, table_graph, table_catalog
from datamind.workspace_native import blocking, completed


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()


def incremental_enabled(workspace, surfaces):
    mode = workspace.settings.workspace.storage_mode
    if mode == "snapshot":
        return False
    if mode == "incremental":
        return True
    head = workspace.current_revision
    if head:
        return workspace.manifest(head).get("format_version") == 2
    cfg = workspace.settings
    return cfg.kb.vector_store == "lancedb" and cfg.db.dialect == "dolt"


def store_bytes(root, kind, data, suffix):
    key = digest(data)
    path = root / kind / key[:2] / (key + suffix)
    if path.exists():
        return path.relative_to(root).as_posix(), False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)
    return path.relative_to(root).as_posix(), True


def save_artifact(root, key, value):
    from datamind.workspace import _atomic_json

    path = root / "artifacts" / key[:2] / (key + ".json")
    _atomic_json(path, value, overwrite=False)
    return path.relative_to(root).as_posix()


def canonical_tables(workspace, item, snapshot, *, include_text=False):
    """Stream tables to immutable cells, retaining schema and source column mappings."""
    from datamind.capabilities.ingest.tables import source_tables, normalize_columns, encode_cell, table_name, _kind
    if Path(item['uri']).suffix.lower() in SQLITE_EXTS:
        from datamind.capabilities.db.native_sqlite import inspect_database
        return inspect_database(snapshot, item, reference=workspace.settings.workspace.input_policy == 'dataspace')['tables']
    output = []
    with source_tables(snapshot, policy=workspace.settings.workspace.input_policy) as inputs:
        for table in inputs:
            cols, mapping = normalize_columns(table.header)
            if not cols and Path(item['uri']).suffix.lower() != '.json':
                raise CapabilityError('ingest', 'Table has no discoverable columns')
            if Path(item['uri']).suffix.lower() in {'.csv', '.tsv'}:
                table.name = Path(item['uri']).stem
            name = table_name(item['uri'], table.name)
            row_column = None
            if Path(item['uri']).suffix.lower() == '.json':
                row_column = '_source_row'
                while row_column.casefold() in {c.casefold() for c in cols}:
                    row_column += '_'
                cols.append(row_column)
            cols.append('_source_file')
            fd, temp = tempfile.mkstemp(dir=workspace.root, prefix='.table-')
            count, records = 0, []
            observed = [set() for _ in table.header]
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
                    writer = csv.writer(handle)
                    writer.writerow(cols)
                    for row in table.rows:
                        values = list(row[:len(table.header)])
                        values += [None if table.typed else ''] * (len(table.header) - len(values))
                        if not table.typed:
                            values = [None if x is None else str(x) for x in values]
                        for i, value in enumerate(values):
                            kind = _kind(value)
                            if kind:
                                observed[i].add(kind)
                            if include_text and Path(item['uri']).suffix.lower() == '.json' and isinstance(value, str) and len(value) >= 160:
                                for part in _split_text(value, chunk_size=workspace.settings.retrieval.chunk_size, chunk_overlap=workspace.settings.retrieval.chunk_overlap):
                                    records.append({'text': f'Table {table.name}, row {count + 1}, {table.header[i]}: {part}',
                                        'metadata': {'kind': 'table_text', 'table': name, 'original_table': table.name,
                                                     'row_ordinal': count + 1, 'row_locator_column': row_column or '', 'column': cols[i]}})
                        writer.writerow([json.dumps(encode_cell(x), ensure_ascii=False) for x in values + ([count + 1] if row_column else []) + [item['uri']]])
                        count += 1
                    handle.flush()
                    os.fsync(handle.fileno())
                sha = hashlib.sha256()
                with open(temp, 'rb') as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        sha.update(block)
                key = sha.hexdigest()
                target = workspace.root / 'tables' / key[:2] / (key + '.csv')
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    os.replace(temp, target)
                schema = {'name': name, 'sheet': table.name, 'columns': cols,
                          'types': table.types + (['INTEGER'] if row_column else []) + ['TEXT'], 'column_mapping': mapping,
                          'row_locator_column': row_column, 'typed': table.typed,
                          'declared_types': table.declared_types, 'primary_key': table.primary_key,
                          'foreign_keys': table.foreign_keys, 'source': item['uri'], 'source_version': item['version'],
                          'mixed_types': any(kinds and not kinds <= ({'INTEGER','REAL'} if typ in {'REAL','NUMERIC'} else {typ})
                                             for kinds, typ in zip(observed, table.types))}
                output.append({**schema, 'rows': count, 'hash': digest(encoded([key, schema])),
                               'path': target.relative_to(workspace.root).as_posix(), 'text_records': records})
            finally:
                Path(temp).unlink(missing_ok=True)
    return output


@asynccontextmanager
async def _writer(workspace):
    if getattr(workspace, "_writer_held", False):
        yield
    else:
        async with AsyncFileLock(
            str(workspace.root / "writer.lock"),
            timeout=workspace.settings.workspace.lock_timeout_s,
        ):
            yield


async def update_incremental(
    workspace,
    path,
    *,
    surfaces,
    relation_types,
    max_triples_per_file,
    metrics,
    graph_sources,
):
    from datamind.workspace import _atomic_json
    from datamind.workspace_graph import put_graph
    from datamind.workspace_native import DoltRepository

    selected = set(surfaces)
    cfg = workspace.settings
    if not selected or not selected <= {"kb", "graph", "db"}:
        raise ConfigError("Invalid workspace surfaces")
    if not 1 <= max_triples_per_file <= 10000:
        raise ConfigError("Invalid graph extraction limit")
    if "kb" in selected and cfg.kb.vector_store != "lancedb":
        raise ConfigError(
            "Incremental Workspace KB requires lancedb; configure kb.vector_store or use snapshot mode"
        )
    if "db" in selected and (cfg.db.dialect != "dolt" or cfg.db.dsn):
        raise ConfigError(
            "Incremental Workspace DB requires local Dolt without an external DSN"
        )
    if "graph" in selected and graph_sources is None:
        raise ConfigError("Incremental Workspace requires Codex-prepared graph_sources")
    source = Path(path).expanduser().resolve()
    started = time.monotonic()
    inventory = await blocking(workspace._inventory, source)
    metrics.update(
        inventory_ms=(time.monotonic() - started) * 1000,
        sources_total=len(inventory),
        object_bytes_written=0,
        objects_reused=0,
        kb_rows_upserted=0,
        kb_rows_deleted=0,
        db_tables_changed=0,
        db_tables_reused=0,
    )
    # Capture the expected parent BEFORE acquiring the writer lock. A queued
    # stale build must conflict, not silently rebase to another caller's update.
    with workspace._connect() as db:
        parent, epoch = db.execute(
            "SELECT revision,epoch FROM head WHERE singleton=1"
        ).fetchone()
    if (
        hasattr(workspace, "_expected_parent")
        and (parent, epoch) != workspace._expected_parent
    ):
        raise CapabilityError("workspace", "publication conflict during migration")
    if (
        parent
        and workspace.manifest(parent).get("format_version") != 2
        and not hasattr(workspace, "_migration_prepared")
    ):
        raise ConfigError(
            "Run explicit workspace migration before changing an existing snapshot Workspace to incremental storage"
        )
    old = workspace.manifest(parent) if parent else None
    prior = (
        {s["uri"]: s for s in old["sources"]}
        if old and old.get("format_version") == 2
        else {}
    )
    assertions = {}
    docs = {
        s["uri"]: s
        for s in inventory
        if Path(s["uri"]).suffix.lower() in (DOCUMENT_EXTS - TABLE_EXTS) | VIDEO_EXTS | SQLITE_EXTS
    }
    for entry in graph_sources or []:
        uri = entry["uri"]
        if (
            uri not in docs
            or uri in assertions
            or entry["version"] != docs[uri]["version"]
        ):
            raise ConfigError("Graph source must match inventory and content hash")
        triples = [GraphTriple(**v) for v in entry["triples"]]
        migrating_source = uri in getattr(workspace, '_migration_prepared', {})
        if len(triples) > max_triples_per_file and not migrating_source:
            raise ConfigError("Too many graph triples for source")
        for triple in triples:
            if relation_types is not None and triple.relation not in relation_types:
                raise ConfigError("Graph relation is outside vocabulary")
            triple.source = uri
            if migrating_source:
                continue  # Preserve frozen table/frame/speech locators during migration.
            triple.evidence = [
                GraphEvidence(
                    source_id="file:" + digest(uri.encode()),
                    source_version=entry["version"],
                    locator={"uri": uri},
                    method="extracted",
                    pipeline_version="codex-explicit-v1",
                )
            ]
        assertions[uri] = {
            "triples": [t.model_dump(mode="json") for t in triples],
            "truncated": bool(entry.get("truncated")),
        }
    if "graph" in selected:
        for uri, item in docs.items():
            if uri not in assertions and not item.get('structured_table'):
                before = prior.get(uri)
                if (
                    before is None
                    or before["version"] != item["version"]
                    or not before.get("graph_artifact")
                ):
                    raise ConfigError(
                        "Supply graph assertions for every new or changed document, including empty lists"
                    )
                # Unchanged assertions are immutable and reused, not re-extracted.
    context = current_context()
    skills = list(context.extra.get("applied_skills", {}).values()) if context else []
    recipe = {
        "version": "workspace-v2-native-sqlite-v1",
        "tables": TABLE_PIPELINE, "input_policy": cfg.workspace.input_policy,
        "surfaces": sorted(selected),
        "embedding": cfg.embedding.model_dump(mode="json", exclude={"api_key"}),
        "chunk_size": cfg.retrieval.chunk_size,
        "chunk_overlap": cfg.retrieval.chunk_overlap,
        "relation_types": relation_types,
        "max_triples_per_file": max_triples_per_file,
        "applied_skills": skills,
    }
    if any(Path(s['uri']).suffix.lower() in VIDEO_EXTS for s in inventory):
        from datamind.capabilities.ingest.video.bundle import recipe as video_recipe
        recipe['video'] = video_recipe(cfg.video)
    pipeline = digest(encoded(recipe))
    revision = "revision-" + uuid.uuid4().hex
    metrics["workspace_revision"] = revision
    async with _writer(workspace):
        with workspace._lock(), workspace._connect() as db:
            if db.execute(
                "SELECT revision,epoch FROM head WHERE singleton=1"
            ).fetchone() != (parent, epoch):
                raise CapabilityError(
                    "workspace", "publication conflict; current revision changed"
                )
            db.execute(
                "INSERT INTO revisions VALUES (?,?,?,NULL)",
                (revision, parent, "BUILDING"),
            )
            db.execute("INSERT INTO incremental_builds VALUES (?)", (revision,))
        root = workspace.root / "revisions" / revision
        _atomic_json(root / "build.json", {"format_version": 2, "parent": parent})
        embedding = None
        sources = []
        bindings = {}
        all_chunks = {}
        tables = {}
        native_databases = {}
        try:
            if "kb" in selected:
                from datamind import runtime

                embedding = runtime.build_embedding(cfg.embedding, fallback_llm=cfg.llm)
            for item in inventory:
                start = time.monotonic()
                original = Path(
                    getattr(workspace, "_migration_sources", {}).get(
                        item["uri"], item["uri"]
                    )
                )
                suffix = Path(item["uri"]).suffix.lower()
                blob = (
                    workspace.root
                    / "objects"
                    / item["version"][7:9]
                    / (item["version"][7:] + suffix)
                )
                native = suffix in SQLITE_EXTS
                reference = native and cfg.workspace.input_policy == 'dataspace'
                if native:
                    from datamind.capabilities.db.native_sqlite import prepare_source, validate_entry
                    frozen = getattr(workspace, '_migration_databases', {}).get(item['uri'])
                    prepare_item = item
                    if frozen is not None:
                        if cfg.workspace.input_policy != 'dataspace':
                            await blocking(validate_entry, frozen)
                        reference = frozen['storage'] == 'reference'
                        prepare_item = {**item, 'version': 'sha256:' + frozen['sha256']}
                    blob = await blocking(prepare_source, original, blob, prepare_item, reference=reference)
                    blob_ref = str(blob) if reference else blob.relative_to(workspace.root).as_posix()
                    if not reference:
                        metrics['object_bytes_written'] += blob.stat().st_size
                elif blob.exists():
                    blob_ref = blob.relative_to(workspace.root).as_posix()
                    metrics["objects_reused"] += 1
                else:
                    # Stream the original into immutable object storage.
                    fd, temp = tempfile.mkstemp(dir=workspace.root, prefix=".source-")
                    sha = hashlib.sha256() if cfg.workspace.input_policy != 'dataspace' else None
                    try:
                        with os.fdopen(fd, "wb") as out, original.open("rb") as inp:
                            for block in iter(lambda: inp.read(1024 * 1024), b""):
                                out.write(block)
                                if sha is not None:
                                    sha.update(block)
                            out.flush()
                            os.fsync(out.fileno())
                        if sha is not None and "sha256:" + sha.hexdigest() != item["version"]:
                            raise CapabilityError(
                                "workspace", "source changed during build; retry"
                            )
                        blob.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(temp, blob)
                        blob_ref = blob.relative_to(workspace.root).as_posix()
                        metrics["object_bytes_written"] += blob.stat().st_size
                    finally:
                        Path(temp).unlink(missing_ok=True)
                metrics["source_copy_ms"] += (time.monotonic() - start) * 1000
                assertion = assertions.get(item["uri"])
                graph_ref = (
                    prior.get(item["uri"], {}).get("graph_artifact")
                    if assertion is None
                    else None
                )
                key = digest(
                    encoded(
                        [
                            item["uri"],
                            item["version"],
                            pipeline,
                            assertion if assertion is not None else graph_ref,
                        ]
                    )
                )
                artifact = workspace.root / "artifacts" / key[:2] / (key + ".json")
                start = time.monotonic()
                if artifact.exists():
                    prepared = json.loads(artifact.read_text())
                    metrics["sources_reused"] += 1
                    metrics["checkpoint_read_ms"] += (time.monotonic() - start) * 1000
                else:
                    # Fast reuse for omitted, unchanged graph assertions.
                    previous = prior.get(item["uri"], {})
                    if (
                        assertion is None
                        and old
                        and old["pipeline_hash"] == pipeline
                        and previous.get("version") == item["version"]
                    ):
                        artifact = workspace.root / previous["artifact"]
                        prepared = json.loads(artifact.read_text())
                        metrics["sources_reused"] += 1
                    elif item["uri"] in getattr(workspace, "_migration_prepared", {}):
                        prepared = dict(workspace._migration_prepared[item["uri"]])
                        if "graph" in selected:
                            prepared["graph_artifact"] = put_graph(
                                workspace.root,
                                assertion["triples"] if assertion else [],
                            )
                        artifact = workspace.root / save_artifact(
                            workspace.root, key, prepared
                        )
                        metrics["sources_reused"] += 1
                    else:
                        prepared = {
                            "chunks": [],
                            "tables": [],
                            "graph_artifact": None,
                            "truncated": False,
                        }
                        video = None
                        records = None
                        structured = item.get('structured_table', False)
                        if (suffix in TABLE_EXTS or structured) and ('db' in selected or structured and selected & {'kb', 'graph'}):
                            prepared['tables'] = await blocking(canonical_tables, workspace, item, blob, include_text='kb' in selected)
                            metrics['parsed_table_files'] += 1
                            if structured:
                                records = table_records(item, prepared['tables'])
                        if suffix in VIDEO_EXTS and selected & {'kb', 'graph'}:
                            from datamind.capabilities.ingest.video.bundle import prepare_video, text_records
                            if 'graph' in selected and assertion is None and old['pipeline_hash'] != pipeline:
                                raise ConfigError('Video pipeline changed; prepare fresh video graph assertions')
                            video = await prepare_video(workspace.root, blob, item, cfg.video,
                                                        verify_source=cfg.workspace.input_policy != 'dataspace')
                            prepared['video_bundle'] = video['bundle_id']
                            records = text_records(video, cfg.retrieval)
                        if "kb" in selected and (suffix in DOCUMENT_EXTS | VIDEO_EXTS or structured):
                            if records is None:
                                text_value = (await blocking(extract_document, blob)).text
                                records = [{'text': part, 'metadata': {}} for part in _split_text(
                                    text_value, chunk_size=cfg.retrieval.chunk_size,
                                    chunk_overlap=cfg.retrieval.chunk_overlap)]
                            parts = [r['text'] for r in records]
                            vector_recipe = cfg.embedding.model_dump(
                                mode="json", exclude={"api_key"}
                            )
                            missing = []
                            refs = []
                            for part in parts:
                                vector_key = digest(encoded([vector_recipe, part]))
                                ref = (
                                    "vectors/"
                                    + vector_key[:2]
                                    + "/"
                                    + vector_key
                                    + ".f32"
                                )
                                refs.append(ref)
                                if not (workspace.root / ref).exists() and ref not in {
                                    r for _, r in missing
                                }:
                                    missing.append((part, ref))
                            if missing:
                                vectors = await embedding.embed_texts(
                                    [p for p, _ in missing]
                                )
                                if len(vectors) != len(missing):
                                    raise CapabilityError(
                                        "kb", "embedding count mismatch"
                                    )
                                from datamind.workspace import _atomic_json

                                for (_, ref), vector in zip(missing, vectors):
                                    if len(vector) != embedding.dimension:
                                        raise CapabilityError(
                                            "kb", "embedding dimension mismatch"
                                        )
                                    dest = workspace.root / ref
                                    dest.parent.mkdir(parents=True, exist_ok=True)
                                    data = struct.pack("<" + "f" * len(vector), *vector)
                                    fd, temporary = tempfile.mkstemp(
                                        dir=dest.parent, prefix=".vector-"
                                    )
                                    try:
                                        with os.fdopen(fd, "wb") as handle:
                                            handle.write(data)
                                            handle.flush()
                                            os.fsync(handle.fileno())
                                        os.replace(temporary, dest)
                                    finally:
                                        Path(temporary).unlink(missing_ok=True)
                                metrics["embedding_calls"] += 1
                                metrics["embedding_texts"] += len(missing)
                            prepared["chunks"] = [
                                {
                                    "id": _hash(part, item["uri"], ordinal=i),
                                    "text": part,
                                    "vector_ref": refs[i],
                                    "metadata": {
                                        "source": item["uri"],
                                        "source_id": "file:"
                                        + digest(item["uri"].encode()),
                                        "source_version": item["version"],
                                        "_chunk_ordinal": i,
                                        **records[i]['metadata'],
                                    },
                                }
                                for i, part in enumerate(parts)
                            ]
                        if "graph" in selected:
                            if assertion is not None:
                                triples = assertion['triples']
                                if video is not None:
                                    from datamind.capabilities.ingest.video.bundle import graph_records
                                    triples = graph_records(video, triples)
                                prepared["graph_artifact"] = put_graph(
                                    workspace.root, triples
                                )
                                prepared["truncated"] = assertion["truncated"]
                            elif graph_ref:
                                prepared["graph_artifact"] = graph_ref
                            else:
                                prepared["graph_artifact"] = put_graph(workspace.root, [])
                            if structured:
                                # Structural evidence is deterministic and rebuilt from the source schema.
                                triples = (assertion['triples'] if assertion else []) + table_graph(item, prepared['tables'])
                                prepared['graph_artifact'] = put_graph(workspace.root, triples)
                        artifact = workspace.root / save_artifact(
                            workspace.root, key, prepared
                        )
                        metrics["sources_computed"] += 1
                        metrics["source_compute_ms"] += (
                            time.monotonic() - start
                        ) * 1000
                if native:
                    from datamind.capabilities.db.native_sqlite import registration
                    entry = await blocking(registration, blob, item, reference=reference, revision=revision)
                    native_databases[entry['database_id']] = entry
                source_entry = {
                    **item,
                    "source_id": "file:" + digest(item["uri"].encode()),
                    "snapshot": blob_ref,
                    **({"storage": "reference", "database_id": entry["database_id"]} if reference else {}),
                    "artifact": artifact.relative_to(workspace.root).as_posix(),
                    "graph_artifact": prepared["graph_artifact"],
                    "truncated": prepared["truncated"],
                }
                if prepared.get('video_bundle'):
                    from datamind.capabilities.ingest.video.bundle import load_bundle
                    await blocking(load_bundle, workspace.root, prepared['video_bundle'],
                                   verify_integrity=cfg.workspace.input_policy != 'dataspace')
                    source_entry['video_bundle'] = prepared['video_bundle']
                sources.append(source_entry)
                all_chunks.update((c["id"], c) for c in prepared["chunks"])
                tables.update((t["name"], t) for t in prepared["tables"])
                metrics["sources_completed"] += 1
            start = time.monotonic()
            if "kb" in selected:
                from datamind.capabilities.kb.providers.lancedb_store import (
                    LanceDBVectorStore,
                )

                kb_key = digest(
                    encoded(
                        [
                            cfg.embedding.model_dump(mode="json", exclude={"api_key"}),
                            embedding.dimension,
                        ]
                    )
                )
                location = "shared/kb/" + kb_key
                store = LanceDBVectorStore(
                    persist_dir=str(workspace.root / location),
                    collection_name="workspace",
                    dimension=embedding.dimension,
                )
                base = (
                    old.get("snapshots", {}).get("kb")
                    if old and old.get("format_version") == 2
                    else None
                )
                previous_chunks = (
                    base.get("chunks", {}) if base and base["path"] == location else {}
                )
                if (
                    base
                    and base["path"] == location
                    and store.latest_version != base["version"]
                ):
                    await blocking(store._collection.restore, base["version"])
                elif not previous_chunks and store.existing_count:
                    await blocking(store._collection.delete, "true")
                new_fingerprints = {
                    key: digest(encoded(c)) for key, c in all_chunks.items()
                }
                removed = set(previous_chunks) - set(all_chunks)
                if removed:
                    await completed(store.delete(sorted(removed)))
                changed = [
                    c
                    for key, c in all_chunks.items()
                    if previous_chunks.get(key) != new_fingerprints[key]
                ]
                for offset in range(0, len(changed), 256):
                    batch = changed[offset : offset + 256]
                    vectors = []
                    for chunk in batch:
                        data = (workspace.root / chunk["vector_ref"]).read_bytes()
                        vectors.append(
                            list(struct.unpack("<" + "f" * (len(data) // 4), data))
                        )
                    await completed(
                        store.add(
                            ids=[c["id"] for c in batch],
                            texts=[c["text"] for c in batch],
                            embeddings=vectors,
                            metadatas=[c["metadata"] for c in batch],
                        )
                    )
                version = store.latest_version
                tag = "dm_workspace_" + str(version)
                if tag not in store._collection.tags.list():
                    await blocking(store._collection.tags.create, tag, version)
                bindings["kb"] = {
                    "provider": "lancedb",
                    "path": location,
                    "version": version,
                    "dimension": embedding.dimension,
                    "embedding_fingerprint": digest(
                        encoded(
                            cfg.embedding.model_dump(mode="json", exclude={"api_key"})
                        )
                    ),
                    "chunks": new_fingerprints,
                }
                metrics["kb_rows_upserted"] = len(changed)
                metrics["kb_rows_deleted"] = len(removed)
            if "db" in selected:
                base = (
                    old.get("snapshots", {}).get("db")
                    if old and old.get("format_version") == 2
                    else None
                )
                managed_tables = {n: t for n, t in tables.items() if not t.get("native_sqlite")}
                hashes = {n: t["hash"] for n, t in managed_tables.items()}
                if base and base["tables"] == hashes:
                    bindings["db"] = base
                else:
                    bindings["db"] = await DoltRepository(workspace).build(
                        base, managed_tables, revision
                    )
                before = base.get("tables", {}) if base else {}
                metrics["db_tables_reused"] = sum(
                    before.get(n) == v for n, v in hashes.items()
                )
                metrics["db_tables_changed"] = (
                    len(hashes)
                    - metrics["db_tables_reused"]
                    + len(before.keys() - hashes.keys())
                )
            if "graph" in selected:
                bindings["graph"] = {
                    "provider": "sqlite-source-graph",
                    "path": "graph.sqlite3",
                }
            metrics["materialization_ms"] += (time.monotonic() - start) * 1000
            if cfg.workspace.input_policy != 'dataspace' and await blocking(workspace._inventory, source) != inventory:
                raise CapabilityError(
                    "workspace", "workspace changed during build; retry"
                )
            manifest = {
                "format_version": 2,
                "workspace_revision": revision,
                "parent_revision": parent,
                "parent_epoch": epoch,
                "source_root": str(source),
                "surfaces": sorted(selected),
                "pipeline_hash": pipeline,
                "input_policy": cfg.workspace.input_policy, "tables": table_catalog(tables.values()),
                "databases": native_databases,
                "sources": sources,
                "snapshots": bindings,
                "reused_sources": metrics["sources_reused"],
                "applied_skills": skills,
                "truncated_sources": [s["uri"] for s in sources if s["truncated"]],
            }
            _atomic_json(root / "manifest.json", manifest)
            start = time.monotonic()
            with workspace._lock(), workspace._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT revision,epoch FROM head WHERE singleton=1"
                ).fetchone() != (parent, epoch):
                    raise CapabilityError(
                        "workspace",
                        "publication conflict; another build published first",
                    )
                db.execute(
                    "UPDATE revisions SET state='PUBLISHED' WHERE id=?", (revision,)
                )
                db.execute(
                    "UPDATE head SET revision=?,epoch=epoch+1 WHERE singleton=1",
                    (revision,),
                )
            metrics["publication_ms"] = (time.monotonic() - start) * 1000
            return manifest
        except BaseException as exc:
            with workspace._connect() as db:
                db.execute(
                    "UPDATE revisions SET state='FAILED',error=? WHERE id=?",
                    (type(exc).__name__, revision),
                )
            raise
        finally:
            if embedding and callable(getattr(embedding, "aclose", None)):
                await embedding.aclose()


async def reader_system(workspace, manifest, selected):
    from datamind.capabilities.graph import GraphService, build_graph_tools
    from datamind.capabilities.db import build_db_tools
    from datamind.capabilities.kb import build_kb_tools
    from datamind.capabilities.kb.service import KBService
    from datamind.capabilities.kb.providers.lancedb_store import LanceDBVectorStore
    from datamind.core.registry import retriever_registry
    from datamind.core.contracts import ToolAccess
    from datamind.workspace_native import PinnedVectors, PinnedDB
    from datamind.workspace_graph import GraphView
    from datamind import runtime

    settings = workspace.settings.model_copy(deep=True)
    settings.hooks.path_allowlist_extra = [
        *settings.hooks.path_allowlist_extra,
        str(workspace.root),
        manifest["source_root"],
    ]
    system = await workspace._system(settings, set())
    try:
        if "kb" in selected:
            binding = manifest["snapshots"]["kb"]
            # A historical index must use the model that built its embeddings.
            current = settings.embedding.model_dump(mode="json", exclude={"api_key"})
            if digest(encoded(current)) != binding["embedding_fingerprint"]:
                raise ConfigError(
                    "Pinned KB embedding configuration changed; use the original configuration"
                )
            embedding = runtime.build_embedding(
                settings.embedding, fallback_llm=settings.llm
            )
            raw = LanceDBVectorStore(
                persist_dir=str(workspace.root / binding["path"]),
                collection_name="workspace",
                dimension=binding["dimension"],
            )
            store = PinnedVectors(raw, binding["version"])
            strategy = settings.retrieval.strategy
            if strategy not in {"simple", "hybrid"}:
                raise ConfigError(
                    "Incremental reads support simple and hybrid retrieval"
                )
            options = {"vector_store": store, "embedding": embedding}
            if strategy == "hybrid":
                options.update(
                    vector_failure_fallback=settings.retrieval.vector_failure_fallback,
                    vector_retries=settings.retrieval.vector_retries,
                    vector_timeout_s=settings.retrieval.vector_timeout_s,
                )
            retriever = retriever_registry.create(strategy, **options)
            kb = KBService(
                embedding=embedding,
                vector_store=store,
                retriever=retriever,
                data_dir=settings.data.data_dir,
                retrieval_cfg=settings.retrieval,
            )

            async def documents():
                return [
                    {
                        "path": s["uri"],
                        "source_version": s["version"],
                        "size": (workspace.root / s["snapshot"]).stat().st_size,
                    }
                    for s in manifest["sources"]
                ]

            kb.list_documents = documents
            system.services.kb = kb
            system.services.embedding = embedding
            system.tools.extend(
                [s for s in build_kb_tools(kb) if s.access != ToolAccess.WRITE]
            )
        if "graph" in selected:
            graph = GraphService(
                store=GraphView(
                    workspace.root,
                    {
                        s["graph_artifact"]
                        for s in manifest["sources"]
                        if s.get("graph_artifact")
                    },
                ),
                data_dir=settings.data.data_dir,
                storage_dir=workspace.root,
            )
            system.services.graph = graph
            system.tools.extend(
                [s for s in build_graph_tools(graph) if s.access != ToolAccess.WRITE]
            )
        if "db" in selected:
            db = PinnedDB(workspace, manifest["snapshots"]["db"])
            system.services.db = db
            system.tools.extend(
                [
                    s
                    for s in build_db_tools(db)
                    if s.access != ToolAccess.WRITE
                    and s.name not in {"db_query_nl", "db_query_nl_at_version"}
                ]
            )
        return system, settings
    except BaseException:
        await system.aclose()
        raise
