# ADR 0004: Bounded bindings, typed Graph, and governed Skills

- Status: Accepted
- Date: 2026-07-30

## Context

The first typed composition layer could join and filter results after source
operations completed, but source-operation parameters were compile-time
literals. A graph traversal could not consume entity identifiers returned by a
table or document operation, and an executable Skill could not consume a value
selected by a prior operation.

The legacy Graph and Skills capabilities were coupled to the old tool registry,
provider DTOs, import-time registration, and application dependencies. Reusing
those contracts would reverse the new Core dependency direction.

Graph providers also differ in result ordering and query-language semantics.
Skill manifests and remote tool annotations describe claimed behavior, but
cannot by themselves grant execution authority.

## Decision

DataMind adds a deliberately limited `ValueBinding`:

- `SINGLE` requires exactly one upstream Binding row.
- `COLLECT` reads one scalar field, deterministically deduplicates it, and
  fails if the explicit `max_items` bound is exceeded.
- Bindings cannot contain arbitrary expressions, model calls, iteration, or
  nested value paths.
- The Executor resolves bindings to literals before a source Adapter is called.
  Adapters therefore remain independent of DataPlan scheduling.

Graph uses provider-independent `GraphNode`, `GraphEdge`, `GraphPath`, and
`GraphPathSet` native values. `Traverse` returns bounded simple paths with an
explicit direction, relation filter, hop range, and result limit. The reference
Adapter produces deterministic ordering, Evidence per path, and a stable
Binding view for later joins. Core does not parse Cypher, Gremlin, or a new
general graph query language in v0.8.

Skills use exact content-derived identity:

```text
SkillRef = name + version + SHA-256 digest
```

`SkillSpec` distinguishes instruction-only, executable, and hybrid Skills.
`ResolveSkill` retrieves governed specs and loads instruction Evidence.
`InvokeSkill` accepts only an exact `SkillRef`; an instruction-only Skill is not
executable.

The authoritative execution effect comes from a trusted Adapter registration,
not from `SKILL.md`, MCP annotations, or model output. The operation declares
the governed effect for static preflight, and the Adapter verifies that it
exactly matches the trusted registration before entering the handler. A lower
forged effect therefore fails closed. Normal approval, resource, idempotency,
write-serialization, Trace, and Replay rules still apply.

The reference Skill Adapter validates a deterministic top-level JSON Schema
subset: object properties, required fields, additional-properties policy, and
primitive field types. Full JSON Schema dialect support remains an Adapter
concern.

## Consequences

- A DataPlan can express SQL-to-Graph and SQL-to-Skill data dependencies
  without becoming a workflow engine.
- Dynamic cardinality is explicit and bounded rather than silently fanning out.
- Graph paths retain native structure, normalized Evidence, and a relational
  Binding view at the same time.
- Skill instructions become queryable reasoning data while executable Skills
  remain governed actions.
- A dynamically resolved Skill is not invoked in the same unchecked step.
  Planner/Replanner must pin its exact identity and compile a checked
  invocation.
- Offline Replay uses recorded source artifacts and recomputes downstream
  deterministic composition without consulting live Graph, Skill, or table
  sources.

## Deferred work

- Semantic entity resolution and fuzzy joins.
- Cypher, Gremlin, ISO GQL, and provider-native graph query compilation.
- Graph mutation.
- Remote Skill/MCP invocation and complete JSON Schema dialect validation.
- LLM Planner, Skill quality optimization, and dynamic capability learning.
- Removal of the legacy Capability and ToolRegistry paths.
