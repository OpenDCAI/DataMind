# Public deployment security boundary

DataMind v1.0.0 is a local-first inference-time data plane. The bundled
FastAPI server is a useful development and private-network interface; it is
**not an internet-facing authentication or authorization gateway**.

## What the server currently assumes

The default `datamind.server:app` configuration:

- exposes `/api/ask`, `/api/store`, `/api/chat`, `/api/upload`, and inspection
  endpoints without application-level user authentication;
- allows `CORS` from `*` so the local browser UI works without setup;
- accepts a caller-provided `X-Session-Id` for conversation scoping, but that
  header is not an identity proof;
- stores profile data, SQLite databases, graph files, indexes, uploads, and
  audit logs on the server filesystem;
- lets StoreAgent perform writes within its registered catalogue;
- can ask for confirmation on destructive SQL, but the API itself is not a
  human approval or policy-management service;
- caps an individual upload at 25 MiB and strips directory components from the
  uploaded filename, but does not replace malware scanning or content policy.

The HookChain is an important execution safeguard, not a perimeter control.
Keep it enabled (`DATAMIND__HOOKS__ENABLED=true`) and treat a profile as an
organization boundary, not as authentication or tenant isolation.

## Safe exposure levels

| Exposure | Recommended use | Boundary |
|---|---|---|
| Loopback (`127.0.0.1`) | Developer laptop, local demo, CI | Safest default; no public ingress |
| Private network / VPN | Small trusted team | Put an authenticated reverse proxy in front; isolate the profile and database |
| Public internet | Only with additional controls | The bare bundled server is not sufficient |

## Minimum controls before public exposure

Place DataMind behind a maintained edge service or reverse proxy that provides:

1. TLS with certificate rotation and HTTP security headers.
2. Authentication and per-user/per-tenant authorization for every API route,
   especially `/api/store`, `/api/upload`, `/api/kb/reindex`, and
   `/api/memory/{namespace}`.
3. An explicit CORS origin allow-list; never retain `*` for a browser-facing
   public deployment.
4. Request, upload, response, and concurrency limits plus upstream model
   spending quotas.
5. Structured access logs and alerting at the proxy and application layers.
6. A separate profile/database per trust boundary, with backups and a tested
   restore path.
7. Network egress restrictions for the model gateway, database, CCR, and any
   embedding provider.
8. Secret injection through the deployment platform; never expose API keys to
   the browser, commit `.env.datamind`, or put real keys in CCR config checked
   into source control.

## Data and tool policy

- Keep `DATAMIND__DB__READ_ONLY=true` unless a narrowly scoped write path has
  been reviewed. StoreAgent imports should target a controlled database or
  staging profile, not an operator's production connection by default.
- Keep `PathAllowlistHook`, `DestructiveSqlHook`, and `AuditLogHook` enabled.
  Review any `DATAMIND__HOOKS__PATH_ALLOWLIST_EXTRA` entry as a filesystem grant.
- Do not use a shared public profile for unrelated customers. Profile names are
  paths, not authorization claims.
- Treat uploaded files and retrieved text as untrusted input. Add malware
  scanning, sensitive-data filtering, retention, and content-size controls at
  the edge or ingestion boundary.
- Review audit logs as operational records, but do not assume they are a
  complete compliance trail until log shipping, retention, and access control
  are configured.
- The SDK backend uses `bypassPermissions` for its isolated process, while
  disabling unrelated shell/filesystem tools and exposing only the DataMind MCP
  catalogue. This is not a reason to run it on an untrusted public endpoint;
  keep the SDK/CCR process private and validate the vendor stack separately.

## Explicit non-goals

The v1.0 server does not provide built-in SSO, API key issuance, role-based
access control, quota accounting, malware scanning, secret rotation, database
row-level security, or a durable human approval queue. Add those controls
before describing a deployment as a public multi-tenant service.

For a private demo, bind to loopback:

```bash
python -m uvicorn datamind.server:app --host 127.0.0.1 --port 8000
```

For deployment review, pair this page with the [stable API contract](./STABLE_API.md)
and the [native / SDK support matrix](./SUPPORT_MATRIX.md).
