#!/usr/bin/env bash
set -euo pipefail

# Install DataMind Context as a local Codex (OpenAI Codex desktop app) plugin.
#
# Codex's plugin model:
#   * marketplaces are directories registered in ~/.codex/config.toml under
#     [marketplaces.<name>] with `source = "<absolute path>"`.
#   * each marketplace contains:
#       <marketplace_root>/.agents/plugins/marketplace.json   (catalogue)
#       <marketplace_root>/plugins/<plugin>/.codex-plugin/plugin.json
#       <marketplace_root>/plugins/<plugin>/...               (the plugin)
#   * plugins are enabled via [plugins."<plugin>@<marketplace>"] enabled = true
#     in ~/.codex/config.toml.
#
# This script lays the plugin out under ~/.codex/marketplaces/datamind/ and
# patches ~/.codex/config.toml to register it. Codex picks it up on next launch.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLUGIN_NAME="datamind-context"
MARKETPLACE_NAME="${DATAMIND_CODEX_MARKETPLACE:-datamind}"
MARKETPLACE_ROOT="${HOME}/.codex/marketplaces/${MARKETPLACE_NAME}"
TARGET_DIR="${MARKETPLACE_ROOT}/plugins/${PLUGIN_NAME}"
MARKETPLACE_JSON="${MARKETPLACE_ROOT}/.agents/plugins/marketplace.json"
CODEX_CONFIG="${HOME}/.codex/config.toml"

REPO_ROOT=""
FORCE="false"
SKIP_DEPS="false"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat <<EOF
Install DataMind Context as a Codex plugin.

Usage:
  ./install.sh [--repo-root /path/to/DataMind] [--force] [--skip-deps] [--python PATH]

Options:
  --repo-root PATH   Existing DataMind repository root. Optional when this
                     script is run from a DataMind checkout.
  --force            Replace an existing install at ${MARKETPLACE_ROOT}.
  --skip-deps        Do not create a venv or install Python dependencies.
  --python PATH      Python interpreter to use for venv creation.
  -h, --help         Show this help.

What this does:
  1. Copies this plugin to ${TARGET_DIR}
  2. Writes ${MARKETPLACE_JSON}
  3. Patches ${CODEX_CONFIG} so Codex sees the marketplace and the plugin

Override the marketplace name by setting DATAMIND_CODEX_MARKETPLACE
(default: datamind).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)  REPO_ROOT="${2:-}"; shift 2 ;;
    --force)      FORCE="true"; shift ;;
    --skip-deps)  SKIP_DEPS="true"; shift ;;
    --python)     PYTHON_BIN="${2:-}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# --- Resolve DataMind runtime root ----------------------------------------

detect_bundled_repo() {
  local base="$1"
  if [[ -f "${base}/pyproject.toml" && -d "${base}/datamind" ]]; then
    printf "%s\n" "$(cd "${base}" && pwd -P)"
    return 0
  fi
  return 1
}
if [[ -z "${REPO_ROOT}" ]]; then
  if BUNDLED_REPO="$(detect_bundled_repo "${SCRIPT_DIR}")"; then
    REPO_ROOT="${BUNDLED_REPO}"
  fi
fi

if [[ -z "${REPO_ROOT}" ]]; then
  CANDIDATE="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
  if [[ -f "${CANDIDATE}/pyproject.toml" && -d "${CANDIDATE}/datamind" ]]; then
    REPO_ROOT="${CANDIDATE}"
  fi
fi

if [[ -z "${REPO_ROOT}" ]]; then
  echo "Missing DataMind runtime." >&2
  echo "Use --repo-root /path/to/DataMind, or run this script from the DataMind repository." >&2
  exit 1
fi

REPO_ROOT="$(cd "${REPO_ROOT}" && pwd -P)"
if [[ ! -f "${REPO_ROOT}/pyproject.toml" || ! -d "${REPO_ROOT}/datamind" ]]; then
  echo "Not a DataMind repository root: ${REPO_ROOT}" >&2
  exit 1
fi

if [[ ! -f "${SCRIPT_DIR}/.codex-plugin/plugin.json" ]]; then
  echo "Cannot find plugin manifest in ${SCRIPT_DIR}" >&2
  exit 1
fi

# --- Sanity: do we have a Codex install? ----------------------------------

if [[ ! -d "${HOME}/.codex" ]]; then
  echo "WARN: ${HOME}/.codex does not exist. Make sure OpenAI Codex desktop is installed." >&2
  echo "      Continuing anyway; the directory will be created if Codex runs once first." >&2
  mkdir -p "${HOME}/.codex"
fi

# --- Stage the marketplace + plugin ---------------------------------------

mkdir -p "${MARKETPLACE_ROOT}/.agents/plugins" "${MARKETPLACE_ROOT}/plugins"

if [[ "${SCRIPT_DIR}" != "${TARGET_DIR}" ]]; then
  if [[ -e "${TARGET_DIR}" && "${FORCE}" != "true" ]]; then
    echo "Target already exists: ${TARGET_DIR}" >&2
    echo "Rerun with --force to replace it." >&2
    exit 1
  fi
  rm -rf "${TARGET_DIR}"
  mkdir -p "${TARGET_DIR}"
  rsync -a \
    --exclude ".DS_Store" \
    --exclude ".datamind-repo-root" \
    --exclude "__pycache__" \
    --exclude ".pytest_cache" \
    --exclude ".venv" \
    "${SCRIPT_DIR}/" "${TARGET_DIR}/"
fi

if [[ "${SCRIPT_DIR}" != "${TARGET_DIR}" ]]; then
  if [[ "${REPO_ROOT}" == "${SCRIPT_DIR}/"* ]]; then
    if BUNDLED_REPO="$(detect_bundled_repo "${TARGET_DIR}")"; then
      REPO_ROOT="${BUNDLED_REPO}"
    fi
  fi
fi

printf "%s\n" "${REPO_ROOT}" > "${TARGET_DIR}/.datamind-repo-root"
chmod +x "${TARGET_DIR}/install.sh" "${TARGET_DIR}/src/run_datamind_mcp.sh"

# --- Bootstrap .env + venv -------------------------------------------------

if [[ ! -f "${REPO_ROOT}/.env.datamind" && -f "${REPO_ROOT}/.env.datamind.example" ]]; then
  cp "${REPO_ROOT}/.env.datamind.example" "${REPO_ROOT}/.env.datamind"
  echo "Created ${REPO_ROOT}/.env.datamind from .env.datamind.example. Configure optional Codex gateway and independent embedding credentials."
fi

if [[ "${SKIP_DEPS}" != "true" ]]; then
  if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    "${PYTHON_BIN}" -m venv "${REPO_ROOT}/.venv"
  fi
  "${REPO_ROOT}/.venv/bin/python" -m pip install --upgrade pip
  "${REPO_ROOT}/.venv/bin/python" -m pip install --prefer-binary -e "${REPO_ROOT}"
fi

# --- Write the marketplace catalogue --------------------------------------

python3 - "${MARKETPLACE_JSON}" "${MARKETPLACE_NAME}" <<'PY'
import json, pathlib, sys

marketplace_path = pathlib.Path(sys.argv[1]).expanduser()
marketplace_name = sys.argv[2]

entry = {
    "name": "datamind-context",
    "source": {
        "source": "local",
        "path": "./plugins/datamind-context",
    },
    "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_USE",
    },
    "category": "Productivity",
}

if marketplace_path.exists():
    try:
        payload = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid marketplace JSON: {marketplace_path}: {exc}")
else:
    payload = {
        "name": marketplace_name,
        "interface": {"displayName": "DataMind"},
        "plugins": [],
    }

payload.setdefault("name", marketplace_name)
payload.setdefault("interface", {}).setdefault("displayName", "DataMind")
plugins = payload.setdefault("plugins", [])
if not isinstance(plugins, list):
    raise SystemExit("marketplace.json field 'plugins' must be an array")

for i, plugin in enumerate(plugins):
    if isinstance(plugin, dict) and plugin.get("name") == entry["name"]:
        plugins[i] = entry
        break
else:
    plugins.append(entry)

marketplace_path.parent.mkdir(parents=True, exist_ok=True)
marketplace_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {marketplace_path}")
PY

# --- Patch ~/.codex/config.toml -------------------------------------------

python3 - "${CODEX_CONFIG}" "${MARKETPLACE_NAME}" "${MARKETPLACE_ROOT}" <<'PY'
"""
Idempotently add (or update) two sections in config.toml:

    [marketplaces.<name>]
    source_type = "local"
    source = "<root>"

    [plugins."datamind-context@<name>"]
    enabled = true

We do NOT use the `tomllib` writer (only stdlib reader), so we edit the file
as text. We replace any existing section with the same header, otherwise we
append.
"""
import pathlib, re, sys, datetime as dt

config_path = pathlib.Path(sys.argv[1]).expanduser()
mname = sys.argv[2]
mroot = sys.argv[3]

config_path.parent.mkdir(parents=True, exist_ok=True)
text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""

now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def replace_or_append_section(text: str, header: str, body: str) -> str:
    """
    `header` is the literal section header line, e.g. '[marketplaces.datamind]'.
    `body` is the lines that follow (no header), terminated with newline.
    Replaces the existing section in place (everything from `header` until the
    next `[...]` header or EOF). Otherwise appends a new section to the end.
    """
    pattern = re.compile(
        r'(?ms)^' + re.escape(header) + r'\n(?:(?!^\[).*\n?)*'
    )
    block = header + "\n" + body
    if not block.endswith("\n"):
        block += "\n"
    if pattern.search(text):
        text = pattern.sub(block, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        if text and not text.endswith("\n\n"):
            text += "\n"
        text += block
    return text

text = replace_or_append_section(
    text,
    f'[marketplaces.{mname}]',
    f'last_updated = "{now}"\nsource_type = "local"\nsource = "{mroot}"\n',
)

text = replace_or_append_section(
    text,
    f'[plugins."datamind-context@{mname}"]',
    'enabled = true\n',
)

config_path.write_text(text, encoding="utf-8")
print(f"Patched {config_path}")
PY

echo
echo "Installed ${PLUGIN_NAME} for Codex (OpenAI desktop app)."
echo "Marketplace name:    ${MARKETPLACE_NAME}"
echo "Marketplace root:    ${MARKETPLACE_ROOT}"
echo "Plugin source:       ${TARGET_DIR}"
echo "DataMind repo root:  ${REPO_ROOT}"
echo "Codex config:        ${CODEX_CONFIG}"
echo
if [[ -f "${REPO_ROOT}/.env" ]]; then
  echo "Before first use, make sure ${REPO_ROOT}/.env contains valid LLM and embedding credentials."
fi
echo
echo "Next step: fully quit Codex (Cmd+Q) and reopen it. The plugin should appear"
echo "          in Codex's plugin list and start automatically."
