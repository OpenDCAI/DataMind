"""Real Dolt integration: shared snapshots, pruning and rollback baselines."""

import hashlib
import shutil
import pytest
from datamind.config import Settings
from datamind.workspace import Workspace
from datamind.core.errors import CapabilityError

pytestmark = pytest.mark.skipif(
    shutil.which("dolt") is None, reason="requires real Dolt binary"
)


@pytest.mark.asyncio
async def test_dolt_snapshots_reuse_unchanged_tables_and_prune_history(tmp_path):
    root = (tmp_path / "sources").resolve()
    root.mkdir()
    sales = root / "sales.csv"
    sales.write_text("amount\n100\n200\n")
    other = root / "other.csv"
    other.write_text("item\nunchanged\n")
    cfg = Settings(
        _env_file=None,
        data={"base_dir": tmp_path / "runtime"},
        db={"dialect": "dolt"},
        workspace={"storage_mode": "incremental", "retention": "all", "auto_gc": False},
    )
    w = Workspace(cfg)
    a = await w.update(str(root), surfaces=["db"])
    b = await w.update(str(root), surfaces=["db"])
    assert a["snapshots"]["db"] == b["snapshots"]["db"]
    assert b["metrics"]["db_tables_changed"] == 0
    assert b["metrics"]["db_tables_reused"] == 2
    sales.write_text("amount\n500\n")
    c = await w.update(str(root), surfaces=["db"])
    assert c["metrics"]["db_tables_changed"] == 1
    assert c["metrics"]["db_tables_reused"] == 1

    async def total(revision):
        async with w.reader(revision) as reader:
            names = (await reader.call("db_list_tables"))["tables"]
            for name in names:
                if "amount" in str(await reader.call("db_describe_table", table=name)):
                    table = name
                    break
            result = await reader.call(
                "db_query_sql",
                sql=f"SELECT SUM(CAST(amount AS DOUBLE)) AS total FROM `{table}`",
            )
            catalog = await reader.call("workspace_catalog", kind="tables", source="sales.csv", column="amount")
            assert catalog["items"][0]["table"] == table
            asset = await reader.call("workspace_read_asset", path="sales.csv", columns=["amount"])
            assert sum(float(row[0]) for row in asset["rows"]) == result["rows"][0][0]
            with pytest.raises(CapabilityError):
                await reader.call(
                    "db_query_sql", sql=f"SELECT * FROM `dolt/main`.`{table}`"
                )
            return result["rows"][0][0]

    assert await total(a["workspace_revision"]) == 300
    assert await total(c["workspace_revision"]) == 500
    w.rollback(a["workspace_revision"], expected_revision=c["workspace_revision"])
    other.write_text("item\nchanged\n")
    sales.write_text("amount\n100\n200\n")
    d = await w.update(str(root), surfaces=["db"])
    assert await total(d["workspace_revision"]) == 300
    w.configure_retention("latest")
    cleanup = await w.collect(dry_run=False, compact=True)
    assert cleanup["providers"]["db"]["physical_gc_pending"] is False
    assert await total(d["workspace_revision"]) == 300
    with pytest.raises(CapabilityError):
        w.manifest(a["workspace_revision"])
    assert len(list((w.root / "shared" / "dolt").glob(".dolt"))) == 1


@pytest.mark.asyncio
async def test_migrate_all_surfaces_and_publish_new_shared_generation(
    tmp_path, monkeypatch
):
    pytest.importorskip("lancedb")
    from datamind import runtime

    class Embed:
        name = "fixture"
        dimension = 4
        calls = 0

        async def embed_texts(self, texts):
            self.calls += len(texts)
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        async def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    embed = Embed()
    monkeypatch.setattr(runtime, "build_embedding", lambda *a, **kw: embed)
    root = (tmp_path / "source").resolve()
    root.mkdir()
    doc = root / "owner.md"
    doc.write_text("Alice owns Project")
    sales = root / "sales.csv"
    sales.write_text("amount\n100\n200\n")
    cfg = Settings(
        _env_file=None,
        data={"base_dir": tmp_path / "runtime"},
        embedding={"model": "fixture", "dimension": 4},
        workspace={"storage_mode": "snapshot", "retention": "all", "auto_gc": False},
    )
    graph = [
        {
            "uri": str(doc),
            "version": "sha256:" + hashlib.sha256(doc.read_bytes()).hexdigest(),
            "triples": [{"subject": "Alice", "relation": "owns", "object": "Project"}],
        }
    ]
    old = Workspace(cfg)
    a = await old.update(str(root), graph_sources=graph)
    old_calls = embed.calls
    doc.unlink()
    sales.unlink()
    cfg.workspace.storage_mode = "incremental"
    cfg.kb.vector_store = "lancedb"
    cfg.db.dialect = "dolt"
    w = Workspace(cfg)
    b = await w.migrate()
    assert b["migrated_from"] == a["workspace_revision"]
    assert embed.calls == old_calls

    async def total(reader):
        tables = (await reader.call("db_list_tables"))["tables"]
        assert len(tables) == 1
        result = await reader.call(
            "db_query_sql",
            sql=f"SELECT SUM(CAST(amount AS DOUBLE)) AS total FROM `{tables[0]}`",
        )
        return result["rows"][0][0]

    for revision in (a["workspace_revision"], b["workspace_revision"]):
        async with w.reader(revision) as reader:
            assert await total(reader) == 300
            assert "Alice" in str(await reader.call("kb_search", query="owner"))
            assert "Alice" in str(
                await reader.call("graph_neighbors", entity="Project")
            )
            assert (
                "Alice" in (await reader.call("raw_file_read", path=str(doc)))["text"]
            )
    doc.write_text("Bob owns Project")
    sales.write_text("amount\n500\n")
    graph[0].update(
        version="sha256:" + hashlib.sha256(doc.read_bytes()).hexdigest(),
        triples=[{"subject": "Bob", "relation": "owns", "object": "Project"}],
    )
    async with w.reader(b["workspace_revision"]) as old_reader:
        await w.update(str(root), graph_sources=graph)
        w.configure_retention("latest")
        await w.collect(dry_run=False, compact=True)
        assert await total(old_reader) == 300
        assert "Alice" in str(await old_reader.call("kb_search", query="owner"))
        assert "Alice" in str(
            await old_reader.call("graph_neighbors", entity="Project")
        )
    await w.collect(dry_run=False, compact=True)
    assert w.status(a["workspace_revision"])["state"] == "PRUNED"
    async with w.reader() as reader:
        assert await total(reader) == 500
        assert "Bob" in str(await reader.call("kb_search", query="owner"))
        assert "Bob" in str(await reader.call("graph_neighbors", entity="Project"))
        table = (await reader.call("db_list_tables"))["tables"][0]
        assert (
            await reader.call(
                "db_query_sql",
                sql=f"WITH a AS (SELECT amount FROM `{table}`) SELECT * FROM a",
            )
        )["rows"] == [["500"]]
        for sql in (
            f"SELECT * FROM `{table}` AS OF HEAD",
            "SELECT * FROM mysql.user",
            "SELECT * FROM dolt_log",
        ):
            with pytest.raises(CapabilityError):
                await reader.call("db_query_sql", sql=sql)


@pytest.mark.asyncio
async def test_interrupted_dolt_child_is_reaped_on_next_operation(tmp_path):
    import asyncio, subprocess, sys, textwrap

    root = (tmp_path / "source").resolve()
    root.mkdir()
    (root / "sales.csv").write_text("amount\n100\n")
    cfg = Settings(
        _env_file=None,
        data={"base_dir": tmp_path / "runtime with spaces"},
        db={"dialect": "dolt"},
        workspace={"storage_mode": "incremental", "retention": "all", "auto_gc": False},
    )
    w = Workspace(cfg)
    first = await w.update(str(root), surfaces=["db"])
    code = """
import asyncio,os,sys
from contextlib import asynccontextmanager
from datamind.config import Settings
from datamind.workspace import Workspace
from datamind.workspace_native import DoltRepository
cfg=Settings(_env_file=None,data={'base_dir':sys.argv[1]},db={'dialect':'dolt'},workspace={'storage_mode':'incremental','retention':'all','auto_gc':False})
w=Workspace(cfg)
async def crash():
    async with DoltRepository(w).session(): os._exit(92)
asyncio.run(crash())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", textwrap.dedent(code), str(cfg.data.base_dir)],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 92, result.stderr
    # The parent is dead, but its private SQL child is still running. A fresh
    # process/object must recover it without touching other Dolt instances.
    fresh = Workspace(cfg)
    async with fresh.reader(first["workspace_revision"]) as reader:
        table = (await reader.call("db_list_tables"))["tables"][0]
        result = await reader.call("db_query_sql", sql=f"SELECT amount FROM `{table}`")
        assert result["rows"] == [["100"]]
    (root / "sales.csv").write_text("amount\n200\n")
    updated = await fresh.update(str(root), surfaces=["db"])
    assert updated["workspace_revision"] != first["workspace_revision"]


@pytest.mark.asyncio
async def test_mcp_native_publication_does_not_open_independent_dolt_store(
    tmp_path, monkeypatch
):
    from datamind import mcp

    root = (tmp_path / "source").resolve()
    root.mkdir()
    (root / "sales.csv").write_text("amount\n100\n")
    cfg = Settings(
        _env_file=None,
        data={"base_dir": tmp_path / "runtime"},
        db={"dialect": "dolt"},
        hooks={"path_allowlist_extra": [str(root)]},
        workspace={"storage_mode": "incremental", "retention": "all", "auto_gc": False},
    )
    monkeypatch.setattr(mcp, "load_settings", lambda profile=None: cfg)
    operation = await mcp.execute("datamind_operation_begin", {"mode": "write"})
    result = await mcp.execute(
        "datamind_workspace_publish",
        {
            "operation_id": operation["operation_id"],
            "path": str(root),
            "surfaces": ["db"],
        },
    )
    receipt = result["result"]
    assert receipt["results"][0]["status"] == "stored", receipt
    assert receipt["results"][0]["details"]["format_version"] == 2
    assert not (cfg.data.storage_dir / ".dolt").exists()
    await mcp.execute(
        "datamind_operation_close", {"operation_id": operation["operation_id"]}
    )
    read = await mcp.execute("datamind_operation_begin", {"mode": "read"})
    tables = await mcp.execute(
        "datamind_db_list_tables", {"operation_id": read["operation_id"]}
    )
    assert len(tables["result"]["tables"]) == 1
    await mcp.execute(
        "datamind_operation_close", {"operation_id": read["operation_id"]}
    )
