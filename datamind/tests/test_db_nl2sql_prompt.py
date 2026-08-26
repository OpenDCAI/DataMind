from __future__ import annotations

import pytest

from datamind.capabilities.db.nl2sql import generate_sql
from datamind.capabilities.db.tools import build_db_tools
from datamind.core.protocols import ColumnSchema, TableSchema


class _RecordingClient:
    def __init__(self) -> None:
        self.prompt = ""

    async def generate_text(self, prompt: str, **_kwargs):
        self.prompt = prompt
        return "SELECT 1"


@pytest.mark.asyncio
async def test_nl2sql_prompt_defines_numeric_text_empty_and_unit_semantics():
    client = _RecordingClient()
    await generate_sql(
        client=client,
        model="test",
        question="Which value is largest?",
        schemas=[TableSchema(
            name="metrics",
            columns=[ColumnSchema(name="value", type="TEXT")],
            row_count_estimate=2,
        )],
        dialect_name="sqlite",
    )

    assert "do not rely on lexicographic ordering" in client.prompt
    assert "Unicode-whitespace-only strings" in client.prompt
    assert "does not mean fractional values should be multiplied by 100" in client.prompt
    assert "Do not invent a numeric encoding" in client.prompt


def test_db_tool_descriptions_expose_same_semantic_safeguards():
    tools = {tool.name: tool.description for tool in build_db_tools(object())}
    assert "cast explicitly" in tools["db_query_sql"]
    assert "preserve stored percentage scale" in tools["db_query_sql"]
    assert "numeric casts" in tools["db_query_nl"]
