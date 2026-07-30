"""Static DataPlan validation independent of adapters and execution."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

from datamind.kernel import (
    PlanValidationError,
    SourceDescriptor,
    Usage,
)

from .base import DataOp
from .plan import DataPlan


@dataclass(frozen=True)
class PlanValidationIssue:
    code: str
    message: str
    op_id: Optional[str] = None

    def render(self) -> str:
        if self.op_id:
            return "{} [{}]: {}".format(self.code, self.op_id, self.message)
        return "{}: {}".format(self.code, self.message)


@dataclass(frozen=True)
class PlanValidationReport:
    issues: Tuple[PlanValidationIssue, ...]
    topological_order: Tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.issues

    def require_valid(self) -> None:
        if self.issues:
            raise PlanValidationError(issue.render() for issue in self.issues)


def _topological_order(
    operations: Mapping[str, DataOp],
) -> Tuple[Tuple[str, ...], bool]:
    indegree: Dict[str, int] = {op_id: 0 for op_id in operations}
    children: Dict[str, list] = {op_id: [] for op_id in operations}
    for op_id, op in operations.items():
        for ref in op.inputs:
            if ref.op_id not in operations:
                continue
            indegree[op_id] += 1
            children[ref.op_id].append(op_id)

    ready = deque(
        op_id for op_id in operations if indegree[op_id] == 0
    )
    ordered = []
    while ready:
        current = ready.popleft()
        ordered.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return tuple(ordered), len(ordered) != len(operations)


def _output_ancestors(
    output_op_id: str,
    operations: Mapping[str, DataOp],
) -> set:
    ancestors = set()
    pending = [output_op_id]
    while pending:
        op_id = pending.pop()
        if op_id in ancestors or op_id not in operations:
            continue
        ancestors.add(op_id)
        pending.extend(ref.op_id for ref in operations[op_id].inputs)
    return ancestors


def validate_plan(
    plan: DataPlan,
    *,
    sources: Optional[Mapping[str, SourceDescriptor]] = None,
) -> PlanValidationReport:
    """Validate graph, type surface, capabilities, effects, and static budget."""

    issues = []
    counts = Counter(op.op_id for op in plan.operations)
    for op_id, count in counts.items():
        if count > 1:
            issues.append(
                PlanValidationIssue(
                    "duplicate_op_id",
                    "operation id appears {} times".format(count),
                    op_id,
                )
            )

    operations = {}
    for op in plan.operations:
        operations.setdefault(op.op_id, op)

    if plan.output.op_id not in operations:
        issues.append(
            PlanValidationIssue(
                "missing_plan_output",
                "output references unknown operation {!r}".format(
                    plan.output.op_id
                ),
            )
        )

    for op in plan.operations:
        if not op.signature.accepts_count(len(op.inputs)):
            issues.append(
                PlanValidationIssue(
                    "input_count_mismatch",
                    "operation expects {}..{} inputs, received {}".format(
                        op.signature.min_inputs,
                        (
                            "unbounded"
                            if op.signature.max_inputs is None
                            else op.signature.max_inputs
                        ),
                        len(op.inputs),
                    ),
                    op.op_id,
                )
            )
        for ref in op.inputs:
            if ref.op_id not in operations:
                issues.append(
                    PlanValidationIssue(
                        "missing_input",
                        "input references unknown operation {!r}".format(
                            ref.op_id
                        ),
                        op.op_id,
                    )
                )
            elif ref.op_id == op.op_id:
                issues.append(
                    PlanValidationIssue(
                        "self_reference",
                        "operation cannot consume its own output",
                        op.op_id,
                    )
                )
            else:
                upstream = operations[ref.op_id]
                if not op.signature.accepts_kind(upstream.output_kind):
                    issues.append(
                        PlanValidationIssue(
                            "incompatible_input_kind",
                            "{} cannot consume {} from {!r}".format(
                                op.operation,
                                upstream.output_kind.value,
                                upstream.op_id,
                            ),
                            op.op_id,
                        )
                    )
                if ref.path and not op.signature.allow_input_paths:
                    issues.append(
                        PlanValidationIssue(
                            "unsupported_input_path",
                            "{} requires the complete upstream result".format(
                                op.operation
                            ),
                            op.op_id,
                        )
                    )

        if op.effect.level > plan.max_effect:
            issues.append(
                PlanValidationIssue(
                    "effect_exceeds_plan",
                    "{} exceeds plan maximum {}".format(
                        op.effect.level.name, plan.max_effect.name
                    ),
                    op.op_id,
                )
            )

        if sources is not None and op.source is not None:
            descriptor = sources.get(op.source.source_id)
            if descriptor is None:
                issues.append(
                    PlanValidationIssue(
                        "unknown_source",
                        "source {!r} is not registered".format(
                            op.source.source_id
                        ),
                        op.op_id,
                    )
                )
            else:
                if descriptor.ref != op.source:
                    issues.append(
                        PlanValidationIssue(
                            "source_identity_mismatch",
                            "registered descriptor does not match operation source",
                            op.op_id,
                        )
                    )
                if (
                    op.operation != "describe"
                    and not descriptor.supports(op.operation)
                ):
                    issues.append(
                        PlanValidationIssue(
                            "unsupported_operation",
                            "source does not support {!r}".format(op.operation),
                            op.op_id,
                        )
                    )
                if op.effect.level > descriptor.max_effect:
                    issues.append(
                        PlanValidationIssue(
                            "effect_exceeds_source",
                            "{} exceeds source maximum {}".format(
                                op.effect.level.name,
                                descriptor.max_effect.name,
                            ),
                            op.op_id,
                        )
                    )

    static_usage = Usage(actions=len(plan.operations))
    for violation in plan.budget.violations(static_usage):
        issues.append(
            PlanValidationIssue("budget_exceeded", violation)
        )

    topo_order, has_cycle = _topological_order(operations)
    if has_cycle:
        issues.append(
            PlanValidationIssue(
                "cyclic_plan",
                "operation graph contains a cycle",
            )
        )
    else:
        ancestors = _output_ancestors(plan.output.op_id, operations)
        for op_id in operations:
            if op_id not in ancestors:
                issues.append(
                    PlanValidationIssue(
                        "unreachable_operation",
                        "operation does not contribute to the plan output",
                        op_id,
                    )
                )

    return PlanValidationReport(
        issues=tuple(issues),
        topological_order=topo_order,
    )


def require_valid_plan(
    plan: DataPlan,
    *,
    sources: Optional[Mapping[str, SourceDescriptor]] = None,
) -> DataPlan:
    report = validate_plan(plan, sources=sources)
    report.require_valid()
    return plan
