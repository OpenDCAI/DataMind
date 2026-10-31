"""Workspace dirty tracking and strict latest-read consistency checks."""

import hashlib
import importlib.util
import os
import sqlite3

import httpx
import pytest

from datamind.config import Settings
from datamind.core.errors import CapabilityError, ConfigError
from datamind.capabilities.ingest.lineage import LineageFile, build_lineage_triples
from datamind.workspace import Workspace


class Model:
    """Minimal deterministic graph extractor used by workspace tests."""

    async def generate_text(self, prompt, **kwargs):
        # The production extractor only needs a JSON array from the client;
        # avoid network/model setup in tests.
        return '[{"subject":"Alice","relation":"owns","object":"Project"}]'

    async def aclose(self):
        pass


def graph_source(path):
    return [{
        "uri": str(path.resolve()),
        "version": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "triples": [{"subject": "Alice", "relation": "owns", "object": "Project"}],
    }]


def dirty(workspace):
    with workspace._connect() as db:
        return bool(db.execute("SELECT dirty FROM head WHERE singleton=1").fetchone()[0])


def detached(workspace):
    with workspace._connect() as db:
        return bool(db.execute("SELECT detached FROM head WHERE singleton=1").fetchone()[0])


async def reset_terminusdb(settings, path):
    from datamind.capabilities.graph.providers.terminusdb_store import TerminusDBGraphStore

    store = TerminusDBGraphStore(
        dsn=settings.graph.dsn,
        password=settings.graph.password,
        persist_path=path,
    )
    try:
        await store.reset()
    finally:
        await store.aclose()


def require_terminusdb():
    """Return test credentials without triggering provider auto-installation."""
    if importlib.util.find_spec("terminusdb_client") is None:
        pytest.skip("terminusdb_client is not installed")
    dsn = os.getenv("DATAMIND_TEST_TERMINUSDB_DSN")
    password = os.getenv("DATAMIND_TEST_TERMINUSDB_PASSWORD")
    if not dsn or not password:
        pytest.skip("TerminusDB integration test credentials are not configured")
    try:
        response = httpx.get(f"{dsn.rstrip('/')}/api/ok", timeout=2.0)
    except httpx.HTTPError as exc:
        pytest.skip(f"TerminusDB is not already running: {type(exc).__name__}")
    if not response.is_success:
        pytest.skip(f"TerminusDB health check returned HTTP {response.status_code}")
    return dsn, password


@pytest.fixture
def snapshot_workspace(tmp_path):
    settings = Settings(
        _env_file=None,
        llm={"api_key": "test", "model": "test"},
        data={"base_dir": tmp_path / "runtime"},
        workspace={"storage_mode": "snapshot", "auto_gc": False},
    )
    root = (tmp_path / "source").resolve()
    root.mkdir()
    document = root / "owner.md"
    document.write_text("Alice owns Project", encoding="utf-8")

    async def builder(cfg, enable):
        from datamind.data_plane import build_data_plane
        return await build_data_plane(cfg, enable=enable, text_client=Model())

    return Workspace(settings, builder=builder), root, document


@pytest.mark.asyncio
async def test_mark_dirty_only_for_paths_inside_published_root(snapshot_workspace, tmp_path):
    workspace, root, document = snapshot_workspace
    await workspace.update(str(root), surfaces=("graph",))
    assert dirty(workspace) is False
    
    assert workspace.mark_dirty(tmp_path / "outside.md") is False
    assert dirty(workspace) is False
    assert workspace.mark_dirty(document) is True
    assert dirty(workspace) is True


@pytest.mark.asyncio
async def test_latest_check_rejects_dirty_without_publishing(snapshot_workspace):
    workspace, root, document = snapshot_workspace
    published = await workspace.update(str(root), surfaces=("graph",))
    workspace.mark_dirty(document)

    with pytest.raises(CapabilityError, match="workspace_publish"):
        await workspace.assert_current_revision()

    assert workspace.current_revision == published["workspace_revision"]
    assert dirty(workspace) is True


@pytest.mark.asyncio
async def test_latest_check_detects_external_change_without_dirty_flag(snapshot_workspace):
    workspace, root, document = snapshot_workspace
    published = await workspace.update(str(root), surfaces=("graph",))
    document.write_text("Bob owns Project", encoding="utf-8")

    assert dirty(workspace) is False
    with pytest.raises(CapabilityError, match="source files changed"):
        await workspace.assert_current_revision()
    assert workspace.current_revision == published["workspace_revision"]


@pytest.mark.asyncio
async def test_latest_check_allows_clean_rollback_to_immutable_revision(snapshot_workspace):
    workspace, root, document = snapshot_workspace
    first = await workspace.update(str(root), surfaces=("graph",))
    document.write_text("Bob owns Project", encoding="utf-8")
    second = await workspace.update(str(root), surfaces=("graph",))

    workspace.rollback(
        first["workspace_revision"],
        expected_revision=second["workspace_revision"],
    )

    assert await workspace.assert_current_revision() == first["workspace_revision"]
    async with workspace.reader() as reader:
        result = await reader.call("graph_neighbors", entity="Project")
    assert "Alice" in str(result)
    assert "Bob" not in str(result)


@pytest.mark.asyncio
async def test_write_after_rollback_marks_detached_head_dirty(snapshot_workspace):
    workspace, root, document = snapshot_workspace
    first = await workspace.update(str(root), surfaces=("graph",))
    document.write_text("Bob owns Project", encoding="utf-8")
    second = await workspace.update(str(root), surfaces=("graph",))
    workspace.rollback(first["workspace_revision"], expected_revision=second["workspace_revision"])

    assert detached(workspace) is True
    assert dirty(workspace) is False
    assert workspace.mark_dirty(document) is True
    assert detached(workspace) is True
    assert dirty(workspace) is True
    with pytest.raises(CapabilityError, match="workspace_publish"):
        await workspace.assert_current_revision()


@pytest.mark.asyncio
async def test_publish_after_rollback_branches_from_target_and_clears_state(snapshot_workspace):
    workspace, root, document = snapshot_workspace
    first = await workspace.update(str(root), surfaces=("graph",))
    document.write_text("Bob owns Project", encoding="utf-8")
    second = await workspace.update(str(root), surfaces=("graph",))
    workspace.rollback(first["workspace_revision"], expected_revision=second["workspace_revision"])
    document.write_text("Carol owns Project", encoding="utf-8")
    workspace.mark_dirty(document)

    third = await workspace.update(str(root), surfaces=("graph",))

    assert third["parent_revision"] == first["workspace_revision"]
    assert workspace.current_revision == third["workspace_revision"]
    assert dirty(workspace) is False
    assert detached(workspace) is False
    async with workspace.reader(second["workspace_revision"]) as reader:
        assert "Bob" in (await reader.call("raw_file_read", path=str(document)))["text"]


@pytest.mark.asyncio
async def test_failed_publish_preserves_head_and_dirty(snapshot_workspace):
    workspace, root, document = snapshot_workspace
    first = await workspace.update(str(root), surfaces=("graph",))
    document.write_text("Bob owns Project", encoding="utf-8")
    workspace.mark_dirty(document)

    with pytest.raises(ConfigError, match="vocabulary"):
        await workspace.update(
            str(root),
            surfaces=("graph",),
            relation_types=["depends_on"],
            graph_sources=graph_source(document),
        )

    assert workspace.current_revision == first["workspace_revision"]
    assert dirty(workspace) is True


@pytest.mark.asyncio
async def test_snapshot_publish_clears_dirty(snapshot_workspace):
    workspace, root, document = snapshot_workspace
    first = await workspace.update(str(root), surfaces=("graph",))
    document.write_text("Bob owns Project", encoding="utf-8")
    workspace.mark_dirty(document)

    second = await workspace.update(str(root), surfaces=("graph",))

    assert second["workspace_revision"] != first["workspace_revision"]
    assert dirty(workspace) is False
    assert await workspace.assert_current_revision() == second["workspace_revision"]


@pytest.mark.asyncio
async def test_incremental_publish_clears_dirty(tmp_path):
    root = (tmp_path / "source").resolve()
    root.mkdir()
    document = root / "owner.md"
    document.write_text("Alice owns Project", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        data={"base_dir": tmp_path / "runtime"},
        workspace={"storage_mode": "incremental", "auto_gc": False},
        kb={"vector_store": "lancedb"},
        db={"dialect": "dolt"},
    )
    workspace = Workspace(settings)
    await workspace.update(
        str(root), surfaces=("graph",), graph_sources=graph_source(document),
    )
    document.write_text("Alice owns Project v2", encoding="utf-8")
    workspace.mark_dirty(document)

    result = await workspace.update(
        str(root), surfaces=("graph",), graph_sources=graph_source(document),
    )

    assert dirty(workspace) is False
    assert await workspace.assert_current_revision() == result["workspace_revision"]


def test_existing_catalog_migrates_dirty_column(tmp_path):
    settings = Settings(_env_file=None, data={"base_dir": tmp_path / "runtime"})
    root = (settings.data.storage_dir / "workspace").resolve()
    root.mkdir(parents=True)
    catalog = root / "catalog.sqlite3"
    with sqlite3.connect(catalog) as db:
        db.executescript("""
            CREATE TABLE revisions (id TEXT PRIMARY KEY, parent TEXT, state TEXT, error TEXT);
            CREATE TABLE head (singleton INTEGER PRIMARY KEY, revision TEXT, epoch INTEGER NOT NULL DEFAULT 0);
            INSERT INTO head (singleton, revision) VALUES (1, NULL);
        """)

    Workspace(settings)

    with sqlite3.connect(catalog) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(head)")}
        values = db.execute(
            "SELECT dirty, detached FROM head WHERE singleton=1"
        ).fetchone()
    assert "dirty" in columns
    assert "detached" in columns
    assert values == (0, 0)


@pytest.mark.asyncio
async def test_mcp_latest_read_rejects_dirty_workspace(snapshot_workspace, monkeypatch):
    pytest.importorskip("jsonschema")
    from datamind import mcp

    workspace, root, document = snapshot_workspace
    await workspace.update(str(root), surfaces=("graph",))
    workspace.mark_dirty(document)
    monkeypatch.setattr(mcp, "load_settings", lambda profile=None: workspace.settings)

    with pytest.raises(CapabilityError, match="workspace_publish"):
        await mcp.execute("datamind_operation_begin", {"mode": "read"})

    await workspace.update(str(root), surfaces=("graph",))
    document.write_text("Bob owns Project", encoding="utf-8")
    with pytest.raises(CapabilityError, match="workspace_publish"):
        await mcp.execute("datamind_operation_begin", {"mode": "read"})


@pytest.mark.asyncio
async def test_mcp_explicit_revision_reads_while_workspace_is_dirty(snapshot_workspace, monkeypatch):
    pytest.importorskip("jsonschema")
    from datamind import mcp

    workspace, root, document = snapshot_workspace
    published = await workspace.update(str(root), surfaces=("graph",))
    document.write_text("Bob owns Project", encoding="utf-8")
    workspace.mark_dirty(document)
    monkeypatch.setattr(mcp, "load_settings", lambda profile=None: workspace.settings)

    operation = await mcp.execute(
        "datamind_operation_begin",
        {"mode": "read", "revision": published["workspace_revision"]},
    )

    assert operation["revision"] == published["workspace_revision"]
    await mcp.execute(
        "datamind_operation_close",
        {"operation_id": operation["operation_id"]},
    )

@pytest.mark.asyncio
async def test_pinned_read_ignores_current_source_changes(snapshot_workspace):
    workspace, root, document = snapshot_workspace
    first = await workspace.update(str(root), surfaces=("graph",))
    document.write_text("Bob owns Project", encoding="utf-8")

    async with workspace.reader(first["workspace_revision"]) as reader:
        result = await reader.call("graph_neighbors", entity="Project")

    assert "Alice" in str(result)
    assert "Bob" not in str(result)


def test_lineage_evidence_covers_all_input_files(tmp_path):
    left = tmp_path / "left.csv"
    right = tmp_path / "right.csv"
    left.write_text("id,name\n1,Alice\n", encoding="utf-8")
    right.write_text("id,name\n2,Bob\n", encoding="utf-8")
    files = [
        LineageFile(
            uri=str(path),
            relative_path=path.name,
            version="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            content_path=path,
        )
        for path in (left, right)
    ]

    triples = build_lineage_triples(root=tmp_path, files=files)
    overlap = next(triple for triple in triples if triple.relation == "schema_overlap")

    assert {item.locator["uri"] for item in overlap.evidence} == {
        str(left), str(right)
    }
    assert all(item.method == "inferred" for item in overlap.evidence)
    assert all(item.pipeline_version == "lineage-v1" for item in overlap.evidence)


@pytest.mark.asyncio
async def test_v1_reader_includes_lineage(snapshot_workspace):
    workspace, root, document = snapshot_workspace
    related = root / "project.md"
    related.write_text("See owner.md", encoding="utf-8")
    revision = await workspace.update(str(root), surfaces=("graph",))

    async with workspace.reader(revision["workspace_revision"]) as reader:
        schema = await reader.call("graph_schema")

    assert schema["relations"]["contains"] == 2
    assert schema["relations"]["mentions"] == 1


@pytest.mark.asyncio
async def test_v2_reader_includes_lineage(tmp_path):
    root = (tmp_path / "source").resolve()
    root.mkdir()
    document = root / "owner.md"
    related = root / "project.md"
    document.write_text("Alice owns Project", encoding="utf-8")
    related.write_text("See owner.md", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        data={"base_dir": tmp_path / "runtime"},
        workspace={"storage_mode": "incremental", "auto_gc": False},
        kb={"vector_store": "lancedb"},
        db={"dialect": "dolt"},
    )
    workspace = Workspace(settings)
    assertions = [
        {
            "uri": str(path),
            "version": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            "triples": [],
        }
        for path in (document, related)
    ]
    revision = await workspace.update(str(root), surfaces=("graph",), graph_sources=assertions)

    async with workspace.reader(revision["workspace_revision"]) as reader:
        schema = await reader.call("graph_schema")

    assert schema["relations"]["contains"] == 2
    assert schema["relations"]["mentions"] == 1


@pytest.mark.asyncio
async def test_terminusdb_migration_requires_target_backend(tmp_path):
    """Provider conversion is explicit and only runs with TerminusDB configured."""
    dsn, password = require_terminusdb()
    root = (tmp_path / "source").resolve()
    root.mkdir()
    document = root / "owner.md"
    document.write_text("Alice owns Project", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        data={"base_dir": tmp_path / "runtime"},
        workspace={"storage_mode": "snapshot", "auto_gc": False},
        graph={"backend": "networkx"},
    )
    async def builder(cfg, enable):
        from datamind.data_plane import build_data_plane
        return await build_data_plane(cfg, enable=enable, text_client=Model())
    workspace = Workspace(settings, builder=builder)
    first = await workspace.update(str(root), surfaces=("graph",))
    settings.graph.backend = "terminusdb"
    settings.graph.dsn = dsn
    settings.graph.password = password
    await reset_terminusdb(settings, tmp_path / "terminusdb-reset")
    workspace.settings = settings
    migrated = await workspace.migrate()

    assert migrated["migrated_from"] == first["workspace_revision"]
    assert migrated["snapshots"]["graph"]["provider"] == "terminusdb"
    async with workspace.reader(first["workspace_revision"]) as before:
        before_schema = await before.call("graph_schema")
        before_neighbors = await before.call("graph_neighbors", entity="Project")
    async with workspace.reader(migrated["workspace_revision"]) as after:
        after_schema = await after.call("graph_schema")
        after_neighbors = await after.call("graph_neighbors", entity="Project")
    assert after_schema["relations"] == before_schema["relations"]
    assert "Alice" in str(before_neighbors)
    assert "Alice" in str(after_neighbors)


@pytest.mark.asyncio
async def test_terminusdb_incremental_can_extract_without_graph_sources(tmp_path):
    """TerminusDB mode may use the ingest extractor when assertions are omitted."""
    dsn, password = require_terminusdb()
    root = (tmp_path / "source").resolve()
    root.mkdir()
    document = root / "owner.md"
    document.write_text("Alice owns Project", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        llm={"api_key": "test", "model": "test"},
        data={"base_dir": tmp_path / "runtime"},
        workspace={"storage_mode": "incremental", "auto_gc": False},
        graph={
            "backend": "terminusdb",
            "dsn": dsn,
            "password": password,
        },
    )

    async def builder(cfg, enable):
        from datamind.data_plane import build_data_plane
        return await build_data_plane(cfg, enable=enable, text_client=Model())

    await reset_terminusdb(settings, tmp_path / "terminusdb-reset")
    workspace = Workspace(settings, builder=builder)
    result = await workspace.update(str(root), surfaces=("graph",), graph_sources=None)

    assert result["snapshots"]["graph"]["provider"] == "terminusdb"
    async with workspace.reader(result["workspace_revision"]) as reader:
        assert "Alice" in str(await reader.call("graph_neighbors", entity="Project"))

