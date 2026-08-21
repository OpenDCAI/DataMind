"""Reliable benchmark runner for the current ``datamind/`` stack.

This is intentionally dataset-neutral. Dataset-specific adapters and scorers
live outside the product repository and can consume the JSONL records emitted
here.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from datamind import __version__
from datamind.agent import DataMind, build_datamind
from datamind.config import Settings


def load_questions(filepath: str | Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    with Path(filepath).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                item = {"question": line}
            if not isinstance(item, dict) or not str(item.get("question", "")).strip():
                raise ValueError(f"invalid question at line {line_number}")
            item.setdefault("task_id", item.get("question_id") or str(line_number - 1))
            task_id = str(item["task_id"])
            if task_id in task_ids:
                raise ValueError(f"duplicate task_id {task_id!r} at line {line_number}")
            task_ids.add(task_id)
            items.append(item)
    return items


def _sha256_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _git_commit(repo: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _prompt_hash(system: DataMind) -> str:
    prompt = getattr(getattr(system.retrieve.loop, "_cfg", None), "system_prompt", "")
    return hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()


def build_provenance(
    *,
    settings: Settings,
    system: DataMind,
    questions_path: Path,
    surfaces: set[str] | None,
    run_id: str,
) -> dict[str, Any]:
    repo = Path(__file__).resolve().parent.parent
    index_manifest_path = settings.data.storage_dir / "kb_index_manifest.json"
    index_fingerprint = None
    if index_manifest_path.is_file():
        try:
            index_fingerprint = json.loads(
                index_manifest_path.read_text(encoding="utf-8")
            ).get("fingerprint")
        except (OSError, json.JSONDecodeError):
            index_fingerprint = "invalid"
    config = {
        "llm": {
            "protocol": settings.llm.protocol,
            "requested_model": settings.llm.model,
            "fallback_protocol": settings.llm.fallback_protocol or settings.llm.protocol,
            "fallback_model": settings.llm.fallback_model,
            "temperature": settings.llm.temperature,
        },
        "embedding": {
            "provider": settings.embedding.provider,
            "model": settings.embedding.model,
            "dimension": (
                system.services.embedding.dimension
                if system.services.embedding is not None
                else settings.embedding.dimension
            ),
            "index_fingerprint": index_fingerprint,
        },
        "retrieval": settings.retrieval.model_dump(),
        "agent_budgets": {
            "max_turns": settings.agent.max_turns,
            "max_tool_calls": settings.agent.max_tool_calls,
            "max_input_tokens": settings.agent.max_input_tokens,
            "max_tool_result_chars": settings.agent.max_tool_result_chars,
            "max_tool_result_rows": settings.agent.max_tool_result_rows,
            "wall_clock_timeout_s": settings.agent.wall_clock_timeout_s,
        },
        "profile": settings.data.profile,
        "surfaces": sorted(surfaces) if surfaces is not None else "default",
    }
    question_bytes = questions_path.read_bytes()
    return {
        "schema_version": 1,
        "run_id": run_id,
        "datamind_version": __version__,
        "datamind_commit": _git_commit(repo),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "questions_path": str(questions_path.resolve()),
        "questions_sha256": hashlib.sha256(question_bytes).hexdigest(),
        "prompt_sha256": _prompt_hash(system),
        "config": config,
        "config_fingerprint": _sha256_json(config),
    }


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _append_jsonl_durable(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _load_completed(path: Path, run_id: str) -> set[str]:
    completed: set[str] = set()
    if not path.is_file():
        return completed
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"corrupt checkpoint line {line_number}: {exc}") from exc
            if row.get("run_id") == run_id:
                completed.add(str(row["task_id"]))
    return completed


def _error_record(exc: Exception) -> dict[str, Any]:
    message = str(exc)
    message = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", message)
    message = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", message)
    return {
        "type": type(exc).__name__,
        "code": getattr(exc, "status_code", None),
        "message": message[:1000],
    }


async def _run_one(
    *,
    item: dict[str, Any],
    system: DataMind,
    run_id: str,
    semaphore: asyncio.Semaphore,
    task_retries: int,
) -> dict[str, Any]:
    async with semaphore:
        started = time.perf_counter()
        result: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        attempts = 0
        while attempts <= task_retries:
            attempts += 1
            try:
                result = await system.query(
                    str(item["question"]),
                    final_contract=item.get("final_contract"),
                )
                error = None
                break
            except Exception as exc:  # noqa: BLE001 - failures are benchmark data
                error = _error_record(exc)
                transient = (
                    isinstance(exc, (TimeoutError, ConnectionError))
                    or getattr(exc, "status_code", None) in {408, 409, 429, 500, 502, 503, 504}
                )
                if not transient or attempts > task_retries:
                    break
                await asyncio.sleep(min(4.0, 0.5 * (2 ** (attempts - 1))))
        elapsed = time.perf_counter() - started
        row: dict[str, Any] = {
            "run_id": run_id,
            "task_id": str(item["task_id"]),
            "question_id": item.get("question_id"),
            "question": item["question"],
            "answer": result.get("answer", "") if result else "",
            "latency_s": round(elapsed, 6),
            "attempts": attempts,
            "error": error,
            "resolved_models": (result or {}).get("usage", {}).get("resolved_models", []),
            "usage": (result or {}).get("usage", {}),
            "iterations": (result or {}).get("iterations"),
            "stop_reason": (result or {}).get("stop_reason"),
            "surfaces_used": (result or {}).get("surfaces_used", []),
            "tool_trace": (result or {}).get("tool_trace", []),
            "evidence": (result or {}).get("evidence", []),
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        for passthrough in ("reference_answer", "metadata"):
            if passthrough in item:
                row[passthrough] = item[passthrough]
        return row


async def run_benchmark(
    items: list[dict[str, Any]],
    *,
    system: DataMind,
    output: Path,
    run_id: str,
    concurrency: int = 5,
    task_retries: int = 1,
    completed: set[str] | None = None,
) -> list[dict[str, Any]]:
    done = completed or set()
    pending = [item for item in items if str(item["task_id"]) not in done]
    semaphore = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()
    results: list[dict[str, Any]] = []

    async def execute(item: dict[str, Any]) -> None:
        row = await _run_one(
            item=item, system=system, run_id=run_id, semaphore=semaphore,
            task_retries=task_retries,
        )
        async with write_lock:
            _append_jsonl_durable(output, row)
            results.append(row)
            print(
                f"\r  completed {len(done) + len(results)}/{len(items)}"
                f"  errors={sum(1 for r in results if r['error'])}",
                end="", flush=True,
            )

    await asyncio.gather(*(execute(item) for item in pending))
    if pending:
        print()
    return results


async def _async_main(args: argparse.Namespace) -> int:
    questions_path = Path(args.questions)
    output = Path(args.output)
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    items = load_questions(questions_path)
    surfaces = set(filter(None, (args.surfaces or "").split(","))) or None
    settings = Settings()
    system = await build_datamind(settings, enable=surfaces)
    try:
        run_id = args.run_id or f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        provenance = build_provenance(
            settings=settings, system=system, questions_path=questions_path,
            surfaces=surfaces, run_id=run_id,
        )
        if output.exists() and not args.resume:
            raise FileExistsError(
                f"refusing to overwrite existing run artifact {output}; use --resume or a new --output"
            )
        completed: set[str] = set()
        if args.resume:
            if not manifest_path.is_file():
                raise FileNotFoundError(f"resume requires manifest: {manifest_path}")
            stored = json.loads(manifest_path.read_text(encoding="utf-8"))
            if args.run_id and stored.get("run_id") != args.run_id:
                raise ValueError("--run-id does not match the checkpoint manifest")
            run_id = str(stored["run_id"])
            provenance["run_id"] = run_id
            for key in ("questions_sha256", "config_fingerprint", "prompt_sha256"):
                if stored.get(key) != provenance.get(key):
                    raise ValueError(f"resume provenance mismatch: {key}")
            completed = _load_completed(output, run_id)
        else:
            _write_json_atomic(manifest_path, provenance)

        await run_benchmark(
            items, system=system, output=output, run_id=run_id,
            concurrency=args.concurrency, task_retries=args.task_retries,
            completed=completed,
        )
        print(f"run_id={run_id} artifact={output} tasks={len(items)}")
        return 0
    finally:
        await system.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="DataMind current-stack benchmark runner")
    parser.add_argument("--questions", required=True, help="input JSONL")
    parser.add_argument("--output", default="benchmark_results.jsonl", help="checkpoint JSONL")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--task-retries", type=int, default=1)
    parser.add_argument("--surfaces", help="comma-separated enabled surfaces")
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_async_main(args)))
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
