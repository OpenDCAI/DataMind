"""DataMind v1.0 configuration.

Nested `pydantic-settings` with env prefix `DATAMIND__` and double-underscore
delimiter. Example:

    DATAMIND__LLM__API_BASE=https://gateway.example.com
    DATAMIND__LLM__MODEL=claude-sonnet-4-6
    DATAMIND__EMBEDDING__PROVIDER=openai
    DATAMIND__DB__DIALECT=mysql

Design choices:
- SecretStr for credentials so they never leak into repr / logs.
- Every extensible axis is just a string name that keys into a Registry
  (provider / dialect / strategy / backend). Adding a new option is a pure
  "register a class" operation — config.py doesn't change.
- `DataConfig` derives paths from `profile`; switching `profile` moves data
  and storage in lockstep.
- Legacy flat vars (LLM_API_BASE, EMBEDDING_MODEL, ...) are left untouched
  for the old `modules/*` codepath. They don't influence new-stack Settings.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root — one up from `datamind/`.
_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------


class LLMConfig(BaseModel):
    """Protocol-explicit LLM gateway configuration.

    ``protocol`` describes the wire protocol, independently from the model
    name and from ``agent.backend``.  Internal generation (NL2SQL, query
    rewriting, memory and ingest) uses the same client as the outer loop.
    """

    api_base: AnyUrl = Field(default=AnyUrl("https://api.anthropic.com"))
    api_key: SecretStr = SecretStr("")
    protocol: Literal["anthropic", "openai_chat_completions"] = "anthropic"
    model: str = "claude-sonnet-4-6"
    # Used for cheap background tasks (memory extraction, summarisation).
    fallback_model: str = "claude-haiku-4-5-20251001"
    fallback_protocol: Literal["anthropic", "openai_chat_completions"] | None = None
    # Shared model knobs — individual calls can override.
    max_tokens: int = 4096
    temperature: float = 1.0
    # Request-level timeout applied by the Anthropic client.
    timeout_s: float = 60.0
    connect_timeout_s: float = 10.0
    max_retries: int = Field(default=3, ge=0)
    backoff_base_s: float = Field(default=0.5, gt=0)


class CodexHostConfig(BaseModel):
    """Host-level Codex integration, separate from data capability configuration."""
    model: str | None = None
    reasoning_effort: str | None = None
    base_url: AnyUrl | None = None
    api_key: SecretStr | None = None
    workspace: Path | None = None
    approval_mode: Literal['auto_review', 'deny_all'] = 'auto_review'
    preapproved_tools: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=600, gt=0)
    max_concurrency: int = Field(default=4, ge=1)
    request_max_retries: int = Field(default=2, ge=0)
    stream_idle_timeout_ms: int = Field(default=300000, ge=1000)


class EmbeddingConfig(BaseModel):
    """Embedding backend. `provider` is a key into `embedding_registry`."""

    provider: str = "openai"
    api_base: AnyUrl | None = None
    api_key: SecretStr | None = None
    model: str = "text-embedding-3-small"
    # Leave None to auto-detect at first call / from model defaults.
    dimension: int | None = None
    batch_size: int = 32
    timeout_s: float = Field(default=30.0, gt=0)
    connect_timeout_s: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    backoff_base_s: float = Field(default=0.5, gt=0)
    # Optional conservative split in addition to batch_size.  It uses a
    # deterministic character estimate (roughly four characters per token).
    max_batch_tokens: int | None = Field(default=None, gt=0)


class RetrievalConfig(BaseModel):
    """Retrieval strategy — `strategy` keys into `retriever_registry`."""

    strategy: str = "simple"  # simple | multi_query | hybrid | ...
    top_k: int = 5
    chunk_size: int = 512
    chunk_overlap: int = 64
    rerank: bool = False
    rerank_model: str | None = None
    # Opt-in read degradation; no source/metadata/validation errors are hidden.
    vector_failure_fallback: Literal["error", "bm25"] = "error"
    vector_retries: int = Field(default=0, ge=0, le=5)
    vector_timeout_s: float | None = Field(default=None, gt=0)

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_lt_size(cls, v: int, info) -> int:
        size = info.data.get("chunk_size", 512)
        if v >= size:
            raise ValueError(f"chunk_overlap ({v}) must be < chunk_size ({size})")
        return v


class GraphConfig(BaseModel):
    """Graph store — `backend` keys into `graph_registry`."""

    backend: str = "networkx"  # networkx | neo4j | ...
    dsn: str | None = None     # e.g. bolt://user:pw@host:7687
    embed_entities: bool = False


class DBConfig(BaseModel):
    """SQL database — `dialect` keys into `db_registry`."""

    dialect: str = "sqlite"  # sqlite | mysql | postgres | ...
    dsn: str | None = None   # e.g. mysql+pymysql://user:pw@host/db
    read_only: bool = True
    row_limit: int = Field(default=1000, ge=1)
    query_timeout_s: float = 10.0


class MemoryConfig(BaseModel):
    """Memory store — `backend` keys into `memory_registry`."""

    backend: str = "sqlite"  # sqlite | redis | postgres | ...
    dsn: str | None = None
    short_term_turns: int = 20
    long_term_enabled: bool = True


class DataConfig(BaseModel):
    """Profile-based data layout. Paths derive from `profile`."""

    model_config = ConfigDict(validate_assignment=True)

    profile: str = "default"
    # Root resolved at import time; tests / benchmarks can override.
    base_dir: Path = _REPO_ROOT

    @field_validator("profile")
    @classmethod
    def _profile_name(cls, value: str) -> str:
        if value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError(
                "profile must contain only letters, numbers, '.', '_' or '-'"
            )
        return value

    @property
    def data_dir(self) -> Path:
        """Per-profile raw data: data/profiles/<profile>/"""
        return self.base_dir / "data" / "profiles" / self.profile

    @property
    def storage_dir(self) -> Path:
        """Per-profile index/DB storage: storage/<profile>/"""
        return self.base_dir / "storage" / self.profile

    @property
    def bench_dir(self) -> Path:
        """Cross-profile benchmark question sets."""
        return self.base_dir / "data" / "bench"

    @property
    def skills_dir(self) -> Path:
        """SDK-style skill manifests: <name>/SKILL.md.

        Resolution order:
          1. Repo-level `<base_dir>/.claude/skills/` (matches Anthropic CLI
             convention; developers running from a checkout get this for free)
          2. Package-bundled `datamind/skills/` (pip-install users get a
             default skill set without cloning the repo)
          3. ENV `DATAMIND__DATA__SKILLS_DIR` overrides everything (operators
             pointing the agent at their own skill catalog)
        """
        user_dir = self.base_dir / ".claude" / "skills"
        if user_dir.is_dir():
            return user_dir
        # Bundled fallback. Path(__file__) → datamind/config.py
        # parent → datamind/, /skills → packaged skill set.
        bundled = Path(__file__).resolve().parent / "skills"
        if bundled.is_dir():
            return bundled
        # Last resort: return the user path even if it doesn't exist —
        # SkillsService will simply load 0 skills, no crash.
        return user_dir

    @property
    def profile_skills_dir(self) -> Path:
        """Writable, profile-scoped procedural Skills."""
        return self.data_dir / "skills"


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class AgentConfig(BaseModel):
    """Budgets retained for deterministic baseline loops and workspace reads.
    Product requests always use the Codex host configuration.
    """
    backend: Literal['codex'] = 'codex'

    max_turns: int = 12
    max_tool_calls: int = Field(default=24, ge=0)
    max_input_tokens: int = Field(default=120_000, gt=0)
    max_tool_result_chars: int = Field(default=12_000, ge=256)
    max_tool_result_rows: int = Field(default=100, ge=1)
    wall_clock_timeout_s: float = Field(default=300.0, gt=0)
    finalization_reserve_s: float = Field(default=20.0, ge=0)


class HooksConfig(BaseModel):
    """Phase 8 — sandboxed tool execution.

    Hooks intercept every tool dispatch and can Allow / Deny / AskUser /
    Rewrite. Three built-in hooks ship with v1.0:

    - `path_allowlist`     — refuse paths outside the profile data dir
    - `destructive_sql`    — AskUser on DELETE/UPDATE/DROP, Deny on
                             DROP DATABASE
    - `audit_log`          — append every tool call to audit.jsonl with
                             a hash chain so tampering is detectable

    Set `enabled=False` to bypass the chain entirely (useful for the
    -hooks control arm of R3 red-team experiments).

    `path_allowlist_extra` lets operators extend the allow-list beyond
    the default (profile data dir + cwd). Each entry is a directory that
    path-bearing tools (kb_add_file / db_import_csv / ...) may touch.
    """

    enabled: bool = True
    destructive_sql: bool = True
    path_allowlist: bool = True
    audit_log: bool = True
    # Additional roots the path allow-list will accept on top of profile
    # data_dir and cwd. List of absolute paths (or `~`-prefixed).
    path_allowlist_extra: list[str] = Field(default_factory=list)

class KBConfig(BaseModel):
    ingest_mode: Literal["direct", "cocoindex"] = "direct"
    vector_store: Literal["chroma", "lancedb"] = "chroma"
    lancedb_uri: str = "./lancedb_data"
    cocoindex_inbox: str = "./cocoindex_inbox"

    @model_validator(mode="after")
    def _validate(self):
        if self.ingest_mode == "cocoindex" and self.vector_store != "lancedb":
            raise ValueError(
                "ingest_mode='cocoindex' requires vector_store='lancedb' "
                f"(got vector_store='{self.vector_store}')"
            )
        return self

# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------


class WorkspaceConfig(BaseModel):
    """Storage layout and history retention are independent policies."""
    storage_mode: Literal['auto', 'snapshot', 'incremental'] = 'auto'
    retention: Literal['auto', 'latest', 'recent', 'all'] = 'auto'
    keep_last: int = Field(default=10, ge=1)
    auto_gc: bool = True
    lease_ttl_s: float = Field(default=1800, ge=10)
    lock_timeout_s: float = Field(default=120, gt=0)


class Settings(BaseSettings):
    """Top-level settings. Use `Settings()` to load from env/.env files."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    codex: CodexHostConfig = Field(default_factory=CodexHostConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    embedding: EmbeddingConfig = EmbeddingConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    kb: KBConfig = KBConfig()
    graph: GraphConfig = GraphConfig()
    db: DBConfig = DBConfig()
    memory: MemoryConfig = MemoryConfig()
    data: DataConfig = DataConfig()
    logging: LoggingConfig = LoggingConfig()
    agent: AgentConfig = AgentConfig()
    hooks: HooksConfig = HooksConfig()

    model_config = SettingsConfigDict(
        # Two files tried in order; later entries take precedence.
        env_file=(".env", ".env.datamind"),
        env_nested_delimiter="__",
        env_prefix="DATAMIND__",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ misc

    def ensure_dirs(self) -> None:
        """Create per-profile directories if missing. Idempotent."""
        self.data.data_dir.mkdir(parents=True, exist_ok=True)
        self.data.storage_dir.mkdir(parents=True, exist_ok=True)


__all__ = [
    "Settings",
    "DataSettings",
    "CodexHostConfig",
    "WorkspaceConfig",
    "LLMConfig",
    "EmbeddingConfig",
    "RetrievalConfig",
    "GraphConfig",
    "DBConfig",
    "MemoryConfig",
    "DataConfig",
    "LoggingConfig",
    "AgentConfig",
    "HooksConfig",
]


# Compatibility name for callers introduced with the direct data layer.
DataSettings = Settings
