"""Thread-safe in-memory adapter for traces and replay artifacts."""
from __future__ import annotations

from threading import RLock
from typing import Any, Dict, Mapping, Optional, Tuple

from datamind.kernel import (
    ExecutionTrace,
    TraceConflictError,
    TraceEvent,
    TraceEventKind,
    TraceNotFoundError,
)
from datamind.ports import RecordedPlan, RecordedResult


class InMemoryTraceStore:
    """Reference implementation; persistent deployments inject other stores."""

    def __init__(self) -> None:
        self._events: Dict[str, list] = {}
        self._plans: Dict[str, RecordedPlan] = {}
        self._results: Dict[Tuple[str, str], RecordedResult] = {}
        self._lock = RLock()

    async def start(
        self,
        trace_id: str,
        *,
        details: Mapping[str, Any],
    ) -> TraceEvent:
        with self._lock:
            if trace_id in self._events:
                raise TraceConflictError(
                    "trace {!r} already exists".format(trace_id)
                )
            event = TraceEvent(
                trace_id=trace_id,
                sequence=0,
                kind=TraceEventKind.PLAN_STARTED,
                details=details,
            )
            self._events[trace_id] = [event]
            return event

    async def append(
        self,
        trace_id: str,
        kind: TraceEventKind,
        *,
        op_id: Optional[str] = None,
        details: Mapping[str, Any],
    ) -> TraceEvent:
        with self._lock:
            events = self._events.get(trace_id)
            if events is None:
                raise TraceNotFoundError(
                    "trace {!r} does not exist".format(trace_id)
                )
            event = TraceEvent(
                trace_id=trace_id,
                sequence=len(events),
                kind=kind,
                op_id=op_id,
                details=details,
            )
            ExecutionTrace(
                trace_id=trace_id,
                events=tuple(events) + (event,),
            )
            events.append(event)
            return event

    async def get(self, trace_id: str) -> ExecutionTrace:
        with self._lock:
            events = self._events.get(trace_id)
            if events is None:
                raise TraceNotFoundError(
                    "trace {!r} does not exist".format(trace_id)
                )
            return ExecutionTrace(trace_id=trace_id, events=tuple(events))

    async def save_plan(
        self,
        trace_id: str,
        record: RecordedPlan,
    ) -> None:
        with self._lock:
            if trace_id in self._plans:
                raise TraceConflictError(
                    "plan artifact for trace {!r} already exists".format(
                        trace_id
                    )
                )
            self._plans[trace_id] = record

    async def load_plan(self, trace_id: str) -> RecordedPlan:
        with self._lock:
            record = self._plans.get(trace_id)
            if record is None:
                raise TraceNotFoundError(
                    "plan artifact for trace {!r} does not exist".format(
                        trace_id
                    )
                )
            return record

    async def save_result(
        self,
        trace_id: str,
        op_id: str,
        record: RecordedResult,
    ) -> None:
        key = (trace_id, op_id)
        with self._lock:
            if key in self._results:
                raise TraceConflictError(
                    "result artifact for trace {!r}, op {!r} already exists".format(
                        trace_id,
                        op_id,
                    )
                )
            self._results[key] = record

    async def load_result(
        self,
        trace_id: str,
        op_id: str,
    ) -> RecordedResult:
        key = (trace_id, op_id)
        with self._lock:
            record = self._results.get(key)
            if record is None:
                raise TraceNotFoundError(
                    "result artifact for trace {!r}, op {!r} does not exist".format(
                        trace_id,
                        op_id,
                    )
                )
            return record
