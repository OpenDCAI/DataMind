"""Task policy isolation, typed inputs and complete benchmark query results."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from datamind.config import Settings
from datamind.workspace import Workspace
from datamind.capabilities.ingest.tables import normalize_columns


class Embed:
    dimension = 4
    name = 'fixture'

    async def embed_texts(self, texts):
        return [[1., 0., 0., 0.] for _ in texts]

    async def embed_query(self, query):
        return [1., 0., 0., 0.]


def sources(root):
    pytest.importorskip('ijson')
    root.mkdir()
    (root / '中文.csv').write_text('名称,收入（万元）,名称,备注:说明\n甲,12.5,乙,说明\n', encoding='utf-8')
    path = root / 'source.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE "公司" ("编号" INTEGER PRIMARY KEY, "名称" TEXT);
            CREATE TABLE "销售" ("编号" INTEGER, "金额" REAL, "代码" TEXT, "空值" TEXT, "字节" BLOB,
                                FOREIGN KEY ("编号") REFERENCES "公司"("编号"));
            CREATE TABLE "空表" ("字段" TEXT);
            INSERT INTO "公司" VALUES (1, '甲');
        ''')
        db.executemany('INSERT INTO "销售" VALUES (?,?,?,?,?)', [(1, 12.5, '000021', None, b'\x00\xff')] * 2)
    (root / 'records.json').write_text(json.dumps({'table': 'records', 'records': [
        {'编号': 1, '代码': '000021', '金额': 12.5, '空值': None, '经营范围': '经营范围与业务说明。' * 30},
        {'编号': 2, '代码': '000022', '金额': 8, '空值': None, '经营范围': '短文本'},
    ]}, ensure_ascii=False))
    return path


def config(tmp_path, monkeypatch, *, incremental=False, dataspace=False):
    from datamind import runtime
    monkeypatch.setattr(runtime, 'build_embedding', lambda *a, **kw: Embed())
    return Settings(_env_file=None, data={'base_dir': tmp_path / 'runtime'},
        embedding={'dimension': 4, 'model': 'fixture'},
        kb={'vector_store': 'lancedb' if incremental else 'chroma'},
        workspace={'storage_mode': 'incremental' if incremental else 'snapshot', 'retention': 'all',
                   'auto_gc': False, 'input_policy': 'dataspace' if dataspace else 'standard'})


def test_column_mapping_keeps_unicode_and_duplicate_values():
    columns, mapping = normalize_columns(['名称', '收入（万元）', '名称', '', '_source_file', '备注:说明', 'a"b'])
    assert columns == ['名称', '收入（万元）', 'col_3', 'col_4', 'col_5', '备注:说明', 'a"b']
    assert [m['original_name'] for m in mapping][:3] == ['名称', '收入（万元）', '名称']


@pytest.mark.asyncio
async def test_policy_switch_typed_sqlite_chinese_csv_and_old_revision(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    dbfile = sources(root)
    original = hashlib.sha256(dbfile.read_bytes()).hexdigest()
    cfg = config(tmp_path, monkeypatch)
    w = Workspace(cfg)
    first = await w.update(str(root), surfaces=['db'])
    assert len(first['tables']) == 4  # three SQLite tables plus CSV, no ordinary JSON
    cfg.workspace.input_policy = 'dataspace'
    w = Workspace(cfg)
    second = await w.update(str(root), surfaces=['kb', 'db', 'graph'], graph_sources=[])
    assert len(second['tables']) == 5
    assert first['pipeline_hash'] != second['pipeline_hash']
    by_name = {v['sheet']: k for k, v in second['tables'].items()}
    async with w.reader() as reader:
        sales = '销售'
        database_id = next(iter(second['databases']))
        result = await reader.call('db_query_sql', sql=f'SELECT "编号","金额","代码","空值","字节" FROM "{sales}"', database_id=database_id)
        assert result['rows'] == [[1, 12.5, '000021', None, b'\x00\xff']] * 2
        schema = await reader.call('db_describe_table', table=sales, database_id=database_id)
        assert schema['source_metadata']['foreign_keys'][0]['table'] == '公司'
        result = await reader.call('db_query_sql', sql=f'SELECT * FROM "{by_name["中文"]}"')
        assert result['rows'][0][:4] == ['甲', '12.5', '乙', '说明']
        result = await reader.call('db_query_sql', sql=f'SELECT "编号","代码","金额","空值" FROM "{by_name["records"]}" ORDER BY "编号"')
        assert result['rows'] == [[1, '000021', 12.5, None], [2, '000022', 8.0, None]]
        assert '经营范围' in str(await reader.call('kb_search', query='经营范围', filters={'kind': 'table_text'}))
        assert 'references' in str(await reader.call('graph_schema'))
    async with w.reader(first['workspace_revision']) as reader:
        assert len((await reader.call('db_list_tables'))['tables']) == 1
        database_id = next(iter(first['databases']))
        assert len((await reader.call('db_list_tables', database_id=database_id))['tables']) == 3
    third = await w.update(str(root), surfaces=['kb','db','graph'], graph_sources=[])
    assert third['reused_sources'] == 3
    assert hashlib.sha256(dbfile.read_bytes()).hexdigest() == original
    await w.collect(dry_run=False)
    fourth = await w.update(str(root), surfaces=['kb','db','graph'], graph_sources=[])
    assert fourth['reused_sources'] == 3


@pytest.mark.asyncio
async def test_incremental_schema_text_and_graph_use_the_same_adapter(tmp_path, monkeypatch):
    pytest.importorskip('lancedb')
    root = tmp_path / 'sources'
    sources(root)
    w = Workspace(config(tmp_path, monkeypatch, incremental=True, dataspace=True))
    first = await w.update(str(root), surfaces=['kb','graph'], graph_sources=[])
    assert len(first['tables']) == 4  # SQLite and JSON, CSV has no KB/Graph expansion
    async with w.reader() as reader:
        assert 'references' in str(await reader.call('graph_schema'))
        assert '经营范围' in str(await reader.call('kb_search', query='业务', filters={'kind':'table_text'}))
    dbfile = root / 'source.sqlite'
    original = dbfile.read_bytes()
    dbfile.unlink()
    await w.update(str(root), surfaces=['kb','graph'], graph_sources=[])
    async with w.reader(first['workspace_revision']) as reader:
        # Reading already-built evidence no longer accesses unrelated input files.
        assert 'references' in str(await reader.call('graph_schema'))
    dbfile.write_bytes(original)
    async with w.reader(first['workspace_revision']) as reader:
        assert 'references' in str(await reader.call('graph_schema'))


@pytest.mark.asyncio
async def test_query_cap_export_and_explicit_sql_limit(tmp_path, monkeypatch):
    from datamind.capabilities.db.service import build_db_service
    sql = 'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1505) SELECT x FROM n'
    cfg = config(tmp_path, monkeypatch)
    db = build_db_service(cfg)
    try:
        result = await db.query_sql(sql)
        assert len(result.rows) == 1000 and result.truncated
        limited = await db.export_sql(sql)
        assert limited['row_count'] == 1000 and not limited['complete']
        cfg.db.row_limit = None
        result = await db.query_sql(sql)
        assert len(result.rows) == 1505 and not result.truncated
        full = await db.export_sql(sql)
        assert full['row_count'] == 1505 and full['complete'] and full['preview_only']
        with Path(full['path']).open(newline='') as f:
            rows = list(csv.reader(f))
        assert len(rows) == 1506 and rows[-1] == ['1505']
        small = await db.query_sql(sql + ' LIMIT 10')
        assert len(small.rows) == 10
        with pytest.raises(Exception):
            await db.export_sql('DROP TABLE x')
    finally:
        await db.aclose()


def test_null_limit_is_forwarded_and_test_settings_do_not_mutate_defaults(tmp_path, monkeypatch):
    from datamind.dataspace import task_settings
    from datamind.host import CodexHost
    task = tmp_path / 'dataset' / 'input' / 'task_1'
    (task / 'context').mkdir(parents=True)
    (task / 'task.json').write_text('{"question":"test"}')
    normal = Settings(_env_file=None)
    cfg, _, _ = task_settings(normal, tmp_path/'dataset', 'task_1', tmp_path/'runtime')
    assert normal.db.row_limit == 1000 and normal.workspace.input_policy == 'standard'
    assert cfg.db.row_limit is None and cfg.workspace.input_policy == 'dataspace'
    host = CodexHost(cfg, session_factory=lambda **kw: kw)
    env = host._session(None, 'test', 'read')['env']
    assert env['DATAMIND__DB__ROW_LIMIT'] == 'null'
    for key in ('DATAMIND__DB__ROW_LIMIT','DATAMIND__WORKSPACE__INPUT_POLICY'):
        monkeypatch.setenv(key, env[key])
    child = Settings(_env_file=None)
    assert child.db.row_limit is None and child.workspace.input_policy == 'dataspace'


@pytest.mark.asyncio
async def test_direct_sqlite_and_chinese_csv_import(tmp_path, monkeypatch):
    from datamind.data_plane import build_data_plane
    root = tmp_path / 'sources'
    sources(root)
    cfg = config(tmp_path, monkeypatch)
    cfg.hooks.path_allowlist_extra = [str(root)]
    system = await build_data_plane(cfg, enable={'db'})
    try:
        svc = system.services.ingest
        result = await svc.db_import_path(path=str(root/'source.sqlite'), if_exists='replace')
        assert result['tables_processed'] == 3
        result = await svc.db_import_path(path=str(root/'中文.csv'), if_exists='replace')
        table = result['tables'][0]['table']
        schema = await system.services.db.describe(table)
        assert schema.source_metadata['column_mapping'][1]['original_name'] == '收入（万元）'
        rows = await system.services.db.query_sql(f'SELECT "名称", "收入（万元）", "col_3", "备注:说明" FROM "{table}"')
        assert rows.rows == [['甲', '12.5', '乙', '说明']]
    finally:
        await system.aclose()


@pytest.mark.asyncio
async def test_dataspace_cli_api_build_export_and_task_isolation(tmp_path, monkeypatch):
    pytest.importorskip('ijson')
    from datamind.dataspace import run_task
    dataset = tmp_path/'dataset'
    for n in [1,2]:
        task = dataset/'input'/f'task_{n}'
        (task/'context').mkdir(parents=True)
        (task/'task.json').write_text('{"question":"return numbers"}')
        (task/'context'/'same.json').write_text(json.dumps({'table':'same','records':[{'n':n}]}))
    cfg = Settings(_env_file=None, workspace={'auto_gc':False})
    runtime = tmp_path/'runtime'
    inventories = []
    inventory = Workspace._inventory

    def counted(self, source, *, fingerprint=True):
        inventories.append(fingerprint)
        return inventory(self, source, fingerprint=fingerprint)

    monkeypatch.setattr(Workspace, '_inventory', counted)
    a = await run_task(cfg, dataset, 'task_1', runtime, phase='build', surfaces=['db'])
    b = await run_task(cfg, dataset, 'task_2', runtime, phase='build', surfaces=['db'])
    assert inventories == [True, False, True, False]
    sql = 'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1505) SELECT x FROM n'
    result = await run_task(cfg,dataset,'task_1',runtime,phase='query',sql=sql)
    assert result['rows'] == 1505
    assert result['workspace_revision'] == a['workspace_revision'] != b['workspace_revision']
    assert Path(result['prediction']).is_file()


@pytest.mark.asyncio
async def test_quoted_comment_like_headers_and_json_large_integer(tmp_path, monkeypatch):
    pytest.importorskip('ijson')
    root = tmp_path/'source'
    root.mkdir()
    with (root/'quoted.csv').open('w',newline='') as f:
        csv.writer(f).writerows([['中文--列','a/*b*/','x;y','a"b'],['甲','乙','丙','丁']])
    big = 9007199254740993
    (root/'mixed.json').write_text(json.dumps({'table':'mixed','records':[{'value':big},{'value':1.5},{'value':'001'}]}))
    w=Workspace(config(tmp_path,monkeypatch,dataspace=True))
    m=await w.update(str(root),surfaces=['db'])
    names={t['sheet']:n for n,t in m['tables'].items()}
    async with w.reader() as reader:
        result=await reader.call('db_query_sql',sql=f'SELECT "中文--列","a/*b*/","x;y","a""b" FROM "{names["quoted"]}"')
        assert result['rows']==[['甲','乙','丙','丁']]
        result=await reader.call('db_query_sql',sql=f'SELECT value FROM "{names["mixed"]}" ORDER BY _source_row')
        assert result['rows']==[[big],[1.5],['001']]


@pytest.mark.asyncio
async def test_empty_sqlite_and_wal_do_not_silently_lose_data(tmp_path,monkeypatch):
    root=tmp_path/'source';root.mkdir()
    path=root/'live.sqlite'
    con=sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE t(x INTEGER)');con.commit()
    w=Workspace(config(tmp_path,monkeypatch))
    try:
        m = await w.update(str(root), surfaces=['db'])
        async with w.reader() as reader:
            result = await reader.call('db_query_sql', database_id=next(iter(m['databases'])), sql='SELECT * FROM t')
            assert result['rows'] == []
        cfg = config(tmp_path / 'reference', monkeypatch, dataspace=True)
        with pytest.raises(Exception, match='WAL'):
            await Workspace(cfg).update(str(root), surfaces=['db'])
    finally:
        con.close()
    m=await w.update(str(root),surfaces=['db'])
    assert next(iter(m['tables'].values()))['rows'] is None


@pytest.mark.asyncio
async def test_model_runner_uses_ingest_and_rejects_missing_publication(tmp_path, monkeypatch):
    pytest.importorskip('ijson')
    from datamind import host
    from datamind.dataspace import run_task
    dataset = tmp_path / 'dataset'
    task = dataset / 'input' / 'task_1'
    (task / 'context').mkdir(parents=True)
    (task / 'task.json').write_text('{"question":"return the input"}')
    (task / 'context' / 'rows.json').write_text('{"table":"rows","records":[{"值":1}]}')
    cfg = config(tmp_path, monkeypatch, dataspace=True)
    runtime = tmp_path / 'test_runtime'
    publish = True

    class Host:
        def __init__(self, settings, *, surfaces=None, revision=None):
            self.settings, self.revision = settings, revision

        async def ingest(self, message, *, history=None, session_id=None):
            if publish:
                await Workspace(self.settings).update(str(task / 'context'), surfaces=['kb', 'graph', 'db'], graph_sources=[])
            return {'answer': 'built'}

        async def query(self, message, *, final_contract=None):
            workspace = Workspace(self.settings)
            name = next(iter(workspace.manifest(self.revision)['tables']))
            async with workspace.reader(self.revision, enable={'db'}) as reader:
                export = await reader.call('db_export_sql', sql=f'SELECT "值" FROM "{name}"')
            return {'answer': json.dumps({'result_id': export['result_id']})}

        async def aclose(self):
            pass

    monkeypatch.setattr(host, 'CodexHost', Host)
    await run_task(cfg, dataset, 'task_1', runtime, phase='build')
    result = await run_task(cfg, dataset, 'task_1', runtime, phase='query')
    assert Path(result['prediction']).read_text().splitlines() == ['值', '1']
    publish = False
    with pytest.raises(ValueError, match='did not publish'):
        await run_task(cfg, dataset, 'task_1', runtime, phase='build')


@pytest.mark.asyncio
async def test_empty_json_retains_schema_card_and_source(tmp_path, monkeypatch):
    pytest.importorskip('ijson')
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'empty.json').write_text('{"table":"empty","records":[]}')
    w = Workspace(config(tmp_path, monkeypatch, dataspace=True))
    manifest = await w.update(str(root), surfaces=['kb', 'graph', 'db'], graph_sources=[])
    table = next(iter(manifest['tables'].values()))
    assert table['rows'] == 0 and table['column_mapping'] == []
    async with w.reader() as reader:
        assert 'table_schema' in str(await reader.call('kb_search', query='empty'))


@pytest.mark.asyncio
async def test_table_graph_and_catalog_survive_explicit_migration(tmp_path, monkeypatch):
    pytest.importorskip('lancedb')
    root = tmp_path / 'source'
    sources(root)
    cfg = config(tmp_path, monkeypatch, dataspace=True)
    old = await Workspace(cfg).update(str(root), surfaces=['kb', 'graph'], graph_sources=[])
    cfg.workspace.storage_mode = 'incremental'
    cfg.kb.vector_store = 'lancedb'
    w = Workspace(cfg)
    migrated = await w.migrate()
    assert migrated['tables'] == old['tables']
    for revision in [old['workspace_revision'], migrated['workspace_revision']]:
        async with w.reader(revision) as reader:
            schema = await reader.call('graph_schema')
            assert 'references' in str(schema)
