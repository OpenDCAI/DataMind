"""Versioned artifact and snapshot values shared across Core layers."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .errors import KernelValidationError, SnapshotUnavailableError
from .types import (
    JsonObject,
    SnapshotRef,
    SourceRef,
    freeze_json_object,
    new_id,
    require_aware,
    utc_now,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def sha256_checksum(content: bytes) -> str:
    """Return the canonical checksum used by lifecycle manifests."""

    if not isinstance(content, bytes):
        raise KernelValidationError("artifact content must be bytes")
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class ArtifactRef:
    """Identity of one immutable artifact version."""

    artifact_id: str
    version: str

    def __post_init__(self) -> None:
        for name in ("artifact_id", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "{} must be a non-empty string".format(name)
                )


@dataclass(frozen=True)
class ArtifactManifest:
    """Content-free metadata required to locate and verify an artifact."""

    ref: ArtifactRef
    source: SourceRef
    checksum: str
    locator: str
    media_type: str = "application/octet-stream"
    created_at: datetime = field(default_factory=utc_now)
    data_schema: JsonObject = field(default_factory=freeze_json_object)
    lineage: Tuple[ArtifactRef, ...] = ()
    metadata: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ArtifactRef):
            raise KernelValidationError("manifest ref must be an ArtifactRef")
        if not isinstance(self.source, SourceRef):
            raise KernelValidationError("manifest source must be a SourceRef")
        if (
            not isinstance(self.checksum, str)
            or _SHA256_PATTERN.fullmatch(self.checksum) is None
        ):
            raise KernelValidationError(
                "manifest checksum must be a lowercase SHA-256 hex digest"
            )
        for name in ("locator", "media_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "manifest {} must be a non-empty string".format(name)
                )
        if not isinstance(self.created_at, datetime):
            raise KernelValidationError(
                "manifest created_at must be a datetime"
            )
        require_aware(self.created_at, "manifest created_at")
        object.__setattr__(
            self,
            "data_schema",
            freeze_json_object(self.data_schema),
        )
        object.__setattr__(self, "lineage", tuple(self.lineage))
        if any(not isinstance(item, ArtifactRef) for item in self.lineage):
            raise KernelValidationError(
                "manifest lineage must contain ArtifactRef values"
            )
        if len(set(self.lineage)) != len(self.lineage):
            raise KernelValidationError(
                "manifest lineage cannot contain duplicates"
            )
        object.__setattr__(
            self,
            "metadata",
            freeze_json_object(self.metadata),
        )


class ChangeKind(str, Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ArtifactChange:
    """One explicit artifact transition within a source snapshot."""

    kind: ChangeKind
    ref: ArtifactRef
    manifest: Optional[ArtifactManifest] = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ChangeKind):
            raise KernelValidationError(
                "artifact change kind must be a ChangeKind"
            )
        if not isinstance(self.ref, ArtifactRef):
            raise KernelValidationError(
                "artifact change ref must be an ArtifactRef"
            )
        if self.kind is ChangeKind.DELETE:
            if self.manifest is not None:
                raise KernelValidationError(
                    "delete changes cannot carry a manifest"
                )
        else:
            if not isinstance(self.manifest, ArtifactManifest):
                raise KernelValidationError(
                    "{} changes require an ArtifactManifest".format(
                        self.kind.value
                    )
                )
            if self.manifest.ref != self.ref:
                raise KernelValidationError(
                    "artifact change ref must match manifest ref"
                )


@dataclass(frozen=True)
class ChangeSet:
    """Optimistic, idempotent changes to one logical source."""

    source: SourceRef
    base_version: str
    changes: Tuple[ArtifactChange, ...]
    idempotency_key: str
    change_set_id: str = field(default_factory=lambda: new_id("changeset"))
    protocol_version: str = "1"
    created_at: datetime = field(default_factory=utc_now)
    metadata: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceRef):
            raise KernelValidationError(
                "change set source must be a SourceRef"
            )
        for name in (
            "base_version",
            "idempotency_key",
            "change_set_id",
            "protocol_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "change set {} must be a non-empty string".format(name)
                )
        if self.protocol_version != "1":
            raise KernelValidationError(
                "unsupported change set protocol version {!r}".format(
                    self.protocol_version
                )
            )
        object.__setattr__(self, "changes", tuple(self.changes))
        if not self.changes:
            raise KernelValidationError(
                "change set requires at least one change"
            )
        if any(not isinstance(item, ArtifactChange) for item in self.changes):
            raise KernelValidationError(
                "change set changes must contain ArtifactChange values"
            )
        artifact_ids = tuple(item.ref.artifact_id for item in self.changes)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise KernelValidationError(
                "change set cannot change an artifact more than once"
            )
        for change in self.changes:
            if (
                change.manifest is not None
                and change.manifest.source != self.source
            ):
                raise KernelValidationError(
                    "change manifest belongs to a different source"
                )
        if not isinstance(self.created_at, datetime):
            raise KernelValidationError(
                "change set created_at must be a datetime"
            )
        require_aware(self.created_at, "change set created_at")
        object.__setattr__(
            self,
            "metadata",
            freeze_json_object(self.metadata),
        )


@dataclass(frozen=True)
class SnapshotSet:
    """Per-source immutable bindings used by one plan execution."""

    snapshots: Tuple[SnapshotRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        if any(not isinstance(item, SnapshotRef) for item in self.snapshots):
            raise KernelValidationError(
                "snapshot set must contain SnapshotRef values"
            )
        source_ids = tuple(
            item.source.source_id for item in self.snapshots
        )
        if len(set(source_ids)) != len(source_ids):
            raise KernelValidationError(
                "snapshot set cannot bind a source more than once"
            )

    def get(self, source: SourceRef) -> Optional[SnapshotRef]:
        if not isinstance(source, SourceRef):
            raise KernelValidationError(
                "snapshot lookup requires a SourceRef"
            )
        for snapshot in self.snapshots:
            if snapshot.source == source:
                return snapshot
            if snapshot.source.source_id == source.source_id:
                raise SnapshotUnavailableError(
                    "snapshot source kind does not match {!r}".format(
                        source.source_id
                    )
                )
        return None

    def require(self, source: SourceRef) -> SnapshotRef:
        snapshot = self.get(source)
        if snapshot is None:
            raise SnapshotUnavailableError(
                "source {!r} has no pinned snapshot".format(source.source_id)
            )
        return snapshot

    def with_snapshot(self, snapshot: SnapshotRef) -> "SnapshotSet":
        if not isinstance(snapshot, SnapshotRef):
            raise KernelValidationError(
                "snapshot binding must be a SnapshotRef"
            )
        retained = tuple(
            item
            for item in self.snapshots
            if item.source.source_id != snapshot.source.source_id
        )
        return SnapshotSet(retained + (snapshot,))


@dataclass(frozen=True)
class SyncReceipt:
    """Auditable result of applying or reusing one idempotent change set."""

    change_set_id: str
    idempotency_key: str
    snapshot: SnapshotRef
    previous_snapshot: SnapshotRef
    reused: bool = False
    synced_at: datetime = field(default_factory=utc_now, compare=False)

    def __post_init__(self) -> None:
        for name in ("change_set_id", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "sync receipt {} must be a non-empty string".format(name)
                )
        if not isinstance(self.snapshot, SnapshotRef):
            raise KernelValidationError(
                "sync receipt snapshot must be a SnapshotRef"
            )
        if not isinstance(self.previous_snapshot, SnapshotRef):
            raise KernelValidationError(
                "sync receipt previous_snapshot must be a SnapshotRef"
            )
        if self.snapshot.source != self.previous_snapshot.source:
            raise KernelValidationError(
                "sync receipt snapshots must belong to the same source"
            )
        if not isinstance(self.reused, bool):
            raise KernelValidationError("sync receipt reused must be boolean")
        if not isinstance(self.synced_at, datetime):
            raise KernelValidationError(
                "sync receipt synced_at must be a datetime"
            )
        require_aware(self.synced_at, "sync receipt synced_at")


__all__ = [
    "ArtifactChange",
    "ArtifactManifest",
    "ArtifactRef",
    "ChangeKind",
    "ChangeSet",
    "SnapshotSet",
    "SyncReceipt",
    "sha256_checksum",
]
