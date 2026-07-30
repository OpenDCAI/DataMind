"""Pure runtime functions shared by live execution and offline replay."""
from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Any, Dict, Mapping, Sequence, Tuple

from datamind.dataops import (
    BindingPredicate,
    BindingRow,
    BindingSet,
    ComparisonOperator,
    Compose,
    ContextItem,
    ContextPack,
    Evidence,
    EvidenceSet,
    Filter,
    Fuse,
    Join,
    Project,
    ResultEnvelope,
    ResultKind,
    ResultStatus,
)
from datamind.kernel import (
    ExecutionError,
    Provenance,
    SnapshotRef,
)
from datamind.ports import SourceResult


_DATAFLOW_OPERATION_TYPES = (Compose, Project, Filter, Join, Fuse)


def is_dataflow_operation(operation: Any) -> bool:
    return isinstance(operation, _DATAFLOW_OPERATION_TYPES)


def execute_dataflow_operation(
    operation: Any,
    *,
    prior_results: Mapping[str, ResultEnvelope[Any]],
) -> SourceResult[Any]:
    if isinstance(operation, Compose):
        return compose_results(operation, prior_results=prior_results)
    if isinstance(operation, Project):
        return project_bindings(operation, prior_results=prior_results)
    if isinstance(operation, Filter):
        return filter_bindings(operation, prior_results=prior_results)
    if isinstance(operation, Join):
        return join_bindings(operation, prior_results=prior_results)
    if isinstance(operation, Fuse):
        return fuse_evidence(operation, prior_results=prior_results)
    raise ExecutionError(
        "operation {!r} is not a deterministic dataflow operation".format(
            getattr(operation, "operation", type(operation).__name__)
        )
    )


def compose_results(
    operation: Compose,
    *,
    prior_results: Mapping[str, ResultEnvelope[Any]],
) -> SourceResult[ContextPack]:
    items = []
    evidence = []
    provenance = []
    snapshots = []
    warnings = []
    partial = False
    seen_evidence = set()
    seen_provenance = set()
    seen_snapshots = set()

    for ref in operation.inputs:
        upstream = prior_results.get(ref.op_id)
        if upstream is None:
            raise ExecutionError(
                "compose input {!r} has not been executed".format(ref.op_id)
            )
        selected = select_path(upstream.value, ref.path)
        items.append(ContextItem(ref=ref, value=selected))
        partial = partial or upstream.status is ResultStatus.PARTIAL
        warnings.extend(upstream.warnings)

        for item in upstream.evidence:
            if item.evidence_id not in seen_evidence:
                seen_evidence.add(item.evidence_id)
                evidence.append(item)
        for item in upstream.provenance:
            key = _provenance_key(item)
            if key not in seen_provenance:
                seen_provenance.add(key)
                provenance.append(item)
        for item in upstream.snapshots:
            key = _snapshot_key(item)
            if key not in seen_snapshots:
                seen_snapshots.add(key)
                snapshots.append(item)

    context_pack = ContextPack(
        strategy=operation.strategy,
        items=tuple(items),
        evidence_ids=tuple(item.evidence_id for item in evidence),
    )
    return SourceResult(
        value=context_pack,
        result_kind=ResultKind.EVIDENCE_SET,
        evidence=tuple(evidence),
        provenance=tuple(provenance),
        snapshots=tuple(snapshots),
        warnings=tuple(warnings),
        status=ResultStatus.PARTIAL if partial else ResultStatus.OK,
    )


def project_bindings(
    operation: Project,
    *,
    prior_results: Mapping[str, ResultEnvelope[Any]],
) -> SourceResult[BindingSet]:
    upstream = _require_upstream(operation.inputs[0], prior_results)
    missing = tuple(
        field
        for field in operation.fields
        if field not in upstream.bindings.fields
    )
    if missing:
        raise ExecutionError(
            "project fields do not exist in upstream bindings: {}".format(
                ", ".join(missing)
            )
        )
    projected = BindingSet(
        fields=operation.fields,
        rows=tuple(
            BindingRow(
                values={
                    field: row.values[field]
                    for field in operation.fields
                },
                evidence_ids=row.evidence_ids,
            )
            for row in upstream.bindings.rows
        ),
    )
    return _binding_result(projected, upstream_results=(upstream,))


def filter_bindings(
    operation: Filter,
    *,
    prior_results: Mapping[str, ResultEnvelope[Any]],
) -> SourceResult[BindingSet]:
    upstream = _require_upstream(operation.inputs[0], prior_results)
    predicate = operation.predicate
    if predicate.field not in upstream.bindings.fields:
        raise ExecutionError(
            "filter field {!r} does not exist in upstream bindings".format(
                predicate.field
            )
        )
    filtered = BindingSet(
        fields=upstream.bindings.fields,
        rows=tuple(
            row
            for row in upstream.bindings.rows
            if _matches_predicate(row, predicate)
        ),
    )
    return _binding_result(filtered, upstream_results=(upstream,))


def join_bindings(
    operation: Join,
    *,
    prior_results: Mapping[str, ResultEnvelope[Any]],
) -> SourceResult[BindingSet]:
    left = _require_upstream(operation.inputs[0], prior_results)
    right = _require_upstream(operation.inputs[1], prior_results)
    _require_fields(left.bindings, operation.left_on, side="left")
    _require_fields(right.bindings, operation.right_on, side="right")

    right_index: Dict[Tuple[Any, ...], list] = {}
    for row in right.bindings.rows:
        key = _join_key(row, operation.right_on, side="right")
        if key is None:
            continue
        right_index.setdefault(key, []).append(row)

    fields = tuple(
        "{}.{}".format(operation.left_alias, field)
        for field in left.bindings.fields
    ) + tuple(
        "{}.{}".format(operation.right_alias, field)
        for field in right.bindings.fields
    )
    joined_rows = []
    for left_row in left.bindings.rows:
        key = _join_key(left_row, operation.left_on, side="left")
        if key is None:
            continue
        for right_row in right_index.get(key, ()):
            evidence_ids = tuple(
                dict.fromkeys(
                    left_row.evidence_ids + right_row.evidence_ids
                )
            )
            values = {
                "{}.{}".format(operation.left_alias, field): (
                    left_row.values[field]
                )
                for field in left.bindings.fields
            }
            values.update(
                {
                    "{}.{}".format(operation.right_alias, field): (
                        right_row.values[field]
                    )
                    for field in right.bindings.fields
                }
            )
            joined_rows.append(
                BindingRow(
                    values=values,
                    evidence_ids=evidence_ids,
                )
            )
    joined = BindingSet(fields=fields, rows=tuple(joined_rows))
    return _binding_result(
        joined,
        upstream_results=(left, right),
    )


def fuse_evidence(
    operation: Fuse,
    *,
    prior_results: Mapping[str, ResultEnvelope[Any]],
) -> SourceResult[EvidenceSet]:
    upstream_results = tuple(
        _require_upstream(ref, prior_results)
        for ref in operation.inputs
    )
    scores: Dict[Tuple[Any, ...], float] = {}
    representatives: Dict[Tuple[Any, ...], Tuple[int, Evidence]] = {}
    sequence = 0
    for upstream in upstream_results:
        for rank, item in enumerate(upstream.evidence, start=1):
            key = _provenance_key(item.provenance)
            scores[key] = scores.get(key, 0.0) + (
                1.0 / (operation.rank_constant + rank)
            )
            representatives.setdefault(key, (sequence, item))
            sequence += 1

    ranked = sorted(
        representatives,
        key=lambda key: (
            -scores[key],
            representatives[key][0],
        ),
    )[: operation.limit]
    evidence = tuple(
        replace(
            representatives[key][1],
            score=round(scores[key], 12),
        )
        for key in ranked
    )
    provenance = _unique_provenance(
        item.provenance for item in evidence
    )
    snapshots = _unique_snapshots(
        snapshot
        for upstream in upstream_results
        for snapshot in upstream.snapshots
    )
    warnings = tuple(
        warning
        for upstream in upstream_results
        for warning in upstream.warnings
    )
    partial = any(
        upstream.status is ResultStatus.PARTIAL
        for upstream in upstream_results
    )
    value = EvidenceSet(
        strategy=operation.strategy,
        evidence_ids=tuple(item.evidence_id for item in evidence),
    )
    return SourceResult(
        value=value,
        result_kind=ResultKind.EVIDENCE_SET,
        evidence=evidence,
        provenance=provenance,
        snapshots=snapshots,
        warnings=warnings,
        status=ResultStatus.PARTIAL if partial else ResultStatus.OK,
    )


def select_path(value: Any, path: Sequence[Any]) -> Any:
    selected = value
    for part in path:
        if isinstance(selected, Mapping):
            try:
                selected = selected[part]
            except KeyError as exc:
                raise ExecutionError(
                    "output path key {!r} does not exist".format(part)
                ) from exc
        elif (
            isinstance(selected, Sequence)
            and not isinstance(selected, (str, bytes, bytearray))
            and isinstance(part, int)
        ):
            try:
                selected = selected[part]
            except IndexError as exc:
                raise ExecutionError(
                    "output path index {} is out of range".format(part)
                ) from exc
        elif is_dataclass(selected) and isinstance(part, str):
            field_names = {item.name for item in fields(selected)}
            if part not in field_names:
                raise ExecutionError(
                    "output path field {!r} does not exist".format(part)
                )
            selected = getattr(selected, part)
        else:
            raise ExecutionError(
                "cannot apply output path item {!r} to {}".format(
                    part,
                    type(selected).__name__,
                )
            )
    return selected


def _binding_result(
    bindings: BindingSet,
    *,
    upstream_results: Tuple[ResultEnvelope[Any], ...],
) -> SourceResult[BindingSet]:
    referenced = {
        evidence_id
        for row in bindings.rows
        for evidence_id in row.evidence_ids
    }
    evidence = []
    seen_evidence = set()
    for upstream in upstream_results:
        for item in upstream.evidence:
            if (
                item.evidence_id in referenced
                and item.evidence_id not in seen_evidence
            ):
                seen_evidence.add(item.evidence_id)
                evidence.append(item)
    provenance = _unique_provenance(
        item.provenance for item in evidence
    )
    snapshots = _unique_snapshots(
        snapshot
        for upstream in upstream_results
        for snapshot in upstream.snapshots
    )
    warnings = tuple(
        warning
        for upstream in upstream_results
        for warning in upstream.warnings
    )
    partial = any(
        upstream.status is ResultStatus.PARTIAL
        for upstream in upstream_results
    )
    return SourceResult(
        value=bindings,
        result_kind=ResultKind.BINDING_SET,
        evidence=tuple(evidence),
        bindings=bindings,
        provenance=provenance,
        snapshots=snapshots,
        warnings=warnings,
        status=ResultStatus.PARTIAL if partial else ResultStatus.OK,
    )


def _require_upstream(
    ref: Any,
    prior_results: Mapping[str, ResultEnvelope[Any]],
) -> ResultEnvelope[Any]:
    upstream = prior_results.get(ref.op_id)
    if upstream is None:
        raise ExecutionError(
            "dataflow input {!r} has not been executed".format(ref.op_id)
        )
    return upstream


def _require_fields(
    bindings: BindingSet,
    fields: Tuple[str, ...],
    *,
    side: str,
) -> None:
    missing = tuple(field for field in fields if field not in bindings.fields)
    if missing:
        raise ExecutionError(
            "{} join fields do not exist: {}".format(
                side,
                ", ".join(missing),
            )
        )


def _join_key(
    row: BindingRow,
    fields: Tuple[str, ...],
    *,
    side: str,
) -> Any:
    values = tuple(row.values[field] for field in fields)
    if any(value is None for value in values):
        return None
    if any(
        not isinstance(value, (bool, int, float, str))
        for value in values
    ):
        raise ExecutionError(
            "{} join keys must contain only JSON scalar values".format(side)
        )
    return values


def _matches_predicate(
    row: BindingRow,
    predicate: BindingPredicate,
) -> bool:
    actual = row.values[predicate.field]
    expected = predicate.value
    operator = predicate.operator
    if operator is ComparisonOperator.EQ:
        return actual == expected
    if operator is ComparisonOperator.NE:
        return actual != expected
    if operator is ComparisonOperator.IN:
        if not isinstance(expected, tuple):
            raise ExecutionError(
                "filter 'in' predicate expects an array value"
            )
        return actual in expected
    if operator is ComparisonOperator.CONTAINS:
        if not isinstance(actual, (str, tuple)):
            raise ExecutionError(
                "filter 'contains' requires a string or array field"
            )
        return expected in actual
    try:
        if operator is ComparisonOperator.LT:
            return actual < expected
        if operator is ComparisonOperator.LE:
            return actual <= expected
        if operator is ComparisonOperator.GT:
            return actual > expected
        if operator is ComparisonOperator.GE:
            return actual >= expected
    except TypeError as exc:
        raise ExecutionError(
            "filter values are not comparable for operator {!r}".format(
                operator.value
            )
        ) from exc
    raise ExecutionError(
        "unsupported comparison operator {!r}".format(operator.value)
    )


def _provenance_key(item: Provenance) -> Tuple[Any, ...]:
    return (
        item.source,
        item.locator,
        item.snapshot,
        item.valid_from,
        item.valid_to,
    )


def _snapshot_key(item: SnapshotRef) -> Tuple[Any, ...]:
    return (item.source, item.version, item.checksum)


def _unique_provenance(items: Any) -> Tuple[Provenance, ...]:
    result = []
    seen = set()
    for item in items:
        key = _provenance_key(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return tuple(result)


def _unique_snapshots(items: Any) -> Tuple[SnapshotRef, ...]:
    result = []
    seen = set()
    for item in items:
        key = _snapshot_key(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return tuple(result)


__all__ = [
    "compose_results",
    "execute_dataflow_operation",
    "filter_bindings",
    "fuse_evidence",
    "is_dataflow_operation",
    "join_bindings",
    "project_bindings",
    "select_path",
]
