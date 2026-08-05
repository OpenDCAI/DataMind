"""Pure, content-safe projections over completed benchmark runs."""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Tuple

from datamind.dataops import DataPlan, ResultEnvelope
from datamind.kernel import (
    Budget,
    ExecutionTrace,
    ResolutionTrace,
    Usage,
    thaw_json,
)

from .runner import BenchmarkRun
from .schema import TaskSpec


def task_catalog(tasks: Iterable[TaskSpec]) -> dict:
    """Return the deterministic task surface without constructing engines."""

    return {
        "schema": "datamind.benchmark_catalog",
        "version": "0.1",
        "tasks": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "layer": task.layer.value,
                "supported_modes": [
                    mode.value for mode in task.supported_modes
                ],
            }
            for task in tasks
        ],
    }


def diagnostic_report(
    run: BenchmarkRun,
    *,
    show_plan: bool = False,
    show_trace: bool = False,
    show_result: bool = False,
) -> dict:
    """Project already captured facts; never execute an Engine or provider."""

    report = {
        "run_id": run.run_id,
        "task_id": run.task_id,
        "title": run.observation.task.title,
        "layer": run.observation.task.layer.value,
        "mode": run.mode.value,
        "succeeded": run.succeeded,
        "assertions": [
            {
                "name": assertion.name,
                "passed": assertion.passed,
                "score": (
                    str(assertion.score)
                    if assertion.score is not None
                    else None
                ),
            }
            for assertion in run.outcome.assertions
        ],
    }
    observation = run.observation
    if show_plan:
        report["plans"] = [
            _plan_summary(plan, attempt=index)
            for index, plan in enumerate(observation.plans, start=1)
        ]
    if show_trace:
        captured_ids = {
            trace.trace_id for trace in observation.execution_traces
        }
        report["traces"] = {
            "resolution": _resolution_trace_summary(
                observation.resolution_trace
            ),
            "executions": [
                _execution_trace_summary(trace)
                for trace in observation.execution_traces
            ],
            "missing_trace_ids": [
                trace_id
                for trace_id in observation.trace_ids
                if trace_id not in captured_ids
            ],
        }
    if show_result:
        report["result"] = _result_summary(observation.result)
        report["error"] = _error_summary(observation.error)
        report["replay"] = {
            "result": _result_summary(observation.replayed),
            "error": _error_summary(observation.replay_error),
        }
    return report


def _plan_summary(plan: DataPlan, *, attempt: int) -> dict:
    return {
        "attempt": attempt,
        "plan_id": plan.plan_id,
        "version": plan.version,
        "output": _output_ref_summary(plan.output),
        "max_effect": plan.max_effect.name,
        "budget": _budget_summary(plan.budget),
        "operations": [
            _operation_summary(operation)
            for operation in plan.operations
        ],
    }


def _operation_summary(operation: Any) -> dict:
    source = operation.source
    effect = operation.effect
    return {
        "op_id": operation.op_id,
        "operation": operation.operation,
        "output_kind": operation.output_kind.value,
        "inputs": [
            _output_ref_summary(ref) for ref in operation.inputs
        ],
        "source": (
            {
                "source_id": source.source_id,
                "kind": source.kind.value,
            }
            if source is not None
            else None
        ),
        "effect": {
            "level": effect.level.name,
            "reversible": effect.reversible,
            "requires_approval": effect.requires_approval,
            "has_idempotency_key": effect.idempotency_key is not None,
        },
    }


def _output_ref_summary(ref: Any) -> dict:
    return {
        "op_id": ref.op_id,
        "path": list(ref.path),
    }


def _execution_trace_summary(trace: ExecutionTrace) -> dict:
    return {
        "trace_id": trace.trace_id,
        "completed": trace.completed,
        "failed": trace.failed,
        "events": [
            {
                "sequence": event.sequence,
                "kind": event.kind.value,
                "op_id": event.op_id,
                "details": thaw_json(event.details),
            }
            for event in trace.events
        ],
    }


def _resolution_trace_summary(
    trace: Optional[ResolutionTrace],
) -> Optional[dict]:
    if trace is None:
        return None
    return {
        "resolution_id": trace.resolution_id,
        "completed": trace.completed,
        "failed": trace.failed,
        "events": [
            {
                "sequence": event.sequence,
                "kind": event.kind.value,
                "attempt_number": event.attempt_number,
                "trace_id": event.trace_id,
                "details": thaw_json(event.details),
            }
            for event in trace.events
        ],
    }


def _result_summary(
    result: Optional[ResultEnvelope[Any]],
) -> Optional[dict]:
    if result is None:
        return None
    return {
        "op_id": result.op_id,
        "trace_id": result.trace_id,
        "result_kind": result.result_kind.value,
        "native_type": type(result.value).__name__,
        "native_shape": _native_shape(result.value),
        "status": result.status.value,
        "bindings": {
            "fields": list(result.bindings.fields),
            "row_count": len(result.bindings.rows),
        },
        "evidence_count": len(result.evidence),
        "provenance_sources": [
            {
                "source_id": item.source.source_id,
                "kind": item.source.kind.value,
            }
            for item in result.provenance
        ],
        "snapshots": [
            {
                "source_id": item.source.source_id,
                "kind": item.source.kind.value,
                "version": item.version,
                "checksum": item.checksum,
            }
            for item in result.snapshots
        ],
        "usage": _usage_summary(result.usage),
        "warnings": list(result.warnings),
    }


def _native_shape(value: Any) -> dict:
    """Describe a native value without reading or copying its content."""

    shape = {"python_type": type(value).__name__}
    if isinstance(value, (tuple, list)):
        shape["item_count"] = len(value)
        shape["item_types"] = sorted(
            {type(item).__name__ for item in value}
        )
    elif isinstance(value, Mapping):
        shape["item_count"] = len(value)
    return shape


def _error_summary(error: Optional[Exception]) -> Optional[dict]:
    if error is None:
        return None
    return {"type": type(error).__name__}


def _budget_summary(budget: Budget) -> dict:
    return {
        "max_tokens": budget.max_tokens,
        "max_latency_ms": budget.max_latency_ms,
        "max_cost_usd": (
            str(budget.max_cost_usd)
            if budget.max_cost_usd is not None
            else None
        ),
        "max_actions": budget.max_actions,
    }


def _usage_summary(usage: Usage) -> dict:
    return {
        "tokens": usage.tokens,
        "latency_ms": usage.latency_ms,
        "cost_usd": str(usage.cost_usd),
        "actions": usage.actions,
    }


__all__ = ["diagnostic_report", "task_catalog"]
