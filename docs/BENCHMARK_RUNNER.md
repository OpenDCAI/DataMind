# Benchmark runner

DataMind provides a dataset-neutral benchmark entry point for repeatable,
long-running evaluations. Dataset adapters, official scorers, expected answers,
and task-specific anomaly rules belong to the evaluation project that owns
them, not to `datamind/`.

## Input contract

The runner accepts JSONL records with these fields:

- `question`: required question text;
- `task_id` or `question_id`: stable identity used for checkpoint and resume;
- `final_contract`: optional caller-owned presentation constraints;
- `reference_answer` and `metadata`: optional opaque fields passed through to
  the output for an external evaluator.

Example:

```bash
python -m benchmark.run \
  --questions /path/to/questions.jsonl \
  --output /path/to/artifacts/results.jsonl \
  --surfaces kb,db,graph \
  --concurrency 2
```

The runner writes each completed task as a durable JSONL checkpoint. A sibling
`.manifest.json` records non-secret provenance, including repository revision
and prompt/configuration fingerprints. Existing output is never overwritten.

## Resume

Resume an interrupted run with the same inputs and configuration:

```bash
python -m benchmark.run \
  --questions /path/to/questions.jsonl \
  --output /path/to/artifacts/results.jsonl \
  --resume
```

Resume fails if question, prompt, or configuration fingerprints differ. Start a
new output file instead of combining incompatible runs.

## Operational gates

Increase scope deliberately:

1. run a small deterministic smoke set;
2. run a representative sample across supported data surfaces and answer
   contracts;
3. start a full evaluation only after reviewing failures, runtime, and expected
   API cost.

Stop expansion on protocol errors, index incompatibility, filter leakage,
resource growth, or finalization failures.

## Artifact review

Each result should be auditable from the primary JSONL and manifest: run and
task identity, requested configuration, resolved models, latency, attempts,
usage, stop reason, surfaces, structured tool errors, and normalized evidence.
Apply dataset-specific scoring only in the external evaluation project.
