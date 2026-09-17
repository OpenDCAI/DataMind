"""Shared SQLAlchemy-backed base for dialect providers.

Concrete dialects (SQLite, MySQL, ...) only need to:
- pick a default driver URL format
- override `is_destructive` if they have extra forbidden verbs

Everything else — engine construction, listing tables, describing schema,
bounded read-only query execution — lives in BaseSQLDialect.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError

from datamind.core.errors import CapabilityError
from datamind.core.protocols import ColumnSchema, QueryResult, TableSchema

from .safeguard import (
    DestructiveSQLError,
    MultiStatementSQLError,
    contains_multiple_statements,
    ensure_row_limit,
    is_destructive_sql,
)


class BaseSQLDialect:
    """Common async-friendly SQLAlchemy wrapper."""

    name: str = "base"

    # Dialects that ship natively with SQLAlchemy — no extra driver needed.
    # Subclasses override if a specific driver must be used.
    driver_hint: str = ""

    # ------------------------------------------------------------- engine ---

    def build_engine(self, dsn: str, **kwargs: Any) -> Engine:
        """Build a SQLAlchemy Engine from a DSN.

        We apply a few safe defaults: pool_pre_ping on (handles stale
        connections), future=True (2.0-style).
        """
        opts = {"pool_pre_ping": True, "future": True}
        opts.update(kwargs)
        try:
            return create_engine(dsn, **opts)
        except SQLAlchemyError as exc:
            raise CapabilityError("db", f"Failed to build engine: {exc}", cause=exc)

    # ----------------------------------------------------------- inspection

    async def list_tables(self, engine: Engine) -> list[str]:
        def _run() -> list[str]:
            insp = inspect(engine)
            tables = set(insp.get_table_names())
            # Include views so the agent sees everything queryable.
            try:
                tables.update(insp.get_view_names())
            except NotImplementedError:
                pass
            return sorted(tables)

        return await asyncio.to_thread(_run)

    async def describe(self, engine: Engine, table: str) -> TableSchema:
        def _run() -> TableSchema:
            insp = inspect(engine)
            cols_raw = insp.get_columns(table)
            pk_cols: Sequence[str] = insp.get_pk_constraint(table).get("constrained_columns", []) or []
            columns = [
                ColumnSchema(
                    name=c["name"],
                    type=str(c["type"]),
                    nullable=bool(c.get("nullable", True)),
                    primary_key=c["name"] in pk_cols,
                )
                for c in cols_raw
            ]
            # Best-effort row count; skip on big tables or permission failures.
            row_count: int | None = None
            try:
                with engine.connect() as conn:
                    result = conn.execute(text(f"SELECT COUNT(*) FROM {self._quote_ident(table)}"))
                    row_count = int(result.scalar() or 0)
            except SQLAlchemyError:
                row_count = None
            return TableSchema(name=table, columns=columns, row_count_estimate=row_count)

        return await asyncio.to_thread(_run)

    # ------------------------------------------------------------- execute

    async def execute_readonly(
        self,
        engine: Engine,
        sql: str,
        *,
        row_limit: int = 1000,
        timeout_s: float = 10.0,
    ) -> QueryResult:
        if not sql or not sql.strip():
            raise CapabilityError("db", "Empty SQL")
        if row_limit < 1:
            raise CapabilityError("db", "row_limit must be at least 1")
        if contains_multiple_statements(sql):
            raise MultiStatementSQLError(
                "multiple statements are not allowed (use a single SELECT)"
            )
        if self.is_destructive(sql):
            raise DestructiveSQLError(
                f"destructive SQL rejected (leading verb is write/DDL): {sql[:80]!r}"
            )

        bounded = ensure_row_limit(sql, row_limit + 1)

        def _run() -> QueryResult:
            start = time.perf_counter()
            with engine.connect() as conn:
                # Per-connection read-only hint where supported. Base class
                # is conservative — dialects can strengthen (SET TRANSACTION
                # READ ONLY etc.).
                self._before_query(conn)
                result = conn.execute(text(bounded))
                columns = list(result.keys())
                # Fetch one extra row so we can detect truncation.
                rows = [list(row) for row in result.fetchmany(row_limit + 1)]
            truncated = len(rows) > row_limit
            if truncated:
                rows = rows[:row_limit]
            elapsed = (time.perf_counter() - start) * 1000.0
            return QueryResult(
                columns=columns,
                rows=rows,
                truncated=truncated,
                elapsed_ms=round(elapsed, 2),
            )

        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s)

    # ------------------------------------------------------------ safeguard

    def is_destructive(self, sql: str) -> bool:
        return is_destructive_sql(sql)

    # ---------------------------------------------------------- extension hooks

    def _before_query(self, conn: Any) -> None:  # noqa: D401
        """Override to issue dialect-specific read-only pragmas."""
        return None

    def _quote_ident(self, name: str) -> str:
        """Default identifier quoting; dialects override for back-ticks etc."""
        return '"' + name.replace('"', '""') + '"'
