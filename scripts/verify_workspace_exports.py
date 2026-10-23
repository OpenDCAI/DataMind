"""Compare pinned table exports with independent original-cell reads, offline.

Run after a source-only publication. No task questions or expected answers are
accepted. This proves data/evidence reuse, not end-to-end Agent superiority.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
from pathlib import Path

from datamind.config import DataSettings
from datamind.workspace import Workspace


def independent_cells(path, sheet):
    if path.suffix.lower() in {".csv", ".tsv"}:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.reader(handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ","))
    import openpyxl
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        chosen = book[sheet]
        chosen.reset_dimensions()
        return [[None if v is None else str(v) for v in row] for row in chosen.iter_rows(values_only=True)]
    finally:
        book.close()


def column_index(letter):
    result = 0
    for char in letter:
        result = result * 26 + ord(char) - ord("A") + 1
    return result - 1


async def verify(settings):
    w = Workspace(settings)
    checked, values, rows_total = [], 0, 0
    async with w.reader(enable=set()) as reader:
        tables, offset = [], 0
        while True:
            page = await reader.call("workspace_catalog", kind="tables", offset=offset, limit=100)
            tables.extend(page["items"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        for table in tables:
            request = {"path": table["source"], "sheet": table["sheet"]}
            result = await reader.call("workspace_export_bundle", requests=[request], max_rows=1000000, max_bytes=134217728)
            manifest = json.loads(Path(result["manifest"]["path"]).read_text())
            item = manifest["items"][0]
            if item["lineage"] != "source_row_and_column":
                raise AssertionError("Republish legacy tables before verifying exact cell lineage")
            source = Path(item["source"])
            source_kind = "original"
            if not source.exists():
                # Source-only builds may include a documented derived CSV whose
                # immutable evidence lives only in the published object store.
                entry = next(s for s in reader.manifest["sources"] if s["uri"] == item["source"])
                base = w.root if reader.manifest.get("format_version") == 2 else w._settings_for(reader.revision).data.base_dir
                source = base / entry["snapshot"]
                source_kind = "frozen_source"
            actual_hash = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            assert actual_hash == item["source_version"], "Original changed since publication"
            original = independent_cells(source, item["sheet"])
            count, subtotal = 0, 0
            with Path(item["path"]).open() as handle:
                for line in handle:
                    record = json.loads(line)
                    source_row = original[record["_source_row"] - 1]
                    for field in item["fields"]:
                        index = column_index(field["source_column"])
                        expected = source_row[index] if index < len(source_row) else ("" if source.suffix.lower() in {".csv", ".tsv"} else None)
                        assert record[field["key"]] == expected, (source.name, item["sheet"], record["_source_row"], field["source_column"])
                        subtotal += 1
                    count += 1
            assert count == item["row_count"]
            again = await reader.call("workspace_export_bundle", requests=[request], max_rows=1000000, max_bytes=134217728)
            assert again["items"][0]["reused"] and again["items"][0]["sha256"] == item["sha256"]
            checked.append({"source": item["source"], "sheet": item["sheet"], "rows": count,
                            "cells": subtotal, "evidence_source": source_kind,
                            "header_status": item["structure"]["status"], "cache_reused": True})
            values += subtotal
            rows_total += count
        return {"status": "passed", "workspace_revision": reader.revision, "tables": len(checked),
                "rows": rows_total, "cells": values, "checked": checked,
                "scope": "source cell/value and export reuse only; no model or task quality claim"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = asyncio.run(verify(DataSettings(_env_file=None, data={"base_dir": args.data_root, "profile": args.profile})))
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "checked"}, ensure_ascii=False))
