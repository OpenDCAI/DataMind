"""Append-only parent trace types for bounded request resolution."""
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


class ResolutionEventKind(str, Enum):
    RESOLUTION_STARTED = "resolution_started"
    PLAN_ATTEMPT_STARTED = "plan_attempt_started"
    PLAN_ATTEMPT_COMPLETED = "plan_attempt_completed"
    PLAN_ATTEMPT_FAILED = "plan_attempt_failed"
    RESOLUTION_COMPLETED = "resolution_completed"
    RESOLUTION_FAILED = "resolution_failed"

    def __str__(self) -> str:
        return self.value


_PLAN_ATTEMPT_KINDS = frozenset(
    (
        ResolutionEventKind.PLAN_ATTEMPT_STARTED,
        ResolutionEventKind.PLAN_ATTEMPT_COMPLETED,
        ResolutionEventKind.PLAN_ATTEMPT_FAILED,
    )
)
_TERMINAL_KINDS = frozenset(
    (
        ResolutionEventKind.RESOLUTION_COMPLETED,
        ResolutionEventKind.RESOLUTION_FAILED,
    )
)


@dataclass(frozen=True)
class ResolutionEvent:
    resolution_id: str
    sequence: int
    kind: ResolutionEventKind
    event_id: str = field(default_factory=lambda: new_id("event"))
    timestamp: datetime = field(default_factory=utc_now)
    attempt_number: Optional[int] = None
    trace_id: Optional[str] = None
    details: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        for name in ("resolution_id", "event_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "{} must be a non-empty string".format(name)
                )
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise KernelValidationError(
                "resolution event sequence must be an integer"
            )
        if self.sequence < 0:
            raise KernelValidationError(
                "resolution event sequence cannot be negative"
            )
        if not isinstance(self.kind, ResolutionEventKind):
            raise KernelValidationError(
                "resolution event kind must be ResolutionEventKind"
            )
        if not isinstance(self.timestamp, datetime):
            raise KernelValidationError(
                "resolution event timestamp must be a datetime"
            )
        require_aware(self.timestamp, "resolution event timestamp")
        is_attempt_event = self.kind in _PLAN_ATTEMPT_KINDS
        if is_attempt_event:
            if (
                isinstance(self.attempt_number, bool)
                or not isinstance(self.attempt_number, int)
                or self.attempt_number <= 0
            ):
                raise KernelValidationError(
                    "plan-attempt event requires a positive attempt_number"
                )
            if not isinstance(self.trace_id, str) or not self.trace_id.strip():
                raise KernelValidationError(
                    "plan-attempt event requires a child trace_id"
                )
        elif self.attempt_number is not None or self.trace_id is not None:
            raise KernelValidationError(
                "only plan-attempt events may identify an attempt or trace"
            )
        object.__setattr__(
            self,
            "details",
            freeze_json_object(self.details),
        )


@dataclass(frozen=True)
class ResolutionTrace:
    resolution_id: str
    events: Tuple[ResolutionEvent, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.resolution_id, str)
            or not self.resolution_id.strip()
        ):
            raise KernelValidationError(
                "resolution trace id must be non-empty"
            )
        object.__setattr__(self, "events", tuple(self.events))
        if not self.events:
            raise KernelValidationError(
                "resolution trace requires at least one event"
            )
        if any(
            not isinstance(item, ResolutionEvent)
            for item in self.events
        ):
            raise KernelValidationError(
                "resolution trace events must contain ResolutionEvent values"
            )
        for expected, event in enumerate(self.events):
            if event.resolution_id != self.resolution_id:
                raise KernelValidationError(
                    "resolution event belongs to a different trace"
                )
            if event.sequence != expected:
                raise KernelValidationError(
                    "resolution event sequence must be contiguous from zero"
                )
        if (
            self.events[0].kind
            is not ResolutionEventKind.RESOLUTION_STARTED
        ):
            raise KernelValidationError(
                "resolution trace must begin with RESOLUTION_STARTED"
            )
        terminal_indexes = [
            index
            for index, event in enumerate(self.events)
            if event.kind in _TERMINAL_KINDS
        ]
        if len(terminal_indexes) > 1:
            raise KernelValidationError(
                "resolution trace can have only one terminal event"
            )
        if terminal_indexes and terminal_indexes[0] != len(self.events) - 1:
            raise KernelValidationError(
                "no events may follow resolution termination"
            )
        states = {}
        trace_ids = {}
        for event in self.events:
            if event.kind is ResolutionEventKind.PLAN_ATTEMPT_STARTED:
                if event.attempt_number in states:
                    raise KernelValidationError(
                        "plan attempt cannot start more than once"
                    )
                if event.attempt_number != len(states) + 1:
                    raise KernelValidationError(
                        "plan attempts must start in contiguous order"
                    )
                states[event.attempt_number] = "started"
                trace_ids[event.attempt_number] = event.trace_id
            elif event.kind in (
                ResolutionEventKind.PLAN_ATTEMPT_COMPLETED,
                ResolutionEventKind.PLAN_ATTEMPT_FAILED,
            ):
                if states.get(event.attempt_number) != "started":
                    raise KernelValidationError(
                        "plan attempt must start before it terminates"
                    )
                if trace_ids[event.attempt_number] != event.trace_id:
                    raise KernelValidationError(
                        "plan attempt child trace_id cannot change"
                    )
                states[event.attempt_number] = "terminal"
        if terminal_indexes and any(
            state != "terminal" for state in states.values()
        ):
            raise KernelValidationError(
                "resolution cannot terminate with an open plan attempt"
            )
        if self.completed:
            if not states:
                raise KernelValidationError(
                    "completed resolution requires a plan attempt"
                )
            plan_terminals = [
                event.kind
                for event in self.events
                if event.kind in (
                    ResolutionEventKind.PLAN_ATTEMPT_COMPLETED,
                    ResolutionEventKind.PLAN_ATTEMPT_FAILED,
                )
            ]
            if (
                not plan_terminals
                or plan_terminals[-1]
                is not ResolutionEventKind.PLAN_ATTEMPT_COMPLETED
            ):
                raise KernelValidationError(
                    "completed resolution requires a completed final plan"
                )

    @property
    def completed(self) -> bool:
        return (
            self.events[-1].kind
            is ResolutionEventKind.RESOLUTION_COMPLETED
        )

    @property
    def failed(self) -> bool:
        return (
            self.events[-1].kind
            is ResolutionEventKind.RESOLUTION_FAILED
        )


__all__ = [
    "ResolutionEvent",
    "ResolutionEventKind",
    "ResolutionTrace",
]
