"""DataMind v0.2 configuration.

Nested `pydantic-settings` with env prefix `DATAMIND__` and double-underscore
delimiter. Example:

    DATAMIND__LLM__API_BASE=http://35.220.164.252:3888
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

from pathlib import Path
from typing import Literal

from pydantic import AnyUrl, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root — one up from `datamind/`.
_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------


class LLMConfig(BaseModel):
    """LLM backend — an Anthropic-compatible gateway."""

    api_base: AnyUrl = Field(default=AnyUrl("http://35.220.164.252:3888"))
    api_key: SecretStr
    model: str = "claude-sonnet-4-6"
    # Used for cheap background tasks (memory extraction, summarisation).
    fallback_model: str = "claude-haiku-4-5-20251001"
    # Shared model knobs — individual calls can override.
    max_tokens: int = 4096
    temperature: float = 1.0
    # Request-level timeout applied by the Anthropic client.
    timeout_s: float = 60.0


class EmbeddingConfig(BaseModel):
    """Embedding backend. `provider` is a key into `embedding_registry`."""

    provider: str = "openai"
    api_base: AnyUrl | None = None
    api_key: SecretStr | None = None
    model: str = "text-embedding-3-small"
    # Leave None to auto-detect at first call / from model defaults.
    dimension: int | None = None
    batch_size: int = 32


class RetrievalConfig(BaseModel):
    """Retrieval strategy — `strategy` keys into `retriever_registry`."""

    strategy: str = "simple"  # simple | multi_query | hybrid | ...
    top_k: int = 5
    chunk_size: int = 512
    chunk_overlap: int = 64
    rerank: bool = False
    rerank_model: str | None = None

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
    row_limit: int = 1000
    query_timeout_s: float = 10.0


class MemoryConfig(BaseModel):
    """Memory store — `backend` keys into `memory_registry`."""

    backend: str = "sqlite"  # sqlite | redis | postgres | ...
    dsn: str | None = None
    short_term_turns: int = 20
    long_term_enabled: bool = True


class DataConfig(BaseModel):
    """Profile-based data layout. Paths derive from `profile`."""

    profile: str = "default"
    # Root resolved at import time; tests / benchmarks can override.
    base_dir: Path = _REPO_ROOT

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
        """SDK-style skill manifests: .claude/skills/<name>/SKILL.md"""
        return self.base_dir / ".claude" / "skills"


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class AgentConfig(BaseModel):
    """Agent loop backend selection.

    Two implementations live side-by-side, picked at build_agent() time:

    - `native` (default): thin tool-use loop on top of the `anthropic`
      Python SDK. Talks directly to whatever `LLMConfig.api_base` points
      at — no local helper process required. ~280 LOC, unit-testable,
      mirrors exactly what the rest of DataMind's 12 non-loop LLM calls do.

    - `sdk`: uses `claude-agent-sdk` as the loop, which in turn spawns
      the `claude` CLI and speaks Anthropic stdio to it. Best run behind
      `claude-code-router` (CCR) when the real upstream only speaks
      OpenAI `/v1/chat/completions` — CCR translates Anthropic↔OpenAI.
      You get Hooks / Subagents / Compaction / Plan mode for free.

    The two backends share the exact same tool catalogue (all 23 DataMind
    tools bridge into both) and emit the same `AgentEvent` stream, so the
    server / CLI / frontend don't need to know which is active.
    """

    backend: Literal["native", "sdk"] = "native"

    # Used only when backend == "sdk". Points at a local CCR instance.
    # Falls back to 127.0.0.1:13456 (our default bootstrap port). Auth is
    # a nominal value — CCR doesn't check the incoming key, only the one
    # it forwards to the upstream gateway (configured in CCR's config.json).
    ccr_base_url: str = "http://127.0.0.1:13456"
    ccr_api_key: SecretStr = SecretStr("dummy")

    # Cap the tool-use loop. Hits for both backends.
    max_turns: int = 12


class HooksConfig(BaseModel):
    """Phase 8 — sandboxed tool execution.

    Hooks intercept every tool dispatch and can Allow / Deny / AskUser /
    Rewrite. Three built-in hooks ship with v0.3:

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


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Top-level settings. Use `Settings()` to load from env/.env files."""

    llm: LLMConfig
    embedding: EmbeddingConfig = EmbeddingConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
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
