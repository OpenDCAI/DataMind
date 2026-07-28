"""Typed, scoped, bi-temporal Memory Recall contract tests."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from datamind.adapters import (
    DocumentRecord,
    InMemoryDocumentSource,
    InMemoryMemorySource,
)
from datamind.adapters.audit import InMemoryTraceStore
from datamind.dataops import (
    DataPlan,
    Compose,
    OutputRef,
    Recall,
    ResultKind,
    Search,
    plan_from_json,
    plan_to_json,
)
from datamind.engine import Engine
from datamind.kernel import (
    EvidenceRef,
    ExecutionContext,
    KernelValidationError,
    MemoryKind,
    MemoryLink,
    MemoryLinkKind,
    MemoryRecord,
    Provenance,
    ScopeKind,
    ScopePolicyError,
    ScopeRef,
    SnapshotRef,
    SnapshotSet,
    SnapshotUnavailableError,
    SourceKind,
    SourceRef,
    TraceEventKind,
)
from datamind.lifecycle import SourceCatalog


def timestamp(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def build_memory_records() -> tuple:
    principal = ScopeRef(ScopeKind.PRINCIPAL, "user-alice")
    workspace = ScopeRef(ScopeKind.WORKSPACE, "risk-team")
    source_evidence = EvidenceRef(
        evidence_id="evidence-supplier-correction",
        provenance=Provenance(
            source=SourceRef("vendor-db", SourceKind.TABLE),
            locator="table://vendors/supplier-a",
            observed_at=timestamp(10),
        ),
    )
    records = (
        MemoryRecord(
            memory_id="supplier-approved-v1",
            kind=MemoryKind.FACT,
            scope=workspace,
            content="Supplier A approval status is approved.",
            recorded_from=timestamp(1),
            recorded_to=timestamp(10),
            valid_from=timestamp(1),
        ),
        MemoryRecord(
            memory_id="supplier-approved-v2",
            kind=MemoryKind.FACT,
            scope=workspace,
            content="Supplier A approval status is revoked.",
            recorded_from=timestamp(10),
            valid_from=timestamp(5),
            evidence=(source_evidence,),
            links=(
                MemoryLink(
                    MemoryLinkKind.SUPERSEDES,
                    "supplier-approved-v1",
                ),
            ),
        ),
        MemoryRecord(
            memory_id="preference-email",
            kind=MemoryKind.PREFERENCE,
            scope=principal,
            content="Communication preference is email.",
            recorded_from=timestamp(2),
        ),
        MemoryRecord(
            memory_id="preference-phone",
            kind=MemoryKind.PREFERENCE,
            scope=principal,
            content="Communication preference is phone.",
            recorded_from=timestamp(3),
            links=(
                MemoryLink(
                    MemoryLinkKind.CONTRADICTS,
                    "preference-email",
                ),
            ),
        ),
        MemoryRecord(
            memory_id="workspace-review",
            kind=MemoryKind.PROCEDURE,
            scope=workspace,
            content=(
                "Workspace escalation policy requires a security review."
            ),
            recorded_from=timestamp(4),
        ),
    )
    return principal, workspace, records


class MemoryValueTests(unittest.TestCase):
    def test_memory_uses_non_empty_half_open_intervals(self) -> None:
        scope = ScopeRef(ScopeKind.PRINCIPAL, "user-alice")

        with self.assertRaises(KernelValidationError):
            MemoryRecord(
                memory_id="invalid",
                kind=MemoryKind.FACT,
                scope=scope,
                content="Invalid interval.",
                recorded_from=timestamp(2),
                recorded_to=timestamp(2),
            )

    def test_bitemporal_slice_separates_reality_from_system_knowledge(
        self,
    ) -> None:
        _, workspace, records = build_memory_records()
        old = next(
            item
            for item in records
            if item.memory_id == "supplier-approved-v1"
        )
        corrected = next(
            item
            for item in records
            if item.memory_id == "supplier-approved-v2"
        )

        self.assertTrue(
            old.is_visible_at(
                valid_at=timestamp(7),
                known_at=timestamp(7),
            )
        )
        self.assertFalse(
            corrected.is_visible_at(
                valid_at=timestamp(7),
                known_at=timestamp(7),
            )
        )
        self.assertFalse(
            old.is_visible_at(
                valid_at=timestamp(7),
                known_at=timestamp(11),
            )
        )
        self.assertTrue(
            corrected.is_visible_at(
                valid_at=timestamp(7),
                known_at=timestamp(11),
            )
        )
        self.assertEqual(corrected.scope, workspace)

    def test_recall_plan_codec_preserves_scopes_and_both_times(self) -> None:
        scope = ScopeRef(ScopeKind.WORKSPACE, "risk-team")
        operation = Recall(
            source=SourceRef("enterprise-memory", SourceKind.MEMORY),
            query="supplier approval status",
            scopes=(scope,),
            kinds=(MemoryKind.FACT,),
            valid_at=timestamp(7),
            known_at=timestamp(11),
            op_id="recall-supplier",
        )
        plan = DataPlan(
            operations=(operation,),
            output=OutputRef(operation.op_id),
            plan_id="temporal-recall",
        )

        self.assertEqual(plan_from_json(plan_to_json(plan)), plan)

    def test_snapshot_cannot_claim_future_transaction_history(self) -> None:
        scope = ScopeRef(ScopeKind.WORKSPACE, "risk-team")
        future_record = MemoryRecord(
            memory_id="future-record",
            kind=MemoryKind.FACT,
            scope=scope,
            content="This record has not been learned yet.",
            recorded_from=timestamp(12),
        )

        with self.assertRaises(KernelValidationError):
            InMemoryMemorySource(
                source_id="invalid-memory",
                records=(future_record,),
                observed_at=timestamp(11),
            )


class MemoryExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.principal, self.workspace, records = build_memory_records()
        self.source = InMemoryMemorySource(
            source_id="enterprise-memory",
            records=records,
            version="memory-v1",
            observed_at=timestamp(12),
        )
        self.catalog = SourceCatalog()
        self.catalog.register(self.source)
        self.trace_store = InMemoryTraceStore()
        self.engine = Engine(
            self.catalog,
            trace_store=self.trace_store,
            replay_artifact_store=self.trace_store,
        )

    async def recall(
        self,
        query: str,
        *,
        scopes: tuple,
        readable_scopes: frozenset,
        valid_at: datetime = None,
        known_at: datetime = None,
        trace_id: str = "trace-memory",
    ):
        return await self.engine.execute(
            Recall(
                source=self.source.descriptor.ref,
                query=query,
                scopes=scopes,
                valid_at=valid_at,
                known_at=known_at,
            ),
            context=ExecutionContext(
                request_id="request-memory",
                trace_id=trace_id,
                readable_scopes=readable_scopes,
            ),
        )

    async def test_recall_answers_as_known_then_and_as_corrected_now(
        self,
    ) -> None:
        historical = await self.recall(
            "supplier approval status",
            scopes=(self.workspace,),
            readable_scopes=frozenset((self.workspace,)),
            valid_at=timestamp(7),
            known_at=timestamp(7),
            trace_id="trace-as-known",
        )
        corrected = await self.recall(
            "supplier approval status",
            scopes=(self.workspace,),
            readable_scopes=frozenset((self.workspace,)),
            valid_at=timestamp(7),
            known_at=timestamp(11),
            trace_id="trace-corrected",
        )

        self.assertEqual(
            historical.value.records[0].memory_id,
            "supplier-approved-v1",
        )
        self.assertEqual(
            corrected.value.records[0].memory_id,
            "supplier-approved-v2",
        )
        self.assertEqual(corrected.result_kind, ResultKind.MEMORY_RECORDS)
        self.assertEqual(
            corrected.provenance[0].derived_from,
            ("evidence-supplier-correction",),
        )

    async def test_explicit_scope_selection_has_no_implicit_inheritance(
        self,
    ) -> None:
        result = await self.recall(
            "workspace escalation security review",
            scopes=(self.workspace,),
            readable_scopes=frozenset(
                (self.principal, self.workspace)
            ),
            trace_id="trace-workspace-only",
        )

        self.assertEqual(
            tuple(item.memory_id for item in result.value.records),
            ("workspace-review",),
        )

    async def test_unreadable_scope_is_rejected_even_for_history(
        self,
    ) -> None:
        with self.assertRaises(ScopePolicyError):
            await self.recall(
                "communication preference",
                scopes=(self.principal,),
                readable_scopes=frozenset((self.workspace,)),
                known_at=timestamp(3),
                trace_id="trace-scope-denied",
            )
        trace = await self.trace_store.get("trace-scope-denied")
        self.assertFalse(
            any(
                item.kind is TraceEventKind.OP_STARTED
                for item in trace.events
            )
        )

    async def test_explicit_contradictions_are_returned_not_overwritten(
        self,
    ) -> None:
        result = await self.recall(
            "communication preference",
            scopes=(self.principal,),
            readable_scopes=frozenset((self.principal,)),
            trace_id="trace-conflict",
        )

        self.assertEqual(
            {item.memory_id for item in result.value.records},
            {"preference-email", "preference-phone"},
        )
        self.assertEqual(
            set(result.value.conflicts[0].record_ids),
            {"preference-email", "preference-phone"},
        )

    async def test_snapshot_bounds_the_available_knowledge_history(
        self,
    ) -> None:
        snapshot = await self.source.current_snapshot()
        context = ExecutionContext.new(
            snapshots=SnapshotSet((snapshot,)),
            readable_scopes=frozenset((self.workspace,)),
        )
        operation = Recall(
            source=self.source.descriptor.ref,
            query="supplier approval status",
            scopes=(self.workspace,),
            known_at=datetime(
                2026,
                1,
                13,
                tzinfo=timezone.utc,
            ),
        )

        with self.assertRaises(SnapshotUnavailableError):
            await self.engine.execute(operation, context=context)

    async def test_trace_hashes_scope_identity_and_replays_offline(
        self,
    ) -> None:
        original = await self.recall(
            "communication preference",
            scopes=(self.principal,),
            readable_scopes=frozenset((self.principal,)),
            trace_id="trace-memory-replay",
        )

        replayed = await self.engine.replay("trace-memory-replay")
        trace = await self.trace_store.get("trace-memory-replay")
        started = next(
            item
            for item in trace.events
            if item.kind is TraceEventKind.OP_STARTED
        )

        self.assertEqual(replayed, original)
        self.assertEqual(len(started.details["scope_fingerprints"]), 1)
        self.assertNotIn(
            self.principal.scope_id,
            str(tuple(item.details for item in trace.events)),
        )

    async def test_recall_composes_with_another_data_surface(self) -> None:
        documents = InMemoryDocumentSource(
            source_id="vendor-policy",
            documents=(
                DocumentRecord(
                    document_id="supplier-rule",
                    content=(
                        "Supplier approval requires a current security review."
                    ),
                ),
            ),
        )
        self.catalog.register(documents)
        recall = Recall(
            source=self.source.descriptor.ref,
            query="supplier approval status",
            scopes=(self.workspace,),
            op_id="recall-status",
        )
        search = Search(
            source=documents.descriptor.ref,
            query="supplier approval security review",
            op_id="search-policy",
        )
        compose = Compose(
            inputs=(OutputRef(recall.op_id), OutputRef(search.op_id)),
            op_id="compose-context",
        )
        plan = DataPlan(
            operations=(recall, search, compose),
            output=OutputRef(compose.op_id),
            plan_id="memory-policy-context",
        )

        result = await self.engine.execute(
            plan,
            context=ExecutionContext.new(
                readable_scopes=frozenset((self.workspace,))
            ),
        )

        self.assertEqual(result.result_kind, ResultKind.EVIDENCE_SET)
        self.assertEqual(
            {item.kind for item in result.evidence},
            {SourceKind.MEMORY, SourceKind.DOCUMENT},
        )
        self.assertEqual(len(result.snapshots), 2)


if __name__ == "__main__":
    unittest.main()
