"""Deterministic assertion registry for DataMind-Bench v0.1."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, TYPE_CHECKING

from datamind.kernel import thaw_json

from .schema import AssertionSpec, BenchmarkValidationError

if TYPE_CHECKING:
    from .runner import RunObservation

Oracle = Callable[
    ["RunObservation", Mapping[str, Any]],
    Awaitable["AssertionVerdict"],
]


@dataclass(frozen=True)
class AssertionVerdict:
    passed: bool
    score: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise BenchmarkValidationError(
                "assertion verdict passed must be boolean"
            )
        if self.score is not None:
            score = Decimal(str(self.score))
            if not score.is_finite() or score < 0 or score > 1:
                raise BenchmarkValidationError(
                    "assertion score must be between zero and one"
                )
            object.__setattr__(self, "score", score)

    @classmethod
    def boolean(cls, passed: bool) -> "AssertionVerdict":
        return cls(
            passed=passed,
            score=Decimal(1 if passed else 0),
        )


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, MappingProxyType):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _select(value: Any, path: Any) -> Any:
    if not isinstance(path, (list, tuple)):
        raise BenchmarkValidationError(
            "oracle path must be a list"
        )
    current = value
    for segment in path:
        if isinstance(current, Mapping):
            current = current[segment]
        elif isinstance(segment, int):
            current = current[segment]
        else:
            current = getattr(current, segment)
    return current


class OracleRegistry:
    """Explicit oracle lookup; task files can never supply Python code."""

    def __init__(self) -> None:
        self._oracles: Dict[str, Oracle] = {}
        self._register_builtins()

    def register(self, name: str, oracle: Oracle) -> None:
        if not isinstance(name, str) or not name.strip():
            raise BenchmarkValidationError(
                "oracle name must be non-empty"
            )
        if not callable(oracle):
            raise BenchmarkValidationError(
                "oracle must be callable"
            )
        if name in self._oracles:
            raise BenchmarkValidationError(
                "oracle {!r} is already registered".format(name)
            )
        self._oracles[name] = oracle

    async def evaluate(
        self,
        observation: "RunObservation",
        assertion: AssertionSpec,
    ) -> AssertionVerdict:
        try:
            oracle = self._oracles[assertion.oracle]
        except KeyError as exc:
            raise BenchmarkValidationError(
                "unknown oracle {!r}".format(assertion.oracle)
            ) from exc
        verdict = await oracle(observation, assertion.params)
        if not isinstance(verdict, AssertionVerdict):
            raise BenchmarkValidationError(
                "oracle {!r} returned {}, expected AssertionVerdict".format(
                    assertion.oracle,
                    type(verdict).__name__,
                )
            )
        return verdict

    def validate(self, assertions) -> None:
        for assertion in assertions:
            if assertion.oracle not in self._oracles:
                raise BenchmarkValidationError(
                    "unknown oracle {!r}".format(assertion.oracle)
                )

    def _register_builtins(self) -> None:
        self.register("result_kind", _result_kind)
        self.register("value_path_equals", _value_path_equals)
        self.register("binding_rows_equals", _binding_rows_equals)
        self.register("evidence_count", _evidence_count)
        self.register("provenance_sources", _provenance_sources)
        self.register("snapshot_count", _snapshot_count)
        self.register("error_type", _error_type)
        self.register("resolution_attempts", _resolution_attempts)
        self.register("trace_terminal", _trace_terminal)
        self.register(
            "state_value_path_equals",
            _state_value_path_equals,
        )


def _result(observation: "RunObservation"):
    return observation.result


async def _result_kind(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    result = _result(observation)
    expected = params.get("expected")
    return AssertionVerdict.boolean(
        result is not None
        and result.result_kind.value == expected
    )


async def _value_path_equals(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    result = _result(observation)
    if result is None:
        return AssertionVerdict.boolean(False)
    try:
        actual = _select(result.value, params.get("path", ()))
    except (AttributeError, IndexError, KeyError, TypeError):
        return AssertionVerdict.boolean(False)
    return AssertionVerdict.boolean(
        _plain(actual) == _plain(params.get("expected"))
    )


async def _binding_rows_equals(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    result = _result(observation)
    if result is None:
        return AssertionVerdict.boolean(False)
    actual = [
        thaw_json(item.values)
        for item in result.bindings.rows
    ]
    return AssertionVerdict.boolean(
        actual == params.get("expected", [])
    )


async def _evidence_count(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    result = _result(observation)
    return AssertionVerdict.boolean(
        result is not None
        and len(result.evidence) == params.get("expected")
    )


async def _provenance_sources(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    result = _result(observation)
    actual = (
        sorted(
            {
                item.source.source_id
                for item in result.provenance
            }
        )
        if result is not None
        else []
    )
    return AssertionVerdict.boolean(
        actual == sorted(params.get("expected", []))
    )


async def _snapshot_count(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    result = _result(observation)
    return AssertionVerdict.boolean(
        result is not None
        and len(result.snapshots) == params.get("expected")
    )


async def _error_type(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    error = observation.error
    expected = params.get("expected")
    return AssertionVerdict.boolean(
        error is not None
        and any(
            item.__name__ == expected
            for item in type(error).__mro__
        )
    )


async def _resolution_attempts(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    resolution = observation.resolution
    return AssertionVerdict.boolean(
        resolution is not None
        and len(resolution.plan_attempts) == params.get("expected")
    )


async def _trace_terminal(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    trace_id = observation.target_trace_id
    if trace_id is None:
        return AssertionVerdict.boolean(False)
    try:
        trace = await observation.environment.trace_store.get(trace_id)
    except Exception:
        return AssertionVerdict.boolean(False)
    expected = params.get("expected")
    if expected == "completed":
        passed = trace.completed and not trace.failed
    elif expected == "failed":
        passed = trace.failed and not trace.completed
    else:
        raise BenchmarkValidationError(
            "trace_terminal expected must be completed or failed"
        )
    return AssertionVerdict.boolean(passed)


async def _state_value_path_equals(
    observation: "RunObservation",
    params: Mapping[str, Any],
) -> AssertionVerdict:
    key = params.get("key")
    if not isinstance(key, str):
        raise BenchmarkValidationError(
            "state oracle key must be a string"
        )
    try:
        root = observation.environment.state[key]
        actual = _select(root, params.get("path", ()))
    except (AttributeError, IndexError, KeyError, TypeError):
        return AssertionVerdict.boolean(False)
    return AssertionVerdict.boolean(
        _plain(actual) == _plain(params.get("expected"))
    )
