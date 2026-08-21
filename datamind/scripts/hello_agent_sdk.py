"""SDK-backed twin of `hello_agent.py`.

Runs the *same* 4 questions, against the *same* seeded profile (KB/DB/Graph),
but with `claude-agent-sdk` driving the loop instead of our self-written
`AgentLoop`. The 23 DataMind tools (kb_*, db_*, graph_*, skill_*, memory_*)
are bridged to the SDK via an in-process MCP server — zero business-logic
rewrite, just a thin adapter that wraps each `ToolSpec.handler`.

Pre-req: `claude-code-router` (CCR) must be running, e.g.
    HOME=/tmp/ccr-home node vendor/claude-code-router/.../index.js
with config pointing at the OpenAI-compatible model gateway. See
hello_sdk_native.py for the bootstrap recipe.

Usage:
    export CCR_BASE=http://127.0.0.1:13456
    export DATAMIND__LLM__API_KEY=sk-...        # used for embeddings
    export DATAMIND__LLM__API_BASE=...          # used for embeddings
    python -m datamind.scripts.hello_agent_sdk

Side-by-side vs hello_agent.py: the same questions, the same tool catalogue,
the same gateway. Only the agent loop differs.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from typing import Any

from sqlalchemy import text


# ============================================================ ToolSpec → SDK adapter


def _spec_to_sdk_tool(spec):
    """Wrap a DataMind ToolSpec into an SDK SdkMcpTool.

    The SDK expects:
        @tool(name, description, input_schema_dict)
        async def handler(args: dict) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": "..."}]}

    DataMind handlers are `async def handler(**kwargs) -> JSON-serialisable`,
    so we just splat `args` and JSON-stringify the result into the MCP shape.
    """
    from claude_agent_sdk import tool

    schema = spec.input_schema or {"type": "object", "properties": {}}

    @tool(spec.name, spec.description, schema)
    async def _wrapped(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await spec.handler(**(args or {}))
        except Exception as exc:  # surface tool errors as is_error blocks
            return {
                "content": [{"type": "text", "text": f"[{type(exc).__name__}] {exc}"}],
                "isError": True,
            }
        if isinstance(result, (dict, list)):
            payload = json.dumps(result, ensure_ascii=False, default=str)
        else:
            payload = str(result)
        return {"content": [{"type": "text", "text": payload}]}

    return _wrapped


# ============================================================ main


async def _main() -> int:
    # Mirror LLM__* -> EMBEDDING__* (embeddings still go through the original
    # gateway via our own anthropic-SDK-based EmbeddingProvider — this script
    # only swaps the *agent loop*, not the embedding path).
    for src, dst in (
        ("DATAMIND__LLM__API_BASE", "DATAMIND__EMBEDDING__API_BASE"),
        ("DATAMIND__LLM__API_KEY", "DATAMIND__EMBEDDING__API_KEY"),
    ):
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]

    os.environ.setdefault("DATAMIND__DATA__PROFILE", "hello_agent_sdk_demo")
    os.environ.setdefault("DATAMIND__RETRIEVAL__STRATEGY", "hybrid")

    if not os.environ.get("DATAMIND__LLM__API_KEY"):
        print("[hello_agent_sdk] DATAMIND__LLM__API_KEY not set", file=sys.stderr)
        return 1

    ccr_base = os.environ.get("CCR_BASE", "http://127.0.0.1:13456")
    ccr_key = os.environ.get("CCR_APIKEY") or "dummy"
    model = os.environ.get("DATAMIND__LLM__MODEL", "claude-sonnet-4-6")

    print(f"[hello_agent_sdk] router={ccr_base}  model={model}")

    from datamind.agent import build_datamind
    from datamind.config import Settings
    from datamind.core.logging import setup_logging

    setup_logging("WARNING")
    settings = Settings()
    settings.ensure_dirs()
    data_dir = settings.data.data_dir
    storage_dir = settings.data.storage_dir

    # Reset the demo profile every run.
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

    # Same seed data as hello_agent.py — easier comparison.
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

    receipt = await system.store.tools.get("kb_reindex").handler()
    print(f"[hello_agent_sdk] KB receipt: {receipt}")
    print(
        f"[hello_agent_sdk] Tools: retrieve={len(system.retrieve.tools)} "
        f"store={len(system.store.tools)}"
    )

    # Bridge every DataMind ToolSpec into one SDK MCP server.
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        create_sdk_mcp_server,
        query,
    )

    questions = [
        "我们公司的 Status meeting 是什么时候开？",
        "Search platform 团队负责人是谁？他在哪个城市？",
        "工程部 Shanghai 的员工工资加起来是多少？",
        "帮我记住：我下周三会议不能参加，请调到周四。",
    ]

    summary: list[dict[str, Any]] = []

    for q in questions:
        print(f"\n[Q] {q}")
        agent = system.store if q.startswith("帮我记住") else system.retrieve
        sdk_tools = [_spec_to_sdk_tool(agent.tools.get(n)) for n in agent.tools.names()]
        mcp_server = create_sdk_mcp_server("datamind", tools=sdk_tools)
        allowed = [f"mcp__datamind__{n}" for n in agent.tools.names()]
        system_prompt = agent.loop._cfg.system_prompt
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt,
            mcp_servers={"datamind": mcp_server},
            allowed_tools=allowed,
            disallowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch"],
            permission_mode="bypassPermissions",
            max_turns=12,
            env={
                "ANTHROPIC_BASE_URL": ccr_base,
                "ANTHROPIC_API_KEY": ccr_key,
                "ANTHROPIC_AUTH_TOKEN": ccr_key,
                "DISABLE_TELEMETRY": "1",
                "DISABLE_AUTOUPDATER": "1",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "NO_PROXY": "127.0.0.1,localhost",
            },
            load_timeout_ms=30000,
        )

        t0 = time.monotonic()
        tool_calls: list[tuple[str, dict]] = []
        text_parts: list[str] = []
        result_summary: dict[str, Any] = {}

        try:
            async for msg in query(prompt=q, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            short_name = block.name.removeprefix("mcp__datamind__")
                            tool_calls.append((short_name, block.input))
                elif isinstance(msg, ResultMessage):
                    result_summary = {
                        "subtype": msg.subtype,
                        "is_error": msg.is_error,
                        "duration_ms": msg.duration_ms,
                        "num_turns": getattr(msg, "num_turns", None),
                        "cost_usd": msg.total_cost_usd,
                    }
                elif isinstance(msg, SystemMessage):
                    if msg.subtype in ("api_error", "api_retry"):
                        print(f"  ! system {msg.subtype}: {getattr(msg, 'data', None)}")
        except Exception as exc:
            print(f"  ! ERROR {type(exc).__name__}: {exc}")
            summary.append({"q": q, "error": str(exc)})
            continue

        elapsed = time.monotonic() - t0
        for name, inp in tool_calls:
            short = json.dumps(inp, ensure_ascii=False)[:160]
            print(f"  · tool  {name}  {short}")
        answer = "".join(text_parts).strip() or "<no text>"
        print(f"[A] {answer}")
        print(f"[meta] elapsed={elapsed:.1f}s tools={len(tool_calls)} turns={result_summary.get('num_turns')}")

        summary.append({
            "q": q,
            "answer": answer,
            "tool_calls": [name for name, _ in tool_calls],
            "elapsed_s": round(elapsed, 2),
            **result_summary,
        })

    print("\n" + "=" * 70)
    print("[hello_agent_sdk] summary table")
    print("=" * 70)
    for i, row in enumerate(summary, 1):
        print(f"  Q{i}: {row['q'][:40]}...")
        if "error" in row:
            print(f"      ERROR: {row['error']}")
            continue
        print(f"      tools: {' → '.join(row['tool_calls']) or '<none>'}")
        print(f"      elapsed: {row['elapsed_s']}s  turns: {row.get('num_turns')}")
    print("\n[hello_agent_sdk] OK")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
