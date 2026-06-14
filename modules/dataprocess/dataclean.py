import argparse
import json
import os
import sys
from functools import lru_cache
from pathlib import Path

from tqdm import tqdm

# 允许从仓库根目录直接运行脚本（无需安装为包）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings


@lru_cache(maxsize=1)
def _load_clean_prompt() -> str:
    candidate_paths = [
        Path(__file__).resolve().parent.parent / "prompts" / "Data_clean_prompt.txt",
        PROJECT_ROOT / "prompts" / "Data_clean_prompt.txt",
    ]
    for prompt_path in candidate_paths:
        if prompt_path.exists():
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read().strip()
    raise FileNotFoundError(
        "未找到 Data_clean_prompt.txt，请确认存在于 modules/prompts/ 或 prompts/ 目录。"
    )


@lru_cache(maxsize=1)
def _get_cleaner_client():
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError("缺少依赖 openai，请先安装：pip install openai") from exc
    return OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_api_base or None)


def _get_cleaner_model_name() -> str:
    env_model = os.getenv("DATACLEAN_MODEL_NAME", "").strip()
    if env_model:
        return env_model
    return settings.llm_model or "gpt-4o-mini"


def _strip_response_wrappers(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]).strip()
    return cleaned


def clean_text_with_llm(raw_text: str) -> str:
    """使用 DataMind 配置的 OpenAI 兼容接口清洗文本。"""
    if not isinstance(raw_text, str) or raw_text.strip() == "":
        return raw_text

    if not (settings.llm_api_key or "").strip():
        raise ValueError("llm_api_key 未配置，无法调用 LLM 清洗文本。")

    system_prompt = _load_clean_prompt()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": raw_text},
    ]

    client = _get_cleaner_client()
    response = client.chat.completions.create(
        model=_get_cleaner_model_name(),
        messages=messages,
        temperature=0,
        max_tokens=4096,
    )
    response_text = (response.choices[0].message.content or "").strip()
    cleaned = _strip_response_wrappers(response_text)

    # 防止模型偶发返回空串，避免丢失原始内容
    return cleaned if cleaned else raw_text


def should_skip_existing(input_file: Path, output_file: Path) -> bool:
    """
    判断是否应跳过已存在的输出文件。
    条件：输出文件存在，且其内部 page 数量与输入文件相同。
    """
    if not output_file.exists():
        return False

    try:
        with open(input_file, "r", encoding="utf-8") as f_in:
            input_data = json.load(f_in)
        with open(output_file, "r", encoding="utf-8") as f_out:
            output_data = json.load(f_out)
    except (json.JSONDecodeError, IOError):
        return False  # 文件损坏则重新处理

    # 确保都是列表，且长度一致
    if isinstance(input_data, list) and isinstance(output_data, list):
        return len(input_data) == len(output_data)
    return False


def process_single_file(input_path: Path, output_path: Path) -> None:
    """
    处理单个 JSON 文件：
    1. 读取原始 JSON
    2. 清洗所有 text 字段
    3. 写入输出路径
    """
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 确保数据格式正确
    if not isinstance(data, list):
        print(f"警告：{input_path} 不是预期的列表格式，跳过")
        return

    cleaned_data = []
    for item in data:
        if not isinstance(item, dict) or "text" not in item:
            # 保留不符合格式的项原样
            cleaned_data.append(item)
            continue

        # 深拷贝一份，避免修改原数据
        new_item = item.copy()
        raw_text = item.get("text", "")
        cleaned_text = clean_text_with_llm(raw_text)
        new_item["text"] = cleaned_text
        cleaned_data.append(new_item)

    # 确保输出目录存在
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(cleaned_data, f, ensure_ascii=False, indent=2)


def process_wrapper(input_file: Path, output_root: Path, input_root: Path) -> str:
    rel_path = input_file.relative_to(input_root)
    output_file = output_root / rel_path
    if should_skip_existing(input_file, output_file):
        return "skipped"
    process_single_file(input_file, output_file)
    return "done"


def main() -> None:
    parser = argparse.ArgumentParser(description="批量清洗 OCR JSON 中的 text 字段")
    parser.add_argument(
        "--input_root",
        type=str,
        required=True,
        help="输入根目录，包含多个子文件夹的 JSON 文件",
    )
    parser.add_argument(
        "--output_root",
        type=str,
        required=True,
        help="输出根目录，保持相同的子目录结构",
    )
    args = parser.parse_args()

    input_root = Path(args.input_root)
    output_root = Path(args.output_root)

    if not input_root.exists():
        print(f"错误：输入目录 {input_root} 不存在")
        return

    # 收集所有 JSON 文件
    json_files = list(input_root.rglob("*.json"))
    print(f"找到 {len(json_files)} 个 JSON 文件")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(process_wrapper, f, output_root, input_root): f
            for f in json_files
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="处理进度"):
            future.result()
            
if __name__ == "__main__":
    main()