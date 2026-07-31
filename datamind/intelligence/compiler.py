"""Natural-language to DataPlan compiler with deterministic verification."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional, Tuple

from datamind.dataops import (
    DATA_PLAN_SCHEMA,
    DATA_PLAN_VERSION,
    ApplyMutation,
    DataPlan,
    InvokeSkill,
    ProposeMutation,
    Recall,
    data_plan_draft_schema,
    plan_from_dict,
    plan_to_dict,
    validate_plan,
)
from datamind.kernel import (
    Budget,
    EffectLevel,
    PlanCompilationError,
    SerializationError,
    SourceDescriptor,
    Usage,
    json_object_violations,
    new_id,
    thaw_json,
)
from datamind.ports import (
    CompilationAttempt,
    CompilationIssue,
    CompiledPlan,
    ModelOutputError,
    ModelPort,
    PlanningRequest,
    StructuredModelRequest,
)


_CORE_COMPOSITION_OPERATIONS = (
    "discover",
    "describe",
    "project",
    "filter",
    "join",
    "fuse",
    "compose",
)

_SYSTEM_INSTRUCTION = """\
You compile an authorized enterprise data request into one finite DataPlan DAG.
Return only the structured plan draft selected by the output schema.
Use exact source_id, scope, Skill identity, and field names from the catalog.
Every operation must contribute to the declared output. Use unique op_id values.
Independent operations should not depend on each other; express dependencies
only with output references. Never invent permissions, approvals, effects,
source kinds, plan budgets, or provider capabilities. Use JSON-encoded strings
for fields ending in _json. The runtime, not you, executes the plan.
When runtime_recovery is present, return one complete replacement plan using
only its sanitized failure facts; do not return a patch or repeat a failed
source operation unchanged when an authorized alternative can satisfy intent.
"""


class _DraftError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        op_id: Optional[str] = None,
    ) -> None:
        self.code = code
        self.message = message
        self.op_id = op_id
        super().__init__(message)


class DataPlanCompiler:
    """Compile once, verify deterministically, and repair at most once."""

    def __init__(
        self,
        model: ModelPort,
        *,
        max_attempts: int = 2,
        max_output_tokens: int = 4096,
    ) -> None:
        if max_attempts not in (1, 2):
            raise ValueError(
                "DataMind 0.8 supports one attempt or one bounded repair"
            )
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be positive")
        self._model = model
        self._max_attempts = max_attempts
        self._max_output_tokens = max_output_tokens

    async def compile(self, request: PlanningRequest) -> CompiledPlan:
        sources = {
            item.ref.source_id: item
            for item in request.sources
        }
        allowed_operations = self._allowed_operations(request)
        output_schema = data_plan_draft_schema(
            allowed_operations=allowed_operations,
        )
        base_input = self._compiler_input(
            request,
            allowed_operations=allowed_operations,
        )
        attempts = []
        total_usage = Usage()
        previous_output: Optional[Mapping[str, Any]] = None
        previous_issues: Tuple[CompilationIssue, ...] = ()

        for number in range(1, self._max_attempts + 1):
            request.budget.require(
                total_usage + Usage(actions=1)
            )
            input_payload = dict(base_input)
            if previous_issues:
                input_payload["repair"] = {
                    "diagnostics": [
                        {
                            "code": item.code,
                            "message": item.message,
                            "op_id": item.op_id,
                        }
                        for item in previous_issues
                    ],
                    "previous_output": (
                        thaw_json(previous_output)
                        if previous_output is not None
                        else None
                    ),
                }
            try:
                response = await self._model.generate_structured(
                    StructuredModelRequest(
                        instruction=_SYSTEM_INSTRUCTION,
                        input_text=json.dumps(
                            input_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        output_schema=output_schema,
                        schema_name="datamind_plan_draft",
                        max_output_tokens=self._max_output_tokens,
                        temperature=0.0,
                    )
                )
            except ModelOutputError as exc:
                call_usage = exc.usage + Usage(actions=1)
                total_usage = total_usage + call_usage
                request.budget.require(total_usage)
                issues = (
                    CompilationIssue(
                        code="model_output_error",
                        message=(
                            "model returned no usable structured object"
                        ),
                    ),
                )
                attempts.append(
                    CompilationAttempt(
                        number=number,
                        model=exc.model,
                        usage=call_usage,
                        issues=issues,
                        response_id=exc.response_id,
                    )
                )
                previous_output = None
                previous_issues = issues
                continue

            call_usage = response.usage + Usage(actions=1)
            total_usage = total_usage + call_usage
            request.budget.require(total_usage)
            previous_output = response.output
            output_fingerprint = self._fingerprint(response.output)
            issues, plan = self._compile_output(
                response.output,
                request=request,
                sources=sources,
                remaining_budget=request.budget.remaining(total_usage),
            )
            attempts.append(
                CompilationAttempt(
                    number=number,
                    model=response.model,
                    usage=call_usage,
                    issues=issues,
                    response_id=response.response_id,
                    output_fingerprint=output_fingerprint,
                )
            )
            if plan is not None:
                return CompiledPlan(
                    plan=plan,
                    attempts=tuple(attempts),
                    usage=total_usage,
                )
            previous_issues = issues

        final_issues = previous_issues or (
            CompilationIssue(
                code="compilation_failed",
                message="no valid plan was produced",
            ),
        )
        raise PlanCompilationError(
            item.render() for item in final_issues
        )

    def _compile_output(
        self,
        output: Mapping[str, Any],
        *,
        request: PlanningRequest,
        sources: Mapping[str, SourceDescriptor],
        remaining_budget: Budget,
    ) -> Tuple[
        Tuple[CompilationIssue, ...],
        Optional[DataPlan],
    ]:
        try:
            payload = self._normalize_plan(
                output,
                request=request,
                sources=sources,
                remaining_budget=remaining_budget,
            )
            plan = plan_from_dict(payload)
        except _DraftError as exc:
            return (
                CompilationIssue(
                    code=exc.code,
                    message=exc.message,
                    op_id=exc.op_id,
                ),
            ), None
        except (SerializationError, KeyError, TypeError, ValueError) as exc:
            return (
                CompilationIssue(
                    code="serialization_error",
                    message=str(exc),
                ),
            ), None

        report = validate_plan(plan, sources=sources)
        issues = tuple(
            CompilationIssue(
                code=item.code,
                message=item.message,
                op_id=item.op_id,
            )
            for item in report.issues
        )
        issues += self._policy_issues(plan, request=request)
        if issues:
            return issues, None
        return (), plan

    def _normalize_plan(
        self,
        output: Mapping[str, Any],
        *,
        request: PlanningRequest,
        sources: Mapping[str, SourceDescriptor],
        remaining_budget: Budget,
    ) -> dict:
        operations = output["operations"]
        if isinstance(operations, (str, bytes)) or not isinstance(
            operations, (list, tuple)
        ):
            raise _DraftError(
                "invalid_operations",
                "operations must be an array",
            )
        normalized = [
            self._normalize_operation(
                item,
                request=request,
                sources=sources,
            )
            for item in operations
        ]
        return {
            "schema": DATA_PLAN_SCHEMA,
            "version": DATA_PLAN_VERSION,
            "plan_id": new_id("plan"),
            "description": output.get("description"),
            "max_effect": request.max_effect.name,
            "budget": {
                "max_tokens": remaining_budget.max_tokens,
                "max_latency_ms": remaining_budget.max_latency_ms,
                "max_cost_usd": (
                    str(remaining_budget.max_cost_usd)
                    if remaining_budget.max_cost_usd is not None
                    else None
                ),
                "max_actions": remaining_budget.max_actions,
            },
            "operations": normalized,
            "output": thaw_json(output["output"]),
        }

    def _normalize_operation(
        self,
        value: Any,
        *,
        request: PlanningRequest,
        sources: Mapping[str, SourceDescriptor],
    ) -> dict:
        if not isinstance(value, Mapping):
            raise _DraftError(
                "invalid_operation",
                "each operation must be an object",
            )
        operation = thaw_json(value)
        op_type = str(operation.get("type", ""))
        op_id = str(operation.get("op_id", "")) or None
        if op_type == "apply_mutation":
            raise _DraftError(
                "automatic_write_forbidden",
                "resolve() cannot compile ApplyMutation",
                op_id,
            )
        if "source" in operation:
            source_id = operation["source"]
            descriptor = sources.get(source_id)
            if descriptor is None:
                raise _DraftError(
                    "unknown_source",
                    "source {!r} is not authorized".format(source_id),
                    op_id,
                )
            operation["source"] = {
                "source_id": descriptor.ref.source_id,
                "kind": descriptor.ref.kind.value,
            }

        if op_type == "search":
            operation["filters"] = self._json_object(
                operation.pop("filters_json"),
                field_name="filters_json",
                op_id=op_id,
            )
        elif op_type == "query":
            operation["parameters"] = self._json_object(
                operation.pop("parameters_json"),
                field_name="parameters_json",
                op_id=op_id,
            )
        elif op_type == "filter":
            predicate = operation["predicate"]
            predicate["value"] = self._json_value(
                predicate.pop("value_json"),
                field_name="value_json",
                op_id=op_id,
            )
        elif op_type == "invoke_skill":
            operation = self._normalize_skill(
                operation,
                descriptor=descriptor,
                op_id=op_id,
            )
        elif op_type == "propose_mutation":
            operation = self._normalize_mutation(
                operation,
                request=request,
                op_id=op_id,
            )
        return operation

    def _normalize_skill(
        self,
        operation: dict,
        *,
        descriptor: SourceDescriptor,
        op_id: Optional[str],
    ) -> dict:
        skill = operation["skill"]
        governed = thaw_json(descriptor.schema).get(
            "governed_skills",
            [],
        )
        trusted = next(
            (
                item
                for item in governed
                if item.get("name") == skill.get("name")
                and item.get("version") == skill.get("version")
                and item.get("digest") == skill.get("digest")
            ),
            None,
        )
        if trusted is None:
            raise _DraftError(
                "unknown_skill",
                "Skill identity is absent from the authorized catalog",
                op_id,
            )
        effect = EffectLevel[str(trusted["effect"])]
        if effect > EffectLevel.READ:
            raise _DraftError(
                "automatic_write_forbidden",
                "resolve() cannot invoke a write-effect Skill",
                op_id,
            )
        requires_approval = bool(
            trusted.get("requires_approval", False)
        )
        if requires_approval:
            raise _DraftError(
                "automatic_approval_forbidden",
                "resolve() cannot synthesize Skill approval",
                op_id,
            )
        arguments = self._json_object(
            operation.pop("arguments_json"),
            field_name="arguments_json",
            op_id=op_id,
        )
        argument_violations = json_object_violations(
            arguments,
            trusted.get("input_schema", {}),
            label="Skill input",
        )
        if argument_violations:
            raise _DraftError(
                "invalid_skill_arguments",
                "; ".join(argument_violations),
                op_id,
            )
        operation["arguments"] = arguments
        operation.update(
            {
                "governed_effect": effect.name,
                "reversible": bool(trusted.get("reversible", True)),
                "requires_approval": False,
                "approval_key": None,
                "idempotency_key": None,
            }
        )
        return operation

    def _normalize_mutation(
        self,
        operation: dict,
        *,
        request: PlanningRequest,
        op_id: Optional[str],
    ) -> dict:
        draft = operation["draft"]
        changes = []
        for change in draft["changes"]:
            normalized = dict(change)
            normalized["evidence"] = []
            if normalized["action"] in ("assert", "supersede"):
                normalized["metadata"] = self._json_object(
                    normalized.pop("metadata_json"),
                    field_name="metadata_json",
                    op_id=op_id,
                )
            changes.append(normalized)
        draft["changes"] = changes
        draft["idempotency_key"] = "resolve:{}:{}".format(
            request.request_id,
            op_id or "mutation",
        )
        draft["approval_key"] = None
        return operation

    def _policy_issues(
        self,
        plan: DataPlan,
        *,
        request: PlanningRequest,
    ) -> Tuple[CompilationIssue, ...]:
        issues = []
        readable = frozenset(request.readable_scopes)
        writable = frozenset(request.writable_scopes)
        for operation in plan.operations:
            if isinstance(operation, ApplyMutation):
                issues.append(
                    CompilationIssue(
                        "automatic_write_forbidden",
                        "resolve() cannot execute ApplyMutation",
                        operation.op_id,
                    )
                )
            elif isinstance(operation, Recall):
                missing = frozenset(operation.scopes) - readable
                if missing:
                    issues.append(
                        CompilationIssue(
                            "unauthorized_memory_scope",
                            "Recall references a scope outside the "
                            "authorized planning view",
                            operation.op_id,
                        )
                    )
            elif isinstance(operation, ProposeMutation):
                scope = operation.draft.scope
                if scope not in readable or scope not in writable:
                    issues.append(
                        CompilationIssue(
                            "unauthorized_memory_proposal",
                            "Memory proposal scope is not authorized for "
                            "read and proposal",
                            operation.op_id,
                        )
                    )
            elif (
                isinstance(operation, InvokeSkill)
                and operation.effect.level > EffectLevel.READ
            ):
                issues.append(
                    CompilationIssue(
                        "automatic_write_forbidden",
                        "resolve() cannot invoke a write-effect Skill",
                        operation.op_id,
                    )
                )
        return tuple(issues)

    def _allowed_operations(
        self,
        request: PlanningRequest,
    ) -> Tuple[str, ...]:
        capabilities = {
            capability
            for source in request.sources
            for capability in source.capabilities
        }
        selected = set(_CORE_COMPOSITION_OPERATIONS)
        selected.update(capabilities)
        selected.discard("apply_mutation")
        if not request.writable_scopes:
            selected.discard("propose_mutation")
        return tuple(
            name
            for name in (
                "discover",
                "describe",
                "search",
                "query",
                "traverse",
                "recall",
                "resolve_skill",
                "invoke_skill",
                "propose_mutation",
                "project",
                "filter",
                "join",
                "fuse",
                "compose",
            )
            if name in selected
        )

    def _compiler_input(
        self,
        request: PlanningRequest,
        *,
        allowed_operations: Iterable[str],
    ) -> dict:
        allowed = frozenset(allowed_operations)
        payload = {
            "intent": request.intent,
            "catalog": [
                {
                    "source_id": source.ref.source_id,
                    "kind": source.ref.kind.value,
                    "display_name": source.display_name,
                    "capabilities": sorted(
                        source.capabilities & allowed
                    ),
                    "max_effect": source.max_effect.name,
                    "version": source.version,
                    "schema": thaw_json(source.schema),
                }
                for source in sorted(
                    request.sources,
                    key=lambda item: item.ref.source_id,
                )
            ],
            "authorized_memory": {
                "readable_scopes": [
                    {
                        "kind": item.kind.value,
                        "scope_id": item.scope_id,
                    }
                    for item in request.readable_scopes
                ],
                "proposal_scopes": [
                    {
                        "kind": item.kind.value,
                        "scope_id": item.scope_id,
                    }
                    for item in request.writable_scopes
                ],
            },
            "constraints": {
                "allowed_operations": sorted(allowed),
                "max_effect": request.max_effect.name,
                "max_actions": request.budget.max_actions,
                "external_writes": False,
                "destructive_actions": False,
            },
        }
        if request.replanning is not None:
            failure = request.replanning.failure
            payload["runtime_recovery"] = {
                "attempt_number": request.replanning.attempt_number,
                "previous_plan": plan_to_dict(
                    request.replanning.previous_plan
                ),
                "failure": {
                    "kind": failure.kind.value,
                    "error_type": failure.error_type,
                    "error_fingerprint": failure.error_fingerprint,
                    "failed_op_id": failure.failed_op_id,
                    "operation": failure.operation,
                    "source_id": failure.source_id,
                    "completed_op_ids": list(
                        failure.completed_op_ids
                    ),
                    "recoverable": failure.recoverable,
                    "usage": {
                        "tokens": failure.usage.tokens,
                        "latency_ms": failure.usage.latency_ms,
                        "cost_usd": str(failure.usage.cost_usd),
                        "actions": failure.usage.actions,
                    },
                },
            }
        return payload

    @staticmethod
    def _json_value(
        raw: Any,
        *,
        field_name: str,
        op_id: Optional[str],
    ) -> Any:
        if not isinstance(raw, str):
            raise _DraftError(
                "invalid_json_field",
                "{} must be a JSON-encoded string".format(field_name),
                op_id,
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _DraftError(
                "invalid_json_field",
                "{} contains invalid JSON: {}".format(
                    field_name,
                    exc,
                ),
                op_id,
            ) from exc

    @classmethod
    def _json_object(
        cls,
        raw: Any,
        *,
        field_name: str,
        op_id: Optional[str],
    ) -> dict:
        value = cls._json_value(
            raw,
            field_name=field_name,
            op_id=op_id,
        )
        if not isinstance(value, dict):
            raise _DraftError(
                "invalid_json_object",
                "{} must encode a JSON object".format(field_name),
                op_id,
            )
        return value

    @staticmethod
    def _fingerprint(value: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            thaw_json(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


__all__ = ["DataPlanCompiler"]
