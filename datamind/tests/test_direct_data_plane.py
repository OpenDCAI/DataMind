"""Contracts for direct Codex tool execution, without nested model turns."""
from __future__ import annotations

import asyncio
import json
import pytest
from jsonschema import ValidationError

from datamind.config import DataSettings
from datamind.core.context import RequestContext
from datamind.core.hooks import Deny, HookChain
from datamind.data_plane import build_data_plane
from datamind.mcp import execute


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('DATAMIND__LLM__API_KEY', raising=False)
    monkeypatch.setenv('DATAMIND_DATA_ROOT', str(tmp_path))
    monkeypatch.delenv('DATAMIND_PROFILE', raising=False)
    from datamind import runtime as options
    def forbidden(*a, **kw):
        pytest.fail('Direct tools must never construct an agent or conversation client')
    from datamind.core import model_clients
    monkeypatch.setattr(model_clients, 'build_model_client', forbidden)
    cfg = DataSettings(_env_file=None)
    cfg.data.base_dir = tmp_path
    return cfg


async def begin(mode='write', **kwargs):
    result = await execute('datamind_operation_begin', {'mode': mode, **kwargs})
    return result['operation_id']


async def call(token, tool_name, **kwargs):
    return await execute('datamind_' + tool_name, {'operation_id': token, **kwargs})


@pytest.mark.asyncio
async def test_direct_graph_sql_skills_memory_no_model(settings):
    token = await begin()
    skill = await call(token, 'skill_get', name='workspace-ingest')
    assert skill['result']['found']
    response = await call(token, 'graph_upsert_triples', triples=[{
        'subject': 'Policy', 'relation': 'governs', 'object': 'Procedure',
        'subject_type': 'policy', 'object_type': 'procedure'}])
    assert response['result']['applied_skills'][0]['name'] == 'workspace-ingest'
    schema = await call(token, 'graph_schema')
    assert schema['result']['relations'] == {'governs': 1}
    edges = await call(token, 'graph_neighbors', entity='Policy')
    assert edges['result']['count'] == 1
    await call(token, 'db_import_records', table='sales', records=[{'amount': 42}])
    rows = await call(token, 'db_query_sql', sql='SELECT amount FROM sales')
    assert str(rows['result']['rows'][0][0]) == '42'
    await call(token, 'memory_save', content='Reports are in Chinese')
    recalled = await call(token, 'memory_recall', query='Chinese')
    assert 'Chinese' in json.dumps(recalled)
    audit = settings.data.storage_dir / 'audit.jsonl'
    assert 'graph_upsert_triples' in audit.read_text()


@pytest.mark.asyncio
async def test_build_metadata_remains_usable_without_business_write_receipts(settings,tmp_path):
    source=tmp_path/'sources';source.mkdir()
    (source/'notes.md').write_text('A source document')
    writer=await begin()
    started=(await call(writer,'build_start',path=str(source)))['result']
    assert 'receipt_id' not in started
    build_id=started['build_id']
    frozen=(await call(writer,'build_freeze',build_id=build_id))['result']
    assert frozen['build_id']==build_id
    reader=await begin('read',pin_workspace=False)
    with pytest.raises(PermissionError):
        await call(reader,'build_start',path=str(source))


@pytest.mark.asyncio
async def test_operation_isolation_and_validation(settings):
    a, b = await begin(), await begin()
    await call(a, 'skill_get', name='workspace-ingest')
    result = await call(b, 'graph_upsert_triples', triples=[{'subject':'a','relation':'r','object':'b'}])
    assert result['result']['applied_skills'] == []
    reader = await begin('read', pin_workspace=False)
    with pytest.raises(PermissionError):
        await call(reader, 'graph_upsert_triples', triples=[])
    with pytest.raises(ValidationError):
        await call(reader, 'graph_neighbors', entity='a', operation_override='hack')
    with pytest.raises(ValueError, match='Unknown operation'):
        await call('unknown', 'graph_schema')
    with pytest.raises(ValueError):
        await begin('write', pin_workspace=True)


@pytest.mark.asyncio
async def test_direct_hooks_and_paths(settings):
    runtime = await build_data_plane(settings, enable={'graph'})
    try:
        with pytest.raises(ValidationError):
            await runtime.call('graph_neighbors', {'entity':'x', 'limit':-1})
        with pytest.raises(PermissionError):
            await runtime.call('graph_schema', context=RequestContext.new(profile='other'))
        class Block:
            async def pre_tool_use(self, *args):
                return Deny('policy denied')
            async def post_tool_use(self, *args):
                pass
        runtime.hooks = HookChain([Block()])
        with pytest.raises(PermissionError, match='policy denied'):
            await runtime.call('graph_upsert_triples', {'triples':[]})
        assert runtime.services.graph.stats()['edges'] == 0
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_publish_pin_update_delete_and_invalid_retains_head(settings, tmp_path):
    path = tmp_path / 'source'
    path.mkdir()
    doc = path / 'policy.md'
    doc.write_text('Policy governs Procedure')
    writer = await begin()
    await call(writer, 'skill_get', name='workspace-ingest')
    inventory = (await call(writer, 'workspace_prepare', path=str(path)))['result']
    entry = inventory['sources'][0]
    sources = [{**{k:entry[k] for k in ('uri','version')}, 'triples':[
        {'subject':'Policy','relation':'governs','object':'Procedure'}]}]
    first = await call(writer, 'workspace_publish', path=str(path), surfaces=['graph'], graph_sources=sources)
    assert first['result']['results'][0]['status'] != 'failed'
    first_revision = first['result']['results'][0]['details']['workspace_revision']
    reader = await begin('read')
    before = await call(reader, 'graph_neighbors', entity='Policy')
    assert before['workspace_revision'] == first_revision
    assert before['result']['count'] == 1
    await asyncio.sleep(0)
    doc.write_text('No relationship remains')
    stale = await call(writer, 'workspace_publish', path=str(path), surfaces=['graph'], graph_sources=sources)
    assert stale['result']['results'][0]['status'] == 'failed'
    from datamind.workspace import Workspace
    assert Workspace(settings).current_revision == first_revision
    entry = (await call(writer, 'workspace_prepare', path=str(path)))['result']['sources'][0]
    second = await call(writer, 'workspace_publish', path=str(path), surfaces=['graph'],
                        graph_sources=[{'uri':entry['uri'],'version':entry['version'],'triples':[]}])
    assert second['result']['results'][0]['status'] != 'failed'
    assert (await call(reader, 'graph_neighbors', entity='Policy'))['result']['count'] == 1
    latest = await begin('read')
    assert (await call(latest, 'graph_neighbors', entity='Policy'))['result']['count'] == 0
    assert 'No relationship' not in json.dumps(await call(reader, 'raw_file_read', path=str(doc)))
    with pytest.raises(PermissionError):
        await call(reader, 'graph_upsert_triples', triples=[])
    doc.unlink()
    deleted = await call(writer, 'workspace_publish', path=str(path), surfaces=['graph'], graph_sources=[])
    assert deleted['result']['results'][0]['details']['sources'] == []


@pytest.mark.asyncio
async def test_replace_source_preserves_other_support_and_validates_before_retract(settings,tmp_path):
    import hashlib
    a,b = tmp_path/'a.md',tmp_path/'b.md'
    a.write_text('a says A knows B'); b.write_text('b says A knows B')
    token = await begin()
    triple = {'subject':'A','relation':'knows','object':'B'}
    for path in (a,b):
        receipt = await call(token,'graph_replace_source',path=str(path),
            expected_version='sha256:'+hashlib.sha256(path.read_bytes()).hexdigest(),triples=[triple])
        assert receipt['result']['results'][0]['status'] == 'stored'
    assert (await call(token,'graph_neighbors',entity='A'))['result']['count'] == 2
    bad = await call(token,'graph_replace_source',path=str(a),expected_version='sha256:'+'0'*64,triples=[])
    assert bad['result']['results'][0]['status'] == 'failed'
    assert (await call(token,'graph_neighbors',entity='A'))['result']['count'] == 2
    await call(token,'graph_forget_source',path=str(a))
    assert a.exists()
    assert (await call(token,'graph_neighbors',entity='A'))['result']['count'] == 1
    await call(token,'graph_forget_source',path=str(b))
    assert (await call(token,'graph_neighbors',entity='A'))['result']['count'] == 0


@pytest.mark.asyncio
async def test_direct_publication_pins_kb_sql_graph_and_raw_across_calls(settings,tmp_path,monkeypatch):
    from datamind import runtime as options
    from datamind.workspace import Workspace
    class Embed:
        name = 'test'
        dimension = 4
        async def embed_texts(self, texts):
            return [[1.,0.,0.,0.] for _ in texts]
        async def embed_query(self, text):
            return [1.,0.,0.,0.]
    monkeypatch.setattr(options,'build_embedding',lambda *a,**kw: Embed())
    root = tmp_path / 'documents'; root.mkdir()
    doc, table = root/'owner.md',root/'data.csv'
    doc.write_text('OldOwner owns Project')
    table.write_text('amount\n10\n')
    writer = await begin()
    await call(writer,'skill_get',name='workspace-ingest')
    async def publish(owner):
        inventory = (await call(writer,'workspace_prepare',path=str(root)))['result']
        source = next(s for s in inventory['sources'] if s['uri'].endswith('owner.md'))
        result = await call(writer,'workspace_publish',path=str(root),surfaces=['kb','db','graph'],
            graph_sources=[{'uri':source['uri'],'version':source['version'],'triples':[
                {'subject':owner,'relation':'owns','object':'Project'}]}])
        assert all(r['status'] == 'stored' for r in result['result']['results']), result
        manifest = result['result']['results'][0]['details']
        assert manifest['metrics']['graph_extraction_calls'] == 0
        assert manifest['applied_skills'][0]['version'] == '1.2.0'
        return manifest['workspace_revision']
    first = await publish('OldOwner')
    reader = await begin('read')
    tables = (await call(reader,'db_list_tables'))['result']['tables']
    doc.write_text('NewOwner owns Project'); table.write_text('amount\n20\n')
    second = await publish('NewOwner')
    assert first != second
    for token,owner,amount in [(reader,'OldOwner','10'),(await begin('read'),'NewOwner','20')]:
        kb = await call(token,'kb_search',query='Project owner')
        graph = await call(token,'graph_search_entities',query=owner)
        sql = await call(token,'db_query_sql',sql=f'SELECT amount FROM "{tables[0]}"')
        raw = await call(token,'raw_file_read',path=str(doc))
        assert owner in json.dumps(kb['result'])
        assert graph['result']['entities'][0]['id'] == owner
        assert str(sql['result']['rows'][0][0]) == amount
        assert owner in raw['result']['text']
        assert len({r['workspace_revision'] for r in (kb,graph,sql,raw)}) == 1
    Workspace(settings).rollback(first,expected_revision=second)
    assert (await call(await begin('read'),'graph_search_entities',query='OldOwner'))['result']['entities']


@pytest.mark.asyncio
async def test_profile_binding_rejects_cross_profile_operation(settings,monkeypatch):
    monkeypatch.setenv('DATAMIND_PROFILE','team-a')
    with pytest.raises(PermissionError,match='different profile'):
        await execute('datamind_operation_begin',{'profile':'team-b','mode':'write'})
    token = await begin()
    monkeypatch.setenv('DATAMIND_PROFILE','team-b')
    with pytest.raises(ValueError,match='Unknown operation'):
        await call(token,'graph_schema')


@pytest.mark.asyncio
async def test_workspace_publication_respects_explicit_enabled_surfaces(settings,tmp_path):
    runtime = await build_data_plane(settings,enable={'graph'})
    source = tmp_path/'input';source.mkdir()
    try:
        result = await runtime.call('workspace_publish',{'path':str(source),'surfaces':['db']})
        assert result['results'][0]['status'] == 'failed'
        assert 'not enabled' in result['results'][0]['error']
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_host_read_mode_and_bound_session_enforced_below_agent_prompt(settings,monkeypatch):
    monkeypatch.setenv('DATAMIND_SESSION_ID','session-a')
    write=await begin()
    monkeypatch.setenv('DATAMIND_ACCESS_MODE','read')
    with pytest.raises(PermissionError,match='only permits data reads'):
        await begin('write')
    with pytest.raises(PermissionError,match='only permits data reads'):
        await call(write,'graph_upsert_triples',triples=[])
    reader=await begin('read',pin_workspace=False)
    monkeypatch.setenv('DATAMIND_SESSION_ID','session-b')
    with pytest.raises(PermissionError,match='different Codex session'):
        await call(reader,'graph_schema')


@pytest.mark.asyncio
async def test_mcp_capability_set_cannot_be_expanded_by_agent(settings,monkeypatch):
    monkeypatch.setenv('DATAMIND_ENABLED_SURFACES','["graph"]')
    token=await begin()
    with pytest.raises(ValueError,match='Unknown direct data tool'):
        await call(token,'db_list_tables')
    with pytest.raises(PermissionError,match='capability set'):
        await call(token,'workspace_publish',path='.',surfaces=['db'])
