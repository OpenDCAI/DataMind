"""MySQL / MariaDB dialect.

Install the extra first: `pip install datamind[mysql]` (pymysql + cryptography).
Expected DSN format:

    mysql+pymysql://user:password@host:3306/dbname?charset=utf8mb4

We start a read-only transaction for each query. Closing the query
connection rolls it back without changing pooled session defaults. On servers where the user lacks the privilege this is a no-op — the
upstream safeguard (is_destructive / contains_multiple_statements) still
holds the line.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from datamind.core.errors import ConfigError
from datamind.core.registry import db_registry

from ..base import BaseSQLDialect


@db_registry.register("mysql")
class MySQLDialect(BaseSQLDialect):
    name = "mysql"
    driver_hint = "pymysql"

    def build_engine(self, dsn: str | None = None, **kwargs: Any):
        if not dsn:
            raise ConfigError(
                "mysql dialect requires a DSN, e.g. "
                "mysql+pymysql://user:pw@host:3306/dbname"
            )
        if not dsn.startswith(("mysql://", "mysql+")):
            raise ConfigError(f"invalid mysql DSN: {dsn!r}")
        # Default to pymysql driver for broadest compatibility.
        if dsn.startswith("mysql://"):
            dsn = "mysql+pymysql://" + dsn[len("mysql://"):]
        return super().build_engine(dsn, **kwargs)

    def _before_query(self, conn: Any) -> None:
        # Scope the hint to this transaction, not future pooled writes.
        try:
            conn.execute(text("START TRANSACTION READ ONLY"))
        except Exception:  # pragma: no cover
            pass

    def _quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"
