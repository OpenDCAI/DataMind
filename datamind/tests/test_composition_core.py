"""Typed cross-surface composition and bounded scheduling tests."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from typing import Any, ClassVar, FrozenSet, Tuple

from datamind.dataops import (
    BindingPredicate,
    BindingSet,
    ComparisonOperator,
    Compose,
    DataPlan,
    EvidenceSet,
    Filter,
    Fuse,
    Join,
    OperationMixin,
    OperationSignature,
    OutputRef,
    Project,
    Query,
    ResultKind,
    Search,
    plan_from_json,
    plan_to_json,
    validate_plan,
)
from datamind.adapters.audit import InMemoryTraceStore
from datamind.engine import Executor
from datamind.kernel import (
    Budget,
    EffectLevel,
    ExecutionContext,
    SourceDescriptor,
    SourceKind,
    SourceRef,
)
from datamind.lifecycle import SourceCatalog
from datamind.ports import SourceResult
from datamind.tests.test_execution_core import ExecutionFixture


class CompositionExecutionTests(ExecutionFixture):
    def joined_plan(self) -> DataPlan:
        search = Search(
            source=self.document_source.descriptor.ref,
            query="travel reimbursement policy",
            filters={"department": "sales"},
            op_id="search-policy",
        )
        query = Query(
            source=self.sqlite_source.descriptor.ref,
            statement=(
                "SELECT 'sales' AS department, SUM(amount) AS amount "
                "FROM expenses WHERE employee_id = 7"
            ),
            op_id="query-expenses",
        )
        project_policy = Project(
            inputs=(OutputRef(search.op_id),),
            fields=("document_id", "metadata.department"),
            op_id="project-policy",
        )
        project_expenses = Project(
            inputs=(OutputRef(query.op_id),),
            fields=("department", "amount"),
            op_id="project-expenses",
        )
        join = Join(
            inputs=(
                OutputRef(project_policy.op_id),
                OutputRef(project_expenses.op_id),
            ),
            left_on=("metadata.department",),
            right_on=("department",),
            left_alias="policy",
            right_alias="expense",
            op_id="join-policy-expenses",
        )
        return DataPlan(
            operations=(
                search,
                query,
                project_policy,
                project_expenses,
                join,
            ),
            output=OutputRef(join.op_id),
            plan_id="typed-policy-expense-join",
            budget=Budget(max_actions=5),
        )

    async def test_project_and_exact_join_preserve_evidence(self) -> None:
        result = await self.executor.execute(
            self.joined_plan(),
            context=self.context(budget=Budget(max_actions=5)),
        )

        self.assertEqual(result.result_kind, ResultKind.BINDING_SET)
        self.assertIsInstance(result.value, BindingSet)
        self.assertEqual(len(result.value.rows), 1)
        row = result.value.rows[0]
        self.assertEqual(
            row.values["policy.document_id"],
            "travel-policy",
        )
        self.assertEqual(row.values["expense.amount"], 680.0)
        self.assertEqual(len(row.evidence_ids), 2)
        self.assertEqual(
            set(row.evidence_ids),
            {item.evidence_id for item in result.evidence},
        )
        self.assertEqual(result.bindings, result.value)
        self.assertEqual(len(result.snapshots), 2)

    async def test_filter_returns_only_evidence_for_retained_rows(self) -> None:
        query = Query(
            source=self.sqlite_source.descriptor.ref,
            statement=(
                "SELECT category, amount FROM expenses "
                "WHERE employee_id = 7 ORDER BY amount"
            ),
            op_id="query-expenses",
        )
        project = Project(
            inputs=(OutputRef(query.op_id),),
            fields=("category", "amount"),
            op_id="project-expenses",
        )
        filtered = Filter(
            inputs=(OutputRef(project.op_id),),
            predicate=BindingPredicate(
                field="amount",
                operator=ComparisonOperator.GT,
                value=100,
            ),
            op_id="filter-large-expenses",
        )
        plan = DataPlan(
            operations=(query, project, filtered),
            output=OutputRef(filtered.op_id),
            plan_id="filter-expenses",
        )

        result = await self.executor.execute(
            plan,
            context=self.context(),
        )

        self.assertEqual(len(result.value.rows), 1)
        self.assertEqual(result.value.rows[0].values["category"], "hotel")
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(
            result.value.rows[0].evidence_ids,
            (result.evidence[0].evidence_id,),
        )

    async def test_fuse_deduplicates_by_provenance_and_reranks(self) -> None:
        first = Search(
            source=self.document_source.descriptor.ref,
            query="travel reimbursement meals",
            op_id="search-travel",
        )
        second = Search(
            source=self.document_source.descriptor.ref,
            query="meals reimbursable dollars",
            op_id="search-meals",
        )
        fuse = Fuse(
            inputs=(OutputRef(first.op_id), OutputRef(second.op_id)),
            limit=5,
            op_id="fuse-policy",
        )
        plan = DataPlan(
            operations=(first, second, fuse),
            output=OutputRef(fuse.op_id),
            plan_id="fuse-evidence",
        )

        result = await self.executor.execute(
            plan,
            context=self.context(),
        )

        self.assertIsInstance(result.value, EvidenceSet)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(
            result.value.evidence_ids,
            (result.evidence[0].evidence_id,),
        )
        self.assertGreater(result.evidence[0].score, 1 / 61)

    def test_validator_rejects_native_table_as_filter_input(self) -> None:
        query = Query(
            source=self.sqlite_source.descriptor.ref,
            statement="SELECT category FROM expenses",
            op_id="query-expenses",
        )
        filtered = Filter(
            inputs=(OutputRef(query.op_id),),
            predicate=BindingPredicate(
                field="category",
                operator=ComparisonOperator.EQ,
                value="meal",
            ),
            op_id="invalid-filter",
        )
        plan = DataPlan(
            operations=(query, filtered),
            output=OutputRef(filtered.op_id),
            plan_id="invalid-dataflow",
        )

        report = validate_plan(
            plan,
            sources=self.catalog.descriptors(),
        )

        self.assertIn(
            "incompatible_input_kind",
            {issue.code for issue in report.issues},
        )

    def test_new_operations_have_lossless_plan_codec(self) -> None:
        plan = self.joined_plan()

        decoded = plan_from_json(plan_to_json(plan))

        self.assertEqual(decoded, plan)

    async def test_join_plan_replays_without_live_core_reexecution_drift(
        self,
    ) -> None:
        store = InMemoryTraceStore()
        executor = Executor(
            self.catalog,
            trace_store=store,
            artifact_store=store,
        )
        context = self.context(budget=Budget(max_actions=5))

        original = await executor.execute(
            self.joined_plan(),
            context=context,
        )
        replayed = await executor.replay(context.trace_id)

        self.assertEqual(replayed, original)


class ReadWaveSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_reads_start_in_the_same_wave(self) -> None:
        release = asyncio.Event()
        started = (asyncio.Event(), asyncio.Event())

        class CoordinatedReadSource:
            def __init__(self, index: int) -> None:
                self._index = index
                self.descriptor = SourceDescriptor(
                    ref=SourceRef(
                        "read-{}".format(index),
                        SourceKind.DOCUMENT,
                    ),
                    display_name="Coordinated read {}".format(index),
                    capabilities=frozenset(("search",)),
                )

            async def execute(
                self,
                operation: Search,
                *,
                context: ExecutionContext,
            ) -> SourceResult[Any]:
                del operation, context
                started[self._index].set()
                await release.wait()
                return SourceResult(
                    value=(),
                    result_kind=ResultKind.DOCUMENT_HITS,
                )

        sources = (CoordinatedReadSource(0), CoordinatedReadSource(1))
        catalog = SourceCatalog()
        for source in sources:
            catalog.register(source)
        reads = tuple(
            Search(
                source=source.descriptor.ref,
                query="policy",
                op_id="read-{}".format(index),
            )
            for index, source in enumerate(sources)
        )
        compose = Compose(
            inputs=tuple(OutputRef(item.op_id) for item in reads),
            op_id="compose-reads",
        )
        plan = DataPlan(
            operations=reads + (compose,),
            output=OutputRef(compose.op_id),
            plan_id="parallel-read-wave",
        )
        task = asyncio.create_task(
            Executor(catalog, max_parallelism=2).execute(
                plan,
                context=ExecutionContext.new(),
            )
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*(item.wait() for item in started)),
                timeout=1,
            )
        finally:
            release.set()
        result = await task

        self.assertEqual(result.result_kind, ResultKind.EVIDENCE_SET)


@dataclass(frozen=True)
class _WriteOperation(OperationMixin):
    source: SourceRef
    op_id: str
    inputs: Tuple[OutputRef[Any], ...] = field(default=(), init=False)

    operation: ClassVar[str] = "test_write"
    output_kind: ClassVar[ResultKind] = ResultKind.TABLE
    signature: ClassVar[OperationSignature] = OperationSignature(
        output_kind=output_kind,
    )
    effect_level: ClassVar[EffectLevel] = EffectLevel.INTERNAL_WRITE
    allowed_source_kinds: ClassVar[FrozenSet[SourceKind]] = frozenset(
        (SourceKind.TABLE,)
    )

    def __post_init__(self) -> None:
        self._validate_common()


class WriteSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_writes_are_serialized(self) -> None:
        class WriteSource:
            def __init__(self) -> None:
                self.in_flight = 0
                self.max_in_flight = 0
                self.descriptor = SourceDescriptor(
                    ref=SourceRef("state", SourceKind.TABLE),
                    display_name="State",
                    capabilities=frozenset(("test_write",)),
                    max_effect=EffectLevel.INTERNAL_WRITE,
                )

            async def execute(
                self,
                operation: _WriteOperation,
                *,
                context: ExecutionContext,
            ) -> SourceResult[Any]:
                del context
                self.in_flight += 1
                self.max_in_flight = max(
                    self.max_in_flight,
                    self.in_flight,
                )
                await asyncio.sleep(0)
                self.in_flight -= 1
                return SourceResult(
                    value={"op_id": operation.op_id},
                    result_kind=ResultKind.TABLE,
                )

        source = WriteSource()
        catalog = SourceCatalog()
        catalog.register(source)
        writes = (
            _WriteOperation(source=source.descriptor.ref, op_id="write-a"),
            _WriteOperation(source=source.descriptor.ref, op_id="write-b"),
        )
        compose = Compose(
            inputs=tuple(OutputRef(item.op_id) for item in writes),
            op_id="compose-writes",
        )
        plan = DataPlan(
            operations=writes + (compose,),
            output=OutputRef(compose.op_id),
            plan_id="serialized-writes",
            max_effect=EffectLevel.INTERNAL_WRITE,
        )

        await Executor(catalog, max_parallelism=4).execute(
            plan,
            context=ExecutionContext.new(
                max_effect=EffectLevel.INTERNAL_WRITE,
            ),
        )

        self.assertEqual(source.max_in_flight, 1)
