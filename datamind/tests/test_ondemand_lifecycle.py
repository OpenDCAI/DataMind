"""On-demand imports, source-bound output and outstanding MCP request lifecycle."""
import asyncio
import csv
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, text
from datamind.config import Settings, VideoConfig
from datamind.operations import OperationStore
from datamind.host import CodexHost
from datamind import mcp


def settings(tmp_path):
    return Settings(_env_file=None, data={'base_dir':tmp_path/'data'},
                    logging={'directory':Path(os.environ.get('DATAMIND_TEST_LOG_ROOT', str(tmp_path/'logs')))/tmp_path.name},
                    codex={'managed_operations':True, 'timeout_s':5})


def test_json_live_import_preserves_types_and_rejects_schema_change(tmp_path):
    from datamind.capabilities.ingest.tables import import_json_table
    from datamind.capabilities.db.providers.sqlite import SQLiteDialect
    source=tmp_path/'table.json'
    row={'代码':'00021','数值':12.5,'空值':None,'整数':9007199254740993}
    source.write_text(json.dumps({'table':'数据','records':[row,row]}))
    engine=create_engine('sqlite:///'+str(tmp_path/'live.sqlite'))
    db=SimpleNamespace(engine=engine,dialect=SQLiteDialect())
    result=import_json_table(db,source,table='sample',if_exists='fail')
    assert result['rows_inserted']==2
    with engine.connect() as conn:
        assert list(conn.execute(text('SELECT "代码","数值","空值","整数" FROM sample'))) == [('00021',12.5,None,9007199254740993)]*2
    row['数值']='wrong-type'
    source.write_text(json.dumps({'table':'数据','records':[row]}))
    with pytest.raises(Exception,match='identical'):
        import_json_table(db,source,table='sample',if_exists='append')
    with engine.connect() as conn: assert conn.execute(text('SELECT COUNT(*) FROM sample')).scalar()==2
    engine.dispose()


async def test_dispatcher_serializes_data_but_status_and_ping_remain_available(tmp_path,monkeypatch):
    cfg=settings(tmp_path);monkeypatch.setattr(mcp,'load_settings',lambda *a:cfg)
    monkeypatch.setenv('DATAMIND_REQUEST_ID','request')
    gate=asyncio.Event();started=[]
    async def execute(name,args):
        if name=='datamind_task_status':return {'jobs':OperationStore(cfg).jobs('request')}
        started.append(name);await gate.wait();return {'result':{'done':name}}
    monkeypatch.setattr(mcp,'execute',execute)
    dispatcher=mcp.Dispatcher()
    def msg(i,name):return {'id':i,'method':'tools/call','params':{'name':name}}
    a=asyncio.create_task(dispatcher.dispatch(msg(1,'first')))
    b=asyncio.create_task(dispatcher.dispatch(msg(2,'second')))
    await asyncio.sleep(.03)
    result=await asyncio.wait_for(dispatcher.dispatch(msg(3,'datamind_task_status')),.2)
    assert {j['status'] for j in result['result']['structuredContent']['jobs']}=={'queued','running'}
    assert started==['first']
    assert (await dispatcher.dispatch({'id':4,'method':'ping'}))['result']=={}
    b.cancel()
    with pytest.raises(asyncio.CancelledError):await b
    gate.set();await a
    assert [j['status'] for j in OperationStore(cfg).jobs('request')].count('cancelled')==1
    assert OperationStore(cfg).jobs('other-request')==[]


async def test_host_drains_unfinished_tool_before_session_close_and_resumes_same_thread(tmp_path):
    cfg=settings(tmp_path);calls=[];closed=[]
    class Session:
        def __init__(self,**kw):
            self.request=kw['env']['DATAMIND_REQUEST_ID'];self.store=OperationStore(cfg)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):
            closed.extend(j['status'] for j in self.store.jobs(self.request))
        async def stream(self,prompt,**kwargs):
            calls.append(prompt)
            if len(calls)==1:
                self.store.set_job(self.request,'rpc1','datamind_workspace_publish','running')
                async def complete():
                    await asyncio.sleep(.1)
                    self.store.set_job(self.request,'rpc1','datamind_workspace_publish','completed',{'receipt':'done'})
                asyncio.create_task(complete())
                yield {'method':'item/started','payload':{'item':{'id':'sdk1','type':'mcpToolCall','tool':'datamind_workspace_publish'}}}
            else:
                assert 'receipt' in prompt and 'done' in prompt
                yield {'method':'item/completed','payload':{'item':{'type':'agentMessage','text':'final'}}}
            yield {'method':'turn/completed','payload':{'turn':{'status':'completed'}}}
    host=CodexHost(cfg,session_factory=Session)
    result=await host.ingest('do the task')
    assert result['answer']=='final' and len(calls)==2 and closed==['completed']
    await host.aclose()


async def test_host_pending_wait_obeys_original_deadline(tmp_path):
    cfg=settings(tmp_path);cfg.codex.timeout_s=.15
    requests=[]
    class Session:
        def __init__(self,**kw):self.req=kw['env']['DATAMIND_REQUEST_ID'];requests.append(self.req)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def stream(self,*args,**kwargs):
            OperationStore(cfg).set_job(self.req,'1','long','running')
            yield {'method':'turn/completed','payload':{'turn':{'status':'completed'}}}
    host=CodexHost(cfg,session_factory=Session)
    with pytest.raises(TimeoutError):await host.ingest('work')
    assert OperationStore(cfg).jobs(requests[0])[0]['status']=='cancelled'


async def test_ondemand_does_not_publish_and_rejects_export_from_another_request(tmp_path):
    from datamind.dataspace import run_task,save_prediction
    from datamind.workspace import Workspace
    root=tmp_path/'dataset';task=root/'input/task_1';(task/'context').mkdir(parents=True)
    (task/'task.json').write_text(json.dumps({'question':'Return the amount.'}))
    (task/'context/records.json').write_text('{"table":"t","records":[{"amount":42}]}')
    cfg=settings(tmp_path);requests=[]
    class Host:
        def __init__(self,cfg,**kwargs):self.cfg=cfg;requests.append(cfg)
        async def ingest(self,prompt,**kw):
            assert 'Return the amount.' in prompt and 'no requirement to build all' in prompt
            assert kw['final_contract']['required']==['result_id']
            data=b'amount\n42\n';rid='a'*32;store=self.cfg.data.storage_dir/'query_results';store.mkdir(parents=True)
            from datamind.core.context import RequestContext
            from datamind.core.logging import bind_context
            from datamind.capabilities.db.query_contract import AnswerSchemas
            with bind_context(RequestContext(session_id='fixture',profile=self.cfg.data.profile,extra={'host_request_id':'current'})):
                schema=AnswerSchemas(store).define('Return the amount.',[{'name':'amount','requirement':'Requested amount'}],'scalar')
            (store/(rid+'.csv')).write_bytes(data)
            (store/(rid+'.json')).write_text(json.dumps({'complete':True,'truncated':False,'workspace_revision':None,
                'answer_validated':True,'answer_schema_id':schema['schema_id'],'answer_schema_revision':schema['revision'],
                'columns':['amount'],'request_scope':'current',
                'sha256':hashlib.sha256(data).hexdigest(),'profile':self.cfg.data.profile,'request_id':'current','row_count':1}))
            return {'answer':json.dumps({'result_id':rid}),'request_id':'current'}
        async def aclose(self):pass
    result=await run_task(cfg,root,'task_1',tmp_path/'runtime',host_factory=Host)
    assert Path(result['prediction']).read_text()=='amount\n42\n'
    assert Workspace(requests[0]).current_revision is None
    from datamind.core.errors import CapabilityError
    with pytest.raises(CapabilityError,match='profile/request'):
        save_prediction(requests[0],'a'*32,None,tmp_path/'bad.csv',request_id='other')


async def test_strict_vision_schema_is_sent_and_locally_validated(tmp_path,monkeypatch):
    from datamind.capabilities.ingest.video.providers import describe_frame
    from datamind.core import model_clients
    monkeypatch.setattr(model_clients,'_record_nested_usage',lambda *a:None)
    cfg=VideoConfig(vision={'model':'test','protocol':'openai_chat_completions'},ocr_response_format='json_schema')
    image=tmp_path/'frame.png';image.write_bytes(b'frame')
    calls=[]
    class Client:
        invalid=False
        async def complete(self,**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=[{'type':'text','text': 'garbage' if self.invalid else json.dumps({'layout':{'tag':'page','children':[{'tag':'value','children':['42']} ]}})}],stop_reason='stop',resolved_model='test')
    client=Client();out=await describe_frame(client,image,cfg)
    assert out['ocr_text']=='42' and calls[0]['response_format']['json_schema']['strict']
    client.invalid=True
    with pytest.raises(Exception,match='invalid JSON'):
        await describe_frame(client,image,cfg)


async def test_frames_only_requires_no_models_and_returns_images(video_workspace):
    from datamind.video_tools import video_tools,mcp_content
    workspace,source,video,calls=video_workspace
    cfg=workspace.settings;cfg.video.vision=None;cfg.video.asr_model_dir=None
    tools={t.name:t for t in video_tools(cfg)}
    prepared=await tools['video_prepare'].handler(path=str(video),ocr=False,asr=False)
    assert prepared['asr_status']=='disabled' and prepared['ocr_status']=='disabled'
    assert calls['ocr']==calls['asr']==0
    read=await tools['kb_video_read'].handler(bundle_id=prepared['bundle_id'])
    assert read['frames'][0]['ocr_status']=='disabled'
    frame=await tools['kb_video_frame'].handler(bundle_id=prepared['bundle_id'],frame_id=read['frames'][0]['frame_id'])
    assert mcp_content({'result':frame})['content'][1]['type']=='image'
    assert workspace.current_revision is None

from datamind.tests.test_video_workspace import video_workspace


async def test_plaintext_ocr_never_parses_model_json(tmp_path,monkeypatch):
    from datamind.capabilities.ingest.video.providers import describe_frame,parse_layout
    from datamind.core import model_clients
    monkeypatch.setattr(model_clients,'_record_nested_usage',lambda *a:None)
    raw='Quotes "42", brackets [unclosed, Unicode 数值 and lines\nsecond line'
    class Client:
        async def complete(self,**kw):
            assert 'response_format' not in kw
            return SimpleNamespace(content=[{'type':'text','text':raw}],stop_reason='stop',resolved_model='test')
    image=tmp_path/'frame.png';image.write_bytes(b'frame')
    result=await describe_frame(Client(),image,VideoConfig(vision={'model':'test'},ocr_response_format='text'))
    assert result['ocr_text']==raw
    assert parse_layout(json.dumps(result['layout']))[1]==raw
    assert result['layout_origin']=='program_wrapped_transcript'


async def test_empty_kb_reports_unindexed_and_makes_no_retrieval_request(tmp_path):
    from datamind.capabilities.kb.tools import build_kb_tools
    class KB:
        data_dir=tmp_path
        vector_store=SimpleNamespace(name='fixture')
        async def count(self):return 0
        async def search(self,*a,**kw):raise AssertionError('Empty index must not call the retriever/embedding API')
    tools={t.name:t for t in build_kb_tools(KB())}
    result=await tools['kb_search'].handler(query='question')
    assert result['index_status']=='empty' and result['indexed_chunks']==0
    assert 'raw_file_read' in result['hint'] and 'kb_add_path' in result['hint']
