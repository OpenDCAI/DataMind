"""Shared contracts for DataMind's two-agent data plane.

The contracts in this module are deliberately small.  They describe what was
stored and what evidence was retrieved; they are not an execution plan, DAG,
or operator language.  StoreAgent and RetrieveAgent remain ordinary tool-use
agents and exchange state only through the five data surfaces plus receipts.
"""
from __future__ import annotations

import hashlib
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class DataSurface(str, Enum):
    """The five user-visible knowledge surfaces."""

    KB = "kb"
    DB = "db"
    GRAPH = "graph"
    SKILLS = "skills"
    MEMORY = "memory"


class ToolAccess(str, Enum):
    """Whether a tool observes or mutates a data surface."""

    READ = "read"
    WRITE = "write"
    UTILITY = "utility"


class SourceRef(BaseModel):
    """Stable provenance for one ingest source."""

    source_id: str
    kind: Literal["file", "text", "records", "triples", "skill", "memory"]
    uri: str | None = None
    checksum: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path, **metadata: Any) -> "SourceRef":
        resolved = Path(path).expanduser().resolve()
        hasher = hashlib.sha256()
        with resolved.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        return cls(
            source_id=f"file:{digest[:24]}",
            kind="file",
            uri=str(resolved),
            checksum=f"sha256:{digest}",
            metadata=metadata,
        )

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        kind: Literal["text", "triples", "skill", "memory"] = "text",
        **metadata: Any,
    ) -> "SourceRef":
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return cls(
            source_id=f"{kind}:{digest[:24]}",
            kind=kind,
            checksum=f"sha256:{digest}",
            metadata=metadata,
        )


class SurfaceWriteResult(BaseModel):
    """Result of one concrete write into one surface."""

    surface: DataSurface
    operation: str
    status: Literal["stored", "unchanged", "failed"] = "stored"
    items_written: int = 0
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class IngestReceipt(BaseModel):
    """Auditable receipt emitted after a StoreAgent tool call."""

    receipt_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    profile: str
    revision: int
    source: SourceRef
    results: list[SurfaceWriteResult]

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.status != "failed" for r in self.results)


class Evidence(BaseModel):
    """A normalized fact returned by a RetrieveAgent tool."""

    surface: DataSurface
    source_id: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    content: Any = None
    score: float | None = None


class InferenceResult(BaseModel):
    """Provider-neutral result shape for retrieval-time inference."""

    answer: str
    evidence: list[Evidence] = Field(default_factory=list)
    surfaces_used: list[DataSurface] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "DataSurface",
    "ToolAccess",
    "SourceRef",
    "SurfaceWriteResult",
    "IngestReceipt",
    "Evidence",
    "InferenceResult",
]
