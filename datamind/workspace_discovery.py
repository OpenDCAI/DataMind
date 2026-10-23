"""Bounded discovery and parsed evidence from one immutable Workspace manifest.

These reads need neither a database connection nor an embedding/model request.
The manifest is an internal binding, never a routine model-facing response.
"""
from __future__ import annotations

import csv
import hashlib
import itertools
import json
from pathlib import Path

from datamind.capabilities.ingest.formats import TABLE_EXTS, extract_document
from datamind.core.errors import CapabilityError


def access_summary(manifest):
    return {
        "source_root": manifest["source_root"],
        "source_count": len(manifest["sources"]),
        "surfaces": manifest["surfaces"],
        "truncated_source_count": len(manifest.get("truncated_sources", [])),
        "coverage_scope": "published sources only; other workspace files may be unindexed",
        "discovery_tool": "workspace_catalog",
    }


class WorkspaceView:
    def __init__(self, manifest, root, *, checkpoint_root=None, ingest=None):
        self.manifest = manifest
        self.root = Path(root)
        self.checkpoint_root = Path(checkpoint_root or root)
        self.ingest = ingest

    def _prepared(self, source):
        if source.get("artifact"):
            path = self.checkpoint_root / source["artifact"]
        elif source.get("checkpoint"):
            path = self.checkpoint_root / source["checkpoint"]
        else:
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def source(self, path):
        matches = [s for s in self.manifest["sources"] if path in {s["uri"], s["relative_path"]}]
        if len(matches) != 1:
            raise CapabilityError("workspace", "source does not belong to the pinned revision")
        return matches[0]

    def _tables(self, source, prepared):
        for table in prepared.get("tables", []):
            if isinstance(table, dict):
                yield table
            else:
                sheet, headers, records = table
                if not records:
                    continue  # Legacy builds do not materialize empty tables.
                columns, rows = self.ingest._read_from_records(records, source_file=source["uri"])
                yield {"name": "ws_" + hashlib.sha256((source["source_id"] + sheet).encode()).hexdigest()[:24],
                       "sheet": sheet, "columns": columns, "headers": headers,
                       "rows": len(rows), "records": rows}

    def catalog(self, *, query="", kind="sources", source="", sheet="", column="", offset=0, limit=20):
        if kind not in {"sources", "tables"} or offset < 0 or not 1 <= limit <= 100:
            raise ValueError("kind must be sources/tables, offset nonnegative, limit 1..100")
        if kind == "sources" and (sheet or column):
            raise ValueError("sheet/column filters require kind=tables")
        terms = query.casefold().split()
        rows = []
        for item in self.manifest["sources"]:
            if source.casefold() not in item["uri"].casefold():
                continue
            if kind == "sources":
                candidates = [{"uri": item["uri"], "relative_path": item["relative_path"],
                               "source_version": item["version"]}]
            elif Path(item["uri"]).suffix.lower() in TABLE_EXTS:
                candidates = []
                for table in self._tables(item, self._prepared(item)):
                    headers = table.get("headers", table["columns"])
                    if sheet.casefold() not in table["sheet"].casefold():
                        continue
                    if column and not any(column.casefold() in str(c).casefold()
                                          for c in [*table["columns"], *headers,
                                                    *[f["name"] for f in table.get("structure", {}).get("fields", [])]]):
                        continue
                    candidates.append({"table": table["name"], "source": item["uri"],
                        "source_version": item["version"], "sheet": table["sheet"],
                        "columns": table["columns"], "source_headers": headers, "rows": table["rows"],
                        "structure": table.get("structure"), "source_rows": table.get("source_rows", False)})
            else:
                continue
            for row in candidates:
                searchable = json.dumps(row, ensure_ascii=False).casefold()
                if all(term in searchable for term in terms):
                    rows.append(row)
        page = rows[offset:offset + limit]
        # Wide spreadsheets must not turn discovery into another huge response.
        for row in page:
            if "columns" in row:
                row["column_count"] = len(row["columns"])
                row["columns_truncated"] = len(row["columns"]) > 50
                pairs = list(itertools.zip_longest(row["columns"], row["source_headers"], fillvalue=""))
                if column:
                    pairs.sort(key=lambda pair: not any(column.casefold() in str(c).casefold() for c in pair))
                row["columns"] = [pair[0] for pair in pairs[:50]]
                row["source_headers"] = [pair[1] for pair in pairs[:50]]
                if row.get("structure"):
                    row["structure"] = {**row["structure"], "fields": row["structure"]["fields"][:50],
                                        "fields_truncated": len(row["structure"]["fields"]) > 50}
        end = offset + len(page)
        provider = self.manifest.get("snapshots", {}).get("db", {}).get("provider")
        return {"workspace_revision": self.manifest["workspace_revision"], "kind": kind,
                "sql_dialect": "mysql" if provider == "dolt" else provider,
                "items": page, "count": len(page), "total_count": len(rows),
                "next_offset": end if end < len(rows) else None, "truncated": end < len(rows)}

    def read_asset(self, *, path, sheet=None, offset=0, limit=100, max_chars=20000, columns=None):
        if offset < 0 or not 1 <= limit <= 200 or not 1 <= max_chars <= 100000:
            raise ValueError("offset must be nonnegative, limit 1..200, max_chars 1..100000")
        # Only published source URIs/relative paths are accepted, never arbitrary paths.
        source = self.source(path)
        prepared = self._prepared(source)
        evidence = {"workspace_revision": self.manifest["workspace_revision"],
                    "source": source["uri"], "source_version": source["version"],
                    "pipeline_hash": self.manifest.get("pipeline_hash")}
        if Path(source["uri"]).suffix.lower() in TABLE_EXTS:
            tables = list(self._tables(source, prepared))
            selected = [t for t in tables if sheet is None or t["sheet"] == sheet]
            if len(selected) != 1:
                raise CapabilityError("workspace", "select one sheet using workspace_catalog(kind=tables)")
            table = selected[0]
            names = columns if columns is not None else table["columns"][:50]
            if len(names) > 100 or not set(names) <= set(table["columns"]):
                raise ValueError("select at most 100 existing SQL column names")
            indices = [table["columns"].index(name) for name in names]
            if "path" in table:
                with (self.checkpoint_root / table["path"]).open(encoding="utf-8", newline="") as handle:
                    reader = csv.reader(handle)
                    next(reader)
                    rows = [[json.loads(row[i]) for i in indices]
                            for row in itertools.islice(reader, offset, offset + limit)]
            else:
                rows = [[r.get(name) for name in names] for r in table["records"][offset:offset + limit]]
            end = offset + len(rows)
            from datamind.workspace_tables import column_letter
            data_columns = [c for c in table["columns"] if c not in {"_source_file", "_source_row"}]
            return {**evidence, "kind": "table", "table": table["name"], "sheet": table["sheet"],
                    "source_rows": list(range(offset + 2, end + 2)) if table.get("source_rows") else None,
                    "source_columns": ({c: column_letter(data_columns.index(c) + 1) for c in names if c in data_columns}
                                       if table.get("source_rows") else None),
                    "structure": ({**table["structure"], "fields": [f for f in table["structure"]["fields"] if f["column"] in names]}
                                  if table.get("structure") else None),
                    "columns": names, "rows": rows, "offset": offset, "count": len(rows),
                    "total_count": table["rows"], "next_offset": end if end < table["rows"] else None,
                    "truncated": end < table["rows"] or len(names) < len(table["columns"]),
                    "columns_truncated": len(names) < len(table["columns"]), "reused": True}
        if sheet is not None or columns is not None:
            raise ValueError("sheet and columns apply only to table sources")
        cached = prepared.get("parsed_text")
        extracted = cached if cached is not None else extract_document(self.root / source["snapshot"]).text
        end = min(offset + max_chars, len(extracted))
        return {**evidence, "kind": "text", "text": extracted[offset:end], "offset": offset,
                "total_chars": len(extracted), "next_offset": end if end < len(extracted) else None,
                "truncated": end < len(extracted), "reused": cached is not None,
                "warnings": prepared.get("parse_warnings", [])}
