"""Idempotent receipt ledger for StoreAgent tool calls."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable

from datamind.core.contracts import (
    DataSurface,
    IngestReceipt,
    SourceRef,
    SurfaceWriteResult,
)
from datamind.core.tools import ToolRegistry, ToolSpec
from datamind.core.logging import current_context


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _directory_source(path: Path, *, tool_name: str) -> SourceRef:
    digest = hashlib.sha256()
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(child.relative_to(path)).encode("utf-8"))
        with child.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    hexdigest = digest.hexdigest()
    return SourceRef(
        source_id=f"directory:{hexdigest[:24]}",
        kind="file",
        uri=str(path),
        checksum=f"sha256:{hexdigest}",
        metadata={"tool": tool_name},
    )


def _source_from_call(spec: ToolSpec, args: dict[str, Any]) -> SourceRef:
    tool_name = spec.name
    path_value = args.get("path")
    if tool_name == "workspace_update" and isinstance(path_value, str):
        uri = str(Path(path_value).expanduser().resolve())
        return SourceRef(source_id="workspace:" + _hash_text(uri), kind="file", uri=uri,
                         metadata={"tool": tool_name})
    if isinstance(path_value, str) and path_value:
        path = Path(path_value).expanduser().resolve(strict=False)
        if path.is_file():
            return SourceRef.from_file(path, tool=tool_name)
        if path.is_dir():
            return _directory_source(path, tool_name=tool_name)
        digest = _hash_text(str(path))
        return SourceRef(
            source_id=f"path:{digest[:24]}",
            kind="file",
            uri=str(path),
            metadata={"tool": tool_name, "unresolved": True},
        )

    source_path = spec.metadata.get("source_path")
    if isinstance(source_path, str):
        path = Path(source_path).expanduser().resolve(strict=False)
        if path.is_dir():
            return _directory_source(path, tool_name=tool_name)

    if tool_name == "kb_add_text":
        return SourceRef.from_text(str(args.get("text", "")), kind="text", tool=tool_name)
    if tool_name.startswith("graph_"):
        payload = str(args.get("text") or _canonical(args.get("triples", [])))
        return SourceRef.from_text(payload, kind="triples", tool=tool_name)
    if tool_name.startswith("skill_"):
        return SourceRef.from_text(
            str(args.get("body", "")),
            kind="skill",
            tool=tool_name,
            name=args.get("name"),
        )
    if tool_name.startswith("memory_"):
        payload = str(args.get("content") or args.get("item_id") or "")
        return SourceRef.from_text(payload, kind="memory", tool=tool_name)

    payload = _canonical(args)
    digest = _hash_text(payload)
    return SourceRef(
        source_id=f"records:{digest[:24]}",
        kind="records",
        checksum=f"sha256:{digest}",
        metadata={"tool": tool_name},
    )


def _items_written(result: Any) -> int:
    if not isinstance(result, dict):
        return 1 if result is not None else 0
    for key in (
        "chunks_added",
        "rows_inserted",
        "triples_added",
        "upserted",
        "total_embedded",
        "files_processed",
    ):
        value = result.get(key)
        if isinstance(value, int):
            return value
    return 1 if result else 0


class IngestLedger:
    """Append-only receipt log plus a compact successful-call index."""

    def __init__(self, *, storage_dir: Path, profile: str) -> None:
        self._storage_dir = Path(storage_dir)
        self._profile = profile
        self._state_path = self._storage_dir / "ingest_state.json"
        self._receipts_path = self._storage_dir / "ingest_receipts.jsonl"
        self._lock = asyncio.Lock()
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        return sqlite3.connect(self._storage_dir / "ingest_ledger.sqlite3", timeout=30)

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS ledger_state (singleton INTEGER PRIMARY KEY, revision INTEGER);
                CREATE TABLE IF NOT EXISTS successful (fingerprint TEXT PRIMARY KEY, receipt_id TEXT);
                CREATE TABLE IF NOT EXISTS receipts (receipt_id TEXT PRIMARY KEY, payload TEXT);
            """)
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT revision FROM ledger_state WHERE singleton=1").fetchone() is None:
                try:
                    legacy = json.loads(self._state_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    legacy = {"revision": 0, "successful": {}}
                conn.execute("INSERT INTO ledger_state VALUES (1, ?)", (legacy.get("revision", 0),))
                conn.executemany("INSERT OR IGNORE INTO successful VALUES (?, ?)",
                                 list(legacy.get("successful", {}).items()))

    def _previous(self, fingerprint: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT receipt_id FROM successful WHERE fingerprint=?", (fingerprint,)).fetchone()
        return row[0] if row else None

    def _save_state(self, state: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision = conn.execute("SELECT revision FROM ledger_state WHERE singleton=1").fetchone()[0] + 1
            conn.execute("UPDATE ledger_state SET revision=? WHERE singleton=1", (revision,))
            conn.executemany("INSERT OR REPLACE INTO successful VALUES (?, ?)", state["successful"].items())
            state["revision"] = revision

    def _append(self, receipt: IngestReceipt) -> None:
        context = current_context()
        if context is not None:
            receipt.applied_skills = list(context.extra.get("applied_skills", {}).values())
        payload = receipt.model_dump_json()
        # SQLite is the durable receipt store; the serialized JSONL append remains
        # for existing tooling. Cross-process writers cannot interleave log lines.
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?)", (receipt.receipt_id, payload))
            with self._receipts_path.open("a", encoding="utf-8") as fh:
                fh.write(payload + "\n")

    @property
    def revision(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT revision FROM ledger_state WHERE singleton=1").fetchone()[0]

    async def execute(
        self,
        *,
        spec: ToolSpec,
        args: dict[str, Any],
        invoke: Callable[..., Awaitable[Any]],
    ) -> dict[str, Any]:
        """Deduplicate retryable imports, but always apply explicit DB replacement."""
        source = _source_from_call(spec, args)
        fingerprint = _hash_text(
            _canonical(
                {
                    "tool": spec.name,
                    "args": args,
                    "source_checksum": source.checksum,
                }
            )
        )
        surface = spec.surface
        if surface is None:
            raise ValueError(f"Store tool '{spec.name}' does not declare a surface")

        async with self._lock:
            state = {"revision": self.revision, "successful": {}}
            previous = self._previous(fingerprint)
            # A receipt proves that these arguments succeeded in the past, not
            # that the target table still contains those rows. Replacements
            # must execute even for A -> B -> A (or an out-of-band DB change).
            # Reapplying replace is safe from duplicate appends; append/fail
            # imports retain their existing receipt-based retry behavior.
            replaces_table = (
                spec.name in {"db_import_records", "db_import_csv", "db_import_path"}
                and args.get("if_exists") == "replace"
            )
            replaces_source = spec.name in {
                "kb_add_file", "kb_add_path", "kb_update_workspace", "kb_delete_file", "workspace_update",
                "graph_add_path", "graph_add_triples_from_text", "graph_upsert_triples",
                "db_update_csv", "db_update_path", "db_update_records", "db_delete_path",
            }
            if previous and not replaces_table and not replaces_source:
                receipt = IngestReceipt(
                    profile=self._profile,
                    revision=int(state["revision"]),
                    source=source,
                    results=[
                        SurfaceWriteResult(
                            surface=surface,
                            operation=spec.name,
                            status="unchanged",
                            items_written=0,
                            details={"duplicate_of": previous},
                        )
                    ],
                )
                self._append(receipt)
                return receipt.model_dump(mode="json")

            try:
                result = await invoke(**args)
            except Exception as exc:  # receipt is retained even when a write fails
                receipt = IngestReceipt(
                    profile=self._profile,
                    revision=int(state["revision"]),
                    source=source,
                    results=[
                        SurfaceWriteResult(
                            surface=surface,
                            operation=spec.name,
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    ],
                )
                self._append(receipt)
                return receipt.model_dump(mode="json")

            if isinstance(result, dict) and result.get("skipped_count", 0):
                receipt = IngestReceipt(
                    profile=self._profile, revision=int(state["revision"]), source=source,
                    results=[SurfaceWriteResult(surface=surface, operation=spec.name,
                        status="failed", items_written=_items_written(result), details=result,
                        error="Some sources failed; successful writes may exist. Retry failed sources.")],
                )
                self._append(receipt)
                return receipt.model_dump(mode="json")
            state["revision"] = int(state["revision"]) + 1
            receipt = IngestReceipt(
                profile=self._profile,
                revision=int(state["revision"]),
                source=source,
                results=[
                    SurfaceWriteResult(
                        surface=surface,
                        operation=spec.name,
                        items_written=_items_written(result),
                        details=result if isinstance(result, dict) else {"result": result},
                    )
                ],
            )
            if spec.metadata.get("multi_surface") and isinstance(result, dict):
                receipt.results = [SurfaceWriteResult(
                    surface=name, operation=spec.name, items_written=len(result.get("sources", [])),
                    details=result,
                ) for name in result.get("surfaces", [])]
            state["successful"][fingerprint] = receipt.receipt_id
            self._save_state(state)
            receipt.revision = state["revision"]
            self._append(receipt)
            return receipt.model_dump(mode="json")


def with_receipts(registry: ToolRegistry, ledger: IngestLedger) -> ToolRegistry:
    """Wrap every StoreAgent tool so its result is an IngestReceipt."""
    wrapped = ToolRegistry()
    for original in (registry.get(name) for name in registry.names()):
        async def _handler(
            _spec: ToolSpec = original,
            **kwargs: Any,
        ) -> dict[str, Any]:
            # Apply handler defaults before fingerprinting so omitted defaults
            # and explicitly supplied defaults remain the same idempotent call.
            try:
                bound = inspect.signature(_spec.handler).bind_partial(**kwargs)
                bound.apply_defaults()
                normalized = dict(bound.arguments)
            except (TypeError, ValueError):
                normalized = dict(kwargs)
            return await ledger.execute(spec=_spec, args=normalized, invoke=_spec.handler)

        wrapped.add(
            ToolSpec(
                name=original.name,
                description=original.description,
                input_schema=original.input_schema,
                handler=_handler,
                metadata={**original.metadata, "receipt": True},
            )
        )
    return wrapped


__all__ = ["IngestLedger", "with_receipts"]
