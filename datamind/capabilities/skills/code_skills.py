"""Built-in code skills — ported from the legacy modules/skills/tools.py.

These are pure-Python utility tools the agent can call directly. Adding a
new one is a matter of writing the function and wrapping it in a ToolSpec.
"""
from __future__ import annotations

import datetime
import math
from typing import Any

from datamind.core.tools import ToolSpec


# ---------------------------------------------------------------- time ---


async def _get_current_time() -> dict[str, Any]:
    now = datetime.datetime.now()
    return {
        "iso": now.isoformat(),
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "hour": now.hour,
        "minute": now.minute,
        "weekday": now.strftime("%A"),
    }


# ------------------------------------------------------------ calculator ---


_SAFE_CALC = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log2": math.log2, "log10": math.log10,
    "exp": math.exp, "pow": pow, "abs": abs,
    "pi": math.pi, "e": math.e,
    "floor": math.floor, "ceil": math.ceil,
    "min": min, "max": max, "round": round, "sum": sum,
}


async def _calculator(expression: str) -> dict[str, Any]:
    """Evaluate a safe math expression."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("expression must be a non-empty string")
    # Reject obvious escapes before handing to eval.
    forbidden = ("__", "import", "open", "exec", "eval", "compile", "globals", "locals")
    low = expression.lower()
    if any(tok in low for tok in forbidden):
        raise ValueError("expression contains forbidden tokens")
    result = eval(expression, {"__builtins__": {}}, _SAFE_CALC)  # noqa: S307 — safe env
    return {"expression": expression, "result": result}


# --------------------------------------------------------------- text ---


async def _analyze_text(text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    lines = text.splitlines()
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    words = text.split()
    return {
        "chars": len(text),
        "chars_no_space": len(text.replace(" ", "").replace("\t", "").replace("\n", "")),
        "lines": len(lines),
        "paragraphs": len(paragraphs),
        "words": len(words),
    }


# ------------------------------------------------------------ units ---


_CONVERSIONS: dict[tuple[str, str], Any] = {
    ("km", "m"): lambda v: v * 1000,
    ("m", "km"): lambda v: v / 1000,
    ("m", "cm"): lambda v: v * 100,
    ("cm", "m"): lambda v: v / 100,
    ("mi", "km"): lambda v: v * 1.609344,
    ("km", "mi"): lambda v: v / 1.609344,
    ("kg", "g"): lambda v: v * 1000,
    ("g", "kg"): lambda v: v / 1000,
    ("lb", "kg"): lambda v: v * 0.45359237,
    ("kg", "lb"): lambda v: v / 0.45359237,
    ("c", "f"): lambda v: v * 9 / 5 + 32,
    ("f", "c"): lambda v: (v - 32) * 5 / 9,
}


async def _unit_convert(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    key = (from_unit.lower(), to_unit.lower())
    fn = _CONVERSIONS.get(key)
    if fn is None:
        raise ValueError(f"No conversion from {from_unit!r} to {to_unit!r}. Known pairs: {list(_CONVERSIONS)[:5]}...")
    return {
        "value": value,
        "from": from_unit,
        "to": to_unit,
        "result": fn(value),
    }


# ------------------------------------------------------------ assemble ---


def build_code_skills() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_current_time",
            description="Return the current local date and time.",
            input_schema={"type": "object", "properties": {}},
            handler=_get_current_time,
            metadata={"group": "skill.code", "surface": "skills", "access": "utility"},
        ),
        ToolSpec(
            name="calculator",
            description=(
                "Evaluate a numeric expression. Supports +-*/** % parentheses and "
                "functions: sqrt sin cos tan log log2 log10 exp pow abs floor ceil "
                "min max round sum; constants pi and e."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "e.g. '2 * (3 + sqrt(16))'"},
                },
                "required": ["expression"],
            },
            handler=_calculator,
            metadata={"group": "skill.code", "surface": "skills", "access": "utility"},
        ),
        ToolSpec(
            name="analyze_text",
            description="Count characters, lines, paragraphs, and words in a string.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=_analyze_text,
            metadata={"group": "skill.code", "surface": "skills", "access": "utility"},
        ),
        ToolSpec(
            name="unit_convert",
            description="Convert a numeric value between common units (length, mass, temperature).",
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "number"},
                    "from_unit": {"type": "string", "description": "e.g. 'km' | 'mi' | 'kg' | 'c'"},
                    "to_unit": {"type": "string", "description": "e.g. 'm' | 'km' | 'f'"},
                },
                "required": ["value", "from_unit", "to_unit"],
            },
            handler=_unit_convert,
            metadata={"group": "skill.code", "surface": "skills", "access": "utility"},
        ),
    ]


__all__ = ["build_code_skills"]
