"""Compatibility launcher for the unified Codex CLI (DataMind 2.0)."""
from __future__ import annotations

import sys
from datamind.cli import app


def main():
    app(args=sys.argv[1:] or ['chat'])


if __name__ == '__main__':
    main()
