"""Graph service: wraps a GraphStore and handles triplet loading."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from datamind.config import Settings
from datamind.core.logging import get_logger
from datamind.core.protocols import GraphStore, GraphTriple
from datamind.core.registry import graph_registry

# Importing providers populates graph_registry.
from . import providers  # noqa: F401

_log = get_logger("graph.service")


class GraphService:
    def __init__(
        self,
        *,
        store: GraphStore,
        data_dir: Path,
        storage_dir: Path,
    ) -> None:
        self.store = store
        self.data_dir = data_dir
        self.storage_dir = storage_dir

    async def load_from_profile(self) -> dict[str, int]:
        """Read every JSONL under data_dir/triplets/, upsert into the store."""
        triplets_dir = self.data_dir / "triplets"
        count = 0
        batch: list[GraphTriple] = []
        if triplets_dir.is_dir():
            for jsonl in sorted(triplets_dir.glob("*.jsonl")):
                with jsonl.open("r", encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError as exc:
                            _log.warning(
                                "bad_triplet",
                                extra={"file": str(jsonl), "line": lineno, "err": str(exc)},
                            )
                            continue
                        try:
                            triple = GraphTriple(**obj)
                            triple.properties = {
                                **triple.properties,
                                "_profile_managed": True,
                                "profile_source": f"{jsonl.name}:{lineno}",
                            }
                            batch.append(triple)
                        except Exception as exc:  # noqa: BLE001
                            _log.warning(
                                "invalid_triplet_shape",
                                extra={"file": str(jsonl), "line": lineno, "err": repr(exc)},
                            )
                            continue
                        count += 1
        reconcile = getattr(self.store, "reconcile_profile_triples", None)
        if callable(reconcile):
            await reconcile(batch)
        else:
            reset = getattr(self.store, "reset", None)
            if callable(reset):
                await reset()
            if batch:
                await self.store.upsert_triples(batch)
        persist = getattr(self.store, "persist", None)
        if callable(persist):
            await persist()
        _log.info("graph_loaded_from_profile", extra={"count": count})
        return {"triples_loaded": count}

    async def search_entities(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        return [e.model_dump() for e in await self.store.search_entities(query, top_k=top_k)]

    async def traverse(
        self,
        start: str,
        *,
        max_hops: int = 2,
        relation_filter: list[str] | None = None,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        paths = await self.store.traverse(
            start, max_hops=max_hops, relation_filter=relation_filter,
            max_results=max_results,
        )
        return [p.model_dump() for p in paths]

    async def neighbors(
        self,
        entity: str,
        *,
        direction: str = "both",
        relation_filter: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        edges = await self.store.neighbors(
            entity, direction=direction, relation_filter=relation_filter, limit=limit,
        )
        return [e.model_dump() for e in edges]

    async def upsert(self, triples: list[dict[str, Any]]) -> dict[str, int]:
        parsed = [GraphTriple(**t) for t in triples]
        await self.store.upsert_triples(parsed)
        persist = getattr(self.store, "persist", None)
        if callable(persist):
            await persist()
        return {"upserted": len(parsed)}

    def stats(self) -> dict[str, int]:
        stats = getattr(self.store, "stats", None)
        return stats() if callable(stats) else {}

    async def aclose(self) -> None:
        close = getattr(self.store, "aclose", None)
        if callable(close):
            await close()


def build_graph_service(settings: Settings) -> GraphService:
    storage_dir = settings.data.storage_dir
    storage_dir.mkdir(parents=True, exist_ok=True)
    backend = settings.graph.backend
    if backend == "networkx":
        store = graph_registry.create(
            "networkx",
            persist_path=str(storage_dir / "graph.json"),
        )
    else:
        # DSN-based remote backends (neo4j, ...) — future work.
        store = graph_registry.create(backend, dsn=settings.graph.dsn)
    return GraphService(
        store=store,
        data_dir=settings.data.data_dir,
        storage_dir=storage_dir,
    )


__all__ = ["GraphService", "build_graph_service"]
