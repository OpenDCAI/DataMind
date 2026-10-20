"""Procedural guidance is usable by writers without granting business reads."""
from pathlib import Path

import pytest

from datamind.capabilities.ingest.ledger import IngestLedger, with_receipts
from datamind.capabilities.skills.service import SkillsService
from datamind.capabilities.skills.tools import build_skills_tools
from datamind.config import Settings
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from datamind.core.tools import ToolRegistry, ToolSpec


@pytest.mark.asyncio
async def test_skill_lookup_is_lazy_versioned_and_captured_in_persisted_receipt(tmp_path):
    skill = tmp_path / 'skills' / 'workspace-ingest' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: workspace-ingest\nversion: 2.0.0\ndescription: Ingest workspace\n---\nInspect then import.')
    skills = SkillsService(skills_dir=skill.parent.parent, embedding=None, vector_store=None)
    get = next(t for t in build_skills_tools(skills) if t.name == 'skill_get')
    raw = ToolRegistry()
    async def save(text):
        return {'chunks_added':1}
    raw.add(ToolSpec(name='kb_add_text',description='save',input_schema={},handler=save,
                     metadata={'surface':'kb','access':'write'}))
    wrapped = with_receipts(raw, IngestLedger(storage_dir=tmp_path / 'ledger',profile='test'))
    with bind_context(RequestContext.new(profile='test')):
        loaded = await get.handler(name='workspace-ingest')
        receipt = await wrapped.get('kb_add_text').handler(text='data')
    assert loaded['version'] == '2.0.0'
    assert receipt['applied_skills'] == [{
        'name':'workspace-ingest','version':'2.0.0','content_hash':loaded['content_hash']}]
    assert loaded['content_hash'] in (tmp_path/'ledger'/'ingest_receipts.jsonl').read_text()
    with bind_context(RequestContext.new(profile='test')):
        another = await wrapped.get('kb_add_text').handler(text='different')
    assert another['applied_skills'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('backend',['native','sdk'])
async def test_writer_only_receives_explicit_guidance_reads(tmp_path,monkeypatch,backend):
    from datamind.agent import options
    from types import SimpleNamespace
    monkeypatch.setattr(options,'build_model_client',lambda *a,**k: SimpleNamespace())
    monkeypatch.setattr(options,'_build_loop',lambda **k: SimpleNamespace())
    settings = Settings(llm={'api_key':'test'},data={'base_dir':tmp_path},agent={'backend':backend})
    system = await options.build_datamind(settings,enable={'graph'})
    assert 'raw_file_read' in system.store.tools
    assert 'workspace_inspect' in system.store.tools
    assert 'workspace_update' in system.store.tools
    assert 'graph_neighbors' not in system.store.tools
    assert 'graph_traverse' not in system.store.tools
    assert 'workspace_update' not in system.retrieve.tools
    assert 'graph_upsert_triples' not in system.retrieve.tools
    await system.aclose()


def test_bundled_ingestion_skills_match_checkout_catalog():
    root=Path(__file__).resolve().parents[2]
    for name in ('workspace-ingest','source-update'):
        assert (root/'.claude'/'skills'/name/'SKILL.md').read_text() == (root/'datamind'/'skills'/name/'SKILL.md').read_text()


@pytest.mark.asyncio
async def test_partial_ingest_is_failed_receipt_and_can_be_retried(tmp_path):
    calls=0
    async def partial():
        nonlocal calls
        calls+=1
        return {'skipped_count':1,'files_processed':1}
    raw=ToolRegistry()
    raw.add(ToolSpec(name='kb_add_path',description='ingest',input_schema={},handler=partial,
                    metadata={'surface':'kb','access':'write'}))
    wrapped=with_receipts(raw,IngestLedger(storage_dir=tmp_path,profile='test'))
    for _ in range(2):
        receipt=await wrapped.get('kb_add_path').handler()
        assert receipt['results'][0]['status']=='failed'
    assert calls==2


@pytest.mark.asyncio
async def test_independent_ledgers_allocate_unique_revisions(tmp_path):
    import asyncio
    first = IngestLedger(storage_dir=tmp_path, profile='test')
    second = IngestLedger(storage_dir=tmp_path, profile='test')
    ready, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def save(text):
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
        await release.wait()
        return {'chunks_added': 1}
    raw = ToolRegistry()
    raw.add(ToolSpec(name='kb_add_text',description='save',input_schema={},handler=save,
                    metadata={'surface':'kb','access':'write'}))
    registries = [with_receipts(raw, ledger) for ledger in [first,second]]
    jobs = [asyncio.create_task(registry.get('kb_add_text').handler(text=str(i))) for i,registry in enumerate(registries)]
    await asyncio.wait_for(ready.wait(), 5)
    release.set()
    receipts = await asyncio.gather(*jobs)
    assert sorted(r['revision'] for r in receipts) == [1,2]
    assert first.revision == second.revision == 2
