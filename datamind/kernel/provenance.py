"""Versioned provenance attached to normalized evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

from .errors import KernelValidationError
from .types import SnapshotRef, SourceRef, require_aware, utc_now


@dataclass(frozen=True)
class Provenance:
    source: SourceRef
    locator: str
    observed_at: datetime = field(default_factory=utc_now)
    snapshot: Optional[SnapshotRef] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    derived_from: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceRef):
            raise KernelValidationError(
                "provenance source must be a SourceRef"
            )
        if not isinstance(self.locator, str):
            raise KernelValidationError("provenance locator must be a string")
        if not self.locator.strip():
            raise KernelValidationError("provenance locator must be non-empty")
        if not isinstance(self.observed_at, datetime):
            raise KernelValidationError("observed_at must be a datetime")
        require_aware(self.observed_at, "observed_at")
        if self.valid_from is not None:
            require_aware(self.valid_from, "valid_from")
        if self.valid_to is not None:
            require_aware(self.valid_to, "valid_to")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_from > self.valid_to
        ):
            raise KernelValidationError("valid_from cannot be after valid_to")
        if self.snapshot is not None and self.snapshot.source != self.source:
            raise KernelValidationError(
                "provenance source must match snapshot source"
            )
        object.__setattr__(self, "derived_from", tuple(self.derived_from))
        if any(not item.strip() for item in self.derived_from):
            raise KernelValidationError("derived_from ids cannot be blank")
