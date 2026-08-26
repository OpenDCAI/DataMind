"""End-to-end DB smoke test.

Creates a small SQLite demo DB under a throwaway profile, exercises:
- db_list_tables / db_describe_table
- db_query_sql (success + destructive-rejection)
- db_query_nl (live gateway call)

Usage:
    DATAMIND__LLM__API_BASE=https://gateway.example.com
    DATAMIND__LLM__API_KEY=sk-...
    python -m datamind.scripts.hello_db
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from sqlalchemy import text


async def _main() -> int:
    os.environ.setdefault("DATAMIND__DATA__PROFILE", "hello_db_demo")
    os.environ.setdefault("DATAMIND__DB__DIALECT", "sqlite")
    # Explicitly clear any DSN so we use the per-profile default.
    os.environ.pop("DATAMIND__DB__DSN", None)

    if not os.environ.get("DATAMIND__LLM__API_KEY"):
        print("[hello_db] DATAMIND__LLM__API_KEY not set", file=sys.stderr)
        return 1

    from anthropic import AsyncAnthropic

    from datamind.capabilities.db import build_db_service, build_db_tools
    from datamind.capabilities.db.safeguard import DestructiveSQLError
    from datamind.config import Settings
    from datamind.core.logging import setup_logging
    from datamind.core.tools import ToolRegistry

    setup_logging("INFO")
    settings = Settings()
    settings.ensure_dirs()

    storage = settings.data.storage_dir
    demo_db = storage / "demo.db"
    # Start fresh each run.
    if demo_db.exists():
        demo_db.unlink()
    print(f"[hello_db] profile     = {settings.data.profile}")
    print(f"[hello_db] dialect     = {settings.db.dialect}")
    print(f"[hello_db] demo db     = {demo_db}")

    client = AsyncAnthropic(
        base_url=str(settings.llm.api_base),
        api_key=settings.llm.api_key.get_secret_value(),
    )
    db = build_db_service(settings, llm_client=client)

    # Seed the demo database via the SAME engine that the service holds.
    with db.engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE employees (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                department TEXT,
                salary INTEGER,
                city TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                name TEXT,
                lead_id INTEGER,
                budget INTEGER
            )
        """))
        conn.execute(text("""
            INSERT INTO employees (id, name, department, salary, city) VALUES
              (1, 'Ann',   'Eng',   12000, 'Shanghai'),
              (2, 'Bob',   'Eng',   11000, 'Shanghai'),
              (3, 'Cam',   'Sales', 9000,  'Beijing'),
              (4, 'Dee',   'HR',    8500,  'Shenzhen'),
              (5, 'Evan',  'Eng',   15000, 'Shanghai')
        """))
        conn.execute(text("""
            INSERT INTO projects (id, name, lead_id, budget) VALUES
              (1, 'Alpha', 1, 100000),
              (2, 'Beta',  5, 250000),
              (3, 'Gamma', 3, 50000)
        """))

    tools = ToolRegistry()
    tools.extend(build_db_tools(db))
    print(f"[hello_db] tools       = {tools.names()}")

    # --- db_list_tables ---
    tables = await tools.get("db_list_tables").handler()
    print(f"\n[hello_db] tables      = {tables}")

    # --- db_describe_table ---
    employees_schema = await tools.get("db_describe_table").handler(table="employees")
    print("\n[hello_db] describe employees:")
    print(json.dumps(employees_schema, indent=2, default=str))

    # --- db_query_sql (plain SQL) ---
    sql_result = await tools.get("db_query_sql").handler(
        sql="SELECT department, COUNT(*) AS n, AVG(salary) AS avg_salary FROM employees GROUP BY department ORDER BY n DESC"
    )
    print("\n[hello_db] db_query_sql (group by department):")
    print(json.dumps(sql_result, indent=2, default=str))

    # --- safeguard: destructive SQL must be rejected ---
    try:
        await tools.get("db_query_sql").handler(sql="DELETE FROM employees")
    except DestructiveSQLError as exc:
        print(f"\n[hello_db] destructive rejected OK: {exc}")
    else:
        print("\n[hello_db] FAIL: destructive SQL was not rejected", file=sys.stderr)
        return 1

    # --- db_query_nl (live gateway) ---
    nl_q = "How many engineers are based in Shanghai, and what's their total salary?"
    nl_result = await tools.get("db_query_nl").handler(question=nl_q)
    print("\n[hello_db] db_query_nl:")
    print(f"  question: {nl_result['question']}")
    print(f"  generated SQL: {nl_result['sql']}")
    print(f"  columns: {nl_result['columns']}")
    print(f"  rows: {nl_result['rows']}")

    # --- anthropic schema snapshot ---
    schema = tools.as_anthropic_tools()
    print(f"\n[hello_db] anthropic tool count = {len(schema)}")

    print("\n[hello_db] OK")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
