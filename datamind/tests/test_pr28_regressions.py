"""Regression coverage for version-provider integration and restart/concurrency."""
import asyncio

import pytest

from datamind.capabilities.db.providers.dolt import DoltDialect
from datamind.core.errors import CapabilityError
from datamind.core.registry import db_registry, vector_store_registry


def test_optional_providers_register_without_constructing_them():
    assert 'dolt' in db_registry
    assert 'lancedb' in vector_store_registry


def test_dolt_versions_all_physical_join_tables_but_not_cte_names_or_literals():
    sql = "WITH c AS (SELECT * FROM db.a) SELECT 'FROM b' AS note FROM c JOIN b x ON c.id=x.id"
    rewritten = DoltDialect()._inject_as_of(sql, 'a' * 32)
    assert rewritten.count(' AS OF ') == 2
    assert "'FROM b'" in rewritten
    assert "FROM c AS OF" not in rewritten
    assert "JOIN b AS OF" in rewritten


@pytest.mark.parametrize('sql', ['DELETE FROM t', 'SELECT * FROM a; SELECT * FROM b'])
def test_dolt_historical_queries_reject_writes_and_multiple_statements(sql):
    with pytest.raises(CapabilityError):
        DoltDialect()._inject_as_of(sql, 'a' * 32)


@pytest.mark.asyncio
async def test_lancedb_history_persists_and_concurrent_reads_do_not_checkout_writer(tmp_path):
    pytest.importorskip('lancedb')
    from datamind.capabilities.kb.providers.lancedb_store import LanceDBVectorStore
    def open_store():
        return LanceDBVectorStore(persist_dir=tmp_path, collection_name='kb', dimension=4)
    store = open_store()
    await store.add(['one'], ['old'], [[1.,0.,0.,0.]], [{'source':'a.md'}])
    v1 = store.latest_version
    await store.add_version(v1)
    await store.add(['one'], ['new'], [[1.,0.,0.,0.]], [{'source':'a.md'}])
    v2 = store.latest_version
    await store.add_version(v2, previous_version=v1)
    assert await store.count() == 1  # Upsert, not append.
    reopened = open_store()
    assert [v['version'] for v in await reopened.list_versions()] == [v1, v2]
    old, current = await asyncio.gather(store.get_all_texts(version=v1), store.get_all_texts())
    assert old[0][1] == 'old'
    assert current[0][1] == 'new'
    assert store.latest_version == v2


@pytest.mark.asyncio
async def test_excel_update_clears_empty_and_removed_sheet_rows(tmp_path):
    import openpyxl
    from datamind.capabilities.db.providers.sqlite import SQLiteDialect
    from datamind.capabilities.db.service import DBService
    from datamind.capabilities.ingest.service import IngestService
    from datamind.config import DBConfig
    dialect = SQLiteDialect()
    engine = dialect.build_engine(None, default_path=str(tmp_path / 'db.sqlite'))
    db = DBService(dialect=dialect, engine=engine, db_cfg=DBConfig())
    ingest = IngestService(kb=None, db=db, graph=None, llm_client=None, llm_model='test',
                          profile_data_dir=tmp_path/'profile', chunk_size=512, chunk_overlap=0)
    source = tmp_path/'book.xlsx'
    book = openpyxl.Workbook()
    book.active.title='one'
    book.active.append(['value'])
    book.active.append([1])
    sheet = book.create_sheet('two')
    sheet.append(['value'])
    sheet.append([2])
    book.save(source)
    await ingest.db_import_path(path=str(source))
    book.remove(sheet)
    book.active.delete_rows(2)
    book.save(source)
    await ingest.db_update_path(path=str(source))
    assert (await db.query_sql('SELECT COUNT(*) FROM book_one')).rows == [[0]]
    assert (await db.query_sql('SELECT COUNT(*) FROM book_two')).rows == [[0]]
    await db.aclose()
