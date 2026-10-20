# Automated verification

Every pull request runs `.github/workflows/python-ci.yml`. The final **CI gate** succeeds only when every required job succeeds; failed, cancelled or skipped jobs fail the gate.

| Job | What it verifies |
| --- | --- |
| Python 3.11 / 3.12 | Package and local Codex plugin tests, compilation, deterministic SQLite demo |
| Optional providers | LanceDB history, CocoIndex ingestion and version-provider regressions; no skipped tests allowed |
| MySQL and Dolt integration | Real MySQL 8.4 and checksum-pinned Dolt 2.5.0: import/update/delete, historical reads and rollback; no skipped tests allowed |
| Installed wheel | Build, install into a fresh environment, import default/optional-provider modules without optional dependencies, verify bundled UI and Skill resources outside the checkout |

The full package suite covers DB query safeguards, KB retrieval, graph provenance and persistence, agent/tool contracts, HTTP endpoints, skills, memory and receipts. Workspace-specific tests cover source reuse, failed publication, deletion/rollback, concurrent publication conflicts, pinned reads during updates and actual process termination followed by recovery.

Run the default suite locally:

```sh
python -m pip install -e ".[dev]"
python -m pytest -q datamind/tests plugins/datamind-context/tests
python -m datamind.scripts.verify_sqlite_demo
```

Optional backend tests need `.[dev,cocoindex,mysql,dolt]`, the Dolt executable on PATH, and `DATAMIND_TEST_MYSQL_DSN` pointing to a disposable server where tests may create and drop databases. Run `python scripts/ci_required_tests.py -q datamind/tests/test_db_ingest.py datamind/tests/test_db_versioning.py` to fail instead of silently skipping unavailable backends.

These are deterministic functional and recovery checks. Passing them does not prove behavior with every model/provider configuration, production-scale concurrency, multi-host deployments or arbitrary datasets. Live model evaluations and the research quality/cost/load benchmarks are separate acceptance work. SQLite/MySQL/LanceDB transactions and source replacement do not make arbitrary external append operations exactly once.
