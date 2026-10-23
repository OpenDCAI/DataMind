from __future__ import annotations

from types import SimpleNamespace
import tomllib
import json
from pathlib import Path
import datamind

import pytest

from datamind.codex import CodexSession, sdk_config


def test_config_is_valid_toml_and_profile_bound(tmp_path):
    cfg = sdk_config(workspace=str(tmp_path), data_root=str(tmp_path / '数据 📊 "root'), profile='team-a')
    doc = tomllib.loads('\n'.join(cfg['config_overrides']))
    server = doc['mcp_servers']['datamind']
    assert server['args'] == ['-m', 'datamind.mcp']
    assert server['env']['DATAMIND_PROFILE'] == 'team-a'
    assert 'model' not in doc
    with pytest.raises(ValueError):
        sdk_config(workspace='.',data_root='.',profile='../other')


@pytest.mark.asyncio
async def test_sdk_drives_one_main_agent_and_cleans_up(monkeypatch, tmp_path):
    sdk = pytest.importorskip('openai_codex')
    events = []
    class Thread:
        async def run(self, prompt):
            events.append(prompt)
            return SimpleNamespace(final_response='Codex answer')
    class Client:
        def __init__(self, config):
            events.append(config)
        async def __aenter__(self):
            return self
        async def thread_start(self, **kwargs):
            assert 'model' not in kwargs
            assert 'main agent' in kwargs['developer_instructions']
            return Thread()
        async def __aexit__(self,*args):
            events.append('closed')
    monkeypatch.setattr(sdk,'AsyncCodex',Client)
    async with CodexSession(workspace=str(tmp_path),data_root=str(tmp_path)) as session:
        result = await session.run('ingest and retrieve')
        assert result.final_response == 'Codex answer'
    assert events[-2:] == ['ingest and retrieve','closed']
    with pytest.raises(RuntimeError):
        await session.run('after close')


@pytest.mark.asyncio
async def test_real_official_sdk_transport_initializes_without_model_turn(tmp_path):
    sdk = pytest.importorskip('openai_codex')
    # Starts the bundled CLI app-server and performs its real JSON-RPC handshake.
    # No thread.run / turn.start, no model traffic or billable inference.
    # Pin the child to the same package as this test, including isolated image
    # runs where an older DataMind wheel may also be installed.
    module_root = str(Path(datamind.__file__).resolve().parents[1])
    config = sdk.CodexConfig(**sdk_config(workspace=str(tmp_path),data_root=str(tmp_path),
        config_overrides=(f'mcp_servers.datamind.env.PYTHONPATH={json.dumps(module_root)}',)))
    from openai_codex.async_client import AsyncCodexClient
    from openai_codex.generated.v2_all import ListMcpServerStatusResponse
    async with AsyncCodexClient(config) as client:
        metadata = await client.initialize()
        assert metadata.userAgent
        status = await client.request('mcpServerStatus/list', {'serverName': 'datamind'},
                                      response_model=ListMcpServerStatusResponse)
        server = next(s for s in status.data if s.name == 'datamind')
        assert server.tools_error is None
        assert 'datamind_operation_begin' in server.tools
        assert 'datamind_workspace_catalog' in server.tools
        assert 'datamind_workspace_read_asset' in server.tools
        assert 'datamind_workspace_export_bundle' in server.tools
        assert 'datamind_graph_neighbors' in server.tools
        assert 'datamind_ask' not in server.tools


def test_preapproval_is_explicit_and_limited_to_known_tools(tmp_path):
    cfg = sdk_config(workspace=str(tmp_path),data_root=str(tmp_path),
        preapproved_tools=('datamind_operation_begin',),
        config_overrides=('model_provider="gateway"',),env={'DEPLOYMENT_TEST':'value'})
    settings = tomllib.loads('\n'.join(cfg['config_overrides']))
    assert settings['mcp_servers']['datamind']['tools'] == {
        'datamind_operation_begin': {'approval_mode':'approve'}}
    assert cfg['env'] == {'DEPLOYMENT_TEST':'value'}
    assert settings['model_provider'] == 'gateway'
    default = tomllib.loads('\n'.join(sdk_config(workspace=str(tmp_path),data_root=str(tmp_path))['config_overrides']))
    assert 'tools' not in default['mcp_servers']['datamind']
    with pytest.raises(ValueError,match='Unknown DataMind tools'):
        sdk_config(workspace=str(tmp_path),data_root=str(tmp_path),preapproved_tools=('unknown',))
    with pytest.raises(ValueError,match='approval_mode'):
        CodexSession(workspace=str(tmp_path),data_root=str(tmp_path),approval_mode='allow_everything')


def test_host_context_forwarded_to_mcp_without_forwarding_gateway_credentials(tmp_path):
    cfg=sdk_config(workspace=str(tmp_path),data_root=str(tmp_path),env={
        'DATAMIND_ACCESS_MODE':'read','DATAMIND_REQUEST_ID':'request-a',
        'DATAMIND_EXPORT_ROOT':'/shared/evidence',
        'DATAMIND__EMBEDDING__API_KEY':'embed-secret','DATAMIND_CODEX_GATEWAY_KEY':'model-secret'})
    options=tomllib.loads('\n'.join(cfg['config_overrides']))
    assert set(options['mcp_servers']['datamind']['env_vars'])=={
        'DATAMIND_ACCESS_MODE','DATAMIND_REQUEST_ID','DATAMIND__EMBEDDING__API_KEY','DATAMIND_EXPORT_ROOT'}
    assert 'embed-secret' not in str(cfg['config_overrides'])
    assert 'model-secret' not in str(cfg['config_overrides'])
