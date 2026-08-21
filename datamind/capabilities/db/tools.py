"""DB tools exposed to the agent."""
from __future__ import annotations

from typing import Any

from datamind.core.tools import ToolSpec, tool_provider_registry

from .service import DBService


def build_db_tools(db: DBService) -> list[ToolSpec]:
    async def _list_tables() -> dict:
        return {"tables": await db.list_tables()}

    async def _describe(table: str) -> dict:
        schema = await db.describe(table)
        return schema.model_dump()

    async def _query_sql(sql: str) -> dict:
        result = await db.query_sql(sql)
        payload = result.model_dump()
        payload["returned_count"] = len(result.rows)
        payload["total_count"] = None
        payload["next_cursor"] = None
        return payload

    async def _query_nl(question: str, tables: list[str] | None = None) -> dict:
        return await db.query_nl(question, tables=tables)

    return [
        ToolSpec(
            name="db_list_tables",
            description="List the tables available in the active SQL database.",
            input_schema={"type": "object", "properties": {}},
            handler=_list_tables,
            metadata={"group": "db", "surface": "db", "access": "read"},
        ),
        ToolSpec(
            name="db_describe_table",
            description=(
                "Describe a table: column names, types, primary keys, and an estimated row count. "
                "Use this before writing SQL by hand so you reference existing columns."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "Table name."},
                },
                "required": ["table"],
            },
            handler=_describe,
            metadata={"group": "db", "surface": "db", "access": "read"},
        ),
        ToolSpec(
            name="db_query_sql",
            description=(
                "Execute a single read-only SELECT against the database. "
                "The runtime enforces a row limit and rejects anything that looks like DML/DDL. "
                "Prefer this when you already know the SQL you want."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single SELECT statement."},
                },
                "required": ["sql"],
            },
            handler=_query_sql,
            metadata={"group": "db", "surface": "db", "access": "read"},
        ),
        ToolSpec(
            name="db_query_nl",
            description=(
                "Answer a question against the SQL database. The runtime generates a SELECT from your question "
                "and returns the rows plus the generated SQL. Prefer this when you don't know the schema cold."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Natural-language question."},
                    "tables": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional subset of tables to focus on.",
                    },
                },
                "required": ["question"],
            },
            handler=_query_nl,
            metadata={"group": "db", "surface": "db", "access": "read"},
        ),
    ]


@tool_provider_registry.register("db")
class _DBToolProvider:
    def build(self, **services: Any) -> list[ToolSpec]:
        db = services.get("db_service")
        if db is None:
            raise ValueError("db tool provider requires 'db_service'")
        return build_db_tools(db)


__all__ = ["build_db_tools"]
