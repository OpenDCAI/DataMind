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
settings = Settings()
assert settings.kb.vector_store == "chroma"
assert settings.kb.ingest_mode == "direct"
checkout = Path(__file__).resolve().parents[1]
assert not Path(datamind.__file__).resolve().is_relative_to(checkout), "imported checkout instead of wheel"
print(f"Installed DataMind {datamind.__version__}: resources and optional-provider imports OK")
