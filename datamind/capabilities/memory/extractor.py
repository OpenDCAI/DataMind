"""Fact extraction helper.

Given a recent exchange (user turn + assistant turn), ask the cheap fallback
model to extract a short list of factual statements worth remembering.
These land in long-term memory under the session namespace; future turns
in the same session can recall them, and so can sibling sessions under
the same user namespace.

Why a tiny dedicated prompt and not "just save the whole turn": the agent
loop will call this on every turn. Saving verbatim quickly fills recall
with chatter ("ok thanks", "how are you"). Extraction keeps memory lean.
"""
from __future__ import annotations

import json
import re
from typing import Any

from datamind.core.logging import get_logger
from datamind.core.protocols import TextModelClient

_log = get_logger("memory.extractor")


_PROMPT = """You extract durable facts worth remembering from a conversation turn.

Return a JSON array of strings. Each string is ONE self-contained statement
that a future chatbot turn might want to look up — preferences, identities,
decisions, numeric thresholds, names, schedules, or domain facts stated by
the user.

Skip: greetings, apologies, filler, questions the user asked, anything the
assistant said unless the user explicitly confirmed it.

If there is nothing worth saving, return [].

User: {user}
Assistant: {assistant}

JSON:"""


_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


async def extract_facts(
    *,
    client: TextModelClient,
    model: str,
    user_turn: str,
    assistant_turn: str,
    max_facts: int = 6,
) -> list[str]:
    try:
        text = (await client.generate_text(
            _PROMPT.format(user=user_turn, assistant=assistant_turn),
            model=model,
            max_tokens=400,
            temperature=0.0,
        )).strip()
        m = _JSON_RE.search(text)
        parsed = json.loads(m.group(0) if m else text)
        if not isinstance(parsed, list):
            return []
        facts: list[str] = []
        for item in parsed:
            if isinstance(item, str) and item.strip():
                facts.append(item.strip())
            if len(facts) >= max_facts:
                break
        return facts
    except Exception as exc:  # noqa: BLE001
        _log.warning("fact_extraction_failed", extra={"err": repr(exc)})
        return []
