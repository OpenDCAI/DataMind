"""External log routing preserves workspace isolation and existing audit chains."""
from __future__ import annotations

import shutil

import pytest

from datamind.config import Settings
from datamind.core.context import RequestContext
from datamind.capabilities.hooks.audit import AuditLogHook, verify_audit_log


def test_external_log_scope_and_host_forwarding(tmp_path):
    from datamind.host import CodexHost
    cfg = Settings(_env_file=None, data={'base_dir': tmp_path / 'workspace'}, logging={'directory': None})
    assert cfg.log_dir == cfg.data.storage_dir
    cfg.logging.directory = tmp_path / 'logs'
    assert cfg.log_dir.is_relative_to(tmp_path / 'logs')
    other = cfg.model_copy(deep=True)
    other.data.base_dir = tmp_path / 'another_workspace'
    assert other.log_dir != cfg.log_dir
    other.data.base_dir = cfg.data.base_dir
    other.data.profile = 'other'
    assert other.log_dir != cfg.log_dir
    host = CodexHost(cfg, session_factory=lambda **kw: kw)
    env = host._session('session', 'request', 'read')['env']
    assert env['DATAMIND__LOGGING__DIRECTORY'] == str(tmp_path / 'logs')


@pytest.mark.asyncio
async def test_runtime_appends_to_relocated_audit_chain(tmp_path):
    from datamind.runtime import _build_hook_chain
    cfg = Settings(_env_file=None, data={'base_dir': tmp_path / 'workspace'},
                   hooks={'path_allowlist': False, 'destructive_sql': False})
    old = cfg.data.storage_dir / 'audit.jsonl'
    ctx = RequestContext.new(profile=cfg.data.profile)
    await AuditLogHook(old).post_tool_use(ctx, 'db_list_tables', {}, result={}, error=None)
    first = old.read_bytes()
    cfg.logging.directory = tmp_path / 'logs'
    new = cfg.log_dir / 'audit.jsonl'
    new.parent.mkdir(parents=True)
    shutil.move(old, new)
    chain = _build_hook_chain(cfg)
    await chain.post('db_list_tables', {}, result={}, error=None)
    assert new.read_bytes().startswith(first)
    assert verify_audit_log(new) == (True, None, 2)
    assert not old.exists()
