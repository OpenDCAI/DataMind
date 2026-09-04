# Changelog

All notable changes to DataMind are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Planned

- HTTP sink adapters for chunk, vector, and graph ingestion.
- Broader MySQL/PostgreSQL validation and deployment examples.
- More recovery and safety evaluations for hook-governed execution.

## [1.0.0] - 2026-09-04

### Added

- StoreAgent and RetrieveAgent as separate, code-enforced roles over five data
  surfaces: KB/RAG, database, graph, Skills, and Memory.
- Scope-typed Memory with `global`, `profile`, and `session` isolation plus
  explicit `kind` and `status` fields.
- Shared `HookChain` with path allow-listing, destructive-SQL confirmation, and
  tamper-evident audit logging on native and SDK tool paths.
- Protocol-neutral native loop for Anthropic Messages and OpenAI Chat
  Completions, with optional Claude Agent SDK + CCR integration.
- Real SSE streaming through FastAPI, profile-scoped storage, idempotent write
  receipts, and packaged browser UI/skill catalog.
- Apache-2.0 repository license, a README inference-time data plane diagram,
  stable API/support/security/concepts documentation, deterministic SQLite
  verification, HTTP API end-to-end coverage, and CI coverage for the pytest
  suite.

### Changed

- Unified package metadata, README, release notes, and release tag at `1.0.0`.
- Marked the native/local profile workflow as the stable core; SDK/CCR and
  remote database dialects are documented as integration-dependent.
- Renamed the old prototype dependency snapshot from `requirements.txt` to
  `requirements-legacy.txt`; current installs use `pyproject.toml` extras.
- Switched packaging metadata to SPDX `Apache-2.0` and removed the deprecated
  license classifier so current setuptools versions can build the project.

### Verification

- `161 passed, 5 skipped` in the repository test suite.
- Source distribution and wheel build successfully; `twine check` passes.
- The five skipped tests exercise the optional `claude_agent_sdk` integration
  when that vendor dependency is not installed.

### Security boundary

- The bundled FastAPI server remains a local/private-network interface, not a
  public authentication gateway. Public deployment requires an authenticated
  reverse proxy, TLS, explicit CORS, rate limits, network isolation, and
  per-profile authorization. See [`docs/SECURITY_BOUNDARIES.md`](docs/SECURITY_BOUNDARIES.md).

## [0.3.0] - 2026-08-31

The previous preview release. Its release note is retained in git history; the
v1.0.0 release note is now the canonical public release summary.

[Unreleased]: https://github.com/OpenDCAI/DataMind/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/OpenDCAI/DataMind/releases/tag/v1.0.0
[0.3.0]: https://github.com/OpenDCAI/DataMind/releases/tag/0.3.0
