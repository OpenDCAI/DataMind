"""Deterministic persistence-race coverage for the NetworkX store."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from datamind.capabilities.graph.providers.networkx_store import NetworkXGraphStore
from datamind.core.protocols import GraphTriple


def triple(subject: str, relation: str, object_: str) -> GraphTriple:
    return GraphTriple(subject=subject, relation=relation, object=object_)


@pytest.mark.asyncio
async def test_mutation_during_save_stays_dirty_until_new_revision_is_written(tmp_path):
    path = tmp_path / "graph.json"
    store = NetworkXGraphStore(persist_path=path)
    await store.upsert_triples([triple("A", "old", "B")])

    started = threading.Event()
    release = threading.Event()
    original_write = store._write_document

    def blocked_write(document):
        started.set()
        assert release.wait(5)
        original_write(document)

    store._write_document = blocked_write
    first = asyncio.create_task(store.persist())
    await asyncio.to_thread(started.wait, 5)

    await store.upsert_triples([triple("B", "new", "C")])
    release.set()
    await first

    assert store._dirty is True
    store._write_document = original_write
    await store.persist()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert {edge["rel"] for edge in saved["edges"]} == {"old", "new"}
    assert NetworkXGraphStore(persist_path=path).stats()["edges"] == 2


@pytest.mark.asyncio
async def test_overlapping_persists_are_serialized_and_keep_the_latest_document(tmp_path):
    path = tmp_path / "graph.json"
    store = NetworkXGraphStore(persist_path=path)
    await store.upsert_triples([triple("A", "old", "B")])

    started = threading.Event()
    release = threading.Event()
    original_write = store._write_document
    writes = []

    def blocked_write(document):
        writes.append(document)
        if len(writes) == 1:
            started.set()
            assert release.wait(5)
        original_write(document)

    store._write_document = blocked_write
    first = asyncio.create_task(store.persist())
    await asyncio.to_thread(started.wait, 5)
    await store.upsert_triples([triple("B", "new", "C")])
    second = asyncio.create_task(store.persist())
    release.set()
    await asyncio.gather(first, second)

    assert len(writes) == 2
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert {edge["rel"] for edge in saved["edges"]} == {"old", "new"}
    assert store._dirty is False


@pytest.mark.asyncio
async def test_write_failure_keeps_store_dirty_for_retry(tmp_path):
    store = NetworkXGraphStore(persist_path=tmp_path / "graph.json")
    await store.upsert_triples([triple("A", "r", "B")])
    original_write = store._write_document

    def fail_write(document):
        raise OSError("disk full")

    store._write_document = fail_write
    with pytest.raises(OSError, match="disk full"):
        await store.persist()
    assert store._dirty is True

    store._write_document = original_write
    await store.persist()
    assert store._dirty is False
