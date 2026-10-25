"""Graph tools exposed to the agent."""
from __future__ import annotations

from typing import Any

from datamind.core.tools import ToolSpec, tool_provider_registry

from .service import GraphService


def build_graph_tools(graph: GraphService) -> list[ToolSpec]:
    async def _search(query: str, top_k: int = 5) -> dict:
        entities = await graph.search_entities(query, top_k=top_k)
        return {"query": query, "count": len(entities), "entities": entities}
    
    async def _search_at_version(query: str, top_k: int = 5, version: str = None) -> dict:
        entities = await graph.search_entities(query, top_k=top_k, version=version)
        return {"query": query, "count": len(entities), "entities": entities}

    async def _traverse(
        start: str,
        max_hops: int = 2,
        relation_filter: list[str] | None = None,
        max_results: int = 100,
    ) -> dict:
        paths = await graph.traverse(
            start, max_hops=max_hops, relation_filter=relation_filter,
            max_results=max_results + 1,
        )
        truncated = len(paths) > max_results
        visible = paths[:max_results]
        return {
            "start": start, "max_hops": max_hops, "count": len(visible),
            "total_count": len(paths), "truncated": truncated,
            "next_cursor": max_results if truncated else None, "paths": visible,
        }
        
    async def _traverse_at_version(
        start: str,
        max_hops: int = 2,
        relation_filter: list[str] | None = None,
        max_results: int = 100,
        version: str = None,
    ) -> dict:
        paths = await graph.traverse(
            start, max_hops=max_hops, relation_filter=relation_filter,
            max_results=max_results + 1, version=version,
        )
        truncated = len(paths) > max_results
        visible = paths[:max_results]
        return {
            "start": start, "max_hops": max_hops, "count": len(visible),
            "total_count": len(paths), "truncated": truncated,
            "next_cursor": max_results if truncated else None, "paths": visible,
        }

    async def _neighbors(
        entity: str,
        direction: str = "both",
        relation_filter: list[str] | None = None,
        limit: int = 100,
    ) -> dict:
        edges = await graph.neighbors(
            entity, direction=direction, relation_filter=relation_filter, limit=limit + 1,
        )
        truncated = len(edges) > limit
        visible = edges[:limit]
        return {
            "entity": entity, "direction": direction, "count": len(visible),
            "total_count": len(edges), "truncated": truncated,
            "next_cursor": limit if truncated else None, "edges": visible,
        }
        
    async def _neighbors_at_version(
        entity: str,
        direction: str = "both",
        relation_filter: list[str] | None = None,
        limit: int = 100,
        version: str = None
    ) -> dict:
        edges = await graph.neighbors(
            entity, direction=direction, relation_filter=relation_filter, limit=limit + 1, version=version,
        )
        truncated = len(edges) > limit
        visible = edges[:limit]
        return {
            "entity": entity, "direction": direction, "count": len(visible),
            "total_count": len(edges), "truncated": truncated,
            "next_cursor": limit if truncated else None, "edges": visible,
        }

    async def _upsert(triples: list[dict]) -> dict:
        return await graph.upsert(triples)

    async def _schema():
        return graph.store.schema()

    async def _list_versions():
        return await graph.list_versions()
    
    async def _rollback(version: str = None):
        return await graph.rollback(version=version)
    
    tools = [
        ToolSpec(name='graph_schema', description='Discover observed node types and relation names before graph search.',
                 input_schema={'type': 'object', 'properties': {}}, handler=_schema,
                 metadata={'surface': 'graph', 'access': 'read'}),
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
                "Returns a bounded set of paths sorted by average edge weight. "
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
                    "max_results": {
                        "type": "integer", "minimum": 1, "maximum": 500, "default": 100,
                        "description": "Hard cap on returned paths.",
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
                "Use direction='out' for what the entity relates TO, 'in' for what relates to it, 'both' for both; "
                "use relation_filter to preserve relation semantics such as required input versus produced output."
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
                    "relation_filter": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Only return these exact relation types.",
                    },
                    "limit": {
                        "type": "integer", "minimum": 1, "maximum": 500, "default": 100,
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
                                "evidence": {"type": "array", "items": {
                                    "type": "object", "properties": {
                                        "source_id": {"type": "string"},
                                        "source_version": {"type": "string"},
                                        "locator": {"type": "object"},
                                        "method": {"type": "string", "enum": ["explicit", "extracted", "inferred"]},
                                        "pipeline_version": {"type": "string"},
                                    }, "required": ["source_id", "source_version"],
                                }},
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
    
    if (
        graph is not None
        and getattr(getattr(graph, "store", None), "name", None) == "terminusdb"
    ):
        tools.extend([
            ToolSpec(
                name="graph_search_entities_at_version",
                description=(
                    "Search for knowledge graph entities by name or alias in a specific "
                    "graph version. Use this tool first when the query mentions an entity "
                    "by name and its entity ID is unknown. Returns matching entities from "
                    "the specified historical state without changing the current graph state."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Entity name or alias."},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                        "version": {
                            "type": "string",
                            "description": (
                                "The exact graph version or commit identifier to query. "
                                "Use an identifier returned by list_versions. "
                            ),
                        },
                    },
                    "required": ["query", "version"],
                },
                handler=_search_at_version,
                metadata={"group": "graph", "surface": "graph", "access": "read"},
            ),
            ToolSpec(
                name="graph_traverse_at_version",
                description=(
                    "Traverse the knowledge graph from a starting entity in a specific "
                    "historical version. Explores paths up to max_hops and returns a bounded "
                    "set of paths ranked by average edge weight. Use relation_filter to "
                    "restrict traversal to exact relation types. The query does not change "
                    "the current graph state."
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
                        "max_results": {
                            "type": "integer", "minimum": 1, "maximum": 500, "default": 100,
                            "description": "Hard cap on returned paths.",
                        },
                        "version": {
                            "type": "string",
                            "description": (
                                "The exact graph version or commit identifier to query. "
                                "Use an identifier returned by list_versions. "
                            ),
                        },
                    },
                    "required": ["start", "version"],
                },
                handler=_traverse_at_version,
                metadata={"group": "graph", "surface": "graph", "access": "read"},
            ),
            ToolSpec(
                name="graph_neighbors_at_version",
                description=(
                    "Retrieve edges connected to an entity in a specific historical graph "
                    "version. Use direction='out' for outgoing edges, 'in' for incoming "
                    "edges, or 'both' for both directions. Optionally restrict results using "
                    "relation_filter and cap the number of returned edges with limit. "
                    "Use this tool to inspect an entity's historical relationships without "
                    "changing the current graph state."
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
                        "relation_filter": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Only return these exact relation types.",
                        },
                        "limit": {
                            "type": "integer", "minimum": 1, "maximum": 500, "default": 100,
                        },
                        "version": {
                            "type": "string",
                            "description": (
                                "The exact graph version or commit identifier to query. "
                                "Use an identifier returned by list_versions. "
                            ),
                        },
                    },
                    "required": ["entity", "version"],
                },
                handler=_neighbors_at_version,
                metadata={"group": "graph", "surface": "graph", "access": "read"},
            ),
            ToolSpec(
                name="list_versions",
                description=(
                    "List available knowledge graph versions or commits. Use this tool to "
                    "discover valid version identifiers before calling historical entity "
                    "search, traversal, or neighbor tools. Use the returned identifiers "
                    "as the version argument for historical queries."
                ),
                input_schema={},
                handler=_list_versions,
                metadata={"group": "graph", "surface": "graph", "access": "read"},
            ),
            ToolSpec(
                name="rollback",
                description=(
                    "Restore the knowledge graph to a specified historical version. "
                    "This is a state-changing operation, unlike historical read tools. "
                    "Only use it when explicitly requested to roll back the graph; do not "
                    "use it merely to query historical data."
                ),
                input_schema={
                    "version": {
                        "type": "string",
                        "description": (
                            "The exact graph version or commit identifier to query. "
                            "Use an identifier returned by list_versions. "
                        ),
                    },
                },
                handler=_rollback,
                metadata={"group": "graph", "surface": "graph", "access": "write", "destructive": True,},
            ),
        ])
        
    return tools

@tool_provider_registry.register("graph")
class _GraphToolProvider:
    def build(self, **services: Any) -> list[ToolSpec]:
        graph = services.get("graph_service")
        if graph is None:
            raise ValueError("graph tool provider requires 'graph_service'")
        return build_graph_tools(graph)


__all__ = ["build_graph_tools"]
