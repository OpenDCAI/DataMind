"""Core runtime primitives: protocols, registry, context, errors, logging, tools."""

from .context import RequestContext
from .errors import (
    CapabilityError,
    ConfigError,
    DataMindError,
    ExternalServiceError,
)
from .logging import bind_context, current_context, get_logger, setup_logging
from .registry import (
    Registry,
    db_registry,
    embedding_registry,
    graph_registry,
    memory_registry,
    retriever_registry,
    vector_store_registry,
)
from .tools import (
    ToolHandler,
    ToolProvider,
    ToolRegistry,
    ToolSpec,
    tool_provider_registry,
)
from .contracts import (
    DataSurface,
    ToolAccess,
    SourceRef,
    SurfaceWriteResult,
    IngestReceipt,
    Evidence,
    InferenceResult,
)

__all__ = [
    # Errors
    "DataMindError",
    "ConfigError",
    "CapabilityError",
    "ExternalServiceError",
    # Context + logging
    "RequestContext",
    "setup_logging",
    "get_logger",
    "bind_context",
    "current_context",
    # Registry
    "Registry",
    "embedding_registry",
    "retriever_registry",
    "graph_registry",
    "db_registry",
    "memory_registry",
    "vector_store_registry",
    # Tools
    "ToolSpec",
    "ToolHandler",
    "ToolProvider",
    "ToolRegistry",
    "tool_provider_registry",
    "DataSurface",
    "ToolAccess",
    "SourceRef",
    "SurfaceWriteResult",
    "IngestReceipt",
    "Evidence",
    "InferenceResult",
]
