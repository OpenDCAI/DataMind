"""Query/answer contracts on ordinary DataMind profiles and real SQLite engines."""
import asyncio
import csv
import hashlib
import json

import pytest
from sqlalchemy import text

from datamind.config import DBConfig
from datamind.core.context import RequestContext
from datamind.core.errors import CapabilityError
from datamind.core.logging import bind_context
from datamind.capabilities.db.providers.sqlite import SQLiteDialect
from datamind.capabilities.db.service import DBService
from datamind.capabilities.db.tools import build_db_tools
from datamind.capabilities.db.query_contract import AnswerSchemas, validate_answer_export


@pytest.fixture
def db(tmp_path):
    dialect = SQLiteDialect()
    engine = dialect.build_engine(None, default_path=str(tmp_path/'source.sqlite'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE employees (name TEXT, department TEXT, salary INTEGER)'))
        conn.execute(text("INSERT INTO employees VALUES ('张三','Support',200),('张三','Support',200),(NULL,'Sales',100)"))
    service = DBService(dialect=dialect,engine=engine,db_cfg=DBConfig(row_limit=None),result_dir=tmp_path/'results')
    yield service
    engine.dispose()


def context(scope='ordinary-request', profile='ordinary-profile'):
    return RequestContext(session_id='session',profile=profile,extra={'host_request_id':scope,'query_contract_required':True})


async def call(db, name, **kwargs):
    return await next(s for s in build_db_tools(db) if s.name==name).handler(**kwargs)


async def schema(db, names=('name',), row_shape='records', **kwargs):
    return await call(db,'db_answer_schema',question='Find employees working in Support.',
                      columns=[{'name':name,'requirement':'Requested output '+name} for name in names],
                      row_shape=row_shape,**kwargs)


def intent(columns, purpose='explore', definition=None):
    value={'purpose':purpose,'question':'Find employees working in Support.','return_columns':columns}
    if definition:value['schema_id']=definition['schema_id']
    return value


async def test_generic_agent_requires_intent_but_python_api_keeps_compatibility(db):
    with bind_context(context()):
        with pytest.raises(CapabilityError,match='declare intent'):
            await call(db,'db_query_sql',sql='SELECT name FROM employees')
    assert (await call(db,'db_query_sql',sql='SELECT name FROM employees'))['rows'][0]==['张三']
    assert (await db.query_sql('SELECT name FROM employees')).columns==['name']
    from datamind import mcp
    definitions=mcp.definitions()
    for name in ('datamind_db_query_sql','datamind_db_export_sql'):
        assert 'intent' in definitions[name]['inputSchema']['required']
    assert 'datamind_db_answer_schema' in definitions


async def test_extra_output_rejected_before_database_execution_and_corrected(db):
    with bind_context(context()):
        definition=await schema(db)
        # A nonexistent table would fail if SQL were executed before projection checks.
        with pytest.raises(CapabilityError,match='SQL projects'):
            await call(db,'db_query_sql',sql='SELECT name, department FROM missing',intent=intent(['name']))
        with pytest.raises(CapabilityError,match='exactly match'):
            await call(db,'db_export_sql',sql='SELECT name, department FROM employees',
                       intent=intent(['name','department'],'answer',definition))
        result=await call(db,'db_export_sql',sql="SELECT name FROM employees WHERE department='Support' ORDER BY salary DESC",
                          intent=intent(['name'],'answer',definition))
    assert result['columns']==['name'] and result['row_count']==2 and result['answer_validated']
    with open(result['path'],newline='') as f:assert list(csv.reader(f))==[['name'],['张三'],['张三']]
    assert validate_answer_export(db.result_dir,result['result_id'],profile='ordinary-profile',scope='ordinary-request')==result


async def test_exploration_star_expands_and_zero_row_metadata_is_checked(db):
    with bind_context(context()):
        result=await call(db,'db_query_sql',sql='SELECT * FROM employees',intent=intent(['name','department','salary']))
        assert len(result['rows'])==3
        with pytest.raises(CapabilityError,match='actual database columns'):
            await call(db,'db_query_sql',sql='SELECT * FROM employees WHERE 0',intent=intent(['name']))
        definition=await schema(db,('name','department','salary'))
        with pytest.raises(CapabilityError,match=r'SELECT \*'):
            await call(db,'db_export_sql',sql='SELECT * FROM employees',
                       intent=intent(['name','department','salary'],'answer',definition))


async def test_scalar_aggregate_allows_filter_join_columns_but_not_output_fields(db):
    with bind_context(context()):
        definition=await schema(db,('count',),'scalar')
        sql="SELECT COUNT(*) AS count FROM employees e JOIN (SELECT 'Support' AS d) x ON e.department=x.d"
        result=await call(db,'db_export_sql',sql=sql,intent=intent(['count'],'answer',definition))
        assert result['preview']==[[2]]
        with pytest.raises(CapabilityError,match='scalar answer requires'):
            await call(db,'db_export_sql',sql='SELECT salary AS count FROM employees',intent=intent(['count'],'answer',definition))
        with pytest.raises(CapabilityError,match='aliases'):
            await call(db,'db_query_sql',sql='SELECT salary+1 FROM employees',intent=intent(['amount']))
    # Failed export must leave no published artifact.
    assert len(list(db.result_dir.glob('*.csv')))==1


async def test_answer_scope_revision_and_truncation(db):
    with bind_context(context()):
        first=await schema(db)
        assert (await schema(db))['schema_id']==first['schema_id']
        result=await call(db,'db_export_sql',sql='SELECT name FROM employees',intent=intent(['name'],'answer',first))
        with pytest.raises(CapabilityError,match='revision_reason'):await schema(db,('salary',))
        second=await schema(db,('salary',),revision_reason='User actually requested salary rather than employee names.')
        assert second['revision']==2
        with pytest.raises(CapabilityError,match='revised'):
            validate_answer_export(db.result_dir,result['result_id'],profile='ordinary-profile',scope='ordinary-request')
        db.db_cfg.row_limit=1
        with pytest.raises(CapabilityError,match='truncated'):
            await call(db,'db_export_sql',sql='SELECT salary FROM employees',intent=intent(['salary'],'answer',second))
    for ctx in (context('different-request'),context(profile='different-profile')):
        with bind_context(ctx),pytest.raises(CapabilityError,match='unknown answer schema'):
            await call(db,'db_query_sql',sql='SELECT salary FROM employees',intent=intent(['salary'],'answer',second))


async def test_concurrency_four_has_independent_intents_and_schemas(db):
    async def run(index):
        ctx=context(f'request-{index}')
        name=f'value_{index}'
        with bind_context(ctx):
            definition=await schema(db,(name,),'scalar')
            await asyncio.sleep(.01)
            result=await call(db,'db_export_sql',sql=f'SELECT {index} AS {name}',intent=intent([name],'answer',definition))
            validate_answer_export(db.result_dir,result['result_id'],profile=ctx.profile,scope=ctx.extra['host_request_id'])
            return result
    results=await asyncio.gather(*(run(i) for i in range(4)))
    assert [r['preview'] for r in results]==[[[i]] for i in range(4)]
    assert len({r['answer_schema_id'] for r in results})==4


@pytest.mark.parametrize('damage',['hash','header','width','count','unvalidated'])
async def test_final_gate_rejects_malformed_or_unvalidated_exports(db,damage):
    with bind_context(context()):
        definition=await schema(db)
        result=await call(db,'db_export_sql',sql='SELECT name FROM employees',intent=intent(['name'],'answer',definition))
    path=db.result_dir/(result['result_id']+'.csv');meta=db.result_dir/(result['result_id']+'.json')
    if damage=='hash':path.write_text('name\nwrong\n')
    elif damage in {'header','width','count'}:
        path.write_text({'header':'unexpected\n张三\n张三\n\n','width':'name\n张三,extra\n','count':'name\n张三\n'}[damage])
        result['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    else:result['answer_validated']=False
    meta.write_text(json.dumps(result))
    with pytest.raises(CapabilityError):
        validate_answer_export(db.result_dir,result['result_id'],profile='ordinary-profile',scope='ordinary-request')
