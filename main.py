"""
DataMind 入口 (命令行模式)

功能:
  - RAG 向量检索 (Chroma)
  - GraphRAG 知识图谱检索 (NetworkX)
  - Database 自然语言查询 (SQLite NL2SQL)
  - Knowledge Skills 技能知识检索
  - 对话记忆 (短期+长期)

用法:
  1. 将文档放入 data/profiles/default/ 目录
  2. 复制 .env.example 为 .env 并填入你的 API Key
  3. pip install -r requirements-legacy.txt
  4. python main.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.bootstrap import initialize
from modules.memory.memory import create_memory


async def chat_loop(agent, memory, state=None):
    """交互式对话循环"""
    print("\n" + "=" * 60)
    print("  DataMind 智能助手")
    print("  工具: RAG | GraphRAG | Database | Skills")
    print("-" * 60)
    print("  输入问题开始对话")
    print("  输入 'quit' 退出")
    print("  输入 'rebuild' 重建所有索引")
    print("=" * 60 + "\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit", "q"):
            print("再见!")
            break

        if user_input.lower() == "rebuild":
            from config import settings
            print(f"[INFO] 请删除 storage/{settings.data_profile}/ 目录后重启程序以重建索引")
            continue

        try:
            if state is not None:
                state.last_retrieved_images = []
            response = await agent.run(user_input, memory=memory)
            print(f"\nAssistant: {response}\n")
            if state and state.last_retrieved_images:
                print(f"[图片] 检索到 {len(state.last_retrieved_images)} 张相关图片:")
                for p in state.last_retrieved_images:
                    print(f"  - {p}")
                print()
        except Exception as e:
            print(f"\n[ERROR] {e}\n")
            print("提示: 请检查 .env 中的 API 配置是否正确\n")


def main():
    state = initialize()
    memory = create_memory()
    asyncio.run(chat_loop(state.agent, memory, state))


if __name__ == "__main__":
    main()
