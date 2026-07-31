"""Versioned, JSON-serializable contracts for DataMind-Bench v0.1."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from datamind.kernel import (
    Budget,
    EffectLevel,
    MemoryOriginChannel,
    ScopeKind,
    ScopeRef,
    SourceKind,
)

BENCHMARK_TASK_SCHEMA = "datamind.benchmark_task"
BENCHMARK_TASK_VERSION = "0.1"


class BenchmarkValidationError(ValueError):
    """Raised when a benchmark contract is malformed."""


class TaskLayer(str, Enum):
    SURFACE_CONTRACT = "surface_contract"
    CROSS_SURFACE = "cross_surface"
    STATE_SHIFT = "state_shift"
    FAILURE_REPLAY = "failure_replay"


class RunnerMode(str, Enum):
    ORACLE_PLAN = "oracle_plan"
    SCRIPTED_COMPILER = "scripted_compiler"
    LIVE_PLANNER = "live_planner"


class ReplayExpectation(str, Enum):
    SKIP = "skip"
    EQUIVALENT = "equivalent"
    FORBIDDEN = "forbidden"


def _non_empty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkValidationError(
            "{} must be a non-empty string".format(name)
        )
    return value


def _string_tuple(value: Any, name: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise BenchmarkValidationError("{} must be a list".format(name))
    result = tuple(_non_empty(item, name) for item in value)
    if len(set(result)) != len(result):
        raise BenchmarkValidationError(
            "{} cannot contain duplicates".format(name)
        )
    return result


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BenchmarkValidationError("{} must be an object".format(name))
    return value


@dataclass(frozen=True)
class PrecedenceConstraint:
    """Require at least one dependency path between operation kinds."""

    before: str
    after: str

    def __post_init__(self) -> None:
        _non_empty(self.before, "precedence before")
        _non_empty(self.after, "precedence after")
        if self.before == self.after:
            raise BenchmarkValidationError(
                "precedence endpoints must be different"
            )


@dataclass(frozen=True)
class PlanConstraints:
    """Semantic plan acceptance, intentionally weaker than an exact gold DAG."""

    required_source_kinds: Tuple[SourceKind, ...] = ()
    required_source_ids: Tuple[str, ...] = ()
    required_operations: Tuple[str, ...] = ()
    allowed_operations: Tuple[str, ...] = ()
    precedence: Tuple[PrecedenceConstraint, ...] = ()
    max_effect: EffectLevel = EffectLevel.READ
    max_actions: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required_source_kinds", tuple(self.required_source_kinds)
        )
        object.__setattr__(
            self, "required_source_ids", tuple(self.required_source_ids)
        )
        object.__setattr__(
            self, "required_operations", tuple(self.required_operations)
        )
        object.__setattr__(
            self, "allowed_operations", tuple(self.allowed_operations)
        )
        object.__setattr__(self, "precedence", tuple(self.precedence))
        if any(
            not isinstance(item, SourceKind)
            for item in self.required_source_kinds
        ):
            raise BenchmarkValidationError(
                "required_source_kinds must contain SourceKind values"
            )
        for name in (
            "required_source_kinds",
            "required_source_ids",
            "required_operations",
            "allowed_operations",
        ):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise BenchmarkValidationError(
                    "{} cannot contain duplicates".format(name)
                )
        if any(
            not isinstance(item, PrecedenceConstraint)
            for item in self.precedence
        ):
            raise BenchmarkValidationError(
                "precedence must contain PrecedenceConstraint values"
            )
        if not isinstance(self.max_effect, EffectLevel):
            raise BenchmarkValidationError(
                "max_effect must be an EffectLevel"
            )
        if self.max_actions is not None and (
            isinstance(self.max_actions, bool)
            or not isinstance(self.max_actions, int)
            or self.max_actions <= 0
        ):
            raise BenchmarkValidationError(
                "max_actions must be a positive integer"
            )
        if self.allowed_operations:
            missing = (
                set(self.required_operations)
                - set(self.allowed_operations)
            )
            if missing:
                raise BenchmarkValidationError(
                    "required operations must also be allowed"
                )


@dataclass(frozen=True)
class ContextSpec:
    """Authority and budget applied to one isolated benchmark run."""

    max_effect: EffectLevel = EffectLevel.READ
    budget: Budget = field(default_factory=Budget)
    approvals: Tuple[str, ...] = ()
    allowed_resources: Tuple[str, ...] = ()
    readable_scopes: Tuple[ScopeRef, ...] = ()
    writable_scopes: Tuple[ScopeRef, ...] = ()
    memory_origin: Optional[MemoryOriginChannel] = None

    def __post_init__(self) -> None:
        if not isinstance(self.max_effect, EffectLevel):
            raise BenchmarkValidationError(
                "context max_effect must be EffectLevel"
            )
        if not isinstance(self.budget, Budget):
            raise BenchmarkValidationError(
                "context budget must be Budget"
            )
        for name in ("approvals", "allowed_resources"):
            values = tuple(getattr(self, name))
            object.__setattr__(self, name, values)
            if any(
                not isinstance(item, str) or not item.strip()
                for item in values
            ):
                raise BenchmarkValidationError(
                    "{} must contain non-empty strings".format(name)
                )
            if len(set(values)) != len(values):
                raise BenchmarkValidationError(
                    "{} cannot contain duplicates".format(name)
                )
        for name in ("readable_scopes", "writable_scopes"):
            values = tuple(getattr(self, name))
            object.__setattr__(self, name, values)
            if any(not isinstance(item, ScopeRef) for item in values):
                raise BenchmarkValidationError(
                    "{} must contain ScopeRef values".format(name)
                )
            if len(set(values)) != len(values):
                raise BenchmarkValidationError(
                    "{} cannot contain duplicates".format(name)
                )
        if self.memory_origin is not None and not isinstance(
            self.memory_origin, MemoryOriginChannel
        ):
            raise BenchmarkValidationError(
                "memory_origin must be MemoryOriginChannel"
            )


@dataclass(frozen=True)
class AssertionSpec:
    """Reference a trusted Python oracle without embedding executable code."""

    name: str
    oracle: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_empty(self.name, "assertion name")
        _non_empty(self.oracle, "assertion oracle")
        params = _mapping(self.params, "assertion params")
        try:
            encoded = json.dumps(
                params,
                ensure_ascii=False,
                sort_keys=True,
            )
            normalized = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise BenchmarkValidationError(
                "assertion params must be JSON serializable"
            ) from exc
        object.__setattr__(
            self,
            "params",
            MappingProxyType(normalized),
        )


@dataclass(frozen=True)
class TaskSpec:
    """One portable task description; executable logic stays in registries."""

    task_id: str
    title: str
    layer: TaskLayer
    fixture_id: str
    workload_id: str
    assertions: Tuple[AssertionSpec, ...]
    supported_modes: Tuple[RunnerMode, ...] = (RunnerMode.ORACLE_PLAN,)
    context: ContextSpec = field(default_factory=ContextSpec)
    plan_constraints: PlanConstraints = field(
        default_factory=PlanConstraints
    )
    request: Optional[str] = None
    script_id: Optional[str] = None
    expected_error: Optional[str] = None
    replay: ReplayExpectation = ReplayExpectation.SKIP
    schema: str = BENCHMARK_TASK_SCHEMA
    version: str = BENCHMARK_TASK_VERSION

    def __post_init__(self) -> None:
        for name in (
            "task_id",
            "title",
            "fixture_id",
            "workload_id",
            "schema",
            "version",
        ):
            _non_empty(getattr(self, name), name)
        if self.schema != BENCHMARK_TASK_SCHEMA:
            raise BenchmarkValidationError(
                "unsupported task schema {!r}".format(self.schema)
            )
        if self.version != BENCHMARK_TASK_VERSION:
            raise BenchmarkValidationError(
                "unsupported task version {!r}".format(self.version)
            )
        if not isinstance(self.layer, TaskLayer):
            raise BenchmarkValidationError("layer must be TaskLayer")
        object.__setattr__(self, "assertions", tuple(self.assertions))
        if not self.assertions:
            raise BenchmarkValidationError(
                "task requires at least one assertion"
            )
        if any(
            not isinstance(item, AssertionSpec)
            for item in self.assertions
        ):
            raise BenchmarkValidationError(
                "assertions must contain AssertionSpec values"
            )
        names = tuple(item.name for item in self.assertions)
        if len(set(names)) != len(names):
            raise BenchmarkValidationError(
                "task assertion names cannot repeat"
            )
        object.__setattr__(
            self, "supported_modes", tuple(self.supported_modes)
        )
        if not self.supported_modes or any(
            not isinstance(item, RunnerMode)
            for item in self.supported_modes
        ):
            raise BenchmarkValidationError(
                "supported_modes must contain RunnerMode values"
            )
        if len(set(self.supported_modes)) != len(self.supported_modes):
            raise BenchmarkValidationError(
                "supported_modes cannot contain duplicates"
            )
        if RunnerMode.SCRIPTED_COMPILER in self.supported_modes:
            if not self.request or not self.script_id:
                raise BenchmarkValidationError(
                    "scripted compiler tasks require request and script_id"
                )
        if RunnerMode.LIVE_PLANNER in self.supported_modes:
            if not self.request:
                raise BenchmarkValidationError(
                    "live planner tasks require a request"
                )
        for name in ("request", "script_id", "expected_error"):
            value = getattr(self, name)
            if value is not None:
                _non_empty(value, name)
        if not isinstance(self.context, ContextSpec):
            raise BenchmarkValidationError(
                "context must be ContextSpec"
            )
        if not isinstance(self.plan_constraints, PlanConstraints):
            raise BenchmarkValidationError(
                "plan_constraints must be PlanConstraints"
            )
        if not isinstance(self.replay, ReplayExpectation):
            raise BenchmarkValidationError(
                "replay must be ReplayExpectation"
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TaskSpec":
        value = _mapping(payload, "task")
        allowed = {
            "schema",
            "version",
            "task_id",
            "title",
            "layer",
            "fixture_id",
            "workload_id",
            "supported_modes",
            "context",
            "plan_constraints",
            "assertions",
            "request",
            "script_id",
            "expected_error",
            "replay",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise BenchmarkValidationError(
                "unknown task fields: {}".format(unknown)
            )
        context_payload = _mapping(value.get("context", {}), "context")
        budget_payload = _mapping(
            context_payload.get("budget", {}),
            "context budget",
        )
        context = ContextSpec(
            max_effect=_effect(
                context_payload.get("max_effect", "READ")
            ),
            budget=Budget(
                max_tokens=budget_payload.get("max_tokens"),
                max_latency_ms=budget_payload.get("max_latency_ms"),
                max_cost_usd=budget_payload.get("max_cost_usd"),
                max_actions=budget_payload.get("max_actions"),
            ),
            approvals=_string_tuple(
                context_payload.get("approvals", ()),
                "approvals",
            ),
            allowed_resources=_string_tuple(
                context_payload.get("allowed_resources", ()),
                "allowed_resources",
            ),
            readable_scopes=_scopes(
                context_payload.get("readable_scopes", ())
            ),
            writable_scopes=_scopes(
                context_payload.get("writable_scopes", ())
            ),
            memory_origin=(
                MemoryOriginChannel(context_payload["memory_origin"])
                if context_payload.get("memory_origin") is not None
                else None
            ),
        )
        constraints_payload = _mapping(
            value.get("plan_constraints", {}),
            "plan_constraints",
        )
        constraints = PlanConstraints(
            required_source_kinds=tuple(
                SourceKind(item)
                for item in constraints_payload.get(
                    "required_source_kinds", ()
                )
            ),
            required_source_ids=_string_tuple(
                constraints_payload.get("required_source_ids", ()),
                "required_source_ids",
            ),
            required_operations=_string_tuple(
                constraints_payload.get("required_operations", ()),
                "required_operations",
            ),
            allowed_operations=_string_tuple(
                constraints_payload.get("allowed_operations", ()),
                "allowed_operations",
            ),
            precedence=tuple(
                PrecedenceConstraint(
                    before=_non_empty(item.get("before"), "before"),
                    after=_non_empty(item.get("after"), "after"),
                )
                for item in constraints_payload.get("precedence", ())
            ),
            max_effect=_effect(
                constraints_payload.get("max_effect", "READ")
            ),
            max_actions=constraints_payload.get("max_actions"),
        )
        assertions_payload = value.get("assertions", ())
        if not isinstance(assertions_payload, (list, tuple)):
            raise BenchmarkValidationError(
                "assertions must be a list"
            )
        return cls(
            schema=value.get("schema", ""),
            version=value.get("version", ""),
            task_id=value.get("task_id", ""),
            title=value.get("title", ""),
            layer=TaskLayer(value.get("layer", "")),
            fixture_id=value.get("fixture_id", ""),
            workload_id=value.get("workload_id", ""),
            supported_modes=tuple(
                RunnerMode(item)
                for item in value.get(
                    "supported_modes",
                    (RunnerMode.ORACLE_PLAN.value,),
                )
            ),
            context=context,
            plan_constraints=constraints,
            assertions=tuple(
                AssertionSpec(
                    name=item.get("name", ""),
                    oracle=item.get("oracle", ""),
                    params=item.get("params", {}),
                )
                for item in assertions_payload
            ),
            request=value.get("request"),
            script_id=value.get("script_id"),
            expected_error=value.get("expected_error"),
            replay=ReplayExpectation(value.get("replay", "skip")),
        )

    def to_dict(self) -> dict:
        """Return the canonical JSON-safe representation."""

        budget = {
            "max_tokens": self.context.budget.max_tokens,
            "max_latency_ms": self.context.budget.max_latency_ms,
            "max_cost_usd": (
                str(self.context.budget.max_cost_usd)
                if self.context.budget.max_cost_usd is not None
                else None
            ),
            "max_actions": self.context.budget.max_actions,
        }
        return {
            "schema": self.schema,
            "version": self.version,
            "task_id": self.task_id,
            "title": self.title,
            "layer": self.layer.value,
            "fixture_id": self.fixture_id,
            "workload_id": self.workload_id,
            "supported_modes": [
                item.value for item in self.supported_modes
            ],
            "context": {
                "max_effect": self.context.max_effect.name,
                "budget": budget,
                "approvals": list(self.context.approvals),
                "allowed_resources": list(
                    self.context.allowed_resources
                ),
                "readable_scopes": [
                    {
                        "kind": item.kind.value,
                        "scope_id": item.scope_id,
                    }
                    for item in self.context.readable_scopes
                ],
                "writable_scopes": [
                    {
                        "kind": item.kind.value,
                        "scope_id": item.scope_id,
                    }
                    for item in self.context.writable_scopes
                ],
                "memory_origin": (
                    self.context.memory_origin.value
                    if self.context.memory_origin is not None
                    else None
                ),
            },
            "plan_constraints": {
                "required_source_kinds": [
                    item.value
                    for item in self.plan_constraints.required_source_kinds
                ],
                "required_source_ids": list(
                    self.plan_constraints.required_source_ids
                ),
                "required_operations": list(
                    self.plan_constraints.required_operations
                ),
                "allowed_operations": list(
                    self.plan_constraints.allowed_operations
                ),
                "precedence": [
                    {
                        "before": item.before,
                        "after": item.after,
                    }
                    for item in self.plan_constraints.precedence
                ],
                "max_effect": self.plan_constraints.max_effect.name,
                "max_actions": self.plan_constraints.max_actions,
            },
            "assertions": [
                {
                    "name": item.name,
                    "oracle": item.oracle,
                    "params": dict(item.params),
                }
                for item in self.assertions
            ],
            "request": self.request,
            "script_id": self.script_id,
            "expected_error": self.expected_error,
            "replay": self.replay.value,
        }


def _effect(value: Any) -> EffectLevel:
    if isinstance(value, EffectLevel):
        return value
    try:
        return EffectLevel[str(value).upper()]
    except KeyError as exc:
        raise BenchmarkValidationError(
            "unknown effect level {!r}".format(value)
        ) from exc


def _scopes(value: Any) -> Tuple[ScopeRef, ...]:
    if not isinstance(value, (list, tuple)):
        raise BenchmarkValidationError("scopes must be a list")
    return tuple(
        ScopeRef(
            ScopeKind(_mapping(item, "scope").get("kind", "")),
            _non_empty(item.get("scope_id"), "scope_id"),
        )
        for item in value
    )


def load_task(path: Path) -> TaskSpec:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkValidationError(
            "cannot load task {}: {}".format(path, exc)
        ) from exc
    return TaskSpec.from_dict(payload)


def load_tasks(directory: Path) -> Tuple[TaskSpec, ...]:
    directory = Path(directory)
    tasks = tuple(
        load_task(path)
        for path in sorted(directory.glob("*.json"))
    )
    ids = tuple(item.task_id for item in tasks)
    if len(set(ids)) != len(ids):
        raise BenchmarkValidationError(
            "task directory contains duplicate task ids"
        )
    return tasks
