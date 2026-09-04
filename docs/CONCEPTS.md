# DataMind concepts and terminology

DataMind is an **inference-time data plane**: data can be added, organized,
recalled, and combined while an agent is answering a request. It is not a new
model-training method and it is not a replacement for a warehouse or an ETL
orchestrator. The goal is to make the data available at inference time with
explicit write/read authority, provenance, and operational boundaries.

## The short version

```text
incoming data ──▶ StoreAgent ──write──▶ five data surfaces
                                         │
question ───────▶ RetrieveAgent ◀─read───┘ ──▶ evidence-backed answer
```

The five surfaces are:

1. **Knowledge base (KB / RAG):** chunked documents and hybrid retrieval.
2. **Database (DB):** structured rows queried through read-only SQL tools.
3. **Graph:** entities and relations for traversals and neighborhood queries.
4. **Skills:** reusable procedures and safe utility tools.
5. **Memory:** explicit long-term facts and preferences with scope and status.

## Core terms

| Term | Meaning | What it is not |
|---|---|---|
| **Inference-time data** | Data available to an agent during a live inference turn, whether persisted earlier or written moments ago. | Model weights, fine-tuning data, or a claim that every write is durable forever. |
| **Data plane** | The five surfaces plus the read/write paths that move data through them. | A planner, workflow DAG, or control-plane scheduler. |
| **StoreAgent** | The write-authorized agent. It chooses among write tools, can write to multiple surfaces, and returns auditable receipts. | A deterministic ETL job or a general-purpose shell agent. |
| **RetrieveAgent** | The read-authorized agent. It selects the minimum useful read/utility tools, combines results, and emits evidence. | A second unrestricted agent with access to writes. |
| **Surface** | One typed persistence/retrieval boundary: KB, DB, graph, Skills, or Memory. | A generic “vector store” label for every kind of data. |
| **Receipt** | A StoreAgent write result containing profile, revision, source identity, per-surface status, and idempotence information. | A guarantee that an external downstream system has committed a transaction. |
| **Evidence** | A normalized read result with a surface, source/locator, content, and optional score. | A citation-quality claim that is stronger than the underlying source. |
| **Profile** | A filesystem and storage namespace selected by `DATAMIND__DATA__PROFILE`. | Authentication or tenant isolation by itself. |
| **Session** | A caller-provided conversation identity used for short-term context and session-scoped memory. | A server-side user account or an authorization token. |
| **HookChain** | Shared pre/post policy hooks for path allow-listing, destructive SQL confirmation, and audit logging. | A substitute for network authentication, TLS, or a full policy engine. |

## How this differs from traditional RAG

Traditional RAG usually means “embed documents, retrieve top-k chunks, add
them to a prompt.” DataMind keeps that path, but treats it as one surface among
five:

| Dimension | Traditional RAG | DataMind inference-time data plane |
|---|---|---|
| Data shape | Mostly unstructured documents | Documents, rows, graph edges, procedures, and scoped memory |
| Write timing | Commonly an offline indexing pipeline | StoreAgent can ingest during an interactive session and return a receipt |
| Query path | Vector/keyword retrieval | RetrieveAgent chooses KB, DB, graph, Skills, and Memory together |
| Authority | Often one application service owns both paths | StoreAgent writes; RetrieveAgent is code-enforced read-only |
| Provenance | Retrieved chunk metadata varies by implementation | Evidence has a normalized surface/source/locator shape |

DataMind does not claim that multi-surface retrieval is automatically better.
It makes the choice explicit so a question about a total, relationship, or
procedure can use the appropriate representation instead of forcing every
answer through a text chunk.

## How this differs from ETL

ETL is a scheduled or triggered data-engineering process: extract from systems,
transform deterministically, and load into a target. DataMind's StoreAgent is a
conversation-facing ingestion router:

- It accepts a user instruction or an uploaded source at inference time.
- It chooses a typed write tool (for example, `db_import_csv` or
  `graph_add_triples_from_text`).
- It records a receipt and deduplicates repeated writes where the tool supports
  a stable source identity.
- It does not replace schema governance, batch quality checks, warehouse
  lineage, backfills, or scheduled orchestration.

ETL can feed DataMind's surfaces; DataMind can also be the last-mile interface
for small, explicit writes that do not justify a new pipeline.

## How this differs from Agent Memory

“Agent memory” often means hidden conversational state or a vector of previous
turns. DataMind Memory is narrower and explicit:

- Every item has a `scope` (`global`, `profile`, or `session`), `kind`, and
  `status` (`active` or `archived`).
- StoreAgent writes memory through `memory_save` / `memory_forget`; RetrieveAgent
  reads through `memory_recall` / `memory_list_profiles`.
- Memory is one of the five surfaces, not the entire data plane.
- Memory recall is evidence in the same result contract as KB, DB, and graph
  reads; it should not silently override authoritative source data.

Use Memory for durable preferences, decisions, workflows, and facts. Use the DB
or KB for authoritative business records and source documents.

## Common boundary mistakes

- Calling a DB import an ETL platform: it is a bounded StoreAgent write tool,
  not scheduling, lineage, or transformation governance.
- Calling the KB “the memory”: KB retrieval and scoped Memory have different
  lifecycles and authority expectations.
- Giving RetrieveAgent a write tool “just in case”: the role boundary is a
  safety property, not merely a prompt convention.
- Treating a profile as security: profiles organize data paths; public
  deployments still need authentication, authorization, TLS, rate limits, and
  network isolation.
- Treating an evidence object as proof: inspect its source and locator and
  preserve the underlying system's access policy.

## Naming guidance for announcements

Use **“inference-time data plane”** when describing the overall concept.
Use **“StoreAgent”** for the write/ingest role and **“RetrieveAgent”** for the
read/evidence role. Say **“five data surfaces”** instead of “one universal
memory.” A precise one-line description is:

> DataMind lets agents write and retrieve across documents, databases, graphs,
> Skills, and scoped Memory at inference time, while keeping write authority,
> read authority, receipts, and evidence explicit.
