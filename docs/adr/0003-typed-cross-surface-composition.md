# ADR 0003: Typed, structure-preserving cross-surface composition

- Status: Accepted
- Date: 2026-07-30

## Context

DataMind must compose document hits, table rows, graph paths, memory records,
and skill results without flattening every source into text. The initial
`DataPlan` validated graph topology, source capabilities, effects, and static
budgets, but it did not validate whether an operation could consume the result
kind produced by an upstream node. Its only cross-surface operator,
`Compose`, packaged native values and unioned evidence.

A single universal representation would erase source-native structure. A
model-powered semantic algebra would also make the deterministic Core depend on
model behavior and duplicate a separate research problem.

## Decision

DataMind uses three complementary result views:

1. `ResultEnvelope.value` preserves the source-native value.
2. `ResultEnvelope.evidence` provides normalized, provenance-bearing evidence.
3. `ResultEnvelope.bindings` provides an optional flat, JSON-safe
   `BindingSet` for deterministic composition.

Bindings carry structured join/filter fields and Evidence references. Document
and Memory bodies are not copied into bindings by default; they remain in the
native and Evidence views.

Each DataOp declares an `OperationSignature` with nominal input kinds, output
kind, input cardinality, and whether nested input paths are allowed. Static
plan validation rejects incompatible data-flow edges before execution.

The deterministic cross-surface algebra is deliberately small:

- `Project` selects fields from a normalized `BindingSet`.
- `Filter` evaluates a serializable scalar predicate.
- `Join` performs an inner exact-key join and namespaces both sides.
- `Fuse` applies deterministic reciprocal-rank fusion and deduplicates by
  provenance identity.
- `Compose` remains structure-preserving packaging and does not pretend to
  perform a join or evidence ranking.

Source adapters, not Core, construct the optional Binding view. Core therefore
does not import or inspect adapter-native result classes.

The Executor schedules independent `PURE` and `READ` nodes in bounded waves.
Any operation above `READ` is an ordering barrier and executes alone. Trace
events preserve deterministic logical order: all operations in a read wave are
recorded as started in topological order, followed by their terminal events in
the same order.

## Consequences

- Planner output can be rejected for nominal type errors before touching a
  source.
- Native structures remain available to source-specific logic.
- Exact cross-source joins preserve the evidence supporting each joined row.
- Replay recomputes deterministic composition operators from protected
  upstream result artifacts and checks their fingerprints.
- Independent reads reduce wall-clock latency without allowing concurrent
  writes.
- Dynamic token, cost, and latency usage is checked at each completed read
  wave; per-operation reservations are deferred until sources can expose
  reliable cost estimates.
- Binding field existence and scalar comparability are checked at deterministic
  runtime in v0.8; a full structural or dependent type system is out of scope.
- Semantic joins, model predicates, fuzzy entity resolution, and learned
  quality-cost optimization remain research extensions for later versions.

## Rejected alternatives

- **Flatten every result to text:** loses rows, paths, temporal state, and
  executable schemas.
- **Make every result a BindingSet:** prevents source-native operations and
  creates a lossy universal representation.
- **Let Core inspect adapter classes:** violates dependency direction.
- **Implement semantic joins in v0.8:** introduces model cost and
  nondeterminism before the deterministic contract and benchmark are stable.
- **Run all DAG nodes concurrently:** independent writes would have ambiguous
  order and state visibility.
