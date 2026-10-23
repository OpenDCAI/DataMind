"""Bounded, content-addressed exports for code sharing the MCP filesystem."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from datamind.core.errors import CapabilityError
from datamind.workspace_tables import interpret, table_rows


def export_root(settings):
    # Server/agent must share this directory (mount it for separate containers).
    base = Path(os.environ.get("DATAMIND_EXPORT_ROOT", Path.cwd() / ".datamind-cache"))
    base = base.expanduser().resolve()
    profile = base / settings.data.profile
    if profile.is_symlink() or (profile / "exports").is_symlink():
        raise CapabilityError("workspace", "profile export cache cannot be a symlink")
    return profile / "exports"


def _cache_file(root, chunks, suffix, budget):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise CapabilityError("workspace", "export cache cannot be a symlink")
    fd, temporary = tempfile.mkstemp(dir=root, prefix=".export-")
    sha, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                data = chunk.encode("utf-8")
                size += len(data)
                budget["bytes"] -= len(data)
                if budget["bytes"] < 0:
                    raise ValueError("Export exceeds max_bytes; narrow the selection")
                out.write(data)
                sha.update(data)
            out.flush()
            os.fsync(out.fileno())
        key = sha.hexdigest()
        target = root / (key + suffix)
        if target.is_symlink():
            raise CapabilityError("workspace", "export file cannot be a symlink")
        reused = False
        if target.exists():
            existing = hashlib.sha256()
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    existing.update(block)
            reused = existing.hexdigest() == key
        if not reused:
            os.replace(temporary, target)
        return {"path": str(target), "sha256": key, "bytes": size, "reused": reused}
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_bundle(view, requests, destination, *, max_rows=100000, max_bytes=33554432, view_mode="data"):
    if (not 1 <= len(requests) <= 20 or not 1 <= max_rows <= 1000000
            or not 1024 <= max_bytes <= 134217728 or view_mode not in {"data", "raw"}):
        raise ValueError("Invalid export request count, bounds or view_mode")
    root = Path(destination)
    budget = {"bytes": max_bytes, "rows": max_rows}
    items, seen = [], set()
    for request in requests:
        source = view.source(request["path"])
        prepared = view._prepared(source)
        evidence = {"source": source["uri"], "relative_path": source["relative_path"],
                    "source_version": source["version"]}
        tables = list(view._tables(source, prepared))
        if tables:
            selected = [t for t in tables if request.get("sheet") in (None, t["sheet"])]
            if not selected:
                raise CapabilityError("workspace", "sheet is absent from pinned source")
            for table in selected:
                if len(items) >= 100:
                    raise ValueError("Export expands to more than 100 assets; select sheets explicitly")
                identity = (source["uri"], table["name"])
                if identity in seen:
                    raise ValueError("Duplicate source/sheet in export")
                seen.add(identity)
                table = table if table.get("structure") else interpret(view.checkpoint_root, table)
                structure = table["structure"]
                fields = structure["fields"]
                chosen = request.get("columns")
                if chosen is not None:
                    if not chosen or len(set(chosen)) != len(chosen) or not set(chosen) <= {f["column"] for f in fields}:
                        raise ValueError("Select unique existing data columns")
                    fields = [next(f for f in fields if f["column"] == col) for col in chosen]
                if len(fields) > 1000:
                    raise ValueError("Too many export columns; select at most 1000")
                count = [0]
                names = [f["name"] if view_mode == "data" else f["column"] for f in fields]

                def lines():
                    for ordinal, row in enumerate(table_rows(view.checkpoint_root, table), 2):
                        ordinal = int(row["_source_row"]) if table.get("source_rows") else ordinal
                        if view_mode == "data" and not structure["data_start_row"] <= ordinal <= structure["data_end_row"]:
                            continue
                        budget["rows"] -= 1
                        if budget["rows"] < 0:
                            raise ValueError("Export exceeds max_rows; narrow the selection")
                        record = {name: row.get(f["column"]) for name, f in zip(names, fields)}
                        # Legacy records may already have lost duplicate columns.
                        record["_source_row"] = int(row["_source_row"]) if table.get("source_rows") else None
                        count[0] += 1
                        yield json.dumps(record, ensure_ascii=False) + "\n"

                artifact = _cache_file(root, lines(), ".jsonl", budget)
                items.append({**evidence, **artifact, "kind": "table", "table": table["name"],
                              "sheet": table["sheet"], "row_count": count[0], "view_mode": view_mode,
                              "structure": structure, "fields": [dict(f, key=name) for name, f in zip(names, fields)],
                              "lineage": "source_row_and_column" if table.get("source_rows") else "unavailable_legacy_republish_required",
                              "raw_headers": table.get("headers", []),
                              "warning": "Raw title/notes remain in the source and raw view; verify suggested data bounds."})
        else:
            from datamind.capabilities.ingest.formats import TABLE_EXTS
            if Path(source["uri"]).suffix.lower() in TABLE_EXTS:
                raise CapabilityError("workspace", "no parsed tables; publish the source with the DB surface")
            if request.get("sheet") is not None or request.get("columns") is not None:
                raise ValueError("sheet/columns require a parsed table")
            if source["uri"] in seen:
                raise ValueError("Duplicate text source in export")
            seen.add(source["uri"])
            text = prepared.get("parsed_text")
            cached = text is not None
            if text is None:
                from datamind.capabilities.ingest.formats import extract_document
                text = extract_document(view.root / source["snapshot"]).text
            artifact = _cache_file(root, [text], ".txt", budget)
            items.append({**evidence, **artifact, "kind": "text", "parsed_cache_reused": cached})
        if len(items) > 100:
            raise ValueError("Export expands to more than 100 assets; select sheets explicitly")
    manifest = {"format_version": 1, "workspace_revision": view.manifest["workspace_revision"],
                "pipeline_hash": view.manifest.get("pipeline_hash"),
                "items": [{k: v for k, v in item.items() if k != "reused"} for item in items],
                "access": "local filesystem; mount export root when agent runs elsewhere",
                "value_semantics": "source strings/NULL; convert explicitly in analysis"}
    manifest_file = _cache_file(root, [json.dumps(manifest, ensure_ascii=False, sort_keys=True)], ".manifest.json", budget)
    return {"workspace_revision": view.manifest["workspace_revision"], "manifest": manifest_file,
            "items": [{k: item[k] for k in ("kind", "path", "source", "source_version", "sha256", "bytes", "reused", "sheet", "row_count", "lineage") if k in item}
                      for item in items], "count": len(items), "truncated": False,
            "access": manifest["access"],
            "usage": "Read manifest for field-to-cell mappings. Load table JSONL in Python; _source_row is the original 1-based row. Reuse these files when generating deliverables."}
