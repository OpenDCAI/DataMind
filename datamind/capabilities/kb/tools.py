"""KB tool provider — exposes KB operations as Anthropic tools.

Usage:
    from datamind.capabilities.kb import build_kb_service
    from datamind.capabilities.kb.tools import build_kb_tools

    kb = build_kb_service(settings, llm_client=client)
    tools = build_kb_tools(kb)
    registry = ToolRegistry(); registry.extend(tools)

The agent loop (Phase 7) consumes `registry.as_anthropic_tools()` and
dispatches tool_use blocks to `registry.get(name).handler(**input)`.
"""
from __future__ import annotations

from typing import Any

from datamind.core.tools import ToolSpec, tool_provider_registry

from .service import KBService


def build_kb_tools(kb: KBService) -> list[ToolSpec]:
    """Return the concrete ToolSpecs bound to this KBService instance."""

    async def _search(query: str, top_k: int = 5, filters: dict | None = None) -> dict:
        chunks = await kb.search(query, top_k=top_k, filters=filters)
        return {
            "query": query,
            "top_k": top_k,
            "results": chunks,
            "count": len(chunks),
            "total_count": None,
            "truncated": len(chunks) >= top_k,
            "next_cursor": None,
        }

    async def _list_documents() -> dict:
        items = await kb.list_documents()
        return {"count": len(items), "items": items}

    async def _reindex() -> dict:
        stats = await kb.reindex()
        return {"status": "ok", **stats}

    async def _count() -> dict:
        return {"chunks": await kb.count()}

    return [
        ToolSpec(
            name="kb_search",
            description=(
                "Search the knowledge base (vector RAG). "
                "Use this for any question about the documents you have indexed. "
                "Returns the top-k most relevant chunks with their text, source path, and relevance score."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language search query."},
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of chunks to return.",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 5,
                    },
                    "filters": {
                        "type": "object",
                        "description": "Optional metadata filter, e.g. {\"source\": \"foo.md\"}.",
                        "additionalProperties": True,
                    },
                },
                "required": ["query"],
            },
            handler=_search,
            metadata={"group": "kb", "surface": "kb", "access": "read"},
        ),
        ToolSpec(
            name="kb_list_documents",
            description="List all documents currently available under the active profile.",
            input_schema={"type": "object", "properties": {}},
            handler=_list_documents,
            metadata={"group": "kb", "surface": "kb", "access": "read"},
        ),
        ToolSpec(
            name="kb_count",
            description="Report how many chunks are currently indexed in the knowledge base.",
            input_schema={"type": "object", "properties": {}},
            handler=_count,
            metadata={"group": "kb", "surface": "kb", "access": "read"},
        ),
        ToolSpec(
            name="kb_reindex",
            description=(
                "Rebuild the knowledge base index from scratch. "
                "This is expensive and only needed after documents are added or removed on disk."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_reindex,
            metadata={
                "group": "kb",
                "surface": "kb",
                "access": "write",
                "destructive": True,
                "source_path": str(kb.data_dir),
            },
        ),
    ]


# Also expose a factory-style provider through the global registry so the
# agent-assembly layer in Phase 7 can discover KB tools generically.
@tool_provider_registry.register("kb")
class _KBToolProvider:
    def build(self, **services: Any) -> list[ToolSpec]:
        kb = services.get("kb_service")
        if kb is None:
            raise ValueError("kb tool provider requires 'kb_service'")
        return build_kb_tools(kb)


__all__ = ["build_kb_tools"]
