"""Pure deterministic file-lineage construction shared by all graph backends."""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from datamind.core.errors import CapabilityError
from datamind.core.protocols import GraphEvidence, GraphTriple

TEXT_EXTS = {".txt", ".md", ".markdown", ".html", ".json", ".xml", ".py", ".java"}
TABLE_EXTS = {".csv", ".tsv"}
VERSION_MARKER = re.compile(
    r"[ _\-]*(v\d+|final|draft|copy|revised|updated|old|new|\(\d+\)|\d{4}[-_]\d{2}[-_]\d{2})$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LineageFile:
    uri: str
    relative_path: str
    version: str
    content_path: Path


def build_lineage_triples(*, root: str | Path, files: list[LineageFile], dependencies=None):
    root = str(Path(root).resolve())
    root_id = hashlib.sha256((root + "lineage").encode()).hexdigest()[:12]
    workspace = f"workspace::{root_id}"
    nodes = {item.uri: f"file::{root_id}::{item.relative_path}" for item in files}
    props = {"_lineage_root": root, "_source_managed": True}
    by_uri = {item.uri: item for item in files}

    def evidence(uris, *, method="inferred", pipeline_version="lineage-v1"):
        return [
            GraphEvidence(
                source_id="file:" + hashlib.sha256(uri.encode()).hexdigest(),
                source_version=by_uri[uri].version,
                locator={"uri": uri, "relative_path": by_uri[uri].relative_path},
                method=method,
                pipeline_version=pipeline_version,
            )
            for uri in dict.fromkeys(uris)
            if uri in by_uri
        ]

    triples = [GraphTriple(subject=workspace, relation="contains", object=nodes[item.uri],
        source=root, evidence=evidence([item.uri]),
        properties={**props, "source_file": item.uri, "sha256": item.version})
        for item in files]

    texts, columns = {}, {}
    for item in files:
        suffix = Path(item.uri).suffix.lower()
        try:
            if suffix in TEXT_EXTS:
                texts[item.uri] = item.content_path.read_text(encoding="utf-8", errors="replace")[:200_000]
            elif suffix in TABLE_EXTS:
                delimiter = "\t" if suffix == ".tsv" else ","
                with item.content_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                    header = next(csv.reader(handle, delimiter=delimiter), [])
                columns[item.uri] = {re.sub(r"[^a-z0-9]+", "_", c.strip().lower()).strip("_")
                                     for c in header if c.strip()}
        except OSError:
            continue

    for left, content in texts.items():
        for right in nodes:
            if left != right and Path(right).name in content:
                triples.append(GraphTriple(subject=nodes[left], relation="mentions", object=nodes[right],
                    source=root, evidence=evidence([left, right]),
                    properties={**props, "source_file": left}))
    table_items = list(columns.items())
    for index, (left, left_cols) in enumerate(table_items):
        for right, right_cols in table_items[index + 1:]:
            union = left_cols | right_cols
            score = len(left_cols & right_cols) / len(union) if union else 0
            if score >= .5:
                triples.append(GraphTriple(subject=nodes[left], relation="schema_overlap", object=nodes[right],
                    source=root, evidence=evidence([left, right]),
                    properties={**props, "source_file": left, "jaccard": round(score, 3)}))

    for index, left in enumerate(files):
        left_stem = VERSION_MARKER.sub("", Path(left.uri).stem).strip().lower()
        for right in files[index + 1:]:
            right_stem = VERSION_MARKER.sub("", Path(right.uri).stem).strip().lower()
            if left_stem and left_stem == right_stem and Path(left.uri).name != Path(right.uri).name:
                triples.append(GraphTriple(subject=nodes[left.uri], relation="version_of", object=nodes[right.uri],
                    source=root, evidence=evidence([left.uri, right.uri]),
                    properties={**props, "source_file": left.uri}))

    by_hash = {}
    for item in files:
        by_hash.setdefault(item.version, []).append(item.uri)
    for version, members in by_hash.items():
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                triples.append(GraphTriple(subject=nodes[left], relation="shared_artifact", object=nodes[right],
                    source=root, evidence=evidence([left, right]),
                    properties={**props, "source_file": left, "sha256": version}))

    by_relative = {item.relative_path: item.uri for item in files}
    for edge in dependencies or []:
        if edge.get("relation", "depends_on") != "depends_on":
            raise CapabilityError("ingest", "explicit file dependencies only support depends_on")
        try:
            left, right = (by_relative[edge[field]] for field in ("source", "target"))
        except (KeyError, TypeError):
            raise CapabilityError("ingest", "dependency endpoints must be workspace files") from None
        triples.append(GraphTriple(subject=nodes[left], relation="depends_on", object=nodes[right], source=root,
            evidence=evidence([left, right], method="explicit", pipeline_version="lineage-explicit-v1"),
            properties={**props, "source_file": left}))
    return triples
