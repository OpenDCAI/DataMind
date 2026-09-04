# DataMind

<p align="center">
  <strong>Store at inference time. Retrieve with evidence.</strong><br>
  A shared data plane for agents — writable during the conversation, useful on the very next question.
</p>

<p align="center">
  <a href="https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml"><img src="https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/datamind/"><img src="https://img.shields.io/pypi/v/datamind.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/datamind/"><img src="https://img.shields.io/pypi/pyversions/datamind.svg" alt="Python"></a>
  <a href="https://github.com/OpenDCAI/DataMind/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/datamind.svg" alt="Apache-2.0"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="./GETTING_STARTED.md">Tutorial</a> ·
  <a href="./docs/STABLE_API.md">API</a> ·
  <a href="./README_zh.md">中文</a>
</p>

<p align="center">
  <img src="./assets/inference-time-data-plane.png" alt="DataMind inference-time data plane" width="100%">
</p>

<p align="center"><sub>StoreAgent writes on the warm path. RetrieveAgent reads across the shared data plane and returns evidence.</sub></p>

> **v1.0.0** — stable native backend + local profile storage. SDK/CCR and remote database adapters are supported integration paths; validate them in your own environment.

## The idea

Most agent systems can retrieve knowledge, but they have nowhere to put the new fact they just learned. DataMind gives the runtime two explicit roles:

~~~text
message / file / CSV / relationship
                 │
                 ▼
           StoreAgent  ─────── write receipt ───────▶  KB · DB · Graph · Skills · Memory
                 │
                 │  next question
                 ▼
           RetrieveAgent  ◀──── evidence + answer ────  shared data plane
~~~

This is **inference-time data**: not model training data, not a batch ETL pipeline,
and not an unbounded chat transcript. It is scoped, inspectable state that can
change while an agent is running.

## Two agents. One hard boundary.

<table>
<tr>
<td valign="top" width="50%">

### StoreAgent

Chooses a destination and writes:

- documents and chunks
- rows and tables
- graph triples
- profile skills
- durable memories

Returns a receipt describing what changed.

</td>
<td valign="top" width="50%">

### RetrieveAgent

Chooses sources and reads:

- semantic KB search
- SQL inspection and queries
- graph traversal
- Skills and safe utilities
- scoped Memory recall

Returns an answer with normalized evidence.

</td>
</tr>
</table>

The split is enforced in code, before tools reach the model. RetrieveAgent sees
19 read/utility tools; StoreAgent sees 11 write tools. Every call also passes
through `PathAllowlistHook`, `DestructiveSqlHook`, and `AuditLogHook`.

## Five surfaces, one answer

- **KB / RAG** — documents, notes, policies, semantic search
- **Database** — exact numbers, filters, joins, aggregations
- **Knowledge Graph** — entities, relationships, multi-hop facts
- **Skills** — reusable procedures and safe utilities
- **Memory** — preferences and durable facts, scoped to `global`, `profile`, or `session`

Default providers are Chroma + BM25, SQLAlchemy (SQLite / MySQL / PostgreSQL),
NetworkX, profile-scoped `SKILL.md`, and SQLite memory.

## Quick start

~~~bash
pip install datamind

export DATAMIND__LLM__API_BASE=https://your-gateway.example.com
export DATAMIND__LLM__API_KEY=sk-...
export DATAMIND__LLM__PROTOCOL=anthropic   # or openai_chat_completions
export DATAMIND__LLM__MODEL=claude-sonnet-4-6

datamind chat
~~~

Or open the local UI:

~~~bash
python -m uvicorn datamind.server:app --port 8000
# http://127.0.0.1:8000
~~~

The protocol is explicit and shared by the outer loop and internal generation
(NL2SQL, multi-query retrieval, Memory, and graph extraction).

<details>
<summary>Optional providers and extras</summary>

~~~bash
pip install 'datamind[mysql]'
pip install 'datamind[postgres]'
pip install 'datamind[voyage]'
pip install 'datamind[huggingface]'
pip install 'datamind[dev]'
~~~
</details>

## A 60-second end-to-end demo

~~~bash
git clone https://github.com/OpenDCAI/DataMind.git
cd DataMind
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.datamind.example .env.datamind
$EDITOR .env.datamind              # set DATAMIND__LLM__API_KEY

python -m datamind.scripts.hello_sdk
python -m datamind.scripts.seed_enterprise_demo
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m datamind.scripts.hello_enterprise
~~~

The bundled dataset contains 17 documents, 64 graph nodes, 6 tables, and 101
rows. To use the browser UI, run:

~~~bash
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m uvicorn datamind.server:app --port 8000
~~~

Drop in `.md`, `.csv`, or `.txt`, ask a question, and watch the role-scoped
tools work. The full walkthrough is in [GETTING_STARTED.md](./GETTING_STARTED.md).

## Data can change during the conversation

~~~text
you            → "Import sales-q2.csv as table q2_sales"
StoreAgent     → db_import_csv(...) → write receipt
you            → "Which sales rep has the largest Q2 pipeline?"
RetrieveAgent  → db_query_sql(...)  → answer + table evidence
~~~

The same flow works for a document, a graph fact, or a profile skill.

## Choose your runtime

The built-in `native` loop is the stable default. The optional `sdk` loop adds
Claude Agent SDK features such as Subagents and Compaction.

| Backend | Protocol | Status |
|---|---|---|
| `native` | Anthropic `/v1/messages` | **Stable** |
| `native` | OpenAI `/v1/chat/completions` | **Stable** |
| `sdk` | Anthropic | Integration |
| `sdk` | OpenAI-compatible via CCR | Integration |

Set both switches explicitly:

~~~bash
DATAMIND__AGENT__BACKEND=native
DATAMIND__LLM__PROTOCOL=anthropic
~~~

Read the complete [native / SDK support matrix](./docs/SUPPORT_MATRIX.md). For
SDK + OpenAI-compatible gateways, [CCR](https://github.com/musistudio/claude-code-router)
is the local Anthropic ↔ OpenAI protocol bridge.

## Python and HTTP APIs

~~~python
from datamind.agent import build_datamind
from datamind.config import Settings

async def answer() -> str:
    system = await build_datamind(Settings())
    try:
        await system.ingest("Remember that weekly reports use Chinese.")
        result = await system.query("What language should weekly reports use?")
        return result["answer"]
    finally:
        await system.aclose()
~~~

The bundled FastAPI server exposes `GET /api/health`, `GET /api/tools`,
`POST /api/ask`, `POST /api/store`, `POST /api/chat` (SSE), and
`POST /api/upload`. See the [stable API contract](./docs/STABLE_API.md).

## Safe to embed, not safe to expose naked

DataMind expects your authentication and authorization layer at the edge. Before
deploying publicly:

- bind local deployments to loopback;
- add authentication, authorization, TLS, and rate limits;
- isolate profile/storage directories and upload paths;
- treat evidence provenance as metadata, never as a permission grant.

See [public deployment security boundaries](./docs/SECURITY_BOUNDARIES.md).

## Verify locally

~~~bash
pytest
python -m datamind.scripts.verify_sqlite_demo
~~~

The repository's CI runs the no-network test suite and the deterministic SQLite
demo. Benchmark and checkpoint/resume details live in
[docs/BENCHMARK_RUNNER.md](./docs/BENCHMARK_RUNNER.md).

## Explore the docs

<table>
<tr>
<td valign="top" width="50%">

**Build with it**

- [Getting started](./GETTING_STARTED.md)
- [Stable API](./docs/STABLE_API.md)
- [Native / SDK matrix](./docs/SUPPORT_MATRIX.md)

</td>
<td valign="top" width="50%">

**Understand it**

- [Concepts and terminology](./docs/CONCEPTS.md)
- [Security boundaries](./docs/SECURITY_BOUNDARIES.md)
- [CHANGELOG](./CHANGELOG.md)

</td>
</tr>
</table>

More architecture notes and tutorials are available in
[DataMind-Doc](https://opendcai.github.io/DataMind-Doc/en/). The supported v1.x
package lives under `datamind/`; the original v0.1 prototype remains in-tree
for comparison.

## License

DataMind is released under the [Apache License 2.0](./LICENSE).
