"""Vertical-slice tests for Catalog -> Executor -> reference adapters."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from datamind.adapters import (
    DocumentRecord,
    InMemoryDocumentSource,
    SQLiteReadSource,
    SQLiteTable,
)
from datamind.dataops import (
    Compose,
    ContextPack,
    DataPlan,
    Describe,
    Discover,
    OutputRef,
    Query,
    ResultKind,
    ResultStatus,
    Search,
)
from datamind.engine import Executor
from datamind.kernel import (
    Budget,
    BudgetExceeded,
    EffectLevel,
    EffectPolicyError,
    ExecutionContext,
    PlanValidationError,
    SourceExecutionError,
    SourceKind,
    SourceRef,
)
from datamind.lifecycle import (
    DuplicateSourceError,
    SourceCatalog,
)


class ExecutionFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self._temporary_directory.name) / "expenses.sqlite3"
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "CREATE TABLE expenses "
                "(employee_id INTEGER, category TEXT, amount REAL)"
            )
            connection.executemany(
                "INSERT INTO expenses VALUES (?, ?, ?)",
                (
                    (7, "meal", 80.0),
                    (7, "hotel", 600.0),
                    (8, "meal", 50.0),
                ),
            )

        self.document_source = InMemoryDocumentSource(
            source_id="policy-kb",
            version="policy-v3",
            documents=(
                DocumentRecord(
                    document_id="travel-policy",
                    content=(
                        "Travel reimbursement policy: meals are reimbursable "
                        "up to 100 dollars."
                    ),
                    metadata={"department": "sales"},
                ),
                DocumentRecord(
                    document_id="security-policy",
                    content="Security policy requires hardware keys.",
                    metadata={"department": "security"},
                ),
            ),
        )
        self.sqlite_source = SQLiteReadSource(
            source_id="warehouse",
            database_path=self.database_path,
        )
        self.catalog = SourceCatalog()
        self.catalog.register(self.document_source)
        self.catalog.register(self.sqlite_source)
        self.executor = Executor(self.catalog)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def context(
        self,
        *,
        max_effect: EffectLevel = EffectLevel.READ,
        budget: Budget = Budget(),
    ) -> ExecutionContext:
        return ExecutionContext.new(
            max_effect=max_effect,
            budget=budget,
        )

    def cross_surface_plan(self) -> DataPlan:
        search = Search(
            source=self.document_source.descriptor.ref,
            query="travel reimbursement policy",
            filters={"department": "sales"},
            op_id="search-policy",
        )
        query = Query(
            source=self.sqlite_source.descriptor.ref,
            statement=(
                "SELECT category, amount FROM expenses "
                "WHERE employee_id = :employee_id ORDER BY category"
            ),
            parameters={"employee_id": 7},
            op_id="query-expenses",
        )
        compose = Compose(
            inputs=(
                OutputRef(search.op_id),
                OutputRef(query.op_id),
            ),
            strategy="evidence_union",
            op_id="compose-context",
        )
        return DataPlan(
            operations=(search, query, compose),
            output=OutputRef(compose.op_id),
            plan_id="travel-compliance",
            budget=Budget(max_actions=3),
        )


class SourceCatalogTests(ExecutionFixture):
    async def test_catalog_registration_is_explicit_and_kind_filterable(
        self,
    ) -> None:
        self.assertEqual(len(self.catalog), 2)
        self.assertEqual(
            tuple(self.catalog.descriptors()),
            ("policy-kb", "warehouse"),
        )
        table_sources = self.catalog.discover((SourceKind.TABLE,))

        self.assertEqual(len(table_sources), 1)
        self.assertEqual(table_sources[0].ref.source_id, "warehouse")

    async def test_catalog_rejects_duplicate_logical_ids(self) -> None:
        with self.assertRaises(DuplicateSourceError):
            self.catalog.register(self.document_source)


class ExecutorTests(ExecutionFixture):
    async def test_cross_surface_plan_executes_end_to_end(self) -> None:
        context = self.context(budget=Budget(max_actions=3))

        result = await self.executor.execute(
            self.cross_surface_plan(),
            context=context,
        )

        self.assertEqual(result.result_kind, ResultKind.EVIDENCE_SET)
        self.assertIsInstance(result.value, ContextPack)
        self.assertEqual(len(result.value.items), 2)
        self.assertEqual(len(result.evidence), 3)
        self.assertEqual(len(result.snapshots), 2)
        self.assertEqual(result.usage.actions, 3)
        self.assertEqual(result.trace_id, context.trace_id)
        self.assertEqual(result.status, ResultStatus.OK)
        self.assertIsInstance(result.value.items[1].value, SQLiteTable)

    async def test_discover_and_describe_are_catalog_operations(self) -> None:
        discovered = await self.executor.execute(
            Discover(kinds=(SourceKind.DOCUMENT,)),
            context=self.context(),
        )
        described = await self.executor.execute(
            Describe(source=self.sqlite_source.descriptor.ref),
            context=self.context(),
        )

        self.assertEqual(len(discovered.value), 1)
        self.assertEqual(
            described.value.ref,
            self.sqlite_source.descriptor.ref,
        )
        self.assertEqual(described.usage.actions, 1)

    async def test_effect_is_denied_before_read_execution(self) -> None:
        operation = Search(
            source=self.document_source.descriptor.ref,
            query="travel",
        )

        with self.assertRaises(EffectPolicyError):
            await self.executor.execute(
                operation,
                context=self.context(max_effect=EffectLevel.PURE),
            )

    async def test_context_action_budget_is_checked_before_plan(self) -> None:
        with self.assertRaises(BudgetExceeded):
            await self.executor.execute(
                self.cross_surface_plan(),
                context=self.context(budget=Budget(max_actions=2)),
            )

    async def test_unknown_source_fails_static_plan_validation(self) -> None:
        operation = Query(
            source=SourceRef("missing", SourceKind.TABLE),
            statement="SELECT 1",
            op_id="unknown-query",
        )
        plan = DataPlan(
            operations=(operation,),
            output=OutputRef(operation.op_id),
            plan_id="unknown-source",
        )

        with self.assertRaises(PlanValidationError):
            await self.executor.execute(plan, context=self.context())

    async def test_output_path_selects_typed_dataclass_fields(self) -> None:
        operation = Query(
            source=self.sqlite_source.descriptor.ref,
            statement=(
                "SELECT category, amount FROM expenses "
                "WHERE employee_id = 7 ORDER BY amount"
            ),
            op_id="path-query",
        )
        plan = DataPlan(
            operations=(operation,),
            output=OutputRef(operation.op_id, ("rows", 0, 1)),
            plan_id="typed-output-path",
        )

        result = await self.executor.execute(plan, context=self.context())

        self.assertEqual(result.value, 80.0)


class SQLiteSafetyTests(ExecutionFixture):
    async def test_sqlite_connection_rejects_write_statements(self) -> None:
        operation = Query(
            source=self.sqlite_source.descriptor.ref,
            statement="DELETE FROM expenses",
        )

        with self.assertRaises(SourceExecutionError):
            await self.executor.execute(operation, context=self.context())

        with sqlite3.connect(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM expenses"
            ).fetchone()[0]
        self.assertEqual(count, 3)

    async def test_sqlite_truncation_is_an_explicit_partial_result(self) -> None:
        limited = SQLiteReadSource(
            source_id="limited-warehouse",
            database_path=self.database_path,
            row_limit=1,
        )
        catalog = SourceCatalog()
        catalog.register(limited)
        executor = Executor(catalog)
        operation = Query(
            source=limited.descriptor.ref,
            statement="SELECT employee_id FROM expenses ORDER BY employee_id",
        )

        result = await executor.execute(operation, context=self.context())

        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertTrue(result.value.truncated)
        self.assertEqual(len(result.value.rows), 1)
        self.assertTrue(result.warnings)


if __name__ == "__main__":
    unittest.main()
