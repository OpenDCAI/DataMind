# DataMind

**English** | [中文](./README_zh.md)

[![PyPI version](https://img.shields.io/pypi/v/datamind.svg)](https://pypi.org/project/datamind/)
[![Python](https://img.shields.io/pypi/pyversions/datamind.svg)](https://pypi.org/project/datamind/)
[![License](https://img.shields.io/pypi/l/datamind.svg)](https://github.com/OpenDCAI/DataMind/blob/main/LICENSE)

An inference-time data system with two cooperating agents. **StoreAgent** routes writes across five data surfaces; **RetrieveAgent** performs strictly read-only cross-surface retrieval and evidence synthesis. The five surfaces are RAG, database, graph, Skills, and Memory. There is no DataOp, DataPlan, or execution DAG.

> **v0.3.0 is a preview release on PyPI.** The current codebase lives under [`datamind/`](./datamind/); the original v0.1 prototype (`main.py` / `server.py` / `modules/`) is kept in-tree for comparison only. End-to-end walkthrough: [`GETTING_STARTED.md`](./GETTING_STARTED.md) · [docs site](https://opendcai.github.io/DataMind-Doc/en/).

---

## Install

```bash
pip install datamind
```

Optional extras:

```bash
pip install 'datamind[mysql]'         # MySQL dialect
pip install 'datamind[postgres]'      # PostgreSQL dialect
pip install 'datamind[voyage]'        # Voyage embeddings
pip install 'datamind[huggingface]'   # Local BGE / e5 embeddings
pip install 'datamind[dev]'           # pytest + build + twine
```

Point it at an Anthropic or OpenAI Chat Completions compatible gateway and start chatting:

```bash
export DATAMIND__LLM__API_BASE=https://your-gateway.example.com
export DATAMIND__LLM__API_KEY=sk-ant-...
export DATAMIND__LLM__PROTOCOL=anthropic  # or openai_chat_completions
export DATAMIND__LLM__MODEL=claude-sonnet-4-6

datamind chat                                          # CLI
python -m uvicorn datamind.server:app --port 8000      # browser UI on http://127.0.0.1:8000
```

The selected protocol is shared by the outer tool loop and internal generation
(NL2SQL, multi-query retrieval, Memory, and Graph ingest). Model names do not
implicitly select a protocol. See [ADR 0001](./docs/adr/0001-protocol-neutral-model-clients.md).

---

## Capabilities

| Data surface | Backend | RetrieveAgent | StoreAgent |
|---|---|---|---|
| **KB (RAG)** | Chroma + BM25 with Reciprocal Rank Fusion | `kb_search`, `kb_list_documents`, `kb_count` | `kb_add_text`, `kb_add_file`, `kb_add_path`, `kb_reindex` |
| **Graph** | NetworkX, JSON-persisted | `graph_search_entities`, `graph_traverse`, `graph_neighbors` | `graph_upsert_triples`, `graph_add_triples_from_text` |
| **Database** | SQLAlchemy (SQLite / MySQL / Postgres) | `db_list_tables`, `db_describe_table`, `db_query_sql`, `db_query_nl` | `db_import_csv`, `db_import_records` |
| **Skills** | base + profile-scoped `SKILL.md`, plus safe Python tools | `skill_search`, `skill_get`, `skill_list`, `calculator`, `unit_convert`, `get_current_time`, `analyze_text` | `skill_upsert` |
| **Memory** | SQLite with cosine recall; scope-typed (`global` / `profile` / `session`) | `memory_recall`, `memory_list_profiles` | `memory_save`, `memory_forget` |

The runtime uses two physically separate registries: 19 read/utility tools for RetrieveAgent and 11 write tools for StoreAgent. Code enforces the boundary before tools reach the model.

Every tool call still passes through the shared safety HookChain: `PathAllowlistHook`, `DestructiveSqlHook`, and `AuditLogHook`.

---

## 60-second demo

> **Just want to use it?** `pip install datamind`, set `DATAMIND__LLM__API_KEY`, run `datamind chat`.
> The walkthrough below clones the repo so you also get the seed scripts and the enterprise-demo dataset.

```bash
git clone https://github.com/OpenDCAI/DataMind.git && cd DataMind
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.datamind.example .env.datamind
$EDITOR .env.datamind     # set DATAMIND__LLM__API_KEY at minimum

# 1. Smoke-test the gateway (~2 s)
python -m datamind.scripts.hello_sdk

# 2. Seed a realistic enterprise dataset (17 docs / 64 graph nodes / 6 tables / 101 rows)
python -m datamind.scripts.seed_enterprise_demo

# 3. Watch the agent answer 8 cross-backend questions on its own
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m datamind.scripts.hello_enterprise

# 4. Or just open the browser UI
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m uvicorn datamind.server:app --port 8000
# → http://127.0.0.1:8000  — drag any .md / .csv / .txt into the dropzone, ask questions, watch tools fire
```

More detail in [`GETTING_STARTED.md`](./GETTING_STARTED.md). The generic
long-running evaluation interface, checkpoint format, and resume guarantees are
documented in [`Benchmark runner`](./docs/BENCHMARK_RUNNER.md).

---

## What "agentic" actually means here

Ask: **"工程部 Shanghai 的员工工资加起来是多少？"**

RetrieveAgent can recover from a failed SQL attempt by inspecting the schema and rewriting the query, combine Graph with KB/Skills, and return evidence. "Remember this for me" is handled by StoreAgent through `memory_save`; RetrieveAgent never receives that write tool.

Both agents support the same two interchangeable loop backends and share the SSE protocol, services, and safety HookChain:

```
DATAMIND__AGENT__BACKEND=native   # default — built-in protocol-neutral loop
DATAMIND__LLM__PROTOCOL=anthropic # or openai_chat_completions
DATAMIND__AGENT__BACKEND=sdk      # claude-agent-sdk + claude-code-router (CCR)
                                  # use this to sit on an OpenAI-format gateway
                                  # (CCR translates); adds Subagents / Compaction
```

DataMind's `HookChain` (path allow-list, destructive-SQL gate, tamper-evident audit) is enforced on **both** backends — at the dispatch chokepoint on `native`, inside each MCP tool wrapper on `sdk`. Both verified end-to-end against the same 8 enterprise-demo questions ([numbers here](./GETTING_STARTED.md#10-bench)).

---

## Protocol support and the optional CCR bridge

The native loop directly supports Anthropic `/v1/messages` and OpenAI
`/v1/chat/completions`, including tool calls and streaming. Set
`DATAMIND__LLM__PROTOCOL` explicitly; the same client is injected into internal
generation paths.

CCR remains useful when choosing the Claude Agent SDK backend, because that SDK
speaks Anthropic protocol while many gateways expose OpenAI Chat Completions:

**[claude-code-router (CCR)](https://github.com/musistudio/claude-code-router)** — a
local proxy that accepts Anthropic `/v1/messages` requests and forwards them to an
OpenAI-format upstream, translating the payloads (and the streaming events) in both
directions.

```
DataMind ──Anthropic /v1/messages──▶  CCR (localhost)  ──OpenAI /v1/chat/completions──▶  your gateway
   (sdk backend)                     translates both ways                                (OpenAI-format key)
```

So DataMind never changes: it always thinks it's talking to Anthropic. CCR absorbs
the format mismatch. This is exactly what the `sdk` agent backend is wired for.

### When do I need it?

| Your upstream gateway speaks… | What to do |
|---|---|
| **Anthropic** (`/v1/messages`, `sk-ant` key) | Nothing. Use `BACKEND=native`, point `DATAMIND__LLM__API_BASE` straight at it. |
| **OpenAI** (`/v1/chat/completions`) | Use `BACKEND=native` with `LLM__PROTOCOL=openai_chat_completions`; use CCR only with `BACKEND=sdk`. |

### Setup (OpenAI-format upstream)

```bash
# 1. Install CCR (Node ≥ 18)
npm install -g @musistudio/claude-code-router
#    …or clone https://github.com/musistudio/claude-code-router and build it.

# 2. Launch the local bridge. It writes a config that registers your
#    OpenAI-format upstream and applies the `anthropic` transformer.
UPSTREAM_BASE=https://your-openai-gateway.example.com/v1 \
UPSTREAM_KEY=sk-your-openai-format-key \
UPSTREAM_MODEL=claude-sonnet-4-6 \
  ./scripts/start_ccr.sh
# → [ccr] listen = http://127.0.0.1:13456

# 3. Point DataMind's sdk backend at CCR (in .env.datamind):
DATAMIND__AGENT__BACKEND=sdk
DATAMIND__AGENT__CCR_BASE_URL=http://127.0.0.1:13456
DATAMIND__AGENT__CCR_API_KEY=dummy       # CCR holds the real key; this is unused
```

`scripts/start_ccr.sh` generates CCR's `config.json` for you, normalises the upstream
URL to `/v1/chat/completions`, and maps the `default` / `background` / `think` routes
onto your primary and fallback models. Override `CCR_PORT`, `UPSTREAM_FALLBACK`, or
`CCR_SERVER_ENTRY` (path to CCR's `packages/server/dist/index.js`) via env vars — see
the header comment in that script.

---

## Add data by talking

StoreAgent owns every write tool while RetrieveAgent remains strictly read-only:

```bash
datamind store "Import /Users/foo/sales-q2.csv as table q2_sales"
datamind store "Remember that weekly reports default to Chinese"
```

```
you  → "把 /Users/foo/sales-q2.csv 导入成数据表 q2_sales"
StoreAgent → calls db_import_csv(path=..., table='q2_sales')   ✓ 18 rows inserted
you  → "Q2 sales pipeline 里 in-pipeline 单子总额是多少？哪个 sales rep 单子最多？"
RetrieveAgent → calls db_query_sql(...)                    ✓ answers from the freshly-imported table
```

Or drop the file into the browser dropzone and click **导入**. Or say "把这段加进图谱：陈诚晋升 Tech Lead，向 Ann 汇报" → agent calls `graph_add_triples_from_text`, LLM extracts triples, graph upserts them. No restart, no reindex.

---

## Why the rewrite (v0.1 → v0.3)

The v0.1 prototype was functional but coupled: a global `AppState`, hard-wired modules, vendor-locked to the `claude` CLI. The current architecture reshapes it around:

- **Protocols + registries** — every capability is a `Protocol`; concrete classes register under a short name. New DB dialect / embedding provider / retriever strategy = one file.
- **Pluggable agent loop** — `native` (anthropic SDK) or `sdk` (claude-agent-sdk + CCR), one ENV switch.
- **Real SSE streaming** through FastAPI — not v0.1's fake character-sliced streaming.
- **Zero global state** — every request owns its own `RequestContext` with a trace id.
- **Side-by-side with v0.1** — the original code paths are untouched, so you can diff old against new.

See [Architecture](https://opendcai.github.io/DataMind-Doc/en/guide/basicinfo/architecture/) for full detail.

---

## Repo layout

```
DataMind/
├── datamind/                     # ── current codebase ────────────────
│   ├── agent/                    # base.py + loop_native.py + loop_sdk.py
│   ├── capabilities/             # kb / graph / db / skills / memory /
│   │                             #   ingest / embedding
│   ├── core/                     # Protocol, Registry, Logging, Tools, Hooks
│   ├── config.py                 # Settings (LLM / embedding / retrieval / …)
│   ├── scripts/                  # hello_*.py + seed_enterprise_demo.py
│   ├── cli.py                    # `python -m datamind ...`
│   ├── server.py                 # FastAPI + real SSE + /api/upload
│   └── tests/                    # no-network unit and contract tests
│
├── .claude/skills/               # SDK-style knowledge skills (SKILL.md)
├── static/app.html               # browser UI (drag-drop + tool cards + sidebar)
├── scripts/start_ccr.sh          # one-line CCR launcher (for sdk backend)
├── demo-uploads/                 # 6 sample files to drag-drop into the UI
│
├── benchmark/                    # current-stack checkpoint/resume runner
├── modules/ core/ main.py server.py              # ── v0.1 legacy ─
│
├── data/profiles/<profile>/      # per-profile raw inputs
├── storage/<profile>/            # per-profile indexes & DBs
├── pyproject.toml                # install + CLI entry
└── .env.datamind.example         # nested env template
```

---

## Profiles

One environment variable switches data + storage directories in lockstep:

```bash
DATAMIND__DATA__PROFILE=customer_a python -m datamind chat
```

Maps to `data/profiles/customer_a/` and `storage/customer_a/`.

---

## Tests

```bash
pytest datamind/tests/
```

Plus live smoke + benchmark scripts:
`hello_sdk`, `hello_kb`, `hello_db`, `hello_graph`, `hello_skills`, `hello_memory`, `hello_agent`,
`seed_enterprise_demo`, `hello_enterprise` (8 cross-backend questions).

---

## Full documentation

See **[DataMind-Doc](https://opendcai.github.io/DataMind-Doc/en/)** for architecture, configuration reference, per-capability deep dives, and tutorials in English and Chinese.
