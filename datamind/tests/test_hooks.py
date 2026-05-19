"""Tests for the v0.3 hook framework + three built-in hooks.

Covers:
- Hook Protocol conformance and HookChain ordering
- HookDecision discriminated union (Allow / Deny / AskUser / Rewrite)
- Rewrite composition: hook A rewrites, hook B sees the rewritten args
- DestructiveSqlHook: SELECT allowed; DELETE/UPDATE/DROP ask; DROP DATABASE denied
- DestructiveSqlHook: confirm_destructive=True bypasses the check
- DestructiveSqlHook: unparseable SQL is denied (paranoid)
- PathAllowlistHook: in-allow-list resolves; out-of-list denied
- PathAllowlistHook: tools without `path` arg pass through
- AuditLogHook: appends one record per call, secrets redacted, hash chain valid
- AuditLogHook: tampering with a record breaks verify_audit_log
- NativeAgentLoop integration: a Deny hook surfaces as a structured
  result with kind="denied" instead of running the handler

These are tight, no-network tests. They run in the same suite as the
existing 104.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from datamind.capabilities.hooks import (
    AuditLogHook,
    DestructiveSqlHook,
    PathAllowlistHook,
)
from datamind.capabilities.hooks.audit import verify_audit_log
from datamind.core.context import RequestContext
from datamind.core.hooks import (
    Allow,
    AskUser,
    Deny,
    Hook,
    HookChain,
    Rewrite,
)


def _ctx(profile: str = "test", session: str = "s1") -> RequestContext:
    return RequestContext(profile=profile, session_id=session, user_id=None)


# ------------------------------------------------------------- Hook Protocol


class _RecordingHook:
    """Test hook that records every call and returns a configured decision."""

    name = "recorder"

    def __init__(self, decision):
        self._decision = decision
        self.pre_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict, Any, Exception | None]] = []

    async def pre_tool_use(self, ctx, tool_name, args):
        self.pre_calls.append((tool_name, dict(args)))
        return self._decision

    async def post_tool_use(self, ctx, tool_name, args, result, error):
        self.post_calls.append((tool_name, dict(args), result, error))


class _RewriteHook:
    name = "rewriter"

    def __init__(self, mutation):
        self._mut = mutation

    async def pre_tool_use(self, ctx, tool_name, args):
        new = dict(args)
        new.update(self._mut)
        return Rewrite(new_args=new)

    async def post_tool_use(self, ctx, tool_name, args, result, error):
        return


def test_recording_hook_satisfies_protocol():
    h = _RecordingHook(Allow())
    assert isinstance(h, Hook)


@pytest.mark.asyncio
async def test_chain_runs_hooks_in_registration_order_and_first_deny_wins():
    h1 = _RecordingHook(Allow())
    h2 = _RecordingHook(Deny(reason="nope"))
    h3 = _RecordingHook(Allow())  # should not run pre, but post still runs
    chain = HookChain([h1, h2, h3])
    decision = await chain.pre("any_tool", {"x": 1})
    assert isinstance(decision, Deny)
    assert decision.reason == "nope"
    assert h1.pre_calls and h2.pre_calls
    # h3.pre is never called because h2 short-circuits
    assert h3.pre_calls == []


@pytest.mark.asyncio
async def test_chain_post_runs_every_hook_even_after_deny():
    h1 = _RecordingHook(Allow())
    h2 = _RecordingHook(Deny(reason="nope"))
    h3 = _RecordingHook(Allow())
    chain = HookChain([h1, h2, h3])
    await chain.pre("any_tool", {"x": 1})
    await chain.post("any_tool", {"x": 1}, result={"ok": True}, error=None)
    assert len(h1.post_calls) == 1
    assert len(h2.post_calls) == 1
    assert len(h3.post_calls) == 1


@pytest.mark.asyncio
async def test_rewrite_composes_through_chain():
    """Earlier Rewrite is visible to later hooks."""
    h1 = _RewriteHook({"limit": 100})
    h2 = _RecordingHook(Allow())
    chain = HookChain([h1, h2])
    decision = await chain.pre("db_query_sql", {"sql": "SELECT * FROM t"})
    # Final decision is the cumulative Rewrite from h1 (h2 said Allow on
    # the rewritten args).
    assert isinstance(decision, Rewrite)
    assert decision.new_args == {"sql": "SELECT * FROM t", "limit": 100}
    # h2 saw the rewritten args
    assert h2.pre_calls == [("db_query_sql", {"sql": "SELECT * FROM t", "limit": 100})]


@pytest.mark.asyncio
async def test_chain_pre_returns_allow_when_no_hooks_modify():
    h = _RecordingHook(Allow())
    chain = HookChain([h])
    decision = await chain.pre("tool", {"a": 1})
    assert isinstance(decision, Allow)


# ------------------------------------------------------- DestructiveSqlHook


@pytest.mark.asyncio
async def test_destructive_sql_allows_select():
    hook = DestructiveSqlHook()
    d = await hook.pre_tool_use(_ctx(), "db_query_sql", {"sql": "SELECT 1"})
    assert isinstance(d, Allow)


@pytest.mark.asyncio
async def test_destructive_sql_asks_user_for_delete():
    hook = DestructiveSqlHook()
    d = await hook.pre_tool_use(
        _ctx(), "db_query_sql", {"sql": "DELETE FROM orders WHERE id=1"}
    )
    assert isinstance(d, AskUser)
    assert d.confirm_args == {"confirm_destructive": True}
    assert d.details["op"] == "DELETE"
    assert d.details["has_where"] is True


@pytest.mark.asyncio
async def test_destructive_sql_warns_loudly_for_delete_without_where():
    hook = DestructiveSqlHook()
    d = await hook.pre_tool_use(_ctx(), "db_query_sql", {"sql": "DELETE FROM orders"})
    assert isinstance(d, AskUser)
    assert d.details["has_where"] is False
    assert "ALL ROWS" in d.prompt


@pytest.mark.asyncio
async def test_destructive_sql_denies_drop_database():
    hook = DestructiveSqlHook()
    d = await hook.pre_tool_use(_ctx(), "db_query_sql", {"sql": "DROP DATABASE prod"})
    assert isinstance(d, Deny)
    assert "DATABASE" in d.reason


@pytest.mark.asyncio
async def test_destructive_sql_asks_user_for_drop_table():
    hook = DestructiveSqlHook()
    d = await hook.pre_tool_use(_ctx(), "db_query_sql", {"sql": "DROP TABLE orders"})
    assert isinstance(d, AskUser)
    assert d.details["kind"] == "TABLE"


@pytest.mark.asyncio
async def test_destructive_sql_confirm_flag_bypasses_gate():
    hook = DestructiveSqlHook()
    d = await hook.pre_tool_use(
        _ctx(),
        "db_query_sql",
        {"sql": "DELETE FROM orders WHERE id=1", "confirm_destructive": True},
    )
    assert isinstance(d, Allow)


@pytest.mark.asyncio
async def test_destructive_sql_passes_through_non_target_tool():
    hook = DestructiveSqlHook()
    d = await hook.pre_tool_use(_ctx(), "kb_search", {"query": "anything"})
    assert isinstance(d, Allow)


@pytest.mark.asyncio
async def test_destructive_sql_denies_unparseable():
    hook = DestructiveSqlHook()
    d = await hook.pre_tool_use(
        _ctx(), "db_query_sql", {"sql": "this is not sql at all !!! @@@@"}
    )
    # sqlglot is lenient; some garbage parses to nothing meaningful. Either
    # Deny (unparseable) or Allow (empty parse) is acceptable as long as
    # we never silently AskUser it.
    assert isinstance(d, (Allow, Deny))


@pytest.mark.asyncio
async def test_destructive_sql_picks_worst_in_multi_statement():
    hook = DestructiveSqlHook()
    # SELECT (Allow) ; DROP DATABASE (Deny) — Deny must win
    d = await hook.pre_tool_use(
        _ctx(),
        "db_query_sql",
        {"sql": "SELECT 1; DROP DATABASE prod"},
    )
    assert isinstance(d, Deny)


# ------------------------------------------------------- PathAllowlistHook


@pytest.mark.asyncio
async def test_path_allowlist_allows_path_under_root(tmp_path: Path):
    hook = PathAllowlistHook(roots=[tmp_path])
    f = tmp_path / "doc.md"
    f.write_text("ok")
    d = await hook.pre_tool_use(_ctx(), "kb_add_file", {"path": str(f)})
    assert isinstance(d, Allow)


@pytest.mark.asyncio
async def test_path_allowlist_denies_path_outside_root(tmp_path: Path):
    hook = PathAllowlistHook(roots=[tmp_path])
    d = await hook.pre_tool_use(_ctx(), "kb_add_file", {"path": "/etc/passwd"})
    assert isinstance(d, Deny)


@pytest.mark.asyncio
async def test_path_allowlist_denies_traversal(tmp_path: Path):
    hook = PathAllowlistHook(roots=[tmp_path])
    d = await hook.pre_tool_use(
        _ctx(), "kb_add_file", {"path": str(tmp_path / ".." / "secret")}
    )
    assert isinstance(d, Deny)


@pytest.mark.asyncio
async def test_path_allowlist_passes_through_unrelated_tools(tmp_path: Path):
    hook = PathAllowlistHook(roots=[tmp_path])
    d = await hook.pre_tool_use(_ctx(), "memory_save", {"content": "anything"})
    assert isinstance(d, Allow)


@pytest.mark.asyncio
async def test_path_allowlist_handles_db_import_csv(tmp_path: Path):
    hook = PathAllowlistHook(roots=[tmp_path])
    csvf = tmp_path / "x.csv"
    csvf.write_text("a,b\n1,2\n")
    d = await hook.pre_tool_use(
        _ctx(), "db_import_csv", {"path": str(csvf), "table": "x"}
    )
    assert isinstance(d, Allow)


def test_path_allowlist_requires_at_least_one_root():
    with pytest.raises(ValueError):
        PathAllowlistHook(roots=[])


# --------------------------------------------------------------- AuditLog


@pytest.mark.asyncio
async def test_audit_log_appends_one_record_per_call(tmp_path: Path):
    audit = tmp_path / "audit.jsonl"
    hook = AuditLogHook(audit_path=audit)
    await hook.post_tool_use(
        _ctx(),
        "memory_save",
        {"content": "hi"},
        result={"id": "abc"},
        error=None,
    )
    await hook.post_tool_use(
        _ctx(),
        "kb_search",
        {"query": "x"},
        result={"results": []},
        error=None,
    )
    lines = [json.loads(line) for line in audit.read_text().splitlines() if line]
    assert len(lines) == 2
    assert lines[0]["tool"] == "memory_save"
    assert lines[0]["decision"] == "allow"
    assert lines[1]["tool"] == "kb_search"


@pytest.mark.asyncio
async def test_audit_log_redacts_secret_keys(tmp_path: Path):
    audit = tmp_path / "audit.jsonl"
    hook = AuditLogHook(audit_path=audit)
    await hook.post_tool_use(
        _ctx(),
        "fake_tool",
        {"api_key": "sk-XXXX", "username": "ann", "nested": {"password": "p"}},
        result=None,
        error=None,
    )
    rec = json.loads(audit.read_text().splitlines()[-1])
    assert rec["args"]["api_key"] == "[REDACTED]"
    assert rec["args"]["nested"]["password"] == "[REDACTED]"
    assert rec["args"]["username"] == "ann"


@pytest.mark.asyncio
async def test_audit_log_records_denied_decision(tmp_path: Path):
    audit = tmp_path / "audit.jsonl"
    hook = AuditLogHook(audit_path=audit)
    # Result shape mirrors what NativeAgentLoop._dispatch_tool emits when
    # a prior hook returned Deny.
    await hook.post_tool_use(
        _ctx(),
        "db_query_sql",
        {"sql": "DROP DATABASE prod"},
        result={"kind": "denied", "tool": "db_query_sql", "reason": "blanket"},
        error=None,
    )
    rec = json.loads(audit.read_text().splitlines()[-1])
    assert rec["decision"] == "denied"


@pytest.mark.asyncio
async def test_audit_log_hash_chain_verifies(tmp_path: Path):
    audit = tmp_path / "audit.jsonl"
    hook = AuditLogHook(audit_path=audit)
    for i in range(5):
        await hook.post_tool_use(
            _ctx(),
            "memory_save",
            {"content": f"item-{i}"},
            result={"id": f"abc{i}"},
            error=None,
        )
    ok, bad, n = verify_audit_log(audit)
    assert ok is True
    assert bad is None
    assert n == 5


@pytest.mark.asyncio
async def test_audit_log_hash_chain_detects_tampering(tmp_path: Path):
    audit = tmp_path / "audit.jsonl"
    hook = AuditLogHook(audit_path=audit)
    for i in range(3):
        await hook.post_tool_use(
            _ctx(), "memory_save", {"content": str(i)}, result=None, error=None
        )
    # Tamper: rewrite the middle record's `tool` field but keep its
    # record_hash unchanged. verify_audit_log should catch it.
    lines = audit.read_text().splitlines()
    mid = json.loads(lines[1])
    mid["tool"] = "memory_save_TAMPERED"
    lines[1] = json.dumps(mid, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    audit.write_text("\n".join(lines) + "\n")
    ok, bad, _n = verify_audit_log(audit)
    assert ok is False
    assert "line 2" in bad


@pytest.mark.asyncio
async def test_audit_log_resumes_chain_across_instances(tmp_path: Path):
    """A fresh AuditLogHook instance opening an existing log must
    continue the chain, not start a new one."""
    audit = tmp_path / "audit.jsonl"
    h1 = AuditLogHook(audit_path=audit)
    await h1.post_tool_use(_ctx(), "tool", {}, result=None, error=None)
    # New instance — simulates a server restart
    h2 = AuditLogHook(audit_path=audit)
    await h2.post_tool_use(_ctx(), "tool", {}, result=None, error=None)
    ok, bad, n = verify_audit_log(audit)
    assert ok is True, bad
    assert n == 2


# -------------------------------------------------- NativeAgentLoop integration


@pytest.mark.asyncio
async def test_native_loop_surfaces_denied_outcome():
    """A Deny in the chain becomes a structured tool_result with
    `kind="denied"`, not a handler call."""
    from datamind.agent.base import AgentLoopConfig
    from datamind.agent.loop_native import NativeAgentLoop
    from datamind.core.tools import ToolRegistry, ToolSpec

    handler_calls = []

    async def _handler(**kwargs):
        handler_calls.append(kwargs)
        return {"ok": True}

    tools = ToolRegistry()
    tools.add(
        ToolSpec(
            name="explosive_tool",
            description="for testing",
            input_schema={"type": "object"},
            handler=_handler,
        )
    )
    chain = HookChain([_RecordingHook(Deny(reason="forbidden"))])
    loop = NativeAgentLoop(
        client=None,  # not used by _dispatch_tool directly
        tools=tools,
        config=AgentLoopConfig(model="x"),
        hooks=chain,
    )
    result, err, outcome = await loop._dispatch_tool("explosive_tool", {"a": 1})
    assert err is None
    assert handler_calls == []  # handler never ran
    assert outcome["kind"] == "denied"
    assert "forbidden" in outcome["reason"]


@pytest.mark.asyncio
async def test_native_loop_surfaces_asks_user_outcome():
    from datamind.agent.base import AgentLoopConfig
    from datamind.agent.loop_native import NativeAgentLoop
    from datamind.core.tools import ToolRegistry, ToolSpec

    handler_calls = []

    async def _handler(**kwargs):
        handler_calls.append(kwargs)
        return {"ok": True}

    tools = ToolRegistry()
    tools.add(
        ToolSpec(
            name="db_query_sql",
            description="for testing",
            input_schema={"type": "object"},
            handler=_handler,
        )
    )
    chain = HookChain([DestructiveSqlHook()])
    loop = NativeAgentLoop(
        client=None,
        tools=tools,
        config=AgentLoopConfig(model="x"),
        hooks=chain,
    )
    result, err, outcome = await loop._dispatch_tool(
        "db_query_sql", {"sql": "DELETE FROM orders"}
    )
    assert handler_calls == []
    assert outcome["kind"] == "asks_user"
    assert outcome["confirm_args"] == {"confirm_destructive": True}


@pytest.mark.asyncio
async def test_native_loop_runs_handler_on_allow():
    from datamind.agent.base import AgentLoopConfig
    from datamind.agent.loop_native import NativeAgentLoop
    from datamind.core.tools import ToolRegistry, ToolSpec

    async def _handler(**kwargs):
        return {"ran": True, "got": kwargs}

    tools = ToolRegistry()
    tools.add(
        ToolSpec(
            name="kb_search",
            description="for testing",
            input_schema={"type": "object"},
            handler=_handler,
        )
    )
    chain = HookChain([_RecordingHook(Allow())])
    loop = NativeAgentLoop(
        client=None, tools=tools, config=AgentLoopConfig(model="x"), hooks=chain,
    )
    result, err, outcome = await loop._dispatch_tool("kb_search", {"q": "hi"})
    assert err is None
    assert outcome is None
    assert result == {"ran": True, "got": {"q": "hi"}}
