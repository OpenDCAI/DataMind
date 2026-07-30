# ADR 0005: Bounded Plan Compilation and Read-Only Resolution

- Status: Accepted
- Date: 2026-07-31
- Scope: DataMind 0.8-C1

## Context

The legacy agent loop lets a model select and invoke one tool at a time. That
path is useful as a baseline, but the complete action graph, total budget,
source permissions, and effects are unavailable before execution.

The deterministic Core already owns versioned `DataPlan` serialization,
static validation, effect preflight, scheduling, Trace, and Replay. The missing
boundary is a narrow compiler from natural-language intent to that existing
instruction set.

## Decision

`Engine.resolve()` uses an injected `PlanCompilerPort`:

```text
intent + authorized catalog view
→ structured model draft
→ deterministic authority binding
→ deserialize and validate DataPlan
→ deterministic Executor
→ Resolution
```

The model-facing draft omits authority-bearing fields. The compiler assigns
the plan identity and version, binds source kinds from the catalog, binds
Skill effect policy from trusted registrations, derives Memory idempotency,
and allocates the residual execution budget after model usage.

Schema conformance is not execution authority. Every draft must still pass
`plan_from_dict()`, `validate_plan()`, scope checks, effect checks, and Executor
preflight.

Compilation uses one initial model call and at most one diagnostic repair.
There is no unbounded retry or candidate search in 0.8.

`resolve()` caps execution at `READ`, even when its caller has a more powerful
context. It may return a `MemoryMutationProposal`, but it cannot apply one.
Only `USER_EXPLICIT` proposals for session or principal scopes are exposed to
automatic compilation. Write-effect and approval-requiring Skills are removed
from the planning view.

The planning catalog contains source identity, schema, capabilities, version,
and governed Skill signatures. It omits source metadata and filters resources
using the same allowlist used by execution.

## Consequences

- A request has a complete, serializable plan before data execution.
- Independent reads can use the Executor's existing parallel scheduling.
- Compiler and execution usage share one request budget.
- Provider SDK types remain outside `kernel`, `dataops`, `ports`, `engine`,
  and `intelligence`.
- Compilation attempts retain model identity, usage, diagnostics, and output
  fingerprints without retaining raw model output in the public record.
- The legacy agent loop remains a benchmark baseline, not an alternative Core
  control path.

Runtime replanning is deferred to 0.8-C2. The current Trace contract is scoped
to one DataPlan; C2 must introduce a resolution-level parent identity before
multiple execution-attempt traces can be represented without weakening Replay.

Quality-cost-latency physical plan optimization is also deferred until
DataMind-Bench provides operator selectivity, latency, cost, and quality
measurements.

## Rejected alternatives

- **Reuse the legacy tool loop:** hides the complete plan until after actions
  execute and weakens preflight validation.
- **Let the model emit source kinds, effects, approvals, or budgets:** treats
  untrusted generation as authority.
- **Automatically execute writes from `resolve()`:** collapses proposal and
  application into one unsafe step.
- **Add runtime replanning to the existing plan trace:** makes one trace
  represent multiple terminal plans and breaks current replay invariants.
