"""Small, deterministic enterprise world spanning all five data surfaces."""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from benchmarks.environment import BenchmarkEnvironment
from benchmarks.faults import FaultInjectingSource, FaultRule
from datamind.adapters import (
    DocumentRecord,
    InMemoryArtifactStore,
    InMemoryDocumentSource,
    InMemoryGraphSource,
    InMemoryMemorySource,
    InMemorySkillSource,
    SQLiteReadSource,
    SkillRegistration,
)
from datamind.kernel import (
    EffectLevel,
    GraphEdge,
    GraphNode,
    MemoryKind,
    MemoryRecord,
    ScopeKind,
    ScopeRef,
    SkillKind,
    SkillSpec,
)
from datamind.lifecycle import LifecycleManager, SourceCatalog

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
PRINCIPAL_SCOPE = ScopeRef(ScopeKind.PRINCIPAL, "alice")


class _AdvancingClock:
    def __init__(self) -> None:
        self._step = 0

    def __call__(self) -> datetime:
        self._step += 1
        return BASE_TIME + timedelta(minutes=self._step)


async def _normalize_risk(arguments, context):
    del context
    return {"label": arguments["risk"].upper()}


async def _record_approval(arguments, context):
    del context
    return {"status": "recorded", "project_id": arguments["project_id"]}


def _database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE expenses "
            "(employee_id INTEGER, department TEXT, category TEXT, amount REAL)"
        )
        connection.executemany(
            "INSERT INTO expenses VALUES (?, ?, ?, ?)",
            (
                (7, "sales", "meal", 80.0),
                (7, "sales", "hotel", 600.0),
                (8, "security", "meal", 50.0),
            ),
        )
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


def _build(*, faulty: bool) -> BenchmarkEnvironment:
    temporary = tempfile.TemporaryDirectory()
    database_path = Path(temporary.name) / "enterprise.sqlite3"
    _database(database_path)

    documents = InMemoryDocumentSource(
        source_id="policy-kb",
        version="policy-v1",
        documents=(
            DocumentRecord(
                "travel-policy",
                (
                    "Travel reimbursement policy: sales meals are "
                    "reimbursable up to 100 dollars."
                ),
                metadata={"department": "sales"},
            ),
            DocumentRecord(
                "security-policy",
                "Security policy requires hardware keys.",
                metadata={"department": "security"},
            ),
        ),
    )
    table = SQLiteReadSource(
        source_id="warehouse",
        database_path=database_path,
    )
    graph = InMemoryGraphSource(
        source_id="enterprise-graph",
        nodes=(
            GraphNode("Acme", labels=("company",)),
            GraphNode("Ann", labels=("person",)),
            GraphNode("Shanghai", labels=("city",)),
            GraphNode("China", labels=("country",)),
        ),
        edges=(
            GraphEdge("e1", "Ann", "Acme", "works_at"),
            GraphEdge("e2", "Acme", "Shanghai", "located_in"),
            GraphEdge("e3", "Shanghai", "China", "in_country"),
        ),
    )
    memory = InMemoryMemorySource(
        source_id="agent-memory",
        records=(
            MemoryRecord(
                memory_id="communication-pref",
                kind=MemoryKind.PREFERENCE,
                scope=PRINCIPAL_SCOPE,
                content="Communication preference is email.",
                recorded_from=BASE_TIME,
            ),
        ),
        version="memory-v1",
        observed_at=BASE_TIME,
        clock=_AdvancingClock(),
    )
    risk = SkillSpec(
        name="risk-label",
        version="1.0.0",
        description="Normalize an enterprise project risk label.",
        instructions="Return the uppercase risk label.",
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
    write = SkillSpec(
        name="record-approval",
        version="1.0.0",
        description="Record a governed internal project approval.",
        instructions="Record only an explicitly approved project.",
        kind=SkillKind.EXECUTABLE,
        input_schema={
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "project_id": {"type": "string"},
            },
            "required": ["status", "project_id"],
            "additionalProperties": False,
        },
        effect_level=EffectLevel.INTERNAL_WRITE,
        requires_approval=True,
    )
    skills = InMemorySkillSource(
        source_id="enterprise-skills",
        registrations=(
            SkillRegistration(risk, _normalize_risk),
            SkillRegistration(write, _record_approval),
        ),
    )

    catalog = SourceCatalog()
    for source in (documents, table, graph, memory, skills):
        catalog.register(source)

    artifacts = InMemoryArtifactStore()
    lifecycle = LifecycleManager(catalog, artifacts)

    failing = None
    if faulty:
        raw_failing = InMemoryDocumentSource(
            source_id="policy-failing",
            documents=(
                DocumentRecord(
                    "backup-policy",
                    "Acme backup policy requires daily snapshots.",
                ),
            ),
        )
        failing = FaultInjectingSource(
            raw_failing,
            rules=(
                FaultRule(
                    "search",
                    calls=(1,),
                    message="injected unavailable document provider",
                ),
            ),
        )
        catalog.register(failing)

    return BenchmarkEnvironment(
        catalog=catalog,
        lifecycle=lifecycle,
        state={
            "artifacts": artifacts,
            "documents": documents,
            "table": table,
            "graph": graph,
            "memory": memory,
            "skills": skills,
            "risk_skill": risk,
            "write_skill": write,
            "principal_scope": PRINCIPAL_SCOPE,
            "base_time": BASE_TIME,
            "failing": failing,
        },
        cleanup=temporary.cleanup,
    )


def build_enterprise_environment() -> BenchmarkEnvironment:
    return _build(faulty=False)


def build_faulty_environment() -> BenchmarkEnvironment:
    return _build(faulty=True)
