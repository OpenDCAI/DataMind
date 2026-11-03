#!/usr/bin/env python3
"""Codex plugin bootstrap for the installed DataMind data-only MCP server."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

def repo_root() -> Path:
    configured = os.environ.get("DATAMIND_REPO_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    marker = PLUGIN_ROOT / ".datamind-repo-root"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").splitlines()[0].strip()
        if value:
            return Path(value).expanduser().resolve()
    return PLUGIN_ROOT.parents[1].resolve()

ROOT = repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datamind.mcp import execute, handle, main

if __name__ == '__main__':
    main()
