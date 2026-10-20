# DataMind

**面向动态 Workspace 的 Codex 数据层。** Codex 负责理解任务、入库规划、关系抽取、
SQL 生成和最终回答；DataMind 负责数据工具、证据、写入回执和版本一致性。

[English](./README.md) · [个人用户插件](./plugins/datamind-context/) ·
[自动测试](https://github.com/OpenDCAI/DataMind/actions/workflows/python-ci.yml)

```text
个人：本地 Codex + 插件      企业：官方 Codex SDK + 应用宿主
              └────── Codex 主 Agent ──────┘
                       Skills + MCP
                            │
                  共用 DataMind 数据层
                KB / SQL / KG / Skills / Memory
                 证据 / 回执 / Workspace 版本
```

默认插件和新 Codex SDK 入口不再启动内部 StoreAgent／RetrieveAgent。旧 Python API、
CLI 和 HTTP/UI 仍作为兼容入口保留。旧配置 `agent.backend=sdk` 指 Claude Agent SDK，
与新的官方 Codex SDK 集成不同。

## 如何使用

个人用户安装插件：

```bash
pip install -e .
./scripts/install_codex_plugin.sh
```

在 Codex 中要求使用 DataMind 入库或查询。插件先开启操作上下文，再按需读取
`workspace-ingest`、`source-update` 或 `workspace-retrieve` Skill。
图、SQL、Skills 和 Memory 的直接操作不需要 DataMind 对话模型密钥。
KB 仍需要 embedding 服务，例如：

```bash
export DATAMIND__EMBEDDING__API_KEY=...
export DATAMIND__EMBEDDING__API_BASE=https://api.openai.com/v1
export DATAMIND__EMBEDDING__MODEL=text-embedding-3-small
```

企业应用使用官方 Python Codex SDK：

```bash
pip install -e '.[codex]'
```

```python
from datamind.codex import CodexSession

async def run():
    async with CodexSession(
        workspace="/srv/workspaces/team-a",
        data_root="/srv/datamind",
        profile="team-a",
    ) as session:
        result = await session.run("读取入库 Skill，然后导入这个工作区。")
        return result.final_response
```

宿主环境负责 Codex 登录以及企业鉴权、租户授权、调度和资源预算。默认继承 Codex
模型配置。每个会话拥有自己的 Codex thread 和绑定 profile 的 MCP 进程。
SDK 能并发运行会话，但当前 NetworkX JSON 的 live 图写入仍应使用单写入者；
这次改造不包含分布式图存储。版本化构建可在单主机上并发，通过 CAS 决定最终发布。

## 数据如何协同更新

1. `datamind_operation_begin(mode="write")` 开启写操作，后续工具传同一个 operation_id。
2. 读取入库 Skill，调用 `datamind_workspace_prepare` 获得源文件清单和哈希。
3. Codex 分页读取文档并抽取关系，为每篇文档提交 `{uri, version, triples}`。
   没有关系也提交空列表；抽取不完整时标记 `truncated=true`。
4. `datamind_workspace_publish` 验证源文件哈希，在私有目录构建选定的 KB、KG、DB，
   复用完成的源文件检查点，全部完成后发布一个 Workspace revision。
5. `datamind_operation_begin(mode="read")` 固定当前发布版本。跨工具查询及原文取证
   都使用该 operation_id；后续发布不会改变该读操作。新开读操作才能看到新版本。

发布失败保留旧版本。删除源文件后重新发布，新版本会同步去除该文件的业务数据。
协调器目前支持本地 SQLite、NetworkX、Chroma/LanceDB；索引仍是完整快照，
检查点节省重复计算，不代表存储或写入成本恒定。
Skills、Memory 属于实时上下文，不在业务数据快照中。

独立 live 入库仍支持 `kb_add_path`、`db_import_path`、`graph_replace_source`。
它们不会修改已发布快照。图更新由 Codex 提供三元组和预期源文件哈希，验证成功后
才替换该源的支持关系；`graph_forget_source` 只撤回该源支持，不删除原文件，
也不影响其他来源的证据。

## 图里存什么，如何查询

存有类型的实体和带来源证据的关系；业务关系名是可扩展字符串。
图默认仍为 NetworkX + JSON。Codex 先用 `graph_schema` 查看实际类型和关系，
再用 `graph_search_entities` 定位实体，通过 `graph_neighbors` 按方向找邻居，
或用 `graph_traverse` 做有边数和跳数上限的遍历。原文放 KB，精确数值和系列放 SQL。
最终回答由主 Codex 整合证据完成。

## 兼容和测试

旧插件的 ask/store/graph_query/workspace_ask 由主 Codex 直接调用数据工具代替；
旧 workspace_update 改用 prepare → Codex 抽取 → publish。
旧双 Agent Python、CLI、HTTP/UI 的使用见[兼容运行时](./docs/LEGACY_RUNTIME.md)。

```bash
pip install -e '.[dev,codex]'
pytest -q datamind/tests plugins/datamind-context/tests
python -m datamind.scripts.verify_sqlite_demo
```

CI 覆盖两版 Python、真实 MySQL/Dolt、本地版本与恢复、wheel 安装、直接 MCP 工具，
以及官方 Codex SDK 的无模型推理握手和编排契约。通过自动测试证明已覆盖契约，
不能保证所有模型回答或部署环境都正确。

[Apache License 2.0](./LICENSE)
