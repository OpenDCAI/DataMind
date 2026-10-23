"""Exercise the installed artifact outside the source checkout."""
from importlib.resources import files
from pathlib import Path

import datamind
from datamind.config import Settings
import datamind.capabilities.db.providers
import datamind.capabilities.kb.providers
from datamind.core.registry import db_registry, vector_store_registry

package = files("datamind")
assert package.joinpath("static/app.html").is_file(), "browser UI missing from wheel"
assert package.joinpath("skills").is_dir(), "skill catalog missing from wheel"
assert any(package.joinpath("skills").glob("*/SKILL.md")), "no packaged skills"
assert "dolt" in db_registry and "lancedb" in vector_store_registry
settings = Settings(llm={"api_key": "wheel-smoke", "model": "test"})
assert settings.kb.vector_store == "chroma"
assert settings.kb.ingest_mode == "direct"
checkout = Path(__file__).resolve().parents[1]
assert not Path(datamind.__file__).resolve().is_relative_to(checkout), "imported checkout instead of wheel"
print(f"Installed DataMind {datamind.__version__}: resources and optional-provider imports OK")

from datamind.mcp import definitions
from datamind.config import DataSettings
from datamind.data_plane import build_data_plane
import asyncio
import tempfile

assert 'datamind_graph_schema' in definitions()
assert 'datamind_workspace_catalog' in definitions()
assert 'datamind_workspace_read_asset' in definitions()
assert 'datamind_workspace_export_bundle' in definitions()
from datamind.workspace_discovery import WorkspaceView
assert package.joinpath('skills/workspace-retrieve/SKILL.md').is_file()
async def direct_smoke():
    with tempfile.TemporaryDirectory() as root:
        cfg = DataSettings(_env_file=None, data={'base_dir': root})
        data = await build_data_plane(cfg, enable={'graph'})
        try:
            assert (await data.call('graph_schema'))['edges'] == 0
        finally:
            await data.aclose()
asyncio.run(direct_smoke())
print('Installed data-only MCP catalogue, retrieval Skill and no-model graph runtime OK')
