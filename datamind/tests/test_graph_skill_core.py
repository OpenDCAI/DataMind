"""Typed Graph, governed Skills, and bounded runtime binding tests."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from datamind.adapters import (
    InMemoryGraphSource,
    InMemorySkillSource,
    SQLiteReadSource,
    SkillRegistration,
)
from datamind.adapters.audit import InMemoryTraceStore
from datamind.dataops import (
    ArgumentBinding,
    DataPlan,
    GraphDirection,
    InvokeSkill,
    OutputRef,
    Project,
    Query,
    ResolveSkill,
    ResultKind,
    Traverse,
    ValueBinding,
    plan_from_json,
    plan_to_json,
    validate_plan,
)
from datamind.engine import Executor
from datamind.kernel import (
    Budget,
    EffectLevel,
    EffectPolicyError,
    ExecutionContext,
    ExecutionError,
    GraphEdge,
    GraphNode,
    GraphPathSet,
    KernelValidationError,
    SkillKind,
    SkillSpec,
    SourceExecutionError,
)
from datamind.lifecycle import SourceCatalog


class GraphSkillCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self._temporary_directory.name) / "projects.sqlite3"
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "CREATE TABLE projects "
                "(project_id TEXT, entity TEXT, risk TEXT)"
            )
            connection.executemany(
                "INSERT INTO projects VALUES (?, ?, ?)",
                (
                    ("p1", "Acme", "critical"),
                    ("p2", "Ann", "low"),
                ),
            )
            connection.commit()

        self.table_source = SQLiteReadSource(
            source_id="projects-db",
            database_path=self.database_path,
        )
        self.graph_source = InMemoryGraphSource(
            source_id="enterprise-graph",
            nodes=(
                GraphNode("Acme", labels=("company",)),
                GraphNode("Ann", labels=("person",)),
                GraphNode("Shanghai", labels=("city",)),
                GraphNode("China", labels=("country",)),
            ),
            edges=(
                GraphEdge(
                    "e1",
                    source_id="Ann",
                    target_id="Acme",
                    relation="works_at",
                ),
                GraphEdge(
                    "e2",
                    source_id="Acme",
                    target_id="Shanghai",
                    relation="located_in",
                ),
                GraphEdge(
                    "e3",
                    source_id="Shanghai",
                    target_id="China",
                    relation="in_country",
                ),
            ),
        )
        self.risk_spec = SkillSpec(
            name="risk-label",
            version="1.0.0",
            description="Normalize an enterprise project risk label.",
            instructions=(
                "Use this Skill when a project risk label must be normalized."
            ),
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
        self.instruction_spec = SkillSpec(
            name="review-policy",
            version="1.0.0",
            description="Review a policy document for missing controls.",
            instructions=(
                "Read the policy evidence and enumerate missing controls."
            ),
        )
        self.write_calls = 0
        self.write_spec = SkillSpec(
            name="record-approval",
            version="1.0.0",
            description="Record an approval in governed internal state.",
            instructions="Record an approved project only after policy checks.",
            kind=SkillKind.EXECUTABLE,
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
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
        self.skill_source = InMemorySkillSource(
            source_id="enterprise-skills",
            registrations=(
                SkillRegistration(
                    spec=self.risk_spec,
                    handler=self._normalize_risk,
                ),
                SkillRegistration(spec=self.instruction_spec),
                SkillRegistration(
                    spec=self.write_spec,
                    handler=self._record_approval,
                ),
            ),
        )
        self.catalog = SourceCatalog()
        self.catalog.register(self.table_source)
        self.catalog.register(self.graph_source)
        self.catalog.register(self.skill_source)
        self.executor = Executor(self.catalog)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    @staticmethod
    async def _normalize_risk(arguments, context):
        del context
        return {"label": arguments["risk"].upper()}

    async def _record_approval(self, arguments, context):
        del arguments, context
        self.write_calls += 1
        return {"status": "recorded"}

    @staticmethod
    def context(
        *,
        max_effect: EffectLevel = EffectLevel.READ,
        budget: Budget = Budget(),
    ) -> ExecutionContext:
        return ExecutionContext.new(
            max_effect=max_effect,
            budget=budget,
        )

    async def test_literal_traverse_preserves_paths_evidence_and_bindings(
        self,
    ) -> None:
        result = await self.executor.execute(
            Traverse(
                source=self.graph_source.descriptor.ref,
                starts=("Acme",),
                max_hops=2,
                op_id="traverse-acme",
            ),
            context=self.context(),
        )

        self.assertEqual(result.result_kind, ResultKind.GRAPH_PATHS)
        self.assertIsInstance(result.value, GraphPathSet)
        self.assertEqual(
            tuple(
                tuple(node.node_id for node in path.nodes)
                for path in result.value.paths
            ),
            (
                ("Acme", "Shanghai"),
                ("Acme", "Shanghai", "China"),
            ),
        )
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(
            tuple(row.values["end_id"] for row in result.bindings.rows),
            ("Shanghai", "China"),
        )
        self.assertTrue(
            all(row.evidence_ids for row in result.bindings.rows)
        )

    async def test_table_field_can_bind_graph_start(self) -> None:
        query = Query(
            source=self.table_source.descriptor.ref,
            statement=(
                "SELECT entity FROM projects WHERE project_id = 'p1'"
            ),
            op_id="select-entity",
        )
        traverse = Traverse(
            source=self.graph_source.descriptor.ref,
            start_binding=ValueBinding.single(
                OutputRef(query.op_id),
                "entity",
            ),
            direction=GraphDirection.OUT,
            max_hops=1,
            op_id="traverse-bound-entity",
        )
        plan = DataPlan(
            operations=(query, traverse),
            output=OutputRef(traverse.op_id),
            plan_id="table-to-graph",
        )

        report = validate_plan(
            plan,
            sources=self.catalog.descriptors(),
        )
        self.assertTrue(report.valid, report.issues)
        result = await self.executor.execute(
            plan,
            context=self.context(),
        )

        self.assertEqual(len(result.value.paths), 1)
        self.assertEqual(
            result.value.paths[0].nodes[-1].node_id,
            "Shanghai",
        )

    async def test_collect_binding_is_bounded(self) -> None:
        query = Query(
            source=self.table_source.descriptor.ref,
            statement="SELECT entity FROM projects ORDER BY entity",
            op_id="select-entities",
        )
        traverse = Traverse(
            source=self.graph_source.descriptor.ref,
            start_binding=ValueBinding.collect(
                OutputRef(query.op_id),
                "entity",
                max_items=1,
            ),
            max_hops=1,
            op_id="traverse-too-many",
        )
        plan = DataPlan(
            operations=(query, traverse),
            output=OutputRef(traverse.op_id),
            plan_id="bounded-collect",
        )

        with self.assertRaises(ExecutionError):
            await self.executor.execute(
                plan,
                context=self.context(),
            )

    async def test_resolve_skill_returns_governed_spec_and_instructions(
        self,
    ) -> None:
        result = await self.executor.execute(
            ResolveSkill(
                source=self.skill_source.descriptor.ref,
                query="normalize project risk",
                limit=2,
                op_id="resolve-risk",
            ),
            context=self.context(),
        )

        self.assertEqual(result.result_kind, ResultKind.SKILL_SPECS)
        self.assertEqual(result.value.matches[0].spec.ref, self.risk_spec.ref)
        self.assertIn("normalized", result.evidence[0].content)
        self.assertEqual(
            result.bindings.rows[0].values["effect"],
            "PURE",
        )

    def test_skill_spec_enforces_portable_name(self) -> None:
        with self.assertRaises(KernelValidationError):
            SkillSpec(
                name="Not_Portable",
                version="1.0.0",
                description="Invalid Agent Skills name.",
                instructions="This value should be rejected.",
            )

    async def test_table_field_can_bind_skill_argument(self) -> None:
        query = Query(
            source=self.table_source.descriptor.ref,
            statement=(
                "SELECT risk FROM projects WHERE project_id = 'p1'"
            ),
            op_id="select-risk",
        )
        invoke = InvokeSkill(
            source=self.skill_source.descriptor.ref,
            skill=self.risk_spec.ref,
            governed_effect=self.risk_spec.effect_level,
            argument_bindings=(
                ArgumentBinding(
                    argument="risk",
                    value=ValueBinding.single(
                        OutputRef(query.op_id),
                        "risk",
                    ),
                ),
            ),
            op_id="invoke-risk-label",
        )
        plan = DataPlan(
            operations=(query, invoke),
            output=OutputRef(invoke.op_id),
            plan_id="table-to-skill",
        )

        result = await self.executor.execute(
            plan,
            context=self.context(),
        )

        self.assertEqual(result.result_kind, ResultKind.SKILL_RESULT)
        self.assertEqual(result.value.output["label"], "CRITICAL")
        self.assertEqual(
            result.bindings.rows[0].values["output.label"],
            "CRITICAL",
        )

    async def test_table_to_graph_to_skill_chain(self) -> None:
        query = Query(
            source=self.table_source.descriptor.ref,
            statement=(
                "SELECT entity FROM projects WHERE project_id = 'p1'"
            ),
            op_id="chain-query",
        )
        traverse = Traverse(
            source=self.graph_source.descriptor.ref,
            start_binding=ValueBinding.single(
                OutputRef(query.op_id),
                "entity",
            ),
            max_hops=1,
            op_id="chain-traverse",
        )
        invoke = InvokeSkill(
            source=self.skill_source.descriptor.ref,
            skill=self.risk_spec.ref,
            governed_effect=EffectLevel.PURE,
            argument_bindings=(
                ArgumentBinding(
                    argument="risk",
                    value=ValueBinding.single(
                        OutputRef(traverse.op_id),
                        "end_id",
                    ),
                ),
            ),
            op_id="chain-invoke",
        )
        plan = DataPlan(
            operations=(query, traverse, invoke),
            output=OutputRef(invoke.op_id),
            plan_id="table-graph-skill-chain",
        )

        result = await self.executor.execute(
            plan,
            context=self.context(),
        )

        self.assertEqual(result.value.output["label"], "SHANGHAI")
        self.assertEqual(result.usage.actions, 3)

    async def test_skill_schema_is_checked_before_handler(self) -> None:
        operation = InvokeSkill(
            source=self.skill_source.descriptor.ref,
            skill=self.risk_spec.ref,
            governed_effect=self.risk_spec.effect_level,
            arguments={"unknown": "critical"},
            op_id="invoke-invalid-input",
        )

        with self.assertRaises(SourceExecutionError):
            await self.executor.execute(
                operation,
                context=self.context(),
            )

    async def test_instruction_only_skill_cannot_be_invoked(self) -> None:
        operation = InvokeSkill(
            source=self.skill_source.descriptor.ref,
            skill=self.instruction_spec.ref,
            governed_effect=EffectLevel.PURE,
            arguments={},
            op_id="invoke-instructions",
        )

        with self.assertRaises(SourceExecutionError):
            await self.executor.execute(
                operation,
                context=self.context(),
            )

    async def test_forged_lower_effect_is_rejected_before_handler(
        self,
    ) -> None:
        operation = InvokeSkill(
            source=self.skill_source.descriptor.ref,
            skill=self.write_spec.ref,
            governed_effect=EffectLevel.PURE,
            arguments={"project_id": "p1"},
            op_id="invoke-forged-effect",
        )

        with self.assertRaises(SourceExecutionError):
            await self.executor.execute(
                operation,
                context=self.context(),
            )
        self.assertEqual(self.write_calls, 0)

    async def test_correct_write_effect_requires_context_authority(
        self,
    ) -> None:
        operation = InvokeSkill(
            source=self.skill_source.descriptor.ref,
            skill=self.write_spec.ref,
            governed_effect=EffectLevel.INTERNAL_WRITE,
            arguments={"project_id": "p1"},
            op_id="invoke-write",
        )
        plan = DataPlan(
            operations=(operation,),
            output=OutputRef(operation.op_id),
            max_effect=EffectLevel.INTERNAL_WRITE,
            plan_id="governed-write",
        )

        with self.assertRaises(EffectPolicyError):
            await self.executor.execute(
                plan,
                context=self.context(max_effect=EffectLevel.READ),
            )
        self.assertEqual(self.write_calls, 0)

        result = await self.executor.execute(
            plan,
            context=self.context(
                max_effect=EffectLevel.INTERNAL_WRITE
            ),
        )
        self.assertEqual(result.value.output["status"], "recorded")
        self.assertEqual(self.write_calls, 1)

    def test_new_operations_have_lossless_plan_codec(self) -> None:
        query = Query(
            source=self.table_source.descriptor.ref,
            statement="SELECT entity FROM projects",
            op_id="codec-query",
        )
        traverse = Traverse(
            source=self.graph_source.descriptor.ref,
            start_binding=ValueBinding.collect(
                OutputRef(query.op_id),
                "entity",
                max_items=10,
            ),
            direction=GraphDirection.BOTH,
            relations=("works_at",),
            max_hops=2,
            op_id="codec-traverse",
        )
        invoke = InvokeSkill(
            source=self.skill_source.descriptor.ref,
            skill=self.risk_spec.ref,
            governed_effect=EffectLevel.PURE,
            arguments={"risk": "low"},
            op_id="codec-invoke",
        )
        resolve = ResolveSkill(
            source=self.skill_source.descriptor.ref,
            query="project risk",
            op_id="codec-resolve",
        )
        plans = (
            DataPlan(
                operations=(query, traverse),
                output=OutputRef(traverse.op_id),
                plan_id="codec-traverse-plan",
            ),
            DataPlan(
                operations=(resolve,),
                output=OutputRef(resolve.op_id),
                plan_id="codec-resolve-plan",
            ),
            DataPlan(
                operations=(invoke,),
                output=OutputRef(invoke.op_id),
                plan_id="codec-invoke-plan",
            ),
        )

        for plan in plans:
            self.assertEqual(plan_from_json(plan_to_json(plan)), plan)

    async def test_bound_graph_plan_replays_without_live_sources(
        self,
    ) -> None:
        trace_store = InMemoryTraceStore()
        executor = Executor(
            self.catalog,
            trace_store=trace_store,
            artifact_store=trace_store,
        )
        query = Query(
            source=self.table_source.descriptor.ref,
            statement=(
                "SELECT entity FROM projects WHERE project_id = 'p1'"
            ),
            op_id="replay-query",
        )
        traverse = Traverse(
            source=self.graph_source.descriptor.ref,
            start_binding=ValueBinding.single(
                OutputRef(query.op_id),
                "entity",
            ),
            max_hops=1,
            op_id="replay-traverse",
        )
        project = Project(
            inputs=(OutputRef(traverse.op_id),),
            fields=("end_id",),
            op_id="replay-project",
        )
        plan = DataPlan(
            operations=(query, traverse, project),
            output=OutputRef(project.op_id),
            plan_id="replay-bound-graph",
        )
        context = self.context()

        original = await executor.execute(plan, context=context)
        self.database_path.unlink()
        replayed = await executor.replay(context.trace_id)

        self.assertEqual(replayed, original)


if __name__ == "__main__":
    unittest.main()
