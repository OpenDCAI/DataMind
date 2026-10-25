"""Generic pluggable registry.

Every extensible capability (embedding providers, DB dialects, retrieval
strategies, graph backends, memory stores) shares the same pattern:

    @embedding_registry.register("openai")
    class OpenAIEmbedding: ...

    provider = embedding_registry.create("openai", api_key="...", model="...")

Unknown names raise `ConfigError` with a hint listing what IS registered.
Duplicate names raise `ConfigError` — this is a programming error, not a
recoverable one.

Registries are *module-global* by design so importing a provider module is
enough to make it available anywhere.
"""
from __future__ import annotations

from typing import Callable, Generic, TypeVar

from .errors import ConfigError

T = TypeVar("T")


class Registry(Generic[T]):
    """Name -> class registry with typed factory access."""

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._entries: dict[str, type[T]] = {}

    # ------------------------------------------------------------------ reg

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        """Decorator: register `cls` under `name` in this registry."""
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{self._kind} provider name must be a non-empty str")

        def deco(cls: type[T]) -> type[T]:
            if name in self._entries:
                raise ConfigError(
                    f"{self._kind} provider '{name}' already registered "
                    f"(existing={self._entries[name].__module__}.{self._entries[name].__name__}, "
                    f"new={cls.__module__}.{cls.__name__})"
                )
            self._entries[name] = cls
            cls.name = name # Set the backend identifier for runtime backend detection.
            return cls

        return deco

    # ---------------------------------------------------------------- lookup

    def create(self, name: str, **cfg) -> T:
        """Instantiate the provider named `name` with keyword config."""
        cls = self._entries.get(name)
        if cls is None:
            known = ", ".join(sorted(self._entries)) or "<none>"
            raise ConfigError(
                f"Unknown {self._kind} provider '{name}'. Registered: {known}"
            )
        return cls(**cfg)  # type: ignore[call-arg]

    def get_class(self, name: str) -> type[T]:
        cls = self._entries.get(name)
        if cls is None:
            known = ", ".join(sorted(self._entries)) or "<none>"
            raise ConfigError(
                f"Unknown {self._kind} provider '{name}'. Registered: {known}"
            )
        return cls

    def known(self) -> list[str]:
        return sorted(self._entries)

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)


# --- Typed global registries ------------------------------------------------
#
# We intentionally do NOT import the Protocol types here (circular): the
# Registry is generic over any T. Provider modules still benefit from static
# type checking when they declare their class.

embedding_registry: Registry = Registry("embedding")
retriever_registry: Registry = Registry("retriever")
graph_registry: Registry = Registry("graph")
db_registry: Registry = Registry("database")
memory_registry: Registry = Registry("memory")
vector_store_registry: Registry = Registry("vector_store")

__all__ = [
    "Registry",
    "embedding_registry",
    "retriever_registry",
    "graph_registry",
    "db_registry",
    "memory_registry",
    "vector_store_registry",
]
