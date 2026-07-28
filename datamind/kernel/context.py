"""Request-scoped identity, budget, permissions, and approvals."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import FrozenSet, Iterable, Optional

from .budget import Budget
from .effects import EffectLevel
from .errors import KernelValidationError, ScopePolicyError
from .lifecycle import SnapshotSet
from .memory import ScopeRef
from .types import new_id, require_aware


@dataclass(frozen=True)
class ExecutionContext:
    request_id: str
    trace_id: str
    profile: str = "default"
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    permissions: FrozenSet[str] = frozenset()
    approvals: FrozenSet[str] = frozenset()
    allowed_resources: FrozenSet[str] = frozenset()
    readable_scopes: FrozenSet[ScopeRef] = frozenset()
    writable_scopes: FrozenSet[ScopeRef] = frozenset()
    max_effect: EffectLevel = EffectLevel.READ
    budget: Budget = field(default_factory=Budget)
    snapshots: SnapshotSet = field(default_factory=SnapshotSet)
    deadline: Optional[datetime] = None

    def __post_init__(self) -> None:
        for field_name in ("request_id", "trace_id", "profile"):
            if not getattr(self, field_name).strip():
                raise KernelValidationError(
                    "{} must be non-empty".format(field_name)
                )
        object.__setattr__(self, "permissions", frozenset(self.permissions))
        object.__setattr__(self, "approvals", frozenset(self.approvals))
        object.__setattr__(
            self, "allowed_resources", frozenset(self.allowed_resources)
        )
        object.__setattr__(
            self, "readable_scopes", frozenset(self.readable_scopes)
        )
        object.__setattr__(
            self, "writable_scopes", frozenset(self.writable_scopes)
        )
        if any(
            not isinstance(item, ScopeRef)
            for item in self.readable_scopes
        ):
            raise KernelValidationError(
                "readable_scopes must contain ScopeRef values"
            )
        if any(
            not isinstance(item, ScopeRef)
            for item in self.writable_scopes
        ):
            raise KernelValidationError(
                "writable_scopes must contain ScopeRef values"
            )
        if not isinstance(self.snapshots, SnapshotSet):
            raise KernelValidationError(
                "execution snapshots must be a SnapshotSet"
            )
        if self.deadline is not None:
            require_aware(self.deadline, "deadline")

    @classmethod
    def new(
        cls,
        *,
        profile: str = "default",
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        max_effect: EffectLevel = EffectLevel.READ,
        budget: Optional[Budget] = None,
        snapshots: Optional[SnapshotSet] = None,
        readable_scopes: FrozenSet[ScopeRef] = frozenset(),
        writable_scopes: FrozenSet[ScopeRef] = frozenset(),
    ) -> "ExecutionContext":
        return cls(
            request_id=new_id("req"),
            trace_id=new_id("trace"),
            profile=profile,
            session_id=session_id,
            user_id=user_id,
            max_effect=max_effect,
            budget=budget or Budget(),
            snapshots=snapshots or SnapshotSet(),
            readable_scopes=readable_scopes,
            writable_scopes=writable_scopes,
        )

    def require_readable_scopes(
        self,
        scopes: Iterable[ScopeRef],
    ) -> None:
        requested = frozenset(scopes)
        missing = requested - self.readable_scopes
        if missing:
            raise ScopePolicyError(
                (
                    "recall requested {} scope(s) unavailable to this "
                    "execution context".format(len(missing)),
                )
            )
