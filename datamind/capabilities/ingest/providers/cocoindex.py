"""CocoIndex-backed KB indexer for the store_agent surface.

This module wires `CocoIndex <https://cocoindex.io/>`_ into DataMind as a
drop-in KB backend. It walks a source directory, extracts document text
via `datamind.capabilities.ingest.formats`, splits it into chunks using
the same helpers as `datamind.capabilities.kb.indexer`, embeds each chunk
with the configured embedding provider, and writes the results into a
LanceDB table.

CocoIndex handles the incremental bookkeeping: on each `update()` only
files whose contents or embedder identity changed are reprocessed, and
derived rows are refreshed accordingly. State is kept under
`storage_dir/<name>_state` so re-runs are cheap.

Key pieces:
    - `KBRecord`          : the LanceDB row schema (id / document /
                            embedding / metadata).
    - `process_file`      : per-file flow, memoized on file + chunking
                            params.
    - `process_chunk`     : per-chunk embed + `declare_row`.
    - `app_main`          : mounts the file walker and the LanceDB target.
    - `CocoIndexBackend`  : async wrapper exposing `update()` to the rest
                            of DataMind; lazily builds the CocoIndex `App`
                            on first call.

Path safety: every file is resolved through `_resolve_safe_path` against
`allow_roots` before extraction, so a symlink or `..` cannot escape the
configured data roots.
"""

from __future__ import annotations

import asyncio
import pathlib
import json
import numpy as np
import pyarrow as pa

from pathlib import Path
from typing import Any, Annotated
from collections import defaultdict
from dataclasses import dataclass

import cocoindex as coco
from cocoindex.connectors import localfs, lancedb
from cocoindex.resources.file import FileLike, PatternFilePathMatcher
from cocoindex.connectors.lancedb import LanceType

from datamind.capabilities.kb.indexer import (
    Chunk,
    _hash,
    _split_text,
)
from datamind.capabilities.ingest.formats import extract_document, DOCUMENT_EXTS
from datamind.capabilities.ingest.service import _resolve_safe_path

LANCE_DB = coco.ContextKey[lancedb.LanceAsyncConnection]("datamind_v2")
EMBEDDER = coco.ContextKey[Any]("embedding_provider", detect_change=True)
TARGET_SCHEMA = coco.ContextKey[lancedb.TableSchema]("lance_table_schema")
ALLOW_ROOTS = coco.ContextKey[list[pathlib.Path]]("allow_roots")

@dataclass
class KBRecord:
    id: str
    document: str
    embedding: list[float]
    metadata: str

@coco.fn
async def process_chunk(
    chunk: Chunk,
    table: lancedb.TableTarget[Any],
) -> None:
    provider = coco.use_context(EMBEDDER)
    embedding = (await provider.embed_texts([chunk.text]))[0]
    table.declare_row(
        row=KBRecord(
            id=chunk.id,
            document=chunk.text,
            embedding=embedding,
            metadata=json.dumps(chunk.metadata or {}, ensure_ascii=False),
        )
    )


@coco.fn(memo=True)
async def process_file(
    file: FileLike,
    table: lancedb.TableTarget[Any],
    chunk_size: int,
    chunk_overlap: int,
) -> None:
    allow_roots = coco.use_context(ALLOW_ROOTS)
    resolve = _resolve_safe_path(str(file.file_path), allow_roots)
    
    extracted = extract_document(resolve)
    text = extracted.text
    source = str(resolve)
    chunks = [
        Chunk(
            id=_hash(segment, source, ordinal=ordinal),
            text=segment,
            source=source,
            metadata={"_origin": "store_agent", "_chunk_ordinal": ordinal, "source": source},
        )
        for ordinal, segment in enumerate(
            _split_text(
                text,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        )
    ]
    await coco.map(process_chunk, chunks, table)


@coco.fn
async def app_main(
    sourcedir: pathlib.Path,
    table_name: str,
    chunk_size: int,
    chunk_overlap: int,
) -> None:
    table_schema = coco.use_context(TARGET_SCHEMA)
    target_table = await lancedb.mount_table_target(
        LANCE_DB,
        table_name=table_name,
        table_schema=table_schema,
    )

    files = localfs.walk_dir(
        sourcedir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=[f"**/*{ext}" for ext in DOCUMENT_EXTS],
            excluded_patterns=["**/.*", "chunks/**", "builds/**", "triplets/**"],
        ),
        live=False,  # source supports live watch; pass -L to `cocoindex update` to actually run live
    )
    await coco.mount_each(
        process_file, 
        files.items(), 
        target_table, 
        chunk_size, 
        chunk_overlap, 
    )


class CocoIndexBackend:
    def __init__(
        self,
        *,
        data_dir: pathlib.Path,
        storage_dir: pathlib.Path,
        table_name: str,
        lancedb_uri: str,
        embedding_provider: Any,
        chunk_size: int,
        chunk_overlap: int,
        allow_roots: list[Path]
    ):
        self._data_dir = pathlib.Path(data_dir).resolve()
        self._storage_dir = pathlib.Path(storage_dir).resolve()
        self._table_name = table_name
        self._lancedb_uri = lancedb_uri
        self._embedding_provider = embedding_provider
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._allow_roots = allow_roots
        
        self._app = None
        self._environment = None
        self._lock = asyncio.Lock()

    async def _read_chunks(self) -> tuple[dict[str, dict], int | None]:
        conn = self._environment.get_context(LANCE_DB)

        if self._table_name not in await conn.table_names():
            return {}, None

        table = await conn.open_table(self._table_name)
        version = await table.version()
        
        rows = await table.query().select(["id", "document", "metadata"]).to_list()
        chunk_map = {}
        for row in rows:
            metadata = row.get("metadata")
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (ValueError, TypeError):
                    metadata = {}
            chunk_map[row["id"]] = {
                "document": row["document"],
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        rows = chunk_map
        return rows, version

    async def update(self) -> dict[str, Any]:
        async with self._lock:
            if self._app is None:
                state_dir = (
                    self._storage_dir
                    / f"{self._storage_dir.name}_state"
                )
                state_dir.mkdir(parents=True, exist_ok=True)

                conn = await lancedb.connect_async(self._lancedb_uri)

                real_table_schema = await lancedb.TableSchema.from_class(
                    KBRecord,
                    primary_key=["id"],
                    column_specs={
                        "embedding": LanceType(pa.list_(pa.float32(), self._embedding_provider.dimension)),
                        "metadata": LanceType(pa.json_()),
                    }
                )

                environment = coco.Environment(
                    coco.Settings(db_path=state_dir),
                )
                environment.context_provider.provide(LANCE_DB, conn)
                environment.context_provider.provide(
                    EMBEDDER, self._embedding_provider,
                )
                environment.context_provider.provide(TARGET_SCHEMA, real_table_schema)
                environment.context_provider.provide(ALLOW_ROOTS, self._allow_roots)
                
                self._app = coco.App(
                    coco.AppConfig(
                        name="datamind_kb",
                        environment=environment,
                    ),
                    app_main,
                    sourcedir=self._data_dir,
                    table_name=self._table_name,
                    chunk_size=self._chunk_size,
                    chunk_overlap=self._chunk_overlap
                )
                self._environment = environment

            await self._app.update()
            return {"status": "updated"}


def get_backend(
    *,
    data_dir: pathlib.Path,
    storage_dir: pathlib.Path,
    table_name: str,
    lancedb_uri: str,
    embedding_provider: Any,
    chunk_size: int,
    chunk_overlap: int, 
    allow_roots: list[Path]
) -> CocoIndexBackend:
    return CocoIndexBackend(
        data_dir=data_dir,
        storage_dir=storage_dir,
        table_name=table_name,
        lancedb_uri=lancedb_uri,
        embedding_provider=embedding_provider,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        allow_roots=allow_roots,
    )