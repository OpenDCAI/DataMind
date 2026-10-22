"""Incremental storage, native snapshots, retention and cross-call leases."""

import asyncio
import hashlib

import pytest

from datamind.config import Settings
from datamind.core.errors import CapabilityError
from datamind.workspace import Workspace


class Embed:
    name = "fixture"
    dimension = 4
    calls = 0

    async def embed_texts(self, texts):
        self.calls += len(texts)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    async def embed_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


@pytest.fixture
def incremental(tmp_path, monkeypatch):
    from datamind import runtime

    embed = Embed()
    monkeypatch.setattr(runtime, "build_embedding", lambda *a, **kw: embed)
    root = (tmp_path / "sources").resolve()
    root.mkdir()
    cfg = Settings(
        _env_file=None,
        data={"base_dir": tmp_path / "runtime"},
        embedding={"model": "fixture", "dimension": 4},
        kb={"vector_store": "lancedb"},
        workspace={"storage_mode": "incremental", "retention": "all", "auto_gc": False},
    )
    return Workspace(cfg), root, embed


async def publish(workspace, root, owner="Alice", surfaces=("graph",)):
    doc = root / "owner.md"
    doc.write_text(owner + " owns Project")
    return await workspace.update(
        str(root),
        surfaces=surfaces,
        graph_sources=[
            {
                "uri": str(doc),
                "version": "sha256:" + hashlib.sha256(doc.read_bytes()).hexdigest(),
                "triples": [
                    {"subject": owner, "relation": "owns", "object": "Project"}
                ],
            }
        ],
    )


@pytest.mark.asyncio
async def test_graph_objects_reused_deleted_and_rollback_remains_pinned(incremental):
    w, root, _ = incremental
    a = await publish(w, root)
    b = await publish(w, root)
    assert a["sources"] == b["sources"]
    assert b["metrics"]["object_bytes_written"] == 0
    assert b["metrics"]["sources_reused"] == 1
    assert not (w.root / "revisions" / b["workspace_revision"] / "storage").exists()
    async with w.reader(a["workspace_revision"]) as old:
        c = await publish(w, root, "Bob")
        assert "Alice" in str(await old.call("graph_neighbors", entity="Project"))
        assert "Bob" not in str(await old.call("graph_neighbors", entity="Project"))
        assert (
            "Alice"
            in (await old.call("raw_file_read", path=str(root / "owner.md")))["text"]
        )
        assert (await old.call("workspace_inspect", path=str(root)))["count"] == 1
    w.rollback(a["workspace_revision"], expected_revision=c["workspace_revision"])
    async with w.reader() as reader:
        assert "Alice" in str(await reader.call("graph_neighbors", entity="Project"))
    (root / "owner.md").unlink()
    await w.update(str(root), surfaces=["graph"], graph_sources=[])
    async with w.reader() as reader:
        assert (await reader.call("graph_schema"))["nodes"] == 0


@pytest.mark.asyncio
async def test_native_kb_versions_share_storage_and_survive_cleanup(incremental):
    pytest.importorskip("lancedb")
    w, root, embed = incremental
    a = await publish(w, root, surfaces=("kb", "graph"))
    b = await publish(w, root, surfaces=("kb", "graph"))
    assert a["snapshots"]["kb"] == b["snapshots"]["kb"]
    assert embed.calls == 1
    assert b["metrics"]["kb_rows_upserted"] == 0
    assert b["metrics"]["object_bytes_written"] == 0
    async with w.reader(a["workspace_revision"]) as old:
        c = await publish(w, root, "Bob", surfaces=("kb", "graph"))
        assert a["snapshots"]["kb"]["path"] == c["snapshots"]["kb"]["path"]
        assert a["snapshots"]["kb"]["version"] != c["snapshots"]["kb"]["version"]
        w.configure_retention("latest")
        cleanup = await w.collect(dry_run=False, compact=True)
        assert a["workspace_revision"] in cleanup["retained_revisions"]
        assert "Alice" in str(await old.call("kb_search", query="owner"))
        assert "Bob" not in str(await old.call("kb_search", query="owner"))
    await w.collect(dry_run=False)
    with pytest.raises(CapabilityError):
        w.manifest(a["workspace_revision"])
    async with w.reader() as reader:
        assert "Bob" in str(await reader.call("kb_search", query="owner"))
        assert (await reader.call("kb_count"))["chunks"] == 1


@pytest.mark.asyncio
async def test_latest_recent_all_and_protected_revisions(incremental):
    w, root, _ = incremental
    versions = []
    for owner in ("Alice", "Bob", "Carol"):
        versions.append(await publish(w, root, owner))
    a, b, c = [m["workspace_revision"] for m in versions]
    w.configure_retention("recent", 2)
    preview = await w.collect()
    assert preview["pruned_revisions"] == [a]
    assert w.manifest(a)
    w.protect(a)
    assert (await w.collect(dry_run=False))["pruned_revisions"] == []
    w.protect(a, False)
    await w.collect(dry_run=False)
    assert w.status(a)["state"] == "PRUNED"
    w.configure_retention("latest")
    result = await w.collect(dry_run=False)
    assert b in result["pruned_revisions"]
    assert w.manifest(c)
    assert (await w.collect(dry_run=False))["reclaimed_bytes"] == 0


@pytest.mark.asyncio
async def test_failed_and_stale_publications_leave_previous_head(
    incremental, monkeypatch
):
    w, root, _ = incremental
    a = await publish(w, root)
    doc = root / "owner.md"
    doc.write_text("Bob owns Project")
    with pytest.raises(Exception):
        await w.update(
            str(root),
            surfaces=["graph"],
            graph_sources=[
                {"uri": str(doc), "version": "sha256:" + "0" * 64, "triples": []}
            ],
        )
    assert w.current_revision == a["workspace_revision"]
    # Publication checks epoch as well as revision, including rollback ABA.
    b = await publish(w, root, "Bob")
    w.rollback(a["workspace_revision"], expected_revision=b["workspace_revision"])
    await publish(w, root, "Carol")
    async with w.reader() as reader:
        assert "Carol" in str(await reader.call("graph_neighbors", entity="Project"))


def test_new_profiles_default_latest_existing_history_defaults_all(tmp_path):
    cfg = Settings(_env_file=None, data={"base_dir": tmp_path})
    w = Workspace(cfg)
    assert w.retention["mode"] == "latest"
    w.configure_retention("all")
    assert Workspace(cfg).retention["mode"] == "all"


@pytest.mark.asyncio
async def test_migration_uses_frozen_sources_and_stored_embeddings(
    tmp_path, monkeypatch
):
    pytest.importorskip("lancedb")
    from datamind import runtime

    embed = Embed()
    monkeypatch.setattr(runtime, "build_embedding", lambda *a, **kw: embed)
    root = (tmp_path / "source").resolve()
    root.mkdir()
    cfg = Settings(
        _env_file=None,
        data={"base_dir": tmp_path / "runtime"},
        embedding={"model": "fixture", "dimension": 4},
        workspace={"retention": "all", "storage_mode": "snapshot", "auto_gc": False},
    )
    old = Workspace(cfg)
    first = await publish(old, root, surfaces=("kb", "graph"))
    assert embed.calls == 1
    (root / "owner.md").unlink()
    cfg.kb.vector_store = "lancedb"
    cfg.workspace.storage_mode = "incremental"
    w = Workspace(cfg)
    result = await w.migrate()
    assert result["migrated_from"] == first["workspace_revision"]
    assert result["format_version"] == 2
    assert embed.calls == 1
    for revision in (first["workspace_revision"], result["workspace_revision"]):
        async with w.reader(revision) as reader:
            assert "Alice" in str(await reader.call("kb_search", query="owner"))
            assert "Alice" in str(
                await reader.call("graph_neighbors", entity="Project")
            )
            assert (
                "Alice"
                in (await reader.call("raw_file_read", path=str(root / "owner.md")))[
                    "text"
                ]
            )


@pytest.mark.asyncio
async def test_mcp_operation_pins_across_calls_and_close_releases_history(
    incremental, monkeypatch
):
    from datamind import mcp

    w, root, _ = incremental
    a = await publish(w, root)
    monkeypatch.setattr(mcp, "load_settings", lambda profile=None: w.settings)
    operation = await mcp.execute("datamind_operation_begin", {"mode": "read"})
    args = {"operation_id": operation["operation_id"], "entity": "Project"}
    assert "Alice" in str(await mcp.execute("datamind_graph_neighbors", args))
    b = await publish(w, root, "Bob")
    w.configure_retention("latest")
    cleanup = await w.collect(dry_run=False)
    assert a["workspace_revision"] in cleanup["retained_revisions"]
    assert "Alice" in str(await mcp.execute("datamind_graph_neighbors", args))
    with pytest.raises(PermissionError):
        await mcp.execute(
            "datamind_workspace_retention",
            {"operation_id": operation["operation_id"], "mode": "all"},
        )
    await mcp.execute(
        "datamind_operation_close", {"operation_id": operation["operation_id"]}
    )
    await w.collect(dry_run=False)
    assert w.status(a["workspace_revision"])["state"] == "PRUNED"
    fresh = await mcp.execute("datamind_operation_begin", {"mode": "read"})
    assert fresh["revision"] == b["workspace_revision"]
    await mcp.execute(
        "datamind_operation_close", {"operation_id": fresh["operation_id"]}
    )


@pytest.mark.asyncio
async def test_expired_operations_fail_instead_of_switching_to_latest(
    incremental, monkeypatch
):
    from datamind import mcp

    w, root, _ = incremental
    await publish(w, root)
    monkeypatch.setattr(mcp, "load_settings", lambda profile=None: w.settings)
    operation = await mcp.execute("datamind_operation_begin", {"mode": "read"})
    await publish(w, root, "Bob")
    with w._connect() as db:
        db.execute("UPDATE leases SET expires=0")
    w.configure_retention("latest")
    await w.collect(dry_run=False)
    with pytest.raises(CapabilityError, match="expired"):
        await mcp.execute(
            "datamind_graph_schema", {"operation_id": operation["operation_id"]}
        )
    assert await mcp.execute(
        "datamind_operation_close", {"operation_id": operation["operation_id"]}
    ) == {"closed": True}


@pytest.mark.asyncio
async def test_failed_native_build_does_not_expose_rows_and_reuses_embeddings(
    incremental, monkeypatch
):
    pytest.importorskip("lancedb")
    import datamind.workspace as module

    w, root, embed = incremental
    a = await publish(w, root, surfaces=("kb", "graph"))
    original = module._atomic_json

    def fail_manifest(path, value, **kwargs):
        if path.name == "manifest.json":
            raise OSError("injected before publication")
        return original(path, value, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_atomic_json", fail_manifest)
        with pytest.raises(OSError):
            await publish(w, root, "Bob", surfaces=("kb", "graph"))
    assert w.current_revision == a["workspace_revision"]
    async with w.reader() as reader:
        assert "Alice" in str(await reader.call("kb_search", query="owner"))
        assert "Bob" not in str(await reader.call("kb_search", query="owner"))
    calls = embed.calls
    b = await publish(Workspace(w.settings), root, "Bob", surfaces=("kb", "graph"))
    assert embed.calls == calls
    assert b["metrics"]["sources_reused"] == 1
    async with w.reader() as reader:
        assert "Bob" in str(await reader.call("kb_search", query="owner"))


@pytest.mark.asyncio
async def test_rollback_aba_during_build_fails_epoch_cas(incremental, monkeypatch):
    import datamind.workspace as module

    w, root, _ = incremental
    a = await publish(w, root)
    b = await publish(w, root, "Bob")
    original = module._atomic_json

    def rollback_between_prepare_and_publish(path, value, **kwargs):
        result = original(path, value, **kwargs)
        if path.name == "manifest.json":
            w.rollback(
                a["workspace_revision"], expected_revision=b["workspace_revision"]
            )
            w.rollback(
                b["workspace_revision"], expected_revision=a["workspace_revision"]
            )
        return result

    monkeypatch.setattr(module, "_atomic_json", rollback_between_prepare_and_publish)
    with pytest.raises(CapabilityError, match="conflict"):
        await publish(w, root, "Carol")
    assert w.current_revision == b["workspace_revision"]
    async with w.reader() as reader:
        assert "Bob" in str(await reader.call("graph_neighbors", entity="Project"))


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["build.json", "manifest.json"])
async def test_process_death_before_publish_is_recovered_without_partial_visibility(
    incremental,
    crash_point,
):
    import subprocess, sys, textwrap

    w, root, _ = incremental
    a = await publish(w, root)
    code = """
import asyncio,hashlib,os,sys
from pathlib import Path
from datamind.config import Settings
from datamind.workspace import Workspace
import datamind.workspace as module
cfg=Settings(_env_file=None,data={'base_dir':sys.argv[1]},workspace={'storage_mode':'incremental','retention':'all','auto_gc':False},embedding={'model':'fixture','dimension':4},kb={'vector_store':'lancedb'})
w=Workspace(cfg);root=Path(sys.argv[2]);doc=root/'owner.md';doc.write_text('Bob owns Project')
original=module._atomic_json
def crash(path,value,**kwargs):
    if path.name==sys.argv[3]:os._exit(91)
    original(path,value,**kwargs)
module._atomic_json=crash
asyncio.run(w.update(str(root),surfaces=['graph'],graph_sources=[{'uri':str(doc),'version':'sha256:'+hashlib.sha256(doc.read_bytes()).hexdigest(),'triples':[{'subject':'Bob','relation':'owns','object':'Project'}]}]))
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            textwrap.dedent(code),
            str(w.settings.data.base_dir),
            str(root),
            crash_point,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 91, result.stderr
    fresh = Workspace(w.settings)
    assert fresh.current_revision == a["workspace_revision"]
    async with fresh.reader() as reader:
        assert "Alice" in str(await reader.call("graph_neighbors", entity="Project"))
    building = [
        r["revision"] for r in fresh.history()["versions"] if r["state"] == "BUILDING"
    ]
    assert len(building) == 1
    await fresh.collect(dry_run=False)
    assert fresh.status(building[0])["state"] == "PRUNED"
    b = await publish(fresh, root, "Bob")
    async with fresh.reader(b["workspace_revision"]) as reader:
        assert "Bob" in str(await reader.call("graph_neighbors", entity="Project"))


@pytest.mark.asyncio
async def test_graph_parallel_sources_retract_independently_and_cycles_do_not_hide_paths(
    incremental,
):
    from datamind.workspace_graph import put_graph, GraphView

    w, root, _ = incremental
    first = put_graph(
        w.root,
        [
            {"subject": "A", "relation": "owns", "object": "A", "source": "one"},
            {"subject": "A", "relation": "owns", "object": "B", "source": "one"},
        ],
    )
    second = put_graph(
        w.root, [{"subject": "A", "relation": "owns", "object": "B", "source": "two"}]
    )
    view = GraphView(w.root, [first, second])
    assert len(await view.neighbors("A", direction="out")) == 3
    assert (await view.traverse("A", max_results=1))[0].nodes == ["A", "B"]
    await view.aclose()
    view = GraphView(w.root, [second])
    edges = await view.neighbors("A")
    assert len(edges) == 1 and edges[0].properties["source"] == "two"
    await view.aclose()


@pytest.mark.asyncio
async def test_returning_to_old_embedding_index_keeps_old_reader_valid(incremental):
    pytest.importorskip("lancedb")
    w, root, _ = incremental
    a = await publish(w, root, surfaces=("kb", "graph"))
    async with w.reader(a["workspace_revision"]) as old:
        w.settings.embedding.model = "fixture-b"
        await publish(w, root, "Bob", surfaces=("kb", "graph"))
        w.settings.embedding.model = "fixture"
        await publish(w, root, "Carol", surfaces=("kb", "graph"))
        assert "Alice" in str(await old.call("kb_search", query="owner"))
        assert "Carol" not in str(await old.call("kb_search", query="owner"))
    async with w.reader() as reader:
        assert "Carol" in str(await reader.call("kb_search", query="owner"))


@pytest.mark.asyncio
async def test_cancelled_storage_worker_cannot_write_after_releasing_writer_lock(
    tmp_path,
):
    import threading
    from filelock import AsyncFileLock
    from datamind.workspace_native import blocking

    entered = threading.Event()
    finish = threading.Event()
    events = []

    def write():
        entered.set()
        assert finish.wait(5)
        events.append("finished write")

    async def build():
        async with AsyncFileLock(str(tmp_path / "writer.lock")):
            await blocking(write)

    task = asyncio.create_task(build())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    async def next_build():
        async with AsyncFileLock(str(tmp_path / "writer.lock")):
            events.append("next writer")

    next_task = asyncio.create_task(next_build())
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(next_task, 5)
    assert events == ["finished write", "next writer"]


@pytest.mark.asyncio
async def test_open_graph_reader_does_not_block_next_publication(incremental):
    w, root, _ = incremental
    a = await publish(w, root)
    async with w.reader(a["workspace_revision"]) as reader:
        assert "Alice" in str(await reader.call("graph_neighbors", entity="Project"))
        await asyncio.wait_for(publish(w, root, "Bob"), 5)
        assert "Alice" in str(await reader.call("graph_neighbors", entity="Project"))
        w.configure_retention("latest")
        await asyncio.wait_for(w.collect(dry_run=False, compact=True), 5)
        assert "Alice" in str(await reader.call("graph_neighbors", entity="Project"))


def test_saved_retention_overrides_config_defaults_after_restart(tmp_path):
    cfg = Settings(
        _env_file=None, data={"base_dir": tmp_path}, workspace={"retention": "all"}
    )
    w = Workspace(cfg)
    assert w.retention["mode"] == "all"
    w.configure_retention("latest")
    assert Workspace(cfg).retention["mode"] == "latest"
