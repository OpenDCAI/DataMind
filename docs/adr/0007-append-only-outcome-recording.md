# ADR 0007: Append-Only Outcome Recording

- Status: Accepted
- Date: 2026-07-31
- Scope: DataMind 0.7-D1

## Context

Execution Trace answers what DataMind planned and executed. It cannot state
whether an external task oracle, human, or model evaluator considered the
result correct. Putting verdicts inside Trace would mix observed runtime facts
with external evaluation and would make replay artifacts depend on a benchmark.

Both `Engine.resolve()` and explicit `Engine.execute()` must be evaluable.
The former owns a parent Resolution identity, while the latter owns only a
single execution Trace.

## Decision

`OutcomeRecord` is a content-safe, append-only external verdict targeting
either a `RESOLUTION` or `TRACE`.

Every record contains:

- one task identity;
- evaluator kind, name, and version;
- one or more named boolean assertions with optional `[0, 1]` scores;
- an overall `succeeded` value equal to all assertion verdicts;
- an idempotency key, generated outcome identity, and observed timestamp.

Outcome records do not contain native results, evidence payloads, natural
language rationales, model reasoning, or enterprise content.

`OutcomeStore.record()` returns the prior record when the same idempotency key
is retried with equivalent semantic intent. Reusing the key for a different
target, evaluator, task, or verdict is a conflict. Records are never updated or
deleted through this contract.

The Core defines only the `OutcomeStore` port. DataMind 0.7 provides separate
in-memory and single-process JSONL reference adapters. The Store does not
verify that its target exists in a locally configured Trace or Resolution
store; offline evaluators may record outcomes produced in another process.

`Engine.record_outcome()` is an explicit API and does not feed the verdict back
into the Planner, Executor, Memory, or optimization policy.

## Consequences

- Benchmark evaluation remains separate from deterministic execution.
- Oracle plans and natural-language resolutions share one evaluation model.
- Multiple evaluators may append independent records for the same target.
- Retry safety does not require mutable outcome rows.
- Target integrity is checked by the future Benchmark runner when it joins
  tasks, executions, and outcomes.

## Deferred

- LLM Judge services and free-form feedback.
- Online learning, reward construction, or Planner adaptation.
- Aggregation databases, dashboards, rankings, or statistics APIs.
- Target existence validation and distributed multi-process JSONL locking.
