"""General final checks and bounded recovery, independent of benchmark tasks."""
import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from datamind.config import Settings, DBConfig
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from datamind.core.errors import CapabilityError, FinalAnswerContractError
from datamind.core.diagnostics import exception_detail
from datamind.capabilities.db.providers.sqlite import SQLiteDialect
from datamind.capabilities.db.native_sqlite import database_target
from datamind.capabilities.db.service import DBService
from datamind.capabilities.db.tools import build_db_tools
from datamind.capabilities.db.query_contract import MANAGED_ANSWER_SCHEMA, ManagedAnswerExportValidator
from datamind.host import CodexHost


def settings(tmp_path, timeout=5):
    return Settings(_env_file=None,data={'base_dir':tmp_path/'data','profile':'ordinary'},
        logging={'directory':Path(os.environ.get('DATAMIND_TEST_LOG_ROOT', str(tmp_path/'logs')))/tmp_path.name,'event_trace':True},
        codex={'timeout_s':timeout,'managed_operations':True})


def events(answer):
    return [{'method':'item/completed','payload':{'item':{'type':'agentMessage','phase':'final_answer','text':answer}}},
            {'method':'turn/completed','payload':{'turn':{'status':'completed'}}}]


async def test_unknown_database_error_is_actionable_and_never_guesses(tmp_path):
    path=tmp_path/'db.sqlite'
    with sqlite3.connect(path) as con:con.executescript('CREATE TABLE t(value); INSERT INTO t VALUES(42);')
    exact='sqlite_1234567890abcdef12345678';wrong='sqlite_12345678'
    db=SimpleNamespace(native_databases={exact:{'database_id':exact,'path':str(path),'source_uri':'file:///allowed/source.sqlite'}},
        native_reference=True,db_cfg=DBConfig(),result_dir=None)
    for args in ({'database_id':wrong},{'database_ids':[exact,wrong]}):
        with pytest.raises(CapabilityError) as exc:
            async with database_target(db,**args):pytest.fail('Must not resolve an invalid identifier')
        detail=exception_detail(exc.value)
        assert detail['details']['code']=='unknown_database_id'
        assert detail['details']['invalid_database_ids']==[wrong]
        assert detail['details']['available_databases']==[{'database_id':exact,'source_uri':'file:///allowed/source.sqlite'}]
        assert exact in detail['message'] and 'do not shorten' in detail['message']
    async with database_target(db,database_id=exact) as target:
        assert (await target.query_sql('SELECT value FROM t')).rows==[[42]]
    # A pinned registry is authoritative, even when a different live registry exists.
    live=tmp_path/'live.json';live.write_text(json.dumps({'another_profile_secret':{}}))
    db.native_databases={};db.native_registry_path=live
    with pytest.raises(CapabilityError) as exc:
        async with database_target(db,database_id=wrong):pass
    assert exc.value.details['available_databases']==[] and 'another_profile_secret' not in str(exc.value)


async def make_export(cfg, request_id, revision=None):
    root=cfg.data.storage_dir/'query_results';root.mkdir(parents=True,exist_ok=True)
    dialect=SQLiteDialect();engine=dialect.build_engine(None,default_path=str(root/'sample.sqlite'))
    db=DBService(dialect=dialect,engine=engine,db_cfg=DBConfig(row_limit=None),result_dir=root)
    tools={s.name:s.handler for s in build_db_tools(db)}
    ctx=RequestContext(session_id='test',profile=cfg.data.profile,extra={'host_request_id':request_id,'workspace_revision':revision})
    try:
        with bind_context(ctx):
            schema=await tools['db_answer_schema'](question='Return value 42.',columns=[{'name':'value','requirement':'Requested value'}],row_shape='scalar')
            result=await tools['db_export_sql'](sql='SELECT 42 AS value',intent={'purpose':'answer','question':'Return value 42.',
                'return_columns':['value'],'schema_id':schema['schema_id']})
        return result['result_id']
    finally:await db.aclose()


async def test_invalid_message_then_missing_export_recovers_in_same_session(tmp_path):
    cfg=settings(tmp_path);instances=[];prompts=[];exports=[];closed=[]
    class Session:
        def __init__(self,**kw):self.request=kw['env']['DATAMIND_REQUEST_ID'];instances.append(self)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):closed.append(True)
        async def stream(self,prompt,**kwargs):
            prompts.append(prompt)
            if len(prompts)==1:answer=json.dumps({'result_id':'BLOCKED: query did not succeed'})
            elif len(prompts)==2:
                assert 'pattern' in prompt
                answer=json.dumps({'result_id':'a'*32})
            else:
                assert 'missing' in prompt and 'Do not repeat successful imports' in prompt
                exports.append(await make_export(cfg,self.request))
                answer=json.dumps({'result_id':exports[-1]})
            for e in events(answer):yield e
    host=CodexHost(cfg,session_factory=Session)
    result=await host.ingest('Return 42.',final_contract=MANAGED_ANSWER_SCHEMA,
        final_validator=ManagedAnswerExportValidator(cfg.data.storage_dir/'query_results',cfg.data.profile))
    assert len(instances)==len(closed)==len(exports)==1 and len(prompts)==3
    assert json.loads(result['answer'])['result_id']==exports[0]
    assert result['recovery']['final_validation_retries']==2 and result['recovery']['host_continuations']==2
    diagnostics=(cfg.log_dir/'diagnostics.jsonl').read_text()
    assert diagnostics.count('final_validation_failed')==2 and 'final_validation_passed' in diagnostics
    await host.aclose()


@pytest.mark.parametrize('corruption',['foreign_request','wrong_revision','bad_hash'])
async def test_real_artifact_must_be_current_and_intact_before_done(tmp_path,corruption):
    cfg=settings(tmp_path);calls=[]
    class Session:
        def __init__(self,**kw):self.req=kw['env']['DATAMIND_REQUEST_ID']
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def stream(self,prompt,**kwargs):
            calls.append(prompt)
            first=len(calls)==1
            rid=await make_export(cfg,'other' if first and corruption=='foreign_request' else self.req,
                revision='old' if first and corruption=='wrong_revision' else None)
            if first and corruption=='bad_hash':
                (cfg.data.storage_dir/'query_results'/(rid+'.csv')).write_text('value\n99\n')
            for e in events(json.dumps({'result_id':rid})):yield e
    host=CodexHost(cfg,session_factory=Session)
    result=await host.ingest('Return 42',final_contract=MANAGED_ANSWER_SCHEMA,
        final_validator=ManagedAnswerExportValidator(cfg.data.storage_dir/'query_results',cfg.data.profile))
    assert len(calls)==2 and result['recovery']['final_validation_retries']==1
    await host.aclose()


async def test_generic_custom_validator_and_json_contract_apply_to_read_host(tmp_path):
    cfg=settings(tmp_path);calls=[];received=[]
    class Session:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def stream(self,prompt,**kwargs):
            calls.append(prompt)
            for e in events(['not json','{"answer":1}','{"answer":42}'][len(calls)-1]):yield e
    async def validate(value,**context):
        received.append(context)
        if value['answer']!=42:raise ValueError('The computed total must be 42 in this fixture')
    host=CodexHost(cfg,session_factory=Session)
    result=await host.query('generic request',final_contract={'type':'object','properties':{'answer':{'type':'integer'}},'required':['answer']},final_validator=validate)
    assert result['answer']=='{"answer":42}' and len(calls)==3 and len(received)==2
    assert len({r['request_id'] for r in received})==1
    await host.aclose()


@pytest.mark.parametrize('corruption',['metadata_array','metadata_invalid_json','missing_csv'])
async def test_missing_or_malformed_artifacts_produce_recoverable_validation_errors(tmp_path,corruption):
    from datamind.core.finalization import validate_final_answer
    cfg=settings(tmp_path);rid=await make_export(cfg,'request')
    root=cfg.data.storage_dir/'query_results'
    if corruption=='missing_csv':(root/(rid+'.csv')).unlink()
    else:(root/(rid+'.json')).write_text('[]' if corruption=='metadata_array' else '{')
    with pytest.raises(FinalAnswerContractError,match='missing|malformed'):
        await validate_final_answer(json.dumps({'result_id':rid}),schema=MANAGED_ANSWER_SCHEMA,
            validator=ManagedAnswerExportValidator(root,cfg.data.profile),request_id='request',workspace_revision=None)


async def test_exhaustion_never_publishes_invalid_done_or_restarts_session(tmp_path):
    cfg=settings(tmp_path);calls=[];closed=[]
    class Session:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):closed.append(True)
        async def stream(self,prompt,**kwargs):
            calls.append(prompt)
            for e in events('{"result_id":"blocked"}'):yield e
    host=CodexHost(cfg,session_factory=Session);observed=[]
    with pytest.raises(FinalAnswerContractError,match='validation_recovery_exhausted'):
        async for event in host.stream('work',final_contract=MANAGED_ANSWER_SCHEMA):observed.append(event)
    assert len(calls)==4 and closed==[True] and not any(e['type']=='done' for e in observed)
    await host.aclose()


async def test_recovery_keeps_original_deadline_and_cancellation_closes_session(tmp_path,monkeypatch):
    cfg=settings(tmp_path,5);calls=[];closed=[];deadlines=[];timers=[]
    original_timeout=asyncio.timeout
    def tracked_timeout(delay):
        timer=original_timeout(delay)
        if delay==cfg.codex.timeout_s:timers.append(timer)
        return timer
    monkeypatch.setattr(asyncio,'timeout',tracked_timeout)
    class Session:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):closed.append(asyncio.get_running_loop().time())
        async def stream(self,prompt,**kwargs):
            calls.append(prompt);deadlines.append(timers[0].when())
            if len(calls)>1:await asyncio.Event().wait()
            for e in events('{"result_id":"blocked"}'):yield e
    host=CodexHost(cfg,session_factory=Session)
    with pytest.raises(TimeoutError):await host.query('work',final_contract=MANAGED_ANSWER_SCHEMA)
    # Observe the actual timeout object rather than counting admission/cleanup I/O.
    assert len(timers)==1 and len(calls)==2 and deadlines[0]==deadlines[1]
    assert len(closed)==1 and deadlines[0]-.05 <= closed[0] < deadlines[0]+.5
    await host.aclose()


async def test_sync_validator_does_not_block_deadline_and_errors_are_not_swallowed(tmp_path):
    cfg=settings(tmp_path,.08);timing=[]
    class Session:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):timing.append(time.monotonic())
        async def stream(self,*args,**kwargs):
            for e in events('answer'):yield e
    def slow(*args,**kwargs):timing.append(time.monotonic());time.sleep(.5)
    host=CodexHost(cfg,session_factory=Session)
    with pytest.raises(TimeoutError):await host.query('work',final_validator=slow)
    assert len(timing)==2 and timing[1]-timing[0]<.4
    await host.aclose()
    def bug(*args,**kwargs):raise RuntimeError('programmer error')
    cfg.codex.timeout_s=5;host=CodexHost(cfg,session_factory=Session)
    with pytest.raises(RuntimeError,match='programmer error'):await host.query('work',final_validator=bug)
    await host.aclose()


async def test_tool_failure_flag_does_not_require_top_level_sdk_error(tmp_path):
    cfg=settings(tmp_path)
    class Session:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def stream(self,*args,**kwargs):
            yield {'method':'item/completed','payload':{'item':{'type':'mcpToolCall','tool':'datamind_db_query_sql',
                'status':'failed','error':None,'result':{'content':[{'type':'text','text':'{"message":"unknown ID"}'}]}}}}
            yield {'method':'item/completed','payload':{'item':{'type':'customToolCall','name':'custom','result':'plain text'}}}
            for e in events('done'):yield e
    host=CodexHost(cfg,session_factory=Session)
    observed=[e async for e in host.stream('work')]
    results=[e for e in observed if e['type']=='tool_result']
    assert results[0]['is_error'] and not results[1]['is_error']
    assert observed[-1]['evidence']==[]
    await host.aclose()
