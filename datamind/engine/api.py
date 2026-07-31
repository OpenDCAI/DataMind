"""Thin authoritative Python API over deterministic Core components."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from datamind.dataops import ApplyMutation, DataPlan, ResultEnvelope
from datamind.kernel import (
    ChangeSet,
    EffectLevel,
    ExecutionFailure,
    ExecutionContext,
    MemoryMutationProposal,
    MemoryMutationReceipt,
    MemoryOriginChannel,
    OutcomeRecord,
    ScopeKind,
    SourceDescriptor,
    SourceKind,
    SyncReceipt,
    UnsupportedPlanningError,
    UnsupportedOutcomeError,
    UnsupportedSyncError,
    Usage,
    new_id,
    thaw_json,
)
from datamind.ports import (
    LifecyclePort,
    OutcomeStore,
    PlanCompilerPort,
    PlanningRequest,
    ReplanningContext,
    ReplayArtifactStore,
    ResolutionTraceStore,
    SourceCatalogPort,
    TraceStore,
)

from .executor import Executor
from .resolution import (
    PlanAttempt,
    Resolution,
    collect_proposed_mutations,
)
from .resolution_recording import ResolutionRecorder


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
        resolution_trace_store: Optional[ResolutionTraceStore] = None,
        outcome_store: Optional[OutcomeStore] = None,
        max_parallelism: int = 4,
    ) -> None:
        self._catalog = catalog
        self._lifecycle = lifecycle
        self._compiler = compiler
        self._outcome_store = outcome_store
        self._executor = Executor(
            catalog,
            trace_store=trace_store,
            artifact_store=replay_artifact_store,
            max_parallelism=max_parallelism,
        )
        if (
            resolution_trace_store is None
            and isinstance(trace_store, ResolutionTraceStore)
        ):
            resolution_trace_store = trace_store
        self._resolution_recorder = ResolutionRecorder(
            resolution_trace_store
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
        """Resolve through at most two independently traced read plans."""

        if self._compiler is None:
            raise UnsupportedPlanningError(
                "engine.resolve() requires a configured PlanCompilerPort"
            )
        planning_effect = min(context.max_effect, EffectLevel.READ)
        planning_sources = self._planning_sources(context)
        readable_scopes = tuple(
            sorted(
                context.readable_scopes,
                key=lambda item: (
                    item.kind.value,
                    item.scope_id,
                ),
            )
        )
        writable_scopes = self._proposal_scopes(context)
        resolution_id = context.trace_id
        total_usage = Usage()
        plan_attempts = []
        replanning = None
        await self._resolution_recorder.start(
            resolution_id,
            request_id=context.request_id,
            intent=request,
            budget=context.budget,
            source_catalog=planning_sources,
        )
        try:
            for attempt_number in (1, 2):
                planning_request = PlanningRequest(
                    intent=request,
                    request_id=context.request_id,
                    sources=planning_sources,
                    readable_scopes=readable_scopes,
                    writable_scopes=writable_scopes,
                    max_effect=planning_effect,
                    budget=context.budget.remaining(total_usage),
                    replanning=replanning,
                )
                compiled = await self._compiler.compile(
                    planning_request
                )
                total_usage = total_usage + compiled.usage
                context.budget.require(total_usage)
                child_trace_id = new_id("trace")
                await self._resolution_recorder.start_attempt(
                    resolution_id,
                    attempt_number=attempt_number,
                    trace_id=child_trace_id,
                    compiled=compiled,
                )
                execution_context = replace(
                    context,
                    trace_id=child_trace_id,
                    max_effect=planning_effect,
                    budget=context.budget.remaining(total_usage),
                )
                outcome = await self._executor.execute_attempt(
                    compiled.plan,
                    context=execution_context,
                )
                total_usage = total_usage + outcome.usage
                context.budget.require(total_usage)
                if outcome.result is not None:
                    attempt = PlanAttempt(
                        number=attempt_number,
                        trace_id=child_trace_id,
                        plan=compiled.plan,
                        compilation_attempts=compiled.attempts,
                        compilation_usage=compiled.usage,
                        execution_usage=outcome.result.usage,
                    )
                    plan_attempts.append(attempt)
                    await self._resolution_recorder.complete_attempt(
                        resolution_id,
                        attempt_number=attempt_number,
                        trace_id=child_trace_id,
                        result=outcome.result,
                    )
                    resolution = Resolution(
                        resolution_id=resolution_id,
                        request_id=context.request_id,
                        plan_attempts=tuple(plan_attempts),
                        result=outcome.result,
                        usage=total_usage,
                        proposed_mutations=collect_proposed_mutations(
                            outcome.result.value
                        ),
                    )
                    await self._resolution_recorder.complete(
                        resolution_id,
                        attempt_count=len(plan_attempts),
                        final_trace_id=child_trace_id,
                        usage=total_usage,
                    )
                    return resolution

                assert outcome.failure is not None
                assert outcome.error is not None
                attempt = PlanAttempt(
                    number=attempt_number,
                    trace_id=child_trace_id,
                    plan=compiled.plan,
                    compilation_attempts=compiled.attempts,
                    compilation_usage=compiled.usage,
                    execution_usage=outcome.failure.usage,
                    failure=outcome.failure,
                )
                plan_attempts.append(attempt)
                will_replan = (
                    attempt_number == 1
                    and self._can_replan(
                        compiled.plan,
                        outcome.failure,
                    )
                )
                await self._resolution_recorder.fail_attempt(
                    resolution_id,
                    attempt_number=attempt_number,
                    trace_id=child_trace_id,
                    failure=outcome.failure,
                    will_replan=will_replan,
                )
                if not will_replan:
                    raise outcome.error
                replanning = ReplanningContext(
                    attempt_number=attempt_number + 1,
                    previous_plan=compiled.plan,
                    failure=outcome.failure,
                )
            raise AssertionError("bounded resolution loop did not terminate")
        except Exception as error:
            await self._resolution_recorder.fail(
                resolution_id,
                error,
                attempt_count=len(plan_attempts),
                usage=total_usage,
            )
            raise

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

    async def record_outcome(
        self,
        outcome: OutcomeRecord,
    ) -> OutcomeRecord:
        """Append an external verdict without interpreting or learning from it."""

        if self._outcome_store is None:
            raise UnsupportedOutcomeError(
                "engine.record_outcome() requires a configured OutcomeStore"
            )
        return await self._outcome_store.record(outcome)

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
    def _can_replan(
        plan: DataPlan,
        failure: ExecutionFailure,
    ) -> bool:
        return (
            failure.recoverable
            and plan.max_effect <= EffectLevel.READ
            and all(
                operation.effect.level <= EffectLevel.READ
                for operation in plan.operations
            )
        )

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
