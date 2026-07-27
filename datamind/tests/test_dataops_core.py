"""Behavioural tests for typed DataOps, plans, results, and codecs."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from datamind.dataops import (
    Compose,
    DataPlan,
    Evidence,
    OutputRef,
    Query,
    ResultEnvelope,
    ResultKind,
    ResultStatus,
    Search,
    plan_from_json,
    plan_to_json,
    require_valid_plan,
    validate_plan,
)
from datamind.kernel import (
    Budget,
    EffectLevel,
    KernelValidationError,
    PlanValidationError,
    Provenance,
    SerializationError,
    SnapshotRef,
    SourceDescriptor,
    SourceKind,
    SourceRef,
)


def build_cross_surface_plan() -> tuple:
    knowledge_base = SourceRef("policy-kb", SourceKind.DOCUMENT)
    warehouse = SourceRef("warehouse", SourceKind.TABLE)
    search = Search(
        source=knowledge_base,
        query="travel reimbursement policy",
        limit=5,
        filters={"department": "sales"},
        op_id="find-policy",
    )
    query = Query(
        source=warehouse,
        statement="SELECT amount FROM expenses WHERE employee_id = :id",
        parameters={"id": 7},
        op_id="find-expenses",
    )
    compose = Compose(
        inputs=(OutputRef(search.op_id), OutputRef(query.op_id)),
        strategy="policy_compliance",
        op_id="compose-answer",
    )
    plan = DataPlan(
        operations=(search, query, compose),
        output=OutputRef(compose.op_id),
        plan_id="travel-review",
        budget=Budget(max_actions=3),
    )
    sources = {
        knowledge_base.source_id: SourceDescriptor(
            ref=knowledge_base,
            display_name="Policy KB",
            capabilities=frozenset(("search",)),
            version="kb-7",
        ),
        warehouse.source_id: SourceDescriptor(
            ref=warehouse,
            display_name="Expense Warehouse",
            capabilities=frozenset(("query",)),
            version="db-12",
        ),
    }
    return plan, sources


class OperationTests(unittest.TestCase):
    def test_operations_reject_incompatible_source_kinds(self) -> None:
        table = SourceRef("warehouse", SourceKind.TABLE)

        with self.assertRaises(KernelValidationError):
            Search(source=table, query="anything")

    def test_operation_payloads_are_copied_and_immutable(self) -> None:
        source = SourceRef("warehouse", SourceKind.TABLE)
        parameters = {"ids": [1, 2]}
        operation = Query(
            source=source,
            statement="SELECT * FROM item WHERE id IN :ids",
            parameters=parameters,
        )
        parameters["ids"].append(3)

        self.assertEqual(operation.parameters["ids"], (1, 2))
        with self.assertRaises(TypeError):
            operation.parameters["ids"] = (9,)


class DataPlanTests(unittest.TestCase):
    def test_valid_cross_surface_plan_has_stable_topological_order(self) -> None:
        plan, sources = build_cross_surface_plan()

        report = validate_plan(plan, sources=sources)

        self.assertTrue(report.valid)
        self.assertEqual(
            report.topological_order,
            ("find-policy", "find-expenses", "compose-answer"),
        )
        self.assertIs(require_valid_plan(plan, sources=sources), plan)

    def test_validator_reports_unknown_capability_and_static_budget(self) -> None:
        plan, sources = build_cross_surface_plan()
        knowledge_base = sources["policy-kb"]
        sources["policy-kb"] = SourceDescriptor(
            ref=knowledge_base.ref,
            display_name=knowledge_base.display_name,
            capabilities=frozenset(("describe",)),
        )
        constrained = DataPlan(
            operations=plan.operations,
            output=plan.output,
            plan_id=plan.plan_id,
            budget=Budget(max_actions=2),
        )

        report = validate_plan(constrained, sources=sources)
        codes = {issue.code for issue in report.issues}

        self.assertIn("unsupported_operation", codes)
        self.assertIn("budget_exceeded", codes)
        with self.assertRaises(PlanValidationError):
            report.require_valid()

    def test_validator_rejects_effect_above_plan_ceiling(self) -> None:
        plan, sources = build_cross_surface_plan()
        pure_only = DataPlan(
            operations=plan.operations,
            output=plan.output,
            plan_id=plan.plan_id,
            max_effect=EffectLevel.PURE,
        )

        report = validate_plan(pure_only, sources=sources)

        self.assertEqual(
            sum(
                issue.code == "effect_exceeds_plan"
                for issue in report.issues
            ),
            2,
        )

    def test_validator_detects_missing_inputs_and_cycles(self) -> None:
        first = Compose(inputs=(OutputRef("second"),), op_id="first")
        second = Compose(inputs=(OutputRef("first"),), op_id="second")
        cyclic = DataPlan(
            operations=(first, second),
            output=OutputRef("first"),
            plan_id="cycle",
        )
        missing = DataPlan(
            operations=(
                Compose(inputs=(OutputRef("absent"),), op_id="compose"),
            ),
            output=OutputRef("compose"),
            plan_id="missing",
        )

        self.assertIn(
            "cyclic_plan",
            {issue.code for issue in validate_plan(cyclic).issues},
        )
        self.assertIn(
            "missing_input",
            {issue.code for issue in validate_plan(missing).issues},
        )

    def test_validator_rejects_operations_unrelated_to_output(self) -> None:
        plan, _ = build_cross_surface_plan()
        disconnected = Search(
            source=SourceRef("archive", SourceKind.DOCUMENT),
            query="unused",
            op_id="unused-search",
        )
        plan_with_dead_work = DataPlan(
            operations=plan.operations + (disconnected,),
            output=plan.output,
            plan_id="dead-work",
        )

        report = validate_plan(plan_with_dead_work)

        self.assertIn(
            "unreachable_operation",
            {issue.code for issue in report.issues},
        )


class SerializationTests(unittest.TestCase):
    def test_json_round_trip_is_explicit_and_lossless(self) -> None:
        plan, _ = build_cross_surface_plan()

        encoded = plan_to_json(plan)
        decoded = plan_from_json(encoded)

        self.assertEqual(decoded, plan)
        self.assertEqual(plan_to_json(decoded), encoded)

    def test_codec_rejects_unknown_schema_version(self) -> None:
        plan, _ = build_cross_surface_plan()
        encoded = plan_to_json(plan).replace(
            '"version": "1"',
            '"version": "2"',
        )

        with self.assertRaises(SerializationError):
            plan_from_json(encoded)


class ResultEnvelopeTests(unittest.TestCase):
    def test_result_keeps_native_value_and_normalized_evidence(self) -> None:
        source = SourceRef("policy-kb", SourceKind.DOCUMENT)
        snapshot = SnapshotRef(
            source=source,
            version="v7",
            observed_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        )
        provenance = Provenance(
            source=source,
            snapshot=snapshot,
            locator="document://travel-policy#meal-limit",
        )
        evidence = Evidence(
            kind=SourceKind.DOCUMENT,
            content="Meals are reimbursable up to the documented limit.",
            provenance=provenance,
            score=0.95,
        )
        native_hits = [{"document_id": "travel-policy", "score": 0.95}]

        result = ResultEnvelope(
            op_id="find-policy",
            value=native_hits,
            result_kind=ResultKind.DOCUMENT_HITS,
            trace_id="trace-1",
            evidence=(evidence,),
            provenance=(provenance,),
            snapshots=(snapshot,),
        )

        self.assertIs(result.value, native_hits)
        self.assertEqual(result.evidence[0].provenance.snapshot, snapshot)

    def test_partial_result_requires_an_explanation(self) -> None:
        with self.assertRaises(KernelValidationError):
            ResultEnvelope(
                op_id="query",
                value=[],
                result_kind=ResultKind.TABLE,
                trace_id="trace-2",
                status=ResultStatus.PARTIAL,
            )


if __name__ == "__main__":
    unittest.main()
