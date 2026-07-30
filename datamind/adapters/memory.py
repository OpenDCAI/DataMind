"""Deterministic reference adapter for governed, bi-temporal Memory."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import quote

from datamind.dataops import (
    ApplyMutation,
    BindingRow,
    BindingSet,
    Evidence,
    MemoryRecallResult,
    ProposeMutation,
    Recall,
    ResultKind,
)
from datamind.kernel import (
    AssertMemory,
    EffectLevel,
    ExecutionContext,
    KernelValidationError,
    MemoryConflict,
    MemoryIdempotencyConflictError,
    MemoryKind,
    MemoryLink,
    MemoryLinkKind,
    MemoryMutationDraft,
    MemoryMutationError,
    MemoryMutationProposal,
    MemoryMutationReceipt,
    MemoryOriginChannel,
    MemoryRecord,
    MemoryVersionConflictError,
    Provenance,
    RetractMemory,
    SnapshotRef,
    SnapshotUnavailableError,
    SourceDescriptor,
    SourceExecutionError,
    SourceKind,
    SourceRef,
    SupersedeMemory,
    memory_write_requires_approval,
    require_aware,
    sha256_checksum,
    thaw_json,
    utc_now,
)
from datamind.ports import SourceResult

_TOKEN_PATTERN = re.compile(r"\w+", flags=re.UNICODE)


@dataclass(frozen=True)
class _MemoryVersion:
    snapshot: SnapshotRef
    records: Tuple[MemoryRecord, ...]


@dataclass(frozen=True)
class _ProposalEntry:
    draft: MemoryMutationDraft
    channel: MemoryOriginChannel
    proposal: MemoryMutationProposal


class InMemoryMemorySource:
    """Versioned reference baseline for Recall and governed state changes."""

    def __init__(
        self,
        *,
        source_id: str,
        records: Iterable[MemoryRecord],
        version: str = "1",
        display_name: str = "In-memory typed memory",
        observed_at: Optional[datetime] = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        supplied = tuple(records)
        if any(not isinstance(item, MemoryRecord) for item in supplied):
            raise KernelValidationError(
                "memory source records must contain MemoryRecord values"
            )
        if not callable(clock):
            raise KernelValidationError("memory source clock must be callable")
        ordered = self._ordered(supplied)
        ref = SourceRef(source_id, SourceKind.MEMORY)
        snapshot_time = observed_at or clock()
        if not isinstance(snapshot_time, datetime):
            raise KernelValidationError(
                "memory snapshot observed_at must be a datetime"
            )
        require_aware(snapshot_time, "memory snapshot observed_at")
        self._validate_history(ordered, snapshot_time=snapshot_time)
        checksum = self._state_checksum(ordered)
        snapshot = SnapshotRef(
            source=ref,
            version=version,
            checksum=checksum,
            observed_at=snapshot_time,
        )
        self._versions: Dict[str, _MemoryVersion] = {
            version: _MemoryVersion(snapshot=snapshot, records=ordered)
        }
        self._current_version = version
        self._clock = clock
        self._proposals: Dict[str, _ProposalEntry] = {}
        self._receipts: Dict[
            str,
            Tuple[MemoryMutationProposal, MemoryMutationReceipt],
        ] = {}
        self._lock = asyncio.Lock()
        self._descriptor = SourceDescriptor(
            ref=ref,
            display_name=display_name,
            capabilities=frozenset(
                ("recall", "propose_mutation", "apply_mutation")
            ),
            max_effect=EffectLevel.INTERNAL_WRITE,
            version=version,
            schema={
                "record": {
                    "memory_id": "string",
                    "kind": [item.value for item in MemoryKind],
                    "scope": "ScopeRef",
                    "content": "string",
                    "valid_time": "[from, to)",
                    "recorded_time": "[from, to)",
                    "origin": "MemoryOrigin",
                    "evidence": "EvidenceRef[]",
                    "links": "MemoryLink[]",
                },
                "mutation": {
                    "actions": ["assert", "supersede", "retract"],
                    "atomicity": "one source and one explicit scope",
                },
            },
            metadata={
                "adapter": "in_memory_memory",
                "time_model": "bitemporal",
                "implicit_scope_inheritance": False,
                "cross_scope_mutation": False,
            },
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        return self._descriptor

    async def current_snapshot(self) -> SnapshotRef:
        async with self._lock:
            return self._versions[self._current_version].snapshot

    async def has_snapshot(self, snapshot: SnapshotRef) -> bool:
        if not isinstance(snapshot, SnapshotRef):
            return False
        async with self._lock:
            recorded = self._versions.get(snapshot.version)
            return (
                recorded is not None
                and recorded.snapshot.same_version_as(snapshot)
            )

    async def execute(
        self,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        if isinstance(operation, Recall):
            return await self._recall(operation, context=context)
        if isinstance(operation, ProposeMutation):
            return await self._propose(operation, context=context)
        if isinstance(operation, ApplyMutation):
            return await self._apply(operation, context=context)
        raise SourceExecutionError(
            "memory source supports Recall, ProposeMutation, and "
            "ApplyMutation"
        )

    async def _recall(
        self,
        operation: Recall,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        context.require_readable_scopes(operation.scopes)
        async with self._lock:
            selected = self._select_version(context)

        snapshot = selected.snapshot
        known_at = operation.known_at or snapshot.observed_at
        valid_at = operation.valid_at or snapshot.observed_at
        if known_at > snapshot.observed_at:
            raise SnapshotUnavailableError(
                "known_at cannot exceed the selected memory snapshot"
            )

        requested_scopes = frozenset(operation.scopes)
        requested_kinds = frozenset(operation.kinds)
        matches = []
        for record in selected.records:
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
        binding_rows = []
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
            evidence_item = Evidence(
                kind=SourceKind.MEMORY,
                content=record.content,
                provenance=origin,
                score=score,
                metadata={
                    "memory_id": record.memory_id,
                    "memory_kind": record.kind.value,
                    "scope_kind": record.scope.kind.value,
                    "origin_channel": record.origin.channel.value,
                },
            )
            evidence.append(evidence_item)
            binding_rows.append(
                BindingRow(
                    values={
                        "memory_id": record.memory_id,
                        "kind": record.kind.value,
                        "scope_kind": record.scope.kind.value,
                        "scope_id": record.scope.scope_id,
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
                        "origin_channel": record.origin.channel.value,
                    },
                    evidence_ids=(evidence_item.evidence_id,),
                )
            )
            provenance.append(origin)

        binding_fields = (
            "memory_id",
            "kind",
            "scope_kind",
            "scope_id",
            "recorded_from",
            "recorded_to",
            "valid_from",
            "valid_to",
            "origin_channel",
        )
        return SourceResult(
            value=MemoryRecallResult(
                records=returned_records,
                conflicts=conflicts,
            ),
            result_kind=ResultKind.MEMORY_RECORDS,
            evidence=tuple(evidence),
            bindings=BindingSet(
                fields=binding_fields,
                rows=tuple(binding_rows),
            ),
            provenance=tuple(provenance),
            snapshots=(snapshot,),
        )

    async def _propose(
        self,
        operation: ProposeMutation,
        *,
        context: ExecutionContext,
    ) -> SourceResult[MemoryMutationProposal]:
        draft = operation.draft
        context.require_readable_scopes((draft.scope,))
        context.require_writable_scopes((draft.scope,))
        origin = context.bind_memory_origin(
            scope=draft.scope,
            approval_key=draft.approval_key,
        )
        channel = origin.channel
        required = memory_write_requires_approval(origin, draft.scope)
        requires_approval = required or draft.approval_key is not None

        async with self._lock:
            prior = self._proposals.get(draft.idempotency_key)
            if prior is not None:
                if prior.draft != draft or prior.channel is not channel:
                    raise MemoryIdempotencyConflictError(
                        "idempotency key {!r} belongs to different "
                        "memory intent".format(draft.idempotency_key)
                    )
                proposal = prior.proposal
            else:
                selected = self._select_version(context)
                self._validate_draft(
                    draft,
                    records=selected.records,
                )
                proposal = MemoryMutationProposal(
                    proposal_id=self._proposal_id(
                        draft.idempotency_key
                    ),
                    source=self.descriptor.ref,
                    base_snapshot=selected.snapshot,
                    draft=draft,
                    origin=origin,
                    requires_approval=requires_approval,
                )
                self._proposals[draft.idempotency_key] = _ProposalEntry(
                    draft=draft,
                    channel=channel,
                    proposal=proposal,
                )

        return SourceResult(
            value=proposal,
            result_kind=ResultKind.MEMORY_MUTATION_PROPOSAL,
            snapshots=(proposal.base_snapshot,),
        )

    async def _apply(
        self,
        operation: ApplyMutation,
        *,
        context: ExecutionContext,
    ) -> SourceResult[MemoryMutationReceipt]:
        proposal = operation.proposal
        draft = proposal.draft
        context.require_writable_scopes((draft.scope,))

        async with self._lock:
            recorded = self._receipts.get(draft.idempotency_key)
            if recorded is not None:
                prior_proposal, prior_receipt = recorded
                if prior_proposal != proposal:
                    raise MemoryIdempotencyConflictError(
                        "idempotency key {!r} belongs to a different "
                        "memory proposal".format(draft.idempotency_key)
                    )
                reused = replace(prior_receipt, reused=True)
                return SourceResult(
                    value=reused,
                    result_kind=ResultKind.MEMORY_MUTATION_RECEIPT,
                    snapshots=(
                        reused.previous_snapshot,
                        reused.snapshot,
                    ),
                )

            issued = self._proposals.get(draft.idempotency_key)
            if issued is None or issued.proposal != proposal:
                raise MemoryMutationError(
                    "apply requires a proposal issued by this source"
                )
            current = self._versions[self._current_version]
            if not current.snapshot.same_version_as(
                proposal.base_snapshot
            ):
                raise MemoryVersionConflictError(
                    "memory source {!r} is at version {!r}, not proposal "
                    "base {!r}".format(
                        self.descriptor.ref.source_id,
                        current.snapshot.version,
                        proposal.base_snapshot.version,
                    )
                )
            pinned = context.snapshots.get(self.descriptor.ref)
            if (
                pinned is not None
                and not pinned.same_version_as(proposal.base_snapshot)
            ):
                raise MemoryVersionConflictError(
                    "execution snapshot does not match proposal base"
                )

            self._validate_draft(draft, records=current.records)
            committed_at = self._clock()
            if not isinstance(committed_at, datetime):
                raise MemoryMutationError(
                    "memory source clock returned a non-datetime value"
                )
            require_aware(committed_at, "memory commit time")
            if committed_at <= current.snapshot.observed_at:
                raise MemoryMutationError(
                    "memory commit time must advance beyond its base snapshot"
                )
            records, created_ids, closed_ids = self._apply_changes(
                current.records,
                proposal=proposal,
                committed_at=committed_at,
            )
            self._validate_history(records, snapshot_time=committed_at)
            checksum = self._state_checksum(records)
            version = "sha256:{}".format(checksum)
            snapshot = SnapshotRef(
                source=self.descriptor.ref,
                version=version,
                checksum=checksum,
                observed_at=committed_at,
            )
            self._versions[version] = _MemoryVersion(
                snapshot=snapshot,
                records=records,
            )
            self._current_version = version
            self._descriptor = replace(
                self._descriptor,
                version=version,
            )
            receipt = MemoryMutationReceipt(
                proposal_id=proposal.proposal_id,
                idempotency_key=draft.idempotency_key,
                scope=draft.scope,
                origin=proposal.origin,
                previous_snapshot=current.snapshot,
                snapshot=snapshot,
                created_ids=created_ids,
                closed_ids=closed_ids,
                applied_at=committed_at,
            )
            self._receipts[draft.idempotency_key] = (
                proposal,
                receipt,
            )

        return SourceResult(
            value=receipt,
            result_kind=ResultKind.MEMORY_MUTATION_RECEIPT,
            snapshots=(current.snapshot, snapshot),
        )

    def _select_version(
        self,
        context: ExecutionContext,
    ) -> _MemoryVersion:
        pinned = context.snapshots.get(self.descriptor.ref)
        if pinned is None:
            return self._versions[self._current_version]
        selected = self._versions.get(pinned.version)
        if (
            selected is None
            or not selected.snapshot.same_version_as(pinned)
        ):
            raise SnapshotUnavailableError(
                "memory source {!r} cannot serve snapshot {!r}".format(
                    self.descriptor.ref.source_id,
                    pinned.version,
                )
            )
        return selected

    def _apply_changes(
        self,
        records: Tuple[MemoryRecord, ...],
        *,
        proposal: MemoryMutationProposal,
        committed_at: datetime,
    ) -> Tuple[Tuple[MemoryRecord, ...], Tuple[str, ...], Tuple[str, ...]]:
        mutable = {item.memory_id: item for item in records}
        created_ids = []
        closed_ids = []
        for index, change in enumerate(proposal.draft.changes):
            if isinstance(change, AssertMemory):
                memory_id = self._memory_id(
                    proposal.draft.idempotency_key,
                    index,
                )
                if memory_id in mutable:
                    raise MemoryMutationError(
                        "deterministic memory id already exists"
                    )
                mutable[memory_id] = MemoryRecord(
                    memory_id=memory_id,
                    kind=change.kind,
                    scope=proposal.draft.scope,
                    content=change.content,
                    recorded_from=committed_at,
                    valid_from=change.valid_from,
                    valid_to=change.valid_to,
                    origin=proposal.origin,
                    mutation_id=proposal.proposal_id,
                    evidence=change.evidence,
                    links=change.links,
                    metadata=change.metadata,
                )
                created_ids.append(memory_id)
                continue

            target = mutable[change.target_id]
            mutable[target.memory_id] = replace(
                target,
                recorded_to=committed_at,
            )
            closed_ids.append(target.memory_id)
            if isinstance(change, RetractMemory):
                continue
            if not isinstance(change, SupersedeMemory):
                raise MemoryMutationError(
                    "unsupported prepared memory change"
                )
            memory_id = self._memory_id(
                proposal.draft.idempotency_key,
                index,
            )
            if memory_id in mutable:
                raise MemoryMutationError(
                    "deterministic memory id already exists"
                )
            mutable[memory_id] = MemoryRecord(
                memory_id=memory_id,
                kind=target.kind,
                scope=proposal.draft.scope,
                content=change.content,
                recorded_from=committed_at,
                valid_from=change.valid_from,
                valid_to=change.valid_to,
                origin=proposal.origin,
                mutation_id=proposal.proposal_id,
                evidence=change.evidence,
                links=(
                    MemoryLink(
                        MemoryLinkKind.SUPERSEDES,
                        target.memory_id,
                    ),
                )
                + change.links,
                metadata=change.metadata,
            )
            created_ids.append(memory_id)
        return (
            self._ordered(mutable.values()),
            tuple(created_ids),
            tuple(closed_ids),
        )

    @staticmethod
    def _validate_draft(
        draft: MemoryMutationDraft,
        *,
        records: Tuple[MemoryRecord, ...],
    ) -> None:
        by_id = {item.memory_id: item for item in records}
        for change in draft.changes:
            if isinstance(change, (SupersedeMemory, RetractMemory)):
                target = by_id.get(change.target_id)
                if target is None:
                    raise MemoryMutationError(
                        "memory target {!r} does not exist".format(
                            change.target_id
                        )
                    )
                if target.scope != draft.scope:
                    raise MemoryMutationError(
                        "memory mutation cannot cross scope boundaries"
                    )
                if target.recorded_to is not None:
                    raise MemoryMutationError(
                        "memory target {!r} is not current".format(
                            change.target_id
                        )
                    )
            links = getattr(change, "links", ())
            for link in links:
                linked = by_id.get(link.target_id)
                if linked is None:
                    raise MemoryMutationError(
                        "memory link target {!r} does not exist".format(
                            link.target_id
                        )
                    )
                if linked.scope != draft.scope:
                    raise MemoryMutationError(
                        "memory mutation cannot create cross-scope links"
                    )

    def _proposal_id(self, idempotency_key: str) -> str:
        digest = hashlib.sha256(
            json.dumps(
                [
                    self.descriptor.ref.source_id,
                    idempotency_key,
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return "memory_proposal_{}".format(digest)

    def _memory_id(self, idempotency_key: str, index: int) -> str:
        digest = hashlib.sha256(
            json.dumps(
                [
                    self.descriptor.ref.source_id,
                    idempotency_key,
                    index,
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return "memory_{}".format(digest)

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
    def _ordered(
        records: Iterable[MemoryRecord],
    ) -> Tuple[MemoryRecord, ...]:
        return tuple(sorted(records, key=lambda item: item.memory_id))

    @staticmethod
    def _state_checksum(records: Tuple[MemoryRecord, ...]) -> str:
        payload = []
        for record in InMemoryMemorySource._ordered(records):
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
                    "origin": {
                        "channel": record.origin.channel.value,
                        "trace_id": record.origin.trace_id,
                    },
                    "mutation_id": record.mutation_id,
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
                            "snapshot": (
                                {
                                    "source_id": (
                                        item.provenance.snapshot
                                        .source.source_id
                                    ),
                                    "source_kind": (
                                        item.provenance.snapshot
                                        .source.kind.value
                                    ),
                                    "version": (
                                        item.provenance.snapshot.version
                                    ),
                                    "checksum": (
                                        item.provenance.snapshot.checksum
                                    ),
                                }
                                if item.provenance.snapshot is not None
                                else None
                            ),
                            "valid_from": (
                                item.provenance.valid_from.isoformat()
                                if item.provenance.valid_from is not None
                                else None
                            ),
                            "valid_to": (
                                item.provenance.valid_to.isoformat()
                                if item.provenance.valid_to is not None
                                else None
                            ),
                            "derived_from": list(
                                item.provenance.derived_from
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
