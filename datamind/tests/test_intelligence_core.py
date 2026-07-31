"""Bounded NL-to-DataPlan compilation and resolve() contract tests."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from datamind.adapters import (
    AnthropicStructuredModel,
    DocumentRecord,
    InMemoryDocumentSource,
    InMemoryGraphSource,
    InMemoryMemorySource,
    InMemorySkillSource,
    ScriptedModel,
    SkillRegistration,
)
from datamind.adapters.audit import InMemoryTraceStore
from datamind.dataops import ContextPack, data_plan_draft_schema
from datamind.engine import Engine
from datamind.intelligence import DataPlanCompiler
from datamind.kernel import (
    Budget,
    BudgetExceeded,
    EffectLevel,
    ExecutionFailureKind,
    ExecutionContext,
    GraphEdge,
    GraphNode,
    MemoryOriginChannel,
    PlanCompilationError,
    ReplayError,
    ResolutionEventKind,
    ScopeKind,
    ScopeRef,
    SnapshotRef,
    SnapshotSet,
    SnapshotUnavailableError,
    SourceDescriptor,
    SourceExecutionError,
    SourceKind,
    SourceRef,
    SkillKind,
    SkillSpec,
    UnsupportedPlanningError,
    Usage,
    thaw_json,
)
from datamind.lifecycle import SourceCatalog
from datamind.ports import (
    StructuredModelRequest,
    StructuredModelResponse,
)


def model_response(output, *, tokens: int = 0, response_id: str = "r1"):
    return StructuredModelResponse(
        output=output,
        model="scripted-planner",
        response_id=response_id,
        usage=Usage(tokens=tokens),
    )


def search_draft(source_id: str = "policy-docs") -> dict:
    return {
        "description": "Find the Acme policy.",
        "operations": [
            {
                "type": "search",
                "op_id": "search-policy",
                "source": source_id,
                "query": "Acme retention policy",
                "limit": 5,
                "filters_json": "{}",
            }
        ],
        "output": {"op_id": "search-policy", "path": []},
    }


class CompilerSchemaTests(unittest.TestCase):
    def test_model_schema_omits_authority_bearing_plan_fields(self) -> None:
        schema = data_plan_draft_schema()
        properties = schema["properties"]

        self.assertNotIn("plan_id", properties)
        self.assertNotIn("budget", properties)
        self.assertNotIn("max_effect", properties)
        operation_types = {
            branch["properties"]["type"]["enum"][0]
            for branch in properties["operations"]["items"]["anyOf"]
        }
        self.assertNotIn("apply_mutation", operation_types)


class ResolveExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.document_source = InMemoryDocumentSource(
            source_id="policy-docs",
            documents=(
                DocumentRecord(
                    "acme-retention",
                    "Acme retention policy requires seven years.",
                ),
                DocumentRecord(
                    "other-policy",
                    "Travel expenses require manager approval.",
                ),
            ),
        )
        self.graph_source = InMemoryGraphSource(
            source_id="enterprise-graph",
            nodes=(
                GraphNode("Acme", labels=("company",)),
                GraphNode("Shanghai", labels=("city",)),
            ),
            edges=(
                GraphEdge(
                    "e1",
                    source_id="Acme",
                    target_id="Shanghai",
                    relation="located_in",
                ),
            ),
        )
        self.catalog = SourceCatalog()
        self.catalog.register(self.document_source)
        self.catalog.register(self.graph_source)

    async def test_resolve_compiles_and_executes_cross_surface_dag(
        self,
    ) -> None:
        draft = {
            "description": "Gather policy and location evidence.",
            "operations": [
                {
                    "type": "search",
                    "op_id": "search-policy",
                    "source": "policy-docs",
                    "query": "Acme retention policy",
                    "limit": 5,
                    "filters_json": "{}",
                },
                {
                    "type": "traverse",
                    "op_id": "find-location",
                    "source": "enterprise-graph",
                    "starts": ["Acme"],
                    "start_binding": None,
                    "direction": "out",
                    "relations": ["located_in"],
                    "min_hops": 1,
                    "max_hops": 1,
                    "limit": 10,
                    "simple_paths": True,
                },
                {
                    "type": "compose",
                    "op_id": "compose-context",
                    "inputs": [
                        {"op_id": "search-policy", "path": []},
                        {"op_id": "find-location", "path": []},
                    ],
                    "strategy": "evidence_union",
                },
            ],
            "output": {"op_id": "compose-context", "path": []},
        }
        model = ScriptedModel((model_response(draft, tokens=12),))
        trace_store = InMemoryTraceStore()
        engine = Engine(
            self.catalog,
            compiler=DataPlanCompiler(model),
            trace_store=trace_store,
            replay_artifact_store=trace_store,
        )
        context = ExecutionContext.new(
            budget=Budget(max_tokens=100, max_actions=4),
        )

        resolution = await engine.resolve(
            "找到 Acme 的留存政策以及公司所在地。",
            context=context,
        )

        self.assertIsInstance(resolution.result.value, ContextPack)
        self.assertEqual(len(resolution.result.value.items), 2)
        self.assertEqual(len(resolution.result.evidence), 2)
        self.assertEqual(resolution.usage.actions, 4)
        self.assertEqual(resolution.usage.tokens, 12)
        self.assertEqual(resolution.plan.max_effect, EffectLevel.READ)
        self.assertTrue(resolution.compilation_attempts[-1].successful)
        replayed = await engine.replay(
            resolution.final_attempt.trace_id
        )
        self.assertEqual(replayed, resolution.result)
        self.assertEqual(len(model.requests), 1)

        compiler_input = json.loads(model.requests[0].input_text)
        self.assertNotIn(
            "metadata",
            compiler_input["catalog"][0],
        )
        self.assertEqual(
            {
                item["source_id"]
                for item in compiler_input["catalog"]
            },
            {"policy-docs", "enterprise-graph"},
        )

    async def test_invalid_source_gets_one_diagnostic_repair(self) -> None:
        model = ScriptedModel(
            (
                model_response(
                    search_draft("invented-source"),
                    tokens=4,
                    response_id="bad",
                ),
                model_response(
                    search_draft(),
                    tokens=5,
                    response_id="fixed",
                ),
            )
        )
        engine = Engine(
            self.catalog,
            compiler=DataPlanCompiler(model),
        )

        resolution = await engine.resolve(
            "查找 Acme 留存政策。",
            context=ExecutionContext.new(
                budget=Budget(max_tokens=100, max_actions=3),
            ),
        )

        self.assertEqual(len(resolution.compilation_attempts), 2)
        self.assertEqual(
            resolution.compilation_attempts[0].issues[0].code,
            "unknown_source",
        )
        self.assertTrue(resolution.compilation_attempts[1].successful)
        self.assertEqual(resolution.usage.actions, 3)
        repair_input = json.loads(model.requests[1].input_text)
        self.assertEqual(
            repair_input["repair"]["diagnostics"][0]["code"],
            "unknown_source",
        )

    async def test_resolve_without_compiler_is_explicitly_unsupported(
        self,
    ) -> None:
        engine = Engine(self.catalog)

        with self.assertRaises(UnsupportedPlanningError):
            await engine.resolve(
                "find policy",
                context=ExecutionContext.new(),
            )

    async def test_authorized_catalog_view_and_discover_share_boundary(
        self,
    ) -> None:
        output = {
            "description": "Discover visible document sources.",
            "operations": [
                {
                    "type": "discover",
                    "op_id": "discover-visible",
                    "kinds": ["document"],
                }
            ],
            "output": {"op_id": "discover-visible", "path": []},
        }
        model = ScriptedModel((model_response(output),))
        engine = Engine(
            self.catalog,
            compiler=DataPlanCompiler(model),
        )
        context = ExecutionContext(
            request_id="request-authorized",
            trace_id="trace-authorized",
            allowed_resources=frozenset(("policy-docs",)),
            budget=Budget(max_actions=2),
        )

        resolution = await engine.resolve(
            "列出我可使用的文档源。",
            context=context,
        )

        self.assertEqual(
            tuple(
                item.ref.source_id
                for item in resolution.result.value
            ),
            ("policy-docs",),
        )
        compiler_input = json.loads(model.requests[0].input_text)
        self.assertEqual(
            [
                item["source_id"]
                for item in compiler_input["catalog"]
            ],
            ["policy-docs"],
        )

    async def test_recoverable_source_failure_gets_one_new_plan(self) -> None:
        class FailingDocumentSource:
            descriptor = SourceDescriptor(
                ref=SourceRef("failing-docs", SourceKind.DOCUMENT),
                display_name="Failing documents",
                capabilities=frozenset(("search",)),
            )

            async def execute(self, operation, *, context):
                del operation, context
                raise RuntimeError("provider payload must remain private")

        failing_source = FailingDocumentSource()
        self.catalog.register(failing_source)
        partial_failure_draft = {
            "description": "Read two sources before composition.",
            "operations": [
                {
                    "type": "search",
                    "op_id": "search-healthy",
                    "source": "policy-docs",
                    "query": "Acme retention policy",
                    "limit": 5,
                    "filters_json": "{}",
                },
                {
                    "type": "search",
                    "op_id": "search-failing",
                    "source": "failing-docs",
                    "query": "Acme retention policy",
                    "limit": 5,
                    "filters_json": "{}",
                },
                {
                    "type": "compose",
                    "op_id": "compose",
                    "inputs": [
                        {"op_id": "search-healthy", "path": []},
                        {"op_id": "search-failing", "path": []},
                    ],
                    "strategy": "evidence_union",
                },
            ],
            "output": {"op_id": "compose", "path": []},
        }
        model = ScriptedModel(
            (
                model_response(
                    partial_failure_draft,
                    tokens=3,
                    response_id="first-plan",
                ),
                model_response(
                    search_draft("policy-docs"),
                    tokens=4,
                    response_id="replacement-plan",
                ),
            )
        )
        store = InMemoryTraceStore()
        engine = Engine(
            self.catalog,
            compiler=DataPlanCompiler(model),
            trace_store=store,
            replay_artifact_store=store,
        )
        context = ExecutionContext.new(
            budget=Budget(max_tokens=100, max_actions=5),
        )

        resolution = await engine.resolve(
            "查找 Acme 留存政策。",
            context=context,
        )

        self.assertEqual(resolution.resolution_id, context.trace_id)
        self.assertEqual(len(resolution.plan_attempts), 2)
        first, second = resolution.plan_attempts
        self.assertFalse(first.successful)
        self.assertTrue(second.successful)
        self.assertEqual(
            first.failure.kind,
            ExecutionFailureKind.SOURCE,
        )
        self.assertTrue(first.failure.recoverable)
        self.assertEqual(first.failure.failed_op_id, "search-failing")
        self.assertEqual(first.failure.source_id, "failing-docs")
        self.assertEqual(
            first.failure.completed_op_ids,
            ("search-healthy",),
        )
        self.assertEqual(first.execution_usage.actions, 2)
        self.assertNotEqual(first.trace_id, second.trace_id)
        self.assertNotEqual(context.trace_id, second.trace_id)
        self.assertEqual(resolution.usage.actions, 5)

        recovery_input = json.loads(model.requests[1].input_text)
        recovery = recovery_input["runtime_recovery"]
        self.assertEqual(recovery["attempt_number"], 2)
        self.assertEqual(
            recovery["failure"]["kind"],
            ExecutionFailureKind.SOURCE.value,
        )
        self.assertEqual(
            recovery["failure"]["source_id"],
            "failing-docs",
        )
        self.assertNotIn(
            "provider payload must remain private",
            json.dumps(recovery, ensure_ascii=False),
        )

        parent = await store.get_resolution(context.trace_id)
        self.assertTrue(parent.completed)
        parent_payload = json.dumps(
            [thaw_json(event.details) for event in parent.events],
            ensure_ascii=False,
        )
        self.assertNotIn(
            "provider payload must remain private",
            parent_payload,
        )
        self.assertEqual(
            tuple(event.kind for event in parent.events),
            (
                ResolutionEventKind.RESOLUTION_STARTED,
                ResolutionEventKind.PLAN_ATTEMPT_STARTED,
                ResolutionEventKind.PLAN_ATTEMPT_FAILED,
                ResolutionEventKind.PLAN_ATTEMPT_STARTED,
                ResolutionEventKind.PLAN_ATTEMPT_COMPLETED,
                ResolutionEventKind.RESOLUTION_COMPLETED,
            ),
        )
        failed_trace = await store.get(first.trace_id)
        final_trace = await store.get(second.trace_id)
        self.assertTrue(failed_trace.failed)
        self.assertTrue(final_trace.completed)
        with self.assertRaises(ReplayError):
            await engine.replay(first.trace_id)
        replayed = await engine.replay(second.trace_id)
        self.assertEqual(replayed, resolution.result)

    async def test_snapshot_failure_is_terminal_and_not_replanned(
        self,
    ) -> None:
        model = ScriptedModel(
            (
                model_response(search_draft(), response_id="only-plan"),
                model_response(
                    search_draft(),
                    response_id="must-not-be-used",
                ),
            )
        )
        store = InMemoryTraceStore()
        engine = Engine(
            self.catalog,
            compiler=DataPlanCompiler(model),
            trace_store=store,
            replay_artifact_store=store,
        )
        unavailable = SnapshotRef(
            source=self.document_source.descriptor.ref,
            version="missing",
            observed_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
        context = ExecutionContext.new(
            snapshots=SnapshotSet((unavailable,)),
            budget=Budget(max_actions=4),
        )

        with self.assertRaises(SnapshotUnavailableError):
            await engine.resolve(
                "查找 Acme 留存政策。",
                context=context,
            )

        self.assertEqual(len(model.requests), 1)
        parent = await store.get_resolution(context.trace_id)
        self.assertTrue(parent.failed)
        failure_event = parent.events[-2]
        self.assertEqual(
            failure_event.kind,
            ResolutionEventKind.PLAN_ATTEMPT_FAILED,
        )
        self.assertEqual(
            failure_event.details["failure"]["kind"],
            ExecutionFailureKind.SNAPSHOT.value,
        )
        self.assertFalse(failure_event.details["will_replan"])

    async def test_replanning_cannot_exceed_original_action_budget(
        self,
    ) -> None:
        class FailingDocumentSource:
            descriptor = SourceDescriptor(
                ref=SourceRef("failing-docs", SourceKind.DOCUMENT),
                display_name="Failing documents",
                capabilities=frozenset(("search",)),
            )

            async def execute(self, operation, *, context):
                del operation, context
                raise RuntimeError("unavailable")

        self.catalog.register(FailingDocumentSource())
        model = ScriptedModel(
            (
                model_response(search_draft("failing-docs")),
                model_response(search_draft("policy-docs")),
            )
        )
        store = InMemoryTraceStore()
        engine = Engine(
            self.catalog,
            compiler=DataPlanCompiler(model),
            trace_store=store,
            replay_artifact_store=store,
        )
        context = ExecutionContext.new(
            budget=Budget(max_actions=2),
        )

        with self.assertRaises(BudgetExceeded):
            await engine.resolve(
                "查找 Acme 留存政策。",
                context=context,
            )

        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.remaining, 1)
        parent = await store.get_resolution(context.trace_id)
        self.assertTrue(parent.failed)
        self.assertEqual(
            parent.events[-1].details["usage"]["actions"],
            2,
        )

    async def test_runtime_replanning_stops_after_one_replacement(
        self,
    ) -> None:
        class FailingDocumentSource:
            descriptor = SourceDescriptor(
                ref=SourceRef("failing-docs", SourceKind.DOCUMENT),
                display_name="Failing documents",
                capabilities=frozenset(("search",)),
            )

            async def execute(self, operation, *, context):
                del operation, context
                raise RuntimeError("still unavailable")

        self.catalog.register(FailingDocumentSource())
        response = model_response(search_draft("failing-docs"))
        model = ScriptedModel((response, response, response))
        store = InMemoryTraceStore()
        engine = Engine(
            self.catalog,
            compiler=DataPlanCompiler(model),
            trace_store=store,
            replay_artifact_store=store,
        )
        context = ExecutionContext.new(
            budget=Budget(max_actions=5),
        )

        with self.assertRaises(SourceExecutionError):
            await engine.resolve(
                "查找 Acme 留存政策。",
                context=context,
            )

        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.remaining, 1)
        parent = await store.get_resolution(context.trace_id)
        self.assertTrue(parent.failed)
        failed_attempts = tuple(
            event
            for event in parent.events
            if event.kind
            is ResolutionEventKind.PLAN_ATTEMPT_FAILED
        )
        self.assertEqual(len(failed_attempts), 2)
        self.assertTrue(
            failed_attempts[0].details["will_replan"]
        )
        self.assertFalse(
            failed_attempts[1].details["will_replan"]
        )


class GovernedResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_skill_policy_is_bound_from_trusted_catalog(
        self,
    ) -> None:
        async def normalize(arguments, context):
            del context
            return {"label": arguments["risk"].upper()}

        spec = SkillSpec(
            name="normalize-risk",
            version="1.0.0",
            description="Normalize a project risk label.",
            instructions="Return an uppercase risk label.",
            kind=SkillKind.EXECUTABLE,
            input_schema={
                "type": "object",
                "properties": {"risk": {"type": "string"}},
                "required": ["risk"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
                "additionalProperties": False,
            },
        )
        source = InMemorySkillSource(
            source_id="enterprise-skills",
            registrations=(
                SkillRegistration(spec=spec, handler=normalize),
            ),
        )
        catalog = SourceCatalog()
        catalog.register(source)
        output = {
            "description": "Normalize the supplied risk label.",
            "operations": [
                {
                    "type": "invoke_skill",
                    "op_id": "normalize",
                    "source": "enterprise-skills",
                    "skill": {
                        "name": spec.name,
                        "version": spec.version,
                        "digest": spec.digest,
                    },
                    "arguments_json": '{"risk":"critical"}',
                    "argument_bindings": [],
                }
            ],
            "output": {"op_id": "normalize", "path": []},
        }
        invalid_output = json.loads(json.dumps(output))
        invalid_output["operations"][0]["arguments_json"] = "{}"
        engine = Engine(
            catalog,
            compiler=DataPlanCompiler(
                ScriptedModel(
                    (
                        model_response(
                            invalid_output,
                            response_id="invalid-arguments",
                        ),
                        model_response(
                            output,
                            response_id="valid-arguments",
                        ),
                    )
                )
            ),
        )

        resolution = await engine.resolve(
            "规范化 critical 风险标签。",
            context=ExecutionContext.new(
                budget=Budget(max_actions=3),
            ),
        )

        operation = resolution.plan.operations[0]
        self.assertEqual(
            resolution.compilation_attempts[0].issues[0].code,
            "invalid_skill_arguments",
        )
        self.assertEqual(operation.governed_effect, EffectLevel.PURE)
        self.assertEqual(
            thaw_json(resolution.result.value.output),
            {"label": "CRITICAL"},
        )

    async def test_resolve_can_propose_but_not_apply_memory(self) -> None:
        scope = ScopeRef(ScopeKind.PRINCIPAL, "alice")
        memory = InMemoryMemorySource(
            source_id="enterprise-memory",
            records=(),
            observed_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
        catalog = SourceCatalog()
        catalog.register(memory)
        draft = {
            "description": "Propose an explicit user preference.",
            "operations": [
                {
                    "type": "propose_mutation",
                    "op_id": "propose-preference",
                    "source": "enterprise-memory",
                    "draft": {
                        "scope": {
                            "kind": "principal",
                            "scope_id": "alice",
                        },
                        "changes": [
                            {
                                "action": "assert",
                                "kind": "preference",
                                "content": "User prefers concise reports.",
                                "valid_from": None,
                                "valid_to": None,
                                "links": [],
                                "metadata_json": "{}",
                            }
                        ],
                    },
                }
            ],
            "output": {
                "op_id": "propose-preference",
                "path": [],
            },
        }
        engine = Engine(
            catalog,
            compiler=DataPlanCompiler(
                ScriptedModel((model_response(draft),))
            ),
        )

        resolution = await engine.resolve(
            "记住我偏好简洁报告。",
            context=ExecutionContext.new(
                readable_scopes=frozenset((scope,)),
                writable_scopes=frozenset((scope,)),
                memory_origin=MemoryOriginChannel.USER_EXPLICIT,
                budget=Budget(max_actions=2),
            ),
        )

        self.assertEqual(len(resolution.proposed_mutations), 1)
        proposal = resolution.proposed_mutations[0]
        self.assertEqual(proposal.draft.scope, scope)
        self.assertEqual(proposal.base_snapshot.version, "1")
        self.assertEqual(
            proposal.draft.idempotency_key,
            "resolve:{}:propose-preference".format(
                resolution.request_id
            ),
        )

    async def test_write_skill_is_hidden_and_cannot_be_compiled(self) -> None:
        write_calls = []

        async def write_handler(arguments, context):
            del context
            write_calls.append(arguments)
            return {"status": "written"}

        write_spec = SkillSpec(
            name="record-approval",
            version="1.0.0",
            description="Record an approval.",
            instructions="Write approval state.",
            kind=SkillKind.EXECUTABLE,
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                },
                "required": ["project_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
                "additionalProperties": False,
            },
            effect_level=EffectLevel.INTERNAL_WRITE,
        )
        source = InMemorySkillSource(
            source_id="enterprise-skills",
            registrations=(
                SkillRegistration(
                    spec=write_spec,
                    handler=write_handler,
                ),
            ),
        )
        catalog = SourceCatalog()
        catalog.register(source)
        output = {
            "description": "Attempt a forbidden write.",
            "operations": [
                {
                    "type": "invoke_skill",
                    "op_id": "invoke-write",
                    "source": "enterprise-skills",
                    "skill": {
                        "name": write_spec.name,
                        "version": write_spec.version,
                        "digest": write_spec.digest,
                    },
                    "arguments_json": '{"project_id":"p1"}',
                    "argument_bindings": [],
                }
            ],
            "output": {"op_id": "invoke-write", "path": []},
        }
        model = ScriptedModel((model_response(output),))
        engine = Engine(
            catalog,
            compiler=DataPlanCompiler(model, max_attempts=1),
        )

        with self.assertRaises(PlanCompilationError):
            await engine.resolve(
                "记录 p1 的审批。",
                context=ExecutionContext.new(
                    max_effect=EffectLevel.INTERNAL_WRITE,
                ),
            )

        self.assertEqual(write_calls, [])
        compiler_input = json.loads(model.requests[0].input_text)
        governed = compiler_input["catalog"][0]["schema"][
            "governed_skills"
        ]
        self.assertEqual(governed, [])


class AnthropicModelAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_adapter_forces_one_strict_tool_call(self) -> None:
        calls = []

        class UsageValue:
            input_tokens = 7
            output_tokens = 3

        class Response:
            id = "provider-response"
            model = "claude-test"
            usage = UsageValue()
            content = [
                {
                    "type": "tool_use",
                    "name": "result",
                    "input": {"ok": True},
                }
            ]

        class Messages:
            async def create(self, **kwargs):
                calls.append(kwargs)
                return Response()

        class Client:
            messages = Messages()

        adapter = AnthropicStructuredModel(
            Client(),
            model="claude-test",
        )
        response = await adapter.generate_structured(
            StructuredModelRequest(
                instruction="Return the result.",
                input_text="input",
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                schema_name="result",
            )
        )

        self.assertEqual(thaw_json(response.output), {"ok": True})
        self.assertEqual(response.usage.tokens, 10)
        self.assertTrue(calls[0]["tools"][0]["strict"])
        self.assertEqual(
            calls[0]["tool_choice"]["name"],
            "result",
        )


if __name__ == "__main__":
    unittest.main()
