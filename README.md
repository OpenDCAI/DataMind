# DataMind

**DataMind is a typed and stateful inference data plane for enterprise agents.**

It gives an agent one explicit model for reading, composing, and updating the
heterogeneous data used during reasoning: documents and knowledge bases,
relational tables, knowledge graphs, memory, and executable skills.

DataMind 0.7 is a research preview of the complete Core path. It is designed
to make every reasoning-data action typed, governed, testable, and replayable
before individual algorithms are optimized for DataMind 0.8 and 1.0.

## Why a data plane?

A free-form tool loop decides and acts one call at a time. DataMind instead
turns intent into a bounded program that can be inspected before execution:

```text
request
  -> optional LLM compiler
  -> JSON-serializable DataPlan
  -> type / source / Effect / budget validation
  -> deterministic Executor
  -> ResultEnvelope with native value, evidence, provenance, snapshots and Trace
```

This separation makes the model responsible for proposing a plan, while the
Core remains responsible for what is legal, executable, and observable.

## DataMind 0.7 scope

- Typed `DataOp` and finite-DAG `DataPlan` contracts.
- Five first-class data surfaces: Document/KB, Table/DB, Graph, Memory, Skill.
- Deterministic cross-surface `Filter`, `Project`, `Join`, `Fuse`, and `Compose`.
- Effect lattice and request-scoped permission, approval, scope, snapshot, and
  budget checks.
- Bi-temporal Memory recall plus governed propose/apply mutation semantics.
- Snapshot and `ChangeSet` lifecycle with historical reads.
- Execution Trace, protected offline Replay, resolution trace, and append-only
  outcome recording.
- Bounded NL-to-`DataPlan` compilation and at most one runtime replan.
- Fourteen deterministic DataMind-Bench v0.1 acceptance tasks.

The Core deliberately does not contain a Web UI, deployment platform,
customer-specific connectors, general agent orchestration, or heavy data
cleaning. DataFlow owns cleaning and training-data production; application
teams integrate DataMind through adapters around the stable ports.

## Architecture

```text
datamind/
├── kernel/          # identity, Effect, budget, scope, snapshots, domain types
├── dataops/         # typed operations, plans, results, schema and validation
├── engine/          # deterministic execution, resolution, recording and replay
├── intelligence/    # bounded structured plan compiler
├── lifecycle/       # explicit source catalog and snapshot synchronization
├── ports/           # source, model, lifecycle, trace and outcome protocols
└── adapters/        # dependency-light reference implementations

benchmarks/          # executable DataMind-Bench v0.1 acceptance layer
docs/adr/            # architectural decisions and rejected alternatives
```

The dependency direction points inward: `kernel` and `dataops` contain no
provider dependencies; Core services know only stable ports; adapters remain
replaceable. Source registration is always explicit.

## Quick start

DataMind Core has no mandatory third-party runtime dependencies.

```bash
git clone https://github.com/OpenDCAI/DataMind.git
cd DataMind
python3.11 -m pip install -e .
python3.11 -m examples.typed_data_plane
```

The minimal execution model is:

```python
result = await engine.execute(
    operation_or_plan,
    context=execution_context,
)
```

`result.value` preserves the adapter's typed native value. The surrounding
`ResultEnvelope` carries evidence, bindings, provenance, pinned snapshots,
usage, warnings, status, and a trace identity without flattening the result.

## Validate the research Core

Run the complete deterministic test suite:

```bash
python3.11 -m unittest discover -s datamind/tests -p 'test_*.py'
```

Run the 14-task acceptance benchmark:

```bash
python3.11 -m benchmarks.run_v01
```

DataMind-Bench v0.1 is intentionally small. It is an executable specification
for understanding and debugging 0.7, not yet the publication-scale benchmark.
See [`benchmarks/README.md`](benchmarks/README.md) for task coverage.

## From 0.7 to 0.8

The 0.7 milestone answers: *does every essential module form one coherent,
inspectable end-to-end system?* The 0.8 milestone will answer: *does each
module behave robustly across representative tasks after manual review,
targeted debugging, and measured trade-off analysis?*

The next work is therefore validation rather than more surface area: inspect
each module and trace, expand high-value cases, characterize failure modes,
and only then optimize planning, memory policy, execution, and evaluation.

## License

Apache-2.0.
