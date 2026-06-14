"""Agent assembly — wire all capabilities into a single DataMindAgent.

One function: `build_agent(settings)` returns a ready-to-use agent with
every tool registered. Cheap to call; builds each capability service once
and shares the Anthropic client across them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from datamind.capabilities.db import DBService, build_db_service, build_db_tools
from datamind.capabilities.graph import GraphService, build_graph_service, build_graph_tools
from datamind.capabilities.hooks import (
    AuditLogHook,
    DestructiveSqlHook,
    PathAllowlistHook,
)
from datamind.capabilities.ingest import (
    IngestService,
    build_ingest_service,
    build_ingest_tools,
)
from datamind.capabilities.kb import KBService, build_kb_service, build_kb_tools
from datamind.capabilities.memory import (
    MemoryService,
    build_memory_service,
    build_memory_tools,
)
from datamind.capabilities.skills import SkillsService, build_skills_service, build_skills_tools
from datamind.config import Settings
from datamind.core.hooks import HookChain
from datamind.core.logging import get_logger
from datamind.core.tools import ToolRegistry

from .base import AgentLoopConfig, AgentLoopProtocol
from .loop_native import NativeAgentLoop
from .prompts import build_system_prompt

_log = get_logger("agent.assemble")


@dataclass
class DataMindAgent:
    """Top-level handle exposing every piece a caller might want."""

    client: AsyncAnthropic
    tools: ToolRegistry
    loop: AgentLoopProtocol
    kb: KBService
    db: DBService
    graph: GraphService
    skills: SkillsService
    memory: MemoryService
    ingest: IngestService
    hooks: HookChain | None = None

    async def warmup(self) -> dict[str, Any]:
        """Load skills index, graph triplets, etc. Returns a stats dict."""
        info: dict[str, Any] = {}
        info["skills"] = await self.skills.load()
        info["graph"] = await self.graph.load_from_profile()
        info["kb_chunks"] = await self.kb.count()
        info["hooks"] = self.hooks.names() if self.hooks else []
        _log.info("agent_warmup", extra=info)
        return info


def _build_hook_chain(settings: Settings) -> HookChain | None:
    """Assemble the HookChain per HooksConfig. Returns None if disabled."""
    cfg = settings.hooks
    if not cfg.enabled:
        return None

    chain_hooks: list[Any] = []

    if cfg.path_allowlist:
        roots: list[Path] = [
            Path(settings.data.data_dir),
            Path.cwd(),
        ]
        for extra in cfg.path_allowlist_extra:
            roots.append(Path(extra).expanduser())
        chain_hooks.append(PathAllowlistHook(roots=roots))

    if cfg.destructive_sql:
        chain_hooks.append(DestructiveSqlHook())

    if cfg.audit_log:
        audit_path = settings.data.storage_dir / "audit.jsonl"
        chain_hooks.append(AuditLogHook(audit_path=audit_path))

    if not chain_hooks:
        return None
    return HookChain(chain_hooks)


async def build_agent(
    settings: Settings,
    *,
    enable: set[str] | None = None,
) -> DataMindAgent:
    """Assemble every capability + the agent loop.

    `enable` lets you restrict which tool groups are active — handy in
    tests where e.g. you don't want the graph warmup to hit the filesystem.
    Defaults to everything.
    """
    active = enable or {"kb", "db", "graph", "skills", "memory", "ingest"}

    client = AsyncAnthropic(
        base_url=str(settings.llm.api_base),
        api_key=settings.llm.api_key.get_secret_value(),
        timeout=settings.llm.timeout_s,
    )

    tools = ToolRegistry()

    # KB
    kb = build_kb_service(settings, llm_client=client)
    if "kb" in active:
        tools.extend(build_kb_tools(kb))

    # DB
    db = build_db_service(settings, llm_client=client)
    if "db" in active:
        tools.extend(build_db_tools(db))

    # Graph
    graph = build_graph_service(settings)
    if "graph" in active:
        tools.extend(build_graph_tools(graph))

    # Skills
    skills = build_skills_service(settings)
    if "skills" in active:
        tools.extend(build_skills_tools(skills))

    # Memory
    # MemoryService binds `default_profile = settings.data.profile`, so
    # scope='profile' calls without an explicit profile target the active
    # tenant. session_id is per-request — tools accept it explicitly until
    # full RequestContext propagation lands in the hooks layer.
    memory = build_memory_service(settings, llm_client=client)
    if "memory" in active:
        tools.extend(build_memory_tools(memory))

    # Ingest — agent-driven additions to KB / DB / Graph. Built last so it
    # can wire into already-constructed services. Tools are registered
    # under the "ingest" group, easy to disable wholesale via permissions.
    ingest = build_ingest_service(
        settings=settings,
        kb=kb,
        db=db,
        graph=graph,
        llm_client=client,
    )
    if "ingest" in active:
        tools.extend(build_ingest_tools(ingest))

    system = build_system_prompt(
        [tools.get(n) for n in tools.names()]
    )

    # Pick the agent-loop backend based on settings. Both satisfy
    # AgentLoopProtocol — everything downstream is backend-agnostic.
    loop_config = AgentLoopConfig(
        model=settings.llm.model,
        max_tokens=settings.llm.max_tokens,
        temperature=settings.llm.temperature,
        system_prompt=system,
        max_tool_turns=settings.agent.max_turns,
    )

    loop: AgentLoopProtocol
    hooks_chain = _build_hook_chain(settings)
    if settings.agent.backend == "sdk":
        # Import locally so the `claude-agent-sdk` dependency is only
        # loaded when actually selected. Native users don't pay for it.
        from .loop_sdk import SdkAgentLoop  # noqa: PLC0415

        # The HookChain runs inside each MCP tool wrapper — the one
        # chokepoint we control under the SDK's own loop — so the sdk
        # backend gets the same Allow/Deny/AskUser/Rewrite + audit
        # guarantees as native.
        loop = SdkAgentLoop(
            tools=tools,
            config=loop_config,
            ccr_base_url=settings.agent.ccr_base_url,
            ccr_api_key=settings.agent.ccr_api_key.get_secret_value(),
            hooks=hooks_chain,
        )
        _log.info(
            "agent_loop_backend",
            extra={
                "backend": "sdk",
                "ccr": settings.agent.ccr_base_url,
                "hooks": hooks_chain.names() if hooks_chain else [],
            },
        )
    else:
        loop = NativeAgentLoop(
            client=client,
            tools=tools,
            config=loop_config,
            hooks=hooks_chain,
        )
        _log.info(
            "agent_loop_backend",
            extra={
                "backend": "native",
                "hooks": hooks_chain.names() if hooks_chain else [],
            },
        )

    return DataMindAgent(
        client=client,
        tools=tools,
        loop=loop,
        kb=kb,
        db=db,
        graph=graph,
        skills=skills,
        memory=memory,
        ingest=ingest,
        hooks=hooks_chain,
    )


__all__ = ["DataMindAgent", "build_agent"]
