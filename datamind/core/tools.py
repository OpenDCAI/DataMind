"""Tool framework.

A Tool is anything the agent can call. We abstract it so that:

1. Tools have a typed schema (Anthropic tool_use format: name / description
   / input_schema).
2. Tools declare their dependencies (an `AppContext`-like services bag) via
   a factory function, so registering a tool doesn't instantiate it eagerly.
3. Tools can be grouped, filtered, and serialised to the Anthropic API in
   one call.

Design notes:
- We don't couple to Anthropic-specific types — `to_anthropic_tool()` is a
  single method on ToolSpec. Swapping providers later stays local.
- Tool handlers are async. Sync code should wrap itself via asyncio.to_thread.
- Errors raised from a handler become a tool_result with `is_error=True`;
  the agent loop in Phase 7 handles that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .errors import ConfigError
from .contracts import DataSurface, ToolAccess
from .registry import Registry

# Handler signature: async callable from JSON input -> JSON-serialisable output.
ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    """Declarative description of a tool + its async handler.

    `name` is unique across a single agent session. `input_schema` is a
    standard JSON Schema document (Draft 2020-12 subset) exactly as the
    Anthropic /v1/messages API expects.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    # Free-form metadata for UI / audit / grouping (e.g. {"group": "kb"}).
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def access(self) -> ToolAccess:
        """Return the declared access role, defaulting legacy tools to read."""
        raw = self.metadata.get("access", ToolAccess.READ.value)
        try:
            return ToolAccess(str(raw))
        except ValueError as exc:
            raise ConfigError(f"Tool '{self.name}' has invalid access role: {raw!r}") from exc

    @property
    def surface(self) -> DataSurface | None:
        raw = self.metadata.get("surface")
        if raw is None:
            return None
        try:
            return DataSurface(str(raw))
        except ValueError as exc:
            raise ConfigError(f"Tool '{self.name}' has invalid data surface: {raw!r}") from exc

    def to_anthropic_tool(self) -> dict[str, Any]:
        """Serialise to the JSON object the Anthropic API accepts in `tools=`."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@runtime_checkable
class ToolProvider(Protocol):
    """A provider that contributes one or more ToolSpecs.

    Used by MCP-server-style modules that want to expose a bundle of related
    tools (e.g. the KB server exposes kb_search + kb_list_documents + kb_reindex).
    """

    def build(self, **services: Any) -> list[ToolSpec]: ...


# Global registry — MCP-server-like bundles register themselves here.
tool_provider_registry: Registry = Registry("tool_provider")


class ToolRegistry:
    """A runtime-assembled collection of ToolSpecs, keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ConfigError(f"Tool name collision: '{spec.name}' already registered")
        self._tools[spec.name] = spec

    def extend(self, specs: list[ToolSpec]) -> None:
        for s in specs:
            self.add(s)

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ConfigError(
                f"Unknown tool '{name}'. Available: {', '.join(sorted(self._tools)) or '<none>'}"
            )
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def as_anthropic_tools(self) -> list[dict[str, Any]]:
        return [t.to_anthropic_tool() for t in self._tools.values()]

    def select(
        self,
        *,
        access: set[ToolAccess] | None = None,
        surfaces: set[DataSurface] | None = None,
    ) -> "ToolRegistry":
        """Create a role-scoped registry without copying handlers.

        This is the enforcement point used by the two agent assemblers.  A
        RetrieveAgent registry accepts read/utility tools while a StoreAgent
        registry accepts write tools; prompts are not trusted for isolation.
        """
        selected = ToolRegistry()
        for spec in self._tools.values():
            if access is not None and spec.access not in access:
                continue
            if surfaces is not None and spec.surface not in surfaces:
                continue
            selected.add(spec)
        return selected

    def assert_access(self, allowed: set[ToolAccess]) -> None:
        disallowed = [s.name for s in self._tools.values() if s.access not in allowed]
        if disallowed:
            raise ConfigError(
                "Tool registry violates its access boundary: " + ", ".join(sorted(disallowed))
            )

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


__all__ = [
    "ToolSpec",
    "ToolHandler",
    "ToolProvider",
    "ToolRegistry",
    "tool_provider_registry",
]
