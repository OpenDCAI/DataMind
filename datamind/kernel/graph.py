"""Dependency-free property-graph values returned by typed traversal."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Tuple

from .errors import KernelValidationError
from .types import JsonObject, freeze_json_object


@dataclass(frozen=True)
class GraphNode:
    """One provider-independent property-graph node."""

    node_id: str
    labels: Tuple[str, ...] = ()
    properties: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise KernelValidationError(
                "graph node_id must be a non-empty string"
            )
        object.__setattr__(self, "labels", tuple(self.labels))
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.labels
        ):
            raise KernelValidationError(
                "graph labels must be non-empty strings"
            )
        if len(set(self.labels)) != len(self.labels):
            raise KernelValidationError(
                "graph labels cannot contain duplicates"
            )
        object.__setattr__(
            self,
            "properties",
            freeze_json_object(self.properties),
        )


@dataclass(frozen=True)
class GraphEdge:
    """One directed, provider-independent property-graph edge."""

    edge_id: str
    source_id: str
    target_id: str
    relation: str
    weight: float = 1.0
    properties: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        for name in ("edge_id", "source_id", "target_id", "relation"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KernelValidationError(
                    "graph {} must be a non-empty string".format(name)
                )
        if isinstance(self.weight, bool) or not isinstance(
            self.weight, (int, float)
        ):
            raise KernelValidationError(
                "graph edge weight must be numeric"
            )
        if not math.isfinite(float(self.weight)):
            raise KernelValidationError(
                "graph edge weight must be finite"
            )
        object.__setattr__(self, "weight", float(self.weight))
        object.__setattr__(
            self,
            "properties",
            freeze_json_object(self.properties),
        )


@dataclass(frozen=True)
class GraphPath:
    """A simple traversal path preserving nodes, edges, and direction."""

    nodes: Tuple[GraphNode, ...]
    edges: Tuple[GraphEdge, ...]
    score: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        if not self.nodes:
            raise KernelValidationError(
                "graph path requires at least one node"
            )
        if len(self.nodes) != len(self.edges) + 1:
            raise KernelValidationError(
                "graph path must contain exactly one more node than edge"
            )
        if any(not isinstance(item, GraphNode) for item in self.nodes):
            raise KernelValidationError(
                "graph path nodes must contain GraphNode values"
            )
        if any(not isinstance(item, GraphEdge) for item in self.edges):
            raise KernelValidationError(
                "graph path edges must contain GraphEdge values"
            )
        node_ids = tuple(item.node_id for item in self.nodes)
        if len(set(node_ids)) != len(node_ids):
            raise KernelValidationError(
                "graph path must be simple and cannot repeat nodes"
            )
        for index, edge in enumerate(self.edges):
            endpoints = frozenset((edge.source_id, edge.target_id))
            expected = frozenset(
                (self.nodes[index].node_id, self.nodes[index + 1].node_id)
            )
            if endpoints != expected:
                raise KernelValidationError(
                    "graph path edge does not connect adjacent nodes"
                )
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(
                self.score, (int, float)
            ):
                raise KernelValidationError(
                    "graph path score must be numeric"
                )
            if not math.isfinite(float(self.score)):
                raise KernelValidationError(
                    "graph path score must be finite"
                )
            object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True)
class GraphPathSet:
    """Bounded native result of one typed Traverse operation."""

    paths: Tuple[GraphPath, ...]
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", tuple(self.paths))
        if any(not isinstance(item, GraphPath) for item in self.paths):
            raise KernelValidationError(
                "graph path set must contain GraphPath values"
            )
        if not isinstance(self.truncated, bool):
            raise KernelValidationError(
                "graph path set truncated must be a boolean"
            )
        signatures = tuple(
            (
                tuple(node.node_id for node in path.nodes),
                tuple(edge.edge_id for edge in path.edges),
            )
            for path in self.paths
        )
        if len(set(signatures)) != len(signatures):
            raise KernelValidationError(
                "graph path set cannot contain duplicate paths"
            )


__all__ = [
    "GraphEdge",
    "GraphNode",
    "GraphPath",
    "GraphPathSet",
]
