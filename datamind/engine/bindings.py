"""Resolve the intentionally small runtime ValueBinding surface."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from datamind.dataops import (
    BindingCardinality,
    InvokeSkill,
    ResultEnvelope,
    Traverse,
    ValueBinding,
)
from datamind.kernel import ExecutionError, thaw_json


def resolve_bound_operation(
    operation: Any,
    *,
    prior_results: Mapping[str, ResultEnvelope[Any]],
) -> Any:
    """Return a literal-only source operation ready for an adapter."""

    if isinstance(operation, Traverse) and operation.start_binding is not None:
        resolved = resolve_value_binding(
            operation.start_binding,
            prior_results=prior_results,
        )
        starts = resolved if isinstance(resolved, tuple) else (resolved,)
        if not starts:
            raise ExecutionError(
                "traverse start binding resolved to no values"
            )
        if any(not isinstance(item, str) or not item.strip() for item in starts):
            raise ExecutionError(
                "traverse start binding must resolve to non-empty strings"
            )
        return replace(
            operation,
            starts=tuple(starts),
            start_binding=None,
        )

    if isinstance(operation, InvokeSkill) and operation.argument_bindings:
        arguments = thaw_json(operation.arguments)
        for item in operation.argument_bindings:
            arguments[item.argument] = resolve_value_binding(
                item.value,
                prior_results=prior_results,
            )
        return replace(
            operation,
            arguments=arguments,
            argument_bindings=(),
        )

    return operation


def resolve_value_binding(
    binding: ValueBinding,
    *,
    prior_results: Mapping[str, ResultEnvelope[Any]],
) -> Any:
    upstream = prior_results.get(binding.ref.op_id)
    if upstream is None:
        raise ExecutionError(
            "value binding input {!r} has not been executed".format(
                binding.ref.op_id
            )
        )
    if binding.field not in upstream.bindings.fields:
        raise ExecutionError(
            "value binding field {!r} does not exist in {!r}".format(
                binding.field,
                binding.ref.op_id,
            )
        )
    values = tuple(
        row.values[binding.field] for row in upstream.bindings.rows
    )
    if binding.cardinality is BindingCardinality.SINGLE:
        if len(values) != 1:
            raise ExecutionError(
                "single value binding requires exactly one row, received "
                "{}".format(len(values))
            )
        return values[0]

    unique = []
    seen = set()
    for value in values:
        if not (
            value is None
            or isinstance(value, (bool, int, float, str))
        ):
            raise ExecutionError(
                "collect value bindings support only JSON scalar fields"
            )
        key = (type(value).__name__, value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
        if len(unique) > binding.max_items:
            raise ExecutionError(
                "collect value binding exceeded max_items={}".format(
                    binding.max_items
                )
            )
    return tuple(unique)


__all__ = [
    "resolve_bound_operation",
    "resolve_value_binding",
]
