"""Skills tools: semantic search + direct lookup + code skills."""
from __future__ import annotations

from typing import Any

from datamind.core.tools import ToolSpec, tool_provider_registry

from .service import SkillsService


def build_skills_tools(skills: SkillsService) -> list[ToolSpec]:
    async def _search(query: str, top_k: int = 3) -> dict:
        hits = await skills.search(query, top_k=top_k)
        return {"query": query, "count": len(hits), "results": hits}

    async def _get(name: str) -> dict:
        return skills.get(name)

    async def _list() -> dict:
        items = skills.list_skills()
        return {"count": len(items), "items": items}

    specs = [
        ToolSpec(
            name="skill_search",
            description=(
                "Semantically search available knowledge-type skills (Markdown SOPs, runbooks, guides). "
                "Call this when the user's question might be covered by one of the team's documented procedures — "
                "the result includes a short snippet and the skill name you can fetch in full with skill_get."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                },
                "required": ["query"],
            },
            handler=_search,
            metadata={"group": "skill.knowledge", "surface": "skills", "access": "read"},
        ),
        ToolSpec(
            name="skill_get",
            description=(
                "Return the full body of a named skill. Call after skill_search (or when you already know the name)."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill identifier, e.g. 'code-review'."}},
                "required": ["name"],
            },
            handler=_get,
            metadata={"group": "skill.knowledge", "surface": "skills", "access": "read"},
        ),
        ToolSpec(
            name="skill_list",
            description="List every available knowledge skill (name + description).",
            input_schema={"type": "object", "properties": {}},
            handler=_list,
            metadata={"group": "skill.knowledge", "surface": "skills", "access": "read"},
        ),
    ]

    # Combine with code skills for a single registry-facing list.
    specs.extend(skills.code_tools)
    return specs


def build_skills_store_tools(skills: SkillsService) -> list[ToolSpec]:
    """Tools available only to StoreAgent."""

    async def _upsert(
        name: str,
        description: str,
        body: str,
        keywords: list[str] | None = None,
        overwrite: bool = True,
    ) -> dict:
        return await skills.upsert(
            name=name,
            description=description,
            body=body,
            keywords=keywords,
            overwrite=overwrite,
        )

    return [
        ToolSpec(
            name="skill_upsert",
            description=(
                "Create or update a profile-scoped procedural skill. Use this "
                "only for reusable SOPs, methods, or operating instructions; "
                "ordinary facts belong in memory or the knowledge base."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "body": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "overwrite": {"type": "boolean", "default": True},
                },
                "required": ["name", "description", "body"],
            },
            handler=_upsert,
            metadata={"group": "skill.store", "surface": "skills", "access": "write"},
        )
    ]


@tool_provider_registry.register("skills")
class _SkillsToolProvider:
    def build(self, **services: Any) -> list[ToolSpec]:
        s = services.get("skills_service")
        if s is None:
            raise ValueError("skills tool provider requires 'skills_service'")
        return build_skills_tools(s)


__all__ = ["build_skills_tools", "build_skills_store_tools"]
