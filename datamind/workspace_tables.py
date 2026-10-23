"""Table interpretation and cell lineage without an inner model or value coercion."""
from __future__ import annotations

import csv
import itertools
import json
import re
from copy import deepcopy
from pathlib import Path

from datamind.core.errors import ConfigError

PARSER_VERSION = "table-evidence-v1"
RESERVED = {"_source_file", "_source_row"}


def column_letter(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def table_rows(root, table):
    """Iterate canonical cells in original order; never query unordered SQL rows."""
    if "path" in table:
        with (Path(root) / table["path"]).open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            for row in reader:
                yield dict(zip(table["columns"], (json.loads(v) for v in row)))
    else:
        yield from table.get("records", [])


def resolve_mappings(inventory, entries, previous=None):
    from datamind.capabilities.ingest.formats import TABLE_EXTS
    current = {s["uri"]: s for s in inventory}
    prior = {s["uri"]: s for s in (previous or {}).get("sources", [])}
    mappings = {uri: deepcopy(prior[uri]["table_mapping"]) for uri, item in current.items()
                if uri in prior and prior[uri]["version"] == item["version"]
                and prior[uri].get("table_mapping")}
    seen = set()
    if entries is not None and (not isinstance(entries, list) or len(entries) > 1000):
        raise ConfigError("Table mappings must be a bounded list")
    for entry in entries or []:
        if (not isinstance(entry, dict) or set(entry) != {"uri", "version", "sheets"}
                or not isinstance(entry["uri"], str) or not isinstance(entry["sheets"], list)):
            raise ConfigError("Expected table mapping uri, version and sheets")
        uri = entry["uri"]
        if uri not in current or uri in seen or entry["version"] != current[uri]["version"]:
            raise ConfigError("Table mapping must match one prepared source and its content hash")
        seen.add(uri)
        if Path(uri).suffix.lower() not in TABLE_EXTS:
            raise ConfigError("Table mappings require a tabular source")
        sheets = entry["sheets"]
        for sheet in sheets:
            if (not isinstance(sheet, dict) or not {"sheet", "header_row"} <= sheet.keys()
                    or set(sheet) - {"sheet", "header_row", "data_start_row", "data_end_row", "fields"}
                    or not isinstance(sheet["sheet"], str)):
                raise ConfigError("Expected sheet name, header row and optional data bounds/fields")
            if not isinstance(sheet.get("fields", []), list):
                raise ConfigError("Table fields must be a list")
            for field in sheet.get("fields", []):
                if (not isinstance(field, dict) or not {"column", "name"} <= field.keys()
                        or set(field) - {"column", "name", "unit"}
                        or not isinstance(field["column"], str) or not isinstance(field["name"], str)
                        or field.get("unit") is not None and not isinstance(field["unit"], str)):
                    raise ConfigError("Expected field column, name and optional explicit unit")
        if len({s["sheet"] for s in sheets}) != len(sheets):
            raise ConfigError("Duplicate sheet in table mapping")
        mappings[uri] = deepcopy(sheets)
    return mappings


def _looks_numeric(value):
    text = str(value).strip().replace(",", "")
    return bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:%|％)?", text))


def interpret(root, table, override=None):
    """Keep the raw table intact; attach a conservative, inspectable data view."""
    value = deepcopy(table)
    columns = [c for c in table["columns"] if c not in RESERVED]
    first = list(table.get("headers", columns))[:len(columns)]
    sample = [first] + [[row.get(c) for c in columns]
                        for row in itertools.islice(table_rows(root, table), 30)]
    candidates = []
    for ordinal, row in enumerate(sample, 1):
        populated = [v for v in row if v is not None and str(v).strip()]
        labels = [str(v).strip() for v in populated
                  if isinstance(v, str) and not _looks_numeric(v) and len(v) <= 80]
        # A title or note occupying one cell of a wide sheet is not a header.
        if (populated and len(labels) == len(populated)
                and len(set(labels)) == len(labels)
                and len(populated) >= (1 if len(columns) == 1 else 2)):
            following = sample[ordinal:ordinal + 3]
            if any(sum(v is not None and str(v).strip() != "" for v in r) >= len(populated)
                   for r in following):
                candidates.append(ordinal)
    header_row = candidates[0] if len(candidates) == 1 else 1
    status = "suggested" if len(candidates) == 1 else "needs_confirmation"
    override = override or {}
    header_row = override.get("header_row", header_row)
    data_start = override.get("data_start_row", header_row + 1)
    data_end = override.get("data_end_row", table["rows"] + 1)
    if (any(not isinstance(v, int) or isinstance(v, bool) for v in (header_row, data_start, data_end))
            or not 1 <= header_row <= table["rows"] + 1
            or not header_row < data_start <= table["rows"] + 2
            or not data_start - 1 <= data_end <= table["rows"] + 1):
        raise ConfigError("Invalid header/data source row bounds")
    if header_row <= len(sample):
        header = sample[header_row - 1]
    else:
        header = next(itertools.islice(table_rows(root, table), header_row - 2, header_row - 1))
        header = [header.get(c) for c in columns]
    fields = []
    specified = {}
    for field in override.get("fields", []):
        col = field["column"]
        if col not in columns or col in specified:
            raise ConfigError("Table fields must name unique existing physical columns")
        if not field.get("name", "").strip() or len(field["name"]) > 200:
            raise ConfigError("Table field names must be nonempty and at most 200 characters")
        specified[col] = field
    used = set(RESERVED)
    for index, col in enumerate(columns):
        raw = header[index] if index < len(header) else None
        name = specified.get(col, {}).get("name") or (str(raw).strip() if raw is not None else "") or col
        base, suffix = name, 2
        while name in used:
            name, suffix = f"{base}_{suffix}", suffix + 1
        used.add(name)
        fields.append({"column": col, "name": name, "source_column": column_letter(index + 1),
                       "unit": specified.get(col, {}).get("unit")})
    value["structure"] = {"status": "confirmed" if override else status,
                          "header_row": header_row, "data_start_row": data_start,
                          "data_end_row": data_end, "fields": fields,
                          "warnings": ([] if override else [
                              "Header is a suggestion; verify multi-section sheets and data bounds."]),
                          "value_semantics": "source strings/NULL; units are explicit, no coercion"}
    return value


def interpret_tables(root, tables, mappings):
    requested = {s["sheet"]: s for s in mappings or []}
    if not set(requested) <= {t["sheet"] for t in tables}:
        raise ConfigError("Table mapping names an unknown sheet")
    return [interpret(root, table, requested.get(table["sheet"])) for table in tables]
