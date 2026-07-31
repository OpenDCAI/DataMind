"""Storage contract for content-safe parent resolution traces."""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from datamind.kernel import (
    ResolutionEvent,
    ResolutionEventKind,
    ResolutionTrace,
)


@runtime_checkable
class ResolutionTraceStore(Protocol):
    """Append-only storage independent from per-plan execution traces."""

    async def start_resolution(
        self,
        resolution_id: str,
        *,
        details: Mapping[str, Any],
    ) -> ResolutionEvent:
        ...

    async def append_resolution(
        self,
        resolution_id: str,
        kind: ResolutionEventKind,
        *,
        attempt_number: Optional[int] = None,
        trace_id: Optional[str] = None,
        details: Mapping[str, Any],
    ) -> ResolutionEvent:
        ...

    async def get_resolution(
        self,
        resolution_id: str,
    ) -> ResolutionTrace:
        ...


__all__ = ["ResolutionTraceStore"]
