"""AuditLogHook — append-only, tamper-evident tool-call audit log.

Every tool dispatch produces (at most) two records:

    pre  — emitted in pre_tool_use, captures tool_name + args + decision
    post — emitted in post_tool_use, captures result hash + error + ts

A Merkle-style hash chain links records: each record carries
`prev_hash` = hash of the previous record's canonical-JSON encoding,
and `record_hash` = hash of (prev_hash || this_record_minus_record_hash).
Tampering with any prior record (or removing one) breaks every
subsequent record's `record_hash`, which `verify_audit_log` detects.

Format: one JSON object per line at `storage/<profile>/audit.jsonl`,
UTF-8, LF-terminated.

Secret redaction: arg values whose KEY matches a redaction regex
(`api_key`, `password`, `token`, `secret`, ...) are replaced with
`"[REDACTED]"` before logging. Values are not scanned (we trust the
caller to put secrets in well-named fields, which is true across
DataMind's own tools).

Concurrency: writes are serialised via an asyncio.Lock per AuditLogHook
instance. For multi-process deployments use one process per profile
(profile is the natural sharding boundary anyway).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from datamind.core.context import RequestContext
from datamind.core.hooks import Allow, AskUser, Deny, HookDecision, Rewrite

# Field names whose VALUES will be redacted before logging. Match is
# case-insensitive on the full key. We prefer false positives (over-
# redact a non-secret field) to false negatives (leak a secret).
_REDACT_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|password|passwd|token|secret|authorization|bearer|access[_-]?key|client[_-]?secret)"
)
_REDACTED = "[REDACTED]"

_HASH_HEX_LEN = 16  # truncated SHA-256 hex; 64 bits of collision resistance


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(obj: Any) -> str:
    """JSON encode with sorted keys + no extra whitespace, for hashing."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _short_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:_HASH_HEX_LEN]


def _redact(value: Any) -> Any:
    """Return a copy of `value` with secret-shaped keys redacted."""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _REDACT_KEY_RE.search(str(k)) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _decision_to_record(decision: HookDecision) -> dict[str, Any]:
    if isinstance(decision, Allow):
        return {"kind": "allow"}
    if isinstance(decision, Deny):
        return {"kind": "deny", "reason": decision.reason}
    if isinstance(decision, AskUser):
        return {
            "kind": "ask_user",
            "prompt": decision.prompt,
            "details": _redact(decision.details),
        }
    if isinstance(decision, Rewrite):
        # Don't log full rewritten args twice — but record that a rewrite
        # happened. (post-record will log the args actually used.)
        return {"kind": "rewrite"}
    return {"kind": "unknown"}


def _result_summary(result: Any) -> dict[str, Any]:
    """Produce a compact result fingerprint for the post record."""
    if result is None:
        return {"hash": None, "size": 0}
    try:
        encoded = _canonical(result)
    except Exception:
        encoded = str(result)
    return {"hash": _short_hash(encoded), "size": len(encoded)}


class AuditLogHook:
    """Both pre and post hooks; emits one record on each call."""

    name = "audit_log"

    def __init__(self, audit_path: Path | str) -> None:
        self._path = Path(audit_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        # `prev_hash` is initialised lazily on first write so we can
        # resume a chain from an existing file (operator restarts).
        self._prev_hash: str | None = None
        self._tail_loaded = False

    # ------------------------------------------------------ public hooks

    async def pre_tool_use(
        self,
        ctx: RequestContext,
        tool_name: str,
        args: dict[str, Any],
    ) -> HookDecision:
        # We don't actually decide anything; we *will* log the eventual
        # decision in `post_tool_use`. Returning Allow keeps us out of
        # the way of policy hooks.
        #
        # NOTE: AuditLogHook is intentionally placed LAST in the chain,
        # so by the time it runs, prior policy hooks have already either
        # vetoed (we won't see post for that call from this hook because
        # the loop already short-circuited and called HookChain.post,
        # which DOES run us — see HookChain.post in core/hooks.py).
        return Allow()

    async def post_tool_use(
        self,
        ctx: RequestContext,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        error: Exception | None,
    ) -> None:
        # `result` is the handler's return value, OR the structured
        # short-circuit dict produced by HookChain.pre when a prior
        # hook returned Deny / AskUser. We tag those distinctly.
        decision_kind = "allow"
        if isinstance(result, dict) and result.get("kind") in ("denied", "asks_user"):
            decision_kind = result["kind"]

        record = {
            "ts": _now_iso(),
            "trace_id": ctx.trace_id,
            "session_id": ctx.session_id,
            "profile": ctx.profile,
            "tool": tool_name,
            "args": _redact(args),
            "decision": decision_kind,
            "result": _result_summary(result if error is None else None),
            "error": (
                None
                if error is None
                else f"{type(error).__name__}: {error}"
            ),
        }
        await self._append(record)

    # ------------------------------------------------------ chain logic

    async def _append(self, record: dict[str, Any]) -> None:
        """Hash-chain append. Acquires the lock for the read-tail-then-write
        critical section."""
        async with self._lock:
            if not self._tail_loaded:
                self._prev_hash = await asyncio.to_thread(_load_tail_hash, self._path)
                self._tail_loaded = True
            record["prev_hash"] = self._prev_hash
            # record_hash = hash(prev_hash || canonical(record_minus_record_hash))
            # Including prev_hash inside the canonical encoding chains the
            # records.
            record_hash = _short_hash(_canonical(record))
            record["record_hash"] = record_hash
            line = _canonical(record) + "\n"
            await asyncio.to_thread(_append_line, self._path, line)
            self._prev_hash = record_hash


# ============================================================ verification


def verify_audit_log(path: Path | str) -> tuple[bool, str | None, int]:
    """Re-check the hash chain end-to-end.

    Returns (ok, first_bad_record_or_None, total_records).
    `first_bad` is a human-readable description, e.g.
    "line 17: prev_hash mismatch (expected abc..., got xyz...)".
    """
    p = Path(path)
    if not p.exists():
        return True, None, 0

    prev: str | None = None
    n = 0
    with p.open("r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as exc:
                return False, f"line {i}: malformed JSON ({exc})", n
            stored = rec.get("record_hash")
            if not isinstance(stored, str):
                return False, f"line {i}: missing record_hash", n
            if rec.get("prev_hash") != prev:
                return (
                    False,
                    f"line {i}: prev_hash mismatch (expected {prev!r}, got {rec.get('prev_hash')!r})",
                    n,
                )
            # Reconstruct the input that was hashed: the record minus its
            # own record_hash.
            recompute = {k: v for k, v in rec.items() if k != "record_hash"}
            if _short_hash(_canonical(recompute)) != stored:
                return False, f"line {i}: record_hash mismatch", n
            prev = stored
            n += 1
    return True, None, n


# ============================================================ helpers


def _load_tail_hash(path: Path) -> str | None:
    """Read the last record's record_hash so we can resume the chain."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    # Audit logs are typically small; a full pass is fine and avoids
    # subtle bugs with seek-and-scan over multi-byte UTF-8.
    last = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        return None
    try:
        rec = json.loads(last)
    except json.JSONDecodeError:
        return None
    h = rec.get("record_hash")
    return h if isinstance(h, str) else None


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


__all__ = ["AuditLogHook", "verify_audit_log"]
