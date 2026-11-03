"""SQL safeguard primitives shared by all dialects.

Three lines of defence:
1. Syntactic check — reject statements whose leading verb implies write.
2. Multiple-statement rejection — SQLAlchemy's `text()` can chain; we don't.
3. Row limit — wrap a LIMIT when enabled; the explicit benchmark policy
   may disable the result cap while retaining read-only checks.

Per-dialect quirks (e.g. T-SQL `TOP`, SQLite `ATTACH`, MySQL `KILL`) can be
overridden in the dialect subclass.
"""
from __future__ import annotations

import re

_DESTRUCTIVE_VERBS = frozenset(
    {
        "INSERT", "UPDATE", "DELETE", "REPLACE",
        "DROP", "CREATE", "ALTER", "TRUNCATE", "RENAME",
        "GRANT", "REVOKE",
        "ATTACH", "DETACH",
        "CALL", "EXEC", "EXECUTE",
        "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
        "LOAD", "COPY",
        "KILL", "LOCK", "UNLOCK",
        "PRAGMA",  # SQLite — can mutate session state
    }
)

# Match a SQL comment (/* */ or -- line) to strip before verb detection.
_COMMENT_RE = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)
# Keywords that imply a multi-statement payload when stray ';' appears mid-string.
_STATEMENT_SEP_RE = re.compile(r";\s*\S")


def strip_comments(sql: str) -> str:
    # Quoted identifiers may contain punctuation that resembles comments.
    tokens = re.compile(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\])|(/\*.*?\*/|--[^\n]*)", re.DOTALL)
    def replace(match):
        if match.group(1) is not None:
            return match.group(1)
        left = sql[match.start() - 1:match.start()] if match.start() else ''
        right = sql[match.end():match.end() + 1]
        return ' ' if left and right and not left.isspace() and not right.isspace() else ''
    return tokens.sub(replace, sql)


def leading_verb(sql: str) -> str:
    """Return the upper-cased first keyword of `sql`, or ''."""
    cleaned = strip_comments(sql).strip()
    if not cleaned:
        return ""
    first = cleaned.split(None, 1)[0]
    # Trim trailing punctuation like 'SELECT('
    first = re.match(r"[A-Za-z]+", first)
    return first.group(0).upper() if first else ""


def is_destructive_sql(sql: str) -> bool:
    """True if `sql` would modify data / schema / session state."""
    verb = leading_verb(sql)
    if verb in _DESTRUCTIVE_VERBS:
        return True
    # Block SELECT ... INTO OUTFILE / INTO TABLE (MySQL/Postgres write trick)
    cleaned = strip_comments(sql).lower()
    if re.search(r"\binto\s+(outfile|dumpfile|temp(orary)?\s+table|table)\b", cleaned):
        return True
    return False


def contains_multiple_statements(sql: str) -> bool:
    """Reject payloads that try to chain `SELECT ...; DELETE ...`.

    We mask out quoted-string literals first so that ';' inside a column
    value never trips the detector.
    """
    cleaned = strip_comments(sql)
    # Replace single- and double-quoted runs (handling doubled-quote escapes).
    masked = re.sub(r"'(?:''|[^'])*'", "''", cleaned)
    masked = re.sub(r'"(?:""|[^"])*"', '""', masked)
    masked = re.sub(r"`(?:``|[^`])*`|\[[^\]]*\]", '""', masked)
    masked = masked.strip().rstrip(";")
    return bool(_STATEMENT_SEP_RE.search(masked))


def ensure_row_limit(sql: str, row_limit: int) -> str:
    """Wrap `sql` so its result can never exceed `row_limit` rows.

    We wrap rather than append because the user SQL might already have
    LIMIT / ORDER BY that'd interact badly with a naive append. The outer
    SELECT is portable across SQLite / MySQL / Postgres.
    """
    cleaned = strip_comments(sql).strip().rstrip(";")
    return f"SELECT * FROM ( {cleaned} ) AS _dm_safe LIMIT {int(row_limit)}"


class DestructiveSQLError(Exception):
    """Raised when a caller submits DDL/DML under read-only mode."""


class MultiStatementSQLError(Exception):
    """Raised when a caller tries to chain multiple statements."""
