"""Engine-default merging must preserve caller-supplied options."""
from __future__ import annotations

from datamind.capabilities.db import base as base_module
from datamind.capabilities.db.base import BaseSQLDialect
from datamind.capabilities.db.providers.sqlite import SQLiteDialect


def test_explicit_options_override_defaults_without_duplicate_keywords(monkeypatch):
    seen = {}
    sentinel = object()

    def fake_create_engine(dsn, **options):
        seen["dsn"] = dsn
        seen["options"] = options
        return sentinel

    monkeypatch.setattr(base_module, "create_engine", fake_create_engine)
    caller_options = {"pool_pre_ping": False, "future": False, "connect_args": {"x": 1}}

    result = BaseSQLDialect().build_engine("sqlite://", **caller_options)

    assert result is sentinel
    assert seen == {
        "dsn": "sqlite://",
        "options": {
            "pool_pre_ping": False,
            "future": False,
            "connect_args": {"x": 1},
        },
    }
    assert caller_options == {"pool_pre_ping": False, "future": False, "connect_args": {"x": 1}}


def test_sqlite_wrapper_accepts_an_explicit_default_override(tmp_path):
    dialect = SQLiteDialect()
    engine = dialect.build_engine(
        None, default_path=str(tmp_path / "options.db"), pool_pre_ping=False,
    )
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    engine.dispose()
