"""Native SQLite registration preserves schema, evidence and read boundaries."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from datamind.workspace import Workspace
from datamind.tests.test_dataspace_inputs import config


def fixture_database(root):
    root.mkdir(parents=True)
    path = root / 'source.sqlite'
    with sqlite3.connect(path) as con:
        con.executescript('''
            CREATE TABLE "公司" (id INTEGER PRIMARY KEY, "名称" TEXT UNIQUE NOT NULL COLLATE NOCASE);
            CREATE TABLE sales (company_id INTEGER REFERENCES "公司"(id), amount REAL CHECK(amount >= 0));
            CREATE INDEX sales_amount ON sales(amount);
            CREATE TABLE audit(message TEXT);
            CREATE TRIGGER sales_insert AFTER INSERT ON sales BEGIN INSERT INTO audit VALUES ('insert'); END;
            INSERT INTO "公司" VALUES (1, 'ALPHA');
            INSERT INTO sales VALUES (1, 12.5), (1, 7.5);
            CREATE VIEW totals AS SELECT company_id, SUM(amount) AS total FROM sales GROUP BY company_id;
            CREATE VIRTUAL TABLE notes USING fts5(body);
            INSERT INTO notes VALUES ('sqlite native search');
        ''')
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize('reference', [False, True])
async def test_native_schema_queries_and_source_ownership(tmp_path, monkeypatch, reference):
    source = fixture_database(tmp_path / 'source')
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    cfg = config(tmp_path, monkeypatch, dataspace=reference)
    cfg.db.row_limit = None
    workspace = Workspace(cfg)
    manifest = await workspace.update(str(source.parent), surfaces=['db', 'graph', 'kb'], graph_sources=[])
    entry = next(iter(manifest['databases'].values()))
    database_id = entry['database_id']
    assert entry['storage'] == ('reference' if reference else 'snapshot')
    assert (Path(entry['path']) == source) is reference
    assert not list((workspace.root / 'tables').rglob('*.csv'))
    objects = {(o['type'], o['name']) for o in entry['schema_objects']}
    assert {('view', 'totals'), ('index', 'sales_amount'), ('trigger', 'sales_insert')} <= objects
    async with workspace.reader() as reader:
        assert (await reader.call('db_list_tables'))['tables'] == []
        assert (await reader.call('db_list_databases'))['databases'][0]['database_id'] == database_id
        for sql, expected in [
            ('SELECT total FROM totals', [[20.0]]),
            ("SELECT id FROM 公司 WHERE 名称='alpha'", [[1]]),
            ("SELECT body FROM notes WHERE notes MATCH 'native'", [['sqlite native search']]),
            ('SELECT COUNT(*) FROM audit', [[2]]),
        ]:
            assert (await reader.call('db_query_sql', sql=sql, database_id=database_id))['rows'] == expected
        schema = await reader.call('db_describe_table', table='sales', database_id=database_id)
        assert schema['source_metadata']['foreign_keys'][0]['table'] == '公司'
        assert any(o['name'] == 'sales_amount' for o in schema['schema_objects'])
        assert 'references' in str(await reader.call('graph_schema'))
        assert database_id in str(await reader.call('kb_search', query='sales'))
        for sql in ['DELETE FROM sales', "ATTACH DATABASE '/tmp/other.sqlite' AS other", "SELECT load_extension('bad')"]:
            with pytest.raises(Exception):
                await reader.tools.get('db_query_sql').handler(sql=sql, database_id=database_id)
        with pytest.raises(Exception, match='Unknown database_id'):
            await reader.call('db_query_sql', sql='SELECT 1', database_id='other-task')
        result = await reader.call('db_export_sql', database_id=database_id,
            sql='WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1201) SELECT x FROM n')
        assert result['complete'] and result['row_count'] == 1201
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not Path(str(source) + '-wal').exists()
    second = await workspace.update(str(source.parent), surfaces=['db', 'graph', 'kb'], graph_sources=[])
    assert second['reused_sources'] == 1
    await workspace.collect(dry_run=False)
    assert source.exists()
    with sqlite3.connect(source) as con:
        con.execute('INSERT INTO sales VALUES (1, 1)')
    async with workspace.reader(manifest['workspace_revision']) as reader:
        if reference:
            # DataSpace deliberately trusts immutable inputs, without update detection.
            assert (await reader.call('db_query_sql', sql='SELECT total FROM totals', database_id=database_id))['rows'] == [[21.0]]
            await reader.call('graph_schema')
        else:
            assert (await reader.call('db_query_sql', sql='SELECT total FROM totals', database_id=database_id))['rows'] == [[20.0]]


@pytest.mark.asyncio
async def test_reference_does_not_copy_and_cross_file_join(tmp_path, monkeypatch):
    source = fixture_database(tmp_path / 'source')
    other = source.with_name('second.sqlite')
    with sqlite3.connect(other) as con:
        con.executescript('CREATE TABLE labels(id INTEGER, label TEXT); INSERT INTO labels VALUES(1,"matched");')
    (source.parent / 'csv.csv').write_text('id,note\n1,csv-value\n')
    from datamind.capabilities.db import native_sqlite
    prepare = native_sqlite.prepare_source

    def references_only(original, target, item, **kwargs):
        assert kwargs['reference'] is True
        assert not target.exists()
        return prepare(original, target, item, **kwargs)

    monkeypatch.setattr(native_sqlite, 'prepare_source', references_only)
    w = Workspace(config(tmp_path, monkeypatch, dataspace=True))
    m = await w.update(str(source.parent), surfaces=['db'])
    ids = {Path(e['source_uri']).name: key for key, e in m['databases'].items()}
    table = next(k for k, t in m['tables'].items() if t['sheet'] == 'csv')
    assert all(s['snapshot'] == s['uri'] for s in m['sources'] if s['uri'].endswith('.sqlite'))
    assert not list((w.root / 'revisions').rglob('*.sqlite'))
    async with w.reader() as reader:
        sql = f'SELECT c.note, t.total, l.label FROM "{table}" c JOIN "{ids["source.sqlite"]}".totals t ON c.id=t.company_id JOIN "{ids["second.sqlite"]}".labels l ON t.company_id=l.id'
        result = await reader.call('db_query_sql', sql=sql, database_ids=list(ids.values()))
        assert result['rows'] == [['csv-value', 20.0, 'matched']]


@pytest.mark.asyncio
async def test_committed_wal_is_in_snapshot_and_source_can_be_removed(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    root.mkdir()
    path = root / 'live.sqlite'
    con = sqlite3.connect(path)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('CREATE TABLE t(x); INSERT INTO t VALUES(42);')
        assert Path(str(path) + '-wal').stat().st_size
        w = Workspace(config(tmp_path, monkeypatch))
        m = await w.update(str(root), surfaces=['db'])
    finally:
        con.close()
    path.unlink()
    async with w.reader() as reader:
        assert (await reader.call('db_query_sql', sql='SELECT * FROM t', database_id=next(iter(m['databases']))))['rows'] == [[42]]


@pytest.mark.asyncio
@pytest.mark.parametrize('reference', [False, True])
async def test_direct_import_registers_native_database(tmp_path, monkeypatch, reference):
    from datamind.data_plane import build_data_plane
    path = fixture_database(tmp_path / 'source')
    cfg = config(tmp_path, monkeypatch, dataspace=reference)
    cfg.hooks.path_allowlist_extra = [str(path.parent)]
    system = await build_data_plane(cfg, enable={'db'})
    try:
        result = await system.services.ingest.db_import_path(path=str(path), if_exists='replace')
        assert result['database_id']
        assert result['storage'] == ('reference' if reference else 'snapshot')
        assert await system.services.db.list_tables() == []
        tool = system.tools.get('db_query_sql')
        assert (await tool.handler(sql='SELECT total FROM totals', database_id=result['database_id']))['rows'] == [[20.0]]
        removed = await system.services.ingest.db_delete_path(path=str(path))
        assert removed['status'] == 'unregistered' and path.is_file()
        with pytest.raises(Exception, match='Unknown database_id'):
            await tool.handler(sql='SELECT 1', database_id=result['database_id'])
    finally:
        await system.aclose()


@pytest.mark.asyncio
async def test_native_snapshot_migration_uses_frozen_database(tmp_path, monkeypatch):
    pytest.importorskip('lancedb')
    path = fixture_database(tmp_path / 'source')
    cfg = config(tmp_path, monkeypatch)
    w = Workspace(cfg)
    old = await w.update(str(path.parent), surfaces=['kb', 'graph'], graph_sources=[])
    path.unlink()
    cfg.workspace.storage_mode = 'incremental'
    cfg.kb.vector_store = 'lancedb'
    w = Workspace(cfg)
    migrated = await w.migrate()
    assert migrated['tables'] == old['tables']
    entry = next(iter(migrated['databases'].values()))
    assert entry['storage'] == 'snapshot' and Path(entry['path']).is_file()
    async with w.reader() as reader:
        assert 'references' in str(await reader.call('graph_schema'))


@pytest.mark.asyncio
@pytest.mark.parametrize('incremental', [False, True])
async def test_dataspace_hashes_source_once_and_never_validates_reads(tmp_path, monkeypatch, incremental):
    from datamind.capabilities.db import native_sqlite as native
    source = fixture_database(tmp_path / 'source')
    cfg = config(tmp_path, monkeypatch, dataspace=True, incremental=incremental)
    workspace = Workspace(cfg)
    calls = []
    original = native.sha_file

    def counted(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(native, 'sha_file', counted)
    surfaces = ['graph'] if incremental else ['db', 'graph']
    result = await workspace.update(str(source.parent), surfaces=surfaces, graph_sources=[])
    assert calls == [str(source)]
    database_id = next(iter(result['databases']))

    def forbidden(*args, **kwargs):
        raise AssertionError('Immutable DataSpace reads must not validate sources')

    monkeypatch.setattr(native, 'validate_entry', forbidden)
    monkeypatch.setattr(native, 'sha_file', forbidden)
    # Published policy, not the caller's current defaults, controls pinned reads.
    cfg.workspace.input_policy = 'standard'
    async with workspace.reader() as reader:
        for _ in range(2):
            await reader.call('graph_schema')
            if not incremental:
                await reader.call('db_list_tables')
                await reader.call('db_describe_table', table='sales', database_id=database_id)
                assert (await reader.call('db_query_sql', sql='SELECT total FROM totals', database_id=database_id))['rows'] == [[20.0]]
        if not incremental:
            assert (await reader.call('db_export_sql', sql='SELECT * FROM sales', database_id=database_id))['complete']


@pytest.mark.asyncio
async def test_standard_checks_reuse_hash_and_detect_changed_snapshot(tmp_path, monkeypatch):
    from datamind.capabilities.db import native_sqlite as native
    source = fixture_database(tmp_path / 'source')
    workspace = Workspace(config(tmp_path, monkeypatch))
    result = await workspace.update(str(source.parent), surfaces=['db'])
    entry = next(iter(result['databases'].values()))
    snapshot = Path(entry['path'])
    native._validate_file.cache_clear()
    calls = []
    original = native.sha_file

    def counted(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(native, 'sha_file', counted)
    async with workspace.reader() as reader:
        for _ in range(2):
            await reader.call('db_query_sql', sql='SELECT total FROM totals', database_id=entry['database_id'])
        assert calls == [str(snapshot)]
        stat = snapshot.stat()
        with sqlite3.connect(snapshot) as con:
            con.execute('UPDATE sales SET amount=13.5 WHERE amount=12.5')
        # Same size and restored mtime must not conceal a changed file.
        os.utime(snapshot, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert snapshot.stat().st_size == stat.st_size
        with pytest.raises(Exception, match='changed'):
            await reader.call('db_query_sql', sql='SELECT total FROM totals', database_id=entry['database_id'])
        assert len(calls) >= 2


def test_standard_validation_cache_invalidates_sidecars_and_replacement(tmp_path):
    from datamind.capabilities.db import native_sqlite as native
    path = fixture_database(tmp_path / 'source')
    entry = {'path': str(path), 'sha256': native.sha_file(path)}
    native.validate_entry(entry)
    wal = Path(str(path) + '-wal')
    wal.write_bytes(b'pending')
    with pytest.raises(Exception, match='WAL'):
        native.validate_entry(entry)
    wal.unlink()
    replacement = path.with_suffix('.replacement')
    replacement.write_bytes(path.read_bytes() + b'changed')
    replacement.replace(path)
    with pytest.raises(Exception, match='changed'):
        native.validate_entry(entry)
