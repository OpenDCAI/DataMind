"""Deterministic in-memory property-graph reference adapter."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import quote

from datamind.dataops import (
    BindingRow,
    BindingSet,
    Evidence,
    GraphDirection,
    ResultKind,
    Traverse,
)
from datamind.kernel import (
    ExecutionContext,
    GraphEdge,
    GraphNode,
    GraphPath,
    GraphPathSet,
    KernelValidationError,
    Provenance,
    SnapshotRef,
    SnapshotUnavailableError,
    SourceDescriptor,
    SourceExecutionError,
    SourceKind,
    SourceRef,
    sha256_checksum,
    thaw_json,
)
from datamind.ports import SourceResult


class InMemoryGraphSource:
    """A small, immutable property graph for Core contract tests."""

    def __init__(
        self,
        *,
        source_id: str,
        nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge],
        version: str = "",
        display_name: str = "In-memory property graph",
    ) -> None:
        node_values = tuple(nodes)
        edge_values = tuple(edges)
        node_ids = tuple(item.node_id for item in node_values)
        edge_ids = tuple(item.edge_id for item in edge_values)
        if len(set(node_ids)) != len(node_ids):
            raise KernelValidationError(
                "graph source node ids must be unique"
            )
        if len(set(edge_ids)) != len(edge_ids):
            raise KernelValidationError(
                "graph source edge ids must be unique"
            )
        if any(not isinstance(item, GraphNode) for item in node_values):
            raise KernelValidationError(
                "graph source nodes must contain GraphNode values"
            )
        if any(not isinstance(item, GraphEdge) for item in edge_values):
            raise KernelValidationError(
                "graph source edges must contain GraphEdge values"
            )
        known_nodes = set(node_ids)
        for edge in edge_values:
            if (
                edge.source_id not in known_nodes
                or edge.target_id not in known_nodes
            ):
                raise KernelValidationError(
                    "graph edge endpoints must exist in the source"
                )

        self._nodes = {
            item.node_id: item
            for item in sorted(node_values, key=lambda item: item.node_id)
        }
        self._edges = tuple(
            sorted(
                edge_values,
                key=lambda item: (
                    item.source_id,
                    item.target_id,
                    item.relation,
                    item.edge_id,
                ),
            )
        )
        ref = SourceRef(source_id, SourceKind.GRAPH)
        checksum = self._checksum()
        snapshot_version = version or "sha256:{}".format(checksum)
        self._snapshot = SnapshotRef(
            source=ref,
            version=snapshot_version,
            checksum=checksum,
        )
        self._descriptor = SourceDescriptor(
            ref=ref,
            display_name=display_name,
            capabilities=frozenset(("traverse",)),
            version=snapshot_version,
            schema={
                "node": {
                    "node_id": "string",
                    "labels": "tuple[string]",
                    "properties": "object",
                },
                "edge": {
                    "edge_id": "string",
                    "source_id": "string",
                    "target_id": "string",
                    "relation": "string",
                    "weight": "number",
                    "properties": "object",
                },
                "path_semantics": "bounded_simple_paths",
            },
            metadata={
                "adapter": "in_memory_property_graph",
                "ordering": "hops_then_path_identity",
            },
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        return self._descriptor

    async def current_snapshot(self) -> SnapshotRef:
        return self._snapshot

    async def has_snapshot(self, snapshot: SnapshotRef) -> bool:
        return (
            isinstance(snapshot, SnapshotRef)
            and self._snapshot.same_version_as(snapshot)
        )

    async def execute(
        self,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        if not isinstance(operation, Traverse):
            raise SourceExecutionError(
                "graph source only supports Traverse"
            )
        if operation.start_binding is not None:
            raise SourceExecutionError(
                "Traverse bindings must be resolved before adapter execution"
            )
        pinned = context.snapshots.get(self.descriptor.ref)
        if pinned is not None and not await self.has_snapshot(pinned):
            raise SnapshotUnavailableError(
                "graph source {!r} cannot serve snapshot {!r}".format(
                    self.descriptor.ref.source_id,
                    pinned.version,
                )
            )

        selected, truncated, missing = self._traverse(operation)
        evidence = []
        provenance = []
        binding_rows = []
        for path in selected:
            identity = {
                "nodes": [item.node_id for item in path.nodes],
                "edges": [item.edge_id for item in path.edges],
            }
            encoded = json.dumps(
                identity,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            path_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            origin = Provenance(
                source=self.descriptor.ref,
                locator="graph://{}/path/{}".format(
                    quote(self.descriptor.ref.source_id, safe=""),
                    path_hash,
                ),
                snapshot=self._snapshot,
            )
            evidence_item = Evidence(
                kind=SourceKind.GRAPH,
                content=encoded,
                provenance=origin,
                score=path.score,
                metadata={
                    "start_id": path.nodes[0].node_id,
                    "end_id": path.nodes[-1].node_id,
                    "hops": len(path.edges),
                },
            )
            evidence.append(evidence_item)
            provenance.append(origin)
            binding_rows.append(
                BindingRow(
                    values={
                        "start_id": path.nodes[0].node_id,
                        "end_id": path.nodes[-1].node_id,
                        "hops": len(path.edges),
                        "node_path": tuple(
                            item.node_id for item in path.nodes
                        ),
                        "relation_path": tuple(
                            item.relation for item in path.edges
                        ),
                        "edge_path": tuple(
                            item.edge_id for item in path.edges
                        ),
                        "score": path.score,
                    },
                    evidence_ids=(evidence_item.evidence_id,),
                )
            )

        warnings = []
        if missing:
            warnings.append(
                "graph start nodes were not found: {}".format(
                    ", ".join(missing)
                )
            )
        if truncated:
            warnings.append(
                "graph traversal reached the explicit limit={}".format(
                    operation.limit
                )
            )
        return SourceResult(
            value=GraphPathSet(
                paths=tuple(selected),
                truncated=truncated,
            ),
            result_kind=ResultKind.GRAPH_PATHS,
            evidence=tuple(evidence),
            bindings=BindingSet(
                fields=(
                    "start_id",
                    "end_id",
                    "hops",
                    "node_path",
                    "relation_path",
                    "edge_path",
                    "score",
                ),
                rows=tuple(binding_rows),
            ),
            provenance=tuple(provenance),
            snapshots=(self._snapshot,),
            warnings=tuple(warnings),
        )

    def _traverse(
        self,
        operation: Traverse,
    ) -> Tuple[Tuple[GraphPath, ...], bool, Tuple[str, ...]]:
        allowed = set(operation.relations) if operation.relations else None
        adjacency = self._adjacency(operation.direction, allowed)
        paths: List[GraphPath] = []
        starts = tuple(sorted(operation.starts))
        missing = [
            item for item in starts if item not in self._nodes
        ]
        frontier = deque(
            (start, (start,), ())
            for start in starts
            if start in self._nodes
        )
        while frontier and len(paths) <= operation.limit:
            current, node_ids, edges = frontier.popleft()
            hops = len(edges)
            if hops >= operation.min_hops:
                score = sum(item.weight for item in edges) / hops
                paths.append(
                    GraphPath(
                        nodes=tuple(
                            self._nodes[item] for item in node_ids
                        ),
                        edges=edges,
                        score=score,
                    )
                )
                if len(paths) > operation.limit:
                    break
            if hops >= operation.max_hops:
                continue
            for next_id, edge in adjacency.get(current, ()):
                if next_id in node_ids:
                    continue
                frontier.append(
                    (
                        next_id,
                        node_ids + (next_id,),
                        edges + (edge,),
                    )
                )
        ordered = tuple(
            sorted(
                paths,
                key=lambda path: (
                    len(path.edges),
                    tuple(item.node_id for item in path.nodes),
                    tuple(item.edge_id for item in path.edges),
                ),
            )
        )
        return (
            ordered[: operation.limit],
            len(ordered) > operation.limit,
            tuple(missing),
        )

    def _adjacency(
        self,
        direction: GraphDirection,
        allowed: Any,
    ) -> Dict[str, Tuple[Tuple[str, GraphEdge], ...]]:
        values: Dict[str, list] = {}
        for edge in self._edges:
            if allowed is not None and edge.relation not in allowed:
                continue
            if direction in (GraphDirection.OUT, GraphDirection.BOTH):
                values.setdefault(edge.source_id, []).append(
                    (edge.target_id, edge)
                )
            if direction in (GraphDirection.IN, GraphDirection.BOTH):
                values.setdefault(edge.target_id, []).append(
                    (edge.source_id, edge)
                )
        return {
            node_id: tuple(
                sorted(
                    items,
                    key=lambda item: (
                        item[1].relation,
                        item[1].edge_id,
                        item[0],
                    ),
                )
            )
            for node_id, items in values.items()
        }

    def _checksum(self) -> str:
        payload = {
            "nodes": [
                {
                    "node_id": item.node_id,
                    "labels": list(item.labels),
                    "properties": thaw_json(item.properties),
                }
                for item in self._nodes.values()
            ],
            "edges": [
                {
                    "edge_id": item.edge_id,
                    "source_id": item.source_id,
                    "target_id": item.target_id,
                    "relation": item.relation,
                    "weight": item.weight,
                    "properties": thaw_json(item.properties),
                }
                for item in self._edges
            ],
        }
        return sha256_checksum(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )


__all__ = ["InMemoryGraphSource"]
