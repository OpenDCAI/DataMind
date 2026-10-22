"""Immutable, source-scoped graph assertions with indexed snapshot reads."""

from __future__ import annotations
import difflib
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from datamind.core.protocols import Edge, Entity, GraphPath, GraphTriple


def connect(root):
    db = sqlite3.connect(Path(root) / "graph.sqlite3", timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS edges (
            artifact TEXT NOT NULL, identity TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
            rel TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(artifact,identity));
        CREATE INDEX IF NOT EXISTS graph_out ON edges(src,artifact,rel);
        CREATE INDEX IF NOT EXISTS graph_in ON edges(dst,artifact,rel);
        CREATE TABLE IF NOT EXISTS nodes (
            artifact TEXT NOT NULL, id TEXT NOT NULL, type TEXT NOT NULL,
            PRIMARY KEY(artifact,id));
        CREATE INDEX IF NOT EXISTS graph_entities ON nodes(id,artifact);
    """)
    return db


def put_graph(root, triples):
    values = [GraphTriple(**t).model_dump(mode="json") for t in triples]
    value = json.dumps(values, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(value.encode()).hexdigest()
    with closing(connect(root)) as db, db:
        if not db.execute("SELECT 1 FROM artifacts WHERE id=?", (key,)).fetchone():
            for t in values:
                identity = json.dumps(
                    [t["subject"], t["relation"], t["object"], t["source"]],
                    ensure_ascii=False,
                )
                db.execute(
                    "INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?,?)",
                    (
                        key,
                        identity,
                        t["subject"],
                        t["object"],
                        t["relation"],
                        json.dumps(t, ensure_ascii=False),
                    ),
                )
                for node, typ in (
                    (t["subject"], t["subject_type"]),
                    (t["object"], t["object_type"]),
                ):
                    db.execute(
                        "INSERT OR REPLACE INTO nodes VALUES (?,?,?)", (key, node, typ)
                    )
            db.execute("INSERT INTO artifacts VALUES (?,?)", (key, len(values)))
    return key


def prune_graph(root, manifests, *, compact=False):
    if not (Path(root) / "graph.sqlite3").exists():
        return
    keep = {
        s["graph_artifact"]
        for m in manifests
        for s in m["sources"]
        if s.get("graph_artifact")
    }
    with closing(connect(root)) as db, db:
        for (key,) in db.execute("SELECT id FROM artifacts").fetchall():
            if key not in keep:
                db.execute("DELETE FROM edges WHERE artifact=?", (key,))
                db.execute("DELETE FROM nodes WHERE artifact=?", (key,))
                db.execute("DELETE FROM artifacts WHERE id=?", (key,))
        db.commit()
        if compact:
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class GraphView:
    def __init__(self, root, artifacts):
        self.db = connect(root)
        self.db.execute("CREATE TEMP TABLE visible (id TEXT PRIMARY KEY)")
        self.db.executemany(
            "INSERT OR IGNORE INTO visible VALUES (?)", [(k,) for k in artifacts]
        )
        self.db.commit()  # The manifest pins visibility; never hold a SQLite read transaction across calls.

    def _nodes(self, where="", args=()):
        # One deterministic type per entity; no other revision enters this view.
        return self.db.execute(
            "SELECT n.id,MIN(n.type) FROM nodes n JOIN visible v ON n.artifact=v.id "
            + where
            + " GROUP BY n.id",
            args,
        )

    def stats(self):
        return {
            "nodes": self.db.execute(
                "SELECT COUNT(DISTINCT n.id) FROM nodes n JOIN visible v ON n.artifact=v.id"
            ).fetchone()[0],
            "edges": self.db.execute(
                "SELECT COUNT(*) FROM edges e JOIN visible v ON e.artifact=v.id"
            ).fetchone()[0],
        }

    def schema(self):
        stats = self.stats()
        types = Counter(typ for _, typ in self._nodes())
        relations = dict(
            self.db.execute(
                "SELECT e.rel,COUNT(*) FROM edges e JOIN visible v ON e.artifact=v.id GROUP BY e.rel ORDER BY e.rel"
            )
        )
        return {
            **stats,
            "node_types": dict(types),
            "relations": relations,
            "extensible": True,
        }

    async def search_entities(self, query, *, top_k=5):
        q = query.lower().strip()
        if not q:
            return []
        scored = []
        # Iterate, retaining only top-k candidates; do not load the whole graph.
        for node, typ in self._nodes():
            if node.lower() == q:
                score = 1.0
            elif q in node.lower():
                score = 0.7
            else:
                ratio = difflib.SequenceMatcher(None, q, node.lower()).ratio()
                if ratio < 0.4:
                    continue
                score = 0.4 + 0.4 * ratio
            scored.append(Entity(id=node, label=node, type=typ, score=score))
            scored.sort(key=lambda e: (-e.score, e.id))
            del scored[top_k:]
        return scored

    def _edges(self, entity, *, direction="both", relation_filter=None, limit=None):
        cond = {"out": "e.src=?", "in": "e.dst=?", "both": "(e.src=? OR e.dst=?)"}[
            direction
        ]
        args = [entity, entity] if direction == "both" else [entity]
        if relation_filter:
            cond += " AND e.rel IN (" + ",".join("?" for _ in relation_filter) + ")"
            args += list(relation_filter)
        sql = (
            "SELECT e.payload FROM edges e JOIN visible v ON e.artifact=v.id WHERE "
            + cond
            + " ORDER BY e.dst,e.rel,e.identity,e.artifact"
        )
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = self.db.execute(sql, args)
        for (payload,) in rows:
            t = json.loads(payload)
            yield Edge(
                source=t["subject"],
                target=t["object"],
                relation=t["relation"],
                weight=t["confidence"],
                properties={
                    **t["properties"],
                    **({"source": t["source"]} if t["source"] else {}),
                },
                evidence=t["evidence"],
            )

    async def neighbors(
        self, entity, *, direction="both", relation_filter=None, limit=100
    ):
        if limit <= 0:
            return []
        return list(
            self._edges(
                entity,
                direction=direction,
                relation_filter=relation_filter,
                limit=limit,
            )
        )

    async def traverse(
        self, start, *, max_hops=2, relation_filter=None, max_results=100
    ):
        if max_hops <= 0 or max_results <= 0:
            return []
        frontier = [(start, [], {start})]
        paths = []
        for _ in range(max_hops):
            next_frontier = []
            for node, prior, seen in frontier:
                # Bounded BFS retains exact edge sequences, including origins.
                for edge in self._edges(
                    node, direction="out", relation_filter=relation_filter
                ):
                    if edge.target in seen:
                        continue
                    edges = prior + [edge]
                    paths.append(
                        GraphPath(
                            nodes=[start] + [e.target for e in edges],
                            edges=edges,
                            score=sum(e.weight for e in edges) / len(edges),
                        )
                    )
                    if len(paths) >= max_results:
                        return sorted(paths, key=lambda p: -p.score)
                    next_frontier.append((edge.target, edges, seen | {edge.target}))
            frontier = next_frontier
            if not frontier:
                break
        return sorted(paths, key=lambda p: -p.score)

    async def aclose(self):
        self.db.close()
