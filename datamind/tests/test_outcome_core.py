"""Append-only external Outcome contract and reference-store tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Optional

from datamind.adapters.audit import (
    InMemoryOutcomeStore,
    JsonlOutcomeStore,
)
from datamind.engine import Engine
from datamind.kernel import (
    EvaluatorKind,
    KernelValidationError,
    OutcomeAssertion,
    OutcomeConflictError,
    OutcomeNotFoundError,
    OutcomeRecord,
    OutcomeTarget,
    OutcomeTargetKind,
    UnsupportedOutcomeError,
)
from datamind.lifecycle import SourceCatalog


def passing_outcome(
    *,
    target: Optional[OutcomeTarget] = None,
    idempotency_key: str = "run-1:task-1",
    outcome_id: Optional[str] = None,
) -> OutcomeRecord:
    values = {
        "target": target
        or OutcomeTarget(
            OutcomeTargetKind.RESOLUTION,
            "resolution-1",
        ),
        "task_id": "task-1",
        "evaluator_kind": EvaluatorKind.PROGRAM,
        "evaluator_name": "datamind-bench",
        "evaluator_version": "0.1",
        "assertions": (
            OutcomeAssertion(
                "native_result_correct",
                True,
                score=1,
            ),
            OutcomeAssertion(
                "provenance_complete",
                True,
                score="0.95",
            ),
        ),
        "succeeded": True,
        "idempotency_key": idempotency_key,
    }
    if outcome_id is not None:
        values["outcome_id"] = outcome_id
    return OutcomeRecord(**values)


class OutcomeValueTests(unittest.TestCase):
    def test_overall_success_must_equal_all_assertions(self) -> None:
        with self.assertRaises(KernelValidationError):
            OutcomeRecord(
                target=OutcomeTarget(
                    OutcomeTargetKind.TRACE,
                    "trace-1",
                ),
                task_id="task",
                evaluator_kind=EvaluatorKind.PROGRAM,
                evaluator_name="oracle",
                evaluator_version="1",
                assertions=(
                    OutcomeAssertion("result", False),
                ),
                succeeded=True,
                idempotency_key="run:task",
            )

    def test_assertion_score_is_bounded_and_decimal_backed(self) -> None:
        assertion = OutcomeAssertion("quality", True, score=0.8)

        self.assertEqual(str(assertion.score), "0.8")
        with self.assertRaises(KernelValidationError):
            OutcomeAssertion("quality", True, score="1.01")

    def test_assertion_names_cannot_repeat(self) -> None:
        with self.assertRaises(KernelValidationError):
            OutcomeRecord(
                target=OutcomeTarget(
                    OutcomeTargetKind.TRACE,
                    "trace-1",
                ),
                task_id="task",
                evaluator_kind=EvaluatorKind.HUMAN,
                evaluator_name="reviewer",
                evaluator_version="1",
                assertions=(
                    OutcomeAssertion("correct", True),
                    OutcomeAssertion("correct", True),
                ),
                succeeded=True,
                idempotency_key="review:task",
            )


class InMemoryOutcomeStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_equivalent_retry_returns_original_record(self) -> None:
        store = InMemoryOutcomeStore()
        first = passing_outcome(outcome_id="outcome-original")
        retried = passing_outcome(outcome_id="outcome-retry")

        recorded = await store.record(first)
        repeated = await store.record(retried)

        self.assertIs(recorded, first)
        self.assertIs(repeated, first)
        self.assertEqual(
            await store.get("outcome-original"),
            first,
        )
        self.assertEqual(
            await store.list_for(first.target),
            (first,),
        )

    async def test_idempotency_key_rejects_changed_verdict(self) -> None:
        store = InMemoryOutcomeStore()
        first = passing_outcome()
        await store.record(first)
        conflicting = OutcomeRecord(
            target=first.target,
            task_id=first.task_id,
            evaluator_kind=first.evaluator_kind,
            evaluator_name=first.evaluator_name,
            evaluator_version=first.evaluator_version,
            assertions=(OutcomeAssertion("failed", False),),
            succeeded=False,
            idempotency_key=first.idempotency_key,
        )

        with self.assertRaises(OutcomeConflictError):
            await store.record(conflicting)

    async def test_trace_and_resolution_targets_are_independent(self) -> None:
        store = InMemoryOutcomeStore()
        resolution = passing_outcome()
        trace = passing_outcome(
            target=OutcomeTarget(
                OutcomeTargetKind.TRACE,
                "trace-1",
            ),
            idempotency_key="run-1:trace-task",
        )

        await store.record(resolution)
        await store.record(trace)

        self.assertEqual(
            await store.list_for(resolution.target),
            (resolution,),
        )
        self.assertEqual(
            await store.list_for(trace.target),
            (trace,),
        )

    async def test_missing_outcome_is_explicit(self) -> None:
        with self.assertRaises(OutcomeNotFoundError):
            await InMemoryOutcomeStore().get("missing")


class JsonlOutcomeStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_survive_reconstruction_without_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = JsonlOutcomeStore(path)
            first = passing_outcome(outcome_id="persisted-outcome")
            await store.record(first)
            repeated = await store.record(
                passing_outcome(outcome_id="retry-outcome")
            )
            reconstructed = JsonlOutcomeStore(path)
            loaded = await reconstructed.get(first.outcome_id)
            listed = await reconstructed.list_for(first.target)
            lines = (path / "outcomes.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()

        self.assertEqual(repeated, first)
        self.assertEqual(loaded, first)
        self.assertEqual(listed, (first,))
        self.assertEqual(len(lines), 1)
        self.assertNotIn("result_payload", lines[0])
        self.assertNotIn("rationale", lines[0])


class EngineOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_outcome_requires_explicit_store(self) -> None:
        engine = Engine(SourceCatalog())

        with self.assertRaises(UnsupportedOutcomeError):
            await engine.record_outcome(passing_outcome())

    async def test_engine_records_without_requiring_local_target(self) -> None:
        store = InMemoryOutcomeStore()
        engine = Engine(SourceCatalog(), outcome_store=store)
        outcome = passing_outcome()

        recorded = await engine.record_outcome(outcome)

        self.assertIs(recorded, outcome)
        self.assertEqual(
            await store.get(outcome.outcome_id),
            outcome,
        )


if __name__ == "__main__":
    unittest.main()
