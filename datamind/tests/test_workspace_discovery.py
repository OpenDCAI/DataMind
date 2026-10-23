"""Discovery is bounded and shares the business read's immutable revision."""
import hashlib
import json
from pathlib import Path

import pytest

from datamind.config import DataSettings
from datamind.core.errors import CapabilityError
from datamind.mcp import execute
from datamind.workspace import Workspace
from datamind.workspace_discovery import WorkspaceView, access_summary


class Embed:
    name = "fixture"
    dimension = 4
    async def embed_texts(self, texts):
        return [[1., 0., 0., 0.] for _ in texts]
    async def embed_query(self, text):
        return [1., 0., 0., 0.]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from datamind import runtime
    monkeypatch.setattr(runtime, "build_embedding", lambda *a, **kw: Embed())
    monkeypatch.setenv("DATAMIND_DATA_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DATAMIND__WORKSPACE__RETENTION", "all")
    monkeypatch.setenv("DATAMIND__WORKSPACE__STORAGE_MODE", "snapshot")
    monkeypatch.delenv("DATAMIND_PROFILE", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = DataSettings(_env_file=None, data={"base_dir": tmp_path / "runtime"},
                            workspace={"storage_mode": "snapshot", "retention": "all"})
    root = tmp_path / "sources"
    root.mkdir()
    (root / "notes.md").write_text("Original operating plan.", encoding="utf-8")
    (root / "sales.csv").write_text("amount,region\n10,North\n20,South\n", encoding="utf-8")
    return Workspace(settings), root


async def call(operation, tool, **kwargs):
    result = await execute("datamind_" + tool, {"operation_id": operation["operation_id"], **kwargs})
    assert result["workspace_revision"] == operation["revision"]
    return result["result"]


@pytest.mark.asyncio
async def test_catalog_and_parsed_assets_stay_pinned_through_publish_and_rollback(workspace, monkeypatch):
    w, root = workspace
    first = await w.update(str(root), surfaces=["kb", "db"])
    op = await execute("datamind_operation_begin", {"mode": "read"})
    assert "manifest" not in op
    assert op["workspace"]["source_count"] == 2
    assert len(json.dumps(op)) < 1500
    assert (await call(op, "workspace_catalog", query="sales.csv"))["total_count"] == 1
    table = (await call(op, "workspace_catalog", kind="tables", source="sales.csv", column="amount"))["items"][0]
    assert table["rows"] == 2 and "amount" in table["columns"]
    assert table["source_version"] == "sha256:" + hashlib.sha256((root / "sales.csv").read_bytes()).hexdigest()
    parsed = await call(op, "workspace_read_asset", path="sales.csv", offset=1, limit=1, columns=["amount"])
    assert parsed["rows"] == [["20"]] and parsed["reused"]
    assert parsed["next_offset"] is None
    rows = await call(op, "db_query_sql", sql=f'SELECT SUM(CAST(amount AS INTEGER)) FROM "{table["table"]}"')
    assert rows["rows"][0][0] == 30
    (root / "notes.md").write_text("Changed plan.")
    (root / "sales.csv").write_text("amount,region\n99,West\n")
    second = await w.update(str(root), surfaces=["kb", "db"])
    # Prove text is reused even if the original file changes and parsing is unavailable.
    from datamind import workspace_discovery
    monkeypatch.setattr(workspace_discovery, "extract_document", lambda *a: pytest.fail("unexpected reparsing"))
    assert (await call(op, "workspace_read_asset", path="notes.md"))["text"] == "Original operating plan."
    assert (await call(op, "workspace_catalog", kind="tables"))["items"][0]["rows"] == 2
    fresh = await execute("datamind_operation_begin", {"mode": "read"})
    assert (await call(fresh, "workspace_read_asset", path="sales.csv", columns=["amount"]))["rows"] == [["99"]]
    w.rollback(first["workspace_revision"], expected_revision=second["workspace_revision"])
    # An already-open reader stays on the newer revision even after rollback.
    assert (await call(fresh, "workspace_read_asset", path="notes.md"))["text"] == "Changed plan."
    for operation in [op, fresh]:
        await execute("datamind_operation_close", {"operation_id": operation["operation_id"]})


@pytest.mark.asyncio
async def test_metadata_and_assets_do_not_open_business_backends(workspace, monkeypatch):
    w, root = workspace
    await w.update(str(root), surfaces=["kb", "db"])
    from datamind import runtime
    def forbidden(*args, **kwargs):
        pytest.fail("catalog/asset read opened a business backend")
    monkeypatch.setattr(runtime, "build_embedding", forbidden)
    monkeypatch.setattr(runtime, "build_db_service", forbidden)
    op = await execute("datamind_operation_begin", {"mode": "read"})
    assert (await call(op, "workspace_catalog", kind="tables"))["count"] == 1
    assert (await call(op, "workspace_read_asset", path="notes.md"))["reused"]
    with pytest.raises(CapabilityError):
        await call(op, "workspace_read_asset", path="../outside.txt")
    with pytest.raises(ValueError):
        await call(op, "workspace_read_asset", path="sales.csv", columns=["invented"])
    await execute("datamind_operation_close", {"operation_id": op["operation_id"]})
    with pytest.raises(ValueError, match="Unknown operation"):
        await call(op, "workspace_catalog")


def test_summary_and_source_pages_do_not_grow_with_manifest_payload():
    manifest = {"workspace_revision": "r1", "source_root": "/workspace", "surfaces": ["kb"],
                "sources": [{"uri": f"/workspace/file-{i}.md", "relative_path": f"file-{i}.md",
                             "version": "sha256:" + "a" * 64, "huge_internal_binding": "x" * 10000}
                            for i in range(2000)]}
    assert len(json.dumps(access_summary(manifest))) < 500
    view = WorkspaceView(manifest, Path("/unused"))
    page = view.catalog(limit=7)
    assert page["total_count"] == 2000 and page["next_offset"] == 7
    assert "huge_internal_binding" not in json.dumps(page)
    assert view.catalog(offset=1999, limit=7)["next_offset"] is None
    assert view.catalog(query="file-19.md")["count"] == 1
    assert view.catalog(query="not-indexed")["count"] == 0
    with pytest.raises(ValueError):
        view.catalog(limit=101)


@pytest.mark.asyncio
async def test_incremental_assets_reuse_typed_table_objects_and_text(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    from datamind import runtime
    from datamind.workspace_incremental import canonical_tables, save_artifact
    monkeypatch.setattr(runtime, "build_embedding", lambda *a, **kw: Embed())
    settings = DataSettings(_env_file=None, data={"base_dir": tmp_path / "runtime"},
        kb={"vector_store": "lancedb"}, embedding={"model": "fixture", "dimension": 4},
        workspace={"storage_mode": "incremental", "retention": "all", "auto_gc": False})
    root = tmp_path / "sources"; root.mkdir()
    (root / "notes.md").write_text("Frozen parsed evidence.")
    w = Workspace(settings)
    manifest = await w.update(str(root), surfaces=["kb"])
    async with w.reader(enable=set()) as reader:
        assert (await reader.call("workspace_read_asset", path="notes.md"))["reused"]
    csv = root / "sales.csv"; csv.write_text("收入,region\n10,North\n,South\n")
    entry = {"uri": str(csv), "relative_path": csv.name, "version": "sha256:fixture"}
    tables = canonical_tables(w, entry, csv)
    entry["artifact"] = save_artifact(w.root, "fixture", {"tables": tables})
    entry["source_id"] = "file:fixture"
    manifest["sources"].append(entry)
    manifest["surfaces"].append("db")
    view = WorkspaceView(manifest, w.root)
    catalog = view.catalog(kind="tables", column="收入")["items"][0]
    assert catalog["source_headers"][0] == "收入" and catalog["columns"][0] == "col_1"
    assert view.read_asset(path="sales.csv", columns=["col_1"])["rows"] == [["10"], [""]]
