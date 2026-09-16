from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "src" / "datamind_mcp.py"
SPEC = importlib.util.spec_from_file_location("datamind_mcp", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
datamind_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(datamind_mcp)


def test_read_only_file_tools_skip_embedding_surfaces():
    assert datamind_mcp.enabled_surfaces("datamind_raw_file_read") == {"graph"}
    assert datamind_mcp.enabled_surfaces("datamind_workspace_inspect") == {"graph"}


def test_direct_surface_tools_build_only_required_services():
    assert datamind_mcp.enabled_surfaces("datamind_rag_query") == {"kb"}
    assert datamind_mcp.enabled_surfaces("datamind_table_ingest") == {"db"}
    assert datamind_mcp.enabled_surfaces("datamind_graph_query") == {"graph"}
    assert datamind_mcp.enabled_surfaces("datamind_remember") == {"memory"}


def test_agent_and_router_tools_keep_all_surfaces():
    assert datamind_mcp.enabled_surfaces("datamind_ask") is None
    assert datamind_mcp.enabled_surfaces("datamind_store") is None
    assert datamind_mcp.enabled_surfaces("datamind_surface_ingest") is None
