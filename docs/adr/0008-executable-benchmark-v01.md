# ADR 0008: Executable DataMind-Bench v0.1

- Status: accepted
- Scope: DataMind 0.7 acceptance, not paper-grade evaluation

## Decision

DataMind-Bench v0.1 is a repository-level consumer of public Core APIs. It does
not add benchmark concepts to `datamind.kernel`, `datamind.dataops`, or the
runtime engine.

Task metadata uses a versioned JSON contract. JSON may reference only explicit
fixture, workload, scripted-output, and oracle registry IDs. Executable code is
never deserialized from task files.

Every task receives a fresh environment. The reference environment contains
Document, Table, Graph, Memory, and Skill sources and owns its temporary
database, ArtifactStore, LifecycleManager, trace store, replay artifacts, and
outcome store. Lifecycle tasks therefore exercise the same public
`Engine.sync()` contract as external callers.

Plans are checked by semantic constraints:

- required source kinds and identities;
- required and allowed operation kinds;
- required dependency paths between operation kinds;
- maximum Effect and action count.

An exact gold plan is intentionally not required because several DAGs may be
semantically equivalent.

The runner supports:

1. handwritten Oracle workloads, including bounded multi-step state tasks;
2. known structured compiler outputs through the real `DataPlanCompiler`;
3. an injected live `PlanCompilerPort`.

Program assertions are primary. The runner converts contract checks and task
oracles into append-only `OutcomeRecord` values. Replay expectations are
explicitly `skip`, `equivalent`, or `forbidden`.

## Consequences

DataMind 0.7 gains a task-level definition of completeness without turning the
Core into a benchmark framework. DataMind 0.8 can use the same tasks for manual
walkthrough, simplification, debugging, and regression.

The v0.1 task count and fixture diversity are insufficient for publication
claims. Paper-scale task generation, hidden splits, baselines, confidence
intervals, and human review remain deferred.
