# DataMind-Bench v0.1

This directory is the executable acceptance layer for DataMind 0.7. It is
deliberately smaller than a publication benchmark: its job is to prove that
each Core capability works in a complete, inspectable task.

Run the canonical suite from the repository root:

```bash
python3.11 -m benchmarks.run_v01
```

The Core acceptance path uses only the Python standard library and the
repository source tree. The `Core Runtime` workflow runs it on Python 3.11 and
3.12 without provider or application dependencies.

The command runs 14 deterministic tasks:

- five single-surface contracts for Document, Table, Graph, Memory, and Skill;
- two cross-surface tasks for typed Join and runtime binding;
- two state-shift tasks for governed Memory write and bi-temporal history;
- one lifecycle task for idempotent ChangeSet sync and historical snapshots;
- four failure/replay tasks for Effect denial, terminal source failure,
  equivalent Replay, and bounded runtime replanning.

## Design boundary

- JSON files in `tasks/v0/` contain versioned metadata and registry IDs only.
- Trusted fixture, workload, script, and oracle implementations are registered
  explicitly in Python; task files cannot deserialize executable code.
- Every run constructs a new five-surface environment and destroys it after
  evaluation. Mutable state never leaks between tasks.
- Plan evaluation accepts semantic constraints rather than one exact gold DAG.
- Program assertions are authoritative. There is no LLM judge, dashboard,
  leaderboard, or paper-scale data-generation pipeline in v0.1.
- Each run produces an append-only `OutcomeRecord` covering execution, sources,
  operations, precedence, Effect, action count, Replay, and task-specific
  assertions.

DataMind-Bench v0.1 is therefore a regression and understanding tool. Dataset
scaling, baselines, statistical reporting, hidden tests, and human review
belong to the later research benchmark.
