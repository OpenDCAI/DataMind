"""LanceDB-backed vector store provider."""
from __future__ import annotations

import asyncio
import re
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence

from datamind.core.logging import get_logger
from datamind.core.protocols import RetrievedChunk
from datamind.core.registry import vector_store_registry

_log = get_logger("vector_store.lancedb")
_REAL_COLUMNS = {"id", "document", "metadata"}

@vector_store_registry.register("lancedb")
class LanceDBVectorStore:
    """Persistent LanceDB collection, one per (profile, collection) tuple."""

    def __init__(
        self,
        *,
        persist_dir: str | Path,
        collection_name: str,
        dimension: int,
    ) -> None:
        try:
            import lancedb
        except ImportError as exc:
            from datamind.core.errors import ConfigError
            raise ConfigError("LanceDB requires pip install datamind[versioning]") from exc
        self.name = "lancedb"
        self._write_lock = threading.RLock()
        self.dimension = dimension
        self._persist_dir = Path(persist_dir)
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._collection_name = collection_name
        self._client = lancedb.connect(self._persist_dir)
        # We supply our own embeddings — disable the default model download.
        # Reopen an existing table without forcing a schema comparison:
        # LanceDB's `exist_ok=True` compares schemas literally and rejects
        # harmless differences (e.g. arrow extension types round-tripped
        # through disk). `open_table` skips the check entirely.
        if self._collection_name in self._client.table_names():
            self._collection = self._client.open_table(self._collection_name)
        else:
            self._collection = self._client.create_table(
                self._collection_name,
                schema=self._make_schema(),
                mode="create",
            )
        self._history_path = self._persist_dir / "version_history.sqlite3"
        with sqlite3.connect(self._history_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS history (collection TEXT, version INTEGER, payload TEXT, PRIMARY KEY(collection, version))")
        self.existing_count = int(self._collection.count_rows())
        _log.info(
            "lancedb_collection_ready",
            extra={
                "collection": collection_name,
                "path": str(self._persist_dir),
                "count": self._collection.count_rows(),
            },
        )

    def _make_schema(self):
        import pyarrow as pa
        return pa.schema([
            pa.field("id", pa.string()),
            pa.field("document", pa.string()),
            pa.field("embedding", pa.list_(pa.float32(), self.dimension)),
            pa.field("metadata", pa.json_()),
        ])

    def _dict_to_where(self, where: dict[str, Any]) -> str:
        """Build a LanceDB SQL WHERE clause.

        Keys matching real columns (id/document/metadata) are compared directly;
        other keys are looked up as JSON fields inside `metadata` via json_get_*.
        """
        if not where:
            return ""

        conditions: list[str] = []

        for key, value in where.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"Invalid field name: {key}")

            is_real = key in _REAL_COLUMNS
            def lhs(typ: str) -> str:
                return key if is_real else f"json_get_{typ}(metadata, '{key}')"

            if value is None:
                conditions.append(f"{lhs('string')} IS NULL")
            elif isinstance(value, bool):
                ref = key if is_real else f"json_get_bool(metadata, '{key}')"
                conditions.append(f"{ref} = {str(value).lower()}")
            elif isinstance(value, int):
                conditions.append(f"{lhs('int')} = {value}")
            elif isinstance(value, float):
                conditions.append(f"{lhs('float')} = {value}")
            elif isinstance(value, str):
                escaped = self._escape(value)
                conditions.append(f"{lhs('string')} = '{escaped}'")
            elif isinstance(value, (list, tuple)):
                items: list[str] = []
                for v in value:
                    if isinstance(v, str):
                        items.append(f"'{self._escape(v)}'")
                    elif isinstance(v, bool):
                        items.append(str(v).lower())
                    else:
                        items.append(str(v))
                conditions.append(f"{lhs('string')} IN ({', '.join(items)})")
            else:
                raise TypeError(f"Unsupported filter value type: {type(value)}")

        return " AND ".join(conditions)

    @staticmethod
    def _parse_metadata(raw: Any) -> dict[str, Any]:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        try:
            return dict(raw)
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _escape(value: str) -> str:
        """Escape a string for a single-quoted SQL literal (double the quote)."""
        return value.replace("'", "''")

    async def add(
        self,
        ids: Sequence[str],
        texts: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        if not ids:
            return

        rows = []
        for i in range(len(ids)):
            meta_dict = metadatas[i] if metadatas else {}
            rows.append({
                "id": ids[i],
                "document": texts[i],
                "embedding": list(embeddings[i]),
                "metadata": json.dumps(meta_dict),
            })

        def _upsert():
            with self._write_lock:
                self._collection.checkout_latest()
                self._collection.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(rows)
        await asyncio.to_thread(_upsert)
        self.existing_count = int(self._collection.count_rows())

    async def query(
        self,
        embedding: Sequence[float],
        *,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
        version: int | None = None
    ) -> list[RetrievedChunk]:
        try:
            def _search():
                table = self._client.open_table(self._collection_name)
                if version is not None:
                    table.checkout(version)
                q = table.search(embedding, query_type="vector")

                if where:
                    where_sql = self._dict_to_where(where)
                    if where_sql:
                        q = q.where(where_sql)

                return q.distance_type("cosine").limit(top_k).to_list()

            results = await asyncio.to_thread(_search)

            out: list[RetrievedChunk] = []
            for row in results:
                dist = row.get("_distance")
                meta = self._parse_metadata(row.get("metadata"))
                # Cosine distance -> cosine similarity (higher is better)
                score = float(1.0 - dist) if dist is not None else 0.0
                source = meta.pop("_source", None) or meta.pop("source", None)
                out.append(
                    RetrievedChunk(
                        id=str(row.get("id")),
                        text=row.get("document") or "",
                        score=score,
                        source=source,
                        metadata={k: v for k, v in meta.items() if k != "_"},
                    )
                )

            return out
        finally:
            pass  # Historical reads use a private table handle.

    async def count(self) -> int:
        return await asyncio.to_thread(self._collection.count_rows)

    async def delete(self, ids: Sequence[str]) -> None:
        if not ids:
            return

        def _delete():
            quoted = ", ".join(f"'{self._escape(i)}'" for i in ids)
            self._collection.delete(f"id IN ({quoted})")

        await asyncio.to_thread(_delete)
        self.existing_count = int(
            await asyncio.to_thread(self._collection.count_rows)
        )

    async def reset(self) -> None:
        def _reset() -> None:
            self._collection = self._client.create_table(
                self._collection_name,
                schema=self._make_schema(),
                mode="overwrite",
            )

        await asyncio.to_thread(_reset)
        self.existing_count = 0
        with sqlite3.connect(self._history_path) as conn:
            conn.execute("DELETE FROM history WHERE collection = ?", (self._collection_name,))

    async def get_all_texts(self, version: int | None = None) -> list[tuple[str, str, dict[str, Any]]]:
        """Enumerate (id, text, metadata) — used by lexical retrievers."""
        try:
            def _get_all_projected():
                table = self._client.open_table(self._collection_name)
                if version is not None:
                    table.checkout(version)
                rows = (
                    table.search(None)
                    .select(["id", "document", "metadata"])
                    .to_list()
                )
                return rows

            rows = await asyncio.to_thread(_get_all_projected)

        finally:
            pass  # Historical reads use a private table handle.

        out: list[tuple[str, str, dict[str, Any]]] = []
        for r in rows:
            cid = r.get("id")
            doc = r.get("document") or ""
            meta_raw = r.get("metadata") or "{}"
            import json
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else dict(meta_raw)
            meta.pop("_", None)
            out.append((str(cid), doc, meta))
        return out

    async def add_version(
        self,
        version: int,
        *,
        previous_version: int | None = None,
    ) -> dict[str, Any] | None:
        versions = await asyncio.to_thread(self._collection.list_versions)
        info = next((v for v in versions if v.get("version") == version), None)
        if info is None:
            return None

        info = dict(info)

        def _source_key(source: str) -> str:
            path = Path(source)
            return str(path.resolve(strict=False)) if path.is_absolute() else path.as_posix()

        async def _snapshot_files(snapshot_version: int) -> dict[str, dict[str, Any]]:
            rows = await self.get_all_texts(version=snapshot_version)
            files: dict[str, dict[str, Any]] = {}
            for chunk_id, text, metadata in rows:
                source = metadata.get("source_file") or metadata.get("source")
                if not source:
                    continue
                source = str(source)
                file = files.setdefault(
                    _source_key(source), {"source": source, "chunks": {}}
                )
                file["chunks"][chunk_id] = {
                    "text": text,
                    "metadata": metadata,
                }
            return files

        previous_files = (
            await _snapshot_files(previous_version)
            if previous_version is not None else {}
        )
        current_files = await _snapshot_files(version)
        changes = {"Add": [], "Update": [], "Delete": []}

        for source_key, current_file in current_files.items():
            previous_file = previous_files.get(source_key)
            if previous_file is None:
                changes["Add"].append(current_file["source"])
            elif {
                chunk_id: chunk["text"]
                for chunk_id, chunk in previous_file["chunks"].items()
            } != {
                chunk_id: chunk["text"]
                for chunk_id, chunk in current_file["chunks"].items()
            }:
                changes["Update"].append(current_file["source"])

        changes["Delete"].extend(
            previous_file["source"]
            for source_key, previous_file in previous_files.items()
            if source_key not in current_files
        )

        previous_chunks = {
            (source_key, chunk_id): {"source": file["source"], **chunk}
            for source_key, file in previous_files.items()
            for chunk_id, chunk in file["chunks"].items()
        }
        current_chunks = {
            (source_key, chunk_id): {"source": file["source"], **chunk}
            for source_key, file in current_files.items()
            for chunk_id, chunk in file["chunks"].items()
        }
        added_chunks = [
            {"id": chunk_id, **current_chunks[(source_key, chunk_id)]}
            for source_key, chunk_id in current_chunks.keys() - previous_chunks.keys()
        ]
        deleted_chunks = [
            {"id": chunk_id, **previous_chunks[(source_key, chunk_id)]}
            for source_key, chunk_id in previous_chunks.keys() - current_chunks.keys()
        ]
        info["chunks_added"] = len(added_chunks)
        info["chunks_deleted"] = len(deleted_chunks)
        info["added_chunks"] = sorted(added_chunks, key=lambda chunk: (chunk["source"], chunk["id"]))
        info["deleted_chunks"] = sorted(deleted_chunks, key=lambda chunk: (chunk["source"], chunk["id"]))
        info["msg"] = changes
        with sqlite3.connect(self._history_path) as conn:
            conn.execute("INSERT OR REPLACE INTO history VALUES (?, ?, ?)",
                         (self._collection_name, version, json.dumps(info, default=str)))
        return info

    @property
    def latest_version(self) -> int:
        return self._collection.version

    async def _checkout_latest(self):
        await asyncio.to_thread(self._collection.checkout_latest)

    async def list_versions(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self._history_path) as conn:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM history WHERE collection = ? ORDER BY version",
                (self._collection_name,),
            )]

    async def rollback(self, version: int) -> dict[str, Any]:
        previous = self.latest_version
        def _restore():
            with self._write_lock:
                self._collection.checkout_latest()
                self._collection.restore(version)
                self.existing_count = self._collection.count_rows()
                return self._collection.version
        current = await asyncio.to_thread(_restore)
        info = await self.add_version(current, previous_version=previous)
        return {"version": current, "count": self.existing_count, "details": info}
