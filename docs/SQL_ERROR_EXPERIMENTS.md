# SQL error and recovery experiments

Run the complete script from the repository root:

```bash
python -m benchmark.sql_errors --output /tmp/new-sql-errors
```

The output directory must not already exist. The script only creates and queries
experiment-owned local files. It never connects to MySQL/PostgreSQL, calls a
model, or edits an existing workspace.

SQLite needs no optional driver. To include actual MySQL/PostgreSQL exception
classes, install the existing optional extras in your development environment:

```bash
python -m pip install -e '.[mysql,postgres]'
python -m benchmark.sql_errors --require-drivers --output /tmp/new-all-sql-errors
```

Without these extras, corresponding rows are explicitly `skipped`, never passed.
`--require-drivers` returns a nonzero exit status if any case was skipped.

## Coverage

The default matrix has 440 checks when both optional drivers are installed:

- 35 error types, raw and SQLAlchemy-wrapped exceptions, no-retry and retry
  policies, and probabilities 0, 0.3 and 1: 420 simulated-error checks.
- Five native SQLite scenarios, raw/wrapped errors and both policies: 20 checks.

| Backend | Error types |
|---|---|
| SQLite | BUSY, BUSY_SNAPSHOT, LOCKED, SQL error, read-only, interrupt, corruption, cannot-open, constraint, authorization |
| MySQL | Lock timeout, deadlock, server gone, connection lost, authentication, unknown database, permission, syntax, missing table, missing column, duplicate key |
| PostgreSQL | Connection failure, serialization failure, deadlock, startup, administrator termination, lock not available, authentication, permission, syntax, missing table, missing column, duplicate key, cancellation, protocol violation |

`postgres_admin_shutdown` (57P01) and `postgres_lock_not_available` (55P03)
cover omissions discovered against real PostgreSQL. Their labels currently
expect no retry, matching the existing recovery allowlist. A passed check for
these cases documents current behavior, not successful recovery or resolution of
the policy gap. This coverage change does not modify that allowlist.

```bash
python -m benchmark.sql_errors --backend postgresql \
  --errors postgres_admin_shutdown postgres_lock_not_available \
  --require-drivers --output /tmp/new-postgres-gap-checks
```

SQLite simulated cases inject errors at DataMind's common read executor, then
execute real local SELECTs when the attempt is allowed through. MySQL/PostgreSQL
rows are `execution_kind: driver_contract_simulation`: they use real PyMySQL or
Psycopg exception objects and a synthetic successful return value. Their
`sql_executions` and `result_match` are null. These checks validate exception
classification and recovery control flow, not server behavior or adapter support.

Native SQLite scenarios execute invalid SQL for syntax, missing-table and
missing-column errors. Two more scenarios hold an actual `BEGIN EXCLUSIVE`
lock from another connection: one releases the lock after a failed read, allowing
retry to recover; the other holds it until the retry budget is exhausted.
These rows are labeled `native_sql_error` or `real_lock`; no probability-based
exception is injected into them.

## Options

```bash
# SQLite only, with two retries and more samples
python -m benchmark.sql_errors --backend sqlite --retries 2 --repeats 5 \
  --failure-probabilities 0 0.1 0.3 0.5 1 --output /tmp/new-sqlite-sweep

# Selected cases; no native SQLite scenarios
python -m benchmark.sql_errors --errors sqlite_busy mysql_deadlock postgres_deadlock \
  --wrapping sqlalchemy --skip-native --output /tmp/new-selected-errors

# A temporary outage followed by normal operation
python -m benchmark.sql_errors --backend sqlite --errors sqlite_busy --skip-native \
  --failure-probabilities 0 --outage-ms 80 --retries 3 --output /tmp/new-outage-check
```

`--fault-delay-ms` delays injected failures inside the read timeout/deadline.
`--deadline-s` bounds each recovery operation. Outage and probability conditions
are combined with OR, matching KB behavior. There is no fail-first-N mode.
State files and task-local contexts preserve the probability sequence; policies
start from the same seeded sequence but retries consume subsequent events.
The native lock-release scenario is an actual unlock operation, not an additional
fault-scheduling mode.

## Reports and interpretation

- `rows.jsonl`: individual checks, observed codes, classification, attempt and
  retry counts, operation success, execution kind, and timing.
- `results.csv`: the same rows for spreadsheet analysis.
- `REPORT.md`: checks passed, failed and skipped by backend/error/wrapping.
- `summary.json`: aggregate check counts and explicit server-integration status.
- `manifest.json`: arguments, commit, source-code hash and runtime versions.

A check **passes** when behavior matches expectations. For example, a syntax
error should fail the query without retrying; that is a passed check with
`operation_succeeded: false`. It is not a recovered query. Classification labels
and the probability-only schedule oracle are independent of the recovery
implementation. Timing-window scenarios check classification, bounded attempts
and successful-result content without assuming a particular elapsed-time order.

Exit status is nonzero for a failed check, invalid arguments, unavailable required
drivers, or infrastructure failure. Rows are written incrementally. Reports
distinguish simulated errors from native errors and real locks; do not treat
this script's check-pass rate as benchmark task accuracy or recovery success rate.
