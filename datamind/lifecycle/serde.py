"""Explicit JSON contracts for ArtifactManifest and ChangeSet."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from datamind.kernel import (
    ArtifactChange,
    ArtifactManifest,
    ArtifactRef,
    ChangeKind,
    ChangeSet,
    SerializationError,
    SourceKind,
    SourceRef,
    thaw_json,
)

ARTIFACT_MANIFEST_SCHEMA = "datamind.artifact_manifest"
ARTIFACT_MANIFEST_VERSION = "1"
CHANGE_SET_SCHEMA = "datamind.change_set"
CHANGE_SET_VERSION = "1"


def _artifact_ref_to_dict(ref: ArtifactRef) -> dict:
    return {
        "artifact_id": ref.artifact_id,
        "version": ref.version,
    }


def _artifact_ref_from_dict(payload: Mapping[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=str(payload["artifact_id"]),
        version=str(payload["version"]),
    )


def _source_to_dict(source: SourceRef) -> dict:
    return {
        "source_id": source.source_id,
        "kind": source.kind.value,
    }


def _source_from_dict(payload: Mapping[str, Any]) -> SourceRef:
    return SourceRef(
        source_id=str(payload["source_id"]),
        kind=SourceKind(str(payload["kind"])),
    )


def manifest_to_dict(manifest: ArtifactManifest) -> dict:
    return {
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "version": ARTIFACT_MANIFEST_VERSION,
        "artifact": _artifact_ref_to_dict(manifest.ref),
        "source": _source_to_dict(manifest.source),
        "checksum": manifest.checksum,
        "locator": manifest.locator,
        "media_type": manifest.media_type,
        "created_at": manifest.created_at.isoformat(),
        "data_schema": thaw_json(manifest.data_schema),
        "lineage": [
            _artifact_ref_to_dict(item) for item in manifest.lineage
        ],
        "metadata": thaw_json(manifest.metadata),
    }


def manifest_from_dict(payload: Mapping[str, Any]) -> ArtifactManifest:
    try:
        if payload.get("schema") != ARTIFACT_MANIFEST_SCHEMA:
            raise SerializationError("unsupported artifact manifest schema")
        version = str(payload["version"])
        if version != ARTIFACT_MANIFEST_VERSION:
            raise SerializationError(
                "unsupported artifact manifest version {!r}".format(version)
            )
        return ArtifactManifest(
            ref=_artifact_ref_from_dict(payload["artifact"]),
            source=_source_from_dict(payload["source"]),
            checksum=str(payload["checksum"]),
            locator=str(payload["locator"]),
            media_type=str(
                payload.get("media_type", "application/octet-stream")
            ),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            data_schema=payload.get("data_schema", {}),
            lineage=tuple(
                _artifact_ref_from_dict(item)
                for item in payload.get("lineage", ())
            ),
            metadata=payload.get("metadata", {}),
        )
    except SerializationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError(
            "invalid artifact manifest payload: {}".format(exc)
        ) from exc


def change_set_to_dict(change_set: ChangeSet) -> dict:
    changes = []
    for change in change_set.changes:
        changes.append(
            {
                "kind": change.kind.value,
                "artifact": _artifact_ref_to_dict(change.ref),
                "manifest": (
                    manifest_to_dict(change.manifest)
                    if change.manifest is not None
                    else None
                ),
            }
        )
    return {
        "schema": CHANGE_SET_SCHEMA,
        "version": change_set.protocol_version,
        "change_set_id": change_set.change_set_id,
        "source": _source_to_dict(change_set.source),
        "base_version": change_set.base_version,
        "idempotency_key": change_set.idempotency_key,
        "created_at": change_set.created_at.isoformat(),
        "changes": changes,
        "metadata": thaw_json(change_set.metadata),
    }


def change_set_from_dict(payload: Mapping[str, Any]) -> ChangeSet:
    try:
        if payload.get("schema") != CHANGE_SET_SCHEMA:
            raise SerializationError("unsupported change set schema")
        version = str(payload["version"])
        if version != CHANGE_SET_VERSION:
            raise SerializationError(
                "unsupported change set version {!r}".format(version)
            )
        changes = []
        for item in payload["changes"]:
            manifest_payload = item.get("manifest")
            changes.append(
                ArtifactChange(
                    kind=ChangeKind(str(item["kind"])),
                    ref=_artifact_ref_from_dict(item["artifact"]),
                    manifest=(
                        manifest_from_dict(manifest_payload)
                        if manifest_payload is not None
                        else None
                    ),
                )
            )
        return ChangeSet(
            source=_source_from_dict(payload["source"]),
            base_version=str(payload["base_version"]),
            changes=tuple(changes),
            idempotency_key=str(payload["idempotency_key"]),
            change_set_id=str(payload["change_set_id"]),
            protocol_version=version,
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            metadata=payload.get("metadata", {}),
        )
    except SerializationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError(
            "invalid change set payload: {}".format(exc)
        ) from exc


def manifest_to_json(
    manifest: ArtifactManifest,
    *,
    indent: Any = None,
) -> str:
    return json.dumps(
        manifest_to_dict(manifest),
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
    )


def manifest_from_json(raw: str) -> ArtifactManifest:
    return manifest_from_dict(_json_object(raw, "artifact manifest"))


def change_set_to_json(
    change_set: ChangeSet,
    *,
    indent: Any = None,
) -> str:
    return json.dumps(
        change_set_to_dict(change_set),
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
    )


def change_set_from_json(raw: str) -> ChangeSet:
    return change_set_from_dict(_json_object(raw, "change set"))


def _json_object(raw: str, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SerializationError(
            "invalid {} JSON: {}".format(label, exc)
        ) from exc
    if not isinstance(payload, Mapping):
        raise SerializationError("{} JSON must be an object".format(label))
    return payload


__all__ = [
    "ARTIFACT_MANIFEST_SCHEMA",
    "ARTIFACT_MANIFEST_VERSION",
    "CHANGE_SET_SCHEMA",
    "CHANGE_SET_VERSION",
    "change_set_from_dict",
    "change_set_from_json",
    "change_set_to_dict",
    "change_set_to_json",
    "manifest_from_dict",
    "manifest_from_json",
    "manifest_to_dict",
    "manifest_to_json",
]
