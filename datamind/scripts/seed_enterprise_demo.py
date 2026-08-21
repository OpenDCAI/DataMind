"""Seed the `enterprise_demo` profile with realistic, moderate-sized data.

Scale:
    KB   : 18 markdown docs across 6 categories, mix of CN/EN
    Graph: ~60 nodes / ~110 edges across 4 triplet files
    DB   : 6 tables, ~180 rows total, foreign keys + meaningful dates

Why this exists:
    The original hello_agent_demo is tiny (2 docs, 4 edges, 3 employee rows).
    It works for a smoke test but can't exercise agentic multi-hop reasoning
    (e.g. "which Q4 incidents hit teams run by high-performers?"). This
    profile is explicitly designed so cross-backend questions actually need
    to touch KB + graph + DB together.

Run:
    python -m datamind.scripts.seed_enterprise_demo
    # then:
    DATAMIND__DATA__PROFILE=enterprise_demo python -m datamind ingest
    DATAMIND__DATA__PROFILE=enterprise_demo python -m datamind chat

Idempotent: wipes the profile's data + storage directories on each run.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from textwrap import dedent

from sqlalchemy import text


PROFILE = "enterprise_demo"


# ============================================================ KB documents


KB_DOCS: dict[str, str] = {
    # ---------------- Company handbook (CN-heavy) -----------------
    "handbook/employee-handbook.md": dedent("""
        # Acme 员工手册

        ## 1. 公司介绍
        Acme Technologies 成立于 2019 年，总部位于上海，在北京和深圳设有研发中心。
        主要业务包括企业搜索平台（Search Platform）、数据中台（Data Platform）和
        AI 助手（AI Copilot）三大产品线。

        ## 2. 部门与团队
        - **工程部（Engineering）**：下设搜索平台组、数据平台组、AI 平台组、基础架构组
        - **产品部（Product）**：产品经理、设计师、用户研究
        - **运营部（Operations）**：销售、客户成功、市场
        - **职能部门（G&A）**：HR、财务、法务、IT

        ## 3. 会议政策
        - 全员周会（All-Hands）：每周一 14:00（上海时间）
        - 工程周会（Eng Sync）：每周三 10:00
        - OKR review：每季度第一周
        - 1:1：每两周一次，由 manager 发起

        ## 4. 绩效考核
        每半年一次，采用 1.0-5.0 五分制。4.0 以上为"超出预期"，3.0-3.9 为"符合预期"，
        3.0 以下为"低于预期"。连续两次低于 3.0 触发 PIP。

        ## 5. 办公与福利
        - 上海办公室：浦东软件园 E 座 8 层，咖啡吧默认燕麦奶
        - 弹性办公时间：核心时段 10:00-16:00
        - 每年 15 天年假 + 10 天病假

        ## 6. 相关文档
        - 安全政策：`security-policy.md`
        - 事故响应：`incident-response-sop.md`
        - 新员工 onboarding：`onboarding-checklist.md`
    """).strip(),

    "handbook/onboarding-checklist.md": dedent("""
        # 新员工入职清单

        ## Day 1
        - [ ] 领取工牌、笔记本电脑
        - [ ] 账号创建：企业邮箱、Slack、Jira、Confluence、GitLab、1Password
        - [ ] 安装必备软件：VS Code / PyCharm、Docker、kubectl、claude CLI
        - [ ] 阅读《员工手册》和《安全政策》
        - [ ] 与 HR 完成入职培训

        ## Week 1
        - [ ] 与直属 manager 完成第一次 1:1
        - [ ] 熟悉团队 runbook 和 on-call 流程
        - [ ] 申请 staging / production 环境访问权限（走 access-request 系统）
        - [ ] 加入相关 Slack 频道：#eng-general / #team-<your-team> / #incidents

        ## Month 1
        - [ ] 完成第一个小任务并提交 MR
        - [ ] 参加至少一次 incident 复盘会（观察即可）
        - [ ] 阅读产品核心文档：Search Platform 架构、Data Platform 架构
        - [ ] 与跨团队至少 3 人做 coffee chat

        ## 内部系统一览
        Acme 内部系统较多，新员工容易迷路，以下是常用系统及其负责人：
        - **Jira**（任务管理）：IT 组维护，李慧负责
        - **Confluence**（文档中心）：IT 组维护
        - **GitLab**（代码托管）：基础架构组维护，王明负责
        - **Grafana / Prometheus**（监控）：基础架构组，周鹏负责
        - **Airflow**（数据管道）：数据平台组，赵静负责
        - **Vault**（密钥管理）：安全团队，孙伟负责
    """).strip(),

    "handbook/security-policy.md": dedent("""
        # 安全政策

        ## 1. 密钥管理
        - 所有生产环境密钥必须存储在 Vault 中（负责人：孙伟）
        - 禁止将 API key / password 硬编码到代码或配置文件
        - MR 提交前本地运行 `pre-commit run gitleaks --all-files`
        - 个人访问令牌（PAT）有效期不超过 90 天

        ## 2. 数据脱敏
        - 非生产环境数据库必须经过脱敏处理
        - 个人身份信息（PII）：姓名、手机号、邮箱、身份证号
        - 财务信息：工资、银行卡号
        - 脱敏工具：`dataplatform/tools/pii-scrubber`
        - 相关事故参考：`incident-response-sop.md` 附录 B

        ## 3. 访问控制
        - 生产环境访问走 SSO + MFA
        - `sudo` 操作必须通过 access-request 审批
        - 审计日志保留 365 天
        - 离职员工账号在 Last Working Day 当天禁用

        ## 4. 安全事件上报
        发现可疑行为或数据泄露嫌疑，立即联系安全团队（#security-alerts），
        不要试图自行调查。上报流程详见 `incident-response-sop.md`。
    """).strip(),

    # ---------------- Runbooks (SOP) -----------------
    "runbooks/incident-response-sop.md": dedent("""
        # 事故响应 SOP

        ## 响应级别
        | 级别 | 影响 | 首次响应 SLA | 触发升级 |
        |---|---|---|---|
        | P0 | 生产核心服务宕机 / 数据丢失 | 5 分钟 | 10 分钟未响应 → CTO |
        | P1 | 主要功能不可用 / 大面积降级 | 15 分钟 | 30 分钟未缓解 → VP Eng |
        | P2 | 局部降级 / 非核心功能故障 | 1 小时 | 4 小时未缓解 → 直属 manager |
        | P3 | 轻微影响 / 可绕过 | 下个工作日 | — |

        ## 标准流程
        1. **检测**：Grafana 告警或用户反馈（Slack #incidents）
        2. **分级**：on-call 评估级别
        3. **响应**：创建 incident 频道 `#inc-YYYYMMDD-<slug>`，拉相关 owner
        4. **缓解**：先恢复服务，再找根因
        5. **复盘**：72 小时内产出 postmortem 文档

        ## Search Platform 专属 runbook
        - p99 延迟 > 500ms：page Ann（团队负责人）
        - 索引写入失败：检查 Chroma 集群状态，联系 Bob（索引质量负责人）
        - 查询 QPS 异常：看 Grafana "search-qps" 面板，判断是否需要扩容

        ## Data Platform 专属 runbook
        - Airflow DAG 超时：check Slack #dp-alerts，联系赵静
        - Kafka lag > 1000：联系数据平台组 on-call
        - BigQuery 查询失败：检查配额，必要时联系云团队

        ## 附录 B：历史重大事故
        - INC-2025-Q3-07：Search Platform 索引服务宕机 2h，影响 30% 用户
        - INC-2025-Q4-11：Data Platform ETL 延迟 6h，Q4 报表延期
        - INC-2026-Q1-03：AI Copilot 限流配置错误，API 5xx 持续 45min
    """).strip(),

    "runbooks/oncall-rotation.md": dedent("""
        # On-call 轮值

        ## 排班原则
        - 每组至少 3 人，每人每 3 周轮一次
        - on-call 期间工作时间 + 下班后紧急响应
        - on-call 当日无会议压力、无新项目交付

        ## 当前排班（2026 Q2）
        ### 搜索平台组
        - Ann（Eng Manager）：每月最后一周 backup
        - Bob（Senior Engineer）：Week 1, 4, 7, 10
        - 陈诚（Engineer）：Week 2, 5, 8, 11
        - Dana（Engineer）：Week 3, 6, 9, 12

        ### 数据平台组
        - 赵静（Eng Manager）：backup
        - Frank（Senior）：Week 1, 4, 7, 10
        - Grace（Senior）：Week 2, 5, 8, 11
        - Henry（Engineer）：Week 3, 6, 9, 12

        ### 基础架构组
        - 王明：Week 1, 3, 5, 7, 9, 11
        - 周鹏：Week 2, 4, 6, 8, 10, 12

        ## 交接
        每周五 17:00 在 #oncall 频道做简短交接：本周遗留问题、预期风险。
    """).strip(),

    "runbooks/deployment-checklist.md": dedent("""
        # 发布清单

        ## 发布前
        - [ ] 功能已通过 QA 测试
        - [ ] 性能测试：p99 延迟不劣化 > 10%
        - [ ] 数据库 migration review（必要时）
        - [ ] Feature flag 配置：默认关闭，按 cohort 放量
        - [ ] Rollback 方案已写入发布单
        - [ ] 变更已在 #deploys 频道通知

        ## 发布窗口
        - 周二、周四 10:00-16:00 为推荐发布窗口
        - 避开周五、节假日前一天、on-call 交接时段

        ## 发布中
        - 灰度：1% → 10% → 50% → 100%，每阶段观察 15 分钟
        - 监控关键指标：错误率、p99 延迟、业务指标
        - 发现异常立即回滚，不要尝试"修一下再说"

        ## 发布后
        - 发布单更新实际状态
        - 48 小时内跟踪用户反馈
        - 产生增量问题写入对应 runbook
    """).strip(),

    # ---------------- Tech reference -----------------
    "tech/search-platform-architecture.md": dedent("""
        # Search Platform 架构

        ## 概览
        Search Platform 是 Acme 企业搜索的核心产品，为内部用户提供跨知识库的统一检索。
        团队负责人：Ann（Engineering Manager）。当前服务约 800 家企业客户。

        ## 组件
        ### 索引层
        - **Chroma Cluster**：向量检索主存储，分片数 16
        - **Elasticsearch**：倒排索引，支持 BM25 + filter
        - **Index Writer Service**：接收文档写入，同时写入两套存储

        ### 查询层
        - **Query Router**：根据查询类型分发到 vector / bm25 / hybrid
        - **Hybrid Retriever**：Reciprocal Rank Fusion，k=60
        - **Rerank Service**：可选 cross-encoder 重排（未默认开启）

        ### 接入层
        - **Search API**：对外 REST + gRPC
        - **Admin Console**：index 管理 / 查询分析

        ## 关键依赖
        - 上游：Data Platform（提供数据同步）
        - 下游：AI Copilot（调用 Search API）

        ## SLO
        - 可用性：99.9%
        - p99 延迟：< 500ms
        - 索引新鲜度：< 5 分钟

        ## 常见问题
        参见 `runbooks/incident-response-sop.md` 的 Search Platform 专属章节。
    """).strip(),

    "tech/data-platform-architecture.md": dedent("""
        # Data Platform 架构

        ## 概览
        Data Platform 是公司的数据中台，负责数据采集、清洗、存储、分析全链路。
        团队负责人：赵静（Engineering Manager）。

        ## 组件
        ### 采集层
        - **Kafka Cluster**：实时事件流
        - **CDC Connectors**：同步业务库到数据仓库

        ### 存储层
        - **BigQuery**：离线分析主存储
        - **ClickHouse**：实时分析
        - **S3**：原始数据归档

        ### 编排层
        - **Airflow**：离线任务调度
        - **Flink**：实时流处理

        ### 服务层
        - **Metrics API**：对外暴露业务指标
        - **Data Portal**：数据查询与看板

        ## 关键依赖
        - 上游：各业务线写入 Kafka
        - 下游：Search Platform / AI Copilot 订阅

        ## 历史事故
        见 `runbooks/incident-response-sop.md` 附录 B 中的 INC-2025-Q4-11。
    """).strip(),

    "tech/ai-copilot-architecture.md": dedent("""
        # AI Copilot 架构

        ## 概览
        AI Copilot 是 Acme 给客户提供的企业 AI 助手产品，基于大模型 + 企业数据做问答。
        团队负责人：林涛（Engineering Manager）。2025 Q3 GA。

        ## 核心能力
        - 基于 Search Platform 做 RAG
        - 通过 Metrics API 拉取实时数据
        - Function calling 调用企业内部系统（Jira / Confluence / Slack）

        ## 模型选型
        - 默认：Claude Sonnet 4.6（主问答）
        - Fallback：Claude Haiku 4.5（高并发廉价任务、事实抽取）
        - 嵌入：text-embedding-3-small

        ## 关键依赖
        - Search Platform（RAG 检索）
        - Data Platform → Metrics API（实时数据）
        - Vault（密钥管理）

        ## 历史事故
        INC-2026-Q1-03：限流配置错误导致 5xx，详见 incident runbook。
    """).strip(),

    "tech/api-v2-reference.md": dedent("""
        # Public API v2 Reference

        ## Authentication
        All v2 endpoints require `Authorization: Bearer <token>`. Tokens are
        issued via the admin console and rotate every 90 days.

        ## Base URL
        - Production: `https://api.acme.com/v2`
        - Staging: `https://api.staging.acme.com/v2`

        ## Endpoints

        ### Search
        `POST /v2/search`
        Searches across all indexed knowledge bases.
        ```json
        {
          "query": "string",
          "top_k": 10,
          "filters": {"kb_id": "string"},
          "retrieval_mode": "hybrid|vector|bm25"
        }
        ```

        ### Documents
        - `POST /v2/documents` — upload a document (triggers async indexing)
        - `GET /v2/documents/{id}` — fetch document metadata
        - `DELETE /v2/documents/{id}` — soft-delete (reindex required)

        ### Conversations (AI Copilot)
        - `POST /v2/conversations` — start a new conversation
        - `POST /v2/conversations/{id}/messages` — send a message
        - Streaming via SSE: `text`, `tool_use`, `tool_result`, `done` events

        ## Rate limits
        - Search: 100 req/s per tenant
        - Documents write: 50 req/s per tenant
        - Conversations: 20 concurrent per tenant

        ## Error codes
        - `401 invalid_token` — token expired / revoked
        - `403 tenant_suspended` — billing or policy issue
        - `429 rate_limited` — check `Retry-After` header
        - `5xx` — server error, auto-retryable with exponential backoff
    """).strip(),

    # ---------------- Quarterly / product planning -----------------
    "planning/product-roadmap-2026.md": dedent("""
        # 2026 产品路线图

        ## Q1 已完成
        - AI Copilot：支持多轮对话 + 工具调用
        - Search Platform：Hybrid Retriever 上线（向量 + BM25 融合）
        - Data Platform：迁移到 BigQuery 完成

        ## Q2（当前）
        - **AI Copilot Agent Mode**（林涛 owner）：让 Copilot 能主动调用企业系统
        - **Search Multi-Tenant Isolation**（Ann owner）：索引层租户完全隔离
        - **Data Realtime Pipeline**（赵静 owner）：端到端延迟 < 30s

        ## Q3 规划
        - Copilot 私有化部署版本
        - Search 支持图片 / PDF 检索
        - Data Platform 引入 Iceberg 做 lakehouse

        ## Q4 规划（草案）
        - Copilot 行业版（金融 / 医疗）
        - Search 多语言检索优化
        - 合规：GDPR、CCPA 认证

        ## 依赖关系
        - Q2 Copilot Agent Mode 依赖 Search Multi-Tenant（先做完）
        - Q3 图片检索依赖 Data Platform 对象存储改造
    """).strip(),

    "planning/Q4-2025-retrospective.md": dedent("""
        # 2025 Q4 回顾

        ## 业务指标
        - MAU：45 万（QoQ +18%）
        - 企业客户：820 家（QoQ +12%）
        - ARR：$12.3M（QoQ +22%）

        ## 关键项目交付
        - Search Platform Hybrid Retriever（按时交付）
        - Data Platform BigQuery 迁移（延期 2 周，因 INC-2025-Q4-11）
        - AI Copilot 多轮对话（按时交付）

        ## 质量与稳定性
        - 总事故数：11 起（Q3：14 起，-21%）
        - 无 P0 事故
        - P1 事故 3 起，平均 MTTR 42 分钟

        ## 团队与招聘
        - 工程部规模：38 → 45
        - 新入职 Engineering Manager：林涛（AI Copilot）
        - 离职：1 人（搜索平台组初级工程师）

        ## 经验教训
        1. Data Platform 迁移应该拆成更小的阶段，降低单次变更风险
        2. On-call 体系在节假日前需要提前加强值守（见 INC-2025-Q4-11 复盘）
        3. 绩效校准会议应该更早启动，Q4 的评审延期到 1 月中旬才结束

        ## 高绩效员工（考核 >= 4.2）
        详见 `performance_reviews` 表 Q4 记录。公开致谢：
        - Ann（搜索平台组）：带团队按时交付 Hybrid Retriever
        - 赵静（数据平台组）：BigQuery 迁移复盘深入
        - Frank（数据平台组）：INC-2025-Q4-11 快速缓解
        - 周鹏（基础架构组）：全年 on-call 响应 100% SLA 达成
    """).strip(),

    "planning/Q1-2026-okrs.md": dedent("""
        # 2026 Q1 OKR

        ## Company-level
        - **O1**：AI Copilot 成为核心营收增长点
          - KR1: Copilot ARR > $3M
          - KR2: Copilot 客户满意度（NPS）> 40
          - KR3: 至少 5 家标杆客户案例

        - **O2**：夯实平台稳定性
          - KR1: P0 事故 = 0，P1 事故 <= 2 起
          - KR2: 所有生产服务 SLO 达成率 100%
          - KR3: on-call 响应 p95 < 10 分钟

        ## Engineering
        - **O3**：搜索平台租户隔离彻底完成
          - Owner: Ann
          - KR1: 索引层物理隔离（100% 客户迁移）
          - KR2: 查询层逻辑隔离 + 审计日志
          - KR3: 性能不劣化（p99 < 500ms）

        - **O4**：数据平台实时化提升
          - Owner: 赵静
          - KR1: 核心实时管道 E2E 延迟 < 30s
          - KR2: Flink 任务 >= 20 个覆盖关键场景
    """).strip(),

    # ---------------- Misc -----------------
    "misc/coffee-and-culture.md": dedent("""
        # 咖啡与文化

        ## 上海办公室咖啡吧
        位于 8 层茶水间。默认燕麦奶，每周新增一款单品豆。
        咖啡师：程晨（兼职）

        ## 传统
        - **周五 Happy Hour**：17:30 开始，工程部一般在 8 层
        - **月度 Demo Day**：每月最后一个周五，各团队 demo 新功能
        - **年度 Hackathon**：每年 Q4，48 小时，奖金池 10 万
        - **新人破冰**：每月一次，HR 组织

        ## 内部俚语
        - "page Ann"：出 bug 了
        - "赵静说行就行"：数据平台涉及 BigQuery 预算的事情
        - "走 1Password"：密钥相关走 Vault + 1Password 流程
    """).strip(),

    "misc/interview-guide.md": dedent("""
        # 工程师面试指南

        ## 流程
        1. 简历筛选（HR + hiring manager）
        2. 电话面试（30 min，技术基础 + 意愿）
        3. 技术面 1（60 min，算法 + 系统设计基础）
        4. 技术面 2（60 min，领域深度 + 过往项目）
        5. Bar raiser（60 min，跨团队资深工程师）
        6. HR 面（culture fit + 期望）

        ## 评估维度
        - Problem-solving
        - Technical depth（领域相关）
        - Communication
        - Ownership
        - Culture fit（好奇、务实、直接）

        ## 录用标准
        - 每个维度至少 3.0（5 分制）
        - Bar raiser 必须 >= 3.5
        - 至少 2/3 面试官推荐录用

        ## 面试官排班
        - 排班由 hiring manager 负责
        - 工程师每月参与面试不超过 4 场
    """).strip(),

    "misc/tooling-faq.md": dedent("""
        # 工具 FAQ

        ## Q: 为什么要用 VS Code / PyCharm，不能用 Vim 吗？
        A: 可以用。但团队协作的 debug / codelens / AI 补全 在 JetBrains / VS Code 体验更好。

        ## Q: 本地跑 Airflow 需要什么？
        A: Docker Compose 一键起，看 Data Platform 仓库的 README。联系赵静组获取测试数据。

        ## Q: Chroma 跟 Elasticsearch 什么关系？
        A: 互补。Chroma 做向量召回，ES 做关键词召回，最后 Hybrid Retriever 用 RRF 融合。
        详见 `tech/search-platform-architecture.md`。

        ## Q: 我发现 Vault 里某个 secret 过期了，怎么处理？
        A: 不要自己续期。联系孙伟（安全团队），走标准流程。避免留下没人知道的密钥。

        ## Q: on-call 期间突发家事怎么办？
        A: 立即在 #oncall 频道 @backup，通知 Manager。公司对家庭优先事项一直支持。
    """).strip(),

    # ---------------- English-only docs -----------------
    "misc/english-coffee-chat-culture.md": dedent("""
        # Coffee-Chat Culture

        At Acme, coffee chats are how cross-team knowledge actually spreads. We
        don't enforce them, but we highly encourage every engineer to have 2-3
        coffee chats per month outside their immediate team.

        ## What works
        - Ask about the other person's current pain points first
        - Share something specific you're working on, not a vague overview
        - Follow up with a specific ask or offer

        ## What doesn't work
        - Treating it as a networking transaction
        - Bringing up compensation comparisons
        - Trying to "sell" your team or project

        ## Who to chat with
        The onboarding checklist suggests 3 coffee chats in your first month.
        Good starting pairs:
        - Your cross-functional counterpart (eng ↔ product ↔ design)
        - Someone one level senior in another team
        - A new hire from another team who joined around the same time

        See also: `handbook/onboarding-checklist.md`.
    """).strip(),
}


# ============================================================ Graph triples


TRIPLETS: dict[str, list[dict]] = {
    "org.jsonl": [
        # Company → divisions → teams
        {"subject": "Acme Technologies", "relation": "has_division", "object": "Engineering"},
        {"subject": "Acme Technologies", "relation": "has_division", "object": "Product"},
        {"subject": "Acme Technologies", "relation": "has_division", "object": "Operations"},
        {"subject": "Acme Technologies", "relation": "has_division", "object": "G&A"},
        {"subject": "Acme Technologies", "relation": "headquartered_in", "object": "Shanghai"},

        {"subject": "Engineering", "relation": "contains_team", "object": "Search Platform"},
        {"subject": "Engineering", "relation": "contains_team", "object": "Data Platform"},
        {"subject": "Engineering", "relation": "contains_team", "object": "AI Platform"},
        {"subject": "Engineering", "relation": "contains_team", "object": "Infrastructure"},

        # Team leads
        {"subject": "Ann", "relation": "leads", "object": "Search Platform"},
        {"subject": "赵静", "relation": "leads", "object": "Data Platform"},
        {"subject": "林涛", "relation": "leads", "object": "AI Platform"},
        {"subject": "王明", "relation": "leads", "object": "Infrastructure"},

        # Reporting lines
        {"subject": "Ann", "relation": "reports_to", "object": "VP Engineering"},
        {"subject": "赵静", "relation": "reports_to", "object": "VP Engineering"},
        {"subject": "林涛", "relation": "reports_to", "object": "VP Engineering"},
        {"subject": "王明", "relation": "reports_to", "object": "VP Engineering"},

        # Team members
        {"subject": "Bob", "relation": "member_of", "object": "Search Platform"},
        {"subject": "陈诚", "relation": "member_of", "object": "Search Platform"},
        {"subject": "Dana", "relation": "member_of", "object": "Search Platform"},
        {"subject": "Frank", "relation": "member_of", "object": "Data Platform"},
        {"subject": "Grace", "relation": "member_of", "object": "Data Platform"},
        {"subject": "Henry", "relation": "member_of", "object": "Data Platform"},
        {"subject": "周鹏", "relation": "member_of", "object": "Infrastructure"},
        {"subject": "孙伟", "relation": "member_of", "object": "Infrastructure"},
        {"subject": "李慧", "relation": "member_of", "object": "Infrastructure"},

        # Locations
        {"subject": "Search Platform", "relation": "located_in", "object": "Shanghai"},
        {"subject": "Data Platform", "relation": "located_in", "object": "Beijing"},
        {"subject": "AI Platform", "relation": "located_in", "object": "Shanghai"},
        {"subject": "Infrastructure", "relation": "located_in", "object": "Shenzhen"},
    ],

    "products.jsonl": [
        # Products and their owners
        {"subject": "Search Platform Product", "relation": "owned_by", "object": "Search Platform"},
        {"subject": "Data Platform Product", "relation": "owned_by", "object": "Data Platform"},
        {"subject": "AI Copilot", "relation": "owned_by", "object": "AI Platform"},

        # Dependencies
        {"subject": "AI Copilot", "relation": "depends_on", "object": "Search Platform Product"},
        {"subject": "AI Copilot", "relation": "depends_on", "object": "Data Platform Product"},
        {"subject": "Search Platform Product", "relation": "depends_on", "object": "Data Platform Product"},

        # Sub-components
        {"subject": "Search Platform Product", "relation": "has_component", "object": "Chroma Cluster"},
        {"subject": "Search Platform Product", "relation": "has_component", "object": "Elasticsearch"},
        {"subject": "Search Platform Product", "relation": "has_component", "object": "Query Router"},
        {"subject": "Search Platform Product", "relation": "has_component", "object": "Hybrid Retriever"},

        {"subject": "Data Platform Product", "relation": "has_component", "object": "Kafka Cluster"},
        {"subject": "Data Platform Product", "relation": "has_component", "object": "BigQuery"},
        {"subject": "Data Platform Product", "relation": "has_component", "object": "ClickHouse"},
        {"subject": "Data Platform Product", "relation": "has_component", "object": "Airflow"},
        {"subject": "Data Platform Product", "relation": "has_component", "object": "Flink"},

        {"subject": "AI Copilot", "relation": "has_component", "object": "LLM Gateway"},
        {"subject": "AI Copilot", "relation": "has_component", "object": "RAG Orchestrator"},
        {"subject": "AI Copilot", "relation": "has_component", "object": "Tool Router"},

        # Model usage
        {"subject": "AI Copilot", "relation": "uses_model", "object": "Claude Sonnet 4.6"},
        {"subject": "AI Copilot", "relation": "uses_model", "object": "Claude Haiku 4.5"},
        {"subject": "AI Copilot", "relation": "uses_model", "object": "text-embedding-3-small"},
    ],

    "projects.jsonl": [
        # Q2 projects
        {"subject": "Copilot Agent Mode", "relation": "owner", "object": "林涛"},
        {"subject": "Copilot Agent Mode", "relation": "quarter", "object": "2026-Q2"},
        {"subject": "Copilot Agent Mode", "relation": "depends_on_project", "object": "Search Multi-Tenant Isolation"},

        {"subject": "Search Multi-Tenant Isolation", "relation": "owner", "object": "Ann"},
        {"subject": "Search Multi-Tenant Isolation", "relation": "quarter", "object": "2026-Q2"},

        {"subject": "Data Realtime Pipeline", "relation": "owner", "object": "赵静"},
        {"subject": "Data Realtime Pipeline", "relation": "quarter", "object": "2026-Q2"},

        # Q1 completed
        {"subject": "Hybrid Retriever Launch", "relation": "owner", "object": "Ann"},
        {"subject": "Hybrid Retriever Launch", "relation": "quarter", "object": "2026-Q1"},
        {"subject": "Hybrid Retriever Launch", "relation": "status", "object": "completed"},

        {"subject": "BigQuery Migration", "relation": "owner", "object": "赵静"},
        {"subject": "BigQuery Migration", "relation": "quarter", "object": "2025-Q4"},
        {"subject": "BigQuery Migration", "relation": "status", "object": "completed"},
        {"subject": "BigQuery Migration", "relation": "delayed_by_incident", "object": "INC-2025-Q4-11"},

        {"subject": "Multi-turn Conversation", "relation": "owner", "object": "林涛"},
        {"subject": "Multi-turn Conversation", "relation": "quarter", "object": "2026-Q1"},
        {"subject": "Multi-turn Conversation", "relation": "status", "object": "completed"},

        # Project contributors
        {"subject": "Bob", "relation": "contributed_to", "object": "Hybrid Retriever Launch"},
        {"subject": "陈诚", "relation": "contributed_to", "object": "Hybrid Retriever Launch"},
        {"subject": "Frank", "relation": "contributed_to", "object": "BigQuery Migration"},
        {"subject": "Grace", "relation": "contributed_to", "object": "BigQuery Migration"},
        {"subject": "Dana", "relation": "contributed_to", "object": "Search Multi-Tenant Isolation"},
    ],

    "incidents.jsonl": [
        {"subject": "INC-2025-Q3-07", "relation": "affected_product", "object": "Search Platform Product"},
        {"subject": "INC-2025-Q3-07", "relation": "severity", "object": "P1"},
        {"subject": "INC-2025-Q3-07", "relation": "responder", "object": "Ann"},
        {"subject": "INC-2025-Q3-07", "relation": "responder", "object": "Bob"},
        {"subject": "INC-2025-Q3-07", "relation": "duration_minutes", "object": "120"},

        {"subject": "INC-2025-Q4-11", "relation": "affected_product", "object": "Data Platform Product"},
        {"subject": "INC-2025-Q4-11", "relation": "severity", "object": "P1"},
        {"subject": "INC-2025-Q4-11", "relation": "responder", "object": "赵静"},
        {"subject": "INC-2025-Q4-11", "relation": "responder", "object": "Frank"},
        {"subject": "INC-2025-Q4-11", "relation": "duration_minutes", "object": "360"},

        {"subject": "INC-2026-Q1-03", "relation": "affected_product", "object": "AI Copilot"},
        {"subject": "INC-2026-Q1-03", "relation": "severity", "object": "P1"},
        {"subject": "INC-2026-Q1-03", "relation": "responder", "object": "林涛"},
        {"subject": "INC-2026-Q1-03", "relation": "duration_minutes", "object": "45"},

        {"subject": "INC-2026-Q2-02", "relation": "affected_product", "object": "Search Platform Product"},
        {"subject": "INC-2026-Q2-02", "relation": "severity", "object": "P2"},
        {"subject": "INC-2026-Q2-02", "relation": "responder", "object": "Dana"},
        {"subject": "INC-2026-Q2-02", "relation": "duration_minutes", "object": "30"},
    ],
}


# ============================================================ SQL seed


SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    head_name TEXT,
    location TEXT
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    department_id INTEGER REFERENCES departments(id),
    title TEXT,
    level INTEGER,                -- 1..7, IC level
    salary INTEGER,
    city TEXT,
    hire_date TEXT,               -- ISO yyyy-mm-dd
    manager_name TEXT,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    owner_name TEXT,              -- employee.name
    status TEXT,                  -- planning / in_progress / completed / cancelled
    quarter TEXT,                 -- yyyy-Qn
    planned_delivery TEXT,
    actual_delivery TEXT,
    priority TEXT                 -- P0 / P1 / P2
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER REFERENCES projects(id),
    employee_name TEXT,           -- employees.name
    role TEXT,                    -- lead / contributor / reviewer
    PRIMARY KEY (project_id, employee_name)
);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,          -- INC-yyyy-Qn-nn
    occurred_at TEXT,
    severity TEXT,                -- P0 / P1 / P2 / P3
    affected_service TEXT,
    duration_minutes INTEGER,
    responder_names TEXT,         -- comma-separated
    root_cause TEXT,
    quarter TEXT
);

CREATE TABLE IF NOT EXISTS performance_reviews (
    id INTEGER PRIMARY KEY,
    employee_name TEXT,
    review_period TEXT,           -- yyyy-H1 / yyyy-H2
    score REAL,                   -- 1.0..5.0
    summary TEXT
);
"""

SQL_SEED: list[str] = [
    # Departments (4)
    """INSERT INTO departments (id, name, head_name, location) VALUES
       (1, 'Engineering', 'VP Engineering', 'Shanghai'),
       (2, 'Product', 'Head of Product', 'Shanghai'),
       (3, 'Operations', 'Head of Ops', 'Shanghai'),
       (4, 'G&A', 'COO', 'Shanghai')""",

    # Employees (24)
    """INSERT INTO employees (id, name, department_id, title, level, salary, city, hire_date, manager_name) VALUES
       ( 1, 'Ann',    1, 'Engineering Manager', 6, 48000, 'Shanghai', '2021-03-01', 'VP Engineering'),
       ( 2, 'Bob',    1, 'Senior Engineer',     5, 32000, 'Shanghai', '2022-06-15', 'Ann'),
       ( 3, '陈诚',   1, 'Engineer',            4, 24000, 'Shanghai', '2023-09-01', 'Ann'),
       ( 4, 'Dana',   1, 'Engineer',            3, 19000, 'Shanghai', '2024-07-20', 'Ann'),
       ( 5, '赵静',   1, 'Engineering Manager', 6, 50000, 'Beijing',  '2020-11-10', 'VP Engineering'),
       ( 6, 'Frank',  1, 'Senior Engineer',     5, 33000, 'Beijing',  '2022-01-20', '赵静'),
       ( 7, 'Grace',  1, 'Senior Engineer',     5, 31000, 'Beijing',  '2022-08-05', '赵静'),
       ( 8, 'Henry',  1, 'Engineer',            4, 22000, 'Beijing',  '2024-02-12', '赵静'),
       ( 9, '林涛',   1, 'Engineering Manager', 6, 52000, 'Shanghai', '2025-08-01', 'VP Engineering'),
       (10, '王明',   1, 'Engineering Manager', 6, 47000, 'Shenzhen', '2021-07-15', 'VP Engineering'),
       (11, '周鹏',   1, 'Senior Engineer',     5, 30000, 'Shenzhen', '2022-11-01', '王明'),
       (12, '孙伟',   1, 'Senior Engineer',     5, 31000, 'Shenzhen', '2023-02-10', '王明'),
       (13, '李慧',   1, 'Engineer',            4, 23000, 'Shenzhen', '2024-04-20', '王明'),
       (14, 'Ivy',    2, 'Product Manager',     5, 34000, 'Shanghai', '2022-05-01', 'Head of Product'),
       (15, 'Jack',   2, 'Product Manager',     4, 26000, 'Shanghai', '2023-10-15', 'Head of Product'),
       (16, 'Karen',  2, 'Designer',            4, 22000, 'Shanghai', '2023-12-01', 'Head of Product'),
       (17, 'Leo',    3, 'Sales Lead',          5, 35000, 'Shanghai', '2022-03-10', 'Head of Ops'),
       (18, 'Mia',    3, 'Customer Success',    4, 23000, 'Shanghai', '2024-06-01', 'Head of Ops'),
       (19, 'Nick',   3, 'Marketing',           3, 18000, 'Shanghai', '2024-08-15', 'Head of Ops'),
       (20, 'Olivia', 4, 'HR Manager',          5, 30000, 'Shanghai', '2021-09-01', 'COO'),
       (21, 'Peter',  4, 'Finance',             4, 25000, 'Shanghai', '2023-03-10', 'COO'),
       (22, 'Queenie',4, 'Legal',               4, 28000, 'Shanghai', '2023-06-01', 'COO'),
       (23, '程晨',   4, 'Office',              2, 12000, 'Shanghai', '2024-01-15', 'COO'),
       (24, 'Steve',  1, 'Engineer',            3, 18000, 'Shanghai', '2025-11-01', 'Ann')
    """,

    # Projects (12)
    """INSERT INTO projects (id, name, owner_name, status, quarter, planned_delivery, actual_delivery, priority) VALUES
       ( 1, 'Hybrid Retriever Launch',        'Ann',   'completed',    '2026-Q1', '2026-03-15', '2026-03-12', 'P1'),
       ( 2, 'BigQuery Migration',             '赵静',  'completed',    '2025-Q4', '2025-12-10', '2025-12-24', 'P0'),
       ( 3, 'Multi-turn Conversation',        '林涛',  'completed',    '2026-Q1', '2026-03-20', '2026-03-18', 'P1'),
       ( 4, 'Search Multi-Tenant Isolation',  'Ann',   'in_progress',  '2026-Q2', '2026-06-30', NULL,         'P0'),
       ( 5, 'Copilot Agent Mode',             '林涛',  'in_progress',  '2026-Q2', '2026-06-30', NULL,         'P0'),
       ( 6, 'Data Realtime Pipeline',         '赵静',  'in_progress',  '2026-Q2', '2026-06-30', NULL,         'P1'),
       ( 7, 'Chroma Sharding Upgrade',        'Bob',   'in_progress',  '2026-Q2', '2026-05-31', NULL,         'P2'),
       ( 8, 'Vault Rotation Automation',      '孙伟',  'planning',     '2026-Q3', '2026-09-30', NULL,         'P2'),
       ( 9, 'Grafana Dashboards Revamp',      '周鹏',  'planning',     '2026-Q3', '2026-09-30', NULL,         'P2'),
       (10, 'Airflow 2.x Upgrade',            'Frank', 'completed',    '2025-Q3', '2025-09-10', '2025-09-08', 'P1'),
       (11, 'GDPR Compliance',                'Queenie','in_progress', '2026-Q2', '2026-06-30', NULL,         'P1'),
       (12, 'Onboarding Portal v2',           'Ivy',   'planning',     '2026-Q3', '2026-09-15', NULL,         'P2')
    """,

    # project_members (project contributors + lead duplicates)
    """INSERT INTO project_members (project_id, employee_name, role) VALUES
       (1,  'Ann',     'lead'),
       (1,  'Bob',     'contributor'),
       (1,  '陈诚',    'contributor'),
       (1,  'Dana',    'reviewer'),
       (2,  '赵静',    'lead'),
       (2,  'Frank',   'contributor'),
       (2,  'Grace',   'contributor'),
       (2,  'Henry',   'contributor'),
       (3,  '林涛',    'lead'),
       (4,  'Ann',     'lead'),
       (4,  'Dana',    'contributor'),
       (4,  'Bob',     'reviewer'),
       (5,  '林涛',    'lead'),
       (5,  'Steve',   'contributor'),
       (6,  '赵静',    'lead'),
       (6,  'Frank',   'contributor'),
       (6,  'Grace',   'contributor'),
       (7,  'Bob',     'lead'),
       (7,  '陈诚',    'contributor'),
       (8,  '孙伟',    'lead'),
       (9,  '周鹏',    'lead'),
       (9,  '李慧',    'contributor'),
       (10, 'Frank',   'lead'),
       (10, 'Grace',   'contributor'),
       (11, 'Queenie', 'lead'),
       (12, 'Ivy',     'lead'),
       (12, 'Karen',   'contributor')
    """,

    # incidents (12)
    """INSERT INTO incidents (id, occurred_at, severity, affected_service, duration_minutes, responder_names, root_cause, quarter) VALUES
       ('INC-2025-Q3-05', '2025-07-22 09:15:00', 'P2', 'Data Platform',    60,  '赵静,Frank',       'Kafka consumer lag', '2025-Q3'),
       ('INC-2025-Q3-07', '2025-08-14 14:30:00', 'P1', 'Search Platform', 120,  'Ann,Bob',          'Chroma OOM under spike traffic', '2025-Q3'),
       ('INC-2025-Q3-12', '2025-09-03 11:00:00', 'P2', 'Infrastructure',   45,  '王明,周鹏',        'Grafana instance restart', '2025-Q3'),
       ('INC-2025-Q4-02', '2025-10-18 20:45:00', 'P2', 'AI Copilot',       40,  '林涛',             'Timeout config misaligned', '2025-Q4'),
       ('INC-2025-Q4-06', '2025-11-05 16:20:00', 'P2', 'Search Platform',  35,  'Bob,陈诚',         'Query router bug', '2025-Q4'),
       ('INC-2025-Q4-09', '2025-11-28 13:00:00', 'P1', 'Search Platform',  75,  'Ann,Dana',         'Elasticsearch certificate expiry', '2025-Q4'),
       ('INC-2025-Q4-11', '2025-12-12 03:20:00', 'P1', 'Data Platform',   360,  '赵静,Frank,Grace', 'BigQuery migration cutover failure', '2025-Q4'),
       ('INC-2026-Q1-03', '2026-01-22 10:05:00', 'P1', 'AI Copilot',       45,  '林涛',             'Rate-limit config error', '2026-Q1'),
       ('INC-2026-Q1-08', '2026-02-14 22:00:00', 'P2', 'Data Platform',    80,  'Grace,Henry',      'Flink backpressure', '2026-Q1'),
       ('INC-2026-Q2-02', '2026-04-10 09:00:00', 'P2', 'Search Platform',  30,  'Dana',             'Stale cache for tenant X', '2026-Q2'),
       ('INC-2026-Q2-05', '2026-04-28 15:30:00', 'P2', 'Infrastructure',   25,  '周鹏',             'Prometheus disk full', '2026-Q2'),
       ('INC-2026-Q2-09', '2026-05-03 19:00:00', 'P3', 'AI Copilot',       15,  '林涛',             'Non-critical log noise', '2026-Q2')
    """,

    # performance_reviews (everyone for 2025-H2, a subset for 2026-H1 in progress)
    """INSERT INTO performance_reviews (id, employee_name, review_period, score, summary) VALUES
       ( 1, 'Ann',    '2025-H2', 4.5, 'Led Hybrid Retriever delivery on time. Strong team building.'),
       ( 2, 'Bob',    '2025-H2', 4.2, 'Solid senior IC, owned retrieval-quality improvements.'),
       ( 3, '陈诚',   '2025-H2', 3.7, 'Reliable contributor, on-call responsive.'),
       ( 4, 'Dana',   '2025-H2', 3.5, 'Growing steadily in first year.'),
       ( 5, '赵静',   '2025-H2', 4.3, 'BigQuery migration done despite INC-2025-Q4-11 impact. Clear-headed in crisis.'),
       ( 6, 'Frank',  '2025-H2', 4.4, 'Hero of INC-2025-Q4-11 mitigation; Airflow 2.x upgrade lead.'),
       ( 7, 'Grace',  '2025-H2', 4.0, 'Solid senior, consistent delivery.'),
       ( 8, 'Henry',  '2025-H2', 3.4, 'First half, learning curve. Expected trajectory.'),
       ( 9, '林涛',   '2025-H2', 3.8, 'Onboarded mid-year as EM. Early signs positive.'),
       (10, '王明',   '2025-H2', 4.0, 'Infrastructure reliable. Team culture strong.'),
       (11, '周鹏',   '2025-H2', 4.3, '100% on-call SLA all year. Exemplary.'),
       (12, '孙伟',   '2025-H2', 3.9, 'Vault migration on track, security awareness improving across org.'),
       (13, '李慧',   '2025-H2', 3.4, 'First full year, building up skills.'),
       (14, 'Ivy',    '2025-H2', 4.1, 'Clear PM vision, drove Copilot prioritisation.'),
       (15, 'Jack',   '2025-H2', 3.6, 'Good execution, needs to raise bar on strategic framing.'),
       (16, 'Karen',  '2025-H2', 3.8, 'Design system v2 shipped smoothly.'),
       (17, 'Leo',    '2025-H2', 4.0, 'Q4 revenue beat plan by 12%.'),
       (18, 'Mia',    '2025-H2', 3.9, 'Customer NPS trending up.'),
       (19, 'Nick',   '2025-H2', 3.3, 'First year marketing, learning.'),
       (20, 'Olivia', '2025-H2', 4.1, 'Hiring pipeline strong, culture keeper.'),
       (21, 'Peter',  '2025-H2', 3.7, 'Finance controls tight.'),
       (22, 'Queenie','2025-H2', 3.6, 'GDPR prep underway, on track.')
    """,
]


# ============================================================ main


def _reset_profile(settings) -> None:
    data_dir = settings.data.data_dir
    storage_dir = settings.data.storage_dir
    for p in list(data_dir.iterdir()) if data_dir.exists() else []:
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    for name in ("chroma", "chroma_skills", "demo.db", "graph.json", "memory.db"):
        t = storage_dir / name
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        elif t.exists():
            t.unlink()


def _write_kb_docs(data_dir: Path) -> int:
    count = 0
    for rel_path, body in KB_DOCS.items():
        target = data_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body + "\n", encoding="utf-8")
        count += 1
    return count


def _write_triplets(data_dir: Path) -> tuple[int, int]:
    triplets_dir = data_dir / "triplets"
    triplets_dir.mkdir(exist_ok=True)
    files = 0
    edges = 0
    for name, rows in TRIPLETS.items():
        (triplets_dir / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8",
        )
        files += 1
        edges += len(rows)
    return files, edges


async def _seed_db(agent) -> dict[str, int]:
    """Apply schema + rows to the profile's SQLite DB."""
    with agent.db.engine.begin() as conn:
        # Drop then recreate so schema changes propagate cleanly.
        for t in ("performance_reviews", "incidents", "project_members",
                  "projects", "employees", "departments"):
            conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
        for stmt in SQL_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
        for stmt in SQL_SEED:
            conn.execute(text(stmt))

    # Row counts for the summary.
    counts: dict[str, int] = {}
    with agent.db.engine.begin() as conn:
        for t in ("departments", "employees", "projects", "project_members",
                  "incidents", "performance_reviews"):
            row = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).fetchone()
            counts[t] = int(row[0]) if row else 0
    return counts


async def _main() -> int:
    # Mirror LLM creds into embedding if not set separately.
    for src, dst in (
        ("DATAMIND__LLM__API_BASE", "DATAMIND__EMBEDDING__API_BASE"),
        ("DATAMIND__LLM__API_KEY", "DATAMIND__EMBEDDING__API_KEY"),
    ):
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]

    os.environ["DATAMIND__DATA__PROFILE"] = PROFILE

    if not os.environ.get("DATAMIND__LLM__API_KEY"):
        print("[seed] DATAMIND__LLM__API_KEY not set", file=sys.stderr)
        return 1

    from datamind.agent import build_datamind
    from datamind.config import Settings
    from datamind.core.logging import setup_logging

    setup_logging("WARNING")
    settings = Settings()
    settings.ensure_dirs()

    print(f"[seed] profile={PROFILE}")
    print(f"[seed] data_dir={settings.data.data_dir}")
    print(f"[seed] storage_dir={settings.data.storage_dir}")

    _reset_profile(settings)
    settings.ensure_dirs()

    # KB
    kb_count = _write_kb_docs(settings.data.data_dir)
    print(f"[seed] KB docs written: {kb_count}")

    # Graph triples
    t_files, t_edges = _write_triplets(settings.data.data_dir)
    print(f"[seed] Graph triples written: {t_edges} edges across {t_files} files")

    # Build the two-agent system (also initialises DB engine + graph store).
    system = await build_datamind(settings)
    await system.warmup()

    # DB
    db_counts = await _seed_db(system.retrieve)
    print(f"[seed] DB rows: {db_counts}")

    # KB reindex (embeds every chunk via the gateway).
    print("[seed] Indexing KB (this calls the embedding gateway)...")
    receipt = await system.store.tools.get("kb_reindex").handler()
    print(f"[seed] KB receipt: {receipt}")

    print(f"[seed] Graph stats: {system.services.graph.stats()}")
    print(
        f"[seed] Tools registered: retrieve={len(system.retrieve.tools)} "
        f"store={len(system.store.tools)}"
    )
    print(f"\n[seed] OK — profile '{PROFILE}' ready.")
    print(f"       DATAMIND__DATA__PROFILE={PROFILE} python -m datamind chat")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
