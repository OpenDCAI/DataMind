"""Video evidence lifecycle with real stores and deterministic provider doubles."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from datamind.config import Settings, VideoConfig
from datamind.core.errors import CapabilityError, ConfigError
from datamind.core.protocols import ModelResponse
from datamind.capabilities.ingest.video import bundle as media
from datamind.capabilities.ingest.video.frames import candidates
from datamind.capabilities.ingest.video.providers import describe_frame, parse_layout
from datamind.video_tools import mcp_content, video_tools
from datamind.workspace import Workspace


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jr1sAAAAASUVORK5CYII=')


class Embed:
    dimension = 4
    name = 'fixture'

    async def embed_texts(self, texts):
        return [[1., 0., 0., 0.] for _ in texts]

    async def embed_query(self, text):
        return [1., 0., 0., 0.]


@pytest.fixture
def video_workspace(tmp_path, monkeypatch):
    from datamind import runtime
    from datamind.core import model_clients

    monkeypatch.setattr(runtime, 'build_embedding', lambda *a, **kw: Embed())
    source = tmp_path / 'sources'
    source.mkdir()
    video = source / 'briefing.mp4'
    video.write_bytes(b'fixture video version one')
    weights = tmp_path / 'weights'
    weights.mkdir()
    cfg = Settings(_env_file=None, data={'base_dir': tmp_path / 'runtime'},
                   embedding={'dimension': 4}, hooks={'path_allowlist_extra': [str(source)]},
                   video={'vision': {'model': 'fixture', 'api_key': 'fixture'}, 'asr_model_dir': weights},
                   workspace={'retention': 'all', 'auto_gc': False})
    calls = {'ocr': 0, 'asr': 0, 'frames': 0, 'fail': False}

    class Client:
        async def aclose(self):
            pass

    monkeypatch.setattr(model_clients, 'build_model_client', lambda *a, **kw: Client())
    monkeypatch.setattr(media, 'probe', lambda *a: {'duration_s': 10., 'has_audio': True, 'codec': 'h264'})

    def extract(path, output, cfg, info):
        calls['frames'] += 1
        frames = []
        for i in range(2):
            image = f'frame-{i:04d}.png'
            (output / image).write_bytes(PNG)
            frames.append({'frame_id': f'frame-{i:04d}', 'image': image, 'start_s': float(i * 5),
                           'end_s': float((i + 1) * 5), 'capture_s': i * 5 + 0.2})
        return frames

    async def describe(client, path, cfg):
        calls['ocr'] += 1
        if calls['fail']:
            raise RuntimeError('injected vision failure')
        return {'ocr_text': 'Revenue 42 million', 'layout': ['page', ['value', '42 million']]}

    def asr(path, cfg):
        calls['asr'] += 1
        return {'status': 'complete', 'language': 'en', 'segments': [
            {'segment_id': 'speech-0000', 'text': ' Revenue is 42 million', 'start_s': 4., 'end_s': 7.,
             'words': [{'start_s': 4., 'end_s': 4.8, 'text': ' Revenue'},
                       {'start_s': 5.2, 'end_s': 7., 'text': ' is 42 million'}]}]}

    monkeypatch.setattr(media, 'extract_frames', extract)
    monkeypatch.setattr(media, 'describe_frame', describe)
    monkeypatch.setattr(media, 'transcribe', asr)
    return Workspace(cfg), source, video, calls


def assertions(workspace, source, frame_id='frame-0001'):
    return [{'uri': i['uri'], 'version': i['version'], 'triples': [
        {'subject': 'Revenue', 'relation': 'amount', 'object': '42 million',
         'properties': {'video_bundle': media.bundle_key(i, workspace.settings.video),
                        'video_evidence': [{'frame_id': frame_id}]}}]}
        for i in workspace._inventory(source)]


def test_packet_detection_and_explicit_limits():
    cfg = VideoConfig(guard_interval_s=100)
    packets = [(i * 0.1, 10, False) for i in range(100)]
    packets += [(2., 1000, True), (5., 1000, False), (5.1, 1000, False)]
    frames = candidates(packets, 10, cfg)
    assert len(frames) == 2
    assert frames[1]['start_s'] == 5
    assert frames[1]['target_s'] == pytest.approx(5.3)
    assert len(candidates([], 35, VideoConfig())) == 3
    with pytest.raises(CapabilityError, match='no frames were dropped'):
        candidates([], 35, VideoConfig(max_frames=1))


def test_layout_and_word_alignment():
    tree, text = parse_layout('["page",["table",["tr",["th","中文"],["td","42"]]]]')
    assert text == '中文\n42'
    with pytest.raises(ValueError):
        parse_layout('__import__("os")')
    frames = [{'frame_id': 'a', 'start_s': 0., 'end_s': 5.}, {'frame_id': 'b', 'start_s': 5., 'end_s': 10.}]
    segments = [{'segment_id': 's', 'start_s': 4., 'end_s': 6., 'text': 'Both', 'words': []}]
    media.align_speech(frames, segments)
    assert segments[0]['frame_ids'] == ['a', 'b']
    assert frames[0]['speech'][0]['alignment'] == 'segment_overlap'


@pytest.mark.asyncio
async def test_vision_input_reaches_openai_and_rejects_truncation(tmp_path):
    from datamind.core.model_clients import _openai_messages
    path = tmp_path / 'frame.png'
    path.write_bytes(PNG)
    cfg = VideoConfig(vision={'model': 'fixture'})

    class Client:
        truncated = False
        async def complete(self, **kwargs):
            message = _openai_messages(kwargs['messages'], None)[0]
            assert message['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
            return ModelResponse(content=[{'type': 'text', 'text': '["page",["text","42"]]'}],
                                 stop_reason='length' if self.truncated else 'stop')
    client = Client()
    assert (await describe_frame(client, path, cfg))['ocr_text'] == '42'
    client.truncated = True
    with pytest.raises(CapabilityError, match='truncated'):
        await describe_frame(client, path, cfg)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['snapshot', 'incremental'])
async def test_publish_reuse_pin_delete_and_gc(video_workspace, mode):
    w, source, video, calls = video_workspace
    if mode == 'incremental':
        pytest.importorskip('lancedb')
        w.settings.workspace.storage_mode = mode
        w.settings.kb.vector_store = 'lancedb'
    first = await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=assertions(w, source))
    bundle_id = first['sources'][0]['video_bundle']
    bundle = media.load_bundle(w.root, bundle_id)
    assert calls == {'ocr': 2, 'asr': 1, 'frames': 1, 'fail': False}
    assert bundle['frames'][0]['speech'][0]['text'] == ' Revenue'
    assert bundle['frames'][1]['speech'][0]['text'] == ' is 42 million'
    await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=assertions(w, source))
    assert calls['ocr'] == 2
    async with w.reader(first['workspace_revision']) as reader:
        hits = await reader.call('kb_search', query='Revenue')
        assert bundle_id in str(hits) and '42 million' in str(hits)
        edge = await reader.call('graph_neighbors', entity='Revenue')
        assert 'frame-0001' in str(edge) and bundle_id in str(edge)
        frame = await reader.call('kb_video_frame', bundle_id=bundle_id, frame_id='frame-0001')
        assert base64.b64decode(frame['image']['data']) == PNG
        wire = mcp_content({'result': frame})
        assert wire['content'][1]['type'] == 'image'
        assert 'image' not in wire['structuredContent']['result']
        assert base64.b64encode(PNG).decode() not in json.dumps(wire['structuredContent'])
        with pytest.raises(CapabilityError, match='pinned'):
            await reader.call('kb_video_read', bundle_id='0' * 64)
        video.unlink()
        await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=[])
        w.configure_retention('latest')
        os.utime(media.bundle_path(w.root, bundle_id), (0, 0))
        await w.collect(dry_run=False)
        assert media.bundle_path(w.root, bundle_id).exists()
        assert (await reader.call('kb_video_read', bundle_id=bundle_id, limit=1))['next_offset'] == 1
    async with w.reader() as reader:
        assert (await reader.call('graph_neighbors', entity='Revenue'))['count'] == 0
        with pytest.raises(CapabilityError, match='pinned'):
            await reader.call('kb_video_frame', bundle_id=bundle_id, frame_id='frame-0000')
    await w.collect(dry_run=False)
    assert not media.bundle_path(w.root, bundle_id).exists()


@pytest.mark.asyncio
async def test_failure_changed_config_and_bad_evidence_keep_head(video_workspace):
    w, source, video, calls = video_workspace
    first = await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=assertions(w, source))
    with pytest.raises(ConfigError, match='existing frame'):
        await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=assertions(w, source, 'missing'))
    assert w.current_revision == first['workspace_revision']
    video.write_bytes(b'version two')
    calls['fail'] = True
    with pytest.raises(ExceptionGroup, match='TaskGroup'):
        await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=assertions(w, source))
    assert w.current_revision == first['workspace_revision']
    assert len(list((w.root / 'kb_media').glob('*/bundle.json'))) == 1
    calls['fail'] = False
    second = await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=assertions(w, source))
    assert second['sources'][0]['video_bundle'] != first['sources'][0]['video_bundle']
    w.settings.video.cache_revision = 'new-weights'
    third = await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=assertions(w, source))
    assert third['sources'][0]['video_bundle'] != second['sources'][0]['video_bundle']
    stale = assertions(w, source)
    stale[0]['triples'][0]['properties']['video_bundle'] = first['sources'][0]['video_bundle']
    with pytest.raises(ConfigError, match='prepared properties.video_bundle'):
        await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=stale)
    assert w.current_revision == third['workspace_revision']
    w.rollback(first['workspace_revision'])
    async with w.reader() as reader:
        assert (await reader.call('kb_video_read', bundle_id=first['sources'][0]['video_bundle']))['source_version'] == first['sources'][0]['version']


@pytest.mark.asyncio
async def test_tool_paths_hash_and_corruption(video_workspace, tmp_path):
    w, source, video, calls = video_workspace
    tools = {s.name: s.handler for s in video_tools(w.settings)}
    result = await tools['video_prepare'](path=str(video))
    assert w.current_revision is None
    assert result['frames'] == 2
    with pytest.raises(CapabilityError):
        await tools['video_prepare'](path='/etc/passwd')
    with pytest.raises(CapabilityError, match='Invalid video bundle'):
        await tools['kb_video_read'](bundle_id='../other')
    image = media.bundle_path(w.root, result['bundle_id']) / 'frame-0000.png'
    image.write_bytes(b'corrupted')
    with pytest.raises(CapabilityError, match='integrity'):
        await tools['kb_video_frame'](bundle_id=result['bundle_id'], frame_id='frame-0000')


@pytest.mark.asyncio
@pytest.mark.parametrize('dataspace', [False, True])
async def test_video_reads_only_requested_image(video_workspace, monkeypatch, dataspace):
    w, source, video, _ = video_workspace
    w.settings.workspace.input_policy = 'dataspace' if dataspace else 'standard'
    published = await w.update(str(source), surfaces=['kb'])
    bundle_id = published['sources'][0]['video_bundle']
    root = media.bundle_path(w.root, bundle_id)
    (root / 'frame-0001.png').unlink()
    reads = []
    original = Path.read_bytes

    def counted(path):
        if path.suffix == '.png':
            reads.append(path.name)
        return original(path)

    def forbidden(*args, **kwargs):
        raise AssertionError('Reading evidence must not rescan all frame hashes')

    monkeypatch.setattr(Path, 'read_bytes', counted)
    monkeypatch.setattr(media, 'sha_file', forbidden)
    async with w.reader() as reader:
        await reader.call('kb_video_read', bundle_id=bundle_id)
        assert reads == []
        frame = await reader.call('kb_video_frame', bundle_id=bundle_id, frame_id='frame-0000')
        assert base64.b64decode(frame['image']['data']) == PNG
        assert reads == ['frame-0000.png']
        (root / 'frame-0000.png').write_bytes(PNG + b'changed')
        if dataspace:
            frame = await reader.call('kb_video_frame', bundle_id=bundle_id, frame_id='frame-0000')
            assert base64.b64decode(frame['image']['data']) == PNG + b'changed'
        else:
            with pytest.raises(CapabilityError, match='integrity'):
                await reader.call('kb_video_frame', bundle_id=bundle_id, frame_id='frame-0000')
        with pytest.raises(CapabilityError, match='pinned'):
            await reader.call('kb_video_read', bundle_id='0' * 64)
        (root / 'frame-0000.png').unlink()
        (root / 'frame-0000.png').symlink_to(video)
        with pytest.raises(CapabilityError, match='escapes'):
            await reader.call('kb_video_frame', bundle_id=bundle_id, frame_id='frame-0000')


@pytest.mark.asyncio
async def test_missing_models_and_no_audio(video_workspace, monkeypatch):
    w, source, video, calls = video_workspace
    w.settings.video.vision = None
    with pytest.raises(ConfigError, match='video.vision'):
        await w.update(str(source), surfaces=['kb'])
    w.settings.video.vision = Settings(_env_file=None).llm
    w.settings.video.asr_model_dir = None
    with pytest.raises(ConfigError, match='asr_model_dir'):
        await w.update(str(source), surfaces=['kb'])
    monkeypatch.setattr(media, 'probe', lambda *a: {'duration_s': 10., 'has_audio': False, 'codec': 'h264'})
    published = await w.update(str(source), surfaces=['kb'])
    bundle = media.load_bundle(w.root, published['sources'][0]['video_bundle'])
    assert bundle['asr']['status'] == 'no_audio'
    assert calls['asr'] == 0


@pytest.mark.asyncio
async def test_snapshot_migration_preserves_media_and_semantic_locators(video_workspace):
    pytest.importorskip('lancedb')
    w, source, _, calls = video_workspace
    first = await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=assertions(w, source))
    w.settings.kb.vector_store = 'lancedb'
    migrated = await w.migrate()
    assert migrated['format_version'] == 2
    assert migrated['sources'][0]['video_bundle'] == first['sources'][0]['video_bundle']
    assert calls['ocr'] == 2
    async with w.reader() as reader:
        edges = await reader.call('graph_neighbors', entity='Revenue')
        assert 'frame-0001' in str(edges)
        assert (await reader.call('kb_video_frame', bundle_id=migrated['sources'][0]['video_bundle'],
                                  frame_id='frame-0001'))['image']['mimeType'] == 'image/png'


@pytest.mark.asyncio
async def test_mcp_image_wire_and_read_only_preparation(video_workspace, monkeypatch):
    from datamind import mcp
    w, source, _, _ = video_workspace
    first = await w.update(str(source), surfaces=['kb', 'graph'], graph_sources=assertions(w, source))
    monkeypatch.setattr(mcp, 'load_settings', lambda *a: w.settings)
    operation = await mcp.execute('datamind_operation_begin', {'mode': 'read'})
    args = {'operation_id': operation['operation_id'], 'bundle_id': first['sources'][0]['video_bundle'], 'frame_id': 'frame-0000'}
    result = await mcp.execute('datamind_kb_video_frame', args)
    assert mcp_content(result)['content'][1]['data'] == base64.b64encode(PNG).decode()
    with pytest.raises(PermissionError, match='reads only'):
        await mcp.execute('datamind_video_prepare', {'operation_id': operation['operation_id'], 'path': str(source / 'briefing.mp4')})
    await mcp.execute('datamind_operation_close', {'operation_id': operation['operation_id']})
