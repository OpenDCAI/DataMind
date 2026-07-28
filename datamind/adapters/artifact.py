"""In-memory artifact bytes for deterministic lifecycle tests and examples."""
from __future__ import annotations

from threading import RLock
from typing import Dict

from datamind.kernel import (
    ArtifactIntegrityError,
    ArtifactManifest,
    ArtifactNotFoundError,
    ArtifactRef,
    KernelValidationError,
    sha256_checksum,
)


class InMemoryArtifactStore:
    """Reference ArtifactStore; production stores remain external adapters."""

    def __init__(self) -> None:
        self._artifacts: Dict[ArtifactRef, bytes] = {}
        self._lock = RLock()

    def put(self, manifest: ArtifactManifest, content: bytes) -> None:
        if not isinstance(manifest, ArtifactManifest):
            raise KernelValidationError(
                "artifact put expects an ArtifactManifest"
            )
        if not isinstance(content, bytes):
            raise KernelValidationError("artifact content must be bytes")
        if sha256_checksum(content) != manifest.checksum:
            raise ArtifactIntegrityError(
                "artifact {!r} checksum does not match its manifest".format(
                    manifest.ref.artifact_id
                )
            )
        with self._lock:
            existing = self._artifacts.get(manifest.ref)
            if existing is not None and existing != content:
                raise ArtifactIntegrityError(
                    "immutable artifact {!r} already has different "
                    "content".format(manifest.ref.artifact_id)
                )
            self._artifacts[manifest.ref] = bytes(content)

    async def load(self, ref: ArtifactRef) -> bytes:
        if not isinstance(ref, ArtifactRef):
            raise KernelValidationError(
                "artifact load expects an ArtifactRef"
            )
        with self._lock:
            content = self._artifacts.get(ref)
            if content is None:
                raise ArtifactNotFoundError(
                    "artifact {!r} version {!r} is unavailable".format(
                        ref.artifact_id,
                        ref.version,
                    )
                )
            return bytes(content)


__all__ = ["InMemoryArtifactStore"]
