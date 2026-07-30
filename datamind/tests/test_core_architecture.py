"""Dependency direction tests for the new Core boundary."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


CORE_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_TOP_LEVEL_IMPORTS = frozenset(
    (
        "__future__",
        "collections",
        "dataclasses",
        "datamind",
        "datetime",
        "decimal",
        "enum",
        "hashlib",
        "json",
        "math",
        "re",
        "types",
        "typing",
        "uuid",
    )
)
FORBIDDEN_PREFIXES = (
    "anthropic",
    "chromadb",
    "fastapi",
    "networkx",
    "pydantic",
    "sqlalchemy",
    "datamind.agent",
    "datamind.capabilities",
    "datamind.core",
)


def imported_modules(path: Path) -> tuple:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return tuple(modules)


class CoreArchitectureTests(unittest.TestCase):
    def test_core_has_only_stdlib_and_kernel_imports(self) -> None:
        violations = []
        for package in ("kernel", "dataops"):
            for path in sorted((CORE_ROOT / package).glob("*.py")):
                tree = ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                )
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules = tuple(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        modules = (node.module,) if node.module else ()
                    else:
                        continue
                    for module in modules:
                        if module.split(".", 1)[0] not in ALLOWED_TOP_LEVEL_IMPORTS:
                            violations.append(
                                "{} imports {}".format(
                                    path.relative_to(CORE_ROOT),
                                    module,
                                )
                            )

        self.assertEqual(violations, [])

    def test_kernel_and_dataops_do_not_depend_on_legacy_or_vendor_modules(
        self,
    ) -> None:
        violations = []
        for package in ("kernel", "dataops"):
            for path in sorted((CORE_ROOT / package).glob("*.py")):
                for module in imported_modules(path):
                    if module.startswith(FORBIDDEN_PREFIXES):
                        violations.append(
                            "{} imports {}".format(
                                path.relative_to(CORE_ROOT),
                                module,
                            )
                        )

        self.assertEqual(violations, [])

    def test_kernel_does_not_depend_on_higher_layers(self) -> None:
        violations = []
        for path in sorted((CORE_ROOT / "kernel").glob("*.py")):
            for module in imported_modules(path):
                if module.startswith("datamind.") and module != "datamind.kernel":
                    violations.append(
                        "{} imports {}".format(
                            path.relative_to(CORE_ROOT),
                            module,
                        )
                    )

        self.assertEqual(violations, [])

    def test_engine_depends_on_ports_not_concrete_adapters(self) -> None:
        forbidden = (
            "datamind.adapters",
            "datamind.agent",
            "datamind.capabilities",
            "datamind.lifecycle",
        )
        violations = []
        for path in sorted((CORE_ROOT / "engine").glob("*.py")):
            for module in imported_modules(path):
                if module.startswith(forbidden):
                    violations.append(
                        "{} imports {}".format(
                            path.relative_to(CORE_ROOT),
                            module,
                        )
                    )

        self.assertEqual(violations, [])

    def test_intelligence_depends_on_ports_not_runtime_or_adapters(
        self,
    ) -> None:
        forbidden = (
            "datamind.adapters",
            "datamind.agent",
            "datamind.capabilities",
            "datamind.engine",
            "datamind.lifecycle",
        )
        violations = []
        for path in sorted((CORE_ROOT / "intelligence").glob("*.py")):
            for module in imported_modules(path):
                if module.startswith(forbidden):
                    violations.append(
                        "{} imports {}".format(
                            path.relative_to(CORE_ROOT),
                            module,
                        )
                    )

        self.assertEqual(violations, [])

    def test_catalog_and_ports_do_not_import_adapters_or_legacy(self) -> None:
        forbidden = (
            "datamind.adapters",
            "datamind.agent",
            "datamind.capabilities",
            "datamind.core",
        )
        violations = []
        for package in ("ports", "lifecycle"):
            for path in sorted((CORE_ROOT / package).glob("*.py")):
                for module in imported_modules(path):
                    if module.startswith(forbidden):
                        violations.append(
                            "{} imports {}".format(
                                path.relative_to(CORE_ROOT),
                                module,
                            )
                        )

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
