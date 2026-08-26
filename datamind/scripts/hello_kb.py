"""End-to-end KB smoke test against the live gateway.

Set env vars and run:
    DATAMIND__LLM__API_BASE=https://gateway.example.com
    DATAMIND__LLM__API_KEY=sk-...
    DATAMIND__EMBEDDING__API_BASE=https://gateway.example.com
    DATAMIND__EMBEDDING__API_KEY=sk-...
    DATAMIND__DATA__PROFILE=demo_kb

    python -m datamind.scripts.hello_kb

Builds a tiny in-profile corpus, indexes it, then runs a search. Exits 0
on success. Useful as a pre-flight for Phase 7 agent integration.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path


_REQUIRED = (
    "DATAMIND__LLM__API_KEY",
    "DATAMIND__EMBEDDING__API_KEY",
)


async def _main() -> int:
    # Allow overrides but supply sensible defaults from LLM_* so users only
    # need to set one set of vars against a unified gateway.
    for src, dst in (
        ("DATAMIND__LLM__API_BASE", "DATAMIND__EMBEDDING__API_BASE"),
        ("DATAMIND__LLM__API_KEY", "DATAMIND__EMBEDDING__API_KEY"),
    ):
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]

    missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if missing:
        print("[hello_kb] missing env vars:", ", ".join(missing), file=sys.stderr)
        return 1

    # Force a disposable profile so we don't touch real data.
    os.environ.setdefault("DATAMIND__DATA__PROFILE", "hello_kb_demo")
    os.environ.setdefault("DATAMIND__EMBEDDING__PROVIDER", "openai_compatible")
    os.environ.setdefault("DATAMIND__EMBEDDING__MODEL", "text-embedding-3-small")
    os.environ.setdefault("DATAMIND__RETRIEVAL__STRATEGY", "hybrid")
    os.environ.setdefault("DATAMIND__RETRIEVAL__TOP_K", "3")
    os.environ.setdefault("DATAMIND__RETRIEVAL__CHUNK_SIZE", "256")
    os.environ.setdefault("DATAMIND__RETRIEVAL__CHUNK_OVERLAP", "32")

    from anthropic import AsyncAnthropic

    from datamind.capabilities.kb import build_kb_service
    from datamind.capabilities.kb.indexer import build_index
    from datamind.config import Settings
    from datamind.core.logging import setup_logging

    setup_logging("INFO")
    settings = Settings()
    settings.ensure_dirs()

    # Seed the profile with a toy corpus.
    data_dir = settings.data.data_dir
    for p in list(data_dir.iterdir()) if data_dir.exists() else []:
        if p.is_file():
            p.unlink()
        else:
            shutil.rmtree(p, ignore_errors=True)
    (data_dir / "ai_history.md").write_text(
        """# AI history

Artificial intelligence research began in the 1950s. The Dartmouth workshop in
1956 is often considered the founding event of the field.

Expert systems dominated the 1980s, but struggled with scaling and brittleness.

Deep learning, powered by GPUs and massive datasets, revolutionised the field
after 2012. Large language models like GPT and Claude emerged in the 2020s.
""",
        encoding="utf-8",
    )
    (data_dir / "rag.md").write_text(
        """# Retrieval-Augmented Generation

RAG combines a retriever (often a vector index) with a generator LLM. The
retriever fetches relevant documents; the LLM conditions on them to answer.

Hybrid retrieval mixes dense vector search with BM25 or other lexical signals.
Reciprocal Rank Fusion is a common, hyperparameter-light merging strategy.
""",
        encoding="utf-8",
    )

    # Wipe any stale vectors from a previous run of the demo.
    storage_chroma = settings.data.storage_dir / "chroma"
    if storage_chroma.exists():
        shutil.rmtree(storage_chroma, ignore_errors=True)

    client = AsyncAnthropic(
        base_url=str(settings.llm.api_base),
        api_key=settings.llm.api_key.get_secret_value(),
    )
    kb = build_kb_service(settings, llm_client=client)
    print(f"[hello_kb] profile = {settings.data.profile}")
    print(f"[hello_kb] strategy = {settings.retrieval.strategy}")

    stats = await kb.reindex()
    print(f"[hello_kb] indexed: {stats}")

    queries = [
        "When did AI research start?",
        "What is reciprocal rank fusion?",
        "How does hybrid search combine sparse and dense signals?",
    ]
    for q in queries:
        print(f"\n[hello_kb] Q: {q}")
        results = await kb.search(q, top_k=2)
        for r in results:
            text = r["text"].replace("\n", " ")[:120]
            print(f"  - score={r['score']:.3f} source={r['source']!r}  {text!r}")

    count = await kb.count()
    print(f"\n[hello_kb] total chunks in store: {count}")
    print("[hello_kb] OK")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
