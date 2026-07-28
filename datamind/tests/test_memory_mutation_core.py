"""Governed Memory proposal, mutation, and history contract tests."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from datamind.adapters import InMemoryMemorySource
from datamind.adapters.audit import InMemoryTraceStore
from datamind.dataops import (
    ApplyMutation,
    DataPlan,
    OutputRef,
    ProposeMutation,
    Recall,
    plan_from_json,
    plan_to_json,
)
from datamind.engine import Engine
from datamind.kernel import (
    AssertMemory,
    EffectLevel,
    EffectPolicyError,
    ExecutionContext,
    KernelValidationError,
    MemoryIdempotencyConflictError,
    MemoryKind,
    MemoryLink,
    MemoryLinkKind,
    MemoryMutationDraft,
    MemoryMutationError,
    MemoryMutationProposal,
    MemoryOrigin,
    MemoryOriginChannel,
    MemoryRecord,
    MemoryVersionConflictError,
    RetractMemory,
    ScopeKind,
    ScopeRef,
    SnapshotRef,
    SnapshotSet,
    SourceKind,
    SourceRef,
    SupersedeMemory,
    TraceEventKind,
    thaw_json,
)
from datamind.lifecycle import SourceCatalog


def timestamp(day: int) -> datetime:
    return datetime(2026, 2, day, tzinfo=timezone.utc)


class StepClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class MemoryMutationValueTests(unittest.TestCase):
    def test_core_owns_supersedes_links_and_unique_targets(self) -> None:
        with self.assertRaises(KernelValidationError):
            AssertMemory(
                kind=MemoryKind.FACT,
                content="A replacement disguised as an assertion.",
                links=(
                    MemoryLink(
                        MemoryLinkKind.SUPERSEDES,
                        "old-memory",
                    ),
                ),
            )

        scope = ScopeRef(ScopeKind.PRINCIPAL, "alice")
        with self.assertRaises(KernelValidationError):
            MemoryMutationDraft(
                scope=scope,
                changes=(
                    SupersedeMemory("old-memory", "Replacement."),
                    RetractMemory("old-memory", "Also retract it."),
                ),
                idempotency_key="duplicate-target",
            )

    def test_runtime_origin_requires_trace_authority(self) -> None:
        with self.assertRaises(KernelValidationError):
            MemoryOrigin(MemoryOriginChannel.AGENT_INFERRED)

    def test_apply_plan_codec_preserves_governed_proposal(self) -> None:
        source = SourceRef("enterprise-memory", SourceKind.MEMORY)
        scope = ScopeRef(ScopeKind.WORKSPACE, "risk-team")
        draft = MemoryMutationDraft(
            scope=scope,
            changes=(
                SupersedeMemory(
                    target_id="supplier-v1",
                    content="Supplier approval is revoked.",
                    valid_from=timestamp(3),
                ),
            ),
            idempotency_key="supplier-correction",
            approval_key="risk-memory-write",
        )
        proposal = MemoryMutationProposal(
            proposal_id="proposal-supplier-correction",
            source=source,
            base_snapshot=SnapshotRef(
                source=source,
                version="memory-v1",
                observed_at=timestamp(5),
            ),
            draft=draft,
            origin=MemoryOrigin(
                MemoryOriginChannel.AGENT_INFERRED,
                "trace-proposal",
            ),
            requires_approval=True,
        )
        operation = ApplyMutation(
            source=source,
            proposal=proposal,
            op_id="apply-supplier-correction",
        )
        plan = DataPlan(
            operations=(operation,),
            output=OutputRef(operation.op_id),
            max_effect=EffectLevel.INTERNAL_WRITE,
            plan_id="memory-correction",
        )

        self.assertEqual(plan_from_json(plan_to_json(plan)), plan)


class MemoryMutationExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.principal = ScopeRef(ScopeKind.PRINCIPAL, "alice")
        self.workspace = ScopeRef(ScopeKind.WORKSPACE, "risk-team")
        records = (
            MemoryRecord(
                memory_id="preference-email",
                kind=MemoryKind.PREFERENCE,
                scope=self.principal,
                content="Communication preference is email.",
                recorded_from=timestamp(1),
                valid_from=timestamp(1),
            ),
            MemoryRecord(
                memory_id="supplier-approved",
                kind=MemoryKind.FACT,
                scope=self.workspace,
                content="Supplier approval status is approved.",
                recorded_from=timestamp(1),
                valid_from=timestamp(1),
            ),
        )
        self.source = InMemoryMemorySource(
            source_id="enterprise-memory",
            records=records,
            version="memory-v1",
            observed_at=timestamp(5),
            clock=StepClock(
                timestamp(10),
                timestamp(11),
                timestamp(12),
                timestamp(13),
            ),
        )
        self.catalog = SourceCatalog()
        self.catalog.register(self.source)
        self.trace_store = InMemoryTraceStore()
        self.engine = Engine(
            self.catalog,
            trace_store=self.trace_store,
            replay_artifact_store=self.trace_store,
        )

    async def propose(
        self,
        draft: MemoryMutationDraft,
        *,
        channel: MemoryOriginChannel = (
            MemoryOriginChannel.USER_EXPLICIT
        ),
        trace_id: str,
    ):
        return await self.engine.execute(
            ProposeMutation(
                source=self.source.descriptor.ref,
                draft=draft,
            ),
            context=ExecutionContext(
                request_id="request-{}".format(trace_id),
                trace_id=trace_id,
                readable_scopes=frozenset((draft.scope,)),
                writable_scopes=frozenset((draft.scope,)),
                memory_origin=channel,
            ),
        )

    async def apply(
        self,
        proposal: MemoryMutationProposal,
        *,
        trace_id: str,
        approvals: frozenset = frozenset(),
    ):
        return await self.engine.apply(
            proposal,
            context=ExecutionContext(
                request_id="request-{}".format(trace_id),
                trace_id=trace_id,
                max_effect=EffectLevel.INTERNAL_WRITE,
                writable_scopes=frozenset((proposal.draft.scope,)),
                approvals=approvals,
            ),
        )

    async def recall(
        self,
        query: str,
        *,
        scope: ScopeRef,
        trace_id: str,
        snapshots: SnapshotSet = SnapshotSet(),
        valid_at: datetime = None,
        known_at: datetime = None,
    ):
        return await self.engine.execute(
            Recall(
                source=self.source.descriptor.ref,
                query=query,
                scopes=(scope,),
                valid_at=valid_at,
                known_at=known_at,
            ),
            context=ExecutionContext(
                request_id="request-{}".format(trace_id),
                trace_id=trace_id,
                readable_scopes=frozenset((scope,)),
                snapshots=snapshots,
            ),
        )

    async def test_proposal_is_read_only_then_apply_binds_origin(self) -> None:
        initial = await self.source.current_snapshot()
        draft = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                AssertMemory(
                    kind=MemoryKind.FACT,
                    content="Alice prefers tea for afternoon meetings.",
                    valid_from=timestamp(6),
                ),
            ),
            idempotency_key="remember-tea",
        )

        proposed = await self.propose(
            draft,
            trace_id="trace-propose-tea",
        )
        after_proposal = await self.source.current_snapshot()
        applied = await self.apply(
            proposed.value,
            trace_id="trace-apply-tea",
        )
        current = await self.source.current_snapshot()
        recalled = await self.recall(
            "prefers tea afternoon meetings",
            scope=self.principal,
            trace_id="trace-recall-tea",
        )

        self.assertTrue(after_proposal.same_version_as(initial))
        self.assertFalse(current.same_version_as(initial))
        self.assertEqual(
            proposed.value.base_snapshot,
            initial,
        )
        self.assertEqual(
            applied.value.origin.channel,
            MemoryOriginChannel.USER_EXPLICIT,
        )
        record = recalled.value.records[0]
        self.assertEqual(
            record.origin.trace_id,
            "trace-propose-tea",
        )
        self.assertEqual(
            record.mutation_id,
            proposed.value.proposal_id,
        )
        self.assertEqual(
            applied.value.created_ids,
            (record.memory_id,),
        )

        replayed = await self.engine.replay("trace-apply-tea")
        self.assertEqual(replayed, applied)
        self.assertTrue(
            (await self.source.current_snapshot()).same_version_as(current)
        )

        trace = await self.trace_store.get("trace-apply-tea")
        audit = json.dumps(
            [thaw_json(item.details) for item in trace.events],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn("Alice prefers tea", audit)
        self.assertNotIn(self.principal.scope_id, audit)
        self.assertNotIn(draft.idempotency_key, audit)
        started = next(
            item
            for item in trace.events
            if item.kind is TraceEventKind.OP_STARTED
        )
        self.assertEqual(
            started.details["memory_origin_channel"],
            "user_explicit",
        )

    async def test_inferred_and_shared_writes_require_policy_approval(
        self,
    ) -> None:
        inferred_without_key = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                AssertMemory(
                    MemoryKind.SUMMARY,
                    "Alice often asks for concise risk summaries.",
                ),
            ),
            idempotency_key="inferred-summary-no-key",
        )
        with self.assertRaises(EffectPolicyError):
            await self.propose(
                inferred_without_key,
                channel=MemoryOriginChannel.AGENT_INFERRED,
                trace_id="trace-inferred-no-key",
            )
        denied_trace = await self.trace_store.get(
            "trace-inferred-no-key"
        )
        self.assertFalse(
            any(
                item.kind is TraceEventKind.OP_STARTED
                for item in denied_trace.events
            )
        )

        inferred = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                AssertMemory(
                    MemoryKind.SUMMARY,
                    "Alice often asks for concise risk summaries.",
                ),
            ),
            idempotency_key="inferred-summary",
            approval_key="memory-policy",
        )
        proposed = await self.propose(
            inferred,
            channel=MemoryOriginChannel.AGENT_INFERRED,
            trace_id="trace-inferred-proposal",
        )
        with self.assertRaises(EffectPolicyError):
            await self.apply(
                proposed.value,
                trace_id="trace-inferred-denied",
            )
        applied = await self.apply(
            proposed.value,
            trace_id="trace-inferred-approved",
            approvals=frozenset(("memory-policy",)),
        )
        self.assertEqual(
            applied.value.origin.channel,
            MemoryOriginChannel.AGENT_INFERRED,
        )

        shared_without_key = MemoryMutationDraft(
            scope=self.workspace,
            changes=(
                AssertMemory(
                    MemoryKind.FACT,
                    "The risk team reviews critical suppliers weekly.",
                ),
            ),
            idempotency_key="shared-no-key",
        )
        with self.assertRaises(EffectPolicyError):
            await self.propose(
                shared_without_key,
                trace_id="trace-shared-no-key",
            )

    async def test_supersede_preserves_bitemporal_history(self) -> None:
        initial = await self.source.current_snapshot()
        draft = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                SupersedeMemory(
                    target_id="preference-email",
                    content="Communication preference is phone.",
                    valid_from=timestamp(3),
                ),
            ),
            idempotency_key="correct-preference",
        )
        proposed = await self.propose(
            draft,
            trace_id="trace-propose-preference",
        )
        applied = await self.apply(
            proposed.value,
            trace_id="trace-apply-preference",
        )
        current = await self.recall(
            "communication preference",
            scope=self.principal,
            valid_at=timestamp(4),
            known_at=timestamp(10),
            trace_id="trace-current-preference",
        )
        formerly_known = await self.recall(
            "communication preference",
            scope=self.principal,
            valid_at=timestamp(4),
            known_at=timestamp(5),
            trace_id="trace-former-belief",
        )
        old_snapshot = await self.recall(
            "communication preference",
            scope=self.principal,
            valid_at=timestamp(4),
            known_at=timestamp(5),
            snapshots=SnapshotSet((initial,)),
            trace_id="trace-old-snapshot",
        )

        self.assertIn("phone", current.value.records[0].content)
        self.assertEqual(
            formerly_known.value.records[0].memory_id,
            "preference-email",
        )
        self.assertEqual(
            old_snapshot.value.records[0].memory_id,
            "preference-email",
        )
        replacement = current.value.records[0]
        self.assertEqual(
            replacement.links,
            (
                MemoryLink(
                    MemoryLinkKind.SUPERSEDES,
                    "preference-email",
                ),
            ),
        )
        self.assertEqual(
            applied.value.closed_ids,
            ("preference-email",),
        )

    async def test_retract_hides_current_without_erasing_history(self) -> None:
        initial = await self.source.current_snapshot()
        draft = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                RetractMemory(
                    target_id="preference-email",
                    reason="The user withdrew this preference.",
                ),
            ),
            idempotency_key="retract-preference",
        )
        proposed = await self.propose(
            draft,
            trace_id="trace-propose-retract",
        )
        receipt = await self.apply(
            proposed.value,
            trace_id="trace-apply-retract",
        )
        current = await self.recall(
            "communication preference email",
            scope=self.principal,
            trace_id="trace-current-after-retract",
        )
        historical = await self.recall(
            "communication preference email",
            scope=self.principal,
            snapshots=SnapshotSet((initial,)),
            known_at=timestamp(5),
            trace_id="trace-historical-after-retract",
        )

        self.assertEqual(current.value.records, ())
        self.assertEqual(
            historical.value.records[0].memory_id,
            "preference-email",
        )
        self.assertEqual(receipt.value.created_ids, ())
        self.assertEqual(
            receipt.value.closed_ids,
            ("preference-email",),
        )

    async def test_stale_atomic_proposal_writes_nothing(self) -> None:
        stale_draft = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                AssertMemory(
                    MemoryKind.FACT,
                    "Alice needs a window seat.",
                ),
                AssertMemory(
                    MemoryKind.FACT,
                    "Alice avoids overnight flights.",
                ),
            ),
            idempotency_key="two-travel-facts",
        )
        winning_draft = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                AssertMemory(
                    MemoryKind.FACT,
                    "Alice travels from Shanghai.",
                ),
            ),
            idempotency_key="travel-origin",
        )
        stale = await self.propose(
            stale_draft,
            trace_id="trace-stale-proposal",
        )
        winner = await self.propose(
            winning_draft,
            trace_id="trace-winning-proposal",
        )
        await self.apply(
            winner.value,
            trace_id="trace-winning-apply",
        )

        with self.assertRaises(MemoryVersionConflictError):
            await self.apply(
                stale.value,
                trace_id="trace-stale-apply",
            )
        absent = await self.recall(
            "window seat overnight flights",
            scope=self.principal,
            trace_id="trace-atomic-check",
        )
        present = await self.recall(
            "travels from Shanghai",
            scope=self.principal,
            trace_id="trace-winning-check",
        )

        self.assertEqual(absent.value.records, ())
        self.assertEqual(len(present.value.records), 1)

    async def test_idempotent_retry_reuses_receipt_and_rejects_key_reuse(
        self,
    ) -> None:
        draft = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                AssertMemory(
                    MemoryKind.DECISION,
                    "Use the conservative supplier review policy.",
                ),
            ),
            idempotency_key="supplier-policy-decision",
        )
        proposed = await self.propose(
            draft,
            trace_id="trace-propose-decision",
        )
        first = await self.apply(
            proposed.value,
            trace_id="trace-apply-decision",
        )
        retried = await self.apply(
            proposed.value,
            trace_id="trace-retry-decision",
        )

        self.assertFalse(first.value.reused)
        self.assertTrue(retried.value.reused)
        self.assertEqual(first.value.snapshot, retried.value.snapshot)
        self.assertEqual(first.value.created_ids, retried.value.created_ids)

        conflicting = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                AssertMemory(
                    MemoryKind.DECISION,
                    "Use an unrelated permissive policy.",
                ),
            ),
            idempotency_key="supplier-policy-decision",
        )
        with self.assertRaises(MemoryIdempotencyConflictError):
            await self.propose(
                conflicting,
                trace_id="trace-conflicting-key",
            )

    async def test_cross_scope_target_and_link_are_rejected(self) -> None:
        target_crossing = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                SupersedeMemory(
                    target_id="supplier-approved",
                    content="Supplier approval status is revoked.",
                ),
            ),
            idempotency_key="cross-scope-target",
        )
        with self.assertRaises(MemoryMutationError):
            await self.propose(
                target_crossing,
                trace_id="trace-cross-target",
            )

        link_crossing = MemoryMutationDraft(
            scope=self.principal,
            changes=(
                AssertMemory(
                    MemoryKind.FACT,
                    "A local fact linked to shared state.",
                    links=(
                        MemoryLink(
                            MemoryLinkKind.SUPPORTS,
                            "supplier-approved",
                        ),
                    ),
                ),
            ),
            idempotency_key="cross-scope-link",
        )
        with self.assertRaises(MemoryMutationError):
            await self.propose(
                link_crossing,
                trace_id="trace-cross-link",
            )


if __name__ == "__main__":
    unittest.main()
