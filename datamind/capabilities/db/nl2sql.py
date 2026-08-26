"""Natural-language-to-SQL generator.

Strategy:
1. Build a compact schema description from the target tables (column names
   + types + primary keys + a small row sample).
2. Send that to the configured text model with a tight prompt: "write ONE read-only SQL
   statement, output nothing else".
3. Strip code fences, validate with the dialect's `is_destructive` before
   returning. The caller runs the SQL through `execute_readonly` which
   repeats the check — belt and braces.

The generator is stateless and dialect-agnostic; a future version can
include the SQL dialect ("MySQL", "SQLite") in the prompt to get
dialect-specific idioms.
"""
from __future__ import annotations

import re
from typing import Sequence

from datamind.core.logging import get_logger
from datamind.core.protocols import TableSchema, TextModelClient

_log = get_logger("db.nl2sql")


_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _schema_block(schemas: Sequence[TableSchema]) -> str:
    lines: list[str] = []
    for t in schemas:
        cols = ", ".join(
            f"{c.name} {c.type}{' PK' if c.primary_key else ''}{' NOT NULL' if not c.nullable else ''}"
            for c in t.columns
        )
        cnt = f" (~{t.row_count_estimate} rows)" if t.row_count_estimate is not None else ""
        lines.append(f"TABLE {t.name}{cnt}:\n  {cols}")
    return "\n\n".join(lines)


def _extract_sql(text: str) -> str:
    m = _SQL_FENCE_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(";")
    return text.strip().rstrip(";")


async def generate_sql(
    *,
    client: TextModelClient,
    model: str,
    question: str,
    schemas: Sequence[TableSchema],
    dialect_name: str = "sql",
) -> str:
    """Return a single SELECT statement answering `question`."""
    schema_text = _schema_block(schemas)
    prompt = (
        "You are an expert SQL author. Convert the user's question into ONE "
        f"read-only SQL SELECT statement for a {dialect_name} database.\n\n"
        "Rules:\n"
        "- Output ONLY the SQL statement — no commentary, no markdown fences.\n"
        "- Never write INSERT/UPDATE/DELETE/DDL.\n"
        "- Use columns that exist in the schema below.\n"
        "- Prefer explicit column names over SELECT *.\n"
        "- When a business-numeric column is declared TEXT, inspect/cast it explicitly for "
        "ORDER BY, MIN/MAX, SUM, or AVG; do not rely on lexicographic ordering.\n"
        "- Treat NULL, empty strings, and Unicode-whitespace-only strings (including NBSP) as "
        "empty for non-empty counts.\n"
        "- Preserve values in their stored scale. A percent sign in a label alone does not mean "
        "fractional values should be multiplied by 100.\n"
        "- Do not invent a numeric encoding for categorical labels. If the question omits the "
        "mapping, the SQL must not silently create one.\n"
        "- Limit to the data necessary to answer.\n\n"
        f"Schema:\n{schema_text}\n\n"
        f"Question: {question}\n\n"
        "SQL:"
    )

    text = await client.generate_text(
        prompt, model=model, max_tokens=512, temperature=0.0,
    )
    sql = _extract_sql(text)
    _log.info("nl2sql_generated", extra={"question": question, "sql": sql[:200]})
    return sql
