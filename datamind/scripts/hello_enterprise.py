"""End-to-end integration test against the enterprise_demo profile.

Runs a fixed list of complex, cross-backend questions and reports the tool
sequence + final answer for each. Designed to be run with both agent
loops to confirm the SSE event protocol stays identical across backends.

Usage:
    # Run against the default `native` backend
    DATAMIND__DATA__PROFILE=enterprise_demo \
      python -m datamind.scripts.hello_enterprise

    # Or against the SDK + CCR backend (CCR must already be running)
    DATAMIND__DATA__PROFILE=enterprise_demo \
    DATAMIND__AGENT__BACKEND=sdk \
    DATAMIND__AGENT__CCR_BASE_URL=http://127.0.0.1:13456 \
      python -m datamind.scripts.hello_enterprise

The questions are designed so the right answer requires reaching into
multiple backends:
- KB (employee handbook + runbooks + tech docs + policies)
- DB (employees + projects + incidents + performance_reviews + …)
- Graph (org hierarchy + project deps + incident → service)
- Memory (StoreAgent write in turn 8)
- Ingest (StoreAgent write in turn 7)

Pre-req: enterprise_demo profile must be seeded:
    python -m datamind.scripts.seed_enterprise_demo
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time


QUESTIONS: list[str] = [
    # 1. KB-only — multi-doc semantic retrieval
    "公司的发布窗口是什么时候？发布前的检查清单有哪些？",

    # 2. Cross-backend: incidents + performance review
    "2025 Q4 跟 Search Platform 相关的事故有哪几起？影响时长加起来多久？参与响应的工程师在 2025-H2 绩效都怎么样？",

    # 3. Graph multi-hop
    "AI Copilot 依赖哪些产品？这些被依赖的产品的负责人是谁？分别在哪个城市？",

    # 4. DB aggregation across joins
    "去年 2025 年绩效高于 4.0 的工程师，他们现在负责的 in_progress 项目是什么？项目预计什么时候交付？",

    # 5. KB + DB + Graph combo
    "Frank 这个人的所有信息都告诉我：他在哪个团队、汇报给谁、在城市哪、负责过哪些项目、参与过哪些事故、绩效如何？",

    # 6. Skills tool integration check
    "代码审查的最佳实践有哪些关键点？",

    # 7. Ingest a new doc via conversation
    "我想把这段加到知识库里：'2026 年 5 月起，所有工程师每周三下午 14:00-17:00 强制无会议，作为 deep work 时段。'",

    # 8. Memory write-then-recall
    "帮我记住：我下周一全天请假去医院体检。",
]


async def _main() -> int:
    # Mirror credentials into the embedding env (the embedding provider
    # falls back to LLM creds when its own slot is empty — this keeps the
    # script runnable from a single .env.datamind).
    for src, dst in (
        ("DATAMIND__LLM__API_BASE", "DATAMIND__EMBEDDING__API_BASE"),
        ("DATAMIND__LLM__API_KEY", "DATAMIND__EMBEDDING__API_KEY"),
    ):
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]

    os.environ.setdefault("DATAMIND__DATA__PROFILE", "enterprise_demo")

    from datamind.agent import build_datamind
    from datamind.config import Settings
    from datamind.core.logging import setup_logging

    setup_logging("WARNING")
    settings = Settings()
    settings.ensure_dirs()
    backend = settings.agent.backend

    print(f"[hello_enterprise] backend = {backend}")
    print(f"[hello_enterprise] profile = {settings.data.profile}")
    print(f"[hello_enterprise] model   = {settings.llm.model}")
    if backend == "sdk":
        print(f"[hello_enterprise] ccr     = {settings.agent.ccr_base_url}")

    system = await build_datamind(settings)
    await system.warmup()
    print(
        f"[hello_enterprise] tools   = "
        f"retrieve:{len(system.retrieve.tools)} store:{len(system.store.tools)}"
    )

    # Sanity check: profile must already be seeded.
    kb_count = await system.services.kb.count()
    if kb_count == 0:
        print(
            "\n[hello_enterprise] FATAL: KB is empty. Seed first:\n"
            "    python -m datamind.scripts.seed_enterprise_demo",
            file=sys.stderr,
        )
        return 2

    summary: list[dict] = []
    histories: dict[str, list[dict]] = {"retrieve": [], "store": []}

    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n[Q{i}] {q}")
        t0 = time.monotonic()
        text_parts: list[str] = []
        tool_calls: list[tuple[str, str]] = []
        errors: list[str] = []
        role = "store" if i in {7, 8} else "retrieve"
        agent = system.store if role == "store" else system.retrieve
        history = histories[role]

        async for ev in agent.loop.stream_turn(user_message=q, history=history):
            if ev.type == "text":
                text_parts.append(ev.data["delta"])
            elif ev.type == "tool_use":
                # Trim input to keep the printout readable.
                inp = json.dumps(ev.data.get("input", {}), ensure_ascii=False)[:120]
                tool_calls.append((ev.data["name"], inp))
                print(f"  · {ev.data['name']}  {inp}")
            elif ev.type == "tool_result":
                if ev.data.get("is_error"):
                    name = ev.data.get("name", "?")
                    preview = (ev.data.get("preview") or "")[:200]
                    errors.append(f"{name}: {preview}")
                    print(f"  ! ERROR {name}: {preview}")
            elif ev.type == "error":
                errors.append(ev.data.get("message", "?"))

        elapsed = time.monotonic() - t0
        ans = "".join(text_parts).strip() or "<empty>"
        # Print the final answer (capped — full answer goes into summary).
        preview = ans if len(ans) <= 240 else ans[:240] + " …"
        print(f"[A{i}] {preview}")
        print(f"      {len(tool_calls)} tools · {elapsed:.1f}s · {len(errors)} errors")

        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": ans})
        summary.append({
            "q": q,
            "role": role,
            "tools": [t[0] for t in tool_calls],
            "tool_count": len(tool_calls),
            "elapsed_s": round(elapsed, 1),
            "errors": errors,
            "answer_preview": preview,
        })

    # Final tabular summary
    print("\n" + "=" * 78)
    print(f"[hello_enterprise] backend={backend} summary")
    print("=" * 78)
    print(f"{'#':<3} {'tools':<6} {'time':<7} {'tool sequence':<55}")
    print("-" * 78)
    for i, row in enumerate(summary, 1):
        seq = " → ".join(row["tools"]) or "<no tools>"
        if len(seq) > 55:
            seq = seq[:52] + "..."
        flag = "❌" if row["errors"] else "✓"
        print(f"{i:<3} {row['tool_count']:<6} {row['elapsed_s']:<7} {seq:<55} {flag}")

    failed = sum(1 for r in summary if not r["answer_preview"] or r["answer_preview"] == "<empty>")
    recoverable_errors = sum(1 for r in summary if r["errors"] and r["answer_preview"] != "<empty>")
    print("-" * 78)
    print(f"{len(summary)} questions · {sum(r['tool_count'] for r in summary)} tool calls · "
          f"{failed} hard failures · {recoverable_errors} with recoverable tool errors · "
          f"total {sum(r['elapsed_s'] for r in summary):.1f}s")

    # Dump full summary as JSON for downstream comparison.
    out_path = f"/tmp/hello_enterprise_{backend}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "backend": backend,
            "profile": settings.data.profile,
            "model": settings.llm.model,
            "questions": summary,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n[hello_enterprise] full summary written to {out_path}")

    return 0 if failed == 0 else 1


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
