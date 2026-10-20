# Read recovery and robustness experiments

DataMind supports opt-in lexical fallback for hybrid retrieval and source-level
build checkpoint reuse. These are execution mechanisms, not guarantees of answer
correctness. Research quality, latency, and cost must be evaluated separately.

## Configure query recovery

```bash
export DATAMIND__RETRIEVAL__STRATEGY=hybrid
export DATAMIND__RETRIEVAL__VECTOR_FAILURE_FALLBACK=bm25
export DATAMIND__RETRIEVAL__VECTOR_RETRIES=1
export DATAMIND__RETRIEVAL__VECTOR_TIMEOUT_S=2
```

Defaults preserve the existing behavior: fallback is `error`, vector retries are
zero, and no additional vector-attempt timeout is imposed. Without managed read
recovery, the embedding provider retains its configured HTTP retry policy.

- A successful vector call returning no hits is not a fault.
- Only transient connectivity/timeouts and retryable service statuses trigger
  read recovery. Auth failures, invalid filters/configuration, missing local
  files, and malformed embedding responses remain errors.
- After the retry budget is exhausted, an enabled fallback searches the same
  lexical corpus with the same metadata filters. Historical queries use the
  requested historical text, never latest text. Published workspace readers use
  their private immutable source/index generation.
- If lexical data cannot be loaded, the error is propagated. An empty lexical
  result is explicitly reported as a degraded empty result, not fabricated facts.
- There is no automatic DB-to-Python or KG-to-document replacement.

For `Workspace.reader(..., read_retries=N)`, the reader's per-tool retry budget
takes precedence over `vector_retries`. Nested vector/tool recovery and the
built-in HTTP embedding provider share that budget rather than multiplying their
retry counts. Standalone hybrid reads use `vector_retries`. Third-party clients
may still have their own retry implementations; configure them explicitly for
fair experiments.

`vector_timeout_s` bounds each vector attempt. `RetrieveAgent.query` and
`Workspace.query` also enforce the configured `agent.wall_clock_timeout_s` across
the whole query, including retries, sleeps and fallback. Direct `KBService`
callers remain responsible for setting an overall deadline. Cancellation and an
exhausted overall deadline never initiate fallback.

## Observe reads and builds

`kb_search` / `kb_search_at_version` return a `retrieval` field even for zero hits.
It includes `degraded`, retry/fallback counts and events (component, attempts,
error type/status, method, outcome and duration). Degraded chunks also carry
`metadata.degraded=true` and `metadata.retrieval_method=bm25`.

Non-streaming query results expose a `recovery` summary; benchmark rows preserve
it. Events are request scoped, and workspace events include the pinned revision.
Nested events overlap in time: do not sum their elapsed durations as query cost.
Streaming consumers can inspect the KB tool result's `retrieval` field; there is
not yet a backend-neutral streaming final recovery summary.

`Workspace.update()` returns `metrics` and saves `build_metrics.json` alongside
the revision. Metrics include completed/reused/computed sources, logical
embedding/extraction calls, checkpoint load/write, compute, source-copy,
materialization, publication and total durations. Completed phase timings do not
include unfinished work in a failed source; total duration includes failures.
Provider-reported token records are retained when available; unavailable usage
is `null`, not an estimate or a claim of zero billed cost. Logical call counters
are not HTTP request counters, and provider-reported usage may omit failed calls.

Build metrics are observability sidecars, not mutable snapshot contents.
Caught failures write a metrics sidecar when possible. An abrupt process kill
cannot finalize metrics; use the external experiment record for that event.
Re-running `update()` creates a new candidate and reuses complete-source
checkpoints. The unfinished source is retried in full. Index materialization
still builds a complete generation; this is compute reuse, not sub-file
maintenance or automatic background task resumption.

## CPU-only smoke experiments

From a checkout with normal DataMind dependencies installed:

```bash
python -m benchmark.robustness query --fixture \
  --failure-probabilities 0 0.1 0.5 1 --repeats 3 --seed 7 \
  --output /tmp/datamind-query-faults

python -m benchmark.robustness build --fixture --after 2 \
  --output /tmp/datamind-build-recovery

python -m benchmark.robustness build --fixture --crash-stage publication \
  --output /tmp/datamind-publication-recovery
```

Output directories must be new. Fixtures replace model/embedding services with
deterministic CPU-only doubles but use actual local snapshot storage. Every
output is marked `fixture_smoke`; these results are not LLM accuracy or model
cost measurements. Fixture extraction reports no token usage.

## Query fault workload

Live mode uses normal Settings/environment. A task file is JSONL, for example:

```json
{"task_id":"q1","question":"What is the budget for Project A?","expected_sources":["budget.md"],"reference_answer":"120"}
```

Labels are never passed to the agent. `expected_sources` measures retrieval
source hit rate; `reference_answer` enables exact string matching only in agent
mode. They are optional. Official benchmark scoring should be applied externally.

```bash
python -m benchmark.robustness query \
  --source /absolute/workspace --questions /absolute/tasks.jsonl \
  --execution agent --surfaces kb,graph,db --component embedding \
  --failure-probabilities 0 0.05 0.1 0.2 --repeats 3 --seed 7 \
  --retries 1 --deadline-s 60 --output /absolute/new-experiment
```

The runner builds one revision and compares `none`, `retry`, and `fallback`
against it. Faults target embedding or vector queries. `--outage-ms` sets an
initial per-query outage; `--fault-delay-ms` sets failed-call latency. Schedules
are paired by seed/task/repeat/attempt across policies. Order is seeded/shuffled.
Reader setup time is separate from execution latency. JSONL rows retain outputs,
fault/attempt counts, errors, usage and recovery metadata; summary completion is
operation completion, not automatically task accuracy. Unlabeled quality rates
are null. Fault timing can interact with model speed; report this explicitly.

The manifest records the revision, pipeline/configuration, code hash (including
untracked Python source), input hashes, seed and fixture/live mode. It omits
credentials and endpoint configuration. Pin dependencies and retain the source
checkout/environment for reproduction. This runner doesn't overwrite or append
to a prior experiment and does not automatically make API-priced claims.

## Build interruption workload

```bash
python -m benchmark.robustness build --source /absolute/workspace \
  --surfaces kb,graph,db --after 10 --worker-timeout-s 600 \
  --questions /absolute/tasks.jsonl --output /absolute/new-build-experiment
```

The worker really exits (`os._exit`) after N source checkpoints or immediately
before publication. The parent checks that no partial revision was published.
It copies the same interrupted state for both policies: `cold` removes source
checkpoints; `resume` retains them. Both rebuild private indexes normally.
Only experiment-owned copies are removed. Source input is never edited.

Results compare recovery-only costs and logical KB/DB/KG content fingerprints.
Optional downstream probes run after each successful recovery. Fixture probes
measure retrieval; live probes invoke the agent. Live extraction is potentially
nondeterministic, so a fingerprint difference is diagnostic, not sufficient proof
of incorrect recovery. Run downstream gold tasks and repeated trials. To measure
crash positions at 25/50/75%, select source counts from the supported inventory
and run each in a fresh output directory. These process-exit tests do not claim
power-loss durability, distributed recovery, or exactly-once external writes.
