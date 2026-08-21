"""SQLite-backed long-term memory with scope-typed recall (v0.3).

Schema (v0.3):
    CREATE TABLE memory_v2 (
        id          TEXT PRIMARY KEY,
        scope       TEXT NOT NULL CHECK(scope IN ('global','profile','session')),
        profile     TEXT,
        session_id  TEXT,
        kind        TEXT NOT NULL DEFAULT 'fact',
        status      TEXT NOT NULL DEFAULT 'active',
        content     TEXT NOT NULL,
        metadata    TEXT,                       -- JSON
        embedding   BLOB,                       -- float32 packed
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL,
        archived_at REAL                        -- NULL unless soft-deleted
    );
    CREATE INDEX idx_mem2_scope_profile  ON memory_v2(scope, profile)         WHERE status='active';
    CREATE INDEX idx_mem2_scope_session  ON memory_v2(scope, session_id)      WHERE status='active';
    CREATE INDEX idx_mem2_scope_global   ON memory_v2(scope)                  WHERE status='active' AND scope='global';

Recall is the union of three scope-conditioned top-k retrievals (session,
profile, global), with caller-controlled per-scope budgets. The default
distribution (2 + 4 + 2) gives a balanced 8-item context that fits inside
typical LLM prompt budgets without bleeding tenant boundaries.

Migration: if the legacy v0.2 ``memory`` table exists, rows are copied
into ``memory_v2`` with ``scope='profile', profile=<old_namespace>`` so
existing deployments don't lose data on upgrade.

Recall uses cosine similarity in Python; for tens of thousands of items
this is fine. Outgrow it ⇒ register a Postgres+pgvector or Qdrant
provider under ``memory_registry``.
"""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import struct
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from datamind.core.errors import CapabilityError
from datamind.core.logging import get_logger
from datamind.core.protocols import EmbeddingProvider, MemoryItem
from datamind.core.registry import memory_registry

_log = get_logger("memory.sqlite")

Scope = Literal["global", "profile", "session"]
Kind = Literal["preference", "decision", "workflow", "summary", "skill", "fact"]


# ----------------------------------------------------------------- helpers


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(data: bytes) -> list[float]:
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _row_to_item(row: tuple, *, score: float = 0.0) -> MemoryItem:
    rid, scope, profile, session_id, kind, status, content, meta_json, _emb, created_at, _updated, _archived = row
    try:
        meta = json.loads(meta_json) if meta_json else {}
    except json.JSONDecodeError:
        meta = {}
    return MemoryItem(
        id=rid,
        scope=scope,
        profile=profile,
        session_id=session_id,
        kind=kind,
        status=status,
        content=content,
        score=score,
        metadata=meta,
        created_at=str(created_at),
    )


# ----------------------------------------------------------------- store


@memory_registry.register("sqlite")
class SQLiteMemoryStore:
    """Embedding-aware, scope-typed memory store in a single local SQLite file."""

    def __init__(
        self,
        *,
        dsn: str | None = None,
        db_path: str | Path | None = None,
        embedding: EmbeddingProvider | None = None,
    ) -> None:
        if dsn and not db_path:
            if dsn.startswith("sqlite:///"):
                db_path = dsn[len("sqlite:///"):]
            else:
                db_path = dsn
        if not db_path:
            raise CapabilityError("memory", "sqlite memory store needs db_path or dsn")
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._embedding = embedding
        self._init_schema()

    # ------------------------------------------------------------- schema

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_schema(self) -> None:
        with closing(self._conn()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_v2 (
                    id          TEXT PRIMARY KEY,
                    scope       TEXT NOT NULL CHECK(scope IN ('global','profile','session')),
                    profile     TEXT,
                    session_id  TEXT,
                    kind        TEXT NOT NULL DEFAULT 'fact',
                    status      TEXT NOT NULL DEFAULT 'active',
                    content     TEXT NOT NULL,
                    metadata    TEXT,
                    embedding   BLOB,
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    archived_at REAL
                );
                """
            )
            # Partial indexes: cheap and selective for the active slice we
            # always recall against.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem2_scope_profile "
                "ON memory_v2(scope, profile) WHERE status='active'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem2_scope_session "
                "ON memory_v2(scope, session_id) WHERE status='active'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem2_scope_global "
                "ON memory_v2(scope) WHERE status='active' AND scope='global'"
            )
            self._migrate_v1_if_needed(conn)

    def _migrate_v1_if_needed(self, conn: sqlite3.Connection) -> None:
        """Copy rows from the legacy ``memory`` table into ``memory_v2``.

        Old rows had a single ``namespace`` field; we map every legacy row to
        ``scope='profile', profile=<namespace>``. This is conservative — it
        keeps the data accessible without claiming false isolation properties.
        """
        # Does the old table exist?
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory'"
        )
        if cur.fetchone() is None:
            return

        # Anything to migrate?
        cur = conn.execute("SELECT count(*) FROM memory")
        old_count = int(cur.fetchone()[0])
        if old_count == 0:
            conn.execute("DROP TABLE memory")
            return

        cur = conn.execute("SELECT count(*) FROM memory_v2")
        new_count = int(cur.fetchone()[0])
        if new_count > 0:
            # Already migrated; leave the old table for inspection.
            return

        _log.info("memory_v1_migration_start", extra={"rows": old_count})
        cur = conn.execute(
            "SELECT id, namespace, content, metadata, embedding, created_at FROM memory"
        )
        rows = cur.fetchall()
        ts = time.time()
        for rid, namespace, content, meta_json, emb, created_at in rows:
            conn.execute(
                "INSERT OR IGNORE INTO memory_v2 ("
                "id, scope, profile, session_id, kind, status, content, metadata, "
                "embedding, created_at, updated_at, archived_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    rid,
                    "profile",
                    namespace,
                    None,
                    "fact",
                    "active",
                    content,
                    meta_json,
                    emb,
                    created_at,
                    ts,
                ),
            )
        _log.info("memory_v1_migration_done", extra={"rows": old_count})
        # Drop the legacy table after a successful migration. Keep behavior
        # predictable: we don't want lingering "ghost" data in two places.
        conn.execute("DROP TABLE memory")

    # ------------------------------------------------------------- save

    async def save(
        self,
        content: str,
        *,
        scope: Scope = "profile",
        profile: str | None = None,
        session_id: str | None = None,
        kind: Kind = "fact",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if scope == "profile" and not profile:
            raise CapabilityError("memory", "scope='profile' requires profile= argument")
        if scope == "session" and not session_id:
            raise CapabilityError("memory", "scope='session' requires session_id= argument")

        item_id = uuid.uuid4().hex
        ts = time.time()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        emb: bytes | None = None
        if self._embedding is not None and content.strip():
            vec = await self._embedding.embed_query(content)
            emb = _pack(vec)

        def _run() -> None:
            with closing(self._conn()) as conn, conn:
                conn.execute(
                    "INSERT INTO memory_v2 ("
                    "id, scope, profile, session_id, kind, status, content, metadata, "
                    "embedding, created_at, updated_at, archived_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        item_id,
                        scope,
                        profile if scope == "profile" else None,
                        session_id if scope == "session" else None,
                        kind,
                        "active",
                        content,
                        meta_json,
                        emb,
                        ts,
                        ts,
                    ),
                )

        await asyncio.to_thread(_run)
        return item_id

    # ------------------------------------------------------------- recall

    async def recall(
        self,
        query: str,
        *,
        profile: str | None = None,
        session_id: str | None = None,
        top_k: int = 8,
        kinds: Sequence[str] | None = None,
        include_archived: bool = False,
        # advanced — let callers tune the per-scope budget for ablation
        per_scope: dict[str, int] | None = None,
    ) -> list[MemoryItem]:
        # Default per-scope budget: 2 (session) + 4 (profile) + 2 (global) = 8.
        budgets: dict[str, int] = {"session": 2, "profile": 4, "global": 2}
        if per_scope:
            budgets.update({k: v for k, v in per_scope.items() if k in budgets})

        # If no embedding, fall back to lexical scoring within the same
        # scope filters so the contract stays identical.
        scoring = self._score_with_embedding if self._embedding else self._score_lexical
        qv = await self._embedding.embed_query(query) if self._embedding else None

        merged: list[MemoryItem] = []
        seen: set[str] = set()

        async def _slice(scope: Scope, where: str, params: list[Any], budget: int) -> list[MemoryItem]:
            if budget <= 0:
                return []
            return await asyncio.to_thread(
                self._select_and_score,
                scope=scope,
                where=where,
                params=params,
                budget=budget,
                kinds=list(kinds) if kinds else None,
                include_archived=include_archived,
                qvec=qv,
                lexical_query=query,
                scoring=scoring,
            )

        # session
        if session_id and budgets["session"] > 0:
            for item in await _slice(
                "session",
                "scope='session' AND session_id=?",
                [session_id],
                budgets["session"],
            ):
                if item.id not in seen:
                    seen.add(item.id)
                    merged.append(item)

        # profile
        if profile and budgets["profile"] > 0:
            for item in await _slice(
                "profile",
                "scope='profile' AND profile=?",
                [profile],
                budgets["profile"],
            ):
                if item.id not in seen:
                    seen.add(item.id)
                    merged.append(item)

        # global
        if budgets["global"] > 0:
            for item in await _slice(
                "global",
                "scope='global'",
                [],
                budgets["global"],
            ):
                if item.id not in seen:
                    seen.add(item.id)
                    merged.append(item)

        # Final rank: by score desc, but keep stable scope ordering for ties
        # so session-level prefs still win over global on equal-score items.
        merged.sort(key=lambda m: (-m.score, _scope_priority(m.scope)))
        return merged[:top_k]

    # ------------------------------------------------------------- forget

    async def forget(self, item_id: str, *, hard: bool = False) -> bool:
        ts = time.time()

        def _run() -> bool:
            with closing(self._conn()) as conn, conn:
                if hard:
                    cur = conn.execute("DELETE FROM memory_v2 WHERE id=?", (item_id,))
                else:
                    cur = conn.execute(
                        "UPDATE memory_v2 SET status='archived', archived_at=?, updated_at=? "
                        "WHERE id=? AND status='active'",
                        (ts, ts, item_id),
                    )
                return cur.rowcount > 0

        return await asyncio.to_thread(_run)

    # ------------------------------------------------------------- listing

    async def list_profiles(self) -> list[str]:
        def _run() -> list[str]:
            with closing(self._conn()) as conn, conn:
                cur = conn.execute(
                    "SELECT DISTINCT profile FROM memory_v2 "
                    "WHERE scope='profile' AND status='active' "
                    "ORDER BY profile"
                )
                return [r[0] for r in cur.fetchall() if r[0] is not None]

        return await asyncio.to_thread(_run)

    async def count(
        self,
        *,
        scope: Scope | None = None,
        profile: str | None = None,
        session_id: str | None = None,
        include_archived: bool = False,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_archived:
            clauses.append("status='active'")
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if profile:
            clauses.append("profile=?")
            params.append(profile)
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        def _run() -> int:
            with closing(self._conn()) as conn, conn:
                cur = conn.execute(f"SELECT COUNT(*) FROM memory_v2{where}", params)
                return int(cur.fetchone()[0])

        return await asyncio.to_thread(_run)

    # ----------------------------------------------------------- internals

    def _select_and_score(
        self,
        *,
        scope: Scope,
        where: str,
        params: list[Any],
        budget: int,
        kinds: list[str] | None,
        include_archived: bool,
        qvec: list[float] | None,
        lexical_query: str,
        scoring,
    ) -> list[MemoryItem]:
        clauses = [where]
        bind: list[Any] = list(params)
        if not include_archived:
            clauses.append("status='active'")
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            bind.extend(kinds)
        sql = (
            "SELECT id, scope, profile, session_id, kind, status, content, metadata, "
            "embedding, created_at, updated_at, archived_at "
            "FROM memory_v2 WHERE " + " AND ".join(clauses)
        )
        with closing(self._conn()) as conn, conn:
            rows = conn.execute(sql, bind).fetchall()
        if not rows:
            return []
        scored = scoring(rows, qvec=qvec, lexical_query=lexical_query)
        scored.sort(key=lambda pair: -pair[0])
        return [item for _, item in scored[:budget]]

    @staticmethod
    def _score_with_embedding(
        rows: Iterable[tuple],
        *,
        qvec: list[float] | None,
        lexical_query: str,  # unused
    ) -> list[tuple[float, MemoryItem]]:
        out: list[tuple[float, MemoryItem]] = []
        for row in rows:
            emb_blob = row[8]
            score = _cosine(qvec, _unpack(emb_blob)) if (qvec and emb_blob) else 0.0
            out.append((score, _row_to_item(row, score=score)))
        return out

    @staticmethod
    def _score_lexical(
        rows: Iterable[tuple],
        *,
        qvec: list[float] | None,  # unused
        lexical_query: str,
    ) -> list[tuple[float, MemoryItem]]:
        terms = [t for t in lexical_query.lower().split() if t]
        out: list[tuple[float, MemoryItem]] = []
        for row in rows:
            content = row[6]
            lc = content.lower()
            hits = sum(1 for t in terms if t in lc)
            score = hits / max(len(terms), 1)
            out.append((score, _row_to_item(row, score=score)))
        return out


def _scope_priority(scope: str) -> int:
    return {"session": 0, "profile": 1, "global": 2}.get(scope, 9)
