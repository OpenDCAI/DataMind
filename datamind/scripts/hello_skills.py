"""End-to-end skills smoke test.

Discovers SKILL.md manifests under `.claude/skills/`, builds a semantic
index against the live gateway embeddings, runs a search, fetches full
bodies, and exercises the code skills (calculator, unit_convert).

Usage:
    DATAMIND__LLM__API_KEY=sk-...
    DATAMIND__EMBEDDING__API_BASE=https://gateway.example.com
    DATAMIND__EMBEDDING__API_KEY=sk-...
    python -m datamind.scripts.hello_skills
"""
from __future__ import annotations

import asyncio
import json
import os
import sys


async def _main() -> int:
    # Mirror LLM__* onto EMBEDDING__* when the latter isn't set — common
    # case for the unified gateway.
    for src, dst in (
        ("DATAMIND__LLM__API_BASE", "DATAMIND__EMBEDDING__API_BASE"),
        ("DATAMIND__LLM__API_KEY", "DATAMIND__EMBEDDING__API_KEY"),
    ):
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]

    os.environ.setdefault("DATAMIND__DATA__PROFILE", "hello_skills_demo")

    if not os.environ.get("DATAMIND__LLM__API_KEY"):
        print("[hello_skills] DATAMIND__LLM__API_KEY not set", file=sys.stderr)
        return 1

    from datamind.capabilities.skills import build_skills_service, build_skills_tools
    from datamind.config import Settings
    from datamind.core.logging import setup_logging
    from datamind.core.tools import ToolRegistry

    setup_logging("INFO")
    settings = Settings()
    settings.ensure_dirs()

    skills = build_skills_service(settings)
    info = await skills.load()
    print(f"[hello_skills] loaded: {info}")

    tools = ToolRegistry()
    tools.extend(build_skills_tools(skills))
    print(f"[hello_skills] tools: {tools.names()}")

    # --- list ---
    print("\n[hello_skills] skill_list:")
    print(json.dumps(await tools.get("skill_list").handler(), indent=2, ensure_ascii=False))

    # --- semantic search (real embeddings) ---
    for q in ["how should I review a pull request?", "慢查询怎么排查"]:
        print(f"\n[hello_skills] skill_search: {q!r}")
        out = await tools.get("skill_search").handler(query=q, top_k=2)
        for h in out["results"]:
            print(f"  score={h['score']:.3f}  name={h['name']}  desc={h['description']!r}")

    # --- direct get ---
    print("\n[hello_skills] skill_get: code-review")
    got = await tools.get("skill_get").handler(name="code-review")
    print(f"  found={got['found']}  body_preview={got['body'][:80]!r}")

    # --- code skills ---
    calc = await tools.get("calculator").handler(expression="2 * (3 + sqrt(16))")
    print(f"\n[hello_skills] calculator(2*(3+sqrt(16))) = {calc['result']}")

    conv = await tools.get("unit_convert").handler(value=100, from_unit="c", to_unit="f")
    print(f"[hello_skills] 100°C -> {conv['result']}°F")

    print("\n[hello_skills] OK")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
