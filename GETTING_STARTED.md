# Getting started with DataMind 2.0

DataMind provides the data layer. Your local Codex or the official Codex SDK
is the main agent for both ingestion and retrieval.

## Install

```bash
git clone https://github.com/OpenDCAI/DataMind.git
cd DataMind
python -m venv .venv
source .venv/bin/activate
pip install -e '.[codex]'
cp .env.datamind.example .env.datamind
```

For personal use, install the local Codex plugin:

```bash
./scripts/install_codex_plugin.sh --force --skip-deps
```

Reload Codex after installing. The plugin exposes direct MCP data tools and an
ingestion Skill; it does not launch another agent. Local Codex uses its own
model configuration and authentication.

For CLI/HTTP hosting, authenticate the official SDK runtime or configure
`DATAMIND__CODEX__BASE_URL`, `DATAMIND__CODEX__API_KEY`, and optionally
`DATAMIND__CODEX__MODEL`. The endpoint must support the Responses API.
See [gateway deployment](docs/CODEX_GATEWAY_DEPLOYMENT.md) for tool approvals,
timeouts, and an explicit trusted-tool allow-list. Do not reuse the removed
`DATAMIND__AGENT__BACKEND=native|sdk` settings; the host is Codex.

## Configure embeddings separately

Graph, SQL, Skills and Memory do not need a DataMind conversation-model key.
KB needs an embedding provider; a model gateway may not offer embeddings.
For local embeddings:

```bash
pip install -e '.[huggingface]'
export DATAMIND__EMBEDDING__PROVIDER=huggingface
export DATAMIND__EMBEDDING__MODEL=BAAI/bge-small-zh-v1.5
export DATAMIND__EMBEDDING__DIM=512
```

The first use downloads model weights. Otherwise set the embedding URL, model,
dimension, and API key in `.env.datamind.example` for your provider.

## CLI

```bash
python -m datamind info
python -m datamind.scripts.verify_sqlite_demo
python -m datamind.scripts.hello_codex
python -m datamind ask "Which projects are owned by Alice?" --show-tools
python -m datamind store "Remember: weekly reports use Chinese."
python -m datamind chat
```

`info` and the SQLite demo do not call a model. `ask`, `store`, and `chat`
host the same official Codex runtime. `ask` is read-only; `store` permits
writes. In chat, `/store <instruction>` permits writes for that turn, `/new`
clears history, and `/exit` leaves. Inspect every business-write receipt's
per-surface status; an ID alone does not mean success.

The root launcher `python main.py` opens the same chat; other CLI arguments
can also be passed through it.

## HTTP and browser UI

```bash
python -m uvicorn datamind.server:app --host 127.0.0.1 --port 8000
```

`python server.py` and `uvicorn server:app` launch/export the same application.

Open [the local UI](http://127.0.0.1:8000). Uploading only saves a file; the
import action asks Codex to inspect and ingest it. The same main agent handles
ordinary queries with read-only data access.

```bash
curl -s localhost:8000/api/health
curl -s localhost:8000/api/tools
curl -s -X POST localhost:8000/api/ask \
  -H 'Content-Type: application/json' -d '{"message":"Find the project owner"}'
curl -N -X POST localhost:8000/api/chat \
  -H 'Content-Type: application/json' -d '{"message":"Find the project owner"}'
curl -s -X POST localhost:8000/api/store \
  -H 'Content-Type: application/json' -d '{"message":"Remember: release day is Thursday"}'
```

The server is a private/local adapter and has no built-in authentication.
See [deployment boundaries](docs/SECURITY_BOUNDARIES.md) before exposing it.

## Coordinated workspace updates

Ask Codex to fetch `workspace-ingest`, prepare a source inventory, extract
source-backed graph triples, and publish selected KB/DB/Graph surfaces together.
Publication produces a manifest and switches one workspace head. A read request
pins the published revision when it starts, including across later tool calls.
Before the first publication, reads can inspect independent live stores.
Independent direct writes do not silently update published snapshots.

The previous `build_datamind`, StoreAgent/RetrieveAgent, Claude SDK and CCR
product paths were removed in 2.0. Use [the migration guide](docs/LEGACY_RUNTIME.md)
and [public API](docs/STABLE_API.md) when updating integrations.
