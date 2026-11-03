"""Metadata comparator dispatch tests."""
from __future__ import annotations

import pytest

from datamind.capabilities.kb.filters import matches_metadata


@pytest.mark.parametrize(
    ("actual", "operator", "expected", "matched"),
    [
        (42, "$eq", 42, True),
        (42, "$eq", 41, False),
        (42, "$ne", 41, True),
        (42, "$ne", 42, False),
        ("a", "$in", ["a", "b"], True),
        ("c", "$in", ["a", "b"], False),
        ("c", "$nin", ["a", "b"], True),
        ("a", "$nin", ["a", "b"], False),
        (42, "$gt", 40, True),
        (42, "$gt", 42, False),
        (42, "$gte", 42, True),
        (42, "$gte", 43, False),
        (40, "$lt", 42, True),
        (42, "$lt", 42, False),
        (42, "$lte", 42, True),
        (43, "$lte", 42, False),
    ],
)
def test_matches_metadata_dispatches_only_the_requested_comparator(
    actual, operator, expected, matched,
):
    assert matches_metadata({"value": actual}, {"value": {operator: expected}}) is matched


def test_nested_boolean_filters_keep_comparator_dispatch_local():
    assert matches_metadata(
        {"age": 42, "tag": "a"},
        {"$and": [
            {"age": {"$gt": 40}},
            {"tag": {"$in": ["a", "b"]}},
        ]},
    )
