"""Stable SHA-256 fingerprints without persisting raw execution content."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Optional, Set


def fingerprint(value: Any) -> str:
    canonical = _canonical(value)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical(
    value: Any,
    active: Optional[Set[int]] = None,
) -> Any:
    if active is None:
        active = set()
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$float": repr(value)}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, Enum):
        return {
            "$enum": "{}.{}".format(
                type(value).__module__,
                type(value).__qualname__,
            ),
            "value": _canonical(value.value, active),
        }
    if isinstance(value, bytes):
        return {
            "$bytes_sha256": hashlib.sha256(value).hexdigest(),
            "length": len(value),
        }
    is_container = (
        (is_dataclass(value) and not isinstance(value, type))
        or isinstance(value, (Mapping, list, tuple, set, frozenset))
    )
    if is_container:
        identity = id(value)
        if identity in active:
            return {
                "$cycle": "{}.{}".format(
                    type(value).__module__,
                    type(value).__qualname__,
                )
            }
        active.add(identity)
        try:
            if is_dataclass(value) and not isinstance(value, type):
                return {
                    "$dataclass": "{}.{}".format(
                        type(value).__module__,
                        type(value).__qualname__,
                    ),
                    "fields": {
                        item.name: _canonical(
                            getattr(value, item.name),
                            active,
                        )
                        for item in fields(value)
                    },
                }
            if isinstance(value, Mapping):
                items = [
                    (
                        _canonical(key, active),
                        _canonical(item, active),
                    )
                    for key, item in value.items()
                ]
                items.sort(
                    key=lambda pair: json.dumps(
                        pair[0],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return {"$mapping": items}
            if isinstance(value, (list, tuple)):
                return {
                    "$sequence": [
                        _canonical(item, active) for item in value
                    ],
                    "type": type(value).__name__,
                }
            items = [_canonical(item, active) for item in value]
            items.sort(
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return {"$set": items}
        finally:
            active.remove(identity)
    representation = repr(value)
    return {
        "$opaque": "{}.{}".format(
            type(value).__module__,
            type(value).__qualname__,
        ),
        "repr_sha256": hashlib.sha256(
            representation.encode("utf-8")
        ).hexdigest(),
    }


__all__ = ["fingerprint"]
