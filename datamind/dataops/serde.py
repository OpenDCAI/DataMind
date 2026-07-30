"""Explicit, versioned JSON codec for the initial DataOps instruction set."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from datamind.kernel import (
    AssertMemory,
    Budget,
    EffectLevel,
    EvidenceRef,
    MemoryKind,
    MemoryLink,
    MemoryLinkKind,
    MemoryMutationDraft,
    MemoryMutationProposal,
    MemoryOrigin,
    MemoryOriginChannel,
    Provenance,
    RetractMemory,
    ScopeKind,
    ScopeRef,
    SerializationError,
    SkillRef,
    SnapshotRef,
    SourceKind,
    SourceRef,
    SupersedeMemory,
    thaw_json,
)

from .base import OutputRef
from .bindings import (
    ArgumentBinding,
    BindingCardinality,
    ValueBinding,
)
from .operations import (
    ApplyMutation,
    BindingPredicate,
    ComparisonOperator,
    Compose,
    Describe,
    Discover,
    Filter,
    Fuse,
    GraphDirection,
    InvokeSkill,
    Join,
    Project,
    ProposeMutation,
    Query,
    Recall,
    ResolveSkill,
    Search,
    Traverse,
)
from .plan import DataPlan

DATA_PLAN_SCHEMA = "datamind.data_plan"
DATA_PLAN_VERSION = "1"


def _source_to_dict(source: SourceRef) -> dict:
    return {"source_id": source.source_id, "kind": source.kind.value}


def _source_from_dict(payload: Mapping[str, Any]) -> SourceRef:
    return SourceRef(
        source_id=str(payload["source_id"]),
        kind=SourceKind(str(payload["kind"])),
    )


def _output_to_dict(ref: OutputRef) -> dict:
    return {"op_id": ref.op_id, "path": list(ref.path)}


def _output_from_dict(payload: Mapping[str, Any]) -> OutputRef:
    return OutputRef(
        op_id=str(payload["op_id"]),
        path=tuple(payload.get("path", ())),
    )


def _value_binding_to_dict(binding: ValueBinding) -> dict:
    return {
        "ref": _output_to_dict(binding.ref),
        "field": binding.field,
        "cardinality": binding.cardinality.value,
        "max_items": binding.max_items,
    }


def _value_binding_from_dict(
    payload: Mapping[str, Any],
) -> ValueBinding:
    return ValueBinding(
        ref=_output_from_dict(payload["ref"]),
        field=str(payload["field"]),
        cardinality=BindingCardinality(
            str(payload.get("cardinality", "single"))
        ),
        max_items=int(payload.get("max_items", 1)),
    )


def _argument_binding_to_dict(binding: ArgumentBinding) -> dict:
    return {
        "argument": binding.argument,
        "value": _value_binding_to_dict(binding.value),
    }


def _argument_binding_from_dict(
    payload: Mapping[str, Any],
) -> ArgumentBinding:
    return ArgumentBinding(
        argument=str(payload["argument"]),
        value=_value_binding_from_dict(payload["value"]),
    )


def _skill_ref_to_dict(skill: SkillRef) -> dict:
    return {
        "name": skill.name,
        "version": skill.version,
        "digest": skill.digest,
    }


def _skill_ref_from_dict(payload: Mapping[str, Any]) -> SkillRef:
    return SkillRef(
        name=str(payload["name"]),
        version=str(payload["version"]),
        digest=str(payload["digest"]),
    )


def _scope_to_dict(scope: ScopeRef) -> dict:
    return {"kind": scope.kind.value, "scope_id": scope.scope_id}


def _scope_from_dict(payload: Mapping[str, Any]) -> ScopeRef:
    return ScopeRef(
        kind=ScopeKind(str(payload["kind"])),
        scope_id=str(payload["scope_id"]),
    )


def _snapshot_to_dict(snapshot: SnapshotRef) -> dict:
    return {
        "source": _source_to_dict(snapshot.source),
        "version": snapshot.version,
        "checksum": snapshot.checksum,
        "observed_at": snapshot.observed_at.isoformat(),
    }


def _snapshot_from_dict(payload: Mapping[str, Any]) -> SnapshotRef:
    return SnapshotRef(
        source=_source_from_dict(payload["source"]),
        version=str(payload["version"]),
        checksum=payload.get("checksum"),
        observed_at=datetime.fromisoformat(str(payload["observed_at"])),
    )


def _provenance_to_dict(provenance: Provenance) -> dict:
    return {
        "source": _source_to_dict(provenance.source),
        "locator": provenance.locator,
        "observed_at": provenance.observed_at.isoformat(),
        "snapshot": (
            _snapshot_to_dict(provenance.snapshot)
            if provenance.snapshot is not None
            else None
        ),
        "valid_from": _datetime_to_json(provenance.valid_from),
        "valid_to": _datetime_to_json(provenance.valid_to),
        "derived_from": list(provenance.derived_from),
    }


def _provenance_from_dict(payload: Mapping[str, Any]) -> Provenance:
    snapshot = payload.get("snapshot")
    return Provenance(
        source=_source_from_dict(payload["source"]),
        locator=str(payload["locator"]),
        observed_at=datetime.fromisoformat(str(payload["observed_at"])),
        snapshot=(
            _snapshot_from_dict(snapshot)
            if snapshot is not None
            else None
        ),
        valid_from=_datetime_from_json(payload.get("valid_from")),
        valid_to=_datetime_from_json(payload.get("valid_to")),
        derived_from=tuple(payload.get("derived_from", ())),
    )


def _evidence_ref_to_dict(evidence: EvidenceRef) -> dict:
    return {
        "evidence_id": evidence.evidence_id,
        "provenance": _provenance_to_dict(evidence.provenance),
    }


def _evidence_ref_from_dict(payload: Mapping[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=str(payload["evidence_id"]),
        provenance=_provenance_from_dict(payload["provenance"]),
    )


def _link_to_dict(link: MemoryLink) -> dict:
    return {
        "kind": link.kind.value,
        "target_id": link.target_id,
    }


def _link_from_dict(payload: Mapping[str, Any]) -> MemoryLink:
    return MemoryLink(
        kind=MemoryLinkKind(str(payload["kind"])),
        target_id=str(payload["target_id"]),
    )


def _change_to_dict(change: Any) -> dict:
    common = {"action": change.action.value}
    if isinstance(change, AssertMemory):
        common.update(
            {
                "kind": change.kind.value,
                "content": change.content,
                "valid_from": _datetime_to_json(change.valid_from),
                "valid_to": _datetime_to_json(change.valid_to),
                "evidence": [
                    _evidence_ref_to_dict(item)
                    for item in change.evidence
                ],
                "links": [
                    _link_to_dict(item) for item in change.links
                ],
                "metadata": thaw_json(change.metadata),
            }
        )
    elif isinstance(change, SupersedeMemory):
        common.update(
            {
                "target_id": change.target_id,
                "content": change.content,
                "valid_from": _datetime_to_json(change.valid_from),
                "valid_to": _datetime_to_json(change.valid_to),
                "evidence": [
                    _evidence_ref_to_dict(item)
                    for item in change.evidence
                ],
                "links": [
                    _link_to_dict(item) for item in change.links
                ],
                "metadata": thaw_json(change.metadata),
            }
        )
    elif isinstance(change, RetractMemory):
        common.update(
            {
                "target_id": change.target_id,
                "reason": change.reason,
                "evidence": [
                    _evidence_ref_to_dict(item)
                    for item in change.evidence
                ],
            }
        )
    else:
        raise SerializationError(
            "unsupported memory change {!r}".format(
                type(change).__name__
            )
        )
    return common


def _change_from_dict(payload: Mapping[str, Any]) -> Any:
    action = str(payload["action"])
    evidence = tuple(
        _evidence_ref_from_dict(item)
        for item in payload.get("evidence", ())
    )
    if action == "assert":
        return AssertMemory(
            kind=MemoryKind(str(payload["kind"])),
            content=str(payload["content"]),
            valid_from=_datetime_from_json(payload.get("valid_from")),
            valid_to=_datetime_from_json(payload.get("valid_to")),
            evidence=evidence,
            links=tuple(
                _link_from_dict(item)
                for item in payload.get("links", ())
            ),
            metadata=payload.get("metadata", {}),
        )
    if action == "supersede":
        return SupersedeMemory(
            target_id=str(payload["target_id"]),
            content=str(payload["content"]),
            valid_from=_datetime_from_json(payload.get("valid_from")),
            valid_to=_datetime_from_json(payload.get("valid_to")),
            evidence=evidence,
            links=tuple(
                _link_from_dict(item)
                for item in payload.get("links", ())
            ),
            metadata=payload.get("metadata", {}),
        )
    if action == "retract":
        return RetractMemory(
            target_id=str(payload["target_id"]),
            reason=str(payload["reason"]),
            evidence=evidence,
        )
    raise SerializationError(
        "unknown memory change action {!r}".format(action)
    )


def _draft_to_dict(draft: MemoryMutationDraft) -> dict:
    return {
        "scope": _scope_to_dict(draft.scope),
        "changes": [_change_to_dict(item) for item in draft.changes],
        "idempotency_key": draft.idempotency_key,
        "approval_key": draft.approval_key,
    }


def _draft_from_dict(payload: Mapping[str, Any]) -> MemoryMutationDraft:
    return MemoryMutationDraft(
        scope=_scope_from_dict(payload["scope"]),
        changes=tuple(
            _change_from_dict(item)
            for item in payload.get("changes", ())
        ),
        idempotency_key=str(payload["idempotency_key"]),
        approval_key=payload.get("approval_key"),
    )


def _origin_to_dict(origin: MemoryOrigin) -> dict:
    return {
        "channel": origin.channel.value,
        "trace_id": origin.trace_id,
    }


def _origin_from_dict(payload: Mapping[str, Any]) -> MemoryOrigin:
    return MemoryOrigin(
        channel=MemoryOriginChannel(str(payload["channel"])),
        trace_id=payload.get("trace_id"),
    )


def _proposal_to_dict(proposal: MemoryMutationProposal) -> dict:
    return {
        "proposal_id": proposal.proposal_id,
        "source": _source_to_dict(proposal.source),
        "base_snapshot": _snapshot_to_dict(proposal.base_snapshot),
        "draft": _draft_to_dict(proposal.draft),
        "origin": _origin_to_dict(proposal.origin),
        "requires_approval": proposal.requires_approval,
    }


def _proposal_from_dict(
    payload: Mapping[str, Any],
) -> MemoryMutationProposal:
    return MemoryMutationProposal(
        proposal_id=str(payload["proposal_id"]),
        source=_source_from_dict(payload["source"]),
        base_snapshot=_snapshot_from_dict(payload["base_snapshot"]),
        draft=_draft_from_dict(payload["draft"]),
        origin=_origin_from_dict(payload["origin"]),
        requires_approval=payload["requires_approval"],
    )


def _datetime_to_json(value: Any) -> Any:
    return value.isoformat() if value is not None else None


def _datetime_from_json(value: Any) -> Any:
    return datetime.fromisoformat(str(value)) if value is not None else None


def operation_to_dict(op: Any) -> dict:
    common = {"type": op.operation, "op_id": op.op_id}
    if isinstance(op, Discover):
        common["kinds"] = [kind.value for kind in op.kinds]
    elif isinstance(op, Describe):
        common["source"] = _source_to_dict(op.source)
    elif isinstance(op, Search):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "query": op.query,
                "limit": op.limit,
                "filters": thaw_json(op.filters),
            }
        )
    elif isinstance(op, Query):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "statement": op.statement,
                "language": op.language,
                "parameters": thaw_json(op.parameters),
            }
        )
    elif isinstance(op, Traverse):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "starts": list(op.starts),
                "start_binding": (
                    _value_binding_to_dict(op.start_binding)
                    if op.start_binding is not None
                    else None
                ),
                "direction": op.direction.value,
                "relations": list(op.relations),
                "min_hops": op.min_hops,
                "max_hops": op.max_hops,
                "limit": op.limit,
                "simple_paths": op.simple_paths,
            }
        )
    elif isinstance(op, Recall):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "query": op.query,
                "scopes": [
                    _scope_to_dict(scope) for scope in op.scopes
                ],
                "kinds": [kind.value for kind in op.kinds],
                "valid_at": _datetime_to_json(op.valid_at),
                "known_at": _datetime_to_json(op.known_at),
                "limit": op.limit,
            }
        )
    elif isinstance(op, ResolveSkill):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "query": op.query,
                "limit": op.limit,
            }
        )
    elif isinstance(op, InvokeSkill):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "skill": _skill_ref_to_dict(op.skill),
                "governed_effect": op.governed_effect.name,
                "arguments": thaw_json(op.arguments),
                "argument_bindings": [
                    _argument_binding_to_dict(item)
                    for item in op.argument_bindings
                ],
                "reversible": op.reversible,
                "requires_approval": op.requires_approval,
                "approval_key": op.approval_key,
                "idempotency_key": op.idempotency_key,
            }
        )
    elif isinstance(op, ProposeMutation):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "draft": _draft_to_dict(op.draft),
            }
        )
    elif isinstance(op, ApplyMutation):
        common.update(
            {
                "source": _source_to_dict(op.source),
                "proposal": _proposal_to_dict(op.proposal),
            }
        )
    elif isinstance(op, Project):
        common.update(
            {
                "inputs": [_output_to_dict(ref) for ref in op.inputs],
                "fields": list(op.fields),
            }
        )
    elif isinstance(op, Filter):
        common.update(
            {
                "inputs": [_output_to_dict(ref) for ref in op.inputs],
                "predicate": {
                    "field": op.predicate.field,
                    "operator": op.predicate.operator.value,
                    "value": thaw_json(op.predicate.value),
                },
            }
        )
    elif isinstance(op, Join):
        common.update(
            {
                "inputs": [_output_to_dict(ref) for ref in op.inputs],
                "left_on": list(op.left_on),
                "right_on": list(op.right_on),
                "left_alias": op.left_alias,
                "right_alias": op.right_alias,
            }
        )
    elif isinstance(op, Fuse):
        common.update(
            {
                "inputs": [_output_to_dict(ref) for ref in op.inputs],
                "strategy": op.strategy,
                "limit": op.limit,
                "rank_constant": op.rank_constant,
            }
        )
    elif isinstance(op, Compose):
        common.update(
            {
                "inputs": [_output_to_dict(ref) for ref in op.inputs],
                "strategy": op.strategy,
            }
        )
    else:
        raise SerializationError(
            "unsupported operation type {!r}".format(type(op).__name__)
        )
    return common


def operation_from_dict(payload: Mapping[str, Any]) -> Any:
    try:
        op_type = str(payload["type"])
        op_id = str(payload["op_id"])
        if op_type == "discover":
            return Discover(
                op_id=op_id,
                kinds=tuple(
                    SourceKind(str(kind)) for kind in payload.get("kinds", ())
                ),
            )
        if op_type == "describe":
            return Describe(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
            )
        if op_type == "search":
            return Search(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                query=str(payload["query"]),
                limit=int(payload.get("limit", 10)),
                filters=payload.get("filters", {}),
            )
        if op_type == "query":
            return Query(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                statement=str(payload["statement"]),
                language=str(payload.get("language", "sql")),
                parameters=payload.get("parameters", {}),
            )
        if op_type == "traverse":
            start_binding = payload.get("start_binding")
            return Traverse(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                starts=tuple(
                    str(item) for item in payload.get("starts", ())
                ),
                start_binding=(
                    _value_binding_from_dict(start_binding)
                    if start_binding is not None
                    else None
                ),
                direction=GraphDirection(
                    str(payload.get("direction", "out"))
                ),
                relations=tuple(
                    str(item)
                    for item in payload.get("relations", ())
                ),
                min_hops=int(payload.get("min_hops", 1)),
                max_hops=int(payload.get("max_hops", 2)),
                limit=int(payload.get("limit", 100)),
                simple_paths=payload.get("simple_paths", True),
            )
        if op_type == "recall":
            return Recall(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                query=str(payload["query"]),
                scopes=tuple(
                    _scope_from_dict(item)
                    for item in payload.get("scopes", ())
                ),
                kinds=tuple(
                    MemoryKind(str(item))
                    for item in payload.get("kinds", ())
                ),
                valid_at=_datetime_from_json(payload.get("valid_at")),
                known_at=_datetime_from_json(payload.get("known_at")),
                limit=int(payload.get("limit", 10)),
            )
        if op_type == "resolve_skill":
            return ResolveSkill(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                query=str(payload["query"]),
                limit=int(payload.get("limit", 5)),
            )
        if op_type == "invoke_skill":
            return InvokeSkill(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                skill=_skill_ref_from_dict(payload["skill"]),
                governed_effect=EffectLevel[
                    str(payload["governed_effect"])
                ],
                arguments=payload.get("arguments", {}),
                argument_bindings=tuple(
                    _argument_binding_from_dict(item)
                    for item in payload.get("argument_bindings", ())
                ),
                reversible=payload.get("reversible", True),
                requires_approval=payload.get(
                    "requires_approval", False
                ),
                approval_key=payload.get("approval_key"),
                idempotency_key=payload.get("idempotency_key"),
            )
        if op_type == "propose_mutation":
            return ProposeMutation(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                draft=_draft_from_dict(payload["draft"]),
            )
        if op_type == "apply_mutation":
            return ApplyMutation(
                op_id=op_id,
                source=_source_from_dict(payload["source"]),
                proposal=_proposal_from_dict(payload["proposal"]),
            )
        if op_type == "project":
            return Project(
                op_id=op_id,
                inputs=tuple(
                    _output_from_dict(item)
                    for item in payload.get("inputs", ())
                ),
                fields=tuple(
                    str(item) for item in payload.get("fields", ())
                ),
            )
        if op_type == "filter":
            predicate = payload["predicate"]
            return Filter(
                op_id=op_id,
                inputs=tuple(
                    _output_from_dict(item)
                    for item in payload.get("inputs", ())
                ),
                predicate=BindingPredicate(
                    field=str(predicate["field"]),
                    operator=ComparisonOperator(
                        str(predicate["operator"])
                    ),
                    value=predicate.get("value"),
                ),
            )
        if op_type == "join":
            return Join(
                op_id=op_id,
                inputs=tuple(
                    _output_from_dict(item)
                    for item in payload.get("inputs", ())
                ),
                left_on=tuple(
                    str(item) for item in payload.get("left_on", ())
                ),
                right_on=tuple(
                    str(item) for item in payload.get("right_on", ())
                ),
                left_alias=str(payload.get("left_alias", "left")),
                right_alias=str(payload.get("right_alias", "right")),
            )
        if op_type == "fuse":
            return Fuse(
                op_id=op_id,
                inputs=tuple(
                    _output_from_dict(item)
                    for item in payload.get("inputs", ())
                ),
                strategy=str(payload.get("strategy", "rrf")),
                limit=int(payload.get("limit", 20)),
                rank_constant=int(payload.get("rank_constant", 60)),
            )
        if op_type == "compose":
            return Compose(
                op_id=op_id,
                inputs=tuple(
                    _output_from_dict(item)
                    for item in payload.get("inputs", ())
                ),
                strategy=str(payload.get("strategy", "evidence_union")),
            )
        raise SerializationError(
            "unknown operation type {!r}".format(op_type)
        )
    except SerializationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError(
            "invalid operation payload: {}".format(exc)
        ) from exc


def _budget_to_dict(budget: Budget) -> dict:
    return {
        "max_tokens": budget.max_tokens,
        "max_latency_ms": budget.max_latency_ms,
        "max_cost_usd": (
            str(budget.max_cost_usd)
            if budget.max_cost_usd is not None
            else None
        ),
        "max_actions": budget.max_actions,
    }


def _budget_from_dict(payload: Mapping[str, Any]) -> Budget:
    cost = payload.get("max_cost_usd")
    return Budget(
        max_tokens=payload.get("max_tokens"),
        max_latency_ms=payload.get("max_latency_ms"),
        max_cost_usd=Decimal(str(cost)) if cost is not None else None,
        max_actions=payload.get("max_actions"),
    )


def plan_to_dict(plan: DataPlan) -> dict:
    return {
        "schema": DATA_PLAN_SCHEMA,
        "version": plan.version,
        "plan_id": plan.plan_id,
        "description": plan.description,
        "max_effect": plan.max_effect.name,
        "budget": _budget_to_dict(plan.budget),
        "operations": [operation_to_dict(op) for op in plan.operations],
        "output": _output_to_dict(plan.output),
    }


def plan_from_dict(payload: Mapping[str, Any]) -> DataPlan:
    try:
        if payload.get("schema") != DATA_PLAN_SCHEMA:
            raise SerializationError("unsupported plan schema")
        version = str(payload["version"])
        if version != DATA_PLAN_VERSION:
            raise SerializationError(
                "unsupported data plan version {!r}".format(version)
            )
        return DataPlan(
            plan_id=str(payload["plan_id"]),
            version=version,
            description=payload.get("description"),
            max_effect=EffectLevel[str(payload.get("max_effect", "READ"))],
            budget=_budget_from_dict(payload.get("budget", {})),
            operations=tuple(
                operation_from_dict(item)
                for item in payload["operations"]
            ),
            output=_output_from_dict(payload["output"]),
        )
    except SerializationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SerializationError(
            "invalid data plan payload: {}".format(exc)
        ) from exc


def plan_to_json(plan: DataPlan, *, indent: Any = None) -> str:
    return json.dumps(
        plan_to_dict(plan),
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
    )


def plan_from_json(raw: str) -> DataPlan:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SerializationError("invalid data plan JSON: {}".format(exc)) from exc
    if not isinstance(payload, Mapping):
        raise SerializationError("data plan JSON must be an object")
    return plan_from_dict(payload)
