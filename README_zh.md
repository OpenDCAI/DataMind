# DataMind

<p align="center">
  <strong>边聊边入库，需要时跨数据面取证。</strong><br>
  为 Agent 准备的 inference-time data plane：刚写入的数据，下一句话就能使用。
</p>

<p align="center">
  <a href="https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml"><img src="https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/datamind/"><img src="https://img.shields.io/pypi/v/datamind.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/datamind/"><img src="https://img.shields.io/pypi/pyversions/datamind.svg" alt="Python"></a>
  <a href="https://github.com/OpenDCAI/DataMind/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/datamind.svg" alt="Apache-2.0"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="./GETTING_STARTED.md">完整上手</a> ·
  <a href="./docs/STABLE_API.md">API</a> ·
  <a href="./README.md">English</a>
</p>

<p align="center">
  <img src="./assets/inference-time-data-plane.png" alt="DataMind inference-time data plane" width="100%">
</p>

<p align="center"><sub>StoreAgent 负责写入；RetrieveAgent 跨数据面取证并回答。</sub></p>

> **v1.0.0** —— native + 本地 profile 存储是稳定基线；SDK/CCR 和远程数据库属于集成路径，请在目标环境中验证。

## 一句话理解

大多数 Agent 会取数，却没有一个地方放下刚刚学到的新事实。DataMind 把运行时拆成两个明确角色：

~~~text
新事实 / 文件 / CSV / 关系
            │
            ▼
       StoreAgent  ───── 写入 receipt ─────▶  KB · DB · Graph · Skills · Memory
            │
            │  下一句提问
            ▼
      RetrieveAgent  ◀── 答案 + evidence ─── 共享 data plane
~~~

这就是 **inference-time data**：不是训练数据，不是批处理 ETL，也不是无限增长的聊天记录，而是 Agent 运行期间可以更新、检查和复用的有界状态。

## 两个 Agent，一个硬边界

<table>
<tr>
<td valign="top" width="50%">

### StoreAgent

负责选择目标并写入：

- 文档与 chunks
- 行与数据表
- 图谱三元组
- profile 级 Skills
- 持久化 Memory

返回描述变更的 receipt。

</td>
<td valign="top" width="50%">

### RetrieveAgent

负责选择来源并读取：

- 语义知识库搜索
- SQL 检查与查询
- 图谱遍历
- Skills 与安全工具
- 按 scope 召回 Memory

返回带来源的 evidence。

</td>
</tr>
</table>

这个边界在代码层、工具到达模型前就已执行：RetrieveAgent 只能看到 19 个 read/utility 工具，StoreAgent 只能看到 11 个 write 工具。所有调用还会经过 `PathAllowlistHook`、`DestructiveSqlHook` 和 `AuditLogHook`。

## 五个数据面，一个答案

- **KB / RAG** —— 文档、制度、笔记与语义检索
- **Database** —— 精确数字、筛选、Join 与聚合
- **Knowledge Graph** —— 实体、关系与多跳事实
- **Skills** —— 可复用流程与安全工具
- **Memory** —— 偏好和长期事实，支持 `global`、`profile`、`session`

默认实现分别是 Chroma + BM25、SQLAlchemy（SQLite / MySQL / PostgreSQL）、NetworkX、profile 级 `SKILL.md` 和 SQLite Memory。

## 快速开始

~~~bash
pip install datamind

export DATAMIND__LLM__API_BASE=https://your-gateway.example.com
export DATAMIND__LLM__API_KEY=sk-...
export DATAMIND__LLM__PROTOCOL=anthropic   # 或 openai_chat_completions
export DATAMIND__LLM__MODEL=claude-sonnet-4-6

datamind chat
~~~

或者启动浏览器界面：

~~~bash
python -m uvicorn datamind.server:app --port 8000
# http://127.0.0.1:8000
~~~

协议必须显式设置，并会复用于外层 Agent loop 和内部生成（NL2SQL、multi-query、Memory、图谱抽取）。

<details>
<summary>可选扩展</summary>

~~~bash
pip install 'datamind[mysql]'
pip install 'datamind[postgres]'
pip install 'datamind[voyage]'
pip install 'datamind[huggingface]'
pip install 'datamind[dev]'
~~~
</details>

## 60 秒跑通完整 Demo

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

内置数据集包含 17 篇文档、64 个图谱节点、6 张表和 101 行数据。想看 UI：

~~~bash
DATAMIND__DATA__PROFILE=enterprise_demo \
  python -m uvicorn datamind.server:app --port 8000
~~~

把 `.md`、`.csv` 或 `.txt` 拖进去，提问并观察工具调用。完整流程见 [GETTING_STARTED.md](./GETTING_STARTED.md)。

## 对话本身就能改变 data plane

~~~text
你            → “把 sales-q2.csv 导入成数据表 q2_sales”
StoreAgent   → db_import_csv(...) → 写入 receipt
你            → “哪个 sales rep 的 Q2 pipeline 最大？”
RetrieveAgent → db_query_sql(...)  → 答案 + 表格 evidence
~~~

文档、图谱事实和 profile 级 Skills 也遵循同一模式。

## 选择运行时

稳定默认值是内置 `native` loop；需要 Subagents 或 Compaction 时，再选择可选的 `sdk` loop。

| Backend | 协议 | 状态 |
|---|---|---|
| `native` | Anthropic `/v1/messages` | **Stable** |
| `native` | OpenAI `/v1/chat/completions` | **Stable** |
| `sdk` | Anthropic | Integration |
| `sdk` | OpenAI 兼容 + CCR | Integration |

请显式设置：

~~~bash
DATAMIND__AGENT__BACKEND=native
DATAMIND__LLM__PROTOCOL=anthropic
~~~

完整边界见 [native / SDK 支持矩阵](./docs/SUPPORT_MATRIX.md)。SDK + OpenAI 格式路径使用 [CCR](https://github.com/musistudio/claude-code-router) 做本地协议桥接。

## Python 与 HTTP API

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

内置 FastAPI 服务提供 `GET /api/health`、`GET /api/tools`、`POST /api/ask`、`POST /api/store`、`POST /api/chat`（SSE）和 `POST /api/upload`。详见[稳定 API](./docs/STABLE_API.md)。

## 安全地嵌入，不要裸奔到公网

DataMind 期待你在边缘层提供认证与授权。公网部署前请阅读[安全边界](./docs/SECURITY_BOUNDARIES.md)：

- 本地使用绑定 loopback；
- 在边缘层加入认证、授权、TLS 和限流；
- 隔离 profile/storage 与上传路径；
- provenance 是来源元数据，不是权限凭证。

## 本地验证

~~~bash
pytest
python -m datamind.scripts.verify_sqlite_demo
~~~

CI 会运行无网络测试和确定性的 SQLite demo。长跑评测、checkpoint/resume 与 benchmark 见 [BENCHMARK_RUNNER](./docs/BENCHMARK_RUNNER.md)。

## 从这里继续

<table>
<tr>
<td valign="top" width="50%">

**开始构建**

- [完整上手](./GETTING_STARTED.md)
- [稳定 API](./docs/STABLE_API.md)
- [native / SDK 支持矩阵](./docs/SUPPORT_MATRIX.md)

</td>
<td valign="top" width="50%">

**理解概念**

- [术语与概念](./docs/CONCEPTS.md)
- [公网部署安全边界](./docs/SECURITY_BOUNDARIES.md)
- [CHANGELOG](./CHANGELOG.md)

</td>
</tr>
</table>

更多架构说明和教程见 [DataMind-Doc](https://opendcai.github.io/DataMind-Doc/zh/)。v1.x 支持入口位于 `datamind/`；原始 v0.1 原型仍保留用于对照。

## License

DataMind 基于 [Apache License 2.0](./LICENSE) 发布。
