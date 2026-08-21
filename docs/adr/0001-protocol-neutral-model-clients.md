# ADR 0001: Protocol-neutral model clients

- Status: Accepted
- Date: 2026-08-21

## Context

The native agent loop and internal generation paths previously called the
Anthropic SDK directly. A caller could replace only the outer loop with an
OpenAI-compatible implementation while NL2SQL, multi-query retrieval, memory
extraction, and graph ingestion continued to call `/v1/messages`. This made a
single DataMind instance internally inconsistent.

## Decision

DataMind supports two explicit wire protocols:

- `anthropic`
- `openai_chat_completions`

`LLMConfig.protocol` selects the main protocol. `fallback_protocol` defaults to
the main protocol and may be set separately when a gateway genuinely exposes
the fallback model on a different protocol. Model names never imply a wire
protocol, and `agent.backend` no longer serves as a protocol selector.

Both providers implement the same `TextModelClient` and
`ToolCallingModelClient` contracts. The agent loop consumes normalized text and
tool-use blocks. NL2SQL, query rewriting, memory extraction, and ingest receive
one of the same model clients through dependency injection. Tool schemas remain
vendor-neutral in `ToolRegistry`; provider serializers own the wire format.

The `sdk` backend remains available for Claude Agent SDK/CCR deployments. The
native backend selects its serializer from `LLMConfig.protocol` and supports
real streaming for both protocols.

## Consequences

- OpenAI-only gateways no longer make hidden Anthropic requests.
- Requested and resolved model names can be recorded independently.
- Unsupported protocols fail during construction.
- Provider-specific message and streaming differences are isolated in
  `datamind/core/model_clients.py`.
- All tool execution continues through the same registry, HookChain, evidence,
  and budget path.
