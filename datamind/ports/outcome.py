"""Stable storage contract for append-only task outcomes."""
from __future__ import annotations

from typing import Protocol, Tuple

from datamind.kernel import OutcomeRecord, OutcomeTarget


class OutcomeStore(Protocol):
    async def record(self, outcome: OutcomeRecord) -> OutcomeRecord:
        """Persist or return an idempotently equivalent prior record."""
        ...

    async def get(self, outcome_id: str) -> OutcomeRecord:
        ...

    async def list_for(
        self,
        target: OutcomeTarget,
    ) -> Tuple[OutcomeRecord, ...]:
        ...


__all__ = ["OutcomeStore"]
