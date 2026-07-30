"""Governed in-memory Skill catalog and executable reference adapter."""
from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import quote

from datamind.dataops import (
    BindingRow,
    BindingSet,
    Evidence,
    InvokeSkill,
    ResolveSkill,
    ResultKind,
)
from datamind.kernel import (
    EffectLevel,
    ExecutionContext,
    KernelValidationError,
    Provenance,
    SkillInvocationResult,
    SkillKind,
    SkillMatch,
    SkillRef,
    SkillResolution,
    SkillSpec,
    SnapshotRef,
    SnapshotUnavailableError,
    SourceDescriptor,
    SourceExecutionError,
    SourceKind,
    SourceRef,
    freeze_json,
    json_object_violations,
    sha256_checksum,
    thaw_json,
)
from datamind.ports import SourceResult

_TOKEN_PATTERN = re.compile(r"[\w-]+", flags=re.UNICODE)
SkillHandler = Callable[[Mapping[str, Any], ExecutionContext], Any]


@dataclass(frozen=True)
class SkillRegistration:
    """A trusted SkillSpec and its optional local execution entry."""

    spec: SkillSpec
    handler: Optional[SkillHandler] = None

    def __post_init__(self) -> None:
        if not isinstance(self.spec, SkillSpec):
            raise KernelValidationError(
                "skill registration spec must be a SkillSpec"
            )
        if self.spec.is_executable and not callable(self.handler):
            raise KernelValidationError(
                "executable skills require a callable handler"
            )
        if (
            self.spec.kind is SkillKind.INSTRUCTION
            and self.handler is not None
        ):
            raise KernelValidationError(
                "instruction-only skills cannot register a handler"
            )


class InMemorySkillSource:
    """Resolve Skill data and invoke only registry-governed entries."""

    def __init__(
        self,
        *,
        source_id: str,
        registrations: Iterable[SkillRegistration],
        display_name: str = "In-memory governed Skills",
    ) -> None:
        values = tuple(registrations)
        if any(not isinstance(item, SkillRegistration) for item in values):
            raise KernelValidationError(
                "skill source requires SkillRegistration values"
            )
        refs = tuple(item.spec.ref for item in values)
        if len(set(refs)) != len(refs):
            raise KernelValidationError(
                "skill source registrations must have unique SkillRefs"
            )
        names_and_versions = tuple(
            (item.spec.name, item.spec.version) for item in values
        )
        if len(set(names_and_versions)) != len(names_and_versions):
            raise KernelValidationError(
                "skill name and version pairs must be unique"
            )
        self._registrations: Dict[SkillRef, SkillRegistration] = {
            item.spec.ref: item for item in values
        }
        ordered = tuple(
            sorted(
                values,
                key=lambda item: (
                    item.spec.name,
                    item.spec.version,
                    item.spec.digest,
                ),
            )
        )
        self._ordered = ordered
        ref = SourceRef(source_id, SourceKind.SKILL)
        checksum = sha256_checksum(
            json.dumps(
                [
                    {
                        "name": item.spec.name,
                        "version": item.spec.version,
                        "digest": item.spec.digest,
                    }
                    for item in ordered
                ],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        version = "sha256:{}".format(checksum)
        self._snapshot = SnapshotRef(
            source=ref,
            version=version,
            checksum=checksum,
        )
        max_effect = max(
            (item.spec.effect_level for item in ordered),
            default=EffectLevel.READ,
        )
        self._descriptor = SourceDescriptor(
            ref=ref,
            display_name=display_name,
            capabilities=frozenset(("resolve_skill", "invoke_skill")),
            max_effect=max(max_effect, EffectLevel.READ),
            version=version,
            schema={
                "skill_identity": {
                    "name": "string",
                    "version": "string",
                    "digest": "sha256",
                },
                "governed_skills": [
                    {
                        "name": item.spec.name,
                        "version": item.spec.version,
                        "digest": item.spec.digest,
                        "kind": item.spec.kind.value,
                        "effect": item.spec.effect_level.name,
                        "description": item.spec.description,
                        "input_schema": thaw_json(
                            item.spec.input_schema
                        ),
                        "output_schema": thaw_json(
                            item.spec.output_schema
                        ),
                        "reversible": item.spec.reversible,
                        "requires_approval": (
                            item.spec.requires_approval
                        ),
                    }
                    for item in ordered
                ],
            },
            metadata={
                "adapter": "in_memory_governed_skill",
                "effect_authority": "trusted_registration",
            },
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        return self._descriptor

    async def current_snapshot(self) -> SnapshotRef:
        return self._snapshot

    async def has_snapshot(self, snapshot: SnapshotRef) -> bool:
        return (
            isinstance(snapshot, SnapshotRef)
            and self._snapshot.same_version_as(snapshot)
        )

    async def execute(
        self,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        pinned = context.snapshots.get(self.descriptor.ref)
        if pinned is not None and not await self.has_snapshot(pinned):
            raise SnapshotUnavailableError(
                "skill source {!r} cannot serve snapshot {!r}".format(
                    self.descriptor.ref.source_id,
                    pinned.version,
                )
            )
        if isinstance(operation, ResolveSkill):
            return self._resolve(operation)
        if isinstance(operation, InvokeSkill):
            return await self._invoke(operation, context=context)
        raise SourceExecutionError(
            "skill source supports ResolveSkill and InvokeSkill"
        )

    def _resolve(self, operation: ResolveSkill) -> SourceResult[Any]:
        query_tokens = self._tokens(operation.query)
        matches = []
        for registration in self._ordered:
            spec = registration.spec
            searchable = "{} {}".format(spec.name, spec.description)
            candidate_tokens = self._tokens(searchable)
            overlap = len(query_tokens.intersection(candidate_tokens))
            exact = operation.query.casefold().strip() == spec.name.casefold()
            substring = (
                operation.query.casefold().strip()
                in searchable.casefold()
            )
            if not exact and not substring and overlap == 0:
                continue
            score = (
                2.0
                if exact
                else (1.0 if substring else 0.0)
                + overlap / max(1, len(query_tokens))
            )
            matches.append(SkillMatch(spec=spec, score=score))
        matches.sort(
            key=lambda item: (
                -item.score,
                item.spec.name,
                item.spec.version,
                item.spec.digest,
            )
        )
        selected = tuple(matches[: operation.limit])
        evidence = []
        provenance = []
        binding_rows = []
        for match in selected:
            spec = match.spec
            origin = Provenance(
                source=self.descriptor.ref,
                locator="skill://{}/{}@{}#{}".format(
                    quote(self.descriptor.ref.source_id, safe=""),
                    quote(spec.name, safe=""),
                    quote(spec.version, safe=""),
                    spec.digest,
                ),
                snapshot=self._snapshot,
            )
            evidence_item = Evidence(
                kind=SourceKind.SKILL,
                content=spec.instructions,
                provenance=origin,
                score=match.score,
                metadata={
                    "name": spec.name,
                    "version": spec.version,
                    "digest": spec.digest,
                    "kind": spec.kind.value,
                    "effect": spec.effect_level.name,
                },
            )
            evidence.append(evidence_item)
            provenance.append(origin)
            binding_rows.append(
                BindingRow(
                    values={
                        "name": spec.name,
                        "version": spec.version,
                        "digest": spec.digest,
                        "kind": spec.kind.value,
                        "score": match.score,
                        "effect": spec.effect_level.name,
                        "reversible": spec.reversible,
                        "requires_approval": spec.requires_approval,
                        "compatibility": spec.compatibility,
                    },
                    evidence_ids=(evidence_item.evidence_id,),
                )
            )
        return SourceResult(
            value=SkillResolution(
                query=operation.query,
                matches=selected,
            ),
            result_kind=ResultKind.SKILL_SPECS,
            evidence=tuple(evidence),
            bindings=BindingSet(
                fields=(
                    "name",
                    "version",
                    "digest",
                    "kind",
                    "score",
                    "effect",
                    "reversible",
                    "requires_approval",
                    "compatibility",
                ),
                rows=tuple(binding_rows),
            ),
            provenance=tuple(provenance),
            snapshots=(self._snapshot,),
        )

    async def _invoke(
        self,
        operation: InvokeSkill,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        if operation.argument_bindings:
            raise SourceExecutionError(
                "Skill bindings must be resolved before adapter execution"
            )
        registration = self._registrations.get(operation.skill)
        if registration is None:
            raise SourceExecutionError(
                "unknown or stale SkillRef {!r}@{!r}".format(
                    operation.skill.name,
                    operation.skill.version,
                )
            )
        spec = registration.spec
        if not spec.is_executable or registration.handler is None:
            raise SourceExecutionError(
                "instruction-only Skill {!r} cannot be invoked".format(
                    spec.name
                )
            )
        if (
            operation.governed_effect is not spec.effect_level
            or operation.reversible is not spec.reversible
            or operation.requires_approval is not spec.requires_approval
        ):
            raise SourceExecutionError(
                "InvokeSkill policy does not match trusted registration"
            )
        arguments = thaw_json(operation.arguments)
        input_violations = json_object_violations(
            arguments,
            spec.input_schema,
            label="Skill input",
        )
        if input_violations:
            raise SourceExecutionError("; ".join(input_violations))
        try:
            output = registration.handler(arguments, context)
            if inspect.isawaitable(output):
                output = await output
        except Exception as exc:
            raise SourceExecutionError(
                "Skill {!r} execution failed: {}".format(
                    spec.name,
                    exc,
                )
            ) from exc
        frozen_output = freeze_json(output)
        plain_output = thaw_json(frozen_output)
        output_violations = json_object_violations(
            plain_output,
            spec.output_schema,
            label="Skill output",
        )
        if output_violations:
            raise SourceExecutionError("; ".join(output_violations))
        result = SkillInvocationResult(
            skill=spec.ref,
            output=frozen_output,
            effect_level=spec.effect_level,
            idempotency_key=operation.idempotency_key,
        )
        origin = Provenance(
            source=self.descriptor.ref,
            locator="skill://{}/{}@{}/invocations/{}".format(
                quote(self.descriptor.ref.source_id, safe=""),
                quote(spec.name, safe=""),
                quote(spec.version, safe=""),
                quote(operation.op_id, safe=""),
            ),
            snapshot=self._snapshot,
        )
        encoded = json.dumps(
            plain_output,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        evidence_item = Evidence(
            kind=SourceKind.SKILL,
            content=encoded,
            provenance=origin,
            metadata={
                "name": spec.name,
                "version": spec.version,
                "digest": spec.digest,
                "effect": spec.effect_level.name,
            },
        )
        schema_properties = spec.output_schema.get("properties", {})
        output_fields = tuple(
            "output.{}".format(name)
            for name in sorted(schema_properties)
        )
        return SourceResult(
            value=result,
            result_kind=ResultKind.SKILL_RESULT,
            evidence=(evidence_item,),
            bindings=BindingSet(
                fields=(
                    "skill_name",
                    "skill_version",
                    "skill_digest",
                ) + output_fields,
                rows=(
                    BindingRow(
                        values={
                            "skill_name": spec.name,
                            "skill_version": spec.version,
                            "skill_digest": spec.digest,
                            **{
                                "output.{}".format(name): plain_output.get(
                                    name
                                )
                                for name in sorted(schema_properties)
                            },
                        },
                        evidence_ids=(evidence_item.evidence_id,),
                    ),
                ),
            ),
            provenance=(origin,),
            snapshots=(self._snapshot,),
        )

    @staticmethod
    def _tokens(value: str) -> set:
        return {
            item.casefold()
            for item in _TOKEN_PATTERN.findall(value)
            if item.strip()
        }

__all__ = [
    "InMemorySkillSource",
    "SkillHandler",
    "SkillRegistration",
]
