"""Deterministic, no-network verification of the real SQLite data path.

This is intentionally smaller than ``hello_db``: it does not need an LLM
gateway. It creates a temporary profile, seeds a real SQLite database through
the same ``DBService`` used by DataMind, exercises the public DB tools, and
checks that destructive SQL is rejected.

Run:

    python -m datamind.scripts.verify_sqlite_demo
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sqlalchemy import text

from datamind.capabilities.db import build_db_service, build_db_tools
from datamind.capabilities.db.safeguard import DestructiveSQLError
from datamind.config import Settings
from datamind.core.tools import ToolRegistry


async def run_demo(work_dir: Path) -> dict[str, Any]:
    """Run the SQLite checks under ``work_dir`` and return a JSON summary."""
    settings = Settings(llm={"api_key": "offline-demo"})
    settings.data.base_dir = work_dir
    settings.data.profile = "sqlite_demo"
    settings.db.dialect = "sqlite"
    settings.db.dsn = None
    settings.db.read_only = True
    settings.ensure_dirs()

    db = build_db_service(settings)
    try:
        with db.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE employees (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    department TEXT NOT NULL,
                    salary INTEGER NOT NULL,
                    city TEXT NOT NULL
                )
            """))
            conn.execute(text("""
                CREATE TABLE projects (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    lead_id INTEGER NOT NULL,
                    budget INTEGER NOT NULL
                )
            """))
            conn.execute(text("""
                INSERT INTO employees (id, name, department, salary, city) VALUES
                    (1, 'Ann', 'Engineering', 15000, 'Shanghai'),
                    (2, 'Bob', 'Engineering', 14000, 'Shanghai'),
                    (3, 'Cam', 'Sales', 9000, 'Beijing')
            """))
            conn.execute(text("""
                INSERT INTO projects (id, name, lead_id, budget) VALUES
                    (1, 'Search', 1, 100000),
                    (2, 'Copilot', 2, 250000)
            """))

        tools = ToolRegistry()
        tools.extend(build_db_tools(db))
        tables = await tools.get("db_list_tables").handler()
        schema = await tools.get("db_describe_table").handler(table="employees")
        totals = await tools.get("db_query_sql").handler(
            sql=(
                "SELECT department, SUM(salary) AS total_salary "
                "FROM employees GROUP BY department ORDER BY department"
            )
        )

        try:
            await tools.get("db_query_sql").handler(sql="DELETE FROM employees")
        except DestructiveSQLError:
            destructive_rejected = True
        else:  # pragma: no cover - a failure of the safety contract
            destructive_rejected = False

        summary = {
            "status": "ok" if destructive_rejected else "failed",
            "profile": settings.data.profile,
            "database": str(settings.data.storage_dir / "demo.db"),
            "tables": tables["tables"],
            "employee_columns": [column["name"] for column in schema["columns"]],
            "salary_totals": totals["rows"],
            "destructive_sql_rejected": destructive_rejected,
        }
        if summary["tables"] != ["employees", "projects"]:
            summary["status"] = "failed"
        if summary["salary_totals"] != [["Engineering", 29000], ["Sales", 9000]]:
            summary["status"] = "failed"
        return summary
    finally:
        await db.aclose()


async def _main() -> int:
    with TemporaryDirectory(prefix="datamind-sqlite-demo-") as directory:
        summary = await run_demo(Path(directory))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["status"] != "ok":
        print("[verify_sqlite_demo] FAILED", file=sys.stderr)
        return 1
    print("[verify_sqlite_demo] OK")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
