# ADR 0006: Resolution Trace and Bounded Runtime Replanning

- Status: Accepted
- Date: 2026-07-31
- Scope: DataMind 0.7-C2

## Context

ADR 0005 introduced bounded compilation and one independently replayable
`DataPlan` Trace. Reusing that Trace for several replacement plans would break
its invariant that one trace has one plan, one terminal state, and one replay
artifact set.

Execution failures also previously lost the usage and completed operation
identities observed before failure. A replanner cannot share the caller's
budget correctly without those facts.

## Decision

One `Engine.resolve()` call owns a parent `resolution_id`. Each compiled plan
attempt receives a distinct child `trace_id` and remains independently
replayable:

```text
ResolutionTrace
├── PlanAttempt 1 -> ExecutionTrace 1 -> failed
└── PlanAttempt 2 -> ExecutionTrace 2 -> completed
```

DataMind 0.7 permits at most two plan attempts: one initial plan and one
complete replacement plan. Compilation may still perform its independently
bounded diagnostic repair within each plan attempt.

`ExecutionFailure` exposes only content-safe structured facts: category,
exception type and fingerprint, failed operation/source identity, completed
operation identities, observed usage, and recoverability. Raw provider error
messages are not supplied to the replanner or parent audit trace.

Only a `SourceExecutionError` is technically recoverable in 0.7. The Engine
also requires every operation in the failed plan to be `PURE` or `READ`.
Snapshot, budget, deadline, policy, type/plan, trace infrastructure, and other
execution failures terminate the resolution immediately.

Every failed operation consumes one action even when its provider cannot
return detailed token, latency, or cost usage. Successful operations completed
in the same batch retain their full observed usage. Compilation and every plan
attempt draw from one original request budget.

The second compiler request receives:

- the original intent and current authorized catalog;
- the complete prior verified plan;
- the sanitized `ExecutionFailure`;
- the residual request budget.

It must return a complete replacement `DataPlan`, not a patch.

## Consequences

- A failed child trace never becomes replayable as a successful execution.
- The final child trace can be replayed without invoking a model or source.
- Parent resolution audit records recovery decisions without weakening
  per-plan Trace invariants.
- Retrying the same failed provider operation is a planner decision visible in
  the replacement plan, not a hidden executor retry.
- Failed provider calls may under-report tokens, latency, or monetary cost
  until provider ports support metered failure responses; action accounting is
  conservative and mandatory.

## Deferred

- More than one runtime replan.
- Replanning after any write effect.
- Learned failure recovery or candidate-plan search.
- Cross-attempt reuse of successful native results.
- Quality-cost-latency physical optimization.
