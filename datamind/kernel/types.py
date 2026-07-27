"""Dependency-free identity, source, snapshot, and JSON value types."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple, Union
from uuid import uuid4

from .errors import KernelValidationError

JsonScalar = Union[None, bool, int, float, str]
JsonValue = Union[
    JsonScalar,
    Tuple["JsonValue", ...],
    Mapping[str, "JsonValue"],
]
JsonObject = Mapping[str, JsonValue]


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """Create a readable, globally unique Core identifier."""

    clean = prefix.strip().replace(" ", "_")
    if not clean:
        raise KernelValidationError("id prefix must be non-empty")
    return "{}_{}".format(clean, uuid4().hex)


def freeze_json(value: Any) -> JsonValue:
    """Recursively copy a JSON-compatible value into immutable containers."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise KernelValidationError("JSON object keys must be strings")
            frozen[key] = freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise KernelValidationError(
        "value of type {!r} is not JSON-compatible".format(type(value).__name__)
    )


def freeze_json_object(value: Optional[Mapping[str, Any]] = None) -> JsonObject:
    frozen = freeze_json(value or {})
    if not isinstance(frozen, Mapping):  # defensive; value is always a mapping
        raise KernelValidationError("expected a JSON object")
    return frozen


def thaw_json(value: JsonValue) -> Any:
    """Convert an immutable JSON value back to normal dict/list containers."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def require_aware(timestamp: datetime, field_name: str) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise KernelValidationError("{} must be timezone-aware".format(field_name))


class SourceKind(str, Enum):
    DOCUMENT = "document"
    TABLE = "table"
    GRAPH = "graph"
    MEMORY = "memory"
    SKILL = "skill"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class SourceRef:
    """Stable logical identity of a registered inference data source."""

    source_id: str
    kind: SourceKind

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str):
            raise KernelValidationError("source_id must be a string")
        if not self.source_id.strip():
            raise KernelValidationError("source_id must be non-empty")
        if not isinstance(self.kind, SourceKind):
            raise KernelValidationError("source kind must be a SourceKind")


@dataclass(frozen=True)
class SnapshotRef:
    """Version of a source observed by a particular execution."""

    source: SourceRef
    version: str
    checksum: Optional[str] = None
    observed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceRef):
            raise KernelValidationError("snapshot source must be a SourceRef")
        if not isinstance(self.version, str):
            raise KernelValidationError("snapshot version must be a string")
        if not self.version.strip():
            raise KernelValidationError("snapshot version must be non-empty")
        if self.checksum is not None:
            if not isinstance(self.checksum, str):
                raise KernelValidationError("snapshot checksum must be a string")
            if not self.checksum.strip():
                raise KernelValidationError("snapshot checksum cannot be blank")
        if not isinstance(self.observed_at, datetime):
            raise KernelValidationError("observed_at must be a datetime")
        require_aware(self.observed_at, "observed_at")
