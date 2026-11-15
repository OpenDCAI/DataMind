"""Conservative error-code classification for replayable database reads.

True means the caller may retry an independent read in a fresh transaction;
it does not authorize replaying writes or statements inside a failed transaction.
False means a recognized driver error is not on the retry allowlist. None means
this is not a recognized driver error, so generic recovery rules may apply.
Optional database drivers are deliberately not imported here.
"""
from __future__ import annotations

import sqlite3


_MYSQL_TRANSIENT = frozenset({1205, 1213, 2006, 2013})
_POSTGRES_TRANSIENT = frozenset({
    "08000",  # connection_exception
    "08001",  # sqlclient_unable_to_establish_sqlconnection
    "08003",  # connection_does_not_exist
    "08006",  # connection_failure
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "57P03",  # cannot_connect_now (e.g. server startup)
})


def _from_driver(exc: Exception, *modules: str) -> bool:
    # Include driver bases so application-defined subclasses retain their rules.
    return any(
        cls.__module__ == module or cls.__module__.startswith(module + ".")
        for cls in type(exc).__mro__
        for module in modules
    )


def classify_db_error(exc: Exception) -> bool | None:
    """Classify a raw driver exception using codes, never message substrings."""
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, "sqlite_errorcode", None)
        # Extended codes retain the primary code in the low eight bits.
        # BUSY_SNAPSHOT requires a new transaction, not a statement-level retry.
        # LOCKED can indicate a local cursor/schema conflict: do not retry it
        # indiscriminately, nor INTERRUPT, corruption, SQL or permission errors.
        return type(code) is int and (code & 0xFF) == sqlite3.SQLITE_BUSY

    if _from_driver(exc, "pymysql", "MySQLdb", "mysql.connector"):
        code = getattr(exc, "errno", None)
        if code is None and exc.args:
            code = exc.args[0]
        return type(code) is int and code in _MYSQL_TRANSIENT

    if _from_driver(exc, "psycopg", "psycopg2"):
        code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        # Do not allow every 08xxx error: 08P01 is a protocol violation.
        # In particular, 57014 (query cancellation) must not restart the query.
        return isinstance(code, str) and code in _POSTGRES_TRANSIENT

    return None
