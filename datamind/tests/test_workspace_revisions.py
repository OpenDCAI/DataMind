"""Pinned cross-surface reads, publication failure, and restart checkpoints."""
import asyncio
import json

import pytest

from datamind.config import Settings
from datamind.core.errors import CapabilityError
from datamind.workspace import Workspace


class Embed:
    name = 'test'
    dimension = 4
    calls = 0
    async def embed_texts(self, texts):
        self.calls += 1
        return [[1., 0., 0., 0.] for _ in texts]
    async def embed_query(self, text):
        return [1., 0., 0., 0.]


class Model:
    fail = False
    calls = 0
    async def generate_text(self, prompt, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError('injected extractor failure')
        subject = 'Bob' if 'Bob owns' in prompt else 'Alice'
        return json.dumps([{'subject': subject, 'relation': 'owns', 'object': 'Project'}])
    async def aclose(self):
        pass


@pytest.fixture
def setup_workspace(tmp_path, monkeypatch):
    from datamind.agent import options
    model, embedding = Model(), Embed()
    monkeypatch.setattr(options, 'build_model_client', lambda *a, **k: model)
    monkeypatch.setattr(options, 'build_embedding', lambda *a, **k: embedding)
    settings = Settings(llm={'api_key': 'test', 'model': 'test'},
                        data={'base_dir': tmp_path / 'runtime'}, embedding={'dimension': 4})
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'a.md').write_text('Alice owns Project')
    return Workspace(settings), source, model, embedding


@pytest.mark.asyncio
async def test_reader_keeps_old_kb_graph_and_raw_source_across_publication(setup_workspace):
    workspace, source, _, _ = setup_workspace
    first = await workspace.update(str(source))
    async with workspace.reader() as old:
        (source / 'a.md').write_text('Bob owns Project')
        second = await workspace.update(str(source))
        assert first['workspace_revision'] != second['workspace_revision']
        entities = await old.call('graph_search_entities', query='Alice')
        assert entities['entities'][0]['id'] == 'Alice'
        kb = await old.call('kb_search', query='owner')
        assert 'Alice owns' in str(kb) and 'Bob owns' not in str(kb)
        raw = await old.call('raw_file_read', path=str(source / 'a.md'))
        assert raw['text'] == 'Alice owns Project'
        with pytest.raises(CapabilityError, match='pinned'):
            await old.call('raw_file_read', path='/etc/hosts')
    async with workspace.reader() as new:
        assert (await new.call('graph_search_entities', query='Bob'))['entities'][0]['id'] == 'Bob'


@pytest.mark.asyncio
async def test_failed_build_keeps_head_and_reuses_completed_sources_on_restart(setup_workspace):
    workspace, source, model, _ = setup_workspace
    first = await workspace.update(str(source))
    (source / 'b.md').write_text('Bob owns Project')
    model.fail = True
    with pytest.raises(RuntimeError, match='injected'):
        await workspace.update(str(source))
    assert workspace.current_revision == first['workspace_revision']
    model.fail = False
    restarted = Workspace(workspace.settings)
    second = await restarted.update(str(source))
    assert second['reused_sources'] == 1
    assert len(second['sources']) == 2


@pytest.mark.asyncio
async def test_deletion_and_rollback_restore_complete_state(setup_workspace):
    workspace, source, _, _ = setup_workspace
    first = await workspace.update(str(source))
    (source / 'a.md').unlink()
    second = await workspace.update(str(source))
    async with workspace.reader() as reader:
        assert (await reader.call('graph_search_entities', query='Alice'))['entities'] == []
        assert 'Alice owns' not in str(await reader.call('kb_search', query='owner'))
    workspace.rollback(first['workspace_revision'], expected_revision=second['workspace_revision'])
    async with workspace.reader() as reader:
        assert 'Alice owns' in str(await reader.call('kb_search', query='owner'))


@pytest.mark.asyncio
async def test_concurrent_builds_cannot_overwrite_a_newer_publication(setup_workspace):
    workspace, source, model, _ = setup_workspace
    original = model.generate_text
    ready, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def barrier(*a, **k):
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
        await release.wait()
        return await original(*a, **k)
    model.generate_text = barrier
    builds = [asyncio.create_task(workspace.update(str(source), surfaces=['graph'])) for _ in range(2)]
    await asyncio.wait_for(ready.wait(), timeout=10)
    release.set()
    results = await asyncio.gather(*builds, return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, CapabilityError) for r in results) == 1


@pytest.mark.asyncio
async def test_unchanged_sources_reuse_embeddings_and_extraction(setup_workspace):
    workspace, source, model, embedding = setup_workspace
    await workspace.update(str(source))
    calls = model.calls, embedding.calls
    second = await workspace.update(str(source))
    assert second['reused_sources'] == 1
    assert (model.calls, embedding.calls) == calls
    assert second['metrics']['sources_reused'] == 1
    assert second['metrics']['sources_computed'] == 0
    assert second['metrics']['embedding_calls'] == second['metrics']['graph_extraction_calls'] == 0
    assert second['metrics']['materialization_ms'] > 0
    report = workspace._settings_for(second['workspace_revision']).data.base_dir / 'build_metrics.json'
    saved = json.loads(report.read_text())
    assert saved['total_ms'] > 0 and saved['status'] == 'published'
    assert saved['reported_embedding_tokens'] is None  # Fixture doesn't report usage.


@pytest.mark.asyncio
async def test_published_db_and_kb_share_same_input_revision(setup_workspace):
    workspace, source, _, _ = setup_workspace
    (source / 'orders.csv').write_text('item,amount\nA,12\n')
    first = await workspace.update(str(source))
    async with workspace.reader(first['workspace_revision']) as reader:
        tables = await reader.call('db_list_tables')
        assert 'ws_' in str(tables)
        table = (await reader._system.services.db.list_tables())[0]
        result = await reader.call('db_query_sql', sql=f'SELECT amount FROM "{table}"')
        assert '12' in str(result)


@pytest.mark.asyncio
async def test_transient_read_retry_stays_on_pinned_revision(setup_workspace):
    workspace, source, _, _ = setup_workspace
    first = await workspace.update(str(source), surfaces=['graph'])
    async with workspace.reader(read_retries=1) as reader:
        store = reader._system.services.graph.store
        original = store.neighbors
        calls = 0
        async def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                (source / 'a.md').write_text('Bob owns Project')
                await workspace.update(str(source), surfaces=['graph'])
                raise TimeoutError('transient graph outage')
            return await original(*args, **kwargs)
        store.neighbors = fail_once
        result = await reader.call('graph_neighbors', entity='Alice')
        assert calls == 2
        assert 'Alice' in str(result)
        assert reader.revision == first['workspace_revision']
        assert workspace.current_revision != reader.revision


@pytest.mark.asyncio
async def test_lexical_fallback_keeps_old_sources_after_new_publication(setup_workspace):
    workspace, source, _, embedding = setup_workspace
    workspace.settings.retrieval.strategy = 'hybrid'
    workspace.settings.retrieval.vector_failure_fallback = 'bm25'
    for i in range(5):
        (source / f'filler{i}.md').write_text(f'unrelated document number {i}')
    first = await workspace.update(str(source), surfaces=['kb'])
    async with workspace.reader(first['workspace_revision'], read_retries=1) as reader:
        (source/'a.md').write_text('Bob owns Project')
        await workspace.update(str(source), surfaces=['kb'])
        async def unavailable(query):
            raise TimeoutError('embedding unavailable')
        embedding.embed_query = unavailable
        result = await reader.call('kb_search', query='Alice')
        assert 'Alice owns' in str(result['results']) and 'Bob owns' not in str(result['results'])
        assert result['retrieval']['degraded'] is True
        assert result['retrieval']['retries'] == 1
        assert result['retrieval']['events'][0]['workspace_revision'] == first['workspace_revision']


@pytest.mark.asyncio
async def test_inspection_reads_pinned_sources_after_deletion(setup_workspace):
    workspace, source, _, _ = setup_workspace
    first = await workspace.update(str(source), surfaces=['graph'])
    async with workspace.reader() as reader:
        (source / 'a.md').unlink()
        result = await reader.call('workspace_inspect', path=str(source))
        assert result['files_scanned'] == 1
        assert result['files'][0]['sha256'] == first['sources'][0]['version']


@pytest.mark.asyncio
async def test_hard_process_exit_leaves_no_partial_publication_and_reuses_checkpoint(setup_workspace, tmp_path):
    import os
    import subprocess
    import sys
    workspace, source, _, _ = setup_workspace
    (source / 'b.md').write_text('Bob owns Project')
    script = tmp_path / 'crash_builder.py'
    script.write_text('''
import asyncio, os, sys
from datamind.agent import options
from datamind.config import Settings
from datamind.workspace import Workspace
class Model:
    calls = 0
    async def generate_text(self, *a, **k):
        self.calls += 1
        if self.calls == 2:
            os._exit(23)
        return '[{"subject":"Alice","relation":"owns","object":"Project"}]'
    async def aclose(self): pass
model = Model()
options.build_model_client = lambda *a, **k: model
settings = Settings(llm={'api_key':'test','model':'test'}, data={'base_dir':sys.argv[1]}, embedding={'dimension':4})
asyncio.run(Workspace(settings).update(sys.argv[2], surfaces=['graph']))
''')
    process = await asyncio.create_subprocess_exec(sys.executable, str(script),
        str(workspace.settings.data.base_dir), str(source),
        env={**os.environ, 'PYTHONPATH': str(__import__('pathlib').Path(__file__).resolve().parents[2])},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, errors = await asyncio.wait_for(process.communicate(), timeout=30)
    assert process.returncode == 23, errors.decode()
    assert workspace.current_revision is None
    result = await workspace.update(str(source), surfaces=['graph'])
    assert result['reused_sources'] == 1


@pytest.mark.asyncio
async def test_publish_after_intervening_publish_and_rollback_is_rejected(setup_workspace):
    workspace, source, model, _ = setup_workspace
    first = await workspace.update(str(source), surfaces=['graph'])
    (source/'b.md').write_text('Bob owns Project')
    original = model.generate_text
    ready, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def block_first(*a, **k):
        nonlocal calls
        calls += 1
        if calls == 1:
            ready.set()
            await release.wait()
        return await original(*a, **k)
    model.generate_text = block_first
    old_build = asyncio.create_task(workspace.update(str(source), surfaces=['graph']))
    await asyncio.wait_for(ready.wait(), 5)
    second = await workspace.update(str(source), surfaces=['graph'])
    workspace.rollback(first['workspace_revision'], expected_revision=second['workspace_revision'])
    release.set()
    with pytest.raises(CapabilityError, match='publication conflict'):
        await old_build
    assert workspace.current_revision == first['workspace_revision']
