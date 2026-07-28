"""Source-independent coordination for versioned artifact synchronization."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Dict, Tuple

from datamind.kernel import (
    ArtifactIntegrityError,
    ArtifactRef,
    ChangeKind,
    ChangeSet,
    IdempotencyConflictError,
    KernelValidationError,
    SyncReceipt,
    UnsupportedSyncError,
    VersionConflictError,
    sha256_checksum,
)
from datamind.ports import (
    ArtifactStore,
    SourceCatalogPort,
    SyncTarget,
)


class LifecycleManager:
    """Validate and atomically route ChangeSets to versioned sources."""

    def __init__(
        self,
        catalog: SourceCatalogPort,
        artifact_store: ArtifactStore,
    ) -> None:
        self._catalog = catalog
        self._artifact_store = artifact_store
        self._ledger: Dict[str, Tuple[ChangeSet, SyncReceipt]] = {}
        self._lock = asyncio.Lock()

    async def sync(self, change_set: ChangeSet) -> SyncReceipt:
        if not isinstance(change_set, ChangeSet):
            raise KernelValidationError("sync expects a ChangeSet")
        async with self._lock:
            recorded = self._ledger.get(change_set.idempotency_key)
            if recorded is not None:
                prior_change_set, prior_receipt = recorded
                if prior_change_set != change_set:
                    raise IdempotencyConflictError(
                        "idempotency key {!r} belongs to a different "
                        "change set".format(change_set.idempotency_key)
                    )
                return replace(prior_receipt, reused=True)

            source = self._catalog.get(change_set.source)
            if not isinstance(source, SyncTarget):
                raise UnsupportedSyncError(
                    "source {!r} does not support synchronization".format(
                        change_set.source.source_id
                    )
                )
            previous = await source.current_snapshot()
            if previous.version != change_set.base_version:
                raise VersionConflictError(
                    "source {!r} is at version {!r}, not base version "
                    "{!r}".format(
                        change_set.source.source_id,
                        previous.version,
                        change_set.base_version,
                    )
                )

            artifacts = {}
            for change in change_set.changes:
                if change.kind is ChangeKind.DELETE:
                    continue
                if change.manifest is None:  # guarded by the domain type
                    raise ArtifactIntegrityError(
                        "non-delete change has no manifest"
                    )
                content = await self._artifact_store.load(change.ref)
                checksum = sha256_checksum(content)
                if checksum != change.manifest.checksum:
                    raise ArtifactIntegrityError(
                        "artifact {!r} checksum does not match its "
                        "manifest".format(change.ref.artifact_id)
                    )
                artifacts[change.ref] = content

            snapshot = await source.apply_changes(
                change_set,
                artifacts=artifacts,
            )
            if snapshot.source != change_set.source:
                raise ArtifactIntegrityError(
                    "sync target returned a snapshot for a different source"
                )
            if not await source.has_snapshot(snapshot):
                raise ArtifactIntegrityError(
                    "sync target did not retain its returned snapshot"
                )
            receipt = SyncReceipt(
                change_set_id=change_set.change_set_id,
                idempotency_key=change_set.idempotency_key,
                previous_snapshot=previous,
                snapshot=snapshot,
            )
            self._ledger[change_set.idempotency_key] = (
                change_set,
                receipt,
            )
            return receipt


__all__ = ["LifecycleManager"]
