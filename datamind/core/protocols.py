"""Capability protocols.

These are the contracts the agent and MCP servers talk to. Concrete provider
classes in `datamind.capabilities.<cap>.providers.*` must satisfy these,
and register themselves via `datamind.core.registry.<cap>_registry`.

Data-transfer objects (Pydantic models) are defined here too so providers
don't each invent their own shape. Keep them minimal — extensions belong
in provider-specific config, not in the protocol.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Model protocol DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0


@dataclass(frozen=True)
class ModelResponse:
    """Vendor-neutral assistant response.

    Blocks use the small common subset understood by the agent loop:
    ``{"type": "text", "text": ...}`` and
    ``{"type": "tool_use", "id": ..., "name": ..., "input": {...}}``.
    """

    content: list[dict[str, Any]]
    stop_reason: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    resolved_model: str | None = None


@dataclass(frozen=True)
class ModelStreamEvent:
    type: Literal["text", "done"]
    delta: str = ""
    response: ModelResponse | None = None


@runtime_checkable
class TextModelClient(Protocol):
    """Protocol-neutral text generation used by internal capabilities."""

    protocol: str

    async def generate_text(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ToolCallingModelClient(TextModelClient, Protocol):
    """Common contract for Anthropic and OpenAI tool calling."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        tool_choice: str | None = None,
    ) -> ModelResponse: ...

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        tool_choice: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]: ...


# ---------------------------------------------------------------------------
# DTOs — shared across capabilities
# ---------------------------------------------------------------------------


class RetrievedChunk(BaseModel):
    """A document fragment returned by a Retriever."""

    id: str
    text: str
    score: float = 0.0
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphTriple(BaseModel):
    subject: str
    relation: str
    object: str
    subject_type: str = "entity"
    object_type: str = "entity"
    confidence: float = 1.0
    source: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class Entity(BaseModel):
    id: str
    label: str
    type: str = "entity"
    score: float = 0.0
    properties: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    source: str
    target: str
    relation: str
    weight: float = 1.0
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphPath(BaseModel):
    nodes: list[str]
    edges: list[Edge]
    score: float = 0.0


class ColumnSchema(BaseModel):
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False


class TableSchema(BaseModel):
    name: str
    columns: list[ColumnSchema]
    row_count_estimate: int | None = None


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False
    elapsed_ms: float = 0.0


class MemoryItem(BaseModel):
    """A single long-term memory entry.

    `scope` (global / profile / session) is the v0.3 multi-tenant boundary.
    `kind` is a typed tag agent/UI can filter on.
    `status` distinguishes active vs archived (soft-deleted) without losing
    the audit trail.

    `namespace` is retained for v0.2 backward compatibility — providers
    that haven't migrated still surface it; v0.3 callers should rely on
    (scope, profile, session_id) instead.
    """

    id: str
    scope: Literal["global", "profile", "session"] = "profile"
    profile: str | None = None
    session_id: str | None = None
    kind: Literal["preference", "decision", "workflow", "summary", "skill", "fact"] = "fact"
    status: Literal["active", "archived"] = "active"
    content: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None

    # ---- v0.2 compat shim ------------------------------------------------
    namespace: str | None = None  # populated by providers that still use it


# ---------------------------------------------------------------------------
# Capability protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Text (and optionally image) embedding backend.

    Providers are constructed via `embedding_registry.create(name, **cfg)` and
    should be cheap to instantiate but may lazily load heavy models.
    """

    name: str
    dimension: int

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, query: str) -> list[float]: ...

    # Optional hook — providers that support it can implement:
    # async def embed_images(self, paths: Sequence[str]) -> list[list[float]]: ...


@runtime_checkable
class VectorStore(Protocol):
    """Persistent vector index.

    A VectorStore is created once per (profile, collection) pair. Indexer
    code writes chunks in, retrievers query. The contract is intentionally
    small — advanced features (metadata filters, hybrid search) live in
    retriever strategies, not here.
    """

    dimension: int

    async def add(
        self,
        ids: Sequence[str],
        texts: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> None: ...

    async def query(
        self,
        embedding: Sequence[float],
        *,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]: ...

    async def count(self) -> int: ...

    async def delete(self, ids: Sequence[str]) -> None: ...

    async def reset(self) -> None: ...

    async def get_all_texts(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Return (id, text, metadata) for every stored chunk.

        Used by lexical retrievers (BM25) that rebuild their index in memory
        on startup. Providers that can't enumerate may raise NotImplementedError.
        """
        ...


@runtime_checkable
class Retriever(Protocol):
    """KB retrieval strategy (simple / multi-query / hybrid / rerank)."""

    async def aretrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]: ...


@runtime_checkable
class GraphStore(Protocol):
    """Knowledge graph backend (NetworkX / Neo4j / ArangoDB / ...)."""

    async def upsert_triples(self, triples: Sequence[GraphTriple]) -> None: ...

    async def search_entities(self, query: str, *, top_k: int = 5) -> list[Entity]: ...

    async def traverse(
        self,
        start: str,
        *,
        max_hops: int = 2,
        relation_filter: list[str] | None = None,
        max_results: int = 100,
    ) -> list[GraphPath]: ...

    async def neighbors(
        self,
        entity: str,
        *,
        direction: str = "both",
        relation_filter: list[str] | None = None,
        limit: int = 100,
    ) -> list[Edge]: ...


@runtime_checkable
class DatabaseDialect(Protocol):
    """SQL backend (SQLite / MySQL / Postgres / ...).

    Built atop SQLAlchemy so dialects mostly differ in DSN parsing, quoting,
    and read-only enforcement. Each dialect must implement a safeguard
    (`is_destructive`) that runs BEFORE `execute_readonly`.
    """

    name: str

    def build_engine(self, dsn: str, **kwargs: Any) -> Any:
        """Return a SQLAlchemy Engine (or async equivalent)."""
        ...

    async def list_tables(self, engine: Any) -> list[str]: ...

    async def describe(self, engine: Any, table: str) -> TableSchema: ...

    async def execute_readonly(
        self,
        engine: Any,
        sql: str,
        *,
        row_limit: int = 1000,
        timeout_s: float = 10.0,
    ) -> QueryResult: ...

    def is_destructive(self, sql: str) -> bool:
        """True if `sql` would modify data or schema (INSERT/UPDATE/DELETE/DDL)."""
        ...


@runtime_checkable
class MemoryStore(Protocol):
    """Long-term memory storage with three-scope partitioning (v0.3).

    Items live in one of three scopes:
      * ``global``  — applies to every tenant and session
                      ("respond in Chinese", "always cite sources")
      * ``profile`` — bound to a tenant/project
                      (per-customer terminology, project conventions)
      * ``session`` — confined to one conversation thread

    Recall is the union of scope-conditioned top-k retrievals, so callers
    that don't care about scoping can still pass `profile=None,
    session_id=None` and get only the global slice.

    Providers may use embeddings, BM25, or any other ranking — the contract
    only requires `recall` returns a ranked list and respects the scope
    filter. ``status`` defaults to ``active`` and supports soft-delete via
    ``forget``.
    """

    async def save(
        self,
        content: str,
        *,
        scope: Literal["global", "profile", "session"] = "profile",
        profile: str | None = None,
        session_id: str | None = None,
        kind: Literal["preference", "decision", "workflow", "summary", "skill", "fact"] = "fact",
        metadata: dict[str, Any] | None = None,
    ) -> str: ...

    async def recall(
        self,
        query: str,
        *,
        profile: str | None = None,
        session_id: str | None = None,
        top_k: int = 8,
        kinds: Sequence[str] | None = None,
        include_archived: bool = False,
    ) -> list[MemoryItem]: ...

    async def forget(self, item_id: str, *, hard: bool = False) -> bool:
        """Soft-delete by default (status=archived). `hard=True` deletes the row."""
        ...

    async def list_profiles(self) -> list[str]:
        """List every profile that currently has at least one active item."""
        ...
