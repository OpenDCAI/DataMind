"""Graph tools exposed to the agent."""
from __future__ import annotations

from typing import Any

from datamind.core.tools import ToolSpec, tool_provider_registry

from .service import GraphService


def build_graph_tools(graph: GraphService) -> list[ToolSpec]:
    async def _search(query: str, top_k: int = 5) -> dict:
        entities = await graph.search_entities(query, top_k=top_k)
        return {"query": query, "count": len(entities), "entities": entities}

    async def _traverse(
        start: str,
        max_hops: int = 2,
        relation_filter: list[str] | None = None,
    ) -> dict:
        paths = await graph.traverse(start, max_hops=max_hops, relation_filter=relation_filter)
        return {"start": start, "max_hops": max_hops, "count": len(paths), "paths": paths}

    async def _neighbors(entity: str, direction: str = "both") -> dict:
        edges = await graph.neighbors(entity, direction=direction)
        return {"entity": entity, "direction": direction, "count": len(edges), "edges": edges}

    async def _upsert(triples: list[dict]) -> dict:
        return await graph.upsert(triples)

    return [
        ToolSpec(
            name="graph_search_entities",
            description=(
                "Find entities in the knowledge graph by name or alias. "
                "Use this first when the user names a concept — you need the entity id "
                "before you can traverse or look up neighbours."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Entity name or alias."},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                },
                "required": ["query"],
            },
            handler=_search,
            metadata={"group": "graph", "surface": "graph", "access": "read"},
        ),
        ToolSpec(
            name="graph_traverse",
            description=(
                "Walk the knowledge graph from a starting entity, up to N hops. "
                "Returns all paths sorted by average edge weight. "
                "Optional relation_filter restricts which relations you follow (e.g. [\"works_at\", \"located_in\"])."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Entity id to start from."},
                    "max_hops": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
                    "relation_filter": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional allow-list of relation names.",
                    },
                },
                "required": ["start"],
            },
            handler=_traverse,
            metadata={"group": "graph", "surface": "graph", "access": "read"},
        ),
        ToolSpec(
            name="graph_neighbors",
            description=(
                "Return every edge incident to an entity. "
                "Use direction='out' for what the entity relates TO, 'in' for what relates to it, 'both' for all."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["out", "in", "both"],
                        "default": "both",
                    },
                },
                "required": ["entity"],
            },
            handler=_neighbors,
            metadata={"group": "graph", "surface": "graph", "access": "read"},
        ),
        ToolSpec(
            name="graph_upsert_triples",
            description=(
                "Add or update one or more (subject, relation, object) triples in the graph. "
                "Use when the user asks you to remember a new fact; prefer graph_search_entities first to avoid duplicates."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "triples": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "subject": {"type": "string"},
                                "relation": {"type": "string"},
                                "object": {"type": "string"},
                                "subject_type": {"type": "string", "default": "entity"},
                                "object_type": {"type": "string", "default": "entity"},
                                "confidence": {"type": "number", "default": 1.0},
                                "source": {"type": ["string", "null"]},
                            },
                            "required": ["subject", "relation", "object"],
                        },
                    },
                },
                "required": ["triples"],
            },
            handler=_upsert,
            metadata={
                "group": "graph",
                "surface": "graph",
                "access": "write",
                "destructive": True,
            },
        ),
    ]


@tool_provider_registry.register("graph")
class _GraphToolProvider:
    def build(self, **services: Any) -> list[ToolSpec]:
        graph = services.get("graph_service")
        if graph is None:
            raise ValueError("graph tool provider requires 'graph_service'")
        return build_graph_tools(graph)


__all__ = ["build_graph_tools"]
