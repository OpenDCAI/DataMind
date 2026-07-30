"""Portable strict schema for the model-facing DataPlan draft language.

The draft deliberately omits authority-bearing fields. Source kinds, plan
identity, budgets, effects, Skill policy, and Memory idempotency are bound by
the deterministic compiler after generation.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from datamind.kernel import (
    MemoryKind,
    MemoryLinkKind,
    ScopeKind,
    SourceKind,
)

from .operations import ComparisonOperator, GraphDirection


RESOLVABLE_OPERATION_TYPES: Tuple[str, ...] = (
    "discover",
    "describe",
    "search",
    "query",
    "traverse",
    "recall",
    "resolve_skill",
    "invoke_skill",
    "propose_mutation",
    "project",
    "filter",
    "join",
    "fuse",
    "compose",
)


def _object(
    properties: Dict[str, Any],
    *,
    required: Iterable[str] = (),
) -> dict:
    required_values = list(required) or list(properties)
    return {
        "type": "object",
        "properties": properties,
        "required": required_values,
        "additionalProperties": False,
    }


def _array(items: dict) -> dict:
    return {"type": "array", "items": items}


def _enum(values: Iterable[str]) -> dict:
    return {"type": "string", "enum": list(values)}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _source_id() -> dict:
    return {
        "type": "string",
        "description": "Exact authorized source_id from the catalog.",
    }


def _output_ref() -> dict:
    return _object(
        {
            "op_id": {"type": "string"},
            "path": _array(
                {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "integer"},
                    ]
                }
            ),
        }
    )


def _scope_ref() -> dict:
    return _object(
        {
            "kind": _enum(item.value for item in ScopeKind),
            "scope_id": {"type": "string"},
        }
    )


def _value_binding() -> dict:
    return _object(
        {
            "ref": _output_ref(),
            "field": {"type": "string"},
            "cardinality": _enum(("single", "many")),
            "max_items": {"type": "integer", "minimum": 1},
        }
    )


def _argument_binding() -> dict:
    return _object(
        {
            "argument": {"type": "string"},
            "value": _value_binding(),
        }
    )


def _discover() -> dict:
    return _object(
        {
            "type": _enum(("discover",)),
            "op_id": {"type": "string"},
            "kinds": _array(_enum(item.value for item in SourceKind)),
        }
    )


def _describe() -> dict:
    return _object(
        {
            "type": _enum(("describe",)),
            "op_id": {"type": "string"},
            "source": _source_id(),
        }
    )


def _search() -> dict:
    return _object(
        {
            "type": _enum(("search",)),
            "op_id": {"type": "string"},
            "source": _source_id(),
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
            "filters_json": {
                "type": "string",
                "description": "A JSON object encoded as a string.",
            },
        }
    )


def _query() -> dict:
    return _object(
        {
            "type": _enum(("query",)),
            "op_id": {"type": "string"},
            "source": _source_id(),
            "statement": {"type": "string"},
            "language": {"type": "string"},
            "parameters_json": {
                "type": "string",
                "description": "A JSON object encoded as a string.",
            },
        }
    )


def _traverse() -> dict:
    return _object(
        {
            "type": _enum(("traverse",)),
            "op_id": {"type": "string"},
            "source": _source_id(),
            "starts": _array({"type": "string"}),
            "start_binding": _nullable(_value_binding()),
            "direction": _enum(item.value for item in GraphDirection),
            "relations": _array({"type": "string"}),
            "min_hops": {"type": "integer", "minimum": 1},
            "max_hops": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1},
            "simple_paths": {"type": "boolean", "enum": [True]},
        }
    )


def _recall() -> dict:
    return _object(
        {
            "type": _enum(("recall",)),
            "op_id": {"type": "string"},
            "source": _source_id(),
            "query": {"type": "string"},
            "scopes": _array(_scope_ref()),
            "kinds": _array(_enum(item.value for item in MemoryKind)),
            "valid_at": _nullable({"type": "string"}),
            "known_at": _nullable({"type": "string"}),
            "limit": {"type": "integer", "minimum": 1},
        }
    )


def _resolve_skill() -> dict:
    return _object(
        {
            "type": _enum(("resolve_skill",)),
            "op_id": {"type": "string"},
            "source": _source_id(),
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
        }
    )


def _invoke_skill() -> dict:
    return _object(
        {
            "type": _enum(("invoke_skill",)),
            "op_id": {"type": "string"},
            "source": _source_id(),
            "skill": _object(
                {
                    "name": {"type": "string"},
                    "version": {"type": "string"},
                    "digest": {"type": "string"},
                }
            ),
            "arguments_json": {
                "type": "string",
                "description": "A JSON object encoded as a string.",
            },
            "argument_bindings": _array(_argument_binding()),
        }
    )


def _memory_link() -> dict:
    return _object(
        {
            "kind": _enum(
                item.value
                for item in MemoryLinkKind
                if item is not MemoryLinkKind.SUPERSEDES
            ),
            "target_id": {"type": "string"},
        }
    )


def _assert_memory() -> dict:
    return _object(
        {
            "action": _enum(("assert",)),
            "kind": _enum(item.value for item in MemoryKind),
            "content": {"type": "string"},
            "valid_from": _nullable({"type": "string"}),
            "valid_to": _nullable({"type": "string"}),
            "links": _array(_memory_link()),
            "metadata_json": {
                "type": "string",
                "description": "A JSON object encoded as a string.",
            },
        }
    )


def _supersede_memory() -> dict:
    return _object(
        {
            "action": _enum(("supersede",)),
            "target_id": {"type": "string"},
            "content": {"type": "string"},
            "valid_from": _nullable({"type": "string"}),
            "valid_to": _nullable({"type": "string"}),
            "links": _array(_memory_link()),
            "metadata_json": {
                "type": "string",
                "description": "A JSON object encoded as a string.",
            },
        }
    )


def _retract_memory() -> dict:
    return _object(
        {
            "action": _enum(("retract",)),
            "target_id": {"type": "string"},
            "reason": {"type": "string"},
        }
    )


def _propose_mutation() -> dict:
    return _object(
        {
            "type": _enum(("propose_mutation",)),
            "op_id": {"type": "string"},
            "source": _source_id(),
            "draft": _object(
                {
                    "scope": _scope_ref(),
                    "changes": _array(
                        {
                            "anyOf": [
                                _assert_memory(),
                                _supersede_memory(),
                                _retract_memory(),
                            ]
                        }
                    ),
                }
            ),
        }
    )


def _project() -> dict:
    return _object(
        {
            "type": _enum(("project",)),
            "op_id": {"type": "string"},
            "inputs": _array(_output_ref()),
            "fields": _array({"type": "string"}),
        }
    )


def _filter() -> dict:
    return _object(
        {
            "type": _enum(("filter",)),
            "op_id": {"type": "string"},
            "inputs": _array(_output_ref()),
            "predicate": _object(
                {
                    "field": {"type": "string"},
                    "operator": _enum(
                        item.value for item in ComparisonOperator
                    ),
                    "value_json": {
                        "type": "string",
                        "description": "Any JSON value encoded as a string.",
                    },
                }
            ),
        }
    )


def _join() -> dict:
    return _object(
        {
            "type": _enum(("join",)),
            "op_id": {"type": "string"},
            "inputs": _array(_output_ref()),
            "left_on": _array({"type": "string"}),
            "right_on": _array({"type": "string"}),
            "left_alias": {"type": "string"},
            "right_alias": {"type": "string"},
        }
    )


def _fuse() -> dict:
    return _object(
        {
            "type": _enum(("fuse",)),
            "op_id": {"type": "string"},
            "inputs": _array(_output_ref()),
            "strategy": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
            "rank_constant": {"type": "integer", "minimum": 1},
        }
    )


def _compose() -> dict:
    return _object(
        {
            "type": _enum(("compose",)),
            "op_id": {"type": "string"},
            "inputs": _array(_output_ref()),
            "strategy": {"type": "string"},
        }
    )


_OPERATION_SCHEMAS = {
    "discover": _discover,
    "describe": _describe,
    "search": _search,
    "query": _query,
    "traverse": _traverse,
    "recall": _recall,
    "resolve_skill": _resolve_skill,
    "invoke_skill": _invoke_skill,
    "propose_mutation": _propose_mutation,
    "project": _project,
    "filter": _filter,
    "join": _join,
    "fuse": _fuse,
    "compose": _compose,
}


def data_plan_draft_schema(
    *,
    allowed_operations: Iterable[str] = RESOLVABLE_OPERATION_TYPES,
) -> dict:
    """Build the strict model schema for an authorized operation subset."""

    selected = tuple(dict.fromkeys(allowed_operations))
    unknown = sorted(set(selected) - set(_OPERATION_SCHEMAS))
    if unknown:
        raise ValueError(
            "unsupported draft operations: {}".format(", ".join(unknown))
        )
    if not selected:
        raise ValueError("at least one draft operation must be allowed")
    return _object(
        {
            "description": _nullable({"type": "string"}),
            "operations": _array(
                {
                    "anyOf": [
                        _OPERATION_SCHEMAS[name]()
                        for name in selected
                    ]
                }
            ),
            "output": _output_ref(),
        }
    )


__all__ = [
    "RESOLVABLE_OPERATION_TYPES",
    "data_plan_draft_schema",
]
