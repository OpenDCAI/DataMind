# DataMind Maintainer Research Reference Map

Status: maintainer note  
Reference commit: `aa5c7c1`

This note indexes implementation ideas and experimental assets that are not
part of the DataMind Core runtime. It is intended for maintainers planning
research baselines, provider adapters, and benchmark extensions. It should
not be linked from the project README or release-facing documentation.

The current typed contracts, ADRs, and executable tests are authoritative.
Items below are references to mine selectively, not modules to restore as a
second supported execution path.

## Experimental baselines

### Free tool-loop baseline

Reference paths:

- `datamind/agent/loop_native.py`
- `datamind/agent/loop_sdk.py`
- `datamind/tests/test_agent_loop.py`

Potential use: compare incremental model-driven tool selection with bounded
DataPlan compilation and deterministic execution. Reuse only as an isolated
benchmark baseline; do not expose it as an alternative Core API.

### Retrieval baselines

Reference paths:

- `datamind/capabilities/kb/providers/hybrid_retriever.py`
- `datamind/capabilities/kb/providers/multi_query_retriever.py`
- `datamind/tests/test_retrievers.py`

Potential use: BM25/vector Reciprocal Rank Fusion and multi-query expansion
baselines for quality-cost-latency experiments. These are standard retrieval
methods rather than Core research contributions.

### Memory allocation baseline

Reference paths:

- `datamind/capabilities/memory/providers/sqlite_store.py`
- `datamind/capabilities/memory/service.py`
- `datamind/tests/test_memory.py`

Potential use: SQLite persistence, embedding/lexical recall, schema migration,
soft deletion, and fixed session/profile/global recall budgets. The fixed
scope allocation is a useful baseline for future dynamic memory-allocation
research, but it does not implement the typed bi-temporal Memory semantics of
the current Core.

## Governance and failure cases

Reference paths:

- `datamind/capabilities/hooks/audit.py`
- `datamind/capabilities/hooks/destructive_sql.py`
- `datamind/capabilities/hooks/path_allowlist.py`
- `datamind/tests/test_hooks.py`
- `datamind/tests/test_db_safeguard.py`

Potential use: tamper-evident append chains, secret-field redaction, explicit
confirmation, path traversal/symlink cases, and adversarial SQL cases. When a
case is still relevant, port the assertion to Effect, Trace, or an adapter
contract test instead of importing the hook framework.

## Evaluation references

Reference paths:

- `benchmark/evaluate.py`
- `benchmark/metrics/golden_answer_metrics.py`
- `benchmark/models/embedding_models.py`
- `benchmark/models/llm_judges.py`

Potential use: exact match, token F1, semantic similarity, claim extraction,
and NLI-style factuality ideas for a later DataMind-Bench study. The code is
not a drop-in benchmark component: it has provider-heavy and external package
assumptions and lacks the executable state/effect assertions required by the
current benchmark contract.

## Provider adapter prototypes

Reference paths:

- `datamind/capabilities/graph/providers/networkx_store.py`
- `datamind/capabilities/skills/loader.py`
- `datamind/capabilities/db/providers/`
- `datamind/capabilities/embedding/providers/`
- `datamind/capabilities/kb/providers/chroma_store.py`

Potential use: implementation sketches for persistence, filesystem Skill
discovery, database dialects, embedding clients, and vector storage. Any reuse
must be expressed behind the current Ports and pass the shared source contract
tests; provider dependencies remain optional and outside the domain kernel.

## Enterprise task seeds

Reference paths:

- `datamind/scripts/seed_enterprise_demo.py`
- `demo-uploads/`
- `data/profiles/`

Potential use: mine candidate cross-surface tasks and failure scenarios for
DataMind-Bench. Treat the material as synthetic task seeds, not verified gold
data. New public tasks require program oracles and human review where needed.

## Selective reuse rule

Before reusing an item from the reference commit:

1. State the research question or missing adapter behavior in an ADR/RFC.
2. Extract the smallest relevant algorithm, test case, or fixture.
3. Translate it to `DataOp`, `DataPlan`, `ResultEnvelope`, Effect, Snapshot,
   provenance, and Trace semantics as applicable.
4. Keep provider dependencies outside `kernel`, `dataops`, and Core services.
5. Add deterministic contract or benchmark assertions before optimization.

## Retrieval commands

```bash
git show aa5c7c1:datamind/agent/loop_native.py
git show aa5c7c1:datamind/capabilities/memory/providers/sqlite_store.py
git show aa5c7c1:datamind/capabilities/hooks/audit.py
git show aa5c7c1:datamind/capabilities/kb/providers/hybrid_retriever.py
git show aa5c7c1:benchmark/evaluate.py
```
