"""Explicit, versioned JSON codec for the initial DataOps instruction set."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from datamind.kernel import (
    Budget,
    EffectLevel,
    MemoryKind,
    ScopeKind,
    ScopeRef,
    SerializationError,
    SourceKind,
    SourceRef,
    thaw_json,
)

from .base import OutputRef
from .operations import Compose, Describe, Discover, Query, Recall, Search
from .plan import DataPlan

DATA_PLAN_SCHEMA = "datamind.data_plan"
DATA_PLAN_VERSION = "1"


def _source_to_dict(source: SourceRef) -> dict:
    return {"source_id": source.source_id, "kind": source.kind.value}


def _source_from_dict(payload: Mapping[str, Any]) -> SourceRef:
    return SourceRef(
        source_id=str(payload["source_id"]),
        kind=SourceKind(str(payload["kind"])),
    )


def _output_to_dict(ref: OutputRef) -> dict:
    return {"op_id": ref.op_id, "path": list(ref.path)}


def _output_from_dict(payload: Mapping[str, Any]) -> OutputRef:
    return OutputRef(
        op_id=str(payload["op_id"]),
        path=tuple(payload.get("path", ())),
    )


def _scope_to_dict(scope: ScopeRef) -> dict:
    return {"kind": scope.kind.value, "scope_id": scope.scope_id}


def _scope_from_dict(payload: Mapping[str, Any]) -> ScopeRef:
    return ScopeRef(
        kind=ScopeKind(str(payload["kind"])),
        scope_id=str(payload["scope_id"]),
    )


def _datetime_to_json(value: Any) -> Any:
    return value.isoformat() if value is not None else None


def _datetime_from_json(value: Any) -> Any:
    return datetime.fromisoformat(str(value)) if value is not None else None


def operation_to_dict(op: Any) -> dict:
    common = {"type": op.operation, "op_id": op.op_id}
    if isinstance(op, Discover):
        common["kinds"] = [kind.value for kind in op.kinds]
    elif isinstance(op, Describe):
        common["source"] = _source_to_dict(op.source)
    elif isinstance(op, Search):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "query": op.query,
                "limit": op.limit,
                "filters": thaw_json(op.filters),
            }
        )
    elif isinstance(op, Query):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "statement": op.statement,
                "language": op.language,
                "parameters": thaw_json(op.parameters),
            }
        )
    elif isinstance(op, Recall):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "query": op.query,
                "scopes": [
                    _scope_to_dict(scope) for scope in op.scopes
                ],
                "kinds": [kind.value for kind in op.kinds],
                "valid_at": _datetime_to_json(op.valid_at),
                "known_at": _datetime_to_json(op.known_at),
                "limit": op.limit,
            }
        )
    elif isinstance(op, Compose):
        common.update(
            {
                "inputs": [_output_to_dict(ref) for ref in op.inputs],
                "strategy": op.strategy,
            }
        )
    else:
        raise SerializationError(
            "unsupported operation type {!r}".format(type(op).__name__)
        )
    return common


def operation_from_dict(payload: Mapping[str, Any]) -> Any:
    try:
        op_type = str(payload["type"])
        op_id = str(payload["op_id"])
        if op_type == "discover":
            return Discover(
                op_id=op_id,
                kinds=tuple(
                    SourceKind(str(kind)) for kind in payload.get("kinds", ())
                ),
            )
        if op_type == "describe":
            return Describe(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
            )
        if op_type == "search":
            return Search(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                query=str(payload["query"]),
                limit=int(payload.get("limit", 10)),
                filters=payload.get("filters", {}),
            )
        if op_type == "query":
            return Query(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                statement=str(payload["statement"]),
                language=str(payload.get("language", "sql")),
                parameters=payload.get("parameters", {}),
            )
        if op_type == "recall":
            return Recall(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                query=str(payload["query"]),
                scopes=tuple(
                    _scope_from_dict(item)
                    for item in payload.get("scopes", ())
                ),
                kinds=tuple(
                    MemoryKind(str(item))
                    for item in payload.get("kinds", ())
                ),
                valid_at=_datetime_from_json(payload.get("valid_at")),
                known_at=_datetime_from_json(payload.get("known_at")),
                limit=int(payload.get("limit", 10)),
            )
        if op_type == "compose":
            return Compose(
                op_id=op_id,
                inputs=tuple(
                    _output_from_dict(item)
                    for item in payload.get("inputs", ())
                ),
                strategy=str(payload.get("strategy", "evidence_union")),
            )
        raise SerializationError(
            "unknown operation type {!r}".format(op_type)
        )
    except SerializationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError(
            "invalid operation payload: {}".format(exc)
        ) from exc


def _budget_to_dict(budget: Budget) -> dict:
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


def _budget_from_dict(payload: Mapping[str, Any]) -> Budget:
    cost = payload.get("max_cost_usd")
    return Budget(
        max_tokens=payload.get("max_tokens"),
        max_latency_ms=payload.get("max_latency_ms"),
        max_cost_usd=Decimal(str(cost)) if cost is not None else None,
        max_actions=payload.get("max_actions"),
    )


def plan_to_dict(plan: DataPlan) -> dict:
    return {
        "schema": DATA_PLAN_SCHEMA,
        "version": plan.version,
        "plan_id": plan.plan_id,
        "description": plan.description,
        "max_effect": plan.max_effect.name,
        "budget": _budget_to_dict(plan.budget),
        "operations": [operation_to_dict(op) for op in plan.operations],
        "output": _output_to_dict(plan.output),
    }


def plan_from_dict(payload: Mapping[str, Any]) -> DataPlan:
    try:
        if payload.get("schema") != DATA_PLAN_SCHEMA:
            raise SerializationError("unsupported plan schema")
        version = str(payload["version"])
        if version != DATA_PLAN_VERSION:
            raise SerializationError(
                "unsupported data plan version {!r}".format(version)
            )
        return DataPlan(
            plan_id=str(payload["plan_id"]),
            version=version,
            description=payload.get("description"),
            max_effect=EffectLevel[str(payload.get("max_effect", "READ"))],
            budget=_budget_from_dict(payload.get("budget", {})),
            operations=tuple(
                operation_from_dict(item)
                for item in payload["operations"]
            ),
            output=_output_from_dict(payload["output"]),
        )
    except SerializationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError(
            "invalid data plan payload: {}".format(exc)
        ) from exc


def plan_to_json(plan: DataPlan, *, indent: Any = None) -> str:
    return json.dumps(
        plan_to_dict(plan),
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
    )


def plan_from_json(raw: str) -> DataPlan:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SerializationError("invalid data plan JSON: {}".format(exc)) from exc
    if not isinstance(payload, Mapping):
        raise SerializationError("data plan JSON must be an object")
    return plan_from_dict(payload)
