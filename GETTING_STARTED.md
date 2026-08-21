# Getting Started — from zero to a running agent

This is the copy-paste-friendly walkthrough. Every command is tested. Time budget: **~10 minutes** end to end.

## TL;DR

```bash
git clone https://github.com/your-org/DataMind.git && cd DataMind
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.datamind.example .env.datamind
# Edit .env.datamind — set at least DATAMIND__LLM__API_KEY

# 1. Verify gateway connectivity (takes ~2s)
python -m datamind.scripts.hello_sdk

# 2. Watch the whole agent run, picking tools on its own
python -m datamind.scripts.hello_agent

# 3. Start talking to it
python -m datamind chat
```

If every step prints `OK`, you're done. The rest of this file explains each step and what to do when something breaks.

---

## 0. Prerequisites

- Python **3.11+**. Check: `python3 --version`.
- An **Anthropic or OpenAI Chat Completions compatible gateway URL + API key**. Set `DATAMIND__LLM__PROTOCOL=anthropic` or `openai_chat_completions` explicitly; tool calling and streaming are supported on both native paths.
- Optional: a MySQL / PostgreSQL instance — only if you want the `db` capability to point at one of them.

---

## 1. Install

```bash
git clone https://github.com/your-org/DataMind.git
cd DataMind
python -m venv .venv
source .venv/bin/activate

pip install -e .
```

Optional extras:

```bash
pip install -e '.[mysql]'        # pymysql + cryptography
pip install -e '.[huggingface]'  # sentence-transformers (local embeddings)
pip install -e '.[dev]'          # pytest + pytest-asyncio
```

---

## 2. Configure

```bash
cp .env.datamind.example .env.datamind
$EDITOR .env.datamind
```

Minimum required fields:

```bash
DATAMIND__LLM__API_BASE=http://35.220.164.252:3888
DATAMIND__LLM__API_KEY=sk-YOUR-KEY
DATAMIND__LLM__MODEL=claude-sonnet-4-6
```

The same key also drives embeddings. If you don't set `DATAMIND__EMBEDDING__API_KEY` separately, it falls back to the LLM gateway credentials — for unified gateways like `35.220.164.252:3888` this is what you want.

---

## 3. Verify gateway connectivity (≈2 seconds)

```bash
python -m datamind.scripts.hello_sdk
```

Expected:

```
[hello_sdk] gateway = http://35.220.164.252:3888/
[hello_sdk] model   = claude-sonnet-4-6
[hello_sdk] prompt  = 'Reply with just the single word: pong'
[hello_sdk] --- stream ---
pong
[hello_sdk] OK: gateway reachable, streaming works, model replied 'pong'.
```

If this fails, nothing else will — fix credentials or base URL first.

---

## 4. Try each capability individually (2–3 minutes)

Each script uses a throwaway profile (`hello_<cap>_demo`) so they won't touch real data:

```bash
python -m datamind.scripts.hello_kb        # Chroma + embedding + hybrid retriever
python -m datamind.scripts.hello_graph     # Pure local — no network required
python -m datamind.scripts.hello_db        # NL2SQL + safeguards (DELETE rejected)
python -m datamind.scripts.hello_skills    # .claude/skills/ semantic search
python -m datamind.scripts.hello_memory    # Short + long term + LLM fact extraction
python -m datamind.scripts.hello_hooks     # Sandboxed dispatch + tamper-evident audit
```

Each prints a compact narration. The last line of a successful run is always `[hello_<cap>] OK`.

---

## 5. Watch the full agent (≈30 seconds)

```bash
python -m datamind.scripts.hello_agent
```

This is the prize-winning moment. The script:

1. Seeds a profile with a 2-file KB, a SQLite DB (employees + projects), and a small graph.
2. Asks four real questions in Chinese.
3. RetrieveAgent answers the first three questions; StoreAgent handles the final write. You can see each tool sequence in the output.

Expected tool sequences (they may differ slightly between runs — the agent has latitude):

| Question | Tools chosen | Correct answer |
|---|---|---|
| Status meeting 什么时候开？ | `memory_recall` → `kb_search` | 每周一 14:00（上海时间） |
| Search platform 负责人是谁？他在哪个城市？ | `kb_search` → `graph_search_entities` → `graph_neighbors` ×2 | Ann 领导；城市在 SQLite 而非图谱里 |
| 工程部 Shanghai 员工工资加起来是多少？ | `db_query_nl` → `db_describe_table` → `db_query_sql` | ¥26,000 |
| 帮我记住下周三会议调到周四 | `memory_save` | 写入长期记忆 |

---

## 6. Interactive REPL

```bash
python -m datamind chat
```

```
╭──── Chat ─────╮
│ DataMind ready · profile=default · model=claude-sonnet-4-6
│ tools=19 · kb_chunks=0 · graph_triples=0 · skills=2
│ type /exit to quit, /new to reset history
╰───────────────╯
you ›
```

Commands: `/new` resets history, `/exit` or `Ctrl-D` leaves. Tool calls print as they happen.

---

## 7. One-shot question

```bash
python -m datamind ask "如何做代码审查？" --show-tools
```

Uses RetrieveAgent over the **current profile's** knowledge surfaces. To write memory, use `datamind store "帮我记住……"`; StoreAgent chooses `memory_save` and defaults to the active profile scope. Session and global scopes remain available through the memory tool contract.

---

## 8. HTTP server + browser UI

```bash
python -m uvicorn datamind.server:app --host 127.0.0.1 --port 8000
```

Give it ~5 seconds to warm up (loads skills, graph, KB). Then **open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser** — you'll get a chat UI with:

- Streaming token-by-token answers
- Tool calls rendered as collapsible cards (name, input JSON, result preview)
- Sidebar showing the live config, every registered tool, graph stats, KB docs count, and a memory inspector
- One-click "重建索引" for the KB
- Per-session scoping via the `session` field at the bottom-left

Or hit the API directly:

```bash
# Liveness + config snapshot
curl -s localhost:8000/api/health | python3 -m json.tool

# List every tool with its schema
curl -s localhost:8000/api/tools | python3 -m json.tool

# Non-streaming
curl -s -X POST localhost:8000/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"message":"Say 你好"}' | python3 -m json.tool

# Real SSE stream — watch text / tool_use / tool_result / done events
curl -N -X POST localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"告诉我 Status meeting 时间"}'

# Store data through StoreAgent
curl -s -X POST localhost:8000/api/store \
  -H 'Content-Type: application/json' \
  -d '{"message":"记住：发布窗口是每周四 20:00"}' | python3 -m json.tool
```

---

## 8a. (Optional) Switch the agent loop to `claude-agent-sdk`

DataMind ships two interchangeable agent-loop implementations:

| Backend | How it talks to the model | When to pick it |
|---|---|---|
| `native` (default) | Pure Python, `anthropic` SDK → your gateway | Simplest deploy, fewest deps |
| `sdk` | `claude-agent-sdk` → `claude` CLI → **CCR** (local translator) → your gateway | Want the SDK's own Subagents / Compaction facilities |

The role-scoped tool registries (19 read/utility tools for RetrieveAgent and 11 write tools for StoreAgent), the event shape, the frontend, **and DataMind's own safety HookChain** (PathAllowlist / DestructiveSql / AuditLog) all work identically either way — only the inner loop changes. On `native` the HookChain runs at the loop's dispatch chokepoint; on `sdk` it runs inside each MCP tool wrapper. Same chain instance, same Allow/Deny/AskUser/Rewrite decisions, same audit log.

> Note: the SDK's *own* hook system (its `PreToolUse`/`PostToolUse` API) is a separate thing from DataMind's `HookChain`. DataMind's safety hooks are enforced on both backends regardless of which one you pick.

### Why CCR

The SDK always speaks Anthropic's `/v1/messages` protocol. If your upstream gateway only speaks OpenAI `/v1/chat/completions`, put `claude-code-router` (CCR) in the middle — it's a tiny Node process that translates both directions.

### Start CCR

```bash
# Install node >= 18. Then point CCR at your upstream:
export UPSTREAM_BASE=http://your-gateway.example.com/v1    # OpenAI-compatible
export UPSTREAM_KEY=sk-...
export UPSTREAM_MODEL=claude-sonnet-4-6

bash scripts/start_ccr.sh
# → listens on http://127.0.0.1:13456
```

Keep it running in its own terminal.

### Switch DataMind to the SDK backend

```bash
# In .env.datamind or inline:
export DATAMIND__AGENT__BACKEND=sdk
export DATAMIND__AGENT__CCR_BASE_URL=http://127.0.0.1:13456

# Everything else is unchanged:
python -m datamind chat
python -m uvicorn datamind.server:app --port 8000
```

Server startup logs will show the active backend:

```
INFO agent_loop_backend backend=sdk ccr=http://127.0.0.1:13456
```

Switch back to native any time by setting `DATAMIND__AGENT__BACKEND=native` (or removing the var — `native` is the default).

---

## 9. Add your own data — three ways

There are three ways to put data into DataMind, and they coexist. Pick whichever fits your workflow.

### 9.1 Agentic storage (recommended for ad-hoc additions)

StoreAgent owns all writes across the five data surfaces. Each successful tool call returns a durable receipt with its surface, source fingerprint, changed resources, and monotonic revision; retrying the same request is idempotent.

| Surface | Store tools |
|---|---|
| KB | `kb_add_text`, `kb_add_file`, `kb_add_path`, `kb_reindex` |
| DB | `db_import_csv`, `db_import_records` |
| Graph | `graph_upsert_triples`, `graph_add_triples_from_text` |
| Skills | `skill_upsert` |
| Memory | `memory_save`, `memory_forget` |

Try it:

```bash
datamind store "帮我把 /Users/foo/policy.md 加进知识库"
# → StoreAgent calls kb_add_file; RetrieveAgent can immediately use kb_search

datamind store "把 /Users/foo/sales.csv 导入成数据表 sales_q2"
# → StoreAgent calls db_import_csv; RetrieveAgent can immediately answer SQL questions

datamind store "把陈诚晋升 Tech Lead、向 Ann 汇报、负责 Project Kepler 写入图谱"
# → StoreAgent extracts and upserts triples, returning a write receipt
```

### 9.2 Browser drag-drop

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) → drag any `.md` / `.txt` / `.csv` file into the dropzone above the input box. The file uploads to `data/profiles/<profile>/uploads/`; clicking **导入** sends the request to StoreAgent, which picks the write tool. The ordinary chat endpoint remains RetrieveAgent-only.

A handful of demo files live in [`demo-uploads/`](./demo-uploads/) — drag any of them to see the full pipeline:

- `01-remote-work-policy-2026.md` — KB ingest
- `03-customers-2026.csv` — DB ingest with foreign-key relationships to existing employees
- `05-q2-personnel-changes.txt` — KB + Graph triple extraction
- `06-q2-incidents-extended.csv` — DB, joinable with seeded `performance_reviews`

After ingesting a few, ask cross-data questions like:
> "鼎元金融的续约时间？目前 in-pipeline 单子总额是多少？"
> "2026 Q2 哪个服务事故最多？responder 频率最高的工程师 H2 绩效如何？"

### 9.3 Bulk seeding (recommended for fresh profiles)

```bash
# Switch to a named profile
export DATAMIND__DATA__PROFILE=myproject

# KB: drop files anywhere under data/profiles/myproject/
mkdir -p data/profiles/myproject
cp your_docs/*.md data/profiles/myproject/

# Build the index
python -m datamind ingest

# Graph: optional JSONL triples
mkdir -p data/profiles/myproject/triplets
cat > data/profiles/myproject/triplets/people.jsonl <<'EOF'
{"subject": "Ann", "relation": "leads", "object": "Search platform"}
EOF

# SQL: point the `db` capability at your own database
export DATAMIND__DB__DIALECT=mysql
export DATAMIND__DB__DSN="mysql+pymysql://user:pw@host:3306/dbname"

# Talk to it
python -m datamind chat
```

For a complete, realistic example see `python -m datamind.scripts.seed_enterprise_demo` — it sets up 17 KB docs / 64 graph nodes / 6 SQL tables in one command.

---

## 10. Unit tests

```bash
pytest datamind/tests/
# expected: 95 passed in <1s, no network used
```

---

## Troubleshooting

| Symptom | What to do |
|---|---|
| `ValidationError: llm.api_key: Field required` | Export `DATAMIND__LLM__API_KEY` or put it in `.env.datamind`. |
| `HTTP 401 Invalid token` | Your key doesn't match the gateway. Test directly: `curl -X POST $BASE/v1/messages -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" -d '{"model":"claude-sonnet-4-6","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}'` |
| `Unknown embedding provider 'openai'` | You're not running from the repo root. `cd` back and retry. |
| `Agent not ready` from `/api/health` | Server is still warming up (loading skills index + graph). Wait ~5s. |
| CLI output is empty / garbled | Pipe unavoidable — `rich` disables color for pipes. Run interactively or pass `--show-tools false`. |
| `ModuleNotFoundError: claude_agent_sdk` | You don't need that SDK. Remove it from any local `requirements.txt` you edited. |
| Gateway responds with a Chinese error page instead of JSON | You're hitting a HTML-only path. Check `DATAMIND__LLM__API_BASE` — it should be the root (`http://host:port`), not `http://host:port/v1`. |

---

## What next

- [Architecture overview](https://opendcai.github.io/DataMind-Doc/en/guide/basicinfo/architecture/) — how the protocols, registries, and tool framework fit together.
- [Configuration reference](https://opendcai.github.io/DataMind-Doc/en/guide/advanced/config/) — every `DATAMIND__*` variable.
- [Per-capability guides](https://opendcai.github.io/DataMind-Doc/en/guide/modules/rag/) — KB / Graph / DB / Skills / Memory deep dives.
- [Legacy v0.1 README](./README.md#repo-layout) — if you need to compare against the previous implementation, `python main.py` still works.
