"""Agent loop tests using a fake Anthropic client.

The real Anthropic SDK is too heavy to mock per-field; instead we define a
minimal `_FakeClient` that mimics the subset of `messages.create` used by
AgentLoop.run_turn — enough to script a deterministic conversation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from datamind.agent.loop_native import NativeAgentLoop
from datamind.agent.base import AgentLoopConfig
from datamind.core.errors import FinalAnswerContractError
from datamind.core.tools import ToolRegistry, ToolSpec


# ----------------------------------------------------- fake anthropic ---


@dataclass
class _Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class _Usage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Message:
    content: list[_Block]
    stop_reason: str
    usage: _Usage = field(default_factory=_Usage)


class _FakeMessages:
    def __init__(self, script: list[_Message]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def create(self, **kwargs: Any) -> _Message:
        self.calls.append(kwargs)
        if not self._script:
            raise RuntimeError("_FakeClient script exhausted")
        return self._script.pop(0)


class _FakeClient:
    def __init__(self, script: list[_Message]) -> None:
        self.messages = _FakeMessages(script)


# ------------------------------------------------------- tiny tool ---


def _tool_echo() -> ToolSpec:
    async def handler(value: str) -> dict:
        return {"echoed": value}

    return ToolSpec(
        name="echo",
        description="echo",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=handler,
        metadata={"group": "other", "surface": "kb", "access": "read"},
    )


def _tool_boom() -> ToolSpec:
    async def handler() -> dict:
        raise RuntimeError("tool blew up")

    return ToolSpec(
        name="boom",
        description="always raises",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        metadata={"group": "other"},
    )


def _tool_slow() -> ToolSpec:
    async def handler() -> dict:
        await asyncio.sleep(1)
        return {"late": True}

    return ToolSpec(
        name="slow",
        description="sleeps",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        metadata={"group": "other", "surface": "kb", "access": "read"},
    )


# ---------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_single_turn_no_tools():
    script = [
        _Message(content=[_Block(type="text", text="hello")], stop_reason="end_turn"),
    ]
    reg = ToolRegistry()
    loop = NativeAgentLoop(
        client=_FakeClient(script),  # type: ignore[arg-type]
        tools=reg,
        config=AgentLoopConfig(model="m"),
    )
    out = await loop.run_turn(user_message="hi")
    assert out["answer"] == "hello"
    assert out["iterations"] == 1
    assert out["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_tool_use_then_final_answer():
    script = [
        _Message(
            content=[_Block(type="tool_use", id="t1", name="echo", input={"value": "hi"})],
            stop_reason="tool_use",
        ),
        _Message(
            content=[_Block(type="text", text="the echo said hi")],
            stop_reason="end_turn",
        ),
    ]
    reg = ToolRegistry(); reg.add(_tool_echo())
    loop = NativeAgentLoop(
        client=_FakeClient(script),  # type: ignore[arg-type]
        tools=reg,
        config=AgentLoopConfig(model="m"),
    )
    out = await loop.run_turn(user_message="echo hi please")
    assert out["iterations"] == 2
    assert "echo said hi" in out["answer"]
    assert out["surfaces_used"] == ["kb"]
    assert out["tool_trace"][0]["name"] == "echo"
    assert out["evidence"][0]["surface"] == "kb"
    # Second API call should have seen a tool_result in its messages.
    call2 = loop._client.messages.calls[1]  # type: ignore[attr-defined]
    assert any(
        isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" for b in m["content"])
        for m in call2["messages"]
    )


@pytest.mark.asyncio
async def test_invalid_contract_gets_one_validated_no_tool_repair():
    script = [
        _Message(content=[_Block(type="text", text="Answer: [1]")], stop_reason="end_turn"),
        _Message(content=[_Block(type="text", text="[1]")], stop_reason="end_turn"),
    ]
    loop = NativeAgentLoop(
        client=_FakeClient(script),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        config=AgentLoopConfig(model="m"),
    )
    out = await loop.run_turn(
        user_message="return an array",
        final_contract={"type": "array", "json_schema": {"type": "array"}},
    )
    assert out["answer"] == "[1]"
    assert out["stop_reason"] == "contract_repaired"
    assert out["contract_valid"] is True
    assert out["contract_repair_attempted"] is True
    assert "tools" not in loop._client.messages.calls[-1]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_invalid_contract_repair_raises_typed_error_instead_of_leaking_text():
    script = [
        _Message(content=[_Block(type="text", text="not json")], stop_reason="end_turn"),
        _Message(content=[_Block(type="text", text="still not json")], stop_reason="end_turn"),
    ]
    loop = NativeAgentLoop(
        client=_FakeClient(script),  # type: ignore[arg-type]
        tools=ToolRegistry(),
        config=AgentLoopConfig(model="m"),
    )
    with pytest.raises(FinalAnswerContractError, match="not valid JSON"):
        await loop.run_turn(
            user_message="return an array",
            final_contract={"type": "array", "json_schema": {"type": "array"}},
        )


@pytest.mark.asyncio
async def test_slow_tool_is_cut_off_to_preserve_contract_finalization_budget():
    script = [
        _Message(
            content=[_Block(type="tool_use", id="t1", name="slow", input={})],
            stop_reason="tool_use",
        ),
        _Message(content=[_Block(type="text", text='["ok"]')], stop_reason="end_turn"),
    ]
    registry = ToolRegistry(); registry.add(_tool_slow())
    loop = NativeAgentLoop(
        client=_FakeClient(script),  # type: ignore[arg-type]
        tools=registry,
        config=AgentLoopConfig(
            model="m", max_tool_turns=2,
            wall_clock_timeout_s=0.3, finalization_reserve_s=0.15,
        ),
    )
    out = await loop.run_turn(
        user_message="use the tool then return an array",
        final_contract={"type": "array", "json_schema": {"type": "array"}},
    )
    assert out["answer"] == '["ok"]'
    assert out["contract_valid"] is True
    assert out["tool_trace"][0]["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_tool_error_is_surfaced_as_tool_result():
    script = [
        _Message(
            content=[_Block(type="tool_use", id="t1", name="boom", input={})],
            stop_reason="tool_use",
        ),
        _Message(
            content=[_Block(type="text", text="the tool failed, I'll stop")],
            stop_reason="end_turn",
        ),
    ]
    reg = ToolRegistry(); reg.add(_tool_boom())
    loop = NativeAgentLoop(
        client=_FakeClient(script),  # type: ignore[arg-type]
        tools=reg,
        config=AgentLoopConfig(model="m"),
    )
    out = await loop.run_turn(user_message="call boom")
    # On the second API call, the conversation must contain a tool_result
    # with is_error=true — that's how we signal tool failure to the model.
    call2_msgs = loop._client.messages.calls[1]["messages"]  # type: ignore[attr-defined]
    flat_blocks = [
        b
        for m in call2_msgs
        if isinstance(m.get("content"), list)
        for b in m["content"]
    ]
    error_blocks = [b for b in flat_blocks if b.get("type") == "tool_result" and b.get("is_error")]
    assert error_blocks, "expected a tool_result with is_error=true"
    assert "RuntimeError" in error_blocks[0]["content"]


@pytest.mark.asyncio
async def test_max_tool_turns_enforced():
    # Script: infinite tool_use loops.
    script = [
        _Message(
            content=[_Block(type="tool_use", id=f"t{i}", name="echo", input={"value": str(i)})],
            stop_reason="tool_use",
        )
        for i in range(20)
    ]
    reg = ToolRegistry(); reg.add(_tool_echo())
    loop = NativeAgentLoop(
        client=_FakeClient(script),  # type: ignore[arg-type]
        tools=reg,
        config=AgentLoopConfig(model="m", max_tool_turns=3),
    )
    out = await loop.run_turn(user_message="spin forever")
    assert out["stop_reason"] == "max_iterations"
    assert out["iterations"] == 3


@pytest.mark.asyncio
async def test_hooks_fire_around_tool_call():
    calls: list[tuple[str, str]] = []

    async def on_start(name, inp):
        calls.append(("start", name))

    async def on_end(name, inp, result, err):
        calls.append(("end", name))

    script = [
        _Message(
            content=[_Block(type="tool_use", id="t1", name="echo", input={"value": "x"})],
            stop_reason="tool_use",
        ),
        _Message(content=[_Block(type="text", text="done")], stop_reason="end_turn"),
    ]
    reg = ToolRegistry(); reg.add(_tool_echo())
    loop = NativeAgentLoop(
        client=_FakeClient(script),  # type: ignore[arg-type]
        tools=reg,
        config=AgentLoopConfig(model="m"),
        on_tool_start=on_start,
        on_tool_end=on_end,
    )
    await loop.run_turn(user_message="hi")
    assert calls == [("start", "echo"), ("end", "echo")]


def test_prompt_groups_tools_by_category():
    from datamind.agent.prompts import build_system_prompt

    reg = ToolRegistry()
    reg.add(
        ToolSpec(
            name="kb_search", description="", input_schema={},
            handler=lambda: None, metadata={"group": "kb"},  # type: ignore[arg-type]
        )
    )
    reg.add(
        ToolSpec(
            name="db_query_sql", description="", input_schema={},
            handler=lambda: None, metadata={"group": "db"},  # type: ignore[arg-type]
        )
    )
    prompt = build_system_prompt([reg.get(n) for n in reg.names()])
    assert "kb_search" in prompt
    assert "db_query_sql" in prompt
    # Chinese section labels present.
    assert "知识库" in prompt
    assert "数据库" in prompt
