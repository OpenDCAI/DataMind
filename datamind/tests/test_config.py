"""Settings parsing tests.

Validate that:
- Nested env vars (DATAMIND__LLM__API_KEY) hydrate correctly.
- Missing required fields (e.g. llm.api_key) raise at construction time.
- `data.profile` drives `storage_dir` / `data_dir` without code changes.
- Optional sections fall back to sane defaults.

Tests rely on `pydantic-settings` reading env vars — we set them via
monkeypatch to stay hermetic.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from datamind.config import DataConfig, Settings


def test_nested_env_hydrates_required_llm(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATAMIND__LLM__API_BASE", "http://gw.example:3888")
    monkeypatch.setenv("DATAMIND__LLM__API_KEY", "sk-abc")
    monkeypatch.setenv("DATAMIND__LLM__MODEL", "claude-opus-4-7")

    s = Settings()

    assert str(s.llm.api_base).startswith("http://gw.example:3888")
    assert s.llm.api_key.get_secret_value() == "sk-abc"
    assert s.llm.model == "claude-opus-4-7"


def test_missing_api_key_raises(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Explicitly clear the key even if it leaked from the real env
    monkeypatch.delenv("DATAMIND__LLM__API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_profile_paths_switch_in_lockstep(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATAMIND__LLM__API_KEY", "sk-x")
    monkeypatch.setenv("DATAMIND__DATA__PROFILE", "my_kb")

    s = Settings()

    assert s.data.profile == "my_kb"
    assert s.data.data_dir.as_posix().endswith("/data/profiles/my_kb")
    assert s.data.storage_dir.as_posix().endswith("/storage/my_kb")


def test_db_dialect_is_plain_string(monkeypatch, tmp_path):
    """Adding a new dialect must be ENV-only — no Settings surgery."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATAMIND__LLM__API_KEY", "sk-x")
    monkeypatch.setenv("DATAMIND__DB__DIALECT", "mysql")
    monkeypatch.setenv(
        "DATAMIND__DB__DSN",
        "mysql+pymysql://user:pw@host:3306/dbname",
    )

    s = Settings()

    assert s.db.dialect == "mysql"
    assert s.db.dsn.startswith("mysql+pymysql://")


def test_chunk_overlap_validation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATAMIND__LLM__API_KEY", "sk-x")
    monkeypatch.setenv("DATAMIND__RETRIEVAL__CHUNK_SIZE", "128")
    monkeypatch.setenv("DATAMIND__RETRIEVAL__CHUNK_OVERLAP", "256")

    with pytest.raises(ValidationError):
        Settings()


def test_optional_sections_have_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATAMIND__LLM__API_KEY", "sk-x")
    s = Settings()

    assert s.embedding.provider == "openai"
    assert s.retrieval.strategy == "simple"
    assert s.graph.backend == "networkx"
    assert s.db.dialect == "sqlite"
    assert s.memory.backend == "sqlite"


def test_ensure_dirs_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATAMIND__LLM__API_KEY", "sk-x")
    monkeypatch.setenv("DATAMIND__DATA__PROFILE", "tp")

    s = Settings()
    # override base so we stay in tmp_path
    s.data.base_dir = tmp_path
    s.ensure_dirs()
    s.ensure_dirs()  # second call must not raise

    assert (tmp_path / "data" / "profiles" / "tp").is_dir()
    assert (tmp_path / "storage" / "tp").is_dir()


@pytest.mark.parametrize(
    "profile",
    ["../escape", r"..\escape", "nested/name", ".", "..", ""],
)
def test_profile_name_cannot_escape_profile_root(profile, tmp_path):
    with pytest.raises(ValidationError):
        DataConfig(base_dir=tmp_path, profile=profile)


def test_profile_assignment_keeps_path_boundary(tmp_path):
    config = DataConfig(base_dir=tmp_path)
    with pytest.raises(ValidationError):
        config.profile = "../escape"
