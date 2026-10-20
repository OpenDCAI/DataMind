"""Reproducible local fault experiments (fixtures are never quality results).

    python -m benchmark.robustness query --fixture --output /tmp/query-faults
    python -m benchmark.robustness build --fixture --after 2 --output /tmp/build-faults

Live mode uses Settings/environment and requires --source and --questions for
queries. JSONL tasks may contain question, task_id, filters, expected_sources,
and reference_answer. Labels are used only by the external scoring code below.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from datamind.config import Settings
from datamind.workspace import Workspace

REPO = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def append_row(path, value):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def provenance(args, source):
    def git(*cmd):
        return subprocess.run(["git", *cmd], cwd=REPO, capture_output=True).stdout
    return {
        "schema_version": 1, "command": args.command, "fixture": args.fixture,
        "result_kind": "fixture_smoke" if args.fixture else "live_experiment",
        "git_commit": git("rev-parse", "HEAD").decode().strip(),
        "tracked_diff_hash": hashlib.sha256(git("diff", "HEAD")).hexdigest(),
        "code_hash": digest([(str(p.relative_to(REPO)), hashlib.sha256(p.read_bytes()).hexdigest())
                             for package in ("datamind", "benchmark")
                             for p in sorted((REPO / package).rglob("*.py"))]),
        "python": platform.python_version(), "seed": getattr(args, "seed", None),
        "source_hash": digest([(str(p.relative_to(source)), hashlib.sha256(p.read_bytes()).hexdigest())
                               for p in sorted(source.rglob("*")) if p.is_file()]),
        "questions_hash": hashlib.sha256(Path(args.questions).read_bytes()).hexdigest() if args.questions else None,
        # Only harness arguments, never Settings secrets/endpoints.
        "harness": vars(args),
    }


def setup_fixture():
    """Deterministic CPU-only providers with real local snapshot storage."""
    from datamind.agent import options

    class Embed:
        name = "robustness-fixture"
        dimension = 8

        async def embed_texts(self, texts):
            return [[float(i == int(hashlib.sha256(t.encode()).hexdigest()[:4], 16) % 8)
                     for i in range(8)] for t in texts]

        async def embed_query(self, text):
            return (await self.embed_texts([text]))[0]

    class Model:
        async def generate_text(self, prompt, **kwargs):
            return '[{"subject":"Fixture","relation":"owns","object":"Demo"}]'

        async def complete(self, **kwargs):
            raise RuntimeError("Fixture mode evaluates retrieval, not LLM answer quality")

        async def aclose(self):
            pass

    options.build_embedding = lambda *a, **k: Embed()
    options.build_model_client = lambda *a, **k: Model()


def settings_for(runtime, fixture, args=None):
    settings = (Settings(_env_file=None, llm={"api_key": "fixture", "model": "fixture"})
                if fixture else Settings())
    settings.data.base_dir = Path(runtime).resolve()
    settings.retrieval.strategy = "hybrid"
    settings.retrieval.vector_retries = 0  # Reader owns the one shared retry budget.
    settings.retrieval.vector_failure_fallback = "error"
    if fixture:
        settings.llm.api_key = "fixture"
        settings.llm.model = "fixture"
        settings.llm.fallback_model = None
        settings.embedding.dimension = 8
        settings.embedding.model = "fixture"
        settings.kb.vector_store = "chroma"
        settings.kb.ingest_mode = "direct"
        settings.graph.backend = "networkx"
        settings.db.dialect = "sqlite"
        settings.db.dsn = None
        setup_fixture()
    if args is not None and getattr(args, "deadline_s", None):
        settings.agent.wall_clock_timeout_s = args.deadline_s
    return settings


def source_and_tasks(args, output):
    if args.source:
        source = Path(args.source).resolve()
        if not source.is_dir():
            raise ValueError("--source must be a directory")
        if output == source or output.is_relative_to(source):
            raise ValueError("--output must be outside the source workspace")
    elif args.fixture:
        source = output / "inputs"
        source.mkdir()
        for i, term in enumerate(("nebula", "orchid", "cobalt", "quartz", "maple", "harbor")):
            (source / f"doc{i}.md").write_text(f"{term} project budget is {i + 10} units.\n")
        (source / "orders.csv").write_text("item,amount\nA,12\nB,24\n")
    else:
        raise ValueError("Live experiments require --source")
    if args.questions:
        tasks = [json.loads(line) for line in Path(args.questions).read_text().splitlines() if line.strip()]
        for i, task in enumerate(tasks):
            if not isinstance(task.get("question"), str):
                raise ValueError(f"task {i} needs a question")
            task.setdefault("task_id", str(i))
    elif args.fixture:
        tasks = [{"task_id": "0", "question": "nebula", "expected_sources": ["doc0.md"]},
                 {"task_id": "1", "question": "orchid", "expected_sources": ["doc1.md"]}]
    else:
        tasks = []
    return source, tasks


def score(task, result, execution):
    """No LLM judge or heuristic pretending to be official benchmark accuracy."""
    expected = task.get("expected_sources")
    observed = [r.get("source") for r in result.get("results", [])] if execution == "retrieval" else [
        e.get("locator", {}).get("source") or e.get("source_id") for e in result.get("evidence", [])]
    source_hit = None if not expected else any(
        s and (s in expected or Path(s).name in expected) for s in observed)
    reference = task.get("reference_answer")
    exact = None if execution != "agent" or reference is None else str(result.get("answer", "")).strip() == str(reference).strip()
    return {"source_hit": source_hit, "exact_answer_match": exact}


class Faults:
    """Per-task schedules are paired across policies; retries consume later events."""
    def __init__(self, seed, task, probability, outage_ms, delay_ms):
        self.seed, self.task, self.p = seed, task, probability
        self.started = time.monotonic()
        self.outage = outage_ms / 1000
        self.delay = delay_ms / 1000
        self.attempts, self.injected = 0, 0

    async def before(self):
        attempt = self.attempts
        self.attempts += 1
        rng = random.Random(f"{self.seed}:{self.task}:{attempt}")
        if time.monotonic() - self.started < self.outage or rng.random() < self.p:
            self.injected += 1
            await asyncio.sleep(self.delay)
            raise TimeoutError("injected transient fault")


def install_fault(reader, faults, component):
    retriever = reader._system.services.kb.retriever
    if component == "embedding":
        original = retriever._embed.embed_query
        async def failing(query):
            await faults.before()
            return await original(query)
        retriever._embed.embed_query = failing
    else:
        original = retriever._store.query
        async def failing(*a, **k):
            await faults.before()
            return await original(*a, **k)
        retriever._store.query = failing


async def query_experiment(args, output, source, tasks):
    if not tasks:
        raise ValueError("Query experiments require --questions (or --fixture)")
    if args.fixture and args.execution != "retrieval":
        raise ValueError("Fixture mode cannot evaluate LLM answers; use --execution retrieval")
    settings = settings_for(output / "runtime", args.fixture, args)
    workspace = Workspace(settings)
    built = await workspace.update(str(source), surfaces=args.surfaces.split(","))
    manifest = provenance(args, source)
    manifest.update(workspace_revision=built["workspace_revision"], pipeline_hash=built["pipeline_hash"],
                    retrieval=settings.retrieval.model_dump(),
                    model=settings.llm.model, embedding_model=settings.embedding.model,
                    scoring="source-hit / exact string match only; use official scorers for paper results")
    write_json(output / "manifest.json", manifest)
    write_json(output / "build.json", built)
    rows = []
    # Seeded task/policy ordering limits systematic cold/warm and order effects.
    jobs = [(repeat, task, policy, p) for repeat in range(args.repeats) for task in tasks
            for policy in ("none", "retry", "fallback") for p in args.failure_probabilities]
    random.Random(args.seed).shuffle(jobs)
    for repeat, task, policy, probability in jobs:
        workspace.settings.retrieval.vector_failure_fallback = "bm25" if policy == "fallback" else "error"
        reader_started = time.monotonic()
        row = {"task_id": task["task_id"], "repeat": repeat, "policy": policy,
               "failure_probability": probability, "component": args.component,
               "workspace_revision": built["workspace_revision"], "fixture": args.fixture,
               "source_hit": False if task.get("expected_sources") else None,
               "exact_answer_match": False if task.get("reference_answer") is not None and args.execution == "agent" else None}
        try:
            async with workspace.reader(built["workspace_revision"], read_retries=0 if policy == "none" else args.retries) as reader:
                row["reader_setup_ms"] = (time.monotonic() - reader_started) * 1000
                faults = Faults(args.seed + repeat, str(task["task_id"]), probability,
                                args.outage_ms, args.fault_delay_ms)
                install_fault(reader, faults, args.component)
                started = time.monotonic()
                try:
                    async with asyncio.timeout(args.deadline_s):
                        result = await (reader.call("kb_search", query=task["question"], filters=task.get("filters"))
                                        if args.execution == "retrieval" else reader.query(task["question"]))
                    row.update(outcome="ok", result=result, **score(task, result, args.execution))
                except Exception as exc:
                    row.update(outcome="error", error_type=type(exc).__name__, recovery=getattr(exc, "datamind_recovery", {}), source_hit=False if task.get("expected_sources") else None,
                               exact_answer_match=False if task.get("reference_answer") is not None and args.execution == "agent" else None)
                row.update(latency_ms=(time.monotonic() - started) * 1000,
                           injected_faults=faults.injected, component_attempts=faults.attempts)
        except Exception as exc:
            row.update(outcome="reader_error", error_type=type(exc).__name__)
        rows.append(row)
        append_row(output / "rows.jsonl", row)
    groups = []
    for probability in args.failure_probabilities:
        for policy in ("none", "retry", "fallback"):
            batch = [r for r in rows if r["policy"] == policy and r["failure_probability"] == probability]
            latencies = sorted(r["latency_ms"] for r in batch if "latency_ms" in r)
            groups.append({"policy": policy, "failure_probability": probability, "tasks": len(batch),
                           "completion_rate": sum(r["outcome"] == "ok" for r in batch) / len(batch),
                           "p95_latency_ms": latencies[math.ceil(.95 * len(latencies)) - 1] if latencies else None,
                           **{key + "_rate": (sum(r.get(key) is True for r in batch) / sum(r.get(key) is not None for r in batch))
                              if any(r.get(key) is not None for r in batch) else None for key in ("source_hit", "exact_answer_match")}})
    write_json(output / "summary.json", {"fixture": args.fixture, "groups": groups})


async def snapshot_content(workspace, revision):
    """Logical output fingerprint, excluding physical index IDs and timings."""
    manifest = workspace.manifest(revision)
    contents = {"sources": [(s["uri"], s["version"]) for s in manifest["sources"]]}
    async with workspace.reader(revision) as reader:
        services = reader._system.services
        if services.kb:
            contents["kb"] = sorted(await services.kb.vector_store.get_all_texts(), key=lambda x: x[0])
        if services.graph:
            graph = json.loads(services.graph.store._path.read_text())
            contents["graph"] = {k: sorted(v, key=lambda x: json.dumps(x, sort_keys=True)) for k, v in graph.items()}
        if services.db:
            # Local SQLite workspace API: read all rows, not a row-limit sample.
            import sqlite3
            with sqlite3.connect(services.db.engine.url.database) as conn:
                contents["db"] = {}
                for table, in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                    quoted = '"' + table.replace('"', '""') + '"'
                    contents["db"][table] = sorted(conn.execute(f"SELECT * FROM {quoted}").fetchall(), key=str)
    return digest(contents)


async def build_worker(args):
    import datamind.workspace as module
    settings = settings_for(args.runtime, args.fixture)
    workspace = Workspace(settings)
    original = module._atomic_json
    completed = 0
    def injected(path, value, **kwargs):
        nonlocal completed
        original(path, value, **kwargs)
        if path.parent.name == "checkpoints":
            completed += 1
            if args.crash_stage == "checkpoint" and completed == args.after:
                os._exit(23)
        if args.crash_stage == "publication" and path.name == "manifest.json":
            os._exit(23)  # Snapshot prepared, head has not moved.
    module._atomic_json = injected
    result = await workspace.update(args.source, surfaces=args.surfaces.split(","))
    fingerprint = await snapshot_content(workspace, result["workspace_revision"])
    probes = []
    if args.questions:
        tasks = [json.loads(line) for line in Path(args.questions).read_text().splitlines() if line.strip()]
        async with workspace.reader(result["workspace_revision"]) as reader:
            for i, task in enumerate(tasks):
                row = {"task_id": task.get("task_id", str(i))}
                try:
                    execution = "retrieval" if args.fixture else "agent"
                    response = (await reader.call("kb_search", query=task["question"], filters=task.get("filters"))
                                if args.fixture else await reader.query(task["question"]))
                    row.update(outcome="ok", result=response, **score(task, response, execution))
                except Exception as exc:
                    row.update(outcome="error", error_type=type(exc).__name__)
                probes.append(row)
    write_json(Path(args.worker_result), {"build": result, "content_fingerprint": fingerprint,
                                         "probes": probes})


def run_worker(args, runtime, result, crash_stage="none"):
    command = [sys.executable, "-m", "benchmark.robustness", "_build_worker",
               "--source", args.source, "--runtime", str(runtime), "--worker-result", str(result),
               "--surfaces", args.surfaces, "--crash-stage", crash_stage, "--after", str(args.after)]
    if args.fixture:
        command.append("--fixture")
    if args.questions:
        command.extend(["--questions", args.questions])
    with result.with_suffix(".log").open("w") as log:
        return subprocess.run(command, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)},
                              stdout=log, stderr=log, timeout=args.worker_timeout_s).returncode


def build_experiment(args, output, source):
    args.source = str(source)
    settings = settings_for(output / "crashed", args.fixture)
    count = len(Workspace(settings)._inventory(source))
    if args.crash_stage == "checkpoint" and not 1 <= args.after <= count:
        raise ValueError(f"--after must be between 1 and {count} supported sources")
    write_json(output / "manifest.json", provenance(args, source))
    crashed = output / "crashed"
    code = run_worker(args, crashed, output / "interrupted.json", args.crash_stage)
    if code != 23:
        raise RuntimeError(f"expected injected process exit 23, got {code}; inspect interrupted.log")
    assert Workspace(settings).current_revision is None, "partial build was published"
    results = {}
    for policy in ("cold", "resume"):
        runtime = output / policy
        shutil.copytree(crashed, runtime)
        if policy == "cold":
            shutil.rmtree(runtime / "storage" / settings.data.profile / "workspace" / "checkpoints", ignore_errors=True)
        result_path = output / f"{policy}.json"
        code = run_worker(args, runtime, result_path)
        if code:
            raise RuntimeError(f"{policy} recovery failed with {code}; inspect {policy}.log")
        results[policy] = json.loads(result_path.read_text())
    write_json(output / "summary.json", {
        "fixture": args.fixture, "crash_stage": args.crash_stage, "after_sources": args.after,
        "cold_metrics": results["cold"]["build"]["metrics"],
        "resume_metrics": results["resume"]["build"]["metrics"],
        "logical_content_equal": results["cold"]["content_fingerprint"] == results["resume"]["content_fingerprint"],
        "cold_probes": results["cold"]["probes"], "resume_probes": results["resume"]["probes"],
        "note": "Recovery-only costs. Live model extraction can be nondeterministic; assess quality with downstream tasks too.",
    })


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("query", "build", "_build_worker"):
        p = sub.add_parser(command)
        p.add_argument("--fixture", action="store_true", help="CPU-only smoke fixtures, never paper quality results")
        p.add_argument("--source")
        p.add_argument("--questions")
        p.add_argument("--surfaces", default="kb,graph,db" if command != "query" else "kb")
        if command == "_build_worker":
            p.add_argument("--runtime", required=True)
            p.add_argument("--worker-result", required=True)
        else:
            p.add_argument("--output", required=True, help="new directory; never overwrite an experiment")
        if command in {"build", "_build_worker"}:
            p.add_argument("--after", type=int, default=2)
            p.add_argument("--crash-stage", choices=["none", "checkpoint", "publication"], default="checkpoint" if command == "build" else "none")
        if command == "build":
            p.add_argument("--worker-timeout-s", type=float, default=300.)
        if command == "query":
            p.add_argument("--seed", type=int, default=0)
            p.add_argument("--execution", choices=["retrieval", "agent"], default="retrieval")
            p.add_argument("--component", choices=["embedding", "vector"], default="embedding")
            p.add_argument("--failure-probabilities", type=float, nargs="+", default=[0., .1, .5, 1.])
            p.add_argument("--outage-ms", type=float, default=0.)
            p.add_argument("--fault-delay-ms", type=float, default=0.)
            p.add_argument("--retries", type=int, default=1)
            p.add_argument("--repeats", type=int, default=3)
            p.add_argument("--deadline-s", type=float, default=30.)
    return root


def main():
    args = parser().parse_args()
    if args.questions:
        args.questions = str(Path(args.questions).resolve())
    if args.command == "_build_worker":
        asyncio.run(build_worker(args))
        return
    if args.command == "query":
        if any(not 0 <= p <= 1 for p in args.failure_probabilities) or not 0 <= args.retries <= 5:
            raise ValueError("probabilities must be in [0,1]; retries must be in [0,5]")
        if args.repeats < 1 or args.deadline_s <= 0 or min(args.outage_ms, args.fault_delay_ms) < 0:
            raise ValueError("invalid repeats/deadline/fault duration")
        if "kb" not in args.surfaces.split(","):
            raise ValueError("query fault experiments require the kb surface")
    elif args.crash_stage == "none":
        raise ValueError("build experiment requires checkpoint or publication fault")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source, tasks = source_and_tasks(args, output)
    if args.command == "query":
        asyncio.run(query_experiment(args, output, source, tasks))
    else:
        build_experiment(args, output, source)
    print(output / "summary.json")


if __name__ == "__main__":
    main()
