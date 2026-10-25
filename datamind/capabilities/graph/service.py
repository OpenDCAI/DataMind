"""Graph service: wraps a GraphStore and handles triplet loading."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from datamind.config import Settings
from datamind.core.logging import get_logger
from datamind.core.protocols import GraphEvidence, GraphStore, GraphTriple
from .evidence import persist_input
from datamind.core.registry import graph_registry
from datamind.core.errors import CapabilityError

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
    
    async def search_entities_at_version(self, query: str, *, top_k: int = 5, version: str = None) -> list[dict[str, Any]]:
        if self.store.name != "terminusdb":
            raise CapabilityError("kb", "rollback is only supported by the LanceDB vector store")
        if not isinstance(version, str):
            raise CapabilityError("kb", f"version must be an string, got {type(version).__name__}")
        return [e.model_dump() for e in await self.store.search_entities(query, top_k=top_k, version=version)]

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
    
    async def traverse_at_version(
        self,
        start: str,
        *,
        max_hops: int = 2,
        relation_filter: list[str] | None = None,
        max_results: int = 100,
        version: str = None,
    ) -> list[dict[str, Any]]:
        if self.store.name != "terminusdb":
            raise CapabilityError("kb", "rollback is only supported by the LanceDB vector store")
        if not isinstance(version, str):
            raise CapabilityError("kb", f"version must be an string, got {type(version).__name__}")
        paths = await self.store.traverse(
            start, max_hops=max_hops, relation_filter=relation_filter,
            max_results=max_results, version=version,
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
    
    async def neighbors_at_version(
        self,
        entity: str,
        *,
        direction: str = "both",
        relation_filter: list[str] | None = None,
        limit: int = 100,
        version: str = None,
    ) -> list[dict[str, Any]]:
        edges = await self.store.neighbors(
            entity, direction=direction, relation_filter=relation_filter, limit=limit, version=version
        )
        return [e.model_dump() for e in edges]

    async def upsert(self, triples: list[dict[str, Any]]) -> dict[str, int]:
        parsed = [GraphTriple(**t) for t in triples]
        for triple in parsed:
            if triple.evidence:
                continue
            content = json.dumps(triple.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            path, version = persist_input(self.data_dir, content, ".json")
            identity = json.dumps([triple.subject, triple.relation, triple.object, triple.source])
            triple.evidence = [GraphEvidence(
                source_id="assertion:" + hashlib.sha256(identity.encode()).hexdigest(),
                source_version=version, locator={"uri": str(path), "provided_source": triple.source},
                method="explicit", pipeline_version="explicit-v1",
            )]
        await self.store.upsert_triples(parsed)
        persist = getattr(self.store, "persist", None)
        if callable(persist):
            await persist()
            
        add_version = getattr(self.store, "add_version", None)
        if add_version and callable(add_version):
            info = self.store.get_triples_info(added=parsed)
            await add_version(info)
        
        return {"upserted": len(parsed)}

    async def rollback(self, version: str = None) -> dict[str, Any]:
        if self.store.name != "terminusdb":
            raise CapabilityError("kb", "rollback is only supported by the LanceDB vector store")
        if not isinstance(version, str):
            raise CapabilityError("kb", f"version must be an string, got {type(version).__name__}")
        
        versions = await self.list_versions()
        if not any(v["version"] == version for v in versions):
            raise CapabilityError("kb", f"version {version} does not exist in this LanceDB table")

        result = await self.store.rollback(version=version)
        return {
            "status": "ok",
            "message": f"Successfully rolled back to version {version}",
            "rollback_version": version, # Target
            "new_version": result["version"],  # New Commit
        }

    async def list_versions(self) -> dict[str, Any]:
        if self.store.name != "terminusdb":
            raise CapabilityError("kb", "list_versions is only supported by the LanceDB vector store")

        return await self.store.list_versions()
    
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
    elif backend == "terminusdb":
        store = graph_registry.create(
            "terminusdb",
            dsn=settings.graph.dsn,
            password=settings.graph.password,
            persist_path=str(storage_dir / "graph"),
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
