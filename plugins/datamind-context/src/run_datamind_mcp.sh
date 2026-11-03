#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
REPO_ROOT="${DATAMIND_REPO_ROOT:-}"

if [[ -z "${REPO_ROOT}" && -f "${PLUGIN_DIR}/.datamind-repo-root" ]]; then
  REPO_ROOT="$(head -n 1 "${PLUGIN_DIR}/.datamind-repo-root")"
fi

if [[ -z "${REPO_ROOT}" ]]; then
  REPO_ROOT="$(cd "${PLUGIN_DIR}/../.." && pwd -P)"
fi

# Settings loads the repository's .env/.env.datamind relative to the
# process working directory. Codex launches plugin commands from the cached
# plugin directory, so make the resolved DataMind checkout the working root.
cd "${REPO_ROOT}"

if [[ -n "${DATAMIND_PYTHON:-}" && -x "${DATAMIND_PYTHON}" ]]; then
  exec "${DATAMIND_PYTHON}" "${PLUGIN_DIR}/src/datamind_mcp.py"
fi

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  exec "${REPO_ROOT}/.venv/bin/python" "${PLUGIN_DIR}/src/datamind_mcp.py"
fi

exec python3 "${PLUGIN_DIR}/src/datamind_mcp.py"
