"""Shared, offline-testable controls for paired Workspace agent evaluations.

This module never changes official labels or sends metadata to task agents.
Infrastructure failures and post-hoc validity flags remain visible in reports.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import random
import re
import time


def transient_error(error):
    text = json.dumps(error, default=str).casefold()
    markers = ("responsestreamdisconnected", "stream disconnected", "responsefailedtoreadstream",
               "responsetoomanyfailedattempts", "rate_limit", "rate limit", "connection reset",
               "service unavailable", "httpconnectionfailed", "stream idle timeout")
    return any(marker in text for marker in markers) or bool(
        re.search(r'"(?:httpstatus|status_code|status)"\s*:\s*(?:408|429|500|502|503|504)\b', text))


async def run_recoverable_turn(thread, prompt, on_event, *, timeout_s=900, max_retries=2,
                               output_schema=None, effort="medium", retry_delay_s=1):
    """Resume the same thread immediately, inside one fixed wall-clock budget.

    SDK request-level retries may also occur; every request must remain in token
    accounting. No controller/manual recovery gap or fresh task budget is added.
    Use the same settings for all compared methods.
    """
    if timeout_s <= 0 or not 0 <= max_retries <= 5 or retry_delay_s < 0:
        raise ValueError("invalid execution budget/retry policy")
    started = time.monotonic()
    deadline = started + timeout_s
    attempts, active = [], None
    status, category = "error", "agent_error"
    try:
        async with asyncio.timeout(timeout_s):
            for index in range(max_retries + 1):
                record = {"attempt": index, "started_s": time.monotonic() - started, "errors": []}
                attempts.append(record)
                try:
                    turn_prompt = prompt if index == 0 else (
                        "Continue the interrupted task in this same conversation, using the existing "
                        "files and progress. The original requirements and deadline still apply. "
                        "Inspect existing artifacts before repeating work.")
                    active = await thread.turn(turn_prompt, effort=effort, output_schema=output_schema)
                    status = "incomplete"
                    async for event in active.stream():
                        delivered = on_event(event)
                        if inspect.isawaitable(delivered):
                            await delivered
                        payload = event.payload
                        if hasattr(payload, "model_dump"):
                            payload = payload.model_dump(mode="json", by_alias=True)
                        if event.method == "error":
                            record["errors"].append(payload)
                        elif event.method == "turn/completed":
                            turn = payload.get("turn", {})
                            status = turn.get("status", "incomplete")
                            if turn.get("error"):
                                record["errors"].append(turn["error"])
                    record["status"] = status
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    status = "error"
                    record["errors"].append({"type": type(exc).__name__, "message": str(exc)})
                    record["status"] = status
                finally:
                    record["finished_s"] = time.monotonic() - started
                if status == "completed":
                    category = "completed"
                    break
                retryable = transient_error(record["errors"])
                category = "infrastructure_error" if retryable else "agent_error"
                if not retryable or index == max_retries:
                    break
                await asyncio.sleep(min(retry_delay_s * 2 ** index,
                                        max(0, deadline - time.monotonic())))
    except TimeoutError:
        status, category = "timeout", "task_timeout"
        if active is not None:
            try:
                await asyncio.wait_for(active.interrupt(), timeout=5)
            except Exception:
                pass
    return {"status": status, "category": category, "attempts": attempts,
            "seconds": time.monotonic() - started, "timeout_s": timeout_s,
            "quality_eligible": category != "infrastructure_error",
            "recovery_policy": "same thread; immediate bounded recovery; one wall-clock deadline"}


def source_audit(metadata, workspace_root, reference_root):
    """Verify source bytes and flag suspect Python syntax labels for review.

    Same-name files may legitimately differ; these are flags, never automatic
    score corrections. Reference inputs and rubrics belong only in this audit.
    """
    root, references = Path(workspace_root), Path(reference_root)
    by_name = {}
    for path in root.rglob("*"):
        if path.is_file() and path.resolve().is_relative_to(root.resolve()):
            by_name.setdefault(path.name, []).append(path)
    rows, python_sources = [], []
    for entry in metadata.get("data_manifest", []):
        name = entry["filename"]
        relative = Path(entry["stored_relpath"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("reference path must remain inside reference_root")
        ref = references / relative
        expected = hashlib.sha256(ref.read_bytes()).hexdigest() if ref.is_file() else None
        candidates = by_name.get(name, [])
        actual = [{"path": str(p.relative_to(root)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in candidates]
        rows.append({"filename": name, "reference_sha256": expected, "workspace_candidates": actual,
                     "exact_match": expected is not None and any(p["sha256"] == expected for p in actual)})
        python_sources.extend(p for p in candidates if p.suffix == ".py")
    syntax = []
    for path in python_sources:
        try:
            ast.parse(path.read_text(encoding="utf-8"))
            syntax.append({"path": str(path.relative_to(root)), "parseable": True})
        except (SyntaxError, UnicodeError) as exc:
            syntax.append({"path": str(path.relative_to(root)), "parseable": False, "error": str(exc)})
    flags = []
    if any(not row["exact_match"] for row in rows):
        flags.append("reference/workspace byte mismatch or missing source; manual review required")
    if syntax and all(row["parseable"] for row in syntax):
        for index, rubric in enumerate(metadata.get("rubrics", [])):
            if re.search(r"missing (?:closing )?brace|missing indentation|syntax errors?", str(rubric), re.I):
                flags.append({"rubric_index": index,
                              "reason": "syntax-related label but all inspected Python inputs parse; review expected finding"})
    return {"sources": rows, "python_syntax": syntax, "validity_flags": flags,
            "official_scores_modified": False}


def blind_pair_order(candidate_ids, *, seed):
    order = list(candidate_ids)
    random.Random(seed).shuffle(order)
    return [{"label": f"candidate-{i + 1}", "candidate_id": value} for i, value in enumerate(order)]
