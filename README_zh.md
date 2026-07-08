# DataMind

[English](./README.md) | **中文**

[![PyPI version](https://img.shields.io/pypi/v/datamind.svg)](https://pypi.org/project/datamind/)
[![Python](https://img.shields.io/pypi/pyversions/datamind.svg)](https://pypi.org/project/datamind/)
[![License](https://img.shields.io/pypi/l/datamind.svg)](https://github.com/OpenDCAI/DataMind/blob/main/LICENSE)

一个具备自主决策能力（agentic）的检索助手：从 **6 种**不同的知识来源取数，并**自己决定该用哪个工具**。你可以通过命令行或浏览器界面和它对话；把文件拖进去，它会自动把内容路由到正确的后端。

> **v0.3.0 是发布在 PyPI 上的预览版。** 当前代码位于 [`datamind/`](./datamind/) 目录；最初的 v0.1 原型（`main.py` / `server.py` / `modules/`）仅作对比参考而保留在仓库中。完整上手流程见 [`GETTING_STARTED.md`](./GETTING_STARTED.md) · [文档站](https://haolpku.github.io/DataMind-Doc/zh/)。

---

## 安装

```bash
pip install datamind
```

可选扩展：

```bash
pip install 'datamind[mysql]'         # MySQL 方言
pip install 'datamind[postgres]'      # PostgreSQL 方言
pip install 'datamind[voyage]'        # Voyage 向量嵌入
pip install 'datamind[huggingface]'   # 本地 BGE / e5 嵌入
pip install 'datamind[dev]'           # pytest + build + twine
```

指向一个 **Anthropic 兼容**的网关即可开始对话：

```bash
export DATAMIND__LLM__API_BASE=https://your-gateway.example.com
export DATAMIND__LLM__API_KEY=sk-ant-...
export DATAMIND__LLM__MODEL=claude-sonnet-4-6

datamind chat                                          # 命令行
python -m uvicorn datamind.server:app --port 8000      # 浏览器界面 http://127.0.0.1:8000
```

> **DataMind 只讲 Anthropic 协议。** 每一次请求都走 Anthropic 的 `/v1/messages`
> 协议、使用 Anthropic 格式的 key —— 代码里没有任何 OpenAI 客户端路径。如果你手上
> 唯一的网关/key 只支持 **OpenAI 格式**，不要去改 DataMind：在它前面放一个转换器即可。
> 详见 [自带 key —— CCR 桥接](#自带-key--ccr-桥接)。

---

## 能力一览

| 能力 | 后端 | Agent 拿到的工具 |
|---|---|---|
| **知识库（RAG）** | Chroma + BM25，采用倒数排名融合（RRF） | `kb_search`、`kb_list_documents`、`kb_count`、`kb_reindex` |
| **图谱** | NetworkX，JSON 持久化 | `graph_search_entities`、`graph_traverse`、`graph_neighbors`、`graph_upsert_triples` |
| **数据库** | SQLAlchemy（SQLite / MySQL / Postgres） | `db_list_tables`、`db_describe_table`、`db_query_sql`、`db_query_nl` |
| **技能（Skills）** | `.claude/skills/<name>/SKILL.md` + 安全的 Python 工具 | `skill_search`、`skill_get`、`skill_list`、`calculator`、`unit_convert`、`get_current_time`、`analyze_text` |
| **记忆** | SQLite，余弦召回 + LLM 事实抽取；**按作用域分类（`global` / `profile` / `session`）** 实现多租户隔离 | `memory_save`、`memory_recall`、`memory_forget`、`memory_list_profiles` |
| **数据导入** ✨ | 对话式数据导入 —— 通过聊天或浏览器拖拽区放入文件 | `kb_add_file`、`kb_add_path`、`db_import_csv`、`graph_add_triples_from_text` |
| **Hooks** ✨ v0.3 | 沙箱化的工具调度 —— 每次调用都会被拦截；支持 `Allow` / `Deny` / `AskUser` / `Rewrite`；每个 profile 有防篡改审计日志 | `PathAllowlistHook`、`DestructiveSqlHook`、`AuditLogHook`（内置；用户可插拔自定义 hook） |

**共 27 个工具。** 全部通过同一个 `ToolRegistry` 路由；由 agent 决定调用什么、以什么顺序调用。

---

## 60 秒体验

> **只想直接用？** `pip install datamind`，设置 `DATAMIND__LLM__API_KEY`，运行 `datamind chat`。
> 下面的流程会克隆仓库，让你顺带拿到种子脚本和企业 demo 数据集。

```bash
git clone https://github.com/OpenDCAI/DataMind.git && cd DataMind
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.datamind.example .env.datamind
$EDITOR .env.datamind     # 至少设置 DATAMIND__LLM__API_KEY

# 1. 冒烟测试网关连通性（约 2 秒）
python -m datamind.scripts.hello_sdk

# 2. 灌入一个真实的企业数据集（17 篇文档 / 64 个图谱节点 / 6 张表 / 101 行）
python -m datamind.scripts.seed_enterprise_demo

# 3. 看 agent 自主回答 8 个跨后端的问题
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m datamind.scripts.hello_enterprise

# 4. 或者直接打开浏览器界面
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m uvicorn datamind.server:app --port 8000
# → http://127.0.0.1:8000  —— 把任意 .md / .csv / .txt 拖进拖拽区，提问，观察工具触发
```

更多细节见 [`GETTING_STARTED.md`](./GETTING_STARTED.md)。

---

## 这里的"agentic"到底指什么

问它：**「工程部 Shanghai 的员工工资加起来是多少？」**

Agent 判断出需要用 SQL，先尝试 `db_query_nl`，拿到空结果后自己去检查表结构（`db_list_tables` → `db_describe_table`），发现字段是 `Eng` 而不是 `Engineering`，于是自行改写 SQL，最终答出 ¥26,000 —— 这一切都没有任何硬编码。同一个 agent 会为关系类问题选 `graph_search_entities + graph_neighbors`，为流程规范（SOP）类问题选 `kb_search + skill_get`，为"帮我记住这个"类请求选 `memory_save`。

**无论如何前端都不变。** 27 个工具、SSE 流式协议、聊天界面，以及 DataMind 自己的安全 HookChain，在两个可互换的 agent 后端上表现完全一致：

```
DATAMIND__AGENT__BACKEND=native   # 默认 —— 纯 Python 的 anthropic SDK + 自写循环
                                  # 需要一个 Anthropic 格式的上游
DATAMIND__AGENT__BACKEND=sdk      # claude-agent-sdk + claude-code-router (CCR)
                                  # 当你要接 OpenAI 格式的网关时用它（CCR 负责翻译）；
                                  # 额外解锁 Subagents / Compaction / Plan 模式
```

DataMind 的 `HookChain`（路径白名单、破坏性 SQL 拦截、防篡改审计）在**两个后端上都会强制执行** —— 在 `native` 上位于调度咽喉点，在 `sdk` 上位于每个 MCP 工具的包装层内。两者都用同一组 8 个企业 demo 问题做了端到端验证（[具体数据见此](./GETTING_STARTED.md#10-bench)）。

---

## 自带 key —— CCR 桥接

DataMind **只讲 Anthropic，且仅讲 Anthropic**（`/v1/messages` 协议，`sk-ant-...` 这类 key）。这是一个刻意的取舍 —— 只需推理一套协议、一条鉴权路径、一套流式语义。

但大多数自建网关、以及许多更便宜的 key 中转商，只暴露 **OpenAI** 的 Chat Completions 格式（`/v1/chat/completions`）。与其 fork DataMind 去加一个 OpenAI 客户端，我们选择在上游前面放一个小巧的转换器：

**[claude-code-router (CCR)](https://github.com/musistudio/claude-code-router)** —— 一个本地代理，接收 Anthropic `/v1/messages` 请求并转发给 OpenAI 格式的上游，双向翻译请求体（以及流式事件）。

```
DataMind ──Anthropic /v1/messages──▶  CCR（本地）  ──OpenAI /v1/chat/completions──▶  你的网关
   （sdk 后端）                       双向翻译                                     （OpenAI 格式 key）
```

于是 DataMind 本身从不改动：它始终以为自己在和 Anthropic 对话。CCR 吸收了格式差异。这正是 `sdk` agent 后端为之设计的用途。

### 我什么时候需要它？

| 你的上游网关讲的是…… | 该怎么做 |
|---|---|
| **Anthropic**（`/v1/messages`，`sk-ant` key） | 什么都不用做。用 `BACKEND=native`，把 `DATAMIND__LLM__API_BASE` 直接指过去。 |
| **OpenAI**（`/v1/chat/completions`） | 运行 CCR，用 `BACKEND=sdk`，让 DataMind 指向 CCR。 |

### 配置步骤（OpenAI 格式上游）

```bash
# 1. 安装 CCR（Node ≥ 18）
npm install -g @musistudio/claude-code-router
#    …… 或克隆 https://github.com/musistudio/claude-code-router 自行构建。

# 2. 启动本地桥接。它会写一份配置，注册你的 OpenAI 格式上游，
#    并应用 `anthropic` transformer。
UPSTREAM_BASE=https://your-openai-gateway.example.com/v1 \
UPSTREAM_KEY=sk-your-openai-format-key \
UPSTREAM_MODEL=claude-sonnet-4-6 \
  ./scripts/start_ccr.sh
# → [ccr] listen = http://127.0.0.1:13456

# 3. 让 DataMind 的 sdk 后端指向 CCR（写在 .env.datamind 里）：
DATAMIND__AGENT__BACKEND=sdk
DATAMIND__AGENT__CCR_BASE_URL=http://127.0.0.1:13456
DATAMIND__AGENT__CCR_API_KEY=dummy       # 真正的 key 在 CCR 里，这个字段用不到
```

`scripts/start_ccr.sh` 会替你生成 CCR 的 `config.json`，把上游 URL 归一化为
`/v1/chat/completions`，并把 `default` / `background` / `think` 路由映射到你的
主模型和降级模型。可通过环境变量覆盖 `CCR_PORT`、`UPSTREAM_FALLBACK`、
`CCR_SERVER_ENTRY`（CCR 的 `packages/server/dist/index.js` 路径）—— 详见该脚本头部注释。

---

## 靠对话就能加数据

4 个数据导入工具把 agent 变成了一个**可读可写**的界面：

```
你    → "把 /Users/foo/sales-q2.csv 导入成数据表 q2_sales"
agent → 调用 db_import_csv(path=..., table='q2_sales')   ✓ 插入 18 行
你    → "Q2 sales pipeline 里 in-pipeline 单子总额是多少？哪个 sales rep 单子最多？"
agent → 调用 db_query_sql(...)                            ✓ 从刚导入的表里给出答案
```

或者把文件拖进浏览器拖拽区，点击 **导入**。或者说"把这段加进图谱：陈诚晋升 Tech Lead，向 Ann 汇报" → agent 调用 `graph_add_triples_from_text`，LLM 抽取三元组，图谱把它们 upsert 进去。无需重启，无需重建索引。

---

## 为什么要重写（v0.1 → v0.3）

v0.1 原型能跑，但耦合严重：一个全局 `AppState`、写死的模块、被 `claude` CLI 供应商锁定。当前架构围绕以下几点重塑：

- **协议 + 注册表** —— 每种能力都是一个 `Protocol`；具体类以短名注册。新增一个 DB 方言 / 嵌入提供方 / 检索策略 = 一个文件。
- **可插拔的 agent 循环** —— `native`（anthropic SDK）或 `sdk`（claude-agent-sdk + CCR），一个环境变量切换。
- **真正的 SSE 流式** —— 通过 FastAPI，而不是 v0.1 那种假的、按字符切片的伪流式。
- **零全局状态** —— 每个请求拥有自己的 `RequestContext`，带一个 trace id。
- **与 v0.1 并存** —— 原始代码路径原封不动，方便新旧对照。

完整细节见[架构文档](https://haolpku.github.io/DataMind-Doc/zh/notes/guide/basicinfo/architecture/)。

---

## 仓库结构

```
DataMind/
├── datamind/                     # ── 当前代码 ────────────────────────
│   ├── agent/                    # base.py + loop_native.py + loop_sdk.py
│   ├── capabilities/             # kb / graph / db / skills / memory /
│   │                             #   ingest / embedding
│   ├── core/                     # Protocol、Registry、Config、Logging、Tools
│   ├── scripts/                  # hello_*.py + seed_enterprise_demo.py
│   ├── cli.py                    # `python -m datamind ...`
│   ├── server.py                 # FastAPI + 真 SSE + /api/upload
│   └── tests/                    # 95 个通过的测试（无需联网）
│
├── .claude/skills/               # SDK 风格的知识技能（SKILL.md）
├── static/app.html               # 浏览器界面（拖拽 + 工具卡片 + 侧边栏）
├── scripts/start_ccr.sh          # 一行命令启动 CCR（用于 sdk 后端）
├── demo-uploads/                 # 6 个可拖进界面的示例文件
│
├── modules/ core/ main.py server.py benchmark/   # ── v0.1 遗留代码 ─
│
├── data/profiles/<profile>/      # 每个 profile 的原始输入
├── storage/<profile>/            # 每个 profile 的索引与数据库
├── pyproject.toml                # 安装 + CLI 入口
└── .env.datamind.example         # 嵌套式环境变量模板
```

---

## Profiles（多套数据隔离）

一个环境变量即可让数据目录与存储目录联动切换：

```bash
DATAMIND__DATA__PROFILE=customer_a python -m datamind chat
```

映射到 `data/profiles/customer_a/` 和 `storage/customer_a/`。

---

## 测试

```bash
pytest datamind/tests/
# 95 passed in ~0.6s —— 无需联网
```

以及若干在线冒烟 + 基准脚本：
`hello_sdk`、`hello_kb`、`hello_db`、`hello_graph`、`hello_skills`、`hello_memory`、`hello_agent`、
`seed_enterprise_demo`、`hello_enterprise`（8 个跨后端问题）。

---

## 完整文档

架构、配置参考、各能力的深入讲解，以及中英文教程，请见 **[DataMind-Doc](https://haolpku.github.io/DataMind-Doc/zh/)**。
