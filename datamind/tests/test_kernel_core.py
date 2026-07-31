"""Contract tests for the dependency-free DataMind kernel."""
from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal

from datamind.kernel import (
    Budget,
    BudgetExceeded,
    EffectLevel,
    EffectPolicyError,
    EffectSpec,
    ExecutionFailure,
    ExecutionFailureKind,
    KernelValidationError,
    ResolutionEvent,
    ResolutionEventKind,
    ResolutionTrace,
    SnapshotRef,
    SourceKind,
    SourceRef,
    Usage,
    effect_violations,
    freeze_json,
    require_effect_allowed,
    thaw_json,
)


class JsonValueTests(unittest.TestCase):
    def test_freeze_json_copies_and_recursively_freezes_input(self) -> None:
        original = {"tags": ["finance"], "nested": {"active": True}}

        frozen = freeze_json(original)
        original["tags"].append("changed")
        original["nested"]["active"] = False

        self.assertEqual(
            thaw_json(frozen),
            {"tags": ["finance"], "nested": {"active": True}},
        )
        with self.assertRaises(TypeError):
            frozen["new"] = "value"

    def test_freeze_json_rejects_non_json_values(self) -> None:
        with self.assertRaises(KernelValidationError):
            freeze_json({"bad": {1, 2}})


class IdentityTests(unittest.TestCase):
    def test_snapshot_requires_timezone_aware_timestamp(self) -> None:
        source = SourceRef("contracts", SourceKind.DOCUMENT)

        with self.assertRaises(KernelValidationError):
            SnapshotRef(
                source=source,
                version="v1",
                observed_at=datetime(2026, 1, 1),
            )

    def test_source_ref_is_immutable(self) -> None:
        source = SourceRef("warehouse", SourceKind.TABLE)

        with self.assertRaises(FrozenInstanceError):
            source.source_id = "other"

    def test_source_kind_is_not_implicitly_coerced(self) -> None:
        with self.assertRaises(KernelValidationError):
            SourceRef("warehouse", "table")


class BudgetTests(unittest.TestCase):
    def test_usage_is_decimal_backed_and_additive(self) -> None:
        total = Usage(tokens=3, cost_usd=0.1, actions=1) + Usage(
            tokens=4,
            cost_usd="0.2",
            actions=2,
        )

        self.assertEqual(total.tokens, 7)
        self.assertEqual(total.actions, 3)
        self.assertEqual(total.cost_usd, Decimal("0.3"))

    def test_budget_reports_all_violations_and_can_fail_closed(self) -> None:
        budget = Budget(max_tokens=10, max_cost_usd="0.5", max_actions=1)
        usage = Usage(tokens=11, cost_usd="0.6", actions=2)

        self.assertEqual(len(budget.violations(usage)), 3)
        with self.assertRaises(BudgetExceeded):
            budget.require(usage)

    def test_budget_rejects_non_finite_cost(self) -> None:
        with self.assertRaises(KernelValidationError):
            Budget(max_cost_usd="NaN")

    def test_remaining_budget_accounts_for_prior_stage_usage(self) -> None:
        budget = Budget(
            max_tokens=100,
            max_latency_ms=1000,
            max_cost_usd="2.50",
            max_actions=5,
        )

        remaining = budget.remaining(
            Usage(
                tokens=30,
                latency_ms=250,
                cost_usd="0.75",
                actions=2,
            )
        )

        self.assertEqual(remaining.max_tokens, 70)
        self.assertEqual(remaining.max_latency_ms, 750)
        self.assertEqual(remaining.max_cost_usd, Decimal("1.75"))
        self.assertEqual(remaining.max_actions, 3)


class EffectTests(unittest.TestCase):
    def test_external_write_requires_idempotency_key(self) -> None:
        source = SourceRef("crm", SourceKind.TABLE)
        effect = EffectSpec(
            level=EffectLevel.EXTERNAL_WRITE,
            resource=source,
        )

        self.assertIn(
            "external writes require an idempotency key",
            effect_violations(
                effect,
                max_level=EffectLevel.EXTERNAL_WRITE,
            ),
        )

    def test_destructive_effect_requires_declared_and_granted_approval(self) -> None:
        with self.assertRaises(KernelValidationError):
            EffectSpec(level=EffectLevel.DESTRUCTIVE)

        effect = EffectSpec(
            level=EffectLevel.DESTRUCTIVE,
            requires_approval=True,
            approval_key="delete_customer",
            idempotency_key="request-42",
        )
        with self.assertRaises(EffectPolicyError):
            require_effect_allowed(
                effect,
                max_level=EffectLevel.DESTRUCTIVE,
            )

        require_effect_allowed(
            effect,
            max_level=EffectLevel.DESTRUCTIVE,
            approvals=frozenset(("delete_customer",)),
        )

    def test_effect_policy_enforces_resource_scope(self) -> None:
        source = SourceRef("private-kb", SourceKind.DOCUMENT)
        effect = EffectSpec(level=EffectLevel.READ, resource=source)

        violations = effect_violations(
            effect,
            max_level=EffectLevel.READ,
            allowed_resources=frozenset(("public-kb",)),
        )

        self.assertEqual(
            violations,
            ("resource 'private-kb' is not allowed",),
        )


class FailureAndResolutionTraceTests(unittest.TestCase):
    def test_only_source_failure_can_be_marked_recoverable(self) -> None:
        with self.assertRaises(KernelValidationError):
            ExecutionFailure(
                kind=ExecutionFailureKind.BUDGET,
                error_type="BudgetExceeded",
                error_fingerprint="fingerprint",
                recoverable=True,
            )

    def test_resolution_cannot_terminate_with_open_plan_attempt(self) -> None:
        with self.assertRaises(KernelValidationError):
            ResolutionTrace(
                resolution_id="resolution",
                events=(
                    ResolutionEvent(
                        resolution_id="resolution",
                        sequence=0,
                        kind=(
                            ResolutionEventKind.RESOLUTION_STARTED
                        ),
                    ),
                    ResolutionEvent(
                        resolution_id="resolution",
                        sequence=1,
                        kind=(
                            ResolutionEventKind.PLAN_ATTEMPT_STARTED
                        ),
                        attempt_number=1,
                        trace_id="child",
                    ),
                    ResolutionEvent(
                        resolution_id="resolution",
                        sequence=2,
                        kind=(
                            ResolutionEventKind.RESOLUTION_FAILED
                        ),
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
