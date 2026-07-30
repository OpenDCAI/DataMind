"""Small deterministic validator for DataMind's declared object subset."""
from __future__ import annotations

from typing import Any, Mapping, Tuple


def json_object_violations(
    value: Any,
    schema: Mapping[str, Any],
    *,
    label: str = "value",
) -> Tuple[str, ...]:
    """Validate required fields, closed objects, and declared JSON types."""

    if not isinstance(value, Mapping):
        return ("{} must be a JSON object".format(label),)
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return ("{} schema properties must be an object".format(label),)

    violations = []
    required = tuple(schema.get("required", ()))
    missing = sorted(set(required) - set(value))
    if missing:
        violations.append(
            "{} is missing required fields: {}".format(label, missing)
        )
    if schema.get("additionalProperties") is False:
        extra = sorted(set(value) - set(properties))
        if extra:
            violations.append(
                "{} contains undeclared fields: {}".format(label, extra)
            )
    for name, item in value.items():
        item_schema = properties.get(name)
        if not isinstance(item_schema, Mapping):
            continue
        expected = item_schema.get("type")
        if expected is None or _matches_json_type(item, expected):
            continue
        expected_types = (
            tuple(expected)
            if isinstance(expected, (list, tuple))
            else (expected,)
        )
        violations.append(
            "{}.{} does not match declared type {}".format(
                label,
                name,
                expected_types,
            )
        )
    return tuple(violations)


def _matches_json_type(value: Any, expected: Any) -> bool:
    expected_types = (
        tuple(expected)
        if isinstance(expected, (list, tuple))
        else (expected,)
    )
    checks = {
        "null": lambda item: item is None,
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: (
            isinstance(item, int) and not isinstance(item, bool)
        ),
        "number": lambda item: (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
        ),
        "string": lambda item: isinstance(item, str),
        "array": lambda item: isinstance(item, (list, tuple)),
        "object": lambda item: isinstance(item, Mapping),
    }
    return any(
        name in checks and checks[name](value)
        for name in expected_types
    )


__all__ = ["json_object_violations"]
