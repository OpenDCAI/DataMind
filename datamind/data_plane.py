"""Direct, policy-checked data tools. Codex owns planning and interpretation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from datamind.core.context import RequestContext
from datamind.core.contracts import ToolAccess
from datamind.core.hooks import AskUser, Deny, Rewrite
from datamind.core.logging import bind_context, current_context
from datamind.core.tools import ToolRegistry

# These legacy tools perform conversational generation internally. Codex instead
# writes SQL/triples, and publishes externally prepared graph assertions.
MODEL_TOOLS = frozenset({
    'db_query_nl', 'db_query_nl_at_version', 'graph_add_path',
    'graph_add_triples_from_text', 'workspace_update',
})


@dataclass
class DataPlane:
    services: Any
    tools: ToolRegistry
    hooks: Any
    ledger: Any
    profile: str
    _closed: bool = False

    @property
    def read_tools(self):
        return self.tools.select(access={ToolAccess.READ, ToolAccess.UTILITY})

    async def call(self, name: str, arguments: dict[str, Any] | None = None, *,
                   context: RequestContext | None = None, read_only: bool = False):
        spec = self.tools.get(name)
        if read_only and spec.access == ToolAccess.WRITE:
            raise PermissionError('Published workspace revisions are read-only')
        ctx = context or current_context() or RequestContext.new(profile=self.profile)
        if ctx.profile != self.profile:
            raise PermissionError('Request profile does not match runtime')
        args = dict(arguments or {})
        result, error = None, None
        with bind_context(ctx):
            try:
                schema = {**spec.input_schema, 'additionalProperties': False}
                Draft202012Validator(schema).validate(args)
                if self.hooks:
                    decision = await self.hooks.pre(name, args)
                    if isinstance(decision, Deny):
                        raise PermissionError(decision.reason)
                    if isinstance(decision, AskUser):
                        result = {'requires_confirmation': True, 'prompt': decision.prompt,
                                  'details': decision.details, 'confirm_args': decision.confirm_args}
                        return result
                    if isinstance(decision, Rewrite):
                        args = dict(decision.new_args)
                        Draft202012Validator(schema).validate(args)
                result = await spec.handler(**args)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                if self.hooks:
                    await self.hooks.post(name, args, result, error)

    async def aclose(self):
        # One lifecycle implementation shared with the compatibility facade.
        from datamind.agent.options import DataMind
        await DataMind.aclose(self)


async def build_data_plane(settings, *, enable: set[str] | None = None) -> DataPlane:
    from datamind.agent.options import _assemble
    return await _assemble(settings, enable=enable, agent_runtime=False)
