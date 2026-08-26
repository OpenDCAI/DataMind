"""End-to-end agent smoke test.

This is the real deal: seeds a profile with a small KB + SQLite + graph,
then asks the agent real questions and watches it pick the right tool.

Usage:
    DATAMIND__LLM__API_BASE=https://gateway.example.com
    DATAMIND__LLM__API_KEY=sk-...
    python -m datamind.scripts.hello_agent
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
    # One unified gateway: mirror LLM__* -> EMBEDDING__* for convenience.
    for src, dst in (
        ("DATAMIND__LLM__API_BASE", "DATAMIND__EMBEDDING__API_BASE"),
        ("DATAMIND__LLM__API_KEY", "DATAMIND__EMBEDDING__API_KEY"),
    ):
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]

    os.environ.setdefault("DATAMIND__DATA__PROFILE", "hello_agent_demo")
    os.environ.setdefault("DATAMIND__RETRIEVAL__STRATEGY", "hybrid")

    if not os.environ.get("DATAMIND__LLM__API_KEY"):
        print("[hello_agent] DATAMIND__LLM__API_KEY not set", file=sys.stderr)
        return 1

    from datamind.agent import build_datamind
    from datamind.config import Settings
    from datamind.core.logging import setup_logging

    setup_logging("WARNING")  # keep the narration clean
    settings = Settings()
    settings.ensure_dirs()
    data_dir = settings.data.data_dir
    storage_dir = settings.data.storage_dir

    # Reset the demo profile every run so the output is reproducible.
    for p in list(data_dir.iterdir()) if data_dir.exists() else []:
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    for name in ("chroma", "chroma_skills", "demo.db", "graph.json", "memory.db"):
        t = storage_dir / name
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        elif t.exists():
            t.unlink()

    # Seed KB
    (data_dir / "company_handbook.md").write_text(
        "# Acme Company Handbook\n\n"
        "## Meeting policy\n"
        "Status meetings happen every Monday at 14:00 Shanghai time.\n\n"
        "## Engineering team\n"
        "The Search platform is owned by Ann (团队负责人). Bob works on retrieval quality.\n\n"
        "## Coffee\n"
        "The office espresso bar serves oat milk by default.\n",
        encoding="utf-8",
    )
    (data_dir / "runbook.md").write_text(
        "# Incident Runbook\n\n"
        "When the retrieval API p99 latency crosses 500ms, page the on-call "
        "engineer (Ann) and check the hybrid retriever cache hit rate.\n",
        encoding="utf-8",
    )

    # Seed graph triples
    (data_dir / "triplets").mkdir(exist_ok=True)
    (data_dir / "triplets" / "company.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in [
            {"subject": "Ann", "relation": "leads", "object": "Search platform"},
            {"subject": "Bob", "relation": "member_of", "object": "Search platform"},
            {"subject": "Search platform", "relation": "part_of", "object": "Acme Engineering"},
            {"subject": "Acme Engineering", "relation": "located_in", "object": "Shanghai"},
        ]),
        encoding="utf-8",
    )

    system = await build_datamind(settings)
    await system.warmup()

    # Seed SQLite (via the agent's engine)
    with system.services.db.engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY,
                name TEXT, department TEXT, salary INTEGER, city TEXT
            )"""))
        conn.execute(text("DELETE FROM employees"))
        conn.execute(text("""
            INSERT INTO employees VALUES
              (1, 'Ann', 'Eng', 15000, 'Shanghai'),
              (2, 'Bob', 'Eng', 11000, 'Shanghai'),
              (3, 'Cam', 'Sales', 9000, 'Beijing')
        """))

    # Re-index KB now that docs exist
    receipt = await system.store.tools.get("kb_reindex").handler()
    print(f"[hello_agent] KB receipt: {receipt}")
    print(f"[hello_agent] Graph stats: {system.services.graph.stats()}")
    print(f"[hello_agent] Retrieve tools: {system.retrieve.tools.names()}")
    print(f"[hello_agent] Store tools: {system.store.tools.names()}\n")

    questions = [
        "我们公司的 Status meeting 是什么时候开？",
        "Search platform 团队负责人是谁？他在哪个城市？",
        "工程部 Shanghai 的员工工资加起来是多少？",
        "帮我记住：我下周三会议不能参加，请调到周四。",
    ]

    retrieve_history: list[dict] = []
    store_history: list[dict] = []
    for q in questions:
        print(f"\n[Q] {q}")
        is_store = q.startswith("帮我记住")
        agent = system.store if is_store else system.retrieve
        history = store_history if is_store else retrieve_history
        collected_text = []
        tool_calls = []
        async for event in agent.loop.stream_turn(user_message=q, history=history):
            if event.type == "text":
                collected_text.append(event.data["delta"])
            elif event.type == "tool_use":
                tool_calls.append((event.data["name"], event.data["input"]))
            elif event.type == "done":
                answer = "".join(collected_text).strip()
                for name, inp in tool_calls:
                    short = json.dumps(inp, ensure_ascii=False)[:160]
                    print(f"  · tool  {name}  {short}")
                print(f"[A] {answer}")
                history.append({"role": "user", "content": q})
                history.append({"role": "assistant", "content": answer or "…"})

    print("\n[hello_agent] OK")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
