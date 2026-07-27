"""Append-only JSONL adapter for content-safe audit trace events."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Optional

from datamind.kernel import (
    ExecutionTrace,
    TraceConflictError,
    TraceError,
    TraceEvent,
    TraceEventKind,
    TraceNotFoundError,
    thaw_json,
)


class JsonlTraceStore:
    """Single-process reference store with durable, append-only audit files."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory).expanduser().resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    async def start(
        self,
        trace_id: str,
        *,
        details: Mapping[str, Any],
    ) -> TraceEvent:
        return await asyncio.to_thread(
            self._start_sync,
            trace_id,
            details,
        )

    async def append(
        self,
        trace_id: str,
        kind: TraceEventKind,
        *,
        op_id: Optional[str] = None,
        details: Mapping[str, Any],
    ) -> TraceEvent:
        return await asyncio.to_thread(
            self._append_sync,
            trace_id,
            kind,
            op_id,
            details,
        )

    async def get(self, trace_id: str) -> ExecutionTrace:
        return await asyncio.to_thread(self._get_sync, trace_id)

    def _start_sync(
        self,
        trace_id: str,
        details: Mapping[str, Any],
    ) -> TraceEvent:
        event = TraceEvent(
            trace_id=trace_id,
            sequence=0,
            kind=TraceEventKind.PLAN_STARTED,
            details=details,
        )
        path = self._path(trace_id)
        with self._lock:
            try:
                with path.open("x", encoding="utf-8") as handle:
                    self._write_event(handle, event)
            except FileExistsError as exc:
                raise TraceConflictError(
                    "trace {!r} already exists".format(trace_id)
                ) from exc
        return event

    def _append_sync(
        self,
        trace_id: str,
        kind: TraceEventKind,
        op_id: Optional[str],
        details: Mapping[str, Any],
    ) -> TraceEvent:
        path = self._path(trace_id)
        with self._lock:
            trace = self._get_sync(trace_id)
            event = TraceEvent(
                trace_id=trace_id,
                sequence=len(trace.events),
                kind=kind,
                op_id=op_id,
                details=details,
            )
            ExecutionTrace(
                trace_id=trace_id,
                events=trace.events + (event,),
            )
            with path.open("a", encoding="utf-8") as handle:
                self._write_event(handle, event)
        return event

    def _get_sync(self, trace_id: str) -> ExecutionTrace:
        path = self._path(trace_id)
        with self._lock:
            if not path.is_file():
                raise TraceNotFoundError(
                    "trace {!r} does not exist".format(trace_id)
                )
            events = []
            line_number = 0
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        events.append(
                            self._event_from_dict(json.loads(line))
                        )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TraceError(
                    "corrupt trace {!r} near line {}".format(
                        trace_id,
                        line_number,
                    )
                ) from exc
        return ExecutionTrace(trace_id=trace_id, events=tuple(events))

    @staticmethod
    def _write_event(handle: Any, event: TraceEvent) -> None:
        payload = {
            "event_id": event.event_id,
            "trace_id": event.trace_id,
            "sequence": event.sequence,
            "kind": event.kind.value,
            "timestamp": event.timestamp.isoformat(),
            "op_id": event.op_id,
            "details": thaw_json(event.details),
        }
        handle.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    @staticmethod
    def _event_from_dict(payload: Mapping[str, Any]) -> TraceEvent:
        return TraceEvent(
            event_id=str(payload["event_id"]),
            trace_id=str(payload["trace_id"]),
            sequence=int(payload["sequence"]),
            kind=TraceEventKind(str(payload["kind"])),
            timestamp=datetime.fromisoformat(str(payload["timestamp"])),
            op_id=(
                str(payload["op_id"])
                if payload.get("op_id") is not None
                else None
            ),
            details=payload.get("details", {}),
        )

    def _path(self, trace_id: str) -> Path:
        digest = hashlib.sha256(trace_id.encode("utf-8")).hexdigest()
        return self._directory / "{}.jsonl".format(digest)


__all__ = ["JsonlTraceStore"]
