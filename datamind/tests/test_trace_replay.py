"""Audit safety and offline replay tests for deterministic execution."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from datamind.dataops import Query
from datamind.engine import Executor
from datamind.engine.fingerprint import fingerprint
from datamind.kernel import (
    ExecutionContext,
    KernelValidationError,
    ReplayError,
    SourceDescriptor,
    SourceExecutionError,
    SourceKind,
    SourceRef,
    TraceConflictError,
    TraceEventKind,
    thaw_json,
)
from datamind.adapters.audit import InMemoryTraceStore, JsonlTraceStore
from datamind.lifecycle import (
    SourceCatalog,
)
from datamind.ports import SourceResult
from datamind.tests.test_execution_core import ExecutionFixture


class TraceReplayTests(ExecutionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.trace_store = InMemoryTraceStore()
        self.traced_executor = Executor(
            self.catalog,
            trace_store=self.trace_store,
            artifact_store=self.trace_store,
        )

    async def test_successful_plan_has_contiguous_audit_events(self) -> None:
        context = self.context()

        result = await self.traced_executor.execute(
            self.cross_surface_plan(),
            context=context,
        )
        trace = await self.trace_store.get(context.trace_id)

        self.assertTrue(trace.completed)
        self.assertFalse(trace.failed)
        self.assertEqual(
            tuple(event.kind for event in trace.events),
            (
                TraceEventKind.PLAN_STARTED,
                TraceEventKind.PLAN_VALIDATED,
                TraceEventKind.OP_STARTED,
                TraceEventKind.OP_STARTED,
                TraceEventKind.OP_COMPLETED,
                TraceEventKind.OP_COMPLETED,
                TraceEventKind.OP_STARTED,
                TraceEventKind.OP_COMPLETED,
                TraceEventKind.PLAN_COMPLETED,
            ),
        )
        self.assertEqual(
            tuple(event.sequence for event in trace.events),
            tuple(range(len(trace.events))),
        )
        self.assertEqual(
            trace.events[-1].details["result_fingerprint"],
            fingerprint(result),
        )

    async def test_audit_trace_excludes_raw_plan_and_result_content(self) -> None:
        context = self.context()
        plan = self.cross_surface_plan()

        await self.traced_executor.execute(plan, context=context)
        trace = await self.trace_store.get(context.trace_id)
        audit_payload = json.dumps(
            [thaw_json(event.details) for event in trace.events],
            ensure_ascii=False,
            sort_keys=True,
        )

        self.assertNotIn("SELECT category, amount", audit_payload)
        self.assertNotIn("Travel reimbursement policy", audit_payload)
        self.assertNotIn('"employee_id": 7', audit_payload)
        recorded_plan = await self.trace_store.load_plan(context.trace_id)
        query = recorded_plan.plan.operation("query-expenses")
        self.assertIn("SELECT category, amount", query.statement)

    async def test_replay_is_equivalent_without_live_database(self) -> None:
        context = self.context()
        result = await self.traced_executor.execute(
            self.cross_surface_plan(),
            context=context,
        )
        self.database_path.unlink()

        replayed = await self.traced_executor.replay(context.trace_id)
        trace = await self.trace_store.get(context.trace_id)

        self.assertEqual(replayed, result)
        self.assertEqual(
            trace.events[-1].kind,
            TraceEventKind.REPLAY_COMPLETED,
        )
        self.assertEqual(
            trace.events[-1].details["result_fingerprint"],
            fingerprint(result),
        )

    async def test_failed_operation_records_hash_only_and_cannot_replay(
        self,
    ) -> None:
        context = self.context()
        operation = Query(
            source=self.sqlite_source.descriptor.ref,
            statement="DELETE FROM expenses WHERE employee_id = 7",
            op_id="blocked-delete",
        )

        with self.assertRaises(SourceExecutionError):
            await self.traced_executor.execute(
                operation,
                context=context,
            )
        trace = await self.trace_store.get(context.trace_id)
        payload = json.dumps(
            [thaw_json(event.details) for event in trace.events],
            ensure_ascii=False,
            sort_keys=True,
        )

        self.assertTrue(trace.failed)
        self.assertFalse(trace.completed)
        self.assertIn(TraceEventKind.OP_FAILED, tuple(
            event.kind for event in trace.events
        ))
        self.assertEqual(
            trace.events[-1].kind,
            TraceEventKind.PLAN_FAILED,
        )
        self.assertNotIn("DELETE FROM expenses", payload)
        with self.assertRaises(ReplayError):
            await self.traced_executor.replay(context.trace_id)

    async def test_trace_identity_cannot_be_reused(self) -> None:
        context = self.context()

        await self.traced_executor.execute(
            self.cross_surface_plan(),
            context=context,
        )

        with self.assertRaises(TraceConflictError):
            await self.traced_executor.execute(
                self.cross_surface_plan(),
                context=context,
            )

    async def test_replay_requires_explicit_artifact_store(self) -> None:
        executor = Executor(
            self.catalog,
            trace_store=InMemoryTraceStore(),
        )

        with self.assertRaises(ReplayError):
            await executor.replay("trace-without-artifacts")

    async def test_jsonl_audit_survives_store_reconstruction(self) -> None:
        trace_directory = (
            Path(self._temporary_directory.name) / "audit-traces"
        )
        audit_store = JsonlTraceStore(trace_directory)
        artifact_store = InMemoryTraceStore()
        executor = Executor(
            self.catalog,
            trace_store=audit_store,
            artifact_store=artifact_store,
        )
        context = self.context()

        original = await executor.execute(
            self.cross_surface_plan(),
            context=context,
        )
        reconstructed_store = JsonlTraceStore(trace_directory)
        reconstructed = await reconstructed_store.get(context.trace_id)
        persisted = next(trace_directory.glob("*.jsonl")).read_text(
            encoding="utf-8"
        )

        self.assertTrue(reconstructed.completed)
        self.assertNotIn("SELECT category, amount", persisted)
        self.assertNotIn("Travel reimbursement policy", persisted)

        self.database_path.unlink()
        replay_executor = Executor(
            self.catalog,
            trace_store=reconstructed_store,
            artifact_store=artifact_store,
        )
        replayed = await replay_executor.replay(context.trace_id)
        self.assertEqual(replayed, original)

    async def test_replay_detects_mutated_native_result_artifact(self) -> None:
        class MutableTableSource:
            def __init__(self) -> None:
                self.descriptor = SourceDescriptor(
                    ref=SourceRef("mutable-table", SourceKind.TABLE),
                    display_name="Mutable table",
                    capabilities=frozenset(("query",)),
                )

            async def execute(
                self,
                operation: Query,
                *,
                context: ExecutionContext,
            ) -> SourceResult:
                del context
                return SourceResult(
                    value={"rows": [1]},
                    result_kind=operation.output_kind,
                )

        source = MutableTableSource()
        catalog = SourceCatalog()
        catalog.register(source)
        store = InMemoryTraceStore()
        executor = Executor(
            catalog,
            trace_store=store,
            artifact_store=store,
        )
        context = self.context()
        operation = Query(
            source=source.descriptor.ref,
            statement="SELECT 1",
        )
        result = await executor.execute(operation, context=context)
        result.value["rows"].append(2)

        with self.assertRaises(ReplayError):
            await executor.replay(context.trace_id)
        trace = await store.get(context.trace_id)
        self.assertEqual(
            trace.events[-1].kind,
            TraceEventKind.REPLAY_FAILED,
        )

    async def test_untraced_execution_skips_result_fingerprinting(self) -> None:
        class OpaqueValue:
            def __repr__(self) -> str:
                raise AssertionError("untraced execution called repr()")

        class OpaqueTableSource:
            def __init__(self) -> None:
                self.value = OpaqueValue()
                self.descriptor = SourceDescriptor(
                    ref=SourceRef("opaque-table", SourceKind.TABLE),
                    display_name="Opaque table",
                    capabilities=frozenset(("query",)),
                )

            async def execute(
                self,
                operation: Query,
                *,
                context: ExecutionContext,
            ) -> SourceResult:
                del context
                return SourceResult(
                    value=self.value,
                    result_kind=operation.output_kind,
                )

        source = OpaqueTableSource()
        catalog = SourceCatalog()
        catalog.register(source)
        executor = Executor(catalog)
        operation = Query(
            source=source.descriptor.ref,
            statement="SELECT opaque_value",
        )

        result = await executor.execute(
            operation,
            context=self.context(),
        )

        self.assertIs(result.value, source.value)


class FingerprintTests(unittest.TestCase):
    def test_mapping_order_does_not_change_fingerprint(self) -> None:
        first = {"alpha": 1, "beta": [2, 3]}
        second = {"beta": [2, 3], "alpha": 1}

        self.assertEqual(fingerprint(first), fingerprint(second))

    def test_content_change_changes_fingerprint(self) -> None:
        self.assertNotEqual(
            fingerprint({"amount": 80}),
            fingerprint({"amount": 81}),
        )

    def test_recursive_native_values_can_be_fingerprinted(self) -> None:
        value = []
        value.append(value)

        self.assertEqual(fingerprint(value), fingerprint(value))


class TraceStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_event_requires_terminal_plan_event(self) -> None:
        store = InMemoryTraceStore()
        await store.start("trace-state", details={"plan_id": "plan"})

        with self.assertRaises(KernelValidationError):
            await store.append(
                "trace-state",
                TraceEventKind.REPLAY_FAILED,
                details={"error_type": "test"},
            )


if __name__ == "__main__":
    unittest.main()
