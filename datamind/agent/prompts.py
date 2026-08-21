"""Default system prompt template.

We keep the prompt in Python (not a .md file under .claude/) so it can be
unit-tested and composed from config. The template leaves a slot for the
tool manifest description so the agent knows what it has without us having
to repeat each tool's JSON schema.

Design goals:
- Chinese user-facing answers (per user preference)
- Prefer tool calls over guessing (especially for KB/DB)
- Cite sources when retrieval is involved
- Be concise — one turn can easily exceed 4k tokens without discipline
"""
from __future__ import annotations

from typing import Iterable

from datamind.core.tools import ToolSpec


_RETRIEVE_TEMPLATE = """你是 DataMind 的 RetrieveAgent，负责在推理时从五个数据面取数并回答问题。

# 能力总览
你拥有以下几类工具:
{tool_groups}

# 工具使用原则
1. **最小充分数据面**: 只调用回答问题所必需的数据面。表格问题优先 DB，实体关系优先 Graph，文档原文优先 KB；不要为了交叉验证而默认把所有工具都调用一遍。
2. **已有精确定位时直接细查**: 已知 table、entity id 或 source 时可直接 describe/query/neighbors/search；只有定位不明确时才先用 list/search 类工具。
3. **证据不足才扩面**: 当前结果为空、报错或确实缺少另一数据面的字段时，才扩大检索范围。获得足够证据后立即停止调用工具并综合答案；不要重复相同参数的成功调用。
4. **严格只读**: 你没有任何存数、修改或删除工具。缺少数据时明确返回 missing_data，不要假装已经保存或修改。
5. **引用来源**: 使用 kb_search / db_query_nl / graph_traverse 后，简要说明结论来自哪个源（文件名 / 表名 / 实体路径）。

# 回答风格
- 默认用中文回答，保持简洁。
- 需要呈现结构化数据（表、路径、列表）时使用 Markdown。
- 遇到无法回答或工具返回空结果的情况，直接说"没有找到相关信息"并说明尝试了哪些工具。
"""


_STORE_TEMPLATE = """你是 DataMind 的 StoreAgent，唯一职责是把数据安全、准确地存入五个数据面。

# 可写数据面
{tool_groups}

# 存数原则
1. **直接选择工具**: 根据数据形态和用户意图直接调用合适工具，不要生成 DataPlan、DataOp、DAG 或执行计划。
2. **允许多面写入**: 同一来源可以写入多个面，例如文档正文进入 KB，明确关系同时进入 Graph。
3. **语义边界**:
   - KB: 非结构化文档和可检索正文。
   - DB: CSV 等结构化记录。
   - Graph: 实体关系和依赖。
   - Skills: 可复用 SOP、方法和操作规范，不存普通事实。
   - Memory: 用户偏好、决定、会话或 profile 事实。
4. **回执优先**: 每次工具调用都会返回 IngestReceipt。检查 status；failed 时可以修正参数重试，unchanged 表示幂等命中。
5. **不回答业务问题**: 你可以简要汇总存数结果，但不负责检索、分析或生成领域答案。

# 输出
默认用中文，列出写入的数据面、数量、receipt_id 和失败项。
"""


def _group(specs: Iterable[ToolSpec]) -> dict[str, list[ToolSpec]]:
    g: dict[str, list[ToolSpec]] = {}
    for s in specs:
        label = s.metadata.get("group", "other")
        g.setdefault(label, []).append(s)
    return g


_GROUP_LABEL = {
    "kb": "知识库 (kb_*)",
    "db": "数据库 (db_*)",
    "graph": "图谱 (graph_*)",
    "memory": "长期记忆 (memory_*)",
    "skill.knowledge": "知识型技能 (skill_*)",
    "skill.code": "通用小工具",
    "skill.store": "技能写入 (skill_upsert)",
    "ingest": "数据导入 (kb_add_* / db_import_* / graph_add_*)",
    "other": "其他",
}


def _tool_group_lines(specs: Iterable[ToolSpec]) -> str:
    grouped = _group(specs)
    lines: list[str] = []
    for label in [
        "kb", "graph", "db", "skill.knowledge", "skill.code", "skill.store", "memory", "ingest"
    ]:
        if label not in grouped:
            continue
        friendly = _GROUP_LABEL.get(label, label)
        names = ", ".join(s.name for s in grouped[label])
        lines.append(f"- {friendly}: {names}")
    for label, specs_list in grouped.items():
        if label in {
            "kb", "graph", "db", "skill.knowledge", "skill.code", "skill.store", "memory", "ingest"
        }:
            continue
        names = ", ".join(s.name for s in specs_list)
        lines.append(f"- {_GROUP_LABEL.get(label, label)}: {names}")
    return "\n".join(lines) if lines else "(暂无可用工具)"


def build_retrieve_system_prompt(specs: Iterable[ToolSpec]) -> str:
    return _RETRIEVE_TEMPLATE.format(tool_groups=_tool_group_lines(specs))


def build_store_system_prompt(specs: Iterable[ToolSpec]) -> str:
    return _STORE_TEMPLATE.format(tool_groups=_tool_group_lines(specs))


def build_system_prompt(specs: Iterable[ToolSpec]) -> str:
    """Backward-compatible alias for the RetrieveAgent prompt."""
    return build_retrieve_system_prompt(specs)


__all__ = [
    "build_system_prompt",
    "build_retrieve_system_prompt",
    "build_store_system_prompt",
]
