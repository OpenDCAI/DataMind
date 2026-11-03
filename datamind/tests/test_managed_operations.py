import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from datamind.config import Settings, VideoConfig
from datamind.host import CodexHost
from datamind.operations import OperationStore
from datamind import mcp
from datamind.core.diagnostics import diagnostic_scope


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DATAMIND_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('DATAMIND_SESSION_ID', 'test-session')
    monkeypatch.setenv('DATAMIND_ACCESS_MODE', 'write')
    logs = Path(os.environ.get('DATAMIND_TEST_LOG_ROOT', str(tmp_path / 'logs'))) / tmp_path.name
    return Settings(_env_file=None, data={'base_dir': tmp_path},
                    logging={'directory': logs, 'event_trace': True},
                    codex={'managed_operations': True})


async def test_managed_tools_use_bound_context_and_preserve_permissions(config, monkeypatch):
    store = OperationStore(config)
    token = store.begin(session_id='test-session', mode='write')['operation_id']
    monkeypatch.setenv('DATAMIND_OPERATION_ID', token)
    tools = mcp.definitions()
    assert 'datamind_operation_begin' not in tools
    assert 'operation_id' not in tools['datamind_skill_get']['inputSchema']['properties']
    assert (await mcp.execute('datamind_skill_get', {'name': 'workspace-ingest'}))['result']['found']
    with pytest.raises(ValidationError):
        await mcp.execute('datamind_skill_get', {'name': 'workspace-ingest', 'operation_id': 'invented'})
    monkeypatch.setenv('DATAMIND_SESSION_ID', 'other-session')
    with pytest.raises(PermissionError, match='different Codex session'):
        await mcp.execute('datamind_skill_get', {'name': 'workspace-ingest'})
    monkeypatch.setenv('DATAMIND_SESSION_ID', 'test-session')
    monkeypatch.setenv('DATAMIND_ACCESS_MODE', 'read')
    with pytest.raises(PermissionError, match='only permits data reads'):
        await mcp.execute('datamind_skill_get', {'name': 'workspace-ingest'})
    reader = store.begin(session_id='test-session', mode='read')['operation_id']
    monkeypatch.setenv('DATAMIND_OPERATION_ID', reader)
    with pytest.raises(PermissionError, match='reads only'):
        await mcp.execute('datamind_graph_upsert_triples', {'triples': []})


async def test_host_binds_and_closes_operation_on_failure(config):
    operations = []
    class Session:
        def __init__(self, **kwargs):
            env = kwargs['env']
            token = env['DATAMIND_OPERATION_ID']
            ctx, revision, mode = OperationStore(config).load(token)
            assert ctx.session_id == env['DATAMIND_SESSION_ID'] and mode == 'write'
            operations.append(token)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def stream(self, prompt, **kwargs):
            assert 'call tools directly without operation_id' in prompt
            raise RuntimeError('injected transport failure')
            yield
    host = CodexHost(config, session_factory=Session)
    with pytest.raises(RuntimeError, match='injected transport failure'):
        await host.ingest('prepare data')
    with pytest.raises(ValueError, match='Unknown operation'):
        OperationStore(config).load(operations[0])


def test_nested_error_reaches_mcp_and_is_redacted(config, monkeypatch):
    secret = 'test-private-secret'
    monkeypatch.setenv('OPENAI_API_KEY', secret)
    monkeypatch.setattr(mcp, 'load_settings', lambda *args: config)
    async def fail(*args):
        try: raise ValueError('Invalid JSON Hiccup node '+secret)
        except ValueError as cause:
            error = RuntimeError('OCR failed')
            error.__cause__ = cause
            raise ExceptionGroup('frame failures', [error])
    monkeypatch.setattr(mcp, 'execute', fail)
    result = mcp.handle({'id': 1, 'method': 'tools/call', 'params': {'name': 'datamind_video_prepare'}})
    text = json.dumps(result)
    assert 'Invalid JSON Hiccup node' in text and secret not in text
    log = (config.log_dir/'diagnostics.jsonl').read_text()
    assert 'traceback' in log and 'Invalid JSON Hiccup node' in log and secret not in log


async def test_failed_ocr_preserves_response_and_parse_cause(config, tmp_path):
    from datamind.capabilities.ingest.video.providers import describe_frame
    image = tmp_path/'frame.png'; image.write_bytes(b'fake-image-bytes')
    class Client:
        async def complete(self, **kwargs):
            return SimpleNamespace(content=[{'type': 'text', 'text': '["page", 42]'}],
                                   stop_reason='stop', resolved_model='test-vision', usage={})
    with diagnostic_scope(config), pytest.raises(Exception, match='invalid JSON Hiccup'):
        await describe_frame(Client(), image, VideoConfig(vision={'model': 'test-vision'}))
    record = json.loads((config.log_dir/'diagnostics.jsonl').read_text())
    assert record['response_text'] == '["page", 42]'
    assert record['error']['cause']['message'] == 'Invalid JSON Hiccup node'
    assert 'fake-image-bytes' not in json.dumps(record)
