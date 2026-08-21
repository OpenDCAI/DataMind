"""Shared metadata-filter semantics for every retrieval branch."""
from __future__ import annotations

from typing import Any

from datamind.core.errors import ConfigError

_COMPARATORS = {"$eq", "$ne", "$in", "$nin", "$gt", "$gte", "$lt", "$lte"}


def validate_metadata_filter(where: dict[str, Any] | None) -> None:
    if where is None:
        return
    if not isinstance(where, dict):
        raise ConfigError("metadata filter must be an object")
    for key, expected in where.items():
        if key in {"$and", "$or"}:
            if not isinstance(expected, list) or not expected:
                raise ConfigError(f"{key} requires a non-empty array of filters")
            for child in expected:
                validate_metadata_filter(child)
            continue
        if key.startswith("$"):
            raise ConfigError(f"unsupported metadata filter operator: {key}")
        if isinstance(expected, dict):
            unknown = set(expected) - _COMPARATORS
            if unknown:
                raise ConfigError(
                    "unsupported metadata filter operator(s): " + ", ".join(sorted(unknown))
                )
            if len(expected) != 1:
                raise ConfigError(f"metadata field {key!r} must use exactly one comparator")


def matches_metadata(metadata: dict[str, Any], where: dict[str, Any] | None) -> bool:
    validate_metadata_filter(where)
    if not where:
        return True
    for key, expected in where.items():
        if key == "$and":
            if not all(matches_metadata(metadata, child) for child in expected):
                return False
            continue
        if key == "$or":
            if not any(matches_metadata(metadata, child) for child in expected):
                return False
            continue
        actual = metadata.get(key)
        if not isinstance(expected, dict):
            if actual != expected:
                return False
            continue
        op, value = next(iter(expected.items()))
        try:
            ok = {
                "$eq": actual == value,
                "$ne": actual != value,
                "$in": actual in value,
                "$nin": actual not in value,
                "$gt": actual > value,
                "$gte": actual >= value,
                "$lt": actual < value,
                "$lte": actual <= value,
            }[op]
        except (TypeError, KeyError):
            ok = False
        if not ok:
            return False
    return True


__all__ = ["matches_metadata", "validate_metadata_filter"]
