"""Pure runtime functions shared by live execution and offline replay."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any, Mapping, Sequence, Tuple

from datamind.dataops import (
    Compose,
    ContextItem,
    ContextPack,
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


__all__ = ["compose_results", "select_path"]
