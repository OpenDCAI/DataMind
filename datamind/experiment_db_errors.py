"""Construct opt-in DB experiment exceptions without contacting a server.

Expected retry decisions are experiment labels, independent of recovery's rules.
Optional drivers are imported only when their fault is selected.
"""
from dataclasses import dataclass
import importlib
import sqlite3

from sqlalchemy.exc import DBAPIError

from datamind.core.errors import ConfigError


class FaultDriverUnavailable(ConfigError):
    """An optional driver required by the selected experiment is absent."""


@dataclass(frozen=True)
class DBFault:
    backend: str
    code: int | str
    retryable: bool
    kind: str = "OperationalError"
    native_name: str = ""


DB_FAULTS = {
    "sqlite_busy": DBFault("sqlite", 5, True, native_name="SQLITE_BUSY"),
    "sqlite_busy_snapshot": DBFault("sqlite", 517, True, native_name="SQLITE_BUSY_SNAPSHOT"),
    "sqlite_locked": DBFault("sqlite", 6, False, native_name="SQLITE_LOCKED"),
    "sqlite_error": DBFault("sqlite", 1, False, native_name="SQLITE_ERROR"),
    "sqlite_readonly": DBFault("sqlite", 8, False, native_name="SQLITE_READONLY"),
    "sqlite_interrupt": DBFault("sqlite", 9, False, native_name="SQLITE_INTERRUPT"),
    "sqlite_corrupt": DBFault("sqlite", 11, False, "DatabaseError", "SQLITE_CORRUPT"),
    "sqlite_cantopen": DBFault("sqlite", 14, False, native_name="SQLITE_CANTOPEN"),
    "sqlite_constraint": DBFault("sqlite", 19, False, "IntegrityError", "SQLITE_CONSTRAINT"),
    "sqlite_auth": DBFault("sqlite", 23, False, "DatabaseError", "SQLITE_AUTH"),
    "mysql_lock_timeout": DBFault("mysql", 1205, True),
    "mysql_deadlock": DBFault("mysql", 1213, True),
    "mysql_server_gone": DBFault("mysql", 2006, True),
    "mysql_connection_lost": DBFault("mysql", 2013, True),
    "mysql_auth": DBFault("mysql", 1045, False),
    "mysql_unknown_database": DBFault("mysql", 1049, False),
    "mysql_permission": DBFault("mysql", 1142, False),
    "mysql_syntax": DBFault("mysql", 1064, False, "ProgrammingError"),
    "mysql_missing_table": DBFault("mysql", 1146, False, "ProgrammingError"),
    "mysql_missing_column": DBFault("mysql", 1054, False, "ProgrammingError"),
    "mysql_duplicate": DBFault("mysql", 1062, False, "IntegrityError"),
    "postgres_connection_failure": DBFault("postgresql", "08006", True),
    "postgres_serialization": DBFault("postgresql", "40001", True),
    "postgres_deadlock": DBFault("postgresql", "40P01", True),
    "postgres_startup": DBFault("postgresql", "57P03", True),
    "postgres_auth": DBFault("postgresql", "28P01", False),
    "postgres_permission": DBFault("postgresql", "42501", False),
    "postgres_syntax": DBFault("postgresql", "42601", False),
    "postgres_missing_table": DBFault("postgresql", "42P01", False),
    "postgres_missing_column": DBFault("postgresql", "42703", False),
    "postgres_duplicate": DBFault("postgresql", "23505", False),
    "postgres_cancelled": DBFault("postgresql", "57014", False),
    "postgres_protocol": DBFault("postgresql", "08P01", False),
}


def build_db_fault(name, *, wrapped=True):
    """Return an actual driver exception, optionally mapped by SQLAlchemy."""
    if name not in DB_FAULTS:
        raise ConfigError("Unknown database experiment error")
    spec = DB_FAULTS[name]
    try:
        if spec.backend == "sqlite":
            driver = sqlite3
            original = getattr(driver, spec.kind)("injected experiment DB fault")
            original.sqlite_errorcode = spec.code
            original.sqlite_errorname = spec.native_name
        elif spec.backend == "mysql":
            driver = importlib.import_module("pymysql")
            original = getattr(driver.err, spec.kind)(spec.code, "injected experiment DB fault")
        else:
            driver = importlib.import_module("psycopg")
            original = driver.errors.lookup(spec.code)("injected experiment DB fault")
    except ImportError as exc:
        extra = "mysql" if spec.backend == "mysql" else "postgres"
        raise FaultDriverUnavailable(f"Install datamind[{extra}] for {spec.backend} fault simulation") from exc
    error = DBAPIError.instance(None, None, original, driver.Error) if wrapped else original
    error.datamind_fault_injected = True
    return error
