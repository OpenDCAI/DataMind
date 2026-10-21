"""Generic benchmark loop primitives. Product reasoning is hosted by Codex."""
from .base import AgentEvent, AgentLoopConfig, AgentLoopProtocol
from .loop_native import NativeAgentLoop
from .loop_openai import OpenAICompatibleAgentLoop
__all__ = ['AgentEvent','AgentLoopConfig','AgentLoopProtocol','NativeAgentLoop','OpenAICompatibleAgentLoop']
