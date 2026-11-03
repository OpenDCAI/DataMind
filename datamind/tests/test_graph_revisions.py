"""Evidence and complete-document replacement, including failed extractions."""
from types import SimpleNamespace

import pytest

from datamind.capabilities.graph.providers.networkx_store import NetworkXGraphStore
from datamind.capabilities.ingest.service import IngestService


class Model:
    output = '[{"subject":"Alice","relation":"owns","object":"Project"}]'

    async def generate_text(self, *args, **kwargs):
        return self.output


def setup(tmp_path, chunk_size=512):
    model = Model()
    store = NetworkXGraphStore(persist_path=tmp_path / 'graph.json')
    svc = IngestService(kb=None, db=None, graph=SimpleNamespace(store=store),
        llm_client=model, llm_model='test', profile_data_dir=tmp_path / 'profile',
        chunk_size=chunk_size, chunk_overlap=0)
    return svc, store, model


@pytest.mark.asyncio
async def test_evidence_survives_reload_and_other_document_deletion(tmp_path):
    svc, store, _ = setup(tmp_path)
    docs = tmp_path / 'docs'
    docs.mkdir()
    for name in ('a.md', 'b.md'):
        (docs / name).write_text('Alice owns Project')
    await svc.graph_add_path(path=str(docs))
    edges = await store.neighbors('Alice')
    assert len(edges) == 2
    assert all(e.evidence[0].source_version.startswith('sha256:') for e in edges)
    assert len({e.evidence[0].source_id for e in edges}) == 2
    (docs / 'a.md').unlink()
    await svc.graph_add_path(path=str(docs))
    reloaded = NetworkXGraphStore(persist_path=tmp_path / 'graph.json')
    edges = await reloaded.neighbors('Alice')
    assert len(edges) == 1
    assert edges[0].evidence[0].locator['uri'] == str(docs / 'b.md')


@pytest.mark.asyncio
async def test_shrink_and_empty_retract_all_document_parts(tmp_path):
    svc, store, _ = setup(tmp_path, chunk_size=24)
    doc = tmp_path / 'a.md'
    doc.write_text('Alice owns Project\n\nBob owns Another\n\nCarol owns Third')
    await svc.graph_add_path(path=str(doc))
    assert store.stats()['edges'] >= 2
    doc.write_text('Alice owns Project')
    await svc.graph_add_path(path=str(doc))
    assert store.stats()['edges'] == 1
    doc.write_text('')
    await svc.graph_add_path(path=str(doc))
    assert store.stats()['edges'] == 0
    assert NetworkXGraphStore(persist_path=tmp_path / 'graph.json').stats()['edges'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('output', ['not json', '{}', '[{}]', '[{"subject":"","relation":"owns","object":"P"}]'])
async def test_bad_extraction_keeps_last_successful_revision(tmp_path, output):
    svc, store, model = setup(tmp_path)
    doc = tmp_path / 'a.md'
    doc.write_text('Alice owns Project')
    await svc.graph_add_path(path=str(doc))
    before = [e.model_dump() for e in await store.neighbors('Alice')]
    model.output = output
    doc.write_text('New revision')
    result = await svc.graph_add_path(path=str(doc))
    assert result['skipped_count'] == 1
    assert [e.model_dump() for e in await store.neighbors('Alice')] == before


@pytest.mark.asyncio
async def test_later_part_failure_does_not_publish_earlier_parts(tmp_path):
    svc, store, model = setup(tmp_path, chunk_size=24)
    doc = tmp_path / 'a.md'
    doc.write_text('Alice owns Project')
    await svc.graph_add_path(path=str(doc))
    before = [e.model_dump() for e in await store.neighbors('Alice')]
    calls = 0
    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        return model.output if calls == 1 else 'broken'
    model.generate_text = fail_second
    doc.write_text('First piece of content\n\nSecond piece of content')
    result = await svc.graph_add_path(path=str(doc))
    assert result['skipped_count'] == 1
    assert [e.model_dump() for e in await store.neighbors('Alice')] == before


@pytest.mark.asyncio
async def test_relation_schema_is_enforced(tmp_path):
    svc, store, _ = setup(tmp_path)
    doc = tmp_path / 'a.md'
    doc.write_text('Alice owns Project')
    result = await svc.graph_add_path(path=str(doc), relation_types=['depends_on'])
    assert result['skipped_count'] == 1
    assert store.stats()['edges'] == 0


@pytest.mark.asyncio
async def test_inline_graph_input_remains_available_as_immutable_evidence(tmp_path):
    svc, store, _ = setup(tmp_path)
    await svc.graph_add_triples_from_text(text='Alice owns Project')
    edge = (await store.neighbors('Alice'))[0]
    from pathlib import Path
    assert Path(edge.evidence[0].locator['uri']).read_text() == 'Alice owns Project'
    assert edge.evidence[0].source_id.startswith('text:')


@pytest.mark.asyncio
async def test_explicit_assertions_receive_persistent_input_provenance(tmp_path):
    from pathlib import Path
    from datamind.capabilities.graph.service import GraphService
    _, store, _ = setup(tmp_path)
    service = GraphService(store=store, data_dir=tmp_path/'profile', storage_dir=tmp_path)
    await service.upsert([{'subject':'Alice','relation':'owns','object':'Project'}])
    evidence = (await store.neighbors('Alice'))[0].evidence[0]
    assert evidence.method == 'explicit'
    assert 'Alice' in Path(evidence.locator['uri']).read_text()
