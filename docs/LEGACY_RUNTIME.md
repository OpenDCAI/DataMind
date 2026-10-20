# Compatibility runtime

The default Codex plugin and new `datamind.codex.CodexSession` use direct data
operations. This page describes the older built-in agent runtime, retained for
existing applications and benchmark comparisons.

```python
from datamind.agent import build_datamind
from datamind.config import Settings

async def answer():
    system = await build_datamind(Settings())
    try:
        await system.ingest("Remember weekly reports use Chinese.")
        return await system.query("What language should reports use?")
    finally:
        await system.aclose()
```

Configure `DATAMIND__LLM__API_KEY`, `API_BASE`, `MODEL` and `PROTOCOL` for
Anthropic Messages or OpenAI Chat Completions. `agent.backend=native` chooses
the built-in loop; `agent.backend=sdk` means the old Claude Agent SDK/CCR path.
Neither setting selects the official Codex SDK.

`datamind chat`, `python -m uvicorn datamind.server:app --port 8000`, and existing
HTTP ask/store/chat endpoints still use this compatibility runtime. StoreAgent
receives receipt-wrapped writes and procedural/input reads; RetrieveAgent receives
read/utility tools. Existing graph extraction and NL2SQL helpers remain available
here. Direct Codex callers perform those reasoning steps in the main agent.

Existing `Workspace(settings).update(...)` retains legacy model extraction when
no externally prepared graph_sources are supplied. New direct callers build a
Workspace with `builder=build_data_plane` and supply explicit graph_sources, or
use the default MCP prepare/publish tools. Snapshots and direct live writes are
separate in both integrations.
