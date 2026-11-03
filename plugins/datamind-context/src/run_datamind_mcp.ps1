# PowerShell launcher for the DataMind MCP server.
#
# Mirrors src/run_datamind_mcp.sh on Unix:
#   1. Resolve the DataMind runtime root.
#   2. Pick the Python interpreter:
#        $env:DATAMIND_PYTHON   ->   <repo>\.venv\Scripts\python.exe   ->   `python`
#   3. Exec datamind_mcp.py with the chosen interpreter.
#
# Designed to be invoked via mcp.json with:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File <path-to-this-script>
#
# Stdio is passed through to the spawned Python process so the MCP host can
# speak JSON-RPC over stdin/stdout.

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginDir = (Resolve-Path (Join-Path $ScriptDir '..')).Path

$RepoRoot = $env:DATAMIND_REPO_ROOT
$RepoMarker = Join-Path $PluginDir '.datamind-repo-root'

if (-not $RepoRoot -and (Test-Path $RepoMarker)) {
    $RepoRoot = (Get-Content -Path $RepoMarker -TotalCount 1).Trim()
}

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PluginDir '..\..')).Path
}

# Settings loads .env/.env.datamind relative to the process working
# directory. Codex starts plugins from its cache directory, so use the
# resolved DataMind checkout as the working root.
Set-Location -Path $RepoRoot

$McpScript = Join-Path $PluginDir 'src\datamind_mcp.py'

# 1. Explicit override.
if ($env:DATAMIND_PYTHON -and (Test-Path $env:DATAMIND_PYTHON)) {
    & $env:DATAMIND_PYTHON $McpScript
    exit $LASTEXITCODE
}

# 2. Bundled / repo-local venv.
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (Test-Path $VenvPython) {
    & $VenvPython $McpScript
    exit $LASTEXITCODE
}

# 3. Fallback to whatever `python` resolves to in PATH.
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    $Python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $Python) {
    Write-Error "No Python interpreter found. Set `$env:DATAMIND_PYTHON or install Python 3."
    exit 1
}

if ($Python.Name -eq 'py.exe' -or $Python.Name -eq 'py') {
    & $Python.Source -3 $McpScript
} else {
    & $Python.Source $McpScript
}
exit $LASTEXITCODE
