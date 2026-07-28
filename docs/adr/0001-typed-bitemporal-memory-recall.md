# ADR-0001: Typed, bi-temporal Memory Recall

- Status: Accepted
- Date: 2026-07-28

## Context

DataMind treats Memory as one inference data surface alongside documents,
tables, graphs, and executable Skills. A Memory contract defined by a vector
store would conflate logical semantics with one retrieval implementation and
would not preserve corrections, scope boundaries, or historical belief state.

## Decision

1. `MemoryRecord` is the immutable semantic unit. Raw conversations, documents,
   queries, and tool traces are referenced through content-free `EvidenceRef`
   values rather than treated as equivalent semantic memories.
2. Records have both a valid-time interval (when the assertion applies in the
   modeled world) and a transaction-time interval (when it is present in the
   system's knowledge). Intervals are half-open.
3. Recall names every requested `ScopeRef`. Access is checked against the
   execution context before an adapter runs. Scope kinds have no implicit
   inheritance, and historical queries do not bypass current authorization.
4. Corrections preserve history. `SUPERSEDES`, `SUPPORTS`, `CONTRADICTS`, and
   `DERIVED_FROM` are memory lineage relations, not a general graph interface.
5. Memory remains logically independent of the Graph surface. Graphs, vectors,
   and hybrid indexes may implement or optimize Recall without changing its
   contract.
6. The first gate implements only deterministic `Recall` and an immutable
   in-memory reference adapter. Extraction, ranking models, write proposals,
   mutation application, and physical storage optimization are deferred.

When a time is omitted, Recall evaluates at the selected snapshot's reference
time. A `known_at` value later than that snapshot is invalid because the
snapshot cannot expose knowledge it does not contain.

## Consequences

- The same snapshot can answer both "what was true at time V?" and "what did
  the system know at time T?" without overwriting corrections.
- Conflicting current assertions remain visible and explicit.
- Adapter contract tests can evaluate temporal correctness and scope leakage
  independently of retrieval quality.
- Storage and query implementations carry more temporal metadata than a flat
  vector store. This cost is accepted at the logical layer; later adapters may
  optimize it without weakening the semantics.
- Runtime memory writes and cross-scope propagation require a separate ADR
  before `ProposeMutation` and `ApplyMutation` are implemented.
