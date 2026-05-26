# DataMind

[![PyPI version](https://img.shields.io/pypi/v/datamind.svg)](https://pypi.org/project/datamind/)
[![Python](https://img.shields.io/pypi/pyversions/datamind.svg)](https://pypi.org/project/datamind/)
[![License](https://img.shields.io/pypi/l/datamind.svg)](https://github.com/OpenDCAI/DataMind/blob/main/LICENSE)

An agentic retrieval assistant that pulls from **six** distinct knowledge surfaces and **picks the right tool itself**. Talk to it through a CLI or a browser UI; drag a file in and it'll route it into the right backend automatically.

> **v0.3.0 is a preview release on PyPI.** New code lives under [`datamind/`](./datamind/); legacy v0.1 (`main.py` / `server.py` / `modules/`) still works for comparison. End-to-end walkthrough: [`GETTING_STARTED.md`](./GETTING_STARTED.md) · [docs site](https://haolpku.github.io/DataMind-Doc/en/).

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

Configure a Claude-compatible gateway and start chatting:

```bash
export DATAMIND__LLM__API_BASE=https://your-gateway.example.com
export DATAMIND__LLM__API_KEY=sk-...
export DATAMIND__LLM__MODEL=claude-sonnet-4-6

datamind chat                                          # CLI
python -m uvicorn datamind.server:app --port 8000      # browser UI on http://127.0.0.1:8000
```

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

**Frontend stays the same regardless.** The 27 tools, the streaming SSE protocol, and the chat UI work identically across two interchangeable agent backends:

```
DATAMIND__AGENT__BACKEND=native   # default — pure-Python anthropic SDK + self-written loop
DATAMIND__AGENT__BACKEND=sdk      # claude-agent-sdk + claude-code-router (CCR)
                                  # unlocks Hooks / Subagents / Compaction / Plan mode
```

Both verified end-to-end against the same 8 enterprise-demo questions ([numbers here](./GETTING_STARTED.md#10-bench)).

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

## Why v0.2

v0.1 was functional but coupled: a global `AppState`, hard-wired modules, vendor-locked to the `claude` CLI. v0.2 reshapes it around:

- **Protocols + registries** — every capability is a `Protocol`; concrete classes register under a short name. New DB dialect / embedding provider / retriever strategy = one file.
- **Pluggable agent loop** — `native` (anthropic SDK) or `sdk` (claude-agent-sdk + CCR), one ENV switch.
- **Real SSE streaming** through FastAPI — not v0.1's fake character-sliced streaming.
- **Zero global state** — every request owns its own `RequestContext` with a trace id.
- **Side-by-side with v0.1** — old code paths untouched, easy comparison.

See [Architecture](https://haolpku.github.io/DataMind-Doc/en/notes/guide/basicinfo/architecture/) for full detail.

---

## Repo layout

```
DataMind/
├── datamind/                     # ── v0.2 (new code) ──────────────────
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
├── pyproject.toml                # v0.2 install + CLI entry
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
