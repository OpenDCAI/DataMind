# ADR-0002: Governed, bi-temporal Memory mutation

- Status: Accepted
- Date: 2026-07-28

## Context

ADR-0001 established immutable semantic records, explicit scopes, valid time,
transaction time, and deterministic Recall. Runtime writes must preserve those
properties without allowing model-generated content to acquire storage
authority.

Generic CRUD is insufficient for this boundary. `UPDATE` hides whether a fact
was corrected or merely edited, while `DELETE` conflates logical retraction
with irreversible physical erasure. Automatic text merging also makes a
deterministic Core responsible for an uncertain semantic judgment.

## Decision

1. Runtime Memory writes use three semantic changes:
   - `ASSERT` appends an independent assertion.
   - `SUPERSEDE` closes one current record and appends a replacement linked to
     it.
   - `RETRACT` closes one current record without erasing history.
   Physical `PURGE` is excluded and, if introduced, will be a separately
   approved `DESTRUCTIVE` operation.
2. Writes are two-phase. `ProposeMutation` is a `READ` DataOp that validates an
   untrusted `MemoryMutationDraft` and binds it to one source snapshot.
   `ApplyMutation` is an `INTERNAL_WRITE` DataOp that accepts only a validated
   proposal. `Engine.apply()` is a thin wrapper over normal DataOp execution.
3. One proposal contains one or more changes for exactly one source and one
   explicit scope. The reference adapter applies the batch atomically. It does
   not promise cross-source transactions.
4. The execution context, not memory content, binds the origin channel:
   `USER_EXPLICIT`, `AGENT_INFERRED`, `TOOL_DERIVED`, or
   `POLICY_COMPACTION`. Imported seed records use `IMPORTED`.
5. Inferred, tool-derived, and compaction writes require a policy approval key.
   All workspace and organization writes require one, including explicit user
   requests. Approval may be issued automatically by a deterministic policy;
   it is not necessarily a human interaction.
6. Every draft has an idempotency key. Proposal IDs and created record IDs are
   deterministic functions of the source, key, and change position. Reusing a
   key for different intent is rejected; retrying the same applied proposal
   returns its prior receipt.
7. Proposals use optimistic snapshot concurrency. If the source changes after
   proposal validation, the entire apply is rejected and must be proposed
   again.
8. Commit time is source-generated and monotonically advances transaction
   time. A caller may declare valid time but cannot declare transaction time.
   `SUPERSEDE` and `RETRACT` close transaction-time intervals; they never
   rewrite historical snapshots.
9. The Core does not infer duplicate or contradictory text. Superseding always
   names a target. Declared conflicts may coexist and remain visible to Recall.
10. Runtime mutation and links cannot cross scope boundaries. Promotion or
    sharing between scopes requires a future policy and ADR.

The protected replay artifact may retain full proposals and results. Public
trace events contain only operation metadata, fingerprints, source identity,
origin channel, and hashed scope/idempotency identities.

## Consequences

- An LLM or MemoryPolicy can suggest state changes but cannot bypass type,
  scope, effect, approval, snapshot, or idempotency checks.
- Corrections and withdrawals remain answerable as historical belief queries.
- Conservative concurrency may reject proposals after unrelated writes to the
  same source. Record-level conflict detection is a future optimization, not a
  first-version correctness requirement.
- Retraction reasons and supporting evidence remain in the protected proposal
  and replay record; logical state contains the closed record rather than an
  invented replacement fact.
- Semantic consolidation, deduplication, and learned mutation selection remain
  Intelligence-layer research problems that compile to this deterministic
  substrate.
