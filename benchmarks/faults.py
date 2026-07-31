"""Deterministic adapter-level fault injection for failure tasks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from datamind.kernel import (
    ExecutionContext,
    KernelValidationError,
    SourceDescriptor,
    SourceExecutionError,
)
from datamind.ports import DataSource, SourceResult


@dataclass(frozen=True)
class FaultRule:
    """Fail selected ordinal calls of one operation kind."""

    operation: str
    calls: Tuple[int, ...] = (1,)
    message: str = "deterministic benchmark fault"

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise KernelValidationError(
                "fault operation must be non-empty"
            )
        object.__setattr__(self, "calls", tuple(self.calls))
        if not self.calls or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item <= 0
            for item in self.calls
        ):
            raise KernelValidationError(
                "fault calls must contain positive integers"
            )
        if len(set(self.calls)) != len(self.calls):
            raise KernelValidationError(
                "fault calls cannot contain duplicates"
            )
        if not isinstance(self.message, str) or not self.message.strip():
            raise KernelValidationError(
                "fault message must be non-empty"
            )


class FaultInjectingSource:
    """Wrap one source without changing Executor or provider semantics."""

    def __init__(
        self,
        source: DataSource,
        *,
        rules: Tuple[FaultRule, ...],
    ) -> None:
        if not isinstance(
            getattr(source, "descriptor", None),
            SourceDescriptor,
        ) or not callable(getattr(source, "execute", None)):
            raise KernelValidationError(
                "fault wrapper requires a DataSource"
            )
        self._source = source
        self._rules = tuple(rules)
        if any(not isinstance(item, FaultRule) for item in self._rules):
            raise KernelValidationError(
                "fault rules must contain FaultRule values"
            )
        keys = tuple(item.operation for item in self._rules)
        if len(set(keys)) != len(keys):
            raise KernelValidationError(
                "fault wrapper supports one rule per operation"
            )
        self._by_operation = {
            item.operation: item for item in self._rules
        }
        self._calls: Dict[str, int] = {}

    @property
    def descriptor(self):
        return self._source.descriptor

    async def current_snapshot(self):
        method = getattr(self._source, "current_snapshot", None)
        if not callable(method):
            raise SourceExecutionError(
                "wrapped source does not expose current_snapshot"
            )
        return await method()

    async def has_snapshot(self, snapshot) -> bool:
        method = getattr(self._source, "has_snapshot", None)
        if not callable(method):
            return False
        return await method(snapshot)

    async def execute(
        self,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        operation_name = getattr(operation, "operation", "")
        ordinal = self._calls.get(operation_name, 0) + 1
        self._calls[operation_name] = ordinal
        rule: Optional[FaultRule] = self._by_operation.get(operation_name)
        if rule is not None and ordinal in rule.calls:
            raise SourceExecutionError(rule.message)
        return await self._source.execute(operation, context=context)
