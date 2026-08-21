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

from datamind.capabilities.db import DBService, build_db_service, build_db_tools
from datamind.capabilities.embedding import build_embedding
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
from datamind.core.logging import bind_context, current_context
from datamind.core.context import RequestContext
from datamind.core.model_clients import build_model_client
from datamind.core.protocols import EmbeddingProvider, TextModelClient, ToolCallingModelClient
from datamind.core.tools import ToolRegistry

from .base import AgentLoopConfig, AgentLoopProtocol
from .loop_native import NativeAgentLoop
from .loop_openai import OpenAICompatibleAgentLoop
from .prompts import build_retrieve_system_prompt, build_store_system_prompt

_log = get_logger("agent.assemble")


@dataclass
class AgentServices:
    """Long-lived services shared by both agents."""

    client: ToolCallingModelClient
    fallback_client: TextModelClient
    embedding: EmbeddingProvider | None = None
    kb: KBService | None = None
    db: DBService | None = None
    graph: GraphService | None = None
    skills: SkillsService | None = None
    memory: MemoryService | None = None
    ingest: IngestService | None = None


@dataclass
class RetrieveAgent:
    """Read-only inference agent over all five data surfaces."""

    services: AgentServices
    tools: ToolRegistry
    loop: AgentLoopProtocol
    hooks: HookChain | None = None
    ledger: IngestLedger | None = None

    @property
    def client(self) -> ToolCallingModelClient:
        return self.services.client

    @property
    def kb(self) -> KBService:
        if self.services.kb is None:
            raise RuntimeError("KB surface is disabled")
        return self.services.kb

    @property
    def db(self) -> DBService:
        if self.services.db is None:
            raise RuntimeError("DB surface is disabled")
        return self.services.db

    @property
    def graph(self) -> GraphService:
        if self.services.graph is None:
            raise RuntimeError("Graph surface is disabled")
        return self.services.graph

    @property
    def skills(self) -> SkillsService:
        if self.services.skills is None:
            raise RuntimeError("Skills surface is disabled")
        return self.services.skills

    @property
    def memory(self) -> MemoryService:
        if self.services.memory is None:
            raise RuntimeError("Memory surface is disabled")
        return self.services.memory

    @property
    def revision(self) -> int:
        return self.ledger.revision if self.ledger else 0

    async def warmup(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        info["skills"] = (
            await self.services.skills.load() if self.services.skills else
            {"manifests": 0, "indexed": 0, "code_tools": 0}
        )
        info["graph"] = (
            await self.services.graph.load_from_profile() if self.services.graph else
            {"triples_loaded": 0}
        )
        info["kb_chunks"] = await self.services.kb.count() if self.services.kb else 0
        info["revision"] = self.revision
        info["hooks"] = self.hooks.names() if self.hooks else []
        _log.info("retrieve_agent_warmup", extra=info)
        return info

    async def query(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
        final_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.loop.run_turn(
            user_message=message, history=history, final_contract=final_contract,
        )


@dataclass
class StoreAgent:
    """Write-only agent that returns one receipt for every tool call."""

    services: AgentServices
    tools: ToolRegistry
    loop: AgentLoopProtocol
    ledger: IngestLedger
    hooks: HookChain | None = None

    @property
    def client(self) -> ToolCallingModelClient:
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
    profile: str = "default"
    _closed: bool = False

    @property
    def store(self) -> StoreAgent:
        return self.store_agent

    @property
    def retrieve(self) -> RetrieveAgent:
        return self.retrieve_agent

    async def __aenter__(self) -> "DataMind":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def warmup(self) -> dict[str, Any]:
        return await self.retrieve_agent.warmup()

    async def ingest(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        if current_context() is not None:
            return await self.store_agent.store(message, history=history)
        with bind_context(RequestContext.new(profile=self.profile)):
            return await self.store_agent.store(message, history=history)

    async def query(
        self,
        message: str,
        *,
        history: list[dict] | None = None,
        final_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if current_context() is not None:
            return await self.retrieve_agent.query(
                message, history=history, final_contract=final_contract,
            )
        with bind_context(RequestContext.new(profile=self.profile)):
            return await self.retrieve_agent.query(
                message, history=history, final_contract=final_contract,
            )

    async def aclose(self) -> None:
        """Idempotently close clients, engines, stores, and providers."""
        if self._closed:
            return
        self._closed = True
        resources = [
            self.services.db,
            self.services.graph,
            self.services.kb,
            self.services.embedding,
            self.services.fallback_client,
            self.services.client,
        ]
        seen: set[int] = set()
        for resource in resources:
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, "aclose", None)
            if callable(close):
                await close()


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
    client: ToolCallingModelClient,
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
        max_tool_calls=settings.agent.max_tool_calls,
        max_input_tokens=settings.agent.max_input_tokens,
        max_tool_result_chars=settings.agent.max_tool_result_chars,
        max_tool_result_rows=settings.agent.max_tool_result_rows,
        wall_clock_timeout_s=settings.agent.wall_clock_timeout_s,
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
    loop_type = (
        OpenAICompatibleAgentLoop
        if settings.llm.protocol == "openai_chat_completions"
        else NativeAgentLoop
    )
    return loop_type(client=client, tools=tools, config=config, hooks=hooks)


async def build_datamind(
    settings: Settings,
    *,
    enable: set[str] | None = None,
) -> DataMind:
    """Build both agents over one shared set of capability services."""
    defaults = {"kb", "db", "graph", "skills", "memory"}
    active = defaults if enable is None else set(enable)
    unknown = active - defaults
    if unknown:
        raise ValueError(f"Unknown DataMind surfaces: {sorted(unknown)}")
    settings.ensure_dirs()

    client = build_model_client(settings.llm)
    fallback_protocol = settings.llm.fallback_protocol or settings.llm.protocol
    fallback_client = (
        client if fallback_protocol == settings.llm.protocol
        else build_model_client(settings.llm, protocol=fallback_protocol)
    )

    embedding: EmbeddingProvider | None = None
    needs_embedding = bool(active & {"kb", "skills"}) or (
        "memory" in active and settings.memory.long_term_enabled
    )
    if needs_embedding:
        embedding = build_embedding(settings.embedding, fallback_llm=settings.llm)

    kb = (
        build_kb_service(settings, llm_client=fallback_client, embedding=embedding)
        if "kb" in active else None
    )
    db = build_db_service(settings, llm_client=client) if "db" in active else None
    graph = build_graph_service(settings) if "graph" in active else None
    skills = (
        build_skills_service(settings, embedding=embedding)
        if "skills" in active else None
    )
    memory = (
        build_memory_service(
            settings, llm_client=fallback_client, embedding=embedding,
        )
        if "memory" in active else None
    )
    ingest = (
        build_ingest_service(
            settings=settings,
            kb=kb,
            db=db,
            graph=graph,
            llm_client=fallback_client,
        )
        if active & {"kb", "db", "graph"} else None
    )
    services = AgentServices(
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
        profile=settings.data.profile,
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
