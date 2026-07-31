"""Canonical DataMind-Bench v0.1 fixture, workloads, and task suite."""

from pathlib import Path

from benchmarks.registry import BenchmarkRegistry
from benchmarks.schema import load_tasks

from .fixture import build_enterprise_environment, build_faulty_environment
from .scripts import recover_document_script
from .workloads import WORKLOADS


def default_registry() -> BenchmarkRegistry:
    registry = BenchmarkRegistry()
    registry.register_fixture(
        "enterprise-v0",
        build_enterprise_environment,
    )
    registry.register_fixture(
        "enterprise-faulty-v0",
        build_faulty_environment,
    )
    for name, workload in WORKLOADS.items():
        registry.register_workload(name, workload)
    registry.register_script(
        "recover-document-source",
        recover_document_script,
    )
    return registry


def load_v01_tasks():
    return load_tasks(
        Path(__file__).resolve().parents[1] / "tasks" / "v0"
    )


__all__ = ["default_registry", "load_v01_tasks"]
