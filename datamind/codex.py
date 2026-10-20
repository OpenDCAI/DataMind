"""Optional official Codex SDK host. DataMind does not run an inner agent."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from datamind.config import DataConfig
from datamind.mcp import INSTRUCTIONS, definitions


def sdk_config(*, workspace: str, data_root: str, profile: str = 'default',
               python: str = sys.executable, preapproved_tools: tuple[str, ...] = (),
               config_overrides: tuple[str, ...] = (), env: dict[str, str] | None = None):
    """One profile-bound MCP subprocess per SDK client; no model override."""
    DataConfig(profile=profile)  # validate before serializing trusted host config
    cwd = str(Path(workspace).expanduser().resolve())
    unknown = set(preapproved_tools) - definitions().keys()
    if unknown:
        raise ValueError('Unknown DataMind tools in preapproval list: ' + ', '.join(sorted(unknown)))
    approved = tuple(f'mcp_servers.datamind.tools.{name}.approval_mode="approve"' for name in preapproved_tools)
    result = {
        'cwd': cwd,
        'config_overrides': tuple(f'{key}={json.dumps(value, ensure_ascii=False)}' for key, value in {
            'mcp_servers.datamind.command': python,
            'mcp_servers.datamind.args': ['-m', 'datamind.mcp'],
            'mcp_servers.datamind.cwd': cwd,
            'mcp_servers.datamind.env.DATAMIND_DATA_ROOT': str(Path(data_root).expanduser().resolve()),
            'mcp_servers.datamind.env.DATAMIND_PROFILE': profile,
        }.items()) + tuple(config_overrides) + approved,
    }
    if env is not None:
        result['env'] = dict(env)
    return result


class CodexSession:
    """A Codex thread connected to the same tools/Skills as the desktop plugin.

    Authentication, authorization, cancellation, scheduling and resource budgets
    belong to the enterprise host. A profile string supplied by an end user is
    not a tenant authorization check. Do not share one session across users.
    """

    def __init__(self, *, workspace: str, data_root: str, profile='default', model=None,
                 approval_mode='auto_review', preapproved_tools: tuple[str, ...] = (),
                 config_overrides: tuple[str, ...] = (), env: dict[str, str] | None = None):
        if approval_mode not in {'auto_review', 'deny_all'}:
            raise ValueError('approval_mode must be auto_review or deny_all')
        self.approval_mode = approval_mode
        self.config = sdk_config(workspace=workspace, data_root=data_root, profile=profile,
            preapproved_tools=preapproved_tools, config_overrides=config_overrides, env=env)
        self.model = model
        self.client = None
        self.thread = None

    async def __aenter__(self):
        try:
            from openai_codex import AsyncCodex, CodexConfig, ApprovalMode
        except ImportError as exc:
            raise ImportError("Install DataMind's official Codex SDK integration: pip install 'datamind[codex]'") from exc
        self.client = AsyncCodex(CodexConfig(**self.config))
        await self.client.__aenter__()
        try:
            kwargs = {'cwd': self.config['cwd'], 'developer_instructions': INSTRUCTIONS,
                      'approval_mode': ApprovalMode(self.approval_mode)}
            if self.model is not None:
                kwargs['model'] = self.model
            self.thread = await self.client.thread_start(**kwargs)
        except BaseException:
            await self.client.__aexit__(*sys.exc_info())
            raise
        return self

    async def run(self, prompt: str):
        if self.thread is None:
            raise RuntimeError('Use async with CodexSession(...) before run')
        return await self.thread.run(prompt)

    async def __aexit__(self, *exc_info):
        try:
            if self.client:
                await self.client.__aexit__(*exc_info)
        finally:
            self.thread = None
