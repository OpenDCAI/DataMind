"""End-to-end memory smoke test.

Exercises the 3-layer memory:
- Short-term append & recent()
- Long-term save via embedding recall (real gateway)
- Fact extraction via the live LLM
- Tool-dispatch path

Usage:
    DATAMIND__LLM__API_BASE=https://gateway.example.com
    DATAMIND__LLM__API_KEY=sk-...
    python -m datamind.scripts.hello_memory
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys


async def _main() -> int:
    for src, dst in (
        ("DATAMIND__LLM__API_BASE", "DATAMIND__EMBEDDING__API_BASE"),
        ("DATAMIND__LLM__API_KEY", "DATAMIND__EMBEDDING__API_KEY"),
    ):
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]

    os.environ.setdefault("DATAMIND__DATA__PROFILE", "hello_memory_demo")

    if not os.environ.get("DATAMIND__LLM__API_KEY"):
        print("[hello_memory] DATAMIND__LLM__API_KEY not set", file=sys.stderr)
        return 1

    from anthropic import AsyncAnthropic

    from datamind.capabilities.memory import build_memory_service, build_memory_tools
    from datamind.config import Settings
    from datamind.core.logging import setup_logging
    from datamind.core.tools import ToolRegistry

    setup_logging("INFO")
    settings = Settings()
    settings.ensure_dirs()

    # Clear prior run so results are deterministic.
    memdb = settings.data.storage_dir / "memory.db"
    if memdb.exists():
        memdb.unlink()

    client = AsyncAnthropic(
        base_url=str(settings.llm.api_base),
        api_key=settings.llm.api_key.get_secret_value(),
    )
    memory = build_memory_service(settings, llm_client=client)
    print(f"[hello_memory] profile = {settings.data.profile}")

    tools = ToolRegistry()
    tools.extend(build_memory_tools(memory))
    print(f"[hello_memory] tools   = {tools.names()}")

    # --- Short-term (per-session) ---
    await memory.append_turn("demo", "user", "I prefer Mondays for meetings.")
    await memory.append_turn("demo", "assistant", "Got it, Mondays it is.")
    await memory.append_turn("demo", "user", "Also I like oat milk in coffee.")
    recent = await memory.recent_turns("demo", limit=5)
    print(f"\n[hello_memory] short-term turns = {len(recent)}")
    for t in recent:
        print(f"  [{t['role']}] {t['content'][:60]}")

    # --- Long-term (explicit saves, scope='profile' is the default) ---
    print("\n[hello_memory] memory_save (explicit facts):")
    for fact in [
        "The user's name is Ann.",
        "Ann leads the Search platform team at Acme.",
        "Ann's preferred meeting day is Monday afternoon.",
        "Ann takes oat milk in her coffee.",
    ]:
        out = await tools.get("memory_save").handler(content=fact)
        print(f"  saved id={out['id']}: {fact}")

    # --- Long-term semantic recall ---
    for q in [
        "What do we know about Ann's coffee preference?",
        "When does Ann like to meet?",
        "Which team does Ann lead?",
    ]:
        out = await tools.get("memory_recall").handler(query=q, top_k=2)
        print(f"\n[hello_memory] memory_recall: {q!r}")
        for h in out["results"]:
            print(f"  score={h['score']:.3f}  {h['content']}")

    # --- Fact extraction via the live LLM, written to scope='session' ---
    extracted = await memory.extract_and_save(
        user_turn="I'm moving to Shenzhen next month and will start jogging in the morning.",
        assistant_turn="Nice — Shenzhen in April is great for outdoor runs.",
        scope="session",
        session_id="demo",
    )
    print(f"\n[hello_memory] extracted {len(extracted)} fact(s) from a turn:")
    for f in extracted:
        print(f"  - {f}")

    # --- Confirm they're recallable (session-scoped recall) ---
    post = await memory.recall("where is the user moving", session_id="demo", top_k=3)
    print("\n[hello_memory] recall 'where is the user moving':")
    for h in post:
        print(f"  score={h['score']:.3f}  {h['content']}")

    # --- Profiles seen by the store ---
    ns = await tools.get("memory_list_profiles").handler()
    print(f"\n[hello_memory] profiles = {ns['profiles']}")

    print("\n[hello_memory] OK")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
