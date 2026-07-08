# DataMind

**English** | [中文](./README_zh.md)

[![PyPI version](https://img.shields.io/pypi/v/datamind.svg)](https://pypi.org/project/datamind/)
[![Python](https://img.shields.io/pypi/pyversions/datamind.svg)](https://pypi.org/project/datamind/)
[![License](https://img.shields.io/pypi/l/datamind.svg)](https://github.com/OpenDCAI/DataMind/blob/main/LICENSE)

An agentic retrieval assistant that pulls from **six** distinct knowledge surfaces and **picks the right tool itself**. Talk to it through a CLI or a browser UI; drag a file in and it'll route it into the right backend automatically.

> **v0.3.0 is a preview release on PyPI.** The current codebase lives under [`datamind/`](./datamind/); the original v0.1 prototype (`main.py` / `server.py` / `modules/`) is kept in-tree for comparison only. End-to-end walkthrough: [`GETTING_STARTED.md`](./GETTING_STARTED.md) · [docs site](https://haolpku.github.io/DataMind-Doc/en/).

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

Point it at an **Anthropic-compatible** gateway and start chatting:

```bash
export DATAMIND__LLM__API_BASE=https://your-gateway.example.com
export DATAMIND__LLM__API_KEY=sk-ant-...
export DATAMIND__LLM__MODEL=claude-sonnet-4-6

datamind chat                                          # CLI
python -m uvicorn datamind.server:app --port 8000      # browser UI on http://127.0.0.1:8000
```

> **DataMind only speaks Anthropic.** Every request goes out over Anthropic's
> `/v1/messages` protocol with an Anthropic-format key — there is no OpenAI
> client path in the codebase. If the only gateway/key you have speaks **OpenAI
> format**, don't rewrite DataMind: put a translator in front of it. See
> [Bring your own key — the CCR bridge](#bring-your-own-key--the-ccr-bridge).

---

## Capabilities

| Capability | Backend | Tools the agent gets |
|---|---|---|
| **KB (RAG)** | Chroma + BM25 with Reciprocal Rank Fusion | `kb_search`, `kb_list_documents`, `kb_count`, `kb_reindex` |
| **Graph** | NetworkX, JSON-persisted | `graph_search_entities`, `graph_traverse`, `graph_neighbors`, `graph_upsert_triples` |
| **Database** | SQLAlchemy (SQLite / MySQL / Postgres) | `db_list_tables`, `db_describe_table`, `db_query_sql`, `db_query_nl` |
| **Skills** | `.claude/skills/<name>/SKILL.md` + safe Python tools | `skill_search`, `skill_get`, `skill_list`, `calculator`, `unit_convert`, `get_current_time`, `analyze_text` |
| **Memory** | SQLite with cosine recall + LLM fact extraction; **scope-typed (`global` / `profile` / `session`)** for multi-tenant isolation | `memory_save`, `memory_recall`, `memory_forget`, `memory_list_profiles` |
| **Ingest** ✨ | Conversational data import — drop a file in via chat or the browser drag-drop zone | `kb_add_file`, `kb_add_path`, `db_import_csv`, `graph_add_triples_from_text` |
| **Hooks** ✨ v0.3 | Sandboxed tool dispatch — every call is intercepted; `Allow` / `Deny` / `AskUser` / `Rewrite`; tamper-evident audit log per profile | `PathAllowlistHook`, `DestructiveSqlHook`, `AuditLogHook` (built-in; user hooks pluggable) |

**27 tools total.** All routed through one `ToolRegistry`; the agent decides what to call and in what order.

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

More detail in [`GETTING_STARTED.md`](./GETTING_STARTED.md).

---

## What "agentic" actually means here

Ask: **"工程部 Shanghai 的员工工资加起来是多少？"**

The agent figures out it needs SQL, tries `db_query_nl`, gets an empty result, recovers by inspecting the schema (`db_list_tables` → `db_describe_table`), discovers the column is `Eng` not `Engineering`, rewrites the SQL itself, and answers ¥26,000 — without any of that being hard-coded. Same agent picks `graph_search_entities + graph_neighbors` for relationship questions, `kb_search + skill_get` for SOP questions, `memory_save` for "remember this for me" requests.

**Frontend stays the same regardless.** The 27 tools, the streaming SSE protocol, the chat UI, and DataMind's own safety HookChain work identically across two interchangeable agent backends:

```
DATAMIND__AGENT__BACKEND=native   # default — pure-Python anthropic SDK + self-written loop
                                  # requires an Anthropic-format upstream
DATAMIND__AGENT__BACKEND=sdk      # claude-agent-sdk + claude-code-router (CCR)
                                  # use this to sit on an OpenAI-format gateway
                                  # (CCR translates); adds Subagents / Compaction / Plan mode
```

DataMind's `HookChain` (path allow-list, destructive-SQL gate, tamper-evident audit) is enforced on **both** backends — at the dispatch chokepoint on `native`, inside each MCP tool wrapper on `sdk`. Both verified end-to-end against the same 8 enterprise-demo questions ([numbers here](./GETTING_STARTED.md#10-bench)).

---

## Bring your own key — the CCR bridge

DataMind talks **Anthropic and only Anthropic** (the `/v1/messages` protocol, an
`sk-ant-...`-style key). That's a deliberate choice — one protocol, one auth path,
one set of streaming semantics to reason about.

But most self-hosted gateways and many cheaper key resellers only expose the
**OpenAI** Chat Completions format (`/v1/chat/completions`). Rather than fork
DataMind to add an OpenAI client, we sit a tiny translator in front of the upstream:

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
| **OpenAI** (`/v1/chat/completions`) | Run CCR, use `BACKEND=sdk`, point DataMind at CCR. |

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

The 4 ingest tools turn the agent into a **read-and-write** surface:

```
you  → "把 /Users/foo/sales-q2.csv 导入成数据表 q2_sales"
agent → calls db_import_csv(path=..., table='q2_sales')   ✓ 18 rows inserted
you  → "Q2 sales pipeline 里 in-pipeline 单子总额是多少？哪个 sales rep 单子最多？"
agent → calls db_query_sql(...)                            ✓ answers from the freshly-imported table
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

See [Architecture](https://haolpku.github.io/DataMind-Doc/en/notes/guide/basicinfo/architecture/) for full detail.

---

## Repo layout

```
DataMind/
├── datamind/                     # ── current codebase ────────────────
│   ├── agent/                    # base.py + loop_native.py + loop_sdk.py
│   ├── capabilities/             # kb / graph / db / skills / memory /
│   │                             #   ingest / embedding
│   ├── core/                     # Protocol, Registry, Config, Logging, Tools
│   ├── scripts/                  # hello_*.py + seed_enterprise_demo.py
│   ├── cli.py                    # `python -m datamind ...`
│   ├── server.py                 # FastAPI + real SSE + /api/upload
│   └── tests/                    # 95 passing tests (no network required)
│
├── .claude/skills/               # SDK-style knowledge skills (SKILL.md)
├── static/app.html               # browser UI (drag-drop + tool cards + sidebar)
├── scripts/start_ccr.sh          # one-line CCR launcher (for sdk backend)
├── demo-uploads/                 # 6 sample files to drag-drop into the UI
│
├── modules/ core/ main.py server.py benchmark/   # ── v0.1 legacy ─
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
# 95 passed in ~0.6s — no network required
```

Plus live smoke + benchmark scripts:
`hello_sdk`, `hello_kb`, `hello_db`, `hello_graph`, `hello_skills`, `hello_memory`, `hello_agent`,
`seed_enterprise_demo`, `hello_enterprise` (8 cross-backend questions).

---

## Full documentation

See **[DataMind-Doc](https://haolpku.github.io/DataMind-Doc/en/)** for architecture, configuration reference, per-capability deep dives, and tutorials in English and Chinese.
