"""Memory capability tests (no network)."""
from __future__ import annotations

from typing import Sequence

import pytest

from datamind.capabilities.memory import (
    MemoryService,
    ShortTermMemory,
    build_memory_tools,
)
from datamind.capabilities.memory.providers.sqlite_store import SQLiteMemoryStore
from datamind.core.protocols import MemoryStore


class _FakeEmbed:
    """Deterministic embedding: char-bag based, 8 dims.

    Similar strings get similar vectors without needing a real model.
    """

    name = "fake"
    dimension = 8

    @staticmethod
    def _vec(text: str) -> list[float]:
        buckets = [0.0] * 8
        for ch in text.lower():
            buckets[ord(ch) % 8] += 1.0
        total = sum(buckets) or 1.0
        return [b / total for b in buckets]

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, query: str) -> list[float]:
        return self._vec(query)


# ---------------------------------------------------------------- short-term


@pytest.mark.asyncio
async def test_short_term_fifo_respects_max():
    st = ShortTermMemory(max_turns=3)
    await st.append("s", "user", "a")
    await st.append("s", "user", "b")
    await st.append("s", "user", "c")
    await st.append("s", "user", "d")
    recent = await st.recent("s")
    assert [t.content for t in recent] == ["b", "c", "d"]


@pytest.mark.asyncio
async def test_short_term_clear():
    st = ShortTermMemory(max_turns=3)
    await st.append("s", "user", "a")
    n = await st.clear("s")
    assert n == 1
    assert await st.recent("s") == []


# ---------------------------------------------------------------- long-term


@pytest.mark.asyncio
async def test_sqlite_store_satisfies_protocol(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    assert isinstance(s, MemoryStore)


@pytest.mark.asyncio
async def test_save_and_recall_with_embedding(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    await s.save("user likes coffee", scope="profile", profile="A")
    await s.save("user dislikes onions", scope="profile", profile="A")
    await s.save("meeting at 3pm on Friday", scope="profile", profile="A")
    hits = await s.recall("coffee", profile="A", top_k=2)
    assert hits and hits[0].content.startswith("user likes coffee")


@pytest.mark.asyncio
async def test_scope_isolation_profile_to_profile(tmp_path):
    """Profile-A memories must NOT leak into profile-B recall."""
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    await s.save("ARR means annual review report", scope="profile", profile="lawfirm")
    await s.save("ARR means annual recurring revenue", scope="profile", profile="saas")

    a_hits = await s.recall("what does ARR mean", profile="lawfirm", top_k=5)
    b_hits = await s.recall("what does ARR mean", profile="saas", top_k=5)

    assert len(a_hits) == 1 and "review report" in a_hits[0].content
    assert len(b_hits) == 1 and "recurring revenue" in b_hits[0].content


@pytest.mark.asyncio
async def test_global_scope_visible_to_every_profile(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    await s.save("respond in Chinese", scope="global")
    await s.save("project A uses Postgres", scope="profile", profile="A")
    await s.save("project B uses MySQL", scope="profile", profile="B")

    # Querying within profile A should see global + profile-A only.
    a_hits = await s.recall("respond", profile="A", top_k=5)
    contents = {h.content for h in a_hits}
    assert "respond in Chinese" in contents
    assert "project B uses MySQL" not in contents


@pytest.mark.asyncio
async def test_session_scope_isolated_within_profile(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    await s.save("use reviewer perspective", scope="session", session_id="paper-2026")
    await s.save("use compiler perspective", scope="session", session_id="kernel-debug")

    paper_hits = await s.recall("perspective", session_id="paper-2026", top_k=5)
    contents = {h.content for h in paper_hits}
    assert "use reviewer perspective" in contents
    assert "use compiler perspective" not in contents


@pytest.mark.asyncio
async def test_recall_unions_all_three_scopes(tmp_path):
    """A single recall call returns global + profile + session items."""
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    await s.save("global rule", scope="global")
    await s.save("profile-A rule", scope="profile", profile="A")
    await s.save("session-X rule", scope="session", session_id="X")
    await s.save("session-Y rule", scope="session", session_id="Y")  # different session
    await s.save("profile-B rule", scope="profile", profile="B")  # different profile

    hits = await s.recall("rule", profile="A", session_id="X", top_k=10)
    contents = {h.content for h in hits}
    assert "global rule" in contents
    assert "profile-A rule" in contents
    assert "session-X rule" in contents
    assert "session-Y rule" not in contents
    assert "profile-B rule" not in contents


@pytest.mark.asyncio
async def test_lexical_recall_when_no_embedding(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=None)
    await s.save("the capital of France is Paris", scope="global")
    await s.save("pasta recipe requires tomatoes and basil", scope="global")
    hits = await s.recall("paris france", top_k=2)
    assert hits
    assert "Paris" in hits[0].content


@pytest.mark.asyncio
async def test_save_validates_scope_arguments(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=None)
    with pytest.raises(Exception):
        # scope='profile' without profile= must error out
        await s.save("oops", scope="profile")
    with pytest.raises(Exception):
        await s.save("oops", scope="session")


@pytest.mark.asyncio
async def test_soft_delete_archives_and_hides_from_recall(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    rid = await s.save("temporary fact", scope="profile", profile="A")

    # Visible before forget
    pre = await s.recall("temporary", profile="A", top_k=5)
    assert any(h.id == rid for h in pre)

    ok = await s.forget(rid)  # soft by default
    assert ok

    # Hidden from default recall
    post = await s.recall("temporary", profile="A", top_k=5)
    assert not any(h.id == rid for h in post)

    # Still in the store, just archived
    assert await s.count(scope="profile", profile="A", include_archived=True) == 1
    assert await s.count(scope="profile", profile="A", include_archived=False) == 0


@pytest.mark.asyncio
async def test_hard_delete_removes_row(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    rid = await s.save("to drop", scope="profile", profile="A")
    ok = await s.forget(rid, hard=True)
    assert ok
    assert await s.count(scope="profile", profile="A", include_archived=True) == 0


@pytest.mark.asyncio
async def test_kind_filter(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    await s.save("prefer concise answers", scope="profile", profile="A", kind="preference")
    await s.save("decided to use Postgres", scope="profile", profile="A", kind="decision")
    await s.save("just a fact", scope="profile", profile="A", kind="fact")

    # All three kinds visible by default
    all_hits = await s.recall("answer", profile="A", top_k=10)
    assert len(all_hits) == 3

    # Filter to decisions only
    only_decisions = await s.recall("answer", profile="A", top_k=10, kinds=["decision"])
    assert len(only_decisions) == 1
    assert only_decisions[0].kind == "decision"


@pytest.mark.asyncio
async def test_list_profiles(tmp_path):
    s = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    await s.save("a", scope="profile", profile="A")
    await s.save("b", scope="profile", profile="B")
    await s.save("g", scope="global")  # should not appear
    profiles = await s.list_profiles()
    assert set(profiles) == {"A", "B"}


@pytest.mark.asyncio
async def test_v1_to_v2_migration(tmp_path):
    """Existing v0.2 'memory' table should auto-migrate into 'memory_v2'."""
    import sqlite3
    import time

    db_path = tmp_path / "legacy.db"
    # Simulate a v0.2 database state
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE memory (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT,
                embedding BLOB,
                created_at REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO memory VALUES (?,?,?,?,?,?)",
            ("legacy-1", "session:s1", "old fact", "{}", None, time.time()),
        )

    # Opening the v0.3 store should silently migrate.
    s = SQLiteMemoryStore(db_path=str(db_path), embedding=None)

    # Migrated row is now scope='profile', profile='session:s1'
    hits = await s.recall("old", profile="session:s1", top_k=5)
    assert hits and hits[0].content == "old fact"

    # Old table is gone
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory'"
        )
        assert cur.fetchone() is None


# ---------------------------------------------------------------- service


@pytest.mark.asyncio
async def test_service_combines_short_and_long(tmp_path):
    st = ShortTermMemory(max_turns=5)
    lt = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    svc = MemoryService(short_term=st, long_term=lt, default_profile="demo")

    await svc.append_turn("s1", "user", "Hi there")
    await svc.append_turn("s1", "assistant", "Hello")
    turns = await svc.recent_turns("s1")
    assert len(turns) == 2

    rid = await svc.save("the user prefers Monday meetings")
    hits = await svc.recall("when does user like to meet")
    assert hits and "Monday" in hits[0]["content"]
    assert await svc.forget(rid)


@pytest.mark.asyncio
async def test_memory_recall_scope_filter_excludes_unselected_scopes(tmp_path):
    '''A global-only recall must not reintroduce the default profile.'''
    lt = SQLiteMemoryStore(db_path=str(tmp_path / 'm.db'), embedding=_FakeEmbed())
    svc = MemoryService(
        short_term=ShortTermMemory(max_turns=5),
        long_term=lt,
        default_profile='acme',
    )
    tools = {tool.name: tool.handler for tool in build_memory_tools(svc)}

    await tools['memory_save']('global fact', scope='global')
    await tools['memory_save']('profile fact', scope='profile', profile='acme')
    await tools['memory_save'](
        'session fact', scope='session', session_id='chat-1'
    )

    result = await tools['memory_recall'](
        'fact',
        profile='acme',
        session_id='chat-1',
        scope_filter=['global'],
    )

    assert result['count'] == 1
    assert result['results'][0]['scope'] == 'global'
    assert result['results'][0]['content'] == 'global fact'


@pytest.mark.asyncio
async def test_service_default_profile_used_when_omitted(tmp_path):
    """save without explicit profile should land under default_profile."""
    st = ShortTermMemory(max_turns=5)
    lt = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"), embedding=_FakeEmbed())
    svc = MemoryService(short_term=st, long_term=lt, default_profile="acme")

    await svc.save("acme-only fact")
    profiles = await svc.list_profiles()
    assert profiles == ["acme"]
