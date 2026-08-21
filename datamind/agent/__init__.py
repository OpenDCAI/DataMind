"""Agent layer — loop, assembly, prompts."""
from .base import AgentEvent, AgentLoopConfig, AgentLoopProtocol
from .loop_native import NativeAgentLoop
from .loop_openai import OpenAICompatibleAgentLoop
from .options import (
    AgentServices,
    DataMind,
    DataMindAgent,
    RetrieveAgent,
    StoreAgent,
    build_agent,
    build_datamind,
    build_store_agent,
)
from .prompts import build_retrieve_system_prompt, build_store_system_prompt, build_system_prompt

__all__ = [
    "AgentEvent",
    "AgentLoopConfig",
    "AgentLoopProtocol",
    "NativeAgentLoop",
    "OpenAICompatibleAgentLoop",
    "DataMindAgent",
    "AgentServices",
    "RetrieveAgent",
    "StoreAgent",
    "DataMind",
    "build_agent",
    "build_store_agent",
    "build_datamind",
    "build_system_prompt",
    "build_retrieve_system_prompt",
    "build_store_system_prompt",
]
