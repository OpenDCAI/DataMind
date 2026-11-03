# DataMind concepts and terminology

DataMind is a workspace data layer for a main Codex agent. Local Codex serves
personal use; the official Codex SDK hosts the same agent for applications.
DataMind handles storage, retrieval, validated writes, evidence and versions.
Codex handles planning, extraction, SQL generation and answer synthesis.

```text
User request → Codex → DataMind tools → KB / DB / Graph / Skills / Memory
                         │
                         └→ receipts, evidence, workspace revision → Codex answer
```

## Five surfaces

| Surface | Stores | Typical use |
|---|---|---|
| KB | Document chunks and retrieval metadata | Retrieve source passages |
| DB | Structured records | Exact filters, joins, totals |
| Graph | Entities and source-backed relationships | Neighborhoods, paths, dependencies |
| Skills | Versioned procedures | Guide ingestion and retrieval |
| Memory | Explicit scoped facts/preferences | Recall durable preferences |

Graph relation strings are extensible. Domain vocabularies and source evidence
make them useful; graph triples are not substitutes for entire documents or
large numeric tables. Skills guide decisions, while tool schemas and hooks
validate execution.

## Authority and provenance

`read` and `write` describe an operation's permissions, not separate agents.
Read operations reject writes in code. Business writes return durable receipts
with per-surface status and source identity. Build metadata tools return build
state instead. A receipt records an observed operation; inspect failures and
do not treat its existence as a successful distributed transaction.

Evidence identifies the surface, source/locator and returned content. It is
provenance, not proof that a model's answer is correct. A profile isolates local
storage paths; a session scopes conversation and memory. Neither authenticates
a user. Hooks enforce path and SQL rules and record audits; applications still
own identity, authorization and quotas.

## Updates and versions

Independent live writes update selected stores. Coordinated KB/DB/Graph builds
prepare source hashes, reuse unchanged work, validate artifacts, and publish one
manifest/head. Read requests pin the published revision before tool execution,
so a publication between tools does not change their selected snapshot. Failed
builds leave the previous head readable. Historical revisions remain read-only.

Memory and Skills are separate live surfaces unless a specific snapshot
contract includes them; do not claim all five surfaces are one transaction.
The coordinator is local and single-host. See [the lifecycle guide](KNOWLEDGE_LIFECYCLE.md)
and [public API](STABLE_API.md) for concrete operations.
