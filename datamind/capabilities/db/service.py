"""DB service: dialect + engine + NL2SQL orchestration.

One DBService per (profile, dsn) — cheap to build, holds a SQLAlchemy
engine pool inside, so it should be reused across requests. For truly
ephemeral smoke tests just build a fresh one.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from datamind.config import DBConfig, LLMConfig, Settings
from datamind.core.errors import CapabilityError
from datamind.core.logging import get_logger
from datamind.core.protocols import DatabaseDialect, QueryResult, TableSchema, TextModelClient
from datamind.core.registry import db_registry

# Importing providers populates db_registry.
from . import providers  # noqa: F401
from .nl2sql import generate_sql

_log = get_logger("db.service")


class DBService:
    def __init__(
        self,
        *,
        dialect: DatabaseDialect,
        engine: Engine,
        db_cfg: DBConfig,
        llm_client: TextModelClient | None = None,
        llm_model: str | None = None,
    ) -> None:
        self.dialect = dialect
        self.engine = engine
        self.db_cfg = db_cfg
        self._llm = llm_client
        self._model = llm_model
        self._schema_cache: dict[str, TableSchema] = {}

    # --------------------------------------------------------------- public

    async def list_tables(self) -> list[str]:
        return await self.dialect.list_tables(self.engine)

    async def describe(self, table: str) -> TableSchema:
        if table not in self._schema_cache:
            self._schema_cache[table] = await self.dialect.describe(self.engine, table)
        return self._schema_cache[table]

    async def describe_all(self) -> list[TableSchema]:
        names = await self.list_tables()
        return list(await asyncio.gather(*(self.describe(t) for t in names)))

    def invalidate_schema_cache(self) -> None:
        self._schema_cache.clear()

    async def aclose(self) -> None:
        dispose = getattr(self.engine, "dispose", None)
        if callable(dispose):
            result = dispose()
            if hasattr(result, "__await__"):
                await result

    async def query_sql(self, sql: str) -> QueryResult:
        return await self.dialect.execute_readonly(
            self.engine,
            sql,
            row_limit=self.db_cfg.row_limit,
            timeout_s=self.db_cfg.query_timeout_s,
        )

    async def query_nl(
        self,
        question: str,
        *,
        tables: list[str] | None = None,
    ) -> dict[str, Any]:
        """NL -> SQL -> result. Returns {sql, result, columns, rows}."""
        if self._llm is None or not self._model:
            raise CapabilityError(
                "db",
                "NL2SQL requires llm_client + llm_model; pass them in the service.",
            )
        if tables:
            available = set(await self.list_tables())
            missing = [table for table in tables if table not in available]
            if missing:
                raise CapabilityError("db", f"Unknown tables: {missing!r}")
            schemas = list(await asyncio.gather(*(self.describe(t) for t in tables)))
        else:
            schemas = await self.describe_all()
        sql = await generate_sql(
            client=self._llm,
            model=self._model,
            question=question,
            schemas=schemas,
            dialect_name=self.dialect.name,
        )
        result = await self.query_sql(sql)
        return {
            "question": question,
            "sql": sql,
            "columns": result.columns,
            "rows": result.rows,
            "returned_count": len(result.rows),
            "total_count": None,
            "next_cursor": None,
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_db_service(
    settings: Settings,
    *,
    llm_client: TextModelClient | None = None,
) -> DBService:
    dialect_name = settings.db.dialect
    dialect = db_registry.create(dialect_name)

    if settings.db.dialect == "sqlite" and not settings.db.dsn:
        # Default to a per-profile demo.db under storage/
        default_path = settings.data.storage_dir / "demo.db"
        engine = dialect.build_engine(None, default_path=str(default_path))
    else:
        engine = dialect.build_engine(settings.db.dsn)

    return DBService(
        dialect=dialect,
        engine=engine,
        db_cfg=settings.db,
        llm_client=llm_client,
        llm_model=settings.llm.model if llm_client else None,
    )


__all__ = ["DBService", "build_db_service"]
