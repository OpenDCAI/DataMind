"""Ports for artifact resolution, source versioning, and synchronization."""
from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from datamind.kernel import (
    ArtifactRef,
    ChangeSet,
    SnapshotRef,
    SyncReceipt,
)


class ArtifactStore(Protocol):
    """Resolve immutable artifact bytes by their manifest identity."""

    async def load(self, ref: ArtifactRef) -> bytes:
        ...


@runtime_checkable
class SnapshotSource(Protocol):
    """A source that can expose and validate immutable versions."""

    async def current_snapshot(self) -> SnapshotRef:
        ...

    async def has_snapshot(self, snapshot: SnapshotRef) -> bool:
        ...


@runtime_checkable
class SyncTarget(SnapshotSource, Protocol):
    """A versioned source capable of atomically applying a ChangeSet."""

    async def apply_changes(
        self,
        change_set: ChangeSet,
        *,
        artifacts: Mapping[ArtifactRef, bytes],
    ) -> SnapshotRef:
        ...


class LifecyclePort(Protocol):
    """Public synchronization surface consumed by the thin Engine API."""

    async def sync(self, change_set: ChangeSet) -> SyncReceipt:
        ...


__all__ = [
    "ArtifactStore",
    "LifecyclePort",
    "SnapshotSource",
    "SyncTarget",
]
