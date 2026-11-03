import asyncio
import json
import threading

import pytest

from datamind.capabilities.graph.providers.networkx_store import NetworkXGraphStore
from datamind.core.protocols import GraphTriple


@pytest.mark.asyncio
async def test_cancelled_save_cannot_overwrite_a_later_successful_save(tmp_path):
    path = tmp_path / 'graph.json'
    store = NetworkXGraphStore(persist_path=path)
    await store.upsert_triples([GraphTriple(subject='A', relation='old', object='B')])
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_write = store._write_document
    count = 0

    def controlled_write(doc):
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            try:
                assert release.wait(10)
                original_write(doc)
            finally:
                finished.set()
        else:
            original_write(doc)

    store._write_document = controlled_write
    first = asyncio.create_task(store.persist())
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await store.upsert_triples([GraphTriple(subject='B', relation='new', object='C')])
        second = asyncio.create_task(store.persist())
        # Allow an incorrectly unlocked second writer to finish before the
        # first is released; a correctly serialized writer remains pending.
        await asyncio.wait({second}, timeout=0.2)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        if second is not None:
            await second
    # Cancellation of to_thread does not stop its worker. The old writer must
    # never replace the newer committed document after the async lock releases.
    assert len(json.loads(path.read_text())['edges']) == 2
    assert store._dirty is False
    await store.persist()
    assert len(json.loads(path.read_text())['edges']) == 2
