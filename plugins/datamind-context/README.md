# DataMind Context for Codex

Codex is the main agent. This plugin connects it directly to DataMind's shared
KB, SQL, graph, Skills and Memory tools over MCP. It does not start internal
StoreAgent / RetrieveAgent loops.

```bash
python -m pip install -e .
./scripts/install_codex_plugin.sh
```

The bootstrap resolves the checkout from DATAMIND_REPO_ROOT or its installation
marker. The server is also available in installed wheels as `python -m datamind.mcp`.
The official Codex SDK host uses that same server and procedure catalogue.

Start with `datamind_operation_begin`. Pass operation_id on subsequent calls:

- write mode: fetch `workspace-ingest` / `source-update`, prepare the folder,
  read sources, extract triples with Codex, then workspace_publish;
- read mode: pins a published workspace; fetch `workspace-retrieve`, query KB,
  inspect SQL tables, discover graph_schema, then search entities and traverse;
- pin_workspace=false: explicitly use independent live stores;
- direct graph replacement: graph_replace_source validates the source hash;
  graph_forget_source retracts source support without deleting the file.

Skill fetches persist their version/hash into later write receipts. Inspect every
results[].status before claiming success. Open a fresh read operation after a
publication to read the new revision. Skills and Memory are live context outside
the business-data snapshot.

Graph/SQL/Skills/Memory direct tools require no DataMind conversation key. KB needs
an embedding provider. Local snapshots support SQLite/NetworkX/Chroma/LanceDB.
The new default tool catalogue replaces ask/store/graph_query/workspace_ask with
main-agent direct calls. Python agent APIs remain available for compatibility.

See [architecture and SDK setup](../../README.md).
