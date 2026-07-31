# DataMind

**DataMind 是面向企业智能体的类型化、状态化统一推理数据层。**

它用一套显式模型访问、组合和更新智能体推理所需的异构数据：文档与
知识库、关系表与数据库、知识图、Memory 和可执行 Skills。

DataMind 0.7 是完整 Core 路径的研究预备版。它的目标不是堆叠应用功能，
而是先让每一次推理数据操作都可类型检查、可治理、可测试、可追溯和可回放，
再在 0.8 和 1.0 阶段对关键算法做深入优化。

## 本质设计

自由工具循环是一边思考、一边调用，完整执行意图往往只能事后观察。
DataMind 将意图先变成一个有界程序，再交给确定性 Core 执行：

```text
用户请求
  -> 可选的 LLM Compiler
  -> 可 JSON 序列化的 DataPlan
  -> 类型 / 数据源 / Effect / 预算校验
  -> 确定性 Executor
  -> 保留原生结果、证据、来源、快照和 Trace 的 ResultEnvelope
```

模型负责提出计划，Core 负责判断计划是否合法、能否执行，以及如何留下
可验证的执行记录。这是 DataMind 与普通 ToolRegistry 的核心区别。

## DataMind 0.7 已覆盖

- 类型化 `DataOp` 与有限 DAG `DataPlan`。
- 五个一等数据面：Document/KB、Table/DB、Graph、Memory、Skill。
- 确定性的跨面 `Filter`、`Project`、`Join`、`Fuse` 和 `Compose`。
- Effect 层级，以及请求级权限、审批、scope、快照和预算检查。
- 双时间 Memory 查询与受治理的 propose/apply 状态更新。
- Snapshot/`ChangeSet` 生命周期与历史版本读取。
- 执行 Trace、受保护的离线 Replay、Resolution Trace 和追加式 Outcome。
- 受限的自然语言到 `DataPlan` 编译，以及最多一次运行时重规划。
- 14 个确定性的 DataMind-Bench v0.1 验收任务。

Core 不承担 WebUI、部署平台、客户专用连接器、通用 Agent 编排和重度数据
清洗。DataFlow 负责清洗和训练数据生产；应用团队通过稳定 Ports 外围的
Adapter 完成集成。

## 仓库分层

```text
datamind/
├── kernel/          # 身份、Effect、预算、scope、快照和领域类型
├── dataops/         # 类型化操作、计划、结果、schema 与校验
├── engine/          # 确定性执行、resolution、记录与回放
├── intelligence/    # 受限的结构化计划编译
├── lifecycle/       # 显式数据源目录与快照同步
├── ports/           # source/model/lifecycle/trace/outcome 协议
└── adapters/        # 轻依赖参考实现

benchmarks/          # 可执行的 DataMind-Bench v0.1 验收层
docs/adr/            # 架构决策及被放弃的替代方案
```

依赖方向始终向内：`kernel` 和 `dataops` 没有供应商依赖；Core 服务只认识
稳定 Ports；Adapters 可以替换；数据源必须显式注册。

## 快速运行

DataMind Core 没有强制的第三方运行时依赖。

```bash
git clone https://github.com/OpenDCAI/DataMind.git
cd DataMind
python3.11 -m pip install -e .
python3.11 -m examples.typed_data_plane
```

权威执行接口保持很小：

```python
result = await engine.execute(
    operation_or_plan,
    context=execution_context,
)
```

`result.value` 保留 Adapter 的类型化原生值；外层 `ResultEnvelope` 统一携带
证据、bindings、provenance、固定快照、成本、警告、状态和 trace identity。

## 验证 Core

运行全部确定性测试：

```bash
python3.11 -m unittest discover -s datamind/tests -p 'test_*.py'
```

运行 14 个任务的验收 Benchmark：

```bash
python3.11 -m benchmarks.run_v01
```

DataMind-Bench v0.1 的职责是作为 0.7 的可执行规格，帮助逐模块理解和
Debug；它还不是面向论文的大规模 Benchmark。

## 从 0.7 到 0.8

0.7 要回答的是：所有必要模块是否已经组成一个完整、可检查的端到端系统？
0.8 要回答的是：在负责人逐模块理解、测试和 Debug 后，它们能否在有代表性
的任务上稳定工作，并呈现清楚的质量、成本和延迟权衡？

所以下一步重点不是继续扩展功能面，而是逐模块阅读执行 Trace、补充高价值
任务、定位失败模式，再有依据地优化 Planner、Memory Policy、Executor 和
评测体系。

## License

Apache-2.0。
