# DataMind

<p align="center">
  <strong>Inference-time data, built for agents.</strong><br>
  Store knowledge while you work. Retrieve evidence when you need it.
</p>

<p align="center">
  <a href="https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml"><img src="https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/datamind/"><img src="https://img.shields.io/pypi/v/datamind.svg" alt="PyPI version"></a>
  <a href="https://pypi.org/project/datamind/"><img src="https://img.shields.io/pypi/pyversions/datamind.svg" alt="Python versions"></a>
  <a href="https://github.com/OpenDCAI/DataMind/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/datamind.svg" alt="License"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="./GETTING_STARTED.md">Getting started</a> ·
  <a href="./docs/STABLE_API.md">Stable API</a> ·
  <a href="./README_zh.md">中文</a>
</p>

DataMind is a small, protocol-neutral data plane for agent applications. Two
cooperating agents keep the contract simple:

- **StoreAgent** decides where new information belongs and writes it at
  inference time.
- **RetrieveAgent** searches across five data surfaces, combines evidence, and
  stays strictly read-only.

The result is not another chat history or a batch ETL job. It is a shared,
inspectable layer of knowledge that can change during a conversation and be
used immediately by the next question.

![DataMind inference-time data plane](./assets/inference-time-data-plane.png)

<p align="center"><sub>Warm paths write through StoreAgent; cool paths retrieve evidence through RetrieveAgent.</sub></p>

> **v1.0.0 is the first stable release.** The native backend with local profile
> storage is the release baseline. SDK/CCR and remote database integrations are
> supported integration paths and should be validated in the target environment.

## Why DataMind?

| Write at inference time | Retrieve across surfaces | Safe by construction |
|---|---|---|
| Turn a message, file, CSV, or triple into durable data without a separate ingestion service. | Ask one question across RAG, SQL, graph, Skills, and Memory; get a single answer with provenance. | Store and Retrieve use separate tool registries, shared safety hooks, scoped profiles, and auditable receipts. |

### The two-agent contract

| Agent | Owns | Never does |
|---|---|---|
| **StoreAgent** | `kb_add_*`, `db_import_*`, graph upserts, `skill_upsert`, `memory_save` / `memory_forget` | Answer retrieval questions with write privileges |
| **RetrieveAgent** | KB search, SQL inspection/querying, graph traversal, Skills, Memory recall | Mutate a data surface |

The boundary is enforced in code before tools reach the model: RetrieveAgent
gets 19 read/utility tools, StoreAgent gets 11 write tools. Every call also
passes through the shared `PathAllowlistHook`, `DestructiveSqlHook`, and
`AuditLogHook` chain.

## Five surfaces, one answer

| Surface | What it is good at | Default implementation |
|---|---|---|
| **KB / RAG** | Documents, notes, policies, semantic search | Chroma + BM25 with reciprocal-rank fusion |
| **Database** | Exact numbers, filters, joins, aggregations | SQLAlchemy over SQLite / MySQL / PostgreSQL |
| **Knowledge graph** | Entities, relationships, multi-hop facts | NetworkX with JSON persistence |
| **Skills** | Reusable procedures and safe utilities | Profile-scoped `SKILL.md` plus Python tools |
| **Memory** | Preferences and durable facts | SQLite with cosine recall and `global` / `profile` / `session` scopes |

## Quick start

Install the stable native stack:

```bash
pip install datamind
```

Point it at an Anthropic or OpenAI Chat Completions-compatible gateway:

```bash
export DATAMIND__LLM__API_BASE=https://your-gateway.example.com
export DATAMIND__LLM__API_KEY=sk-...
export DATAMIND__LLM__PROTOCOL=anthropic   # or openai_chat_completions
export DATAMIND__LLM__MODEL=claude-sonnet-4-6

datamind chat
```

Or start the local browser UI:

```bash
python -m uvicorn datamind.server:app --port 8000
# open http://127.0.0.1:8000
```

The protocol is explicit and shared by the outer agent loop and internal
generation paths (NL2SQL, multi-query retrieval, Memory, and graph extraction).
Model names never select a protocol implicitly. See [ADR 0001](./docs/adr/0001-protocol-neutral-model-clients.md).

### Optional providers

```bash
pip install 'datamind[mysql]'       # MySQL dialect
pip install 'datamind[postgres]'    # PostgreSQL dialect
pip install 'datamind[voyage]'      # Voyage embeddings
pip install 'datamind[huggingface]' # local BGE / e5 embeddings
pip install 'datamind[dev]'         # pytest + build + twine
```

## See it in action

The repository includes a realistic enterprise demo with 17 documents, 64
graph nodes, 6 tables, and 101 rows:

```bash
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
```

Want the UI instead?

```bash
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m uvicorn datamind.server:app --port 8000
```

Drag `.md`, `.csv`, or `.txt` files into the dropzone, ask a question, and
watch the role-scoped tools fire. The full walkthrough is in
[GETTING_STARTED.md](./GETTING_STARTED.md).

## A conversation can change the data plane

```bash
datamind store "Import /Users/foo/sales-q2.csv as table q2_sales"
datamind store "Remember that weekly reports default to Chinese"
datamind chat
```

The next retrieval can use the new data immediately:

```text
you            → "Which sales rep has the largest Q2 pipeline?"
RetrieveAgent  → db_query_sql(...) → answer + source table evidence
```

The same pattern works for documents, graph facts, and profile-scoped skills.
StoreAgent returns a write receipt; RetrieveAgent returns normalized evidence.

## Choose a runtime

The stable default is the built-in `native` loop. The optional `sdk` loop is
useful when you need Claude Agent SDK features such as Subagents or Compaction.

| Backend | Protocol | Status | Use when |
|---|---|---|---|
| `native` | Anthropic `/v1/messages` | **Stable** | You want the smallest supported path. |
| `native` | OpenAI `/v1/chat/completions` | **Stable** | Your gateway is OpenAI-compatible. |
| `sdk` | Anthropic | Integration | You already run Claude Agent SDK / CLI. |
| `sdk` | OpenAI-compatible via CCR | Integration | You need the SDK loop behind an OpenAI-format gateway. |

Set both switches explicitly:

```bash
DATAMIND__AGENT__BACKEND=native
DATAMIND__LLM__PROTOCOL=anthropic
```

See the complete [native / SDK support matrix](./docs/SUPPORT_MATRIX.md). For
the SDK + OpenAI-compatible path, [CCR](https://github.com/musistudio/claude-code-router)
is a local Anthropic ↔ OpenAI protocol bridge:

<details>
<summary>SDK + CCR setup</summary>

```bash
npm install -g @musistudio/claude-code-router   # Node >= 18

UPSTREAM_BASE=https://your-openai-gateway.example.com/v1 \
UPSTREAM_KEY=sk-your-openai-format-key \
UPSTREAM_MODEL=claude-sonnet-4-6 \
  ./scripts/start_ccr.sh

DATAMIND__AGENT__BACKEND=sdk
DATAMIND__AGENT__CCR_BASE_URL=http://127.0.0.1:13456
DATAMIND__AGENT__CCR_API_KEY=dummy
```

CCR holds the real upstream key. Do not put it in a browser-facing environment
or commit generated CCR configuration.
</details>

## Python and HTTP APIs

The preferred Python facade is small and async:

```python
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
```

The bundled FastAPI server exposes:

| Route | Purpose |
|---|---|
| `GET /api/health` | Liveness, active profile, protocol, and tool counts |
| `GET /api/tools` | Role-scoped tool catalogue and schemas |
| `POST /api/ask` | Read-only RetrieveAgent request |
| `POST /api/store` | StoreAgent request with receipts |
| `POST /api/chat` | Real SSE stream (`text`, `tool_use`, `tool_result`, `done`) |
| `POST /api/upload` | Save an upload and return suggested store prompts |

The stable result shapes and compatibility policy live in
[Stable API](./docs/STABLE_API.md).

## Safety and deployment boundary

DataMind is designed to be embedded behind your own authentication and
authorization layer. The public server does not replace one. Before exposing
it to the internet, read [Public deployment security boundaries](./docs/SECURITY_BOUNDARIES.md):

- bind to loopback for local use;
- put authentication, authorization, rate limits, and TLS at the edge;
- isolate profile/storage directories and restrict upload paths;
- treat evidence provenance as useful metadata, not as an authorization grant.

## Development

```bash
pytest
python -m datamind.scripts.verify_sqlite_demo
```

The first command runs the no-network unit and contract suite. The second is a
one-command SQLite smoke test. Long-running evaluations, checkpoint/resume,
and benchmark details are in [docs/BENCHMARK_RUNNER.md](./docs/BENCHMARK_RUNNER.md).

## Project map

<details>
<summary>Show repository layout</summary>

```text
DataMind/
├── datamind/                 # current v1.x package
│   ├── agent/                # StoreAgent / RetrieveAgent loops
│   ├── capabilities/         # kb / graph / db / skills / memory / ingest
│   ├── core/                 # protocols, registries, hooks, contracts
│   ├── scripts/               # hello_*.py and verification demos
│   ├── cli.py                 # datamind CLI
│   └── server.py              # FastAPI + SSE + upload API
├── docs/                     # stable API, support, security, concepts
├── assets/                   # README architecture graphic
├── demo-uploads/              # browser drag-and-drop examples
├── benchmark/                # checkpoint/resume runner
├── data/profiles/<profile>/   # profile-scoped inputs
├── storage/<profile>/         # profile-scoped indexes and databases
├── modules/ core/ main.py    # v0.1 prototype kept for comparison
├── pyproject.toml             # package and CLI metadata
└── LICENSE                    # Apache-2.0
```

The original v0.1 paths remain in-tree for comparison; the supported v1.x
facade lives under `datamind/`.
</details>

## Documentation and release notes

- [Getting started](./GETTING_STARTED.md)
- [Stable API](./docs/STABLE_API.md)
- [Native / SDK support matrix](./docs/SUPPORT_MATRIX.md)
- [Security boundaries](./docs/SECURITY_BOUNDARIES.md)
- [Concepts and terminology](./docs/CONCEPTS.md) — how inference-time data differs from RAG, ETL, and Agent Memory
- [CHANGELOG](./CHANGELOG.md)
- [DataMind-Doc](https://opendcai.github.io/DataMind-Doc/en/)

## License

DataMind is released under the [Apache License 2.0](./LICENSE).
