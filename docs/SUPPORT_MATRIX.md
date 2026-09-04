# Native / SDK support matrix

This page defines the support boundary for DataMind v1.0.0. “Stable” means
the path is part of the release baseline, covered by the default dependency
set, and exercised by the repository's no-network tests. “Integration” means
the path is implemented but depends on an external process, vendor package, or
deployment-specific gateway that must be validated by the operator.

## Backend and protocol matrix

| Agent backend | `DATAMIND__LLM__PROTOCOL` | Upstream wire path | Extra requirements | v1.0 status |
|---|---|---|---|---|
| `native` (default) | `anthropic` | DataMind → Anthropic `/v1/messages` | `anthropic` package; an Anthropic-compatible gateway | **Stable core** |
| `native` (default) | `openai_chat_completions` | DataMind → OpenAI `/v1/chat/completions` | OpenAI-compatible gateway; no CCR | **Stable core** |
| `sdk` | `anthropic` | DataMind → Claude Agent SDK / `claude` CLI → configured Anthropic endpoint | `claude-agent-sdk`, Claude CLI, compatible endpoint | **Integration** |
| `sdk` | `openai_chat_completions` | DataMind → Claude Agent SDK / `claude` CLI → CCR → OpenAI gateway | `claude-agent-sdk`, Claude CLI, Node CCR, OpenAI-compatible gateway | **Integration** |

The model name never selects a protocol. Set the protocol explicitly, even
when a gateway uses a Claude-named model behind an OpenAI-compatible endpoint.

## Capability parity

| Capability | Native | SDK |
|---|---|---|
| StoreAgent / RetrieveAgent role split | Yes; two physical registries | Yes; the same registries are bridged into an in-process MCP server |
| Read-only RetrieveAgent boundary | Enforced before dispatch | Enforced before the SDK tool wrapper is exposed |
| StoreAgent receipts and idempotence | Yes | Yes; receipts are returned through MCP tool results |
| Path, destructive-SQL, and audit hooks | Yes; dispatch chokepoint | Yes; each MCP wrapper runs the same HookChain |
| SSE / `AgentEvent` stream | Yes; CI and local tests | Same event shape; requires SDK stack for live validation |
| Internal NL2SQL / memory / graph generation | Uses the configured LLM protocol | Uses the configured LLM protocol; backend choice does not silently change it |
| Default installation | Included in `pip install datamind` | Not included; install vendor SDK/CLI/CCR separately |

Both backends deliberately disable the SDK's or host process's unrelated file,
shell, and web tools. The only model-visible tools are the role-scoped
DataMind catalogue for that agent.

## Recommended configurations

### Native + Anthropic

```bash
export DATAMIND__AGENT__BACKEND=native
export DATAMIND__LLM__PROTOCOL=anthropic
export DATAMIND__LLM__API_BASE=https://your-anthropic-gateway.example.com
export DATAMIND__LLM__API_KEY=sk-ant-...
```

### Native + OpenAI-compatible

```bash
export DATAMIND__AGENT__BACKEND=native
export DATAMIND__LLM__PROTOCOL=openai_chat_completions
export DATAMIND__LLM__API_BASE=https://your-openai-gateway.example.com/v1
export DATAMIND__LLM__API_KEY=sk-...
```

### SDK + OpenAI-compatible gateway

```bash
export DATAMIND__AGENT__BACKEND=sdk
export DATAMIND__LLM__PROTOCOL=anthropic
export DATAMIND__AGENT__CCR_BASE_URL=http://127.0.0.1:13456
export DATAMIND__AGENT__CCR_API_KEY=dummy
```

Start CCR with the upstream OpenAI-format base URL and key using
[`scripts/start_ccr.sh`](../scripts/start_ccr.sh). The SDK path speaks
Anthropic to CCR; CCR performs the Anthropic ↔ OpenAI translation. Do not put
the real upstream key in a browser-facing environment or commit generated CCR
configuration.

## Test coverage

The default test command covers the native backend, protocol-neutral model
clients, tool boundaries, safety hooks, and HTTP API contracts. CI then runs
the offline SQLite demo as a separate no-network verification step. SDK-specific
hook tests are collected but skip when
`claude_agent_sdk` is not installed. To validate the SDK path, install its
vendor dependencies and run the same suite in an environment with CCR and a
test gateway.

## Stability rule

For v1.x, the stable surface is the native/local path plus the provider-neutral
contracts documented in [`STABLE_API.md`](./STABLE_API.md). The SDK loop,
`loop_native.py` implementation details, MCP wrapper internals, and provider
classes are extension points rather than promises of source-level stability.
