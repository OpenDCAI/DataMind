"""Deterministic in-memory document source used as a reference adapter."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, Mapping, Tuple
from urllib.parse import quote

from datamind.dataops import (
    BindingRow,
    BindingSet,
    Evidence,
    ResultKind,
    Search,
)
from datamind.kernel import (
    ArtifactRef,
    ChangeKind,
    ChangeSet,
    ExecutionContext,
    JsonObject,
    KernelValidationError,
    Provenance,
    SnapshotRef,
    SnapshotUnavailableError,
    SourceDescriptor,
    SourceExecutionError,
    SourceKind,
    SourceRef,
    SyncError,
    freeze_json_object,
    sha256_checksum,
    thaw_json,
)
from datamind.ports import SourceResult

_TOKEN_PATTERN = re.compile(r"\w+", flags=re.UNICODE)
DOCUMENT_ARTIFACT_MEDIA_TYPE = "application/vnd.datamind.document+json"


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    content: str
    metadata: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, str) or not self.document_id.strip():
            raise KernelValidationError(
                "document_id must be a non-empty string"
            )
        if not isinstance(self.content, str) or not self.content.strip():
            raise KernelValidationError(
                "document content must be a non-empty string"
            )
        object.__setattr__(
            self,
            "metadata",
            freeze_json_object(self.metadata),
        )


@dataclass(frozen=True)
class DocumentHit:
    document_id: str
    content: str
    score: float
    metadata: JsonObject


@dataclass(frozen=True)
class _DocumentVersion:
    snapshot: SnapshotRef
    documents: Tuple[DocumentRecord, ...]


class InMemoryDocumentSource:
    """Versioned lexical baseline for the document Source and Lifecycle Ports."""

    def __init__(
        self,
        *,
        source_id: str,
        documents: Iterable[DocumentRecord],
        version: str = "1",
        display_name: str = "In-memory documents",
    ) -> None:
        initial_documents = tuple(documents)
        if len({item.document_id for item in initial_documents}) != len(
            initial_documents
        ):
            raise KernelValidationError(
                "document ids must be unique within a source"
            )
        ref = SourceRef(source_id, SourceKind.DOCUMENT)
        checksum = self._state_checksum(initial_documents)
        initial_snapshot = SnapshotRef(
            source=ref,
            version=version,
            checksum=checksum,
        )
        self._versions: Dict[str, _DocumentVersion] = {
            version: _DocumentVersion(
                snapshot=initial_snapshot,
                documents=self._ordered(initial_documents),
            )
        }
        self._current_version = version
        self._descriptor = SourceDescriptor(
            ref=ref,
            display_name=display_name,
            capabilities=frozenset(("search",)),
            version=version,
            schema={
                "record": {
                    "document_id": "string",
                    "content": "string",
                    "metadata": "object",
                }
            },
            metadata={"adapter": "in_memory_document"},
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        return self._descriptor

    async def current_snapshot(self) -> SnapshotRef:
        return self._versions[self._current_version].snapshot

    async def has_snapshot(self, snapshot: SnapshotRef) -> bool:
        if not isinstance(snapshot, SnapshotRef):
            return False
        recorded = self._versions.get(snapshot.version)
        return (
            recorded is not None
            and recorded.snapshot.same_version_as(snapshot)
        )

    async def apply_changes(
        self,
        change_set: ChangeSet,
        *,
        artifacts: Mapping[ArtifactRef, bytes],
    ) -> SnapshotRef:
        if change_set.source != self.descriptor.ref:
            raise SyncError("change set belongs to a different source")
        current = self._versions[self._current_version]
        if change_set.base_version != current.snapshot.version:
            raise SyncError(
                "document source changed after lifecycle preflight"
            )
        documents = {
            item.document_id: item for item in current.documents
        }
        for change in change_set.changes:
            artifact_id = change.ref.artifact_id
            exists = artifact_id in documents
            if change.kind is ChangeKind.ADD and exists:
                raise SyncError(
                    "cannot add existing document {!r}".format(artifact_id)
                )
            if change.kind in (ChangeKind.UPDATE, ChangeKind.DELETE) and not exists:
                raise SyncError(
                    "cannot {} missing document {!r}".format(
                        change.kind.value,
                        artifact_id,
                    )
                )
            if change.kind is ChangeKind.DELETE:
                del documents[artifact_id]
                continue
            if change.manifest is None:  # guarded by ArtifactChange
                raise SyncError("document change has no manifest")
            if change.manifest.media_type != DOCUMENT_ARTIFACT_MEDIA_TYPE:
                raise SyncError(
                    "document source cannot decode media type {!r}".format(
                        change.manifest.media_type
                    )
                )
            content = artifacts.get(change.ref)
            if content is None:
                raise SyncError(
                    "document artifact {!r} was not resolved".format(
                        artifact_id
                    )
                )
            record = self._decode_artifact(content)
            if record.document_id != artifact_id:
                raise SyncError(
                    "document artifact id does not match its payload"
                )
            documents[artifact_id] = record

        ordered = self._ordered(documents.values())
        checksum = self._state_checksum(ordered)
        version = "sha256:{}".format(checksum)
        existing = self._versions.get(version)
        if existing is not None:
            snapshot = existing.snapshot
        else:
            snapshot = SnapshotRef(
                source=self.descriptor.ref,
                version=version,
                checksum=checksum,
            )
            self._versions[version] = _DocumentVersion(
                snapshot=snapshot,
                documents=ordered,
            )
        self._current_version = version
        self._descriptor = replace(self._descriptor, version=version)
        return snapshot

    async def execute(
        self,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        if not isinstance(operation, Search):
            raise SourceExecutionError(
                "document source only supports Search"
            )
        pinned = context.snapshots.get(self.descriptor.ref)
        if pinned is None:
            selected = self._versions[self._current_version]
        else:
            selected = self._versions.get(pinned.version)
            if (
                selected is None
                or not selected.snapshot.same_version_as(pinned)
            ):
                raise SnapshotUnavailableError(
                    "document source {!r} cannot serve snapshot {!r}".format(
                        self.descriptor.ref.source_id,
                        pinned.version,
                    )
                )

        matches = []
        for record in selected.documents:
            if not self._matches_filters(record, operation.filters):
                continue
            score = self._score(operation.query, record.content)
            if score > 0:
                matches.append((score, record))
        matches.sort(key=lambda item: (-item[0], item[1].document_id))
        matches = matches[: operation.limit]

        snapshot = selected.snapshot
        hits = []
        evidence = []
        binding_values = []
        provenance = []
        for score, record in matches:
            locator = "document://{}/{}".format(
                quote(self.descriptor.ref.source_id, safe=""),
                quote(record.document_id, safe=""),
            )
            origin = Provenance(
                source=self.descriptor.ref,
                locator=locator,
                snapshot=snapshot,
            )
            hit = DocumentHit(
                document_id=record.document_id,
                content=record.content,
                score=score,
                metadata=record.metadata,
            )
            hits.append(hit)
            evidence_item = Evidence(
                kind=SourceKind.DOCUMENT,
                content=record.content,
                provenance=origin,
                score=score,
                metadata={
                    "document_id": record.document_id,
                    **dict(record.metadata),
                },
            )
            evidence.append(evidence_item)
            values = {
                "document_id": record.document_id,
                "score": score,
            }
            values.update(
                {
                    "metadata.{}".format(key): value
                    for key, value in record.metadata.items()
                }
            )
            binding_values.append((values, evidence_item.evidence_id))
            provenance.append(origin)

        metadata_fields = sorted(
            {
                "metadata.{}".format(key)
                for record in selected.documents
                for key in record.metadata
            }
        )
        binding_fields = (
            "document_id",
            "score",
        ) + tuple(metadata_fields)
        bindings = BindingSet(
            fields=binding_fields,
            rows=tuple(
                BindingRow(
                    values={
                        field: values.get(field)
                        for field in binding_fields
                    },
                    evidence_ids=(evidence_id,),
                )
                for values, evidence_id in binding_values
            ),
        )
        return SourceResult(
            value=tuple(hits),
            result_kind=ResultKind.DOCUMENT_HITS,
            evidence=tuple(evidence),
            bindings=bindings,
            provenance=tuple(provenance),
            snapshots=(snapshot,),
        )

    @staticmethod
    def _matches_filters(
        record: DocumentRecord,
        filters: JsonObject,
    ) -> bool:
        return all(
            record.metadata.get(key) == expected
            for key, expected in filters.items()
        )

    @staticmethod
    def _score(query: str, content: str) -> float:
        normalized_query = query.casefold()
        normalized_content = content.casefold()
        query_terms = set(_TOKEN_PATTERN.findall(normalized_query))
        content_terms = set(_TOKEN_PATTERN.findall(normalized_content))
        if query_terms:
            lexical = len(query_terms & content_terms) / len(query_terms)
        else:
            lexical = 0.0
        phrase = 1.0 if normalized_query in normalized_content else 0.0
        return round(max(lexical, phrase), 8)

    @staticmethod
    def _ordered(
        documents: Iterable[DocumentRecord],
    ) -> Tuple[DocumentRecord, ...]:
        return tuple(sorted(documents, key=lambda item: item.document_id))

    @staticmethod
    def _state_checksum(documents: Iterable[DocumentRecord]) -> str:
        payload = [
            {
                "document_id": item.document_id,
                "content": item.content,
                "metadata": thaw_json(item.metadata),
            }
            for item in InMemoryDocumentSource._ordered(documents)
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256_checksum(encoded)

    @staticmethod
    def _decode_artifact(content: bytes) -> DocumentRecord:
        try:
            payload = json.loads(content.decode("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("document artifact must be a JSON object")
            return DocumentRecord(
                document_id=str(payload["document_id"]),
                content=str(payload["content"]),
                metadata=payload.get("metadata", {}),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise SyncError(
                "invalid document artifact: {}".format(exc)
            ) from exc
