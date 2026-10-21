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
class DataServices:
    client: Any = None
    fallback_client: Any = None
    embedding: Any = None
    kb: Any = None
    db: Any = None
    graph: Any = None
    skills: Any = None
    memory: Any = None
    ingest: Any = None


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
        # Python callers commonly pass None for optional omitted arguments.
        # MCP validates its wire schema before dispatch; unknown keys still fail.
        required = set(spec.input_schema.get('required', []))
        optional = set(spec.input_schema.get('properties', {})) - required
        args = {k:v for k,v in args.items() if v is not None or k not in optional}
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

    @property
    def revision(self):
        return self.ledger.revision

    async def warmup(self):
        return {
            'skills': await self.services.skills.load() if self.services.skills else {'manifests': 0},
            'graph': await self.services.graph.load_from_profile() if self.services.graph else {},
            'kb_chunks': await self.services.kb.count() if self.services.kb else 0,
            'revision': self.revision,
            'hooks': self.hooks.names() if self.hooks else [],
        }

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        seen = set()
        for resource in (self.services.db, self.services.graph, self.services.kb,
                         self.services.embedding, self.services.fallback_client, self.services.client):
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, 'aclose', None)
            if callable(close):
                await close()


async def build_data_plane(settings, *, enable: set[str] | None = None, text_client=None) -> DataPlane:
    from datamind.runtime import assemble_data_plane
    return await assemble_data_plane(settings, enable=enable, text_client=text_client)
