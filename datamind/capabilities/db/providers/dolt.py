"""Dolt dialect — a MySQL-compatible, version-controlled SQL backend.

Dolt speaks the MySQL wire protocol and most of its SQL surface, so we
build on the same machinery as `MySQLDialect` (pymysql driver, backtick
quoting, `SET SESSION TRANSACTION READ ONLY`). On top of that it exposes
commit-level versioning:

    - `build_engine(dsn, storage_dir=...)` starts a local `dolt sql-server`,
      initializes the data directory if needed, and creates the target DB.
    - `commit_version(...)` snapshots the working set as a new commit;
      `list_versions`, `describe_at_version`, `query_sql_at_version`,
      `rollback`, and `list_tables_at_version` read/restore history.

Expected DSN format:

    mysql+pymysql://root:@127.0.0.1:3307/dbname
"""
from __future__ import annotations

import asyncio
import re
import os
import shutil
import subprocess
import socket, time
import mysql.connector

from urllib.parse import urlparse
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, SQLAlchemyError
from sqlalchemy.engine import Engine

from datamind.core.errors import ConfigError
from datamind.core.registry import db_registry
from datamind.core.errors import CapabilityError
from datamind.core.protocols import ColumnSchema, TableSchema

from ..base import BaseSQLDialect

_FULL_HASH_RE = re.compile(r"[0-9a-v]{32}")
_TABLE_RE = re.compile(r"\bFROM\s+(`?[A-Za-z_][\w$]*`?)", re.IGNORECASE)

@db_registry.register("dolt")
class DoltDialect(BaseSQLDialect):
    name = "dolt"
    
    server: subprocess.Popen | None = None
    
    def _parse_dolt_dsn(self, dsn: str) -> dict:
        parsed = urlparse(dsn)
        return {
            "user": parsed.username or "root",
            "password": parsed.password or "",
            "host": parsed.hostname or "127.0.0.1",
            "port": parsed.port or 3307,
            "dbname": parsed.path.lstrip("/") or "defaultdb",
        }
    
    def _wait_ready(self, host, port, user, password, tries=60):
        for i in range(tries):
            time.sleep(0.5)
            try:
                c = mysql.connector.connect(
                    host=host, port=port, user=user, password=password,
                    connection_timeout=2,
                )
                c.close()
                return
            except mysql.connector.errors.DatabaseError as e:
                msg = str(e)
                if "2003" in msg or "Can't connect" in msg:
                    if i == tries - 1:
                        raise RuntimeError(f"Dolt 启动超时: {e}")
                    continue
                raise
        
    def build_engine(self, dsn: str | None = None, **kwargs: Any):
        storage_dir = kwargs.pop("storage_dir", None)
        if not storage_dir:
            raise ValueError("Dolt needs either a data dir.")
        if not dsn:
            raise ConfigError(
                "Dolt dialect requires a DSN, e.g. "
                "mysql+pymysql://root:@host:3306/dbname"
            )
        if not dsn.startswith(("mysql://", "mysql+")):
            raise ConfigError(f"invalid mysql DSN: {dsn!r}")
        
        # Default to pymysql driver for broadest compatibility.
        if dsn.startswith("mysql://"):
            dsn = "mysql+pymysql://" + dsn[len("mysql://"):]
        
        # Dolt is an external binary (not a pip package). Without this check,
        # the first `subprocess.run(["dolt", ...])` below raises a bare
        # FileNotFoundError with no hint about how to fix it.
        if shutil.which("dolt") is None:
            raise CapabilityError(
                "db",
                "The `dolt` binary was not found on PATH. "
                "Install it from https://github.com/dolthub/dolt "
                "(e.g. `brew install dolt`, or run the official install script), "
                "or select another dialect such as 'sqlite' or 'mysql'.",
            )
        cfg = self._parse_dolt_dsn(dsn)
        host, port, dbname = cfg["host"], cfg["port"], cfg["dbname"]
        user, password = cfg["user"], cfg["password"]
    
        if not os.path.exists(os.path.join(storage_dir, ".dolt")):
            os.makedirs(storage_dir, exist_ok=True)
            subprocess.run(
                ["dolt", "init", "--name", "DataMind", "--email", "datamind@example.com"],
                cwd=storage_dir, check=True,
            )

        def _listening():
            with socket.socket() as s:
                s.settimeout(0.3)
                return s.connect_ex((host, port)) == 0

        if not _listening():
            log_path = os.path.join(storage_dir, "dolt_server.log")
            log_file = open(log_path, "w")
            self._server = subprocess.Popen(
                ["dolt", "sql-server", "--port", str(port), "--host", host],
                cwd=storage_dir, stdout=log_file, stderr=log_file,
            )
            self._wait_ready(host, port, user, password)
        
        c = mysql.connector.connect(host=host, port=port, user=user, password=password)
        cur = c.cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{dbname}`")
        c.commit()
        c.close()
        
        return super().build_engine(dsn, **kwargs)

    def aclose(self):
        server = getattr(self, "_server", None)
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
            self._server = None
            
    def _before_query(self, conn: Any) -> None:
        # Best-effort; privilege-dependent.
        try:
            conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
        except Exception:  # pragma: no cover
            pass

    def _quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def _run_sql(self, engine: Engine, sql, params=None, fetch=True):
        try:
            if fetch:
                with engine.connect() as conn:
                    result = conn.execute(text(sql), params) if params else conn.execute(text(sql))
                    return result.fetchall()
            else:
                with engine.begin() as conn:          
                    conn.execute(text(sql), params) if params else conn.execute(text(sql))
                return None
        except Exception as e:
            raise                                       

    def _has_uncommitted_changes(self, engine: Engine) -> bool:
        try:
            rows = self._run_sql(engine, "SELECT COUNT(*) FROM dolt_status")
            return rows[0][0] > 0 if rows else False
        except Exception:
            return True

    def validate_version(self, version: str) -> str:
        """Validate a Dolt commit hash.

        Only accepts full 32-char hashes as returned by `list_versions()`.
        Abbreviated hashes, branch names, tags, and HEAD are deliberately
        rejected — the tool contract is "pass a commit hash from
        list_versions", so accepting more would only invite typos to slip
        through to Dolt as opaque errors.
        """
        if not version or not version.strip():
            raise CapabilityError("db", "version must not be empty")

        v = version.strip()
        if not _FULL_HASH_RE.fullmatch(v):
            raise CapabilityError(
                "db",
                f"version must be a 32-char Dolt commit hash "
                f"(0-9, a-v), got {v!r}",
            )
        return v

    async def commit_version(self, engine: Engine, message: str) -> str | None:
        def _run() -> str | None:
            if not self._has_uncommitted_changes(engine):
                return None

            self._run_sql(engine, "CALL DOLT_ADD('-A')", fetch=False)

            try:
                self._run_sql(
                    engine,
                    "CALL DOLT_COMMIT('-m', :message)",
                    {"message": message},
                    fetch=False,
                )
            except DatabaseError as e:
                if "nothing to commit" in str(e).lower():
                    return None
                raise

            rows = self._run_sql(engine, "SELECT commit_hash FROM dolt_log LIMIT 1")
            return rows[0][0] if rows else None

        return await asyncio.to_thread(_run)
      
    async def list_versions(self, engine: Engine) -> list[dict]:
        def _run() -> list[dict]:
            rows = self._run_sql(
                engine,
                """
                SELECT 
                    commit_hash,
                    author,
                    date,
                    message
                FROM dolt_log
                ORDER BY date DESC
                """,
                fetch=True,
            )
            
            return [
                {
                    "commit": row[0],
                    "author": row[1],
                    "timestamp": str(row[2]),
                    "message": row[3],
                }
                for row in rows
            ]
        
        return await asyncio.to_thread(_run)

    async def list_tables_at_version(
        self, engine: Engine, version: str,
    ) -> list[str]:
        """List table names as of a specific Dolt version.
        """
        version = self.validate_version(version)

        def _run() -> list[str]:
            rows = self._run_sql(
                engine,
                f"SHOW FULL TABLES AS OF '{version}'",
                fetch=True,
            )
            # SHOW FULL TABLES returns: (Tables_in_<db>, Table_type)
            # Table_type is 'BASE TABLE' or 'VIEW'.
            return sorted(r[0] for r in rows)

        return await asyncio.to_thread(_run)

    async def describe_at_version(
        self,
        engine: Engine,
        table: str,
        version: str,
    ) -> TableSchema:
        """Describe a table's schema as of a specific Dolt version.

        Mirrors `BaseSQLDialect.describe` — returns the same `TableSchema`
        shape. Uses `SHOW COLUMNS ... AS OF '<version>'` because
        `inspect(engine)` only reflects the current working set.

        `AS OF` does not accept a bind parameter, so the version reference
        is validated against a restricted charset before being spliced into
        the SQL text.
        """
        version = self.validate_version(version)
        q = self._quote_ident

        def _run() -> TableSchema:
            # 1) Column definitions at that version.
            #    SHOW COLUMNS returns: Field, Type, Null, Key, Default, Extra
            #    Key='PRI' marks a primary-key column.
            rows = self._run_sql(
                engine,
                f"SHOW COLUMNS FROM {q(table)} AS OF '{version}'",
                fetch=True,
            )
            if not rows:
                raise CapabilityError(
                    "db",
                    f"table {table!r} does not exist at version {version!r}",
                )

            columns = [
                ColumnSchema(
                    name=r[0],                                   # Field
                    type=str(r[1]),                              # Type
                    nullable=(str(r[2]).upper() == "YES"),       # Null
                    primary_key=(str(r[3]).upper() == "PRI"),    # Key
                )
                for r in rows
            ]

            # 2) Best-effort row count at that version.
            #    Same "skip on failure" contract as the base class.
            row_count: int | None = None
            try:
                with engine.connect() as conn:
                    result = conn.execute(
                        text(f"SELECT COUNT(*) FROM {q(table)} AS OF '{version}'")
                    )
                    row_count = int(result.scalar() or 0)
            except SQLAlchemyError:
                row_count = None

            return TableSchema(
                name=table,
                columns=columns,
                row_count_estimate=row_count,
            )

        return await asyncio.to_thread(_run)

    async def rollback(self, engine: Engine, version: str) -> str | None:
        """Roll back to `version` by reverting every commit after it.

        History is preserved: `version` and everything after it stay in
        `dolt_log`; a revert commit is appended for each subsequent commit.
        """
        def _run() -> str | None:
            # 1. Find every commit AFTER `version` (newest first).
            rows = self._run_sql(
                engine,
                """
                SELECT commit_hash, date
                FROM dolt_log
                WHERE date > (SELECT date FROM dolt_log WHERE commit_hash = :v)
                ORDER BY date DESC
                """,
                {"v": version},
                fetch=True,
            )
            to_revert = [r[0] for r in rows]

            if not to_revert:
                # Already at `version` — nothing to do.
                return version

            # 2. Revert newest → oldest (mirrors `git revert` semantics).
            self._run_sql(
                engine,
                "CALL DOLT_REVERT(" + ", ".join(f":h{i}" for i in range(len(to_revert))) + ")",
                {f"h{i}": h for i, h in enumerate(to_revert)},
                fetch=False,
            )

            # 3. Return the newest commit (the last revert).
            rows = self._run_sql(
                engine, "SELECT commit_hash FROM dolt_log LIMIT 1", fetch=True,
            )
            return rows[0][0] if rows else None

        return await asyncio.to_thread(_run)
    
    def _inject_as_of(self, sql: str, version: str) -> str:
        """Rewrite `FROM <table>` → `FROM <table> AS OF '<version>'`.

        Best-effort regex rewrite for the common single- or multi-table
        SELECT that tool/LLM code emits. It handles:
          - bare table names: `FROM product`
          - back-ticked names: `FROM \`product\``
          - repeated FROM (JOINs): each one is rewritten

        It does NOT:
          - rewrite strings/comments (`FROM x` inside a literal)
          - rewrite subqueries specially — they are rewritten the same way,
            which is correct for Dolt (AS OF applies to each table)
          - rewrite schema-qualified names like `FROM db.tbl`
            (the `db` part matches, and the `.tbl` is left alone)

        `version` must already be validated by `validate_version` — the
        reference is spliced into SQL text and cannot be a bind parameter.
        """
        q = self._quote_ident

        def _sub(match: re.Match[str]) -> str:
            raw = match.group(1)
            name = raw.strip("`")               # normalize away input quoting
            return f"FROM {q(name)} AS OF '{version}'"

        return _TABLE_RE.sub(_sub, sql)
    
    async def execute_readonly_at_version(
        self,
        engine: Engine,
        sql: str,
        *,
        row_limit: int = 1000,
        timeout_s: float = 10.0,
        version: str | None = None,
    ) -> None:
        """Execute a read-only statement, optionally against a Dolt version.

        When `version` is given, `AS OF '<version>'` is injected into
        each `FROM <table>` clause before the base class appends its
        `LIMIT`. `AS OF` cannot be a bind parameter, so the reference is
        validated against a restricted charset first.
        """
        sql = self._inject_as_of(sql, version)

        return await super().execute_readonly(
            engine, sql,
            row_limit=row_limit,
            timeout_s=timeout_s,
        )