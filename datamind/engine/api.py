"""Thin authoritative Python API over deterministic Core components."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from datamind.dataops import ApplyMutation, ResultEnvelope
from datamind.kernel import (
    ChangeSet,
    EffectLevel,
    ExecutionContext,
    MemoryMutationProposal,
    MemoryMutationReceipt,
    MemoryOriginChannel,
    ScopeKind,
    SourceDescriptor,
    SourceKind,
    SyncReceipt,
    UnsupportedPlanningError,
    UnsupportedSyncError,
    thaw_json,
)
from datamind.ports import (
    LifecyclePort,
    PlanCompilerPort,
    PlanningRequest,
    ReplayArtifactStore,
    SourceCatalogPort,
    TraceStore,
)

from .executor import Executor
from .resolution import Resolution, collect_proposed_mutations


class Engine:
    """Compose execution, synchronization, and replay without provider logic."""

    def __init__(
        self,
        catalog: SourceCatalogPort,
        *,
        lifecycle: Optional[LifecyclePort] = None,
        trace_store: Optional[TraceStore] = None,
        replay_artifact_store: Optional[ReplayArtifactStore] = None,
        compiler: Optional[PlanCompilerPort] = None,
        max_parallelism: int = 4,
    ) -> None:
        self._catalog = catalog
        self._lifecycle = lifecycle
        self._compiler = compiler
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

    async def resolve(
        self,
        request: str,
        *,
        context: ExecutionContext,
    ) -> Resolution:
        """Compile one request into a read-bounded plan, then execute it."""

        if self._compiler is None:
            raise UnsupportedPlanningError(
                "engine.resolve() requires a configured PlanCompilerPort"
            )
        planning_effect = min(context.max_effect, EffectLevel.READ)
        planning_request = PlanningRequest(
            intent=request,
            request_id=context.request_id,
            sources=self._planning_sources(context),
            readable_scopes=tuple(
                sorted(
                    context.readable_scopes,
                    key=lambda item: (
                        item.kind.value,
                        item.scope_id,
                    ),
                )
            ),
            writable_scopes=self._proposal_scopes(context),
            max_effect=planning_effect,
            budget=context.budget,
        )
        compiled = await self._compiler.compile(planning_request)
        execution_context = replace(
            context,
            max_effect=planning_effect,
            budget=context.budget.remaining(compiled.usage),
        )
        result = await self._executor.execute(
            compiled.plan,
            context=execution_context,
        )
        total_usage = compiled.usage + result.usage
        context.budget.require(total_usage)
        return Resolution(
            request_id=context.request_id,
            plan=compiled.plan,
            result=result,
            compilation_attempts=compiled.attempts,
            compilation_usage=compiled.usage,
            usage=total_usage,
            proposed_mutations=collect_proposed_mutations(
                result.value
            ),
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

    def _planning_sources(
        self,
        context: ExecutionContext,
    ) -> tuple:
        descriptors = self._catalog.descriptors()
        selected = []
        proposal_scopes = self._proposal_scopes(context)
        for source_id in sorted(descriptors):
            if (
                context.allowed_resources
                and source_id not in context.allowed_resources
            ):
                continue
            descriptor = descriptors[source_id]
            capabilities = set(descriptor.capabilities)
            capabilities.discard("apply_mutation")
            if not proposal_scopes:
                capabilities.discard("propose_mutation")
            schema = thaw_json(descriptor.schema)
            if descriptor.ref.kind is SourceKind.SKILL:
                schema["governed_skills"] = [
                    item
                    for item in schema.get("governed_skills", ())
                    if EffectLevel[str(item["effect"])] <= EffectLevel.READ
                    and not item.get("requires_approval", False)
                ]
            selected.append(
                SourceDescriptor(
                    ref=descriptor.ref,
                    display_name=descriptor.display_name,
                    capabilities=frozenset(capabilities),
                    max_effect=min(
                        descriptor.max_effect,
                        EffectLevel.READ,
                    ),
                    version=descriptor.version,
                    schema=schema,
                )
            )
        return tuple(selected)

    @staticmethod
    def _proposal_scopes(
        context: ExecutionContext,
    ) -> tuple:
        if context.memory_origin is not MemoryOriginChannel.USER_EXPLICIT:
            return ()
        safe_kinds = frozenset(
            (ScopeKind.SESSION, ScopeKind.PRINCIPAL)
        )
        return tuple(
            sorted(
                (
                    scope
                    for scope in (
                        context.readable_scopes
                        & context.writable_scopes
                    )
                    if scope.kind in safe_kinds
                ),
                key=lambda item: (
                    item.kind.value,
                    item.scope_id,
                ),
            )
        )


__all__ = ["Engine"]
