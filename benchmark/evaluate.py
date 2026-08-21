"""
Benchmark 评估脚本 —— 对比生成答案与标准答案

支持两种匹配指标:
  - Exact Match (EM): 标准答案是否完整出现在生成答案中（大小写不敏感）
  - F1 Score: 基于 token 重叠的 F1（适用于答案较长 / 表述不一致的场景）

用法:
  python -m benchmark.evaluate benchmark_results.json
  python -m benchmark.evaluate benchmark_results.json --output eval_report.json

可选:
  --golden  额外计算 golden 指标:
            - semantic_similarity (embedding cosine similarity)
            - factual_correctness_f1 (claim-based NLI)
"""

import argparse
import json
import os
import re
import sys
from collections import Counter


def _normalize(text: str) -> str:
    """统一大小写、去除多余空白和标点。"""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _tokenize(text: str) -> list[str]:
    return _normalize(text).split()


def exact_match(prediction: str, reference: str) -> bool:
    """严格比较 normalize 后的完整答案。"""
    return _normalize(reference) == _normalize(prediction)


def load_results(path: str) -> list[dict]:
    """Accept either the current checkpoint JSONL or legacy JSON arrays."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not isinstance(parsed, list) or not all(isinstance(row, dict) for row in parsed):
        raise ValueError("results must be a JSON array or JSONL objects")
    return parsed


def token_f1(prediction: str, reference: str) -> float:
    pred_tokens = _tokenize(prediction)
    ref_tokens = _tokenize(reference)
    if not ref_tokens:
        return 1.0 if not pred_tokens else 0.0
    if not pred_tokens:
        return 0.0

    common = Counter(pred_tokens) & Counter(ref_tokens)
    num_common = sum(common.values())
    if num_common == 0:
        return 0.0

    precision = num_common / len(pred_tokens)
    recall = num_common / len(ref_tokens)
    return 2 * precision * recall / (precision + recall)


def _load_env_file(env_path: str) -> None:
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _read_datamind_env() -> dict[str, str | None]:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    env_path = os.path.join(repo_root, "DataMind", ".env")
    _load_env_file(env_path)
    return {
        "llm_model": os.getenv("LLM_MODEL"),
        "llm_api_key": os.getenv("LLM_API_KEY"),
        "llm_api_base": os.getenv("LLM_API_BASE") or None,
        "embedding_model": os.getenv("EMBEDDING_MODEL"),
        "embedding_api_key": os.getenv("EMBEDDING_API_KEY"),
        "embedding_api_base": os.getenv("EMBEDDING_API_BASE") or None,
    }





def evaluate(results: list[dict], include_golden: bool = False) -> dict:
    """对结果列表做评估，返回汇总 + 逐条明细。"""
    semantic_metric = None
    factual_metric = None
    if include_golden:
        env = _read_datamind_env()
        if not env.get("llm_api_key") or not env.get("embedding_api_key"):
            raise ValueError("Missing LLM_API_KEY or EMBEDDING_API_KEY in environment/.env")

        from benchmark.metrics.golden_answer_metrics import (
            FactualCorrectnessMetric,
            SemanticSimilarityMetric,
        )
        from benchmark.models.embedding_models import OpenAIEmbeddingModel
        from benchmark.models.llm_judges import OpenAIModel

        llm_model = OpenAIModel(
            model_options={
                "name": env.get("llm_model") or "gpt-4o-mini",
                "api_key": env["llm_api_key"],
                "base_url": env["llm_api_base"],
            }
        )
        embedding_model = OpenAIEmbeddingModel(
            model_options={
                "name": env.get("embedding_model") or "text-embedding-3-small",
                "api_key": env["embedding_api_key"],
                "base_url": env["embedding_api_base"],
            }
        )
        semantic_metric = SemanticSimilarityMetric(embedding_model)
        factual_metric = FactualCorrectnessMetric(llm_model)

    evaluated = []
    em_correct = 0
    f1_sum = 0.0
    semantic_sum = 0.0
    factual_sum = 0.0
    total = 0

    for r in results:
        ref = r.get("reference_answer")
        if ref is None:
            continue
        total += 1
        answer = r.get("answer", "")
        em = exact_match(answer, ref)
        f1 = token_f1(answer, ref)
        if em:
            em_correct += 1
        f1_sum += f1

        item = {
            "index": r.get("index"),
            "question_id": r.get("question_id"),
            "question": r["question"],
            "answer": answer,
            "reference_answer": ref,
            "exact_match": em,
            "f1": round(f1, 4),
        }

        if include_golden and semantic_metric is not None and factual_metric is not None:
            semantic = float(semantic_metric.compute(r["question"], answer, ref).get("semantic_similarity", 0.0))
            factual_f1 = float(factual_metric.compute(r["question"], answer, ref).get("factual_correctness_f1", 0.0))
            item["semantic_similarity"] = round(semantic, 6)
            item["factual_correctness_f1"] = round(factual_f1, 6)
            semantic_sum += semantic
            factual_sum += factual_f1

        evaluated.append(item)

    summary = {
        "total": total,
        "exact_match_count": em_correct,
        "exact_match_rate": round(em_correct / total, 4) if total else 0,
        "avg_f1": round(f1_sum / total, 4) if total else 0,
    }
    if include_golden:
        summary["semantic_similarity_avg"] = round(semantic_sum / total, 6) if total else 0.0
        summary["factual_correctness_f1_avg"] = round(factual_sum / total, 6) if total else 0.0
    return {"summary": summary, "details": evaluated}


def _print_report(report: dict):
    s = report["summary"]
    print("\n" + "=" * 55)
    print("  Evaluation Report")
    print("=" * 55)
    print(f"  Total evaluated:   {s['total']}")
    print(f"  Exact Match:       {s['exact_match_count']}/{s['total']} ({s['exact_match_rate']*100:.1f}%)")
    print(f"  Avg Token-F1:      {s['avg_f1']:.4f}")
    if "semantic_similarity_avg" in s:
        print(f"  Semantic Sim:      {s['semantic_similarity_avg']:.6f}")
    if "factual_correctness_f1_avg" in s:
        print(f"  Factual F1:        {s['factual_correctness_f1_avg']:.6f}")
    print("=" * 55)

    details = report["details"]
    wrong = [d for d in details if not d["exact_match"]]
    if wrong:
        print(f"\n  ✗ 未命中的问题 ({len(wrong)} 条):\n")
        for d in wrong[:20]:
            print(f"    Q: {d['question']}")
            print(f"    A: {d['answer'][:120]}{'...' if len(d['answer']) > 120 else ''}")
            print(f"    R: {d['reference_answer']}")
            print(f"    F1={d['f1']:.4f}")
            print()


def main():
    parser = argparse.ArgumentParser(description="DataMind Benchmark Evaluator")
    parser.add_argument("results", help="benchmark runner 输出的 JSON 文件路径")
    parser.add_argument("--output", default=None, help="评估报告输出路径 (JSON)")
    parser.add_argument("--golden", action="store_true", help="额外计算 golden 指标 (semantic similarity + factual correctness)")
    args = parser.parse_args()

    results = load_results(args.results)

    has_ref = sum(1 for r in results if "reference_answer" in r)
    if has_ref == 0:
        print("[ERROR] 结果文件中没有 reference_answer 字段，无法评估。")
        print("        请确认问题集 JSONL 包含 reference_answer，并使用新版 benchmark runner 重跑。")
        sys.exit(1)

    report = evaluate(results, include_golden=args.golden)
    _print_report(report)

    out_path = args.output or args.results.replace(".json", "_eval.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[INFO] 评估报告已保存到: {out_path}")


if __name__ == "__main__":
    main()
