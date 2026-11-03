"""DB dialect providers — importing this package registers everything."""
from __future__ import annotations

from . import dolt  # noqa: F401
from . import sqlite  # noqa: F401

# MySQL is registered even without pymysql installed — we only fail when
# the user tries to actually build an engine. This keeps the base install
# lean while letting the registry advertise capabilities.
from . import mysql  # noqa: F401

__all__: list[str] = []
