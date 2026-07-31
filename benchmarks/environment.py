"""Fresh, isolated runtime environments for benchmark tasks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from datamind.adapters.audit import (
    InMemoryOutcomeStore,
    InMemoryTraceStore,
)
from datamind.engine import Engine
from datamind.kernel import ExecutionContext
from datamind.lifecycle import SourceCatalog
from datamind.ports import PlanCompilerPort

from .schema import TaskSpec


@dataclass
class BenchmarkEnvironment:
    """Own all mutable state for exactly one task run."""

    catalog: SourceCatalog
    trace_store: InMemoryTraceStore = field(
        default_factory=InMemoryTraceStore
    )
    outcome_store: InMemoryOutcomeStore = field(
        default_factory=InMemoryOutcomeStore
    )
    state: Dict[str, Any] = field(default_factory=dict)
    cleanup: Optional[Callable[[], None]] = None

    def engine(
        self,
        *,
        compiler: Optional[PlanCompilerPort] = None,
    ) -> Engine:
        return Engine(
            self.catalog,
            compiler=compiler,
            trace_store=self.trace_store,
            replay_artifact_store=self.trace_store,
            resolution_trace_store=self.trace_store,
            outcome_store=self.outcome_store,
        )

    def context(
        self,
        task: TaskSpec,
        run_id: str,
        *,
        suffix: str = "main",
    ) -> ExecutionContext:
        spec = task.context
        return ExecutionContext(
            request_id="bench-request-{}-{}".format(run_id, suffix),
            trace_id="bench-trace-{}-{}".format(run_id, suffix),
            approvals=frozenset(spec.approvals),
            allowed_resources=frozenset(spec.allowed_resources),
            readable_scopes=frozenset(spec.readable_scopes),
            writable_scopes=frozenset(spec.writable_scopes),
            max_effect=spec.max_effect,
            budget=spec.budget,
            memory_origin=spec.memory_origin,
        )

    def close(self) -> None:
        if self.cleanup is not None:
            cleanup = self.cleanup
            self.cleanup = None
            cleanup()
