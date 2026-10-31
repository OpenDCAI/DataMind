"""Integration tests for graph versioning backed by TerminusDB.

Data is deliberately written through ``IngestService``.  That is the public
path which both reconciles a document and registers the resulting TerminusDB
commit in DataMind's graph-version history.

Run these tests against a disposable TerminusDB instance with::

    DATAMIND_TEST_TERMINUSDB_DSN=http://127.0.0.1:6363 \
    DATAMIND_TEST_TERMINUSDB_PASSWORD=xxxxxxx pytest -q \
    datamind/tests/test_graph_versioning.py

The configured database is reset between tests, so the suite is opt-in.
"""
from __future__ import annotations

import inspect
import importlib.util
import os
import sqlite3
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from datamind.capabilities.graph.providers.terminusdb_store import (
    TerminusDBGraphStore,
)
from datamind.capabilities.graph.service import GraphService
from datamind.capabilities.ingest.service import IngestService


class _Model:
    """Deterministic extractor; the entity is selected from the input text."""

    async def generate_text(self, prompt: str, **kwargs) -> str:
        if "Bob" in prompt:
            return '[{"subject":"Bob","relation":"owns","object":"Project"}]'
        return '[{"subject":"Alice","relation":"owns","object":"Project"}]'


async def _maybe_await(value):
    """TerminusDB store methods are synchronous while services are async."""
    return await value if inspect.isawaitable(value) else value


@pytest.fixture(scope="session")
def terminusdb_config() -> tuple[str, str]:
    if importlib.util.find_spec("terminusdb_client") is None:
        pytest.skip("terminusdb_client is not installed")
    dsn = os.getenv("DATAMIND_TEST_TERMINUSDB_DSN")
    if not dsn:
        pytest.skip(
            "set DATAMIND_TEST_TERMINUSDB_DSN to run destructive TerminusDB tests"
        )
    password = os.getenv("DATAMIND_TEST_TERMINUSDB_PASSWORD")
    if not password:
        pytest.skip("set DATAMIND_TEST_TERMINUSDB_PASSWORD to run TerminusDB tests")
    return dsn, password


@pytest_asyncio.fixture
async def graph(terminusdb_config, tmp_path: Path):
    dsn, password = terminusdb_config
    try:
        store = TerminusDBGraphStore(
            dsn=dsn,
            password=password,
            persist_path=tmp_path / "terminusdb",
        )
    except Exception as exc:  # pragma: no cover - depends on local service
        pytest.skip(f"TerminusDB is unavailable: {type(exc).__name__}: {exc}")

    # The provider currently owns one fixed database (kg_default).  Requiring
    # an explicit test DSN above prevents accidental resets of normal setups.
    await _maybe_await(store.reset())
    service = GraphService(
        store=store,
        data_dir=tmp_path / "profile",
        storage_dir=tmp_path / "storage",
    )
    yield service
    await _maybe_await(service.aclose())


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    path = tmp_path / "profile"
    (path / "uploads").mkdir(parents=True)
    return path


@pytest.fixture
def ingest(graph: GraphService, profile: Path) -> IngestService:
    return IngestService(
        kb=None,
        db=None,
        graph=graph,
        llm_client=_Model(),
        llm_model="test",
        profile_data_dir=profile,
        chunk_size=512,
        chunk_overlap=64,
        ingest_mode="direct",
    )


def _write(profile: Path, text: str, name: str = "ownership.md") -> Path:
    path = profile / "uploads" / name
    path.write_text(text, encoding="utf-8")
    return path


async def _versions(graph: GraphService) -> list[dict]:
    return await _maybe_await(graph.store.list_versions())


async def _latest_version(graph: GraphService) -> str:
    versions = await _versions(graph)
    assert versions, "ingest did not register a graph version"
    version = versions[-1].get("version")
    assert isinstance(version, str) and version
    return version


async def test_list_versions_uses_ingest_order_not_commit_hash_order(tmp_path: Path):
    store = TerminusDBGraphStore.__new__(TerminusDBGraphStore)
    store._db = "kg_default"
    store._history_path = tmp_path / "version_history.sqlite3"
    with sqlite3.connect(store._history_path) as conn:
        conn.execute(
            "CREATE TABLE history ("
            "database TEXT, version TEXT, payload TEXT, "
            "PRIMARY KEY(database, version))"
        )
        conn.execute(
            "INSERT INTO history VALUES (?, ?, ?)",
            (store._db, "ffff", '{"step": 1}'),
        )
        conn.execute(
            "INSERT INTO history VALUES (?, ?, ?)",
            (store._db, "0000", '{"step": 2}'),
        )

    versions = await store.list_versions()

    assert [item["version"] for item in versions] == ["ffff", "0000"]
    assert versions[-1]["payload"] == {"step": 2}


@pytest.mark.asyncio
async def test_text_ingest_registers_a_graph_version(ingest, graph):
    before = len(await _versions(graph))

    result = await ingest.graph_add_triples_from_text(text="Alice owns Project")

    assert result["triples_added"] == 1
    versions = await _versions(graph)
    assert len(versions) == before + 1
    assert versions[-1]["payload"]["triples_added"] == 1
    assert versions[-1]["version"]


@pytest.mark.asyncio
async def test_path_ingest_records_added_and_deleted_triples(
    ingest, graph, profile,
):
    source = _write(profile, "Alice owns Project")
    first = await ingest.graph_add_path(path=str(source))
    assert first["triples_added"] == 1

    source.write_text("Bob owns Project", encoding="utf-8")
    second = await ingest.graph_add_path(path=str(source))

    assert second["triples_added"] == 1
    version = (await _versions(graph))[-1]
    assert version["payload"]["triples_added"] == 1
    assert version["payload"]["triples_deleted"] == 1


@pytest.mark.asyncio
async def test_graph_delete_path_removes_file_edges_and_registers_version(
    ingest, graph, profile,
):
    source = _write(profile, "Alice owns Project")
    await ingest.graph_add_path(path=str(source))
    count_before = len(await _versions(graph))

    result = await ingest.graph_delete_path(path=str(source))

    assert result["files_deleted"] == 1
    assert result["triples_deleted"] == 1
    assert result["skipped_count"] == 0
    assert not source.exists()
    assert await graph.neighbors("Alice") == []
    versions = await _versions(graph)
    assert len(versions) == count_before + 1
    assert versions[-1]["payload"]["triples_deleted"] == 1


@pytest.mark.asyncio
async def test_versioned_search_reads_the_ingest_snapshot(ingest, graph, profile):
    source = _write(profile, "Alice owns Project")
    await ingest.graph_add_path(path=str(source))
    alice_version = await _latest_version(graph)
    
    source.write_text("Bob owns Project", encoding="utf-8")
    await ingest.graph_add_path(path=str(source))

    current = await graph.search_entities("Bob", top_k=10)
    historical = await graph.search_entities_at_version(
        "Alice", top_k=10, version=alice_version
    )
    missing_historically = await graph.search_entities_at_version(
        "Bob", top_k=10, version=alice_version
    )

    assert {item["id"] for item in current} == {"Bob"}
    assert {item["id"] for item in historical} == {"Alice"}
    assert missing_historically == []


@pytest.mark.asyncio
async def test_neighbors_at_version_reads_the_ingest_snapshot(
    ingest, graph, profile,
):
    source = _write(profile, "Alice owns Project")
    await ingest.graph_add_path(path=str(source))
    alice_version = await _latest_version(graph)

    source.write_text("Bob owns Project", encoding="utf-8")
    await ingest.graph_add_path(path=str(source))

    historical = await graph.neighbors_at_version(
        "Alice", direction="out", version=alice_version
    )
    assert [(edge["source"], edge["relation"], edge["target"])
            for edge in historical] == [("Alice", "owns", "Project")]
    assert await graph.neighbors_at_version(
        "Bob", direction="out", version=alice_version
    ) == []


@pytest.mark.asyncio
async def test_traverse_at_version_reads_the_ingest_snapshot(
    ingest, graph, profile,
):
    source = _write(profile, "Alice owns Project")
    await ingest.graph_add_path(path=str(source))
    alice_version = await _latest_version(graph)

    source.write_text("Bob owns Project", encoding="utf-8")
    await ingest.graph_add_path(path=str(source))

    paths = await graph.traverse_at_version(
        "Alice", max_hops=1, version=alice_version
    )
    assert [path["nodes"] for path in paths] == [["Alice", "Project"]]
    assert await graph.traverse_at_version(
        "Bob", max_hops=1, version=alice_version
    ) == []


@pytest.mark.asyncio
async def test_rollback_restores_snapshot_and_appends_history(
    ingest, graph, profile,
):
    source = _write(profile, "Alice owns Project")
    await ingest.graph_add_path(path=str(source))
    alice_version = await _latest_version(graph)

    source.write_text("Bob owns Project", encoding="utf-8")
    await ingest.graph_add_path(path=str(source))
    count_before = len(await _versions(graph))

    result = await graph.rollback(alice_version)

    assert result["status"] == "ok"
    assert result["rollback_version"] == alice_version
    assert result["new_version"] != alice_version
    
    assert len(await _versions(graph)) == count_before + 1
    assert {item["id"] for item in await graph.search_entities("Alice")} == {
        "Alice"
    }
    assert await graph.search_entities("Bob") == []

