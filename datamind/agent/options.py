"""Assemble DataMind's cooperating StoreAgent and RetrieveAgent.

There is intentionally no planner or operator layer here.  Both agents are
ordinary tool-use loops over the same capability services.  Their authority is
separated by ToolRegistry before the tool catalogue reaches the model:

* RetrieveAgent receives read + utility tools only.
* StoreAgent receives write tools only, wrapped in idempotent receipts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from datamind.capabilities.db import DBService, build_db_service, build_db_tools
from datamind.capabilities.graph import GraphService, build_graph_service, build_graph_tools
from datamind.capabilities.hooks import AuditLogHook, DestructiveSqlHook, PathAllowlistHook
from datamind.capabilities.ingest import (
    IngestLedger,
    IngestService,
    build_ingest_service,
    build_ingest_tools,
    with_receipts,
)
from datamind.capabilities.kb import KBService, build_kb_service, build_kb_tools
from datamind.capabilities.memory import MemoryService, build_memory_service, build_memory_tools
from datamind.capabilities.skills import (
    SkillsService,
    build_skills_service,
    build_skills_store_tools,
    build_skills_tools,
)
from datamind.config import Settings
from datamind.core.contracts import ToolAccess
from datamind.core.hooks import HookChain
from datamind.core.logging import get_logger
from datamind.core.tools import ToolRegistry

from .base import AgentLoopConfig, AgentLoopProtocol
from .loop_native import NativeAgentLoop
from .prompts import build_retrieve_system_prompt, build_store_system_prompt

_log = get_logger("agent.assemble")


@dataclass
class AgentServices:
    """Long-lived services shared by both agents."""

    client: AsyncAnthropic
    kb: KBService
    db: DBService
    graph: GraphService
    skills: SkillsService
    memory: MemoryService
    ingest: IngestService


@dataclass
class RetrieveAgent:
    """Read-only inference agent over all five data surfaces."""

    services: AgentServices
    tools: ToolRegistry
    loop: AgentLoopProtocol
    hooks: HookChain | None = None
    ledger: IngestLedger | None = None

    @property
    def client(self) -> AsyncAnthropic:
        return self.services.client

    @property
    def kb(self) -> KBService:
        return self.services.kb

    @property
    def db(self) -> DBService:
        return self.services.db

    @property
    def graph(self) -> GraphService:
        return self.services.graph

    @property
    def skills(self) -> SkillsService:
        return self.services.skills

    @property
    def memory(self) -> MemoryService:
        return self.services.memory

    @property
    def revision(self) -> int:
        return self.ledger.revision if self.ledger else 0

    async def warmup(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        info["skills"] = await self.skills.load()
        info["graph"] = await self.graph.load_from_profile()
        info["kb_chunks"] = await self.kb.count()
        info["revision"] = self.revision
        info["hooks"] = self.hooks.names() if self.hooks else []
        _log.info("retrieve_agent_warmup", extra=info)
        return info

    async def query(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        return await self.loop.run_turn(user_message=message, history=history)


@dataclass
class StoreAgent:
    """Write-only agent that returns one receipt for every tool call."""

    services: AgentServices
    tools: ToolRegistry
    loop: AgentLoopProtocol
    ledger: IngestLedger
    hooks: HookChain | None = None

    @property
    def client(self) -> AsyncAnthropic:
        return self.services.client

    @property
    def revision(self) -> int:
        return self.ledger.revision

    async def store(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        return await self.loop.run_turn(user_message=message, history=history)


@dataclass
class DataMind:
    """Public two-agent facade."""

    store_agent: StoreAgent
    retrieve_agent: RetrieveAgent
    services: AgentServices

    @property
    def store(self) -> StoreAgent:
        return self.store_agent

    @property
    def retrieve(self) -> RetrieveAgent:
        return self.retrieve_agent

    async def warmup(self) -> dict[str, Any]:
        return await self.retrieve_agent.warmup()

    async def ingest(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        return await self.store_agent.store(message, history=history)

    async def query(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        return await self.retrieve_agent.query(message, history=history)


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


def _build_loop(
    *,
    settings: Settings,
    client: AsyncAnthropic,
    tools: ToolRegistry,
    system_prompt: str,
    hooks: HookChain | None,
) -> AgentLoopProtocol:
    config = AgentLoopConfig(
        model=settings.llm.model,
        max_tokens=settings.llm.max_tokens,
        temperature=settings.llm.temperature,
        system_prompt=system_prompt,
        max_tool_turns=settings.agent.max_turns,
    )
    if settings.agent.backend == "sdk":
        from .loop_sdk import SdkAgentLoop  # noqa: PLC0415

        return SdkAgentLoop(
            tools=tools,
            config=config,
            ccr_base_url=settings.agent.ccr_base_url,
            ccr_api_key=settings.agent.ccr_api_key.get_secret_value(),
            hooks=hooks,
        )
    return NativeAgentLoop(client=client, tools=tools, config=config, hooks=hooks)


async def build_datamind(
    settings: Settings,
    *,
    enable: set[str] | None = None,
) -> DataMind:
    """Build both agents over one shared set of capability services."""
    active = enable or {"kb", "db", "graph", "skills", "memory"}
    settings.ensure_dirs()

    client = AsyncAnthropic(
        base_url=str(settings.llm.api_base),
        api_key=settings.llm.api_key.get_secret_value(),
        timeout=settings.llm.timeout_s,
    )
    kb = build_kb_service(settings, llm_client=client)
    db = build_db_service(settings, llm_client=client)
    graph = build_graph_service(settings)
    skills = build_skills_service(settings)
    memory = build_memory_service(settings, llm_client=client)
    ingest = build_ingest_service(
        settings=settings,
        kb=kb,
        db=db,
        graph=graph,
        llm_client=client,
    )
    services = AgentServices(
        client=client,
        kb=kb,
        db=db,
        graph=graph,
        skills=skills,
        memory=memory,
        ingest=ingest,
    )

    catalogue = ToolRegistry()
    ingest_tools = build_ingest_tools(ingest)
    if "kb" in active:
        catalogue.extend(build_kb_tools(kb))
        catalogue.extend([t for t in ingest_tools if t.surface and t.surface.value == "kb"])
    if "db" in active:
        catalogue.extend(build_db_tools(db))
        catalogue.extend([t for t in ingest_tools if t.surface and t.surface.value == "db"])
    if "graph" in active:
        catalogue.extend(build_graph_tools(graph))
        catalogue.extend([t for t in ingest_tools if t.surface and t.surface.value == "graph"])
    if "skills" in active:
        catalogue.extend(build_skills_tools(skills))
        catalogue.extend(build_skills_store_tools(skills))
    if "memory" in active:
        catalogue.extend(build_memory_tools(memory))

    retrieve_tools = catalogue.select(access={ToolAccess.READ, ToolAccess.UTILITY})
    raw_store_tools = catalogue.select(access={ToolAccess.WRITE})
    retrieve_tools.assert_access({ToolAccess.READ, ToolAccess.UTILITY})
    raw_store_tools.assert_access({ToolAccess.WRITE})

    ledger = IngestLedger(storage_dir=settings.data.storage_dir, profile=settings.data.profile)
    store_tools = with_receipts(raw_store_tools, ledger)
    hooks = _build_hook_chain(settings)

    retrieve_prompt = build_retrieve_system_prompt(
        [retrieve_tools.get(name) for name in retrieve_tools.names()]
    )
    store_prompt = build_store_system_prompt(
        [store_tools.get(name) for name in store_tools.names()]
    )
    retrieve_loop = _build_loop(
        settings=settings,
        client=client,
        tools=retrieve_tools,
        system_prompt=retrieve_prompt,
        hooks=hooks,
    )
    store_loop = _build_loop(
        settings=settings,
        client=client,
        tools=store_tools,
        system_prompt=store_prompt,
        hooks=hooks,
    )

    retrieve_agent = RetrieveAgent(
        services=services,
        tools=retrieve_tools,
        loop=retrieve_loop,
        hooks=hooks,
        ledger=ledger,
    )
    store_agent = StoreAgent(
        services=services,
        tools=store_tools,
        loop=store_loop,
        ledger=ledger,
        hooks=hooks,
    )
    _log.info(
        "datamind_built",
        extra={
            "backend": settings.agent.backend,
            "retrieve_tools": len(retrieve_tools),
            "store_tools": len(store_tools),
            "profile": settings.data.profile,
        },
    )
    return DataMind(
        store_agent=store_agent,
        retrieve_agent=retrieve_agent,
        services=services,
    )


async def build_agent(
    settings: Settings,
    *,
    enable: set[str] | None = None,
    **_: Any,
) -> RetrieveAgent:
    """Backward-compatible builder; now intentionally returns read-only agent."""
    system = await build_datamind(settings, enable=enable)
    return system.retrieve_agent


async def build_store_agent(
    settings: Settings,
    *,
    enable: set[str] | None = None,
) -> StoreAgent:
    system = await build_datamind(settings, enable=enable)
    return system.store_agent


# Source compatibility for imports made before the two-agent split.
DataMindAgent = RetrieveAgent


__all__ = [
    "AgentServices",
    "RetrieveAgent",
    "StoreAgent",
    "DataMind",
    "DataMindAgent",
    "build_datamind",
    "build_agent",
    "build_store_agent",
]
