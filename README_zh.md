# DataMind

<p align="center">
  <strong>为 Agent 准备的 inference-time data。</strong><br>
  边聊边入库，需要时跨数据面取证。
</p>

<p align="center">
  <a href="https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml"><img src="https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/datamind/"><img src="https://img.shields.io/pypi/v/datamind.svg" alt="PyPI version"></a>
  <a href="https://pypi.org/project/datamind/"><img src="https://img.shields.io/pypi/pyversions/datamind.svg" alt="Python versions"></a>
  <a href="https://github.com/OpenDCAI/DataMind/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/datamind.svg" alt="License"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="./GETTING_STARTED.md">完整上手</a> ·
  <a href="./docs/STABLE_API.md">稳定 API</a> ·
  <a href="./README.md">English</a>
</p>

DataMind 是一个面向 Agent 的小型、协议中立 data plane。它把“数据怎么写入”和“问题怎么取证”拆成两个清晰的角色：

- **StoreAgent**：判断新信息应该落在哪个数据面，并在 inference time 写入。
- **RetrieveAgent**：跨五个数据面检索、组合证据并回答，严格保持只读。

它不是另一份聊天历史，也不是批处理 ETL。它是一层可检查、可持续更新的共享知识：对话中刚写入的数据，下一句话就能被使用。

![DataMind inference-time data plane](./assets/inference-time-data-plane.png)

<p align="center"><sub>暖色路径是 StoreAgent 的写入/入库流；冷色路径是 RetrieveAgent 的跨面取证流。</sub></p>

> **v1.0.0 是首个稳定版本。** native + 本地 profile 存储是发布基线；SDK/CCR 与远程数据库属于已实现的集成路径，建议在目标环境中单独验证。

## 为什么是 DataMind？

| Inference-time 写入 | 跨数据面取证 | 安全边界内建 |
|---|---|---|
| 一句话、一个文件、一张 CSV 或一组关系都能直接变成持久数据，无需另起 ingestion 服务。 | 一个问题同时查询 RAG、SQL、图谱、Skills、Memory，返回统一答案与来源。 | 两个 Agent 使用物理隔离的工具注册表，共享安全 Hook、profile 隔离和可审计 receipt。 |

### 两个 Agent 的契约

| Agent | 负责什么 | 明确不做什么 |
|---|---|---|
| **StoreAgent** | `kb_add_*`、`db_import_*`、图谱 upsert、`skill_upsert`、`memory_save` / `memory_forget` | 不带写权限回答取数问题 |
| **RetrieveAgent** | 知识库搜索、SQL 检查/查询、图谱遍历、Skills、Memory recall | 不修改任何数据面 |

边界在工具到达模型之前就由代码强制执行：RetrieveAgent 获得 19 个 read/utility 工具，StoreAgent 获得 11 个 write 工具。每次调用还会经过 `PathAllowlistHook`、`DestructiveSqlHook` 和 `AuditLogHook`。

## 五个数据面，一个答案

| 数据面 | 最适合解决 | 默认实现 |
|---|---|---|
| **KB / RAG** | 文档、制度、笔记、语义检索 | Chroma + BM25 + 倒数排名融合（RRF） |
| **Database** | 精确数字、筛选、Join、聚合 | SQLAlchemy，支持 SQLite / MySQL / PostgreSQL |
| **Knowledge Graph** | 实体、关系、多跳事实 | NetworkX + JSON 持久化 |
| **Skills** | 可复用流程与安全工具 | profile 级 `SKILL.md` + Python 工具 |
| **Memory** | 偏好、约定、长期事实 | SQLite + 余弦召回，支持 `global` / `profile` / `session` |

## 快速开始

安装稳定的 native 栈：

~~~bash
pip install datamind
~~~

配置一个 Anthropic 或 OpenAI Chat Completions 兼容网关：

~~~bash
export DATAMIND__LLM__API_BASE=https://your-gateway.example.com
export DATAMIND__LLM__API_KEY=sk-...
export DATAMIND__LLM__PROTOCOL=anthropic   # 或 openai_chat_completions
export DATAMIND__LLM__MODEL=claude-sonnet-4-6

datamind chat
~~~

或者启动本地浏览器界面：

~~~bash
python -m uvicorn datamind.server:app --port 8000
# 打开 http://127.0.0.1:8000
~~~

协议必须显式设置，并会同时用于外层 Agent loop 和内部生成路径（NL2SQL、multi-query、Memory、图谱抽取）。模型名不会隐式决定协议。详见 [ADR 0001](./docs/adr/0001-protocol-neutral-model-clients.md)。

### 可选扩展

~~~bash
pip install 'datamind[mysql]'       # MySQL 方言
pip install 'datamind[postgres]'    # PostgreSQL 方言
pip install 'datamind[voyage]'      # Voyage 向量嵌入
pip install 'datamind[huggingface]' # 本地 BGE / e5 嵌入
pip install 'datamind[dev]'         # pytest + build + twine
~~~

## 60 秒看见完整流程

仓库内置一套企业 demo：17 篇文档、64 个图谱节点、6 张表、101 行数据。

~~~bash
git clone https://github.com/OpenDCAI/DataMind.git
cd DataMind
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.datamind.example .env.datamind
$EDITOR .env.datamind              # 至少设置 DATAMIND__LLM__API_KEY

python -m datamind.scripts.hello_sdk
python -m datamind.scripts.seed_enterprise_demo
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m datamind.scripts.hello_enterprise
~~~

想直接看 UI：

~~~bash
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m uvicorn datamind.server:app --port 8000
~~~

把 `.md`、`.csv` 或 `.txt` 拖进拖拽区，提问并观察按角色隔离的工具调用。完整步骤见 [GETTING_STARTED.md](./GETTING_STARTED.md)。

## 对话本身就能改变 data plane

~~~bash
datamind store "把 /Users/foo/sales-q2.csv 导入成数据表 q2_sales"
datamind store "记住：所有周报默认使用中文"
datamind chat
~~~

下一次提问即可使用刚写入的数据：

~~~text
你            → “哪个 sales rep 的 Q2 pipeline 最大？”
RetrieveAgent  → db_query_sql(...) → 答案 + 表格来源证据
~~~

同样的模式也适用于文档、图谱事实和 profile 级 Skills。StoreAgent 返回写入 receipt；RetrieveAgent 返回标准化 evidence。

## 选择运行时

稳定默认值是内置的 `native` loop。需要 Subagents 或 Compaction 等 Claude Agent SDK 能力时，再选择可选的 `sdk` loop。

| Backend | 协议 | 状态 | 适用场景 |
|---|---|---|---|
| `native` | Anthropic `/v1/messages` | **Stable** | 想使用最小、最直接的支持路径 |
| `native` | OpenAI `/v1/chat/completions` | **Stable** | 上游是 OpenAI 兼容网关 |
| `sdk` | Anthropic | Integration | 已经在使用 Claude Agent SDK / CLI |
| `sdk` | OpenAI 兼容 + CCR | Integration | 需要 SDK loop，但上游只有 OpenAI 格式 |

请显式设置两个开关：

~~~bash
DATAMIND__AGENT__BACKEND=native
DATAMIND__LLM__PROTOCOL=anthropic
~~~

完整边界见 [native / SDK 支持矩阵](./docs/SUPPORT_MATRIX.md)。SDK + OpenAI 格式路径使用 [CCR](https://github.com/musistudio/claude-code-router) 做本地 Anthropic ↔ OpenAI 协议桥接：

<details>
<summary>展开 SDK + CCR 配置</summary>

~~~bash
npm install -g @musistudio/claude-code-router   # Node >= 18

UPSTREAM_BASE=https://your-openai-gateway.example.com/v1 \
UPSTREAM_KEY=sk-your-openai-format-key \
UPSTREAM_MODEL=claude-sonnet-4-6 \
  ./scripts/start_ccr.sh

DATAMIND__AGENT__BACKEND=sdk
DATAMIND__AGENT__CCR_BASE_URL=http://127.0.0.1:13456
DATAMIND__AGENT__CCR_API_KEY=dummy
~~~

真实上游 key 保存在 CCR 中，不要放进浏览器侧环境，也不要提交生成的 CCR 配置。
</details>

## Python 与 HTTP API

推荐使用轻量的异步 Python facade：

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

内置 FastAPI 服务提供：

| 路由 | 作用 |
|---|---|
| `GET /api/health` | 存活状态、profile、协议和工具数量 |
| `GET /api/tools` | 按角色隔离的工具目录与 schema |
| `POST /api/ask` | 只读 RetrieveAgent 请求 |
| `POST /api/store` | StoreAgent 请求，返回 receipts |
| `POST /api/chat` | 真 SSE 流（`text`、`tool_use`、`tool_result`、`done`） |
| `POST /api/upload` | 保存上传文件并返回建议的入库提示 |

稳定返回结构与兼容策略见 [稳定 API](./docs/STABLE_API.md)。

## 安全与公网部署边界

DataMind 设计为嵌入你自己的认证和授权层，内置 server 不替代它。准备暴露到公网前，请先阅读[公网部署安全边界](./docs/SECURITY_BOUNDARIES.md)：

- 本地使用时绑定 loopback；
- 在边缘层提供认证、授权、限流与 TLS；
- 隔离 profile/storage 目录，限制上传路径；
- provenance 只是来源元数据，不是授权凭证。

## 开发与验证

~~~bash
pytest
python -m datamind.scripts.verify_sqlite_demo
~~~

第一条命令运行无网络单元测试和协议合同测试；第二条命令一键验证真实 SQLite demo。长跑评测、checkpoint/resume 和 benchmark 见 [BENCHMARK_RUNNER](./docs/BENCHMARK_RUNNER.md)。

<details>
<summary>查看仓库结构</summary>

~~~text
DataMind/
├── datamind/                 # 当前 v1.x 包
│   ├── agent/                # StoreAgent / RetrieveAgent loop
│   ├── capabilities/         # kb / graph / db / skills / memory / ingest
│   ├── core/                 # protocols、registries、hooks、contracts
│   ├── scripts/               # hello_*.py 与验证 demo
│   ├── cli.py                 # datamind CLI
│   └── server.py              # FastAPI + SSE + upload API
├── docs/                     # API、支持矩阵、安全和概念说明
├── assets/                   # README 架构图
├── demo-uploads/              # 可拖拽的示例文件
├── benchmark/                # checkpoint/resume runner
├── data/profiles/<profile>/  # profile 级输入
├── storage/<profile>/        # profile 级索引和数据库
├── modules/ core/ main.py    # v0.1 原型，保留用于对比
├── pyproject.toml             # 包和 CLI 元数据
└── LICENSE                    # Apache-2.0
~~~

v0.1 原型路径仍保留在仓库中；v1.x 的支持入口位于 `datamind/`。
</details>

## 文档与发布信息

- [完整上手](./GETTING_STARTED.md)
- [稳定 API](./docs/STABLE_API.md)
- [native / SDK 支持矩阵](./docs/SUPPORT_MATRIX.md)
- [公网部署安全边界](./docs/SECURITY_BOUNDARIES.md)
- [术语与概念](./docs/CONCEPTS.md) —— inference-time data 与传统 RAG、ETL、Agent Memory 的区别
- [CHANGELOG](./CHANGELOG.md)
- [DataMind-Doc](https://opendcai.github.io/DataMind-Doc/zh/)

## License

DataMind 基于 [Apache License 2.0](./LICENSE) 发布。
