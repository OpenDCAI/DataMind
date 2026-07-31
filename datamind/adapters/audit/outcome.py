"""Reference in-memory and JSONL stores for content-safe outcomes."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Dict, Mapping, Optional, Tuple

from datamind.kernel import (
    EvaluatorKind,
    OutcomeAssertion,
    OutcomeConflictError,
    OutcomeError,
    OutcomeNotFoundError,
    OutcomeRecord,
    OutcomeTarget,
    OutcomeTargetKind,
)


def _required_string(
    payload: Mapping[str, object],
    name: str,
) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise TypeError("{} must be a non-empty string".format(name))
    return value


def _resolve_existing(
    outcome: OutcomeRecord,
    *,
    records: Mapping[str, OutcomeRecord],
    idempotency: Mapping[str, str],
) -> Optional[OutcomeRecord]:
    existing_id = idempotency.get(outcome.idempotency_key)
    if existing_id is not None:
        existing = records[existing_id]
        if existing.equivalent_to(outcome):
            return existing
        raise OutcomeConflictError(
            "outcome idempotency key conflicts with prior intent"
        )
    existing = records.get(outcome.outcome_id)
    if existing is not None:
        if existing == outcome:
            return existing
        raise OutcomeConflictError(
            "outcome_id {!r} already exists".format(outcome.outcome_id)
        )
    return None


def _sorted_for(
    records: Mapping[str, OutcomeRecord],
    target: OutcomeTarget,
) -> Tuple[OutcomeRecord, ...]:
    return tuple(
        sorted(
            (
                item
                for item in records.values()
                if item.target == target
            ),
            key=lambda item: (item.observed_at, item.outcome_id),
        )
    )


class InMemoryOutcomeStore:
    """Thread-safe reference store with semantic idempotency."""

    def __init__(self) -> None:
        self._records: Dict[str, OutcomeRecord] = {}
        self._idempotency: Dict[str, str] = {}
        self._lock = RLock()

    async def record(self, outcome: OutcomeRecord) -> OutcomeRecord:
        if not isinstance(outcome, OutcomeRecord):
            raise OutcomeError(
                "outcome store requires an OutcomeRecord"
            )
        with self._lock:
            existing = _resolve_existing(
                outcome,
                records=self._records,
                idempotency=self._idempotency,
            )
            if existing is not None:
                return existing
            self._records[outcome.outcome_id] = outcome
            self._idempotency[
                outcome.idempotency_key
            ] = outcome.outcome_id
            return outcome

    async def get(self, outcome_id: str) -> OutcomeRecord:
        with self._lock:
            outcome = self._records.get(outcome_id)
            if outcome is None:
                raise OutcomeNotFoundError(
                    "outcome {!r} does not exist".format(outcome_id)
                )
            return outcome

    async def list_for(
        self,
        target: OutcomeTarget,
    ) -> Tuple[OutcomeRecord, ...]:
        if not isinstance(target, OutcomeTarget):
            raise OutcomeError(
                "outcome listing requires an OutcomeTarget"
            )
        with self._lock:
            return _sorted_for(self._records, target)


class JsonlOutcomeStore:
    """Single-process durable append-only OutcomeStore."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory).expanduser().resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._path = self._directory / "outcomes.jsonl"
        self._lock = RLock()

    async def record(self, outcome: OutcomeRecord) -> OutcomeRecord:
        if not isinstance(outcome, OutcomeRecord):
            raise OutcomeError(
                "outcome store requires an OutcomeRecord"
            )
        return await asyncio.to_thread(self._record_sync, outcome)

    async def get(self, outcome_id: str) -> OutcomeRecord:
        return await asyncio.to_thread(self._get_sync, outcome_id)

    async def list_for(
        self,
        target: OutcomeTarget,
    ) -> Tuple[OutcomeRecord, ...]:
        if not isinstance(target, OutcomeTarget):
            raise OutcomeError(
                "outcome listing requires an OutcomeTarget"
            )
        return await asyncio.to_thread(self._list_for_sync, target)

    def _record_sync(self, outcome: OutcomeRecord) -> OutcomeRecord:
        with self._lock:
            records, idempotency = self._load_sync()
            existing = _resolve_existing(
                outcome,
                records=records,
                idempotency=idempotency,
            )
            if existing is not None:
                return existing
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        self._to_dict(outcome),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            return outcome

    def _get_sync(self, outcome_id: str) -> OutcomeRecord:
        with self._lock:
            records, _ = self._load_sync()
            outcome = records.get(outcome_id)
            if outcome is None:
                raise OutcomeNotFoundError(
                    "outcome {!r} does not exist".format(outcome_id)
                )
            return outcome

    def _list_for_sync(
        self,
        target: OutcomeTarget,
    ) -> Tuple[OutcomeRecord, ...]:
        with self._lock:
            records, _ = self._load_sync()
            return _sorted_for(records, target)

    def _load_sync(
        self,
    ) -> Tuple[Dict[str, OutcomeRecord], Dict[str, str]]:
        records: Dict[str, OutcomeRecord] = {}
        idempotency: Dict[str, str] = {}
        if not self._path.is_file():
            return records, idempotency
        line_number = 0
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    outcome = self._from_dict(json.loads(line))
                    existing = _resolve_existing(
                        outcome,
                        records=records,
                        idempotency=idempotency,
                    )
                    if existing is not None:
                        continue
                    records[outcome.outcome_id] = outcome
                    idempotency[
                        outcome.idempotency_key
                    ] = outcome.outcome_id
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            OutcomeConflictError,
        ) as exc:
            raise OutcomeError(
                "corrupt outcome store near line {}".format(line_number)
            ) from exc
        return records, idempotency

    @staticmethod
    def _to_dict(outcome: OutcomeRecord) -> dict:
        return {
            "outcome_id": outcome.outcome_id,
            "target": {
                "kind": outcome.target.kind.value,
                "target_id": outcome.target.target_id,
            },
            "task_id": outcome.task_id,
            "evaluator_kind": outcome.evaluator_kind.value,
            "evaluator_name": outcome.evaluator_name,
            "evaluator_version": outcome.evaluator_version,
            "assertions": [
                {
                    "name": item.name,
                    "passed": item.passed,
                    "score": (
                        str(item.score)
                        if item.score is not None
                        else None
                    ),
                }
                for item in outcome.assertions
            ],
            "succeeded": outcome.succeeded,
            "idempotency_key": outcome.idempotency_key,
            "observed_at": outcome.observed_at.isoformat(),
        }

    @staticmethod
    def _from_dict(payload: Mapping[str, object]) -> OutcomeRecord:
        target = payload["target"]
        if not isinstance(target, Mapping):
            raise TypeError("outcome target must be an object")
        assertions = payload["assertions"]
        if not isinstance(assertions, list):
            raise TypeError("outcome assertions must be an array")
        if any(not isinstance(item, Mapping) for item in assertions):
            raise TypeError(
                "outcome assertions must contain objects"
            )
        return OutcomeRecord(
            outcome_id=_required_string(payload, "outcome_id"),
            target=OutcomeTarget(
                kind=OutcomeTargetKind(
                    _required_string(target, "kind")
                ),
                target_id=_required_string(target, "target_id"),
            ),
            task_id=_required_string(payload, "task_id"),
            evaluator_kind=EvaluatorKind(
                _required_string(payload, "evaluator_kind")
            ),
            evaluator_name=_required_string(
                payload,
                "evaluator_name",
            ),
            evaluator_version=_required_string(
                payload,
                "evaluator_version",
            ),
            assertions=tuple(
                OutcomeAssertion(
                    name=_required_string(item, "name"),
                    passed=item["passed"],
                    score=item.get("score"),
                )
                for item in assertions
            ),
            succeeded=payload["succeeded"],
            idempotency_key=_required_string(
                payload,
                "idempotency_key",
            ),
            observed_at=datetime.fromisoformat(
                _required_string(payload, "observed_at")
            ),
        )


__all__ = ["InMemoryOutcomeStore", "JsonlOutcomeStore"]
