"""Content-safe execution trace event types."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .errors import KernelValidationError
from .types import (
    JsonObject,
    freeze_json_object,
    new_id,
    require_aware,
    utc_now,
)


class TraceEventKind(str, Enum):
    PLAN_STARTED = "plan_started"
    PLAN_VALIDATED = "plan_validated"
    OP_STARTED = "op_started"
    OP_COMPLETED = "op_completed"
    OP_FAILED = "op_failed"
    PLAN_COMPLETED = "plan_completed"
    PLAN_FAILED = "plan_failed"
    REPLAY_COMPLETED = "replay_completed"
    REPLAY_FAILED = "replay_failed"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TraceEvent:
    trace_id: str
    sequence: int
    kind: TraceEventKind
    event_id: str = field(default_factory=lambda: new_id("event"))
    timestamp: datetime = field(default_factory=utc_now)
    op_id: Optional[str] = None
    details: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        for name in ("trace_id", "event_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "{} must be a non-empty string".format(name)
                )
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise KernelValidationError("trace sequence must be an integer")
        if self.sequence < 0:
            raise KernelValidationError("trace sequence cannot be negative")
        if not isinstance(self.kind, TraceEventKind):
            raise KernelValidationError(
                "trace event kind must be a TraceEventKind"
            )
        if not isinstance(self.timestamp, datetime):
            raise KernelValidationError(
                "trace event timestamp must be a datetime"
            )
        require_aware(self.timestamp, "trace event timestamp")
        if self.op_id is not None:
            if not isinstance(self.op_id, str) or not self.op_id.strip():
                raise KernelValidationError(
                    "trace op_id must be a non-empty string"
                )
        object.__setattr__(
            self,
            "details",
            freeze_json_object(self.details),
        )


@dataclass(frozen=True)
class ExecutionTrace:
    trace_id: str
    events: Tuple[TraceEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise KernelValidationError(
                "execution trace_id must be a non-empty string"
            )
        object.__setattr__(self, "events", tuple(self.events))
        if not self.events:
            raise KernelValidationError(
                "execution trace requires at least one event"
            )
        if any(not isinstance(item, TraceEvent) for item in self.events):
            raise KernelValidationError(
                "execution trace events must contain TraceEvent values"
            )
        for expected, event in enumerate(self.events):
            if event.trace_id != self.trace_id:
                raise KernelValidationError(
                    "trace event belongs to a different trace"
                )
            if event.sequence != expected:
                raise KernelValidationError(
                    "trace event sequence must be contiguous from zero"
                )
        if self.events[0].kind is not TraceEventKind.PLAN_STARTED:
            raise KernelValidationError(
                "execution trace must begin with PLAN_STARTED"
            )
        terminal_index = None
        for index, event in enumerate(self.events):
            if event.kind in (
                TraceEventKind.PLAN_COMPLETED,
                TraceEventKind.PLAN_FAILED,
            ):
                if terminal_index is not None:
                    raise KernelValidationError(
                        "execution trace can have only one plan terminal event"
                    )
                terminal_index = index
            if event.kind in (
                TraceEventKind.REPLAY_COMPLETED,
                TraceEventKind.REPLAY_FAILED,
            ) and terminal_index is None:
                raise KernelValidationError(
                    "replay events require a terminal plan event"
                )
        if terminal_index is not None:
            replay_kinds = (
                TraceEventKind.REPLAY_COMPLETED,
                TraceEventKind.REPLAY_FAILED,
            )
            if any(
                event.kind not in replay_kinds
                for event in self.events[terminal_index + 1 :]
            ):
                raise KernelValidationError(
                    "only replay events may follow plan termination"
                )

    @property
    def completed(self) -> bool:
        return any(
            event.kind is TraceEventKind.PLAN_COMPLETED
            for event in self.events
        )

    @property
    def failed(self) -> bool:
        return any(
            event.kind is TraceEventKind.PLAN_FAILED
            for event in self.events
        )
