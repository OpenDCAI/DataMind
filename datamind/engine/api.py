"""Thin authoritative Python API over deterministic Core components."""
from __future__ import annotations

from typing import Any, Optional

from datamind.dataops import ApplyMutation, ResultEnvelope
from datamind.kernel import (
    ChangeSet,
    ExecutionContext,
    MemoryMutationProposal,
    MemoryMutationReceipt,
    SyncReceipt,
    UnsupportedSyncError,
)
from datamind.ports import (
    LifecyclePort,
    ReplayArtifactStore,
    SourceCatalogPort,
    TraceStore,
)

from .executor import Executor


class Engine:
    """Compose execution, synchronization, and replay without provider logic."""

    def __init__(
        self,
        catalog: SourceCatalogPort,
        *,
        lifecycle: Optional[LifecyclePort] = None,
        trace_store: Optional[TraceStore] = None,
        replay_artifact_store: Optional[ReplayArtifactStore] = None,
        max_parallelism: int = 4,
    ) -> None:
        self._lifecycle = lifecycle
        self._executor = Executor(
            catalog,
            trace_store=trace_store,
            artifact_store=replay_artifact_store,
            max_parallelism=max_parallelism,
        )

    async def sync(self, change_set: ChangeSet) -> SyncReceipt:
        if self._lifecycle is None:
            raise UnsupportedSyncError(
                "engine.sync() requires a configured LifecyclePort"
            )
        return await self._lifecycle.sync(change_set)

    async def execute(
        self,
        operation_or_plan: Any,
        *,
        context: ExecutionContext,
    ) -> ResultEnvelope[Any]:
        return await self._executor.execute(
            operation_or_plan,
            context=context,
        )

    async def apply(
        self,
        proposal: MemoryMutationProposal,
        *,
        context: ExecutionContext,
    ) -> ResultEnvelope[MemoryMutationReceipt]:
        """Apply a validated Memory proposal through normal execution."""

        return await self.execute(
            ApplyMutation(
                source=proposal.source,
                proposal=proposal,
            ),
            context=context,
        )

    async def replay(self, trace_id: str) -> ResultEnvelope[Any]:
        return await self._executor.replay(trace_id)


__all__ = ["Engine"]
