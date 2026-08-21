"""OpenAI Chat Completions agent loop.

The execution, hook, evidence, budget, and finalization logic lives in the
shared native loop.  This named class makes the supported wire protocol
explicit for callers and contract tests.
"""
from __future__ import annotations

from .loop_native import NativeAgentLoop


class OpenAICompatibleAgentLoop(NativeAgentLoop):
    """Native loop driven by an OpenAI-compatible model client."""


__all__ = ["OpenAICompatibleAgentLoop"]
