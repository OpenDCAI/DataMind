# Codex SDK deployment with a model gateway

Codex needs the Responses API, including streaming and tool-call continuation.
A successful `/v1/models` request alone is not a deployment readiness check.

Configure the gateway using SDK process overrides; keep credentials in the host
secret store or environment, never in tracked configuration:

```python
import json
import os
from datamind.codex import CodexSession

provider = {
    'model_provider': 'gateway',
    'model_providers.gateway.name': 'Team gateway',
    'model_providers.gateway.base_url': os.environ['DATAMIND_GATEWAY_URL'],
    'model_providers.gateway.env_key': 'DATAMIND_GATEWAY_API_KEY',
    'model_providers.gateway.wire_api': 'responses',
}

async def run():
    async with CodexSession(
        workspace='/srv/workspace', data_root='/srv/datamind', profile='team-a',
        model=os.environ['DATAMIND_CODEX_MODEL'],
        config_overrides=tuple(f'{key}={json.dumps(value)}' for key, value in provider.items()),
        env={'DATAMIND_GATEWAY_API_KEY': os.environ['DATAMIND_GATEWAY_API_KEY']},
    ) as session:
        return (await session.run('Inspect the workspace using DataMind.')).final_response
```

The default approval mode is `auto_review`. That mode can call the additional
`codex-auto-review` model; a gateway may expose the main model without exposing
this reviewer. Such a failure must not be reported as successful ingestion.

For an unattended application with an existing authorization decision, the
trusted host may pass `approval_mode='deny_all'` plus an explicit
`preapproved_tools=('datamind_operation_begin', ...)` list for those authorized
operations. Only listed DataMind tools receive per-tool preapproval; other
approval requests are denied. No tools are preapproved by default. Use separate
profiles and scoped workspace paths, and authorize the operation before building
that list. DataMind path, profile and read/write checks still apply.

KB embeddings are independent of the main Codex model. If the gateway has no
embedding channel, configure a separate embedding service or a local model:

```bash
pip install -e '.[codex,huggingface]'
export DATAMIND__EMBEDDING__PROVIDER=huggingface
export DATAMIND__EMBEDDING__MODEL=BAAI/bge-small-zh-v1.5
```

A live readiness check should use synthetic data to verify actual tool writes,
receipt statuses, SQL/graph answers, embedding-backed KB search, atomic
publication, and an old reader surviving a new publication. CI covers deterministic
contracts and offline SDK tool discovery; it does not spend gateway inference
credits or prove a particular external gateway's availability.
