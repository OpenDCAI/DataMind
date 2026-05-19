"""End-to-end hooks demo: see Phase 8 hooks in action.

Three short scenarios run against an in-memory profile:

  1. Path allow-list — kb_add_file('/etc/passwd') → Deny
  2. Destructive SQL — db_query_sql('DELETE FROM employees') → AskUser
  3. Confirmation flow — agent re-issues with confirm_destructive=True → Allow

After all three, we read the audit.jsonl and verify its hash chain.
The point is to show what shows up in the audit log including the
denied / asks_user records.

Usage:
    DATAMIND__LLM__API_BASE=...
    DATAMIND__LLM__API_KEY=...
    python -m datamind.scripts.hello_hooks
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path


async def _main() -> int:
    for src, dst in (
        ("DATAMIND__LLM__API_BASE", "DATAMIND__EMBEDDING__API_BASE"),
        ("DATAMIND__LLM__API_KEY", "DATAMIND__EMBEDDING__API_KEY"),
    ):
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]
    os.environ.setdefault("DATAMIND__DATA__PROFILE", "hello_hooks_demo")

    if not os.environ.get("DATAMIND__LLM__API_KEY"):
        print("[hello_hooks] DATAMIND__LLM__API_KEY not set", file=sys.stderr)
        return 1

    from datamind.capabilities.hooks import (
        AuditLogHook,
        DestructiveSqlHook,
        PathAllowlistHook,
    )
    from datamind.capabilities.hooks.audit import verify_audit_log
    from datamind.config import Settings
    from datamind.core.context import RequestContext
    from datamind.core.hooks import Allow, AskUser, Deny, HookChain, Rewrite
    from datamind.core.logging import bind_context, setup_logging

    setup_logging("INFO")
    settings = Settings()
    settings.ensure_dirs()

    # Clean previous audit log so the demo is reproducible.
    audit_path = settings.data.storage_dir / "audit.jsonl"
    if audit_path.exists():
        audit_path.unlink()

    print(f"[hello_hooks] profile     = {settings.data.profile}")
    print(f"[hello_hooks] audit log   = {audit_path}")

    chain = HookChain([
        PathAllowlistHook(roots=[settings.data.data_dir, Path.cwd()]),
        DestructiveSqlHook(),
        AuditLogHook(audit_path=audit_path),
    ])
    print(f"[hello_hooks] hooks       = {chain.names()}")

    ctx = RequestContext.new(profile=settings.data.profile)
    with bind_context(ctx):
        # ----- Scenario 1: path outside allowlist -------------------------
        print("\n[hello_hooks] scenario 1: kb_add_file('/etc/passwd')")
        decision = await chain.pre("kb_add_file", {"path": "/etc/passwd"})
        await chain.post(
            "kb_add_file",
            {"path": "/etc/passwd"},
            result={"kind": "denied", "reason": getattr(decision, "reason", "")},
            error=None,
        )
        print(f"  → decision = {type(decision).__name__}")
        if isinstance(decision, Deny):
            print(f"    reason   = {decision.reason}")

        # ----- Scenario 2: destructive SQL without confirmation -----------
        print("\n[hello_hooks] scenario 2: db_query_sql('DELETE FROM employees')")
        sql = "DELETE FROM employees WHERE department = 'sales'"
        decision = await chain.pre("db_query_sql", {"sql": sql})
        await chain.post(
            "db_query_sql",
            {"sql": sql},
            result={
                "kind": "asks_user",
                "prompt": getattr(decision, "prompt", ""),
            },
            error=None,
        )
        print(f"  → decision = {type(decision).__name__}")
        if isinstance(decision, AskUser):
            print(f"    prompt   = {decision.prompt[:120]}")
            print(f"    confirm  = {decision.confirm_args}")

        # ----- Scenario 3: same SQL, with confirmation --------------------
        print("\n[hello_hooks] scenario 3: same SQL with confirm_destructive=True")
        confirmed = {"sql": sql, "confirm_destructive": True}
        decision = await chain.pre("db_query_sql", confirmed)
        await chain.post(
            "db_query_sql", confirmed, result={"affected_rows": 42}, error=None
        )
        print(f"  → decision = {type(decision).__name__}")

        # ----- Scenario 4: a benign call for chain coverage ---------------
        print("\n[hello_hooks] scenario 4: kb_search('quarterly revenue')")
        decision = await chain.pre("kb_search", {"query": "quarterly revenue"})
        await chain.post(
            "kb_search",
            {"query": "quarterly revenue"},
            result={"hits": [{"score": 0.7}]},
            error=None,
        )
        print(f"  → decision = {type(decision).__name__}")

    # ----- Audit log review ---------------------------------------------
    print("\n[hello_hooks] audit.jsonl contents:")
    for i, line in enumerate(audit_path.read_text().splitlines(), start=1):
        rec = json.loads(line)
        print(
            f"  {i}. {rec['ts']} {rec['tool']:20s} "
            f"decision={rec['decision']:10s} "
            f"prev={rec['prev_hash']}"
        )

    ok, bad, n = verify_audit_log(audit_path)
    if ok:
        print(f"\n[hello_hooks] audit chain verified: {n} record(s), no tampering detected")
    else:
        print(f"\n[hello_hooks] AUDIT VERIFY FAILED: {bad}", file=sys.stderr)
        return 2

    print("\n[hello_hooks] OK")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
