"""The published tool contract must describe the arguments the workspace accepts."""
import json
import sqlite3

import pytest
from jsonschema import Draft202012Validator, ValidationError

from datamind.config import Settings
from datamind.direct_tools import workspace_tools
from datamind import mcp


@pytest.mark.parametrize('policy', ['standard', 'dataspace'])
async def test_inventory_separates_tables_from_model_graph_sources(tmp_path, policy):
    source = tmp_path / 'inputs'; source.mkdir()
    (source / 'rows.csv').write_text('id,name\n1,example\n')
    (source / 'notes.md').write_text('A document.')
    (source / 'clip.mp4').write_bytes(b'not decoded during inventory')
    (source / 'rows.json').write_text(json.dumps({'table': 'rows', 'records': [{'id': 1}]}))
    with sqlite3.connect(source / 'native.sqlite') as db:
        db.execute('CREATE TABLE example (id INTEGER)')
    cfg = Settings(_env_file=None, data={'base_dir': tmp_path / 'runtime'},
                   workspace={'input_policy': policy},
                   hooks={'path_allowlist_extra': [str(source)]})
    tool = next(t for t in workspace_tools(cfg) if t.name == 'workspace_prepare')
    result = await tool.handler(path=str(source))
    required = {s['relative_path']: s['graph_assertions_required'] for s in result['sources']}
    assert required == {'rows.csv': False, 'native.sqlite': False, 'notes.md': True,
                        'clip.mp4': True, 'rows.json': policy != 'dataspace'}
    assert all(s['version'].startswith('sha256:') for s in result['sources'])
    # CSV remains on its existing table path; it is not reclassified as native SQLite/JSON.
    assert not next(s for s in result['sources'] if s['relative_path'] == 'rows.csv').get('structured_table')


@pytest.mark.parametrize('managed', [False, True])
def test_mcp_schema_requires_relation_and_resolves_nested_evidence(managed, monkeypatch):
    if managed:
        monkeypatch.setenv('DATAMIND_OPERATION_ID', 'test-bound-operation')
    else:
        monkeypatch.delenv('DATAMIND_OPERATION_ID', raising=False)
    schema = mcp.definitions()['datamind_workspace_publish']['inputSchema']
    Draft202012Validator.check_schema(schema)
    args = {'path': '/inputs', 'graph_sources': [{'uri': '/inputs/notes.md',
            'version': 'sha256:' + 'a' * 64,
            'triples': [{'subject': 'A', 'relation': 'uses', 'object': 'B',
                         'properties': {'video_bundle': 'b' * 64,
                                        'video_evidence': [{'frame_id': 'frame-0000'}]},
                         'evidence': [{'source_id': 'source', 'source_version': 'v1',
                                       'locator': {'uri': '/inputs/notes.md'},
                                       'method': 'extracted', 'pipeline_version': 'v1'}]}]}]}
    if not managed: args['operation_id'] = 'test-operation'
    validator = Draft202012Validator(schema)
    validator.validate(args)
    triple = args['graph_sources'][0]['triples'][0]
    triple['predicate'] = triple.pop('relation')
    with pytest.raises(ValidationError, match='relation'):
        validator.validate(args)
