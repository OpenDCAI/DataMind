"""Deterministic in-memory reference adapter for typed stateful Recall."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Iterable, Optional, Tuple
from urllib.parse import quote

from datamind.dataops import (
    Evidence,
    MemoryRecallResult,
    Recall,
    ResultKind,
)
from datamind.kernel import (
    ExecutionContext,
    KernelValidationError,
    MemoryConflict,
    MemoryKind,
    MemoryLinkKind,
    MemoryRecord,
    Provenance,
    SnapshotRef,
    SnapshotUnavailableError,
    SourceDescriptor,
    SourceExecutionError,
    SourceKind,
    SourceRef,
    require_aware,
    sha256_checksum,
    thaw_json,
    utc_now,
)
from datamind.ports import SourceResult

_TOKEN_PATTERN = re.compile(r"\w+", flags=re.UNICODE)


class InMemoryMemorySource:
    """Immutable, bi-temporal Recall baseline without ranking dependencies."""

    def __init__(
        self,
        *,
        source_id: str,
        records: Iterable[MemoryRecord],
        version: str = "1",
        display_name: str = "In-memory typed memory",
        observed_at: Optional[datetime] = None,
    ) -> None:
        supplied = tuple(records)
        if any(not isinstance(item, MemoryRecord) for item in supplied):
            raise KernelValidationError(
                "memory source records must contain MemoryRecord values"
            )
        ordered = tuple(
            sorted(supplied, key=lambda item: item.memory_id)
        )
        ref = SourceRef(source_id, SourceKind.MEMORY)
        snapshot_time = observed_at or utc_now()
        if not isinstance(snapshot_time, datetime):
            raise KernelValidationError(
                "memory snapshot observed_at must be a datetime"
            )
        require_aware(snapshot_time, "memory snapshot observed_at")
        self._validate_history(ordered, snapshot_time=snapshot_time)
        checksum = self._state_checksum(ordered)
        self._records = ordered
        self._snapshot = SnapshotRef(
            source=ref,
            version=version,
            checksum=checksum,
            observed_at=snapshot_time,
        )
        self._descriptor = SourceDescriptor(
            ref=ref,
            display_name=display_name,
            capabilities=frozenset(("recall",)),
            version=version,
            schema={
                "record": {
                    "memory_id": "string",
                    "kind": [item.value for item in MemoryKind],
                    "scope": "ScopeRef",
                    "content": "string",
                    "valid_time": "[from, to)",
                    "recorded_time": "[from, to)",
                    "evidence": "EvidenceRef[]",
                    "links": "MemoryLink[]",
                }
            },
            metadata={
                "adapter": "in_memory_memory",
                "time_model": "bitemporal",
                "implicit_scope_inheritance": False,
            },
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        return self._descriptor

    async def current_snapshot(self) -> SnapshotRef:
        return self._snapshot

    async def has_snapshot(self, snapshot: SnapshotRef) -> bool:
        return (
            isinstance(snapshot, SnapshotRef)
            and self._snapshot.same_version_as(snapshot)
        )

    async def execute(
        self,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        if not isinstance(operation, Recall):
            raise SourceExecutionError(
                "memory source only supports Recall"
            )
        context.require_readable_scopes(operation.scopes)
        pinned = context.snapshots.get(self.descriptor.ref)
        if pinned is not None and not self._snapshot.same_version_as(pinned):
            raise SnapshotUnavailableError(
                "memory source {!r} cannot serve snapshot {!r}".format(
                    self.descriptor.ref.source_id,
                    pinned.version,
                )
            )
        snapshot = self._snapshot
        known_at = operation.known_at or snapshot.observed_at
        valid_at = operation.valid_at or snapshot.observed_at
        if known_at > snapshot.observed_at:
            raise SnapshotUnavailableError(
                "known_at cannot exceed the selected memory snapshot"
            )

        requested_scopes = frozenset(operation.scopes)
        requested_kinds = frozenset(operation.kinds)
        matches = []
        for record in self._records:
            if record.scope not in requested_scopes:
                continue
            if requested_kinds and record.kind not in requested_kinds:
                continue
            if not record.is_visible_at(
                valid_at=valid_at,
                known_at=known_at,
            ):
                continue
            score = self._score(operation.query, record.content)
            if score > 0:
                matches.append((score, record))
        matches.sort(key=lambda item: (-item[0], item[1].memory_id))
        matches = matches[: operation.limit]

        returned_records = tuple(item[1] for item in matches)
        returned_ids = {item.memory_id for item in returned_records}
        conflicts = self._conflicts(returned_records, returned_ids)
        evidence = []
        provenance = []
        for score, record in matches:
            origin = Provenance(
                source=self.descriptor.ref,
                locator="memory://{}/{}".format(
                    quote(self.descriptor.ref.source_id, safe=""),
                    quote(record.memory_id, safe=""),
                ),
                observed_at=record.recorded_from,
                snapshot=snapshot,
                valid_from=record.valid_from,
                valid_to=record.valid_to,
                derived_from=tuple(
                    item.evidence_id for item in record.evidence
                ),
            )
            evidence.append(
                Evidence(
                    kind=SourceKind.MEMORY,
                    content=record.content,
                    provenance=origin,
                    score=score,
                    metadata={
                        "memory_id": record.memory_id,
                        "memory_kind": record.kind.value,
                        "scope_kind": record.scope.kind.value,
                    },
                )
            )
            provenance.append(origin)

        return SourceResult(
            value=MemoryRecallResult(
                records=returned_records,
                conflicts=conflicts,
            ),
            result_kind=ResultKind.MEMORY_RECORDS,
            evidence=tuple(evidence),
            provenance=tuple(provenance),
            snapshots=(snapshot,),
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
    def _conflicts(
        records: Tuple[MemoryRecord, ...],
        returned_ids: set,
    ) -> Tuple[MemoryConflict, ...]:
        pairs = set()
        for record in records:
            for link in record.links:
                if (
                    link.kind is MemoryLinkKind.CONTRADICTS
                    and link.target_id in returned_ids
                ):
                    pairs.add(
                        tuple(sorted((record.memory_id, link.target_id)))
                    )
        return tuple(
            MemoryConflict(record_ids=pair)
            for pair in sorted(pairs)
        )

    @staticmethod
    def _validate_history(
        records: Tuple[MemoryRecord, ...],
        *,
        snapshot_time: datetime,
    ) -> None:
        by_id = {item.memory_id: item for item in records}
        if len(by_id) != len(records):
            raise KernelValidationError(
                "memory ids must be unique within a source"
            )
        for record in records:
            if record.recorded_from > snapshot_time:
                raise KernelValidationError(
                    "memory cannot be recorded after its source snapshot"
                )
            if (
                record.recorded_to is not None
                and record.recorded_to > snapshot_time
            ):
                raise KernelValidationError(
                    "memory transaction history cannot exceed its snapshot"
                )
            for link in record.links:
                target = by_id.get(link.target_id)
                if target is None:
                    raise KernelValidationError(
                        "memory link target {!r} is missing".format(
                            link.target_id
                        )
                    )
                if target.scope != record.scope:
                    raise KernelValidationError(
                        "reference adapter forbids cross-scope memory links"
                    )
                if link.kind is MemoryLinkKind.SUPERSEDES:
                    if target.kind is not record.kind:
                        raise KernelValidationError(
                            "superseding memory must preserve its kind"
                        )
                    if target.recorded_to != record.recorded_from:
                        raise KernelValidationError(
                            "superseded transaction interval must close "
                            "when its replacement is recorded"
                        )

    @staticmethod
    def _state_checksum(records: Tuple[MemoryRecord, ...]) -> str:
        payload = []
        for record in records:
            payload.append(
                {
                    "memory_id": record.memory_id,
                    "kind": record.kind.value,
                    "scope": {
                        "kind": record.scope.kind.value,
                        "scope_id": record.scope.scope_id,
                    },
                    "content": record.content,
                    "recorded_from": record.recorded_from.isoformat(),
                    "recorded_to": (
                        record.recorded_to.isoformat()
                        if record.recorded_to is not None
                        else None
                    ),
                    "valid_from": (
                        record.valid_from.isoformat()
                        if record.valid_from is not None
                        else None
                    ),
                    "valid_to": (
                        record.valid_to.isoformat()
                        if record.valid_to is not None
                        else None
                    ),
                    "evidence": [
                        {
                            "evidence_id": item.evidence_id,
                            "source_id": (
                                item.provenance.source.source_id
                            ),
                            "source_kind": (
                                item.provenance.source.kind.value
                            ),
                            "locator": item.provenance.locator,
                            "observed_at": (
                                item.provenance.observed_at.isoformat()
                            ),
                        }
                        for item in record.evidence
                    ],
                    "links": [
                        {
                            "kind": item.kind.value,
                            "target_id": item.target_id,
                        }
                        for item in record.links
                    ],
                    "metadata": thaw_json(record.metadata),
                }
            )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256_checksum(encoded)


__all__ = ["InMemoryMemorySource"]
