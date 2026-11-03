"""One Codex host serves CLI/HTTP with scoped context, receipts and cancellation."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path

import pytest

from datamind.config import DataSettings
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from datamind.data_plane import build_data_plane
from datamind.host import CodexHost


class Session:
    instances=[]
    def __init__(self,**kwargs):
        self.kwargs=kwargs;self.closed=False
        self.instances.append(self)
    async def __aenter__(self):return self
    async def __aexit__(self,*args):self.closed=True
    async def stream(self,prompt,**kwargs):
        env=self.kwargs['env'];mode=env['DATAMIND_ACCESS_MODE']
        receipt=None
        if mode=='write':
            cfg=DataSettings(_env_file=None,data={'base_dir':self.kwargs['data_root'],'profile':self.kwargs['profile']})
            data=await build_data_plane(cfg,enable={'memory'})
            ctx=RequestContext(session_id=env['DATAMIND_SESSION_ID'],profile=cfg.data.profile,
                extra={'host_request_id':env['DATAMIND_REQUEST_ID']})
            try:
                with bind_context(ctx):
                    receipt=await data.call('memory_save',{'content':env['DATAMIND_REQUEST_ID']})
            finally:await data.aclose()
        item={'id':'call','type':'mcpToolCall','tool':'memory_save' if mode=='write' else 'graph_neighbors','arguments':{}}
        yield {'method':'item/started','payload':{'item':item}}
        result={'structuredContent':{'result':receipt or {'count':1}}}
        yield {'method':'item/completed','payload':{'item':{**item,'result':result}}}
        yield {'method':'item/agentMessage/delta','payload':{'delta':'answer'}}
        yield {'method':'item/completed','payload':{'item':{'type':'agentMessage','phase':'final_answer','text':'final answer'}}}
        yield {'method':'turn/completed','payload':{'turn':{'status':'completed'}}}


@pytest.mark.asyncio
async def test_same_host_handles_reads_writes_and_isolates_request_receipts(tmp_path):
    cfg=DataSettings(_env_file=None,data={'base_dir':tmp_path})
    host=CodexHost(cfg,session_factory=Session)
    try:
        first,second=await asyncio.gather(host.ingest('save a',session_id='a'),host.ingest('save b',session_id='b'))
        for result,session in ((first,'a'),(second,'b')):
            assert result['answer']=='final answer'
            assert len(result['receipts'])==1
            assert result['receipts'][0]['session_id']==session
            assert result['receipts'][0]['request_id']==result['request_id']
            assert result['tool_trace'][0]['name']=='memory_save'
        assert first['receipts'][0]['receipt_id']!=second['receipts'][0]['receipt_id']
        query=await host.query('read',session_id='a')
        assert query['receipts']==[]
        assert query['evidence'][0]['content']=={'count':1}
        assert all(s.closed for s in Session.instances[-3:])
    finally:await host.aclose()


@pytest.mark.asyncio
async def test_timeout_and_host_close_release_sdk_sessions(tmp_path):
    entered=asyncio.Event()
    class Hanging(Session):
        async def stream(self,*a,**kw):
            entered.set()
            await asyncio.Event().wait()
            yield {}
    cfg=DataSettings(_env_file=None,data={'base_dir':tmp_path},codex={'timeout_s':0.02})
    host=CodexHost(cfg,session_factory=Hanging)
    with pytest.raises(TimeoutError):await host.query('wait')
    assert Hanging.instances[-1].closed
    cfg.codex.timeout_s=20
    host=CodexHost(cfg,session_factory=Hanging)
    task=asyncio.create_task(host.query('wait'))
    await entered.wait();await asyncio.sleep(.01)
    await host.aclose()
    assert task.cancelled()
    assert Hanging.instances[-1].closed


def test_gateway_secrets_stay_in_env_and_pinned_context_is_forwarded(tmp_path):
    cfg=DataSettings(_env_file=None,data={'base_dir':tmp_path},codex={
        'base_url':'https://gateway.example/v1','api_key':'host-test-secret','model':'test'})
    host=CodexHost(cfg,revision='revision-old',session_factory=Session)
    session=host._session('user-a','req-a','read')
    assert session.kwargs['env']['DATAMIND_ACCESS_MODE']=='read'
    assert session.kwargs['env']['DATAMIND_WORKSPACE_REVISION']=='revision-old'
    assert session.kwargs['env']['DATAMIND_CODEX_GATEWAY_KEY']=='host-test-secret'
    assert 'host-test-secret' not in str(session.kwargs['config_overrides'])
    assert session.kwargs['model']=='test'


def test_product_data_layer_has_no_dual_agent_implementation():
    import datamind.agent as module
    assert not hasattr(module,'StoreAgent')
    assert not hasattr(module,'RetrieveAgent')
    assert not hasattr(module,'build_datamind')
    root=Path(__file__).resolve().parents[1]
    assert not (root/'agent/options.py').exists()


@pytest.mark.asyncio
async def test_host_collects_real_mcp_receipts_and_evidence_under_code_mode(tmp_path):
    import os,sys
    class CodeMode(Session):
        async def stream(self,*args,**kwargs):
            env={**os.environ,**self.kwargs['env'],
                 'DATAMIND_DATA_ROOT':self.kwargs['data_root'],
                 'DATAMIND_PROFILE':self.kwargs['profile']}
            process=await asyncio.create_subprocess_exec(sys.executable,'-m','datamind.mcp',
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,env=env)
            async def call(name,args):
                request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}}
                process.stdin.write((json.dumps(request)+'\n').encode());await process.stdin.drain()
                result=json.loads(await process.stdout.readline())['result']
                assert not result.get('isError'),result
                return result['structuredContent']
            try:
                operation=(await call('datamind_operation_begin',{'mode':'write'}))['operation_id']
                await call('datamind_graph_upsert_triples',{'operation_id':operation,
                    'triples':[{'subject':'A','relation':'owns','object':'B'}]})
                await call('datamind_graph_neighbors',{'operation_id':operation,'entity':'A'})
            finally:
                process.stdin.close();await process.wait()
            yield {'method':'item/started','payload':{'item':{'type':'customToolCall','name':'exec','input':'opaque code mode'}}}
            yield {'method':'item/completed','payload':{'item':{'type':'agentMessage','text':'A owns B','phase':None}}}
            yield {'method':'turn/completed','payload':{'turn':{'status':'completed'}}}
    cfg=DataSettings(_env_file=None,data={'base_dir':tmp_path})
    host=CodexHost(cfg,session_factory=CodeMode)
    try:
        result=await host.ingest('authorized graph write',session_id='a')
        assert result['answer']=='A owns B'
        assert len(result['receipts'])==1
        assert result['receipts'][0]['request_id']==result['request_id']
        assert [e['name'] for e in result['tool_trace']]==['graph_upsert_triples','graph_neighbors']
        assert result['evidence'][0]['surface']=='graph'
        assert result['evidence'][0]['content']['count']==1
        from datamind.operations import OperationStore
        assert OperationStore(cfg).take_events(result['request_id'])==[]
    finally:await host.aclose()


@pytest.mark.asyncio
async def test_query_keeps_admitted_revision_when_publication_precedes_first_tool(tmp_path,monkeypatch):
    import hashlib
    from datamind.workspace import Workspace
    cfg=DataSettings(_env_file=None,data={'base_dir':tmp_path})
    root=tmp_path/'source';root.mkdir()
    doc=root/'owner.md'
    workspace=Workspace(cfg)
    async def publish(owner):
        doc.write_text(f'Project owned by {owner}')
        version='sha256:'+hashlib.sha256(doc.read_bytes()).hexdigest()
        return await workspace.update(str(root),surfaces=['graph'],graph_sources=[{
            'uri':str(doc.resolve()),'version':version,
            'triples':[{'subject':'Project','relation':'owned_by','object':owner}]}])
    await publish('Alice')
    admitted=workspace.current_revision
    class Publishing(Session):
        async def stream(self,*args,**kwargs):
            await publish('Bob')
            assert workspace.current_revision!=admitted
            assert self.kwargs['env']['DATAMIND_WORKSPACE_REVISION']==admitted
            async with workspace.reader(self.kwargs['env']['DATAMIND_WORKSPACE_REVISION'],enable={'graph'}) as reader:
                neighbors=await reader.call('graph_neighbors',entity='Project')
                assert 'Alice' in json.dumps(neighbors)
                assert 'Bob' not in json.dumps(neighbors)
            yield {'method':'item/completed','payload':{'item':{'type':'agentMessage','text':'Alice','phase':'final_answer'}}}
            yield {'method':'turn/completed','payload':{'turn':{'status':'completed'}}}
    host=CodexHost(cfg,session_factory=Publishing)
    try:
        result=await host.query('Who owns Project?')
        assert result['answer']=='Alice'
        assert result['workspace_revision']==admitted
    finally:await host.aclose()
    import datamind.host as host_module
    class RecoveredHost:
        def __init__(self,*a,**kw):pass
        async def query(self,*a,**kw):
            return {'answer':'Alice','recovery':{'retries':2,'fallbacks':1,'degraded':True,'events':[]}}
        async def aclose(self):pass
    monkeypatch.setattr(host_module,'CodexHost',RecoveredHost)
    async with workspace.reader(admitted,enable={'graph'}) as reader:
        recovered=await reader.query('read with recovered provider')
    assert recovered['workspace_read_retries']==2
    assert recovered['recovery']['fallbacks']==1
