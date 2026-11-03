"""KB providers — import to populate the registries.

Each submodule registers itself with `retriever_registry` (or
`vector_store_registry`). Callers grab them by name via the registry.
"""
from __future__ import annotations

from . import lancedb_store  # noqa: F401
from . import chroma_store  # noqa: F401 — vector_store_registry
from . import hybrid_retriever  # noqa: F401 — retriever_registry
from . import multi_query_retriever  # noqa: F401
from . import simple_retriever  # noqa: F401

__all__: list[str] = []
