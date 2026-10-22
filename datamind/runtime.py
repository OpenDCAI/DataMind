"""Assemble capability services without constructing an agent or model loop."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from datamind.capabilities.db import build_db_service, build_db_tools
from datamind.capabilities.embedding import build_embedding
from datamind.capabilities.graph import build_graph_service, build_graph_tools
from datamind.capabilities.hooks import AuditLogHook, DestructiveSqlHook, PathAllowlistHook
from datamind.capabilities.ingest import (
    IngestLedger,
    build_ingest_service,
    build_ingest_tools,
    with_receipts,
)
from datamind.capabilities.kb import build_kb_service, build_kb_tools
from datamind.capabilities.memory import build_memory_service, build_memory_tools
from datamind.capabilities.skills import (
    SkillsService,
    build_skills_store_tools,
    build_skills_tools,
)
from datamind.config import Settings
from datamind.core.contracts import ToolAccess
from datamind.core.hooks import HookChain
from datamind.core.protocols import EmbeddingProvider
from datamind.core.tools import ToolRegistry


from datamind.data_plane import DataPlane, DataServices, MODEL_TOOLS
from datamind.direct_tools import workspace_tools, graph_source_tools

def _build_hook_chain(settings: Settings) -> HookChain | None:
    cfg = settings.hooks
    if not cfg.enabled:
        return None

    chain_hooks: list[Any] = []
    if cfg.path_allowlist:
        roots: list[Path] = [Path(settings.data.data_dir), Path.cwd()]
        roots.extend(Path(p).expanduser() for p in cfg.path_allowlist_extra)
        chain_hooks.append(PathAllowlistHook(roots=roots))
    if cfg.destructive_sql:
        chain_hooks.append(DestructiveSqlHook())
    if cfg.audit_log:
        chain_hooks.append(AuditLogHook(audit_path=settings.data.storage_dir / "audit.jsonl"))
    return HookChain(chain_hooks) if chain_hooks else None


async def assemble_data_plane(settings: Settings, *, enable: set[str] | None = None, text_client=None, workspace_enable=None):
    """An optional explicit text utility client supports custom capability pipelines;
    the default never creates conversation clients or any agent loop.
    """
    defaults = {"kb", "db", "graph", "skills", "memory"}
    active = defaults if enable is None else set(enable)
    unknown = active - defaults
    if unknown:
        raise ValueError(f"Unknown DataMind surfaces: {sorted(unknown)}")
    settings.ensure_dirs()

    client = fallback_client = text_client
    embedding: EmbeddingProvider | None = None
    if "kb" in active:
        embedding = build_embedding(settings.embedding, fallback_llm=settings.llm)

    kb = (
        build_kb_service(settings, llm_client=fallback_client, embedding=embedding)
        if "kb" in active else None
    )
    from datamind.experiment_faults import install
    install(kb)
    db = build_db_service(settings, llm_client=client) if "db" in active else None
    graph = build_graph_service(settings) if "graph" in active else None
    skills = (SkillsService(skills_dir=settings.data.skills_dir,
        profile_skills_dir=settings.data.profile_skills_dir, embedding=None, vector_store=None)
        if 'skills' in active else None)
    memory = (
        build_memory_service(
            settings, llm_client=fallback_client, embedding=embedding, auto_embedding=False,
        )
        if "memory" in active else None
    )
    ingest = build_ingest_service(
            settings=settings,
            kb=kb,
            db=db,
            graph=graph,
            llm_client=fallback_client,
    )
    services = DataServices(
        client=client,
        fallback_client=fallback_client,
        embedding=embedding,
        kb=kb,
        db=db,
        graph=graph,
        skills=skills,
        memory=memory,
        ingest=ingest,
    )

    catalogue = ToolRegistry()
    ingest_tools = build_ingest_tools(ingest) if ingest is not None else []
    catalogue.extend([t for t in ingest_tools if t.surface is None])
    if "kb" in active:
        assert kb is not None
        catalogue.extend(build_kb_tools(kb))
        catalogue.extend([t for t in ingest_tools if t.surface and t.surface.value == "kb"])
    if "db" in active:
        assert db is not None
        catalogue.extend(build_db_tools(db))
        catalogue.extend([t for t in ingest_tools if t.surface and t.surface.value == "db"])
    if "graph" in active:
        assert graph is not None
        catalogue.extend(build_graph_tools(graph))
        catalogue.extend([t for t in ingest_tools if t.surface and t.surface.value == "graph"])
    if "skills" in active:
        assert skills is not None
        catalogue.extend(build_skills_tools(skills))
        catalogue.extend(build_skills_store_tools(skills))
    if "memory" in active:
        assert memory is not None
        catalogue.extend(build_memory_tools(memory))

    ledger = IngestLedger(storage_dir=settings.data.storage_dir, profile=settings.data.profile)
    extra = []
    workspace_active = active if workspace_enable is None else set(workspace_enable)
    extra.extend([spec for spec in workspace_tools(settings, workspace_active)
                  if spec.access != ToolAccess.WRITE or spec.metadata.get('workspace_management') or workspace_active & {'kb','db','graph'}])
    if graph is not None:
        extra.extend(graph_source_tools(ingest))
    catalogue.extend(extra)
    reads = catalogue.select(access={ToolAccess.READ, ToolAccess.UTILITY})
    raw_writes = catalogue.select(access={ToolAccess.WRITE})
    business_writes = ToolRegistry()
    business_writes.extend([raw_writes.get(n) for n in raw_writes.names() if raw_writes.get(n).surface is not None])
    writes = with_receipts(business_writes, ledger)
    if active or any(raw_writes.get(n).metadata.get("workspace_management") for n in raw_writes.names()):
        writes.extend([raw_writes.get(n) for n in raw_writes.names() if raw_writes.get(n).surface is None])
    direct = ToolRegistry()
    direct.extend([reads.get(n) for n in reads.names() if n not in MODEL_TOOLS])
    direct.extend([writes.get(n) for n in writes.names() if n not in MODEL_TOOLS])
    return DataPlane(services=services, tools=direct, hooks=_build_hook_chain(settings),
                     ledger=ledger, profile=settings.data.profile)
