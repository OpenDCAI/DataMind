"""NetworkX-backed graph store.

Local, in-memory DiGraph persisted to disk as a JSON document. Good for
profiles up to ~100k edges; past that you swap in a Neo4j provider by
registering under `graph_registry`.

Storage layout (one file per profile):
    storage/<profile>/graph.json
        {"nodes": [{"id": "...", "label": "...", "type": "...", "props": {...}}],
         "edges": [{"src": "...", "dst": "...", "rel": "...", "w": 1.0, "props": {...}}]}
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Sequence

import networkx as nx

from datamind.core.logging import get_logger
from datamind.core.protocols import Edge, Entity, GraphPath, GraphTriple
from datamind.core.registry import graph_registry

_log = get_logger("graph.networkx")


@graph_registry.register("networkx")
class NetworkXGraphStore:
    """DiGraph that persists to a single JSON file per profile."""

    def __init__(
        self,
        *,
        persist_path: str | Path,
        autoload: bool = True,
    ) -> None:
        self._path = Path(persist_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()
        self._dirty = False
        self._mutation_revision = 0
        self._state_lock = threading.RLock()
        self._persist_lock = asyncio.Lock()
        self._persist_worker_lock = threading.Lock()
        if autoload and self._path.exists():
            self._load()
        _log.info(
            "graph_loaded",
            extra={
                "path": str(self._path),
                "nodes": self._g.number_of_nodes(),
                "edges": self._g.number_of_edges(),
            },
        )

    # ------------------------------------------------------------- persist

    def _load(self) -> None:
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("graph_load_failed", extra={"err": str(exc)})
            return
        for n in doc.get("nodes", []):
            self._g.add_node(
                n["id"],
                label=n.get("label", n["id"]),
                type=n.get("type", "entity"),
                **(n.get("props") or {}),
            )
        for e in doc.get("edges", []):
            self._g.add_edge(
                e["src"],
                e["dst"],
                key=e.get("key") or e.get("rel"),
                relation=e.get("rel", "related"),
                weight=float(e.get("w", 1.0)),
                **(e.get("props") or {}),
            )

    async def persist(self) -> None:
        async with self._persist_lock:
            await asyncio.to_thread(self._persist_sync)

    def _persist_sync(self) -> None:
        # Cancelling to_thread's awaiter does not stop its worker. Serialize
        # capture, replacement, and dirty-state bookkeeping in the worker too,
        # so a cancelled save cannot overwrite a later successful save.
        with self._persist_worker_lock:
            with self._state_lock:
                if not self._dirty:
                    return
                captured_revision = self._mutation_revision
                doc = self._document_locked()

            self._write_document(doc)

            with self._state_lock:
                # A mutation may have happened while the captured document
                # was being written. In that case the newer state is not on
                # disk yet and must remain dirty for the next persist call.
                if self._mutation_revision == captured_revision:
                    self._dirty = False

    def _document_locked(self) -> dict[str, Any]:
        """Build a detached JSON document while holding ``_state_lock``."""
        return {
            "nodes": [
                {
                    "id": nid,
                    "label": d.get("label", nid),
                    "type": d.get("type", "entity"),
                    "props": {k: v for k, v in d.items() if k not in {"label", "type"}},
                }
                for nid, d in self._g.nodes(data=True)
            ],
            "edges": [
                {
                    "src": u,
                    "dst": v,
                    "key": key,
                    "rel": d.get("relation", "related"),
                    "w": float(d.get("weight", 1.0)),
                    "props": {
                        k: val
                        for k, val in d.items()
                        if k not in {"relation", "weight"}
                    },
                }
                for u, v, key, d in self._g.edges(keys=True, data=True)
            ],
        }

    def _write_document(self, doc: dict[str, Any]) -> None:
        """Atomically write one detached document through a unique temp file."""
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self._path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    # ------------------------------------------------------------ mutation

    async def upsert_triples(self, triples: Sequence[GraphTriple]) -> None:
        with self._state_lock:
            for t in triples:
                # Nodes
                for side, node_id, node_type in (
                    ("subject", t.subject, t.subject_type),
                    ("object", t.object, t.object_type),
                ):
                    if not self._g.has_node(node_id):
                        self._g.add_node(
                            node_id,
                            label=node_id,
                            type=node_type,
                        )
                # Profile snapshots and runtime writes have separate identities;
                # exact duplicates within either origin overwrite deterministically.
                profile_managed = bool((t.properties or {}).get("_profile_managed"))
                origin = "profile" if profile_managed else (t.source or "runtime")
                self._g.add_edge(
                    t.subject,
                    t.object,
                    key=f"{t.relation}\x1f{origin}",
                    relation=t.relation,
                    weight=float(t.confidence),
                    source=t.source,
                    **{f"p_{k}": v for k, v in (t.properties or {}).items()},
                )
            self._mutation_revision += 1
            self._dirty = True

    async def reconcile_profile_triples(self, triples: Sequence[GraphTriple]) -> None:
        """Replace only edges managed by the profile snapshot."""
        with self._state_lock:
            stale = [
                (u, v, key)
                for u, v, key, data in self._g.edges(keys=True, data=True)
                if data.get("p__profile_managed") is True
            ]
            self._g.remove_edges_from(stale)
        await self.upsert_triples(triples)
        # Drop now-orphaned profile nodes without touching runtime nodes.
        with self._state_lock:
            self._g.remove_nodes_from(list(nx.isolates(self._g)))

    async def reconcile_source_triples(
        self, source: str, triples: Sequence[GraphTriple]
    ) -> None:
        """Replace edges produced from one source file while preserving others."""
        with self._state_lock:
            stale = [
                (u, v, key)
                for u, v, key, data in self._g.edges(keys=True, data=True)
                if data.get("p__source_path") == source
            ]
            self._g.remove_edges_from(stale)
        await self.upsert_triples(triples)
        with self._state_lock:
            self._g.remove_nodes_from(list(nx.isolates(self._g)))

    async def reconcile_lineage_triples(
        self, root: str, triples: Sequence[GraphTriple]
    ) -> None:
        """Replace all deterministic lineage edges for one workspace root."""
        with self._state_lock:
            stale = [
                (u, v, key)
                for u, v, key, data in self._g.edges(keys=True, data=True)
                if data.get("p__lineage_root") == root
            ]
            self._g.remove_edges_from(stale)
        await self.upsert_triples(triples)
        with self._state_lock:
            self._g.remove_nodes_from(list(nx.isolates(self._g)))

    async def reset(self) -> None:
        with self._state_lock:
            self._g = nx.MultiDiGraph()
            self._mutation_revision += 1
            self._dirty = True

    # ------------------------------------------------------------- lookup

    async def search_entities(self, query: str, *, top_k: int = 5) -> list[Entity]:
        q = query.lower().strip()
        if not q:
            return []
        scored: list[tuple[float, str, dict]] = []
        for nid, data in self._g.nodes(data=True):
            label = str(data.get("label", nid))
            # Exact and substring hits beat fuzzy.
            if nid.lower() == q or label.lower() == q:
                score = 1.0
            elif q in nid.lower() or q in label.lower():
                score = 0.7
            else:
                ratio = difflib.SequenceMatcher(None, q, label.lower()).ratio()
                if ratio < 0.4:
                    continue
                score = 0.4 + 0.4 * ratio  # cap below substring score
            scored.append((score, nid, data))
        scored.sort(key=lambda x: -x[0])
        return [
            Entity(
                id=nid,
                label=str(data.get("label", nid)),
                type=str(data.get("type", "entity")),
                score=score,
                properties={
                    k: v for k, v in data.items() if k not in {"label", "type"}
                },
            )
            for score, nid, data in scored[:top_k]
        ]

    async def neighbors(
        self,
        entity: str,
        *,
        direction: str = "both",
        relation_filter: list[str] | None = None,
        limit: int = 100,
    ) -> list[Edge]:
        if not self._g.has_node(entity):
            return []
        allowed = set(relation_filter) if relation_filter else None
        edges: list[Edge] = []
        if direction in {"out", "both"}:
            for u, v, d in self._g.out_edges(entity, data=True):
                if allowed is None or d.get("relation", "related") in allowed:
                    edges.append(self._edge(u, v, d))
        if direction in {"in", "both"}:
            for u, v, d in self._g.in_edges(entity, data=True):
                if allowed is None or d.get("relation", "related") in allowed:
                    edges.append(self._edge(u, v, d))
        return edges[:limit]

    async def traverse(
        self,
        start: str,
        *,
        max_hops: int = 2,
        relation_filter: list[str] | None = None,
        max_results: int = 100,
    ) -> list[GraphPath]:
        """Return edge-distinct simple paths, including parallel relations.

        Traverse breadth-first in stable target/relation/edge-key order and
        stop after ``max_results`` paths. Sort that bounded selection by score;
        this is not an exhaustive global top-k search. Each path excludes
        repeated nodes, while different edge identities may share its nodes.
        """
        if max_hops <= 0 or max_results <= 0 or not self._g.has_node(start):
            return []
        allowed = set(relation_filter) if relation_filter else None

        # BFS over (node, path_edges) up to max_hops.
        paths: list[GraphPath] = []
        frontier: list[tuple[str, list[Edge], set[str]]] = [(start, [], {start})]
        depth = 0
        while frontier and depth < max_hops:
            next_frontier: list[tuple[str, list[Edge], set[str]]] = []
            for node, edges_so_far, seen in frontier:
                outgoing = sorted(
                    self._g.out_edges(node, keys=True, data=True),
                    key=lambda e: (e[1], str(e[3].get("relation", "related")), str(e[2])),
                )
                for u, v, _edge_key, d in outgoing:
                    rel = d.get("relation", "related")
                    if allowed is not None and rel not in allowed:
                        continue
                    if v in seen:
                        continue
                    edge_obj = self._edge(u, v, d)
                    path_edges = edges_so_far + [edge_obj]
                    nodes = [start] + [e.target for e in path_edges]
                    # Each frontier entry is an exact edge-sequence prefix.
                    # Extending it once per keyed edge already enumerates
                    # distinct paths; node-only dedup loses parallel evidence.
                    paths.append(
                        GraphPath(
                            nodes=nodes,
                            edges=path_edges,
                            score=sum(e.weight for e in path_edges) / len(path_edges),
                        )
                    )
                    if len(paths) >= max_results:
                        paths.sort(key=lambda p: -p.score)
                        return paths[:max_results]
                    next_frontier.append((v, path_edges, seen | {v}))
            frontier = next_frontier
            depth += 1
        # Highest-weighted paths first.
        paths.sort(key=lambda p: -p.score)
        return paths[:max_results]

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _edge(u: str, v: str, data: dict) -> Edge:
        return Edge(
            source=u,
            target=v,
            relation=str(data.get("relation", "related")),
            weight=float(data.get("weight", 1.0)),
            properties={
                k[2:] if k.startswith("p_") else k: val
                for k, val in data.items()
                if k not in {"relation", "weight"}
            },
        )

    def stats(self) -> dict[str, int]:
        return {
            "nodes": self._g.number_of_nodes(),
            "edges": self._g.number_of_edges(),
        }
