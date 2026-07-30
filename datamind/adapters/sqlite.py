"""Read-only SQLite reference adapter for the typed Query DataOp."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from datamind.dataops import (
    BindingRow,
    BindingSet,
    Evidence,
    Query,
    ResultKind,
    ResultStatus,
)
from datamind.kernel import (
    ExecutionContext,
    KernelValidationError,
    Provenance,
    SnapshotRef,
    SnapshotUnavailableError,
    SourceDescriptor,
    SourceExecutionError,
    SourceKind,
    SourceRef,
    Usage,
    thaw_json,
    utc_now,
)
from datamind.ports import SourceResult

_SQLITE_DENIED_ACTIONS = frozenset(
    getattr(sqlite3, name)
    for name in (
        "SQLITE_ALTER_TABLE",
        "SQLITE_ANALYZE",
        "SQLITE_ATTACH",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_DELETE",
        "SQLITE_DETACH",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_INSERT",
        "SQLITE_REINDEX",
        "SQLITE_UPDATE",
    )
    if hasattr(sqlite3, name)
)


@dataclass(frozen=True)
class SQLiteTable:
    columns: Tuple[str, ...]
    rows: Tuple[Tuple[Any, ...], ...]
    truncated: bool = False


class SQLiteReadSource:
    """Execute a single statement against an OS-level read-only connection."""

    def __init__(
        self,
        *,
        source_id: str,
        database_path: Path,
        display_name: str = "SQLite database",
        row_limit: int = 1000,
        timeout_seconds: float = 10.0,
    ) -> None:
        path = Path(database_path).expanduser().resolve()
        if not path.is_file():
            raise KernelValidationError(
                "SQLite database does not exist: {}".format(path)
            )
        if isinstance(row_limit, bool) or not isinstance(row_limit, int):
            raise KernelValidationError("SQLite row_limit must be an integer")
        if row_limit <= 0:
            raise KernelValidationError("SQLite row_limit must be positive")
        if timeout_seconds <= 0:
            raise KernelValidationError(
                "SQLite timeout_seconds must be positive"
            )
        self._path = path
        self._row_limit = row_limit
        self._timeout_seconds = float(timeout_seconds)
        ref = SourceRef(source_id, SourceKind.TABLE)
        initial_checksum = self._artifact_checksum()
        self._descriptor = SourceDescriptor(
            ref=ref,
            display_name=display_name,
            capabilities=frozenset(("query",)),
            version="sha256:{}".format(initial_checksum),
            schema={
                "result": {
                    "columns": "tuple[string]",
                    "rows": "tuple[tuple[any]]",
                }
            },
            metadata={
                "adapter": "sqlite_read_only",
                "database_name": path.name,
                "snapshot_semantics": "artifact_hash_after_read",
            },
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        return self._descriptor

    async def current_snapshot(self) -> SnapshotRef:
        checksum = await asyncio.to_thread(self._artifact_checksum)
        snapshot = self._snapshot(checksum)
        self._descriptor = replace(
            self._descriptor,
            version=snapshot.version,
        )
        return snapshot

    async def has_snapshot(self, snapshot: SnapshotRef) -> bool:
        if not isinstance(snapshot, SnapshotRef):
            return False
        current = await self.current_snapshot()
        return current.same_version_as(snapshot)

    async def execute(
        self,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        if not isinstance(operation, Query):
            raise SourceExecutionError(
                "SQLite source only supports Query"
            )
        if operation.language.casefold() not in ("sql", "sqlite"):
            raise SourceExecutionError(
                "SQLite source does not support query language {!r}".format(
                    operation.language
                )
            )
        pinned = context.snapshots.get(self.descriptor.ref)
        if pinned is not None and not await self.has_snapshot(pinned):
            raise SnapshotUnavailableError(
                "SQLite source {!r} cannot serve snapshot {!r}".format(
                    self.descriptor.ref.source_id,
                    pinned.version,
                )
            )
        try:
            columns, rows, truncated, latency_ms = await asyncio.to_thread(
                self._run_query,
                operation.statement,
                thaw_json(operation.parameters),
                context,
            )
        except sqlite3.Error as exc:
            raise SourceExecutionError(
                "SQLite rejected read query: {}".format(exc)
            ) from exc

        checksum = self._artifact_checksum()
        snapshot = self._snapshot(checksum)
        self._descriptor = replace(
            self._descriptor,
            version=snapshot.version,
        )
        if pinned is not None and not snapshot.same_version_as(pinned):
            raise SnapshotUnavailableError(
                "SQLite source {!r} changed during pinned execution".format(
                    self.descriptor.ref.source_id
                )
            )
        table = SQLiteTable(
            columns=columns,
            rows=rows,
            truncated=truncated,
        )
        evidence = []
        binding_rows = []
        provenance = []
        statement_hash = hashlib.sha256(
            operation.statement.encode("utf-8")
        ).hexdigest()[:16]
        for index, row in enumerate(rows):
            row_value = dict(zip(columns, row))
            origin = Provenance(
                source=self.descriptor.ref,
                locator="sqlite://{}/query/{}#row={}".format(
                    self.descriptor.ref.source_id,
                    statement_hash,
                    index,
                ),
                snapshot=snapshot,
            )
            provenance.append(origin)
            evidence_item = Evidence(
                kind=SourceKind.TABLE,
                content=json.dumps(
                    row_value,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                provenance=origin,
                metadata={"row_index": index},
            )
            evidence.append(evidence_item)
            binding_rows.append(
                BindingRow(
                    values={
                        column: self._binding_value(value)
                        for column, value in zip(columns, row)
                    },
                    evidence_ids=(evidence_item.evidence_id,),
                )
            )

        warnings = (
            (
                "SQLite result exceeded row_limit={} and was truncated".format(
                    self._row_limit
                ),
            )
            if truncated
            else ()
        )
        return SourceResult(
            value=table,
            result_kind=ResultKind.TABLE,
            evidence=tuple(evidence),
            bindings=BindingSet(
                fields=columns,
                rows=tuple(binding_rows),
            ),
            provenance=tuple(provenance),
            snapshots=(snapshot,),
            usage=Usage(latency_ms=latency_ms),
            warnings=warnings,
            status=ResultStatus.PARTIAL if truncated else ResultStatus.OK,
        )

    def _run_query(
        self,
        statement: str,
        parameters: Dict[str, Any],
        context: ExecutionContext,
    ) -> Tuple[Tuple[str, ...], Tuple[Tuple[Any, ...], ...], bool, int]:
        start = time.perf_counter()
        uri = "{}?mode=ro".format(self._path.as_uri())
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=self._timeout_seconds,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.set_authorizer(self._authorizer)
            if context.deadline is not None:
                connection.set_progress_handler(
                    lambda: int(utc_now() >= context.deadline),
                    1000,
                )
            cursor = connection.execute(statement, parameters)
            if cursor.description is None:
                raise SourceExecutionError(
                    "SQLite Query must produce a tabular result"
                )
            columns = tuple(item[0] for item in cursor.description)
            fetched = cursor.fetchmany(self._row_limit + 1)
            truncated = len(fetched) > self._row_limit
            rows = tuple(
                tuple(row)
                for row in fetched[: self._row_limit]
            )
        finally:
            connection.close()
        elapsed = max(0, round((time.perf_counter() - start) * 1000))
        return columns, rows, truncated, elapsed

    @staticmethod
    def _authorizer(
        action: int,
        argument_one: Any,
        argument_two: Any,
        database_name: Any,
        trigger_name: Any,
    ) -> int:
        del argument_one, argument_two, database_name, trigger_name
        return (
            sqlite3.SQLITE_DENY
            if action in _SQLITE_DENIED_ACTIONS
            else sqlite3.SQLITE_OK
        )

    @staticmethod
    def _binding_value(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "hex:{}".format(bytes(value).hex())
        return str(value)

    def _snapshot(self, checksum: str) -> SnapshotRef:
        return SnapshotRef(
            source=self.descriptor.ref,
            version="sha256:{}".format(checksum),
            checksum=checksum,
        )

    def _artifact_checksum(self) -> str:
        digest = hashlib.sha256()
        for path in self._artifact_paths():
            digest.update(path.name.encode("utf-8"))
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        return digest.hexdigest()

    def _artifact_paths(self) -> Iterable[Path]:
        yield self._path
        wal = Path(str(self._path) + "-wal")
        if wal.is_file():
            yield wal
