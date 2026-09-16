"""Conversational ingest — let the agent add data via tools.

Store tools:
    kb_add_text              inline text → chunk → embed → upsert into Chroma
    kb_add_file              one file → chunk → embed → upsert into Chroma
    kb_add_path              file or directory → recursive ingest
    db_import_csv            CSV → infer schema → CREATE TABLE → INSERT
    db_import_records        records → infer schema → CREATE TABLE → INSERT
    graph_add_triples_from_text   free-form text → LLM extracts (s,r,o) → upsert
    graph_add_path                 file/directory → bounded extraction → upsert

Design notes:
- Re-uses existing chunker/hasher from capabilities/kb/indexer so ingested
  text is identical in shape to the seeded baseline.
- Hash-based de-dup: same content + same source → same chunk id, so
  repeated calls don't multiply rows.
- Path safety: `_resolve_safe_path` guards against ".." traversal AND
  enforces an allow-list of roots (profile data_dir + cwd by default).
- LLM triple extraction uses the same protocol-neutral model client as the
  rest of DataMind — no hidden provider-specific endpoint.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text as sql_text

from datamind.capabilities.kb.indexer import (
    Chunk,
    _hash,
    _split_text,
    _TEXT_EXTS,
)
from datamind.capabilities.kb.service import KBService
from datamind.capabilities.db.service import DBService
from datamind.capabilities.graph.service import GraphService
from datamind.core.errors import CapabilityError
from datamind.core.logging import get_logger
from datamind.core.protocols import GraphTriple, TextModelClient

from .formats import DOCUMENT_EXTS, TABLE_EXTS, extract_document, extract_tabular

_log = get_logger("ingest")

_WORKSPACE_SURFACE_EXTS: dict[str, tuple[str, ...]] = {
    ".txt": ("kb", "graph_text"),
    ".md": ("kb", "graph_text"),
    ".markdown": ("kb", "graph_text"),
    ".html": ("kb", "graph_text"),
    ".json": ("kb", "graph_text"),
    ".xml": ("kb", "graph_text"),
    ".py": ("kb", "graph_text"),
    ".java": ("kb", "graph_text"),
    ".csv": ("db", "kb", "graph_schema"),
    ".tsv": ("db", "kb", "graph_schema"),
    ".xls": ("db", "kb", "graph_schema"),
    ".xlsx": ("db", "kb", "graph_schema"),
    ".pdf": ("kb", "graph_text"),
    ".doc": ("kb", "graph_text"),
    ".docx": ("kb", "graph_text"),
    ".ppt": ("kb", "graph_text"),
    ".pptx": ("kb", "graph_text"),
}

_LINEAGE_TEXT_EXTS = {".txt", ".md", ".markdown", ".html", ".json", ".xml", ".py", ".java"}
_LINEAGE_TABLE_EXTS = {".csv", ".tsv"}
_GRAPH_DOCUMENT_EXTS = DOCUMENT_EXTS - TABLE_EXTS
_LINEAGE_VERSION_MARKER_RE = re.compile(
    r"[ _\-]*(v\d+|final|draft|copy|revised|updated|old|new|\(\d+\)|\d{4}[-_]\d{2}[-_]\d{2})$",
    re.IGNORECASE,
)


def _infer_table_name(stem: str) -> str:
    """Turn a filename stem into a deterministic, SQL-safe table name."""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_") or "table"
    if name[0].isdigit():
        name = f"t_{name}"
    return name[:64]


# ============================================================ path safety


def _resolve_safe_path(raw: str, allowed_roots: list[Path]) -> Path:
    """Resolve a user-provided path and reject anything outside allowed roots.

    Symlinks are resolved (`Path.resolve(strict=False)`) before the prefix
    check so traversal via symlink is also caught.

    Returns the resolved Path; raises CapabilityError if disallowed.
    """
    if not raw:
        raise CapabilityError("ingest", f"path is required")
    p = Path(raw).expanduser().resolve(strict=False)
    for root in allowed_roots:
        try:
            p.relative_to(root.resolve())
            return p
        except ValueError:
            continue
    pretty = ", ".join(str(r) for r in allowed_roots)
    raise CapabilityError("ingest", f"path '{p}' is outside allowed roots ({pretty}). "
        f"Move the file under one of these directories or pass an allowed path."
    )


# ============================================================ ingest service


class IngestService:
    """Adds data to KB / DB / Graph via simple tool-friendly methods."""

    def __init__(
        self,
        *,
        kb: KBService | None,
        db: DBService | None,
        graph: GraphService | None,
        llm_client: TextModelClient,
        llm_model: str,
        profile_data_dir: Path,
        chunk_size: int,
        chunk_overlap: int,
        allowed_roots: list[Path] | None = None,
    ) -> None:
        self._kb = kb
        self._db = db
        self._graph = graph
        self._client = llm_client
        self._model = llm_model
        self._profile_dir = profile_data_dir
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        # Default allow-list: this profile's data dir + cwd + cwd parent
        # + system temp + macOS-specific /tmp aliases.
        # The parent-of-cwd entry is what lets users keep demo data in
        # `~/Desktop/DataMind/demo-uploads/` while running the server from
        # `~/Desktop/DataMind/DataMind/` — without this they'd have to
        # either move the files into cwd or pass the basename of an
        # already-uploaded file. Add more roots via constructor if your
        # deployment needs a wider blast radius — symlinks resolve before
        # the prefix check.
        import tempfile  # local — avoid global import
        cwd = Path.cwd()
        self._allowed_roots: list[Path] = [
            profile_data_dir,
            cwd,
            cwd.parent,
            Path(tempfile.gettempdir()),
            # On macOS /tmp is a symlink to /private/tmp; resolve()
            # canonicalises to /private/tmp, so we need that explicit root.
            Path("/tmp"),
            Path("/private/tmp"),
            *(allowed_roots or []),
        ]

    def _locate_file(self, raw_path: str) -> Path:
        """Resolve a user-supplied path to an actual file on disk.

        Tries (in order):
          1. The path as given (absolute, or relative to cwd)
          2. Just the basename, looked up under the profile's uploads/ dir

        Step 2 lets users say "add the file foo.md" right after dragging
        it into the browser uploader — the agent doesn't need to know
        where /api/upload stashed it.
        """
        # Step 1: try the path verbatim (after path-safety check).
        try:
            resolved = _resolve_safe_path(raw_path, self._allowed_roots)
            if resolved.is_file():
                return resolved
        except CapabilityError:
            pass

        # Step 2: fall back to <profile>/uploads/<basename>.
        basename = Path(raw_path).name
        if basename:
            uploads_dir = self._profile_dir / "uploads"
            candidate = (uploads_dir / basename).resolve(strict=False)
            try:
                candidate.relative_to(uploads_dir.resolve())
                if candidate.is_file():
                    return candidate
            except ValueError:
                pass

        # Re-run resolve on the original to surface the original error.
        return _resolve_safe_path(raw_path, self._allowed_roots)

    async def workspace_inspect(
        self,
        *,
        path: str,
        recursive: bool = True,
        include_hash: bool = True,
        max_files: int = 2000,
    ) -> dict[str, Any]:
        """Inventory a workspace before choosing a surface to build.

        The result is intentionally read-only and provider-neutral. It does
        not parse or ingest files; it gives a Builder stable file metadata,
        content hashes, and conservative surface candidates for planning.
        """
        if not path or not path.strip():
            raise CapabilityError("ingest", "path is required")
        if not 1 <= max_files <= 10000:
            raise CapabilityError("ingest", "max_files must be between 1 and 10000")

        resolved = _resolve_safe_path(path, self._allowed_roots)
        if resolved.is_file():
            candidates = [resolved]
        elif resolved.is_dir():
            iterator = resolved.rglob("*") if recursive else resolved.glob("*")
            candidates = sorted(item for item in iterator if item.is_file())
        else:
            raise CapabilityError("ingest", f"path does not exist: {resolved}")

        files_found = len(candidates)
        truncated = files_found > max_files
        candidates = candidates[:max_files]
        files: list[dict[str, Any]] = []
        by_extension: dict[str, int] = {}
        by_surface: dict[str, int] = {}
        for candidate in candidates:
            suffix = candidate.suffix.lower() or "<none>"
            try:
                size = candidate.stat().st_size
                digest = None
                if include_hash:
                    hasher = hashlib.sha256()
                    with candidate.open("rb") as handle:
                        for block in iter(lambda: handle.read(1024 * 1024), b""):
                            hasher.update(block)
                    digest = f"sha256:{hasher.hexdigest()}"
            except OSError as exc:
                files.append({
                    "path": str(candidate), "extension": suffix,
                    "status": "unreadable", "error": str(exc),
                })
                continue
            surfaces = list(_WORKSPACE_SURFACE_EXTS.get(suffix, ()))
            files.append({
                "path": str(candidate),
                "relative_path": str(candidate.relative_to(resolved)) if resolved.is_dir() else candidate.name,
                "extension": suffix,
                "bytes": size,
                "sha256": digest,
                "candidate_surfaces": surfaces,
                "status": "supported" if surfaces else "unclassified",
            })
            by_extension[suffix] = by_extension.get(suffix, 0) + 1
            for surface in surfaces:
                by_surface[surface] = by_surface.get(surface, 0) + 1

        return {
            "path": str(resolved),
            "recursive": recursive,
            "files_scanned": len(files),
            "files_total": files_found,
            "truncated": truncated,
            "max_files": max_files,
            "by_extension": dict(sorted(by_extension.items())),
            "by_surface": dict(sorted(by_surface.items())),
            "files": files,
        }

    async def graph_build_lineage(
        self,
        *,
        path: str,
        recursive: bool = True,
        max_files: int = 2000,
        dependencies: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Build a deterministic file-lineage graph for a workspace.

        The graph is intentionally rule-based: file containment, textual
        mentions, tabular schema overlap, version names, and duplicate hashes.
        It complements (and does not replace) LLM concept-triple extraction.
        """
        if self._graph is None:
            raise CapabilityError("ingest", "Graph surface is disabled")
        inventory = await self.workspace_inspect(
            path=path, recursive=recursive, include_hash=True, max_files=max_files,
        )
        resolved = Path(inventory["path"])
        files = [item for item in inventory["files"] if item.get("status") == "supported"]
        if not files:
            return {
                "path": str(resolved), "nodes_added": 0, "edges_added": 0,
                "files_processed": 0, "truncated": inventory["truncated"],
            }

        root = str(resolved if resolved.is_dir() else resolved.parent)
        root_id = _hash(root, "lineage")[:12]
        workspace_node = f"workspace::{root_id}"
        file_nodes: dict[str, str] = {}
        file_text: dict[str, str] = {}
        file_columns: dict[str, set[str]] = {}
        for item in files:
            candidate = Path(item["path"])
            rel = item.get("relative_path") or candidate.name
            node = f"file::{root_id}::{rel}"
            file_nodes[str(candidate)] = node
            suffix = item.get("extension", "")
            if suffix in _LINEAGE_TEXT_EXTS:
                try:
                    file_text[str(candidate)] = candidate.read_text(
                        encoding="utf-8", errors="replace"
                    )[:200_000]
                except OSError:
                    pass
            if suffix in _LINEAGE_TABLE_EXTS:
                try:
                    delimiter = "\t" if suffix == ".tsv" else ","
                    with candidate.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                        header = next(csv.reader(handle, delimiter=delimiter), [])
                    file_columns[str(candidate)] = {
                        re.sub(r"[^a-z0-9]+", "_", col.strip().lower()).strip("_")
                        for col in header if col.strip()
                    }
                except (OSError, StopIteration):
                    pass

        triples: list[GraphTriple] = []
        properties_base = {"_lineage_root": root, "_source_managed": True}
        for item in files:
            candidate = Path(item["path"])
            node = file_nodes[str(candidate)]
            triples.append(GraphTriple(
                subject=workspace_node,
                relation="contains",
                object=node,
                source=root,
                properties={**properties_base, "source_file": str(candidate), "sha256": item.get("sha256")},
            ))

        # Mentions: a text file explicitly names another file in the same workspace.
        for source_path, text_content in file_text.items():
            source_node = file_nodes[source_path]
            for target_path, target_node in file_nodes.items():
                if source_path == target_path:
                    continue
                target_name = Path(target_path).name
                if target_name and target_name in text_content:
                    triples.append(GraphTriple(
                        subject=source_node, relation="mentions", object=target_node,
                        source=root,
                        properties={**properties_base, "source_file": source_path},
                    ))

        table_items = list(file_columns.items())
        for index, (left_path, left_cols) in enumerate(table_items):
            for right_path, right_cols in table_items[index + 1:]:
                union = left_cols | right_cols
                if not union:
                    continue
                jaccard = len(left_cols & right_cols) / len(union)
                if jaccard >= 0.5:
                    triples.append(GraphTriple(
                        subject=file_nodes[left_path], relation="schema_overlap",
                        object=file_nodes[right_path], source=root,
                        properties={**properties_base, "source_file": left_path, "jaccard": round(jaccard, 3)},
                    ))

        paths = list(file_nodes.items())
        for index, (left_path, left_node) in enumerate(paths):
            left_stem = _LINEAGE_VERSION_MARKER_RE.sub(
                "", Path(left_path).stem
            ).strip().lower()
            for right_path, right_node in paths[index + 1:]:
                if left_stem and left_stem == _LINEAGE_VERSION_MARKER_RE.sub(
                    "", Path(right_path).stem
                ).strip().lower() and Path(left_path).name != Path(right_path).name:
                    triples.append(GraphTriple(
                        subject=left_node, relation="version_of", object=right_node,
                        source=root,
                        properties={**properties_base, "source_file": left_path},
                    ))

        by_hash: dict[str, list[str]] = {}
        for item in files:
            digest = item.get("sha256")
            if digest:
                by_hash.setdefault(str(digest), []).append(str(item["path"]))
        for digest, members in by_hash.items():
            if len(members) < 2:
                continue
            for index, left_path in enumerate(members):
                for right_path in members[index + 1:]:
                    triples.append(GraphTriple(
                        subject=file_nodes[left_path], relation="shared_artifact",
                        object=file_nodes[right_path], source=root,
                        properties={**properties_base, "source_file": left_path, "sha256": digest},
                    ))

        # Explicit file dependencies use workspace-relative paths and are never inferred.
        for edge in dependencies or []:
            if edge.get("relation", "depends_on") != "depends_on":
                raise CapabilityError("ingest", "explicit file dependencies only support depends_on")
            endpoints = []
            for field in ("source", "target"):
                value = edge.get(field)
                if not value:
                    raise CapabilityError("ingest", f"dependency {field} is required")
                endpoint = str((Path(root) / value).resolve())
                if endpoint not in file_nodes:
                    raise CapabilityError("ingest", f"dependency file not in inventory: {value}")
                endpoints.append(endpoint)
            triples.append(GraphTriple(
                subject=file_nodes[endpoints[0]], relation="depends_on",
                object=file_nodes[endpoints[1]], source=root,
                properties={**properties_base, "source_file": endpoints[0], "evidence": "explicit"},
            ))

        reconcile = getattr(self._graph.store, "reconcile_lineage_triples", None)
        if callable(reconcile):
            await reconcile(root, triples)
        else:
            await self._graph.store.upsert_triples(triples)
        persist = getattr(self._graph.store, "persist", None)
        if callable(persist):
            result = persist()
            if hasattr(result, "__await__"):
                await result
        return {
            "path": str(resolved),
            "workspace_node": workspace_node,
            "files_processed": len(files),
            "nodes_added": len(files) + 1,
            "edges_added": len(triples),
            "relations": dict(sorted({rel: sum(1 for t in triples if t.relation == rel) for rel in {t.relation for t in triples}}.items())),
            "truncated": inventory["truncated"],
        }

    # ------------------------------------------------------------- Build lifecycle

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return f"sha256:{digest.hexdigest()}"

    def _build_dir(self, build_id: str) -> Path:
        if not re.fullmatch(r"build-[A-Za-z0-9-]+", build_id):
            raise CapabilityError("ingest", "invalid build_id")
        return self._profile_dir / "builds" / build_id

    def _profile_storage_dir(self) -> Path:
        # profile_data_dir is <base>/data/profiles/<profile>; indexes live in
        # the sibling <base>/storage/<profile> directory.
        return self._profile_dir.parents[2] / "storage" / self._profile_dir.name

    async def raw_file_read(
        self, *, path: str, offset: int = 0, max_chars: int = 20000,
    ) -> dict[str, Any]:
        """Read source evidence without writing to any surface; offsets count characters."""
        if offset < 0 or not 1 <= max_chars <= 100000:
            raise CapabilityError("ingest", "offset must be nonnegative and max_chars between 1 and 100000")
        resolved = self._locate_file(path)
        if not resolved.is_file():
            raise CapabilityError("ingest", f"not a file: {resolved}")
        try:
            if resolved.suffix.lower() in DOCUMENT_EXTS:
                extracted = extract_document(resolved)
                content, warnings = extracted.text, extracted.warnings
            elif resolved.suffix.lower() in {".csv", ".tsv"}:
                content, warnings = resolved.read_text(encoding="utf-8"), []
            else:
                raise CapabilityError("ingest", f"no readable text representation for {resolved.suffix}")
        except (RuntimeError, OSError, ValueError) as exc:
            raise CapabilityError("ingest", f"cannot read {resolved.name}: {exc}") from exc
        end = min(offset + max_chars, len(content))
        return {"path": str(resolved), "sha256": self._file_hash(resolved),
                "text": content[offset:end], "offset": offset,
                "next_offset": end if end < len(content) else None,
                "total_chars": len(content), "truncated": end < len(content),
                "warnings": warnings}

    async def build_status(self, *, build_id: str) -> dict[str, Any]:
        state_path = self._build_dir(build_id) / "state.json"
        if not state_path.is_file():
            raise CapabilityError("ingest", f"unknown build_id '{build_id}'")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        result = {key: value for key, value in state.items() if key != "source_inventory"}
        result["source_files"] = state["source_inventory"]["files_scanned"]
        if state["status"] == "FROZEN":
            result["verification"] = await self.build_verify(build_id=build_id)
        return result

    async def build_start(self, *, path: str) -> dict[str, Any]:
        """Start a build run and record the immutable source inventory."""
        resolved = _resolve_safe_path(path, self._allowed_roots)
        inventory = await self.workspace_inspect(path=str(resolved), include_hash=True)
        build_id = f"build-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        run_dir = self._build_dir(build_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        state = {
            "build_id": build_id,
            "status": "BUILDING",
            "workspace": str(resolved),
            "source_inventory": inventory,
            "profile": self._profile_dir.name,
            "created_at": time.time(),
        }
        (run_dir / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"build_id": build_id, "status": "BUILDING", "workspace": str(resolved)}

    async def build_freeze(self, *, build_id: str) -> dict[str, Any]:
        """Freeze current profile artifacts and record their content hashes."""
        run_dir = self._build_dir(build_id)
        state_path = run_dir / "state.json"
        if not state_path.is_file():
            raise CapabilityError("ingest", f"unknown build_id '{build_id}'")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") != "BUILDING":
            raise CapabilityError("ingest", f"build is not BUILDING: {state.get('status')}")
        roots = {"profile": self._profile_dir, "storage": self._profile_storage_dir()}
        artifacts: dict[str, str] = {}
        for label, root in roots.items():
            if not root.is_dir():
                continue
            for candidate in sorted(root.rglob("*")):
                if not candidate.is_file() or "builds" in candidate.parts:
                    continue
                artifacts[f"{label}/{candidate.relative_to(root)}"] = self._file_hash(candidate)
        manifest = {
            "build_id": build_id,
            "workspace": state["workspace"],
            "source_files": {
                item["path"]: item.get("sha256")
                for item in state["source_inventory"].get("files", [])
                if item.get("sha256")
            },
            "artifacts": artifacts,
            "frozen_at": time.time(),
        }
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest["freeze_lock"] = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        state["status"] = "FROZEN"
        state["freeze_lock"] = manifest["freeze_lock"]
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"build_id": build_id, "status": "FROZEN", "artifacts": len(artifacts), "freeze_lock": manifest["freeze_lock"]}

    async def build_verify(self, *, build_id: str) -> dict[str, Any]:
        """Verify that a frozen build's DataMind artifacts have not changed."""
        run_dir = self._build_dir(build_id)
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            raise CapabilityError("ingest", f"build '{build_id}' has not been frozen")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        changed: list[str] = []
        missing: list[str] = []
        for key, expected in manifest.get("artifacts", {}).items():
            label, relative = key.split("/", 1)
            root = self._profile_dir if label == "profile" else self._profile_storage_dir()
            candidate = root / relative
            if not candidate.is_file():
                missing.append(key)
            elif self._file_hash(candidate) != expected:
                changed.append(key)
        return {
            "build_id": build_id,
            "ok": not changed and not missing,
            "artifacts": len(manifest.get("artifacts", {})),
            "changed": changed,
            "missing": missing,
            "freeze_lock": manifest.get("freeze_lock"),
        }

    async def build_export(self, *, build_id: str, output_path: str) -> dict[str, Any]:
        """Export a verified frozen build without copying raw source files."""
        verification = await self.build_verify(build_id=build_id)
        if not verification["ok"]:
            raise CapabilityError("ingest", "cannot export a modified or incomplete build")
        target = _resolve_safe_path(output_path, self._allowed_roots)
        target.mkdir(parents=True, exist_ok=True)
        manifest = json.loads((self._build_dir(build_id) / "manifest.json").read_text(encoding="utf-8"))
        copied = 0
        for key in manifest.get("artifacts", {}):
            label, relative = key.split("/", 1)
            root = self._profile_dir if label == "profile" else self._profile_storage_dir()
            source = root / relative
            destination = target / label / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied += 1
        shutil.copy2(self._build_dir(build_id) / "manifest.json", target / "manifest.json")
        return {"build_id": build_id, "output_path": str(target), "artifacts_exported": copied}

    # ------------------------------------------------------------- KB

    async def kb_add_text(
        self,
        *,
        text: str,
        source: str | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Ingest inline text and optionally persist it under the profile."""
        if self._kb is None:
            raise CapabilityError("ingest", "KB surface is disabled")
        content = text.strip()
        if not content:
            raise CapabilityError("ingest", "text is required")
        source_name = source or f"note-{_hash(content, 'inline')[:12]}.md"
        if Path(source_name).name != source_name:
            raise CapabilityError("ingest", "source must be a filename, not a path")
        if Path(source_name).suffix.lower() not in _TEXT_EXTS:
            source_name += ".md"

        stored_source = source_name
        if persist:
            notes_dir = self._profile_dir / "notes"
            notes_dir.mkdir(parents=True, exist_ok=True)
            target = notes_dir / source_name
            if target.exists() and target.read_text(encoding="utf-8", errors="replace").strip() != content:
                target = notes_dir / (
                    f"{Path(source_name).stem}-{_hash(content, source_name)[:8]}"
                    f"{Path(source_name).suffix}"
                )
            target.write_text(content + "\n", encoding="utf-8")
            stored_source = str(target.relative_to(self._profile_dir))

        chunks = [
            Chunk(
                id=_hash(segment, stored_source, ordinal=ordinal),
                text=segment,
                source=stored_source,
                metadata={"_origin": "store_agent", "_chunk_ordinal": ordinal},
            )
            for ordinal, segment in enumerate(
                _split_text(
                    content,
                    chunk_size=self._chunk_size,
                    chunk_overlap=self._chunk_overlap,
                )
            )
        ]
        await self._upsert_chunks(chunks)
        return {
            "source": stored_source,
            "chunks_added": len(chunks),
            "persisted": persist,
        }

    async def kb_add_file(self, *, path: str, copy_to_profile: bool = True) -> dict[str, Any]:
        """Ingest a single text file into the KB.

        path: absolute, cwd-relative, OR just a basename that exists under
            the profile's uploads/ dir (lets the user say "add foo.md"
            right after dragging it into the browser uploader).
        copy_to_profile: if True (default), copy the file under
            `<data_dir>/uploads/` so the next `reindex()` finds it too.
            If False, only the in-memory ingest happens.
        """
        resolved = self._locate_file(path)
        if not resolved.is_file():
            raise CapabilityError("ingest", f"not a file: {resolved}")
        if resolved.suffix.lower() not in DOCUMENT_EXTS:
            raise CapabilityError("ingest", f"unsupported extension '{resolved.suffix}'. "
                f"Supported: {sorted(DOCUMENT_EXTS)}"
            )
        try:
            extracted = extract_document(resolved)
        except RuntimeError as exc:
            raise CapabilityError("ingest", str(exc)) from exc
        text = extracted.text
        if copy_to_profile:
            dest_dir = self._profile_dir / "uploads"
            dest_dir.mkdir(parents=True, exist_ok=True)
            # If file is already under the profile dir, skip the copy.
            try:
                resolved.relative_to(self._profile_dir.resolve())
                copied_to = None
            except ValueError:
                dest = dest_dir / resolved.name
                # Avoid clobbering existing distinct content.
                if dest.exists():
                    same = dest.read_bytes() == resolved.read_bytes()
                    if not same:
                        dest = dest_dir / f"{resolved.stem}-{_hash(text, resolved.name)[:8]}{resolved.suffix}"
                if resolved.suffix.lower() in _TEXT_EXTS:
                    dest.write_text(text, encoding="utf-8")
                else:
                    shutil.copy2(resolved, dest)
                copied_to = str(dest.relative_to(self._profile_dir))
        else:
            copied_to = None

        # Build chunks identical in shape to indexer's raw path.
        source = copied_to or str(resolved)
        chunks: list[Chunk] = []
        for ordinal, seg in enumerate(
            _split_text(
                text,
                chunk_size=self._chunk_size,
                chunk_overlap=self._chunk_overlap,
            )
        ):
            chunks.append(Chunk(
                id=_hash(seg, source, ordinal=ordinal),
                text=seg,
                source=source,
                metadata={"_origin": "ingest", "_chunk_ordinal": ordinal,
                          "source_file": str(resolved), "source_sha256": self._file_hash(resolved)},
            ))

        if not chunks:
            return {"file": str(resolved), "chunks_added": 0, "note": "file was empty"}

        parsed_chunks = None
        if copy_to_profile and resolved.suffix.lower() not in _TEXT_EXTS:
            chunk_dir = self._profile_dir / "chunks"
            chunk_dir.mkdir(parents=True, exist_ok=True)
            chunk_file = chunk_dir / f"ingest-{_hash(source, 'parsed')}.jsonl"
            chunk_file.write_text("".join(json.dumps({
                "id": chunk.id, "text": chunk.text, "source": chunk.source,
                "metadata": chunk.metadata,
            }, ensure_ascii=False) + "\n" for chunk in chunks), encoding="utf-8")
            parsed_chunks = str(chunk_file.relative_to(self._profile_dir))
        await self._upsert_chunks(
            chunks, replace_sources={source, str(resolved)},
        )
        _log.info("kb_add_file", extra={
            "file": str(resolved), "chunks": len(chunks), "copied_to": copied_to
        })
        return {
            "file": str(resolved),
            "chunks_added": len(chunks),
            "copied_to": copied_to,
            "source": source,
            "parsed_chunks": parsed_chunks,
            "format": extracted.format,
            "blocks": len(extracted.blocks),
            "warnings": extracted.warnings,
        }

    async def kb_add_path(
        self,
        *,
        path: str,
        recursive: bool = True,
        copy_to_profile: bool = True,
    ) -> dict[str, Any]:
        """Ingest one file or every supported file under a directory."""
        # Try locating as a single file first (covers the "agent only knows
        # the basename of an uploaded file" case). Fall back to plain path
        # resolution for genuine directory ingest.
        try:
            single = self._locate_file(path)
            if single.is_file():
                res = await self.kb_add_file(path=str(single), copy_to_profile=copy_to_profile)
                return {"files_processed": 1, "chunks_added": res["chunks_added"], "files": [res]}
        except CapabilityError:
            pass
        resolved = _resolve_safe_path(path, self._allowed_roots)
        if not resolved.is_dir():
            raise CapabilityError("ingest", f"path does not exist: {resolved}")

        iter_func = resolved.rglob if recursive else resolved.glob
        per_file: list[dict[str, Any]] = []
        total_chunks = 0
        skipped: list[str] = []
        for p in sorted(iter_func("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in DOCUMENT_EXTS:
                skipped.append(str(p))
                continue
            try:
                res = await self.kb_add_file(path=str(p), copy_to_profile=copy_to_profile)
                per_file.append(res)
                total_chunks += res["chunks_added"]
            except CapabilityError as exc:
                # Single bad file shouldn't kill a directory ingest.
                _log.warning("kb_add_file_skipped", extra={"path": str(p), "err": str(exc)})
                skipped.append(f"{p}: {exc}")

        return {
            "files_processed": len(per_file),
            "chunks_added": total_chunks,
            "files": per_file,
            "skipped": skipped[:20],  # cap output to keep payload small
            "skipped_count": len(skipped),
        }

    async def _upsert_chunks(
        self,
        chunks: list[Chunk],
        *,
        replace_sources: set[str] | None = None,
    ) -> None:
        """Embed and write chunks, optionally replacing one source revision.

        Chunks are frozen dataclasses, so we keep the embedding vectors in
        a parallel list and pass them straight into the store's `add` API.
        """
        if self._kb is None:
            raise CapabilityError("ingest", "KB surface is disabled")
        provider = self._kb.embedding
        store = self._kb.vector_store
        texts = [c.text for c in chunks]
        vectors = await provider.embed_texts(texts)
        stale_ids: list[str] = []
        if replace_sources:
            new_ids = {chunk.id for chunk in chunks}
            for chunk_id, _text, metadata in await store.get_all_texts():
                recorded_sources = {
                    str(metadata.get("source") or ""),
                    str(metadata.get("source_file") or ""),
                }
                if recorded_sources & replace_sources and chunk_id not in new_ids:
                    stale_ids.append(chunk_id)
        await store.add(
            ids=[c.id for c in chunks],
            texts=texts,
            embeddings=vectors,
            metadatas=[
                {**(c.metadata or {}), "source": c.source or ""}
                for c in chunks
            ],
        )
        if stale_ids:
            await store.delete(stale_ids)
        # Incremental writes must leave the same compatibility metadata that
        # a full KB reindex produces; otherwise the next process startup will
        # reject this otherwise valid persisted index.
        await self._kb.record_incremental_ingest()

    # ------------------------------------------------------------- DB

    async def db_import_csv(
        self,
        *,
        path: str,
        table: str,
        if_exists: str = "append",
        delimiter: str = ",",
    ) -> dict[str, Any]:
        """Import a CSV into a SQLite/MySQL/Postgres table.

        - Schema is inferred from the header row (all columns TEXT).
        - if_exists: "append" (default) | "replace" | "fail"
        - table name is validated to prevent SQL injection.
        """
        if self._db is None:
            raise CapabilityError("ingest", "DB surface is disabled")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", table):
            raise CapabilityError("ingest", f"invalid table name '{table}'. Use letters/digits/underscore only.")
        if if_exists not in ("append", "replace", "fail"):
            raise CapabilityError("ingest", f"if_exists must be append|replace|fail, got '{if_exists}'")

        resolved = self._locate_file(path)
        if not resolved.is_file():
            raise CapabilityError("ingest", f"not a file: {resolved}")

        text = resolved.read_text(encoding="utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            raise CapabilityError("ingest", f"CSV is empty")

        # Sanitise column names: same rule as table names.
        safe_cols: list[str] = []
        for raw in header:
            col = raw.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", col):
                # Fall back to col_<idx> if header is unusable.
                col = f"col_{len(safe_cols) + 1}"
            safe_cols.append(col)

        rows: list[dict[str, str]] = []
        for raw_row in reader:
                # Pad short rows / truncate long rows to header length.
                trimmed = list(raw_row[: len(safe_cols)])
                while len(trimmed) < len(safe_cols):
                    trimmed.append("")
                rows.append(dict(zip(safe_cols, trimmed)))

        if not rows:
            return {"table": table, "rows_inserted": 0, "note": "CSV had header but no data rows"}

        # SQLAlchemy: use bulk insert with parameter binding.
        engine = self._db.engine
        col_defs = ", ".join(f'"{c}" TEXT' for c in safe_cols)
        placeholders = ", ".join(f":{c}" for c in safe_cols)
        insert_cols = ", ".join(f'"{c}"' for c in safe_cols)

        with engine.begin() as conn:
            # RetrieveAgent marks pooled SQLite connections query-only.
            # StoreAgent explicitly re-enables writes when it checks one out.
            if self._db.dialect.name == "sqlite":
                conn.exec_driver_sql("PRAGMA query_only = OFF")
            # Probe existence once.
            existing = self._db.dialect.name in {"sqlite"} and conn.execute(
                sql_text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            ).fetchone()

            if if_exists == "replace":
                conn.execute(sql_text(f'DROP TABLE IF EXISTS "{table}"'))
                conn.execute(sql_text(f'CREATE TABLE "{table}" ({col_defs})'))
            elif if_exists == "fail" and existing:
                raise CapabilityError("ingest", f"table '{table}' already exists")
            else:  # append
                conn.execute(sql_text(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})'))

            conn.execute(
                sql_text(f'INSERT INTO "{table}" ({insert_cols}) VALUES ({placeholders})'),
                rows,
            )

        _log.info("db_import_csv", extra={
            "file": str(resolved), "table": table,
            "rows": len(rows), "cols": len(safe_cols),
        })
        self._db.invalidate_schema_cache()
        return {
            "table": table,
            "columns": safe_cols,
            "rows_inserted": len(rows),
            "if_exists": if_exists,
            "source_file": str(resolved),
        }

    async def db_import_records(
        self,
        *,
        table: str,
        records: list[dict[str, Any]],
        if_exists: str = "append",
    ) -> dict[str, Any]:
        """Import inline JSON-like records into a table as TEXT columns."""
        if self._db is None:
            raise CapabilityError("ingest", "DB surface is disabled")
        if not records:
            raise CapabilityError("ingest", "records must be a non-empty array")
        if not all(isinstance(row, dict) for row in records):
            raise CapabilityError("ingest", "every record must be an object")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", table):
            raise CapabilityError("ingest", f"invalid table name '{table}'")
        if if_exists not in ("append", "replace", "fail"):
            raise CapabilityError(
                "ingest", f"if_exists must be append|replace|fail, got '{if_exists}'"
            )

        original_cols: list[str] = []
        for row in records:
            for key in row:
                normalized_key = str(key)
                if normalized_key not in original_cols:
                    original_cols.append(normalized_key)
        if not original_cols:
            raise CapabilityError("ingest", "records must contain at least one field")
        safe_cols: list[str] = []
        used: set[str] = set()
        for index, raw in enumerate(original_cols, 1):
            col = raw.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", col) or col in used:
                col = f"col_{index}"
            used.add(col)
            safe_cols.append(col)

        rows: list[dict[str, str | None]] = []
        for record in records:
            converted: dict[str, str | None] = {}
            string_keys = {str(k): value for k, value in record.items()}
            for original, safe in zip(original_cols, safe_cols):
                value = string_keys.get(original)
                if value is None:
                    converted[safe] = None
                elif isinstance(value, (dict, list)):
                    converted[safe] = json.dumps(value, ensure_ascii=False)
                else:
                    converted[safe] = str(value)
            rows.append(converted)

        col_defs = ", ".join(f'"{c}" TEXT' for c in safe_cols)
        placeholders = ", ".join(f":{c}" for c in safe_cols)
        insert_cols = ", ".join(f'"{c}"' for c in safe_cols)
        with self._db.engine.begin() as conn:
            if self._db.dialect.name == "sqlite":
                conn.exec_driver_sql("PRAGMA query_only = OFF")
            existing = self._db.dialect.name == "sqlite" and conn.execute(
                sql_text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            ).fetchone()
            if if_exists == "replace":
                conn.execute(sql_text(f'DROP TABLE IF EXISTS "{table}"'))
                conn.execute(sql_text(f'CREATE TABLE "{table}" ({col_defs})'))
            elif if_exists == "fail" and existing:
                raise CapabilityError("ingest", f"table '{table}' already exists")
            else:
                conn.execute(sql_text(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})'))
            conn.execute(
                sql_text(f'INSERT INTO "{table}" ({insert_cols}) VALUES ({placeholders})'),
                rows,
            )
        self._db.invalidate_schema_cache()
        return {
            "table": table,
            "columns": safe_cols,
            "rows_inserted": len(rows),
            "if_exists": if_exists,
            "source": "inline_records",
        }

    async def db_import_path(
        self,
        *,
        path: str,
        table_prefix: str | None = None,
        if_exists: str = "append",
        delimiter: str = ",",
    ) -> dict[str, Any]:
        """Import CSV/TSV or every sheet in an XLSX workbook into DB tables."""
        if self._db is None:
            raise CapabilityError("ingest", "DB surface is disabled")
        resolved = self._locate_file(path)
        if not resolved.is_file():
            raise CapabilityError("ingest", f"not a file: {resolved}")
        suffix = resolved.suffix.lower()
        if suffix in {".csv", ".tsv"}:
            result = await self.db_import_csv(
                path=str(resolved),
                table=table_prefix or _infer_table_name(resolved.stem),
                if_exists=if_exists,
                delimiter="\t" if suffix == ".tsv" else delimiter,
            )
            return {"source_file": str(resolved), "tables": [result], "tables_processed": 1}
        if suffix not in TABLE_EXTS:
            raise CapabilityError("ingest", "db_import_path supports .csv, .tsv, .xlsx and .xls")
        try:
            sheets = extract_tabular(resolved)
        except RuntimeError as exc:
            raise CapabilityError("ingest", str(exc)) from exc
        prefix = table_prefix or _infer_table_name(resolved.stem)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,48}", prefix):
            raise CapabilityError("ingest", "table_prefix must contain letters, digits and underscores")
        results: list[dict[str, Any]] = []
        for sheet_name, _columns, rows in sheets:
            if not rows:
                continue
            safe_sheet = re.sub(r"[^A-Za-z0-9_]+", "_", sheet_name).strip("_") or "sheet"
            table = f"{prefix}_{safe_sheet}"[:64]
            results.append(await self.db_import_records(table=table, records=rows, if_exists=if_exists))
            results[-1]["sheet"] = sheet_name
            results[-1]["source_file"] = str(resolved)
        return {"source_file": str(resolved), "tables": results, "tables_processed": len(results)}

    async def surface_ingest_path(
        self,
        *,
        path: str,
        surfaces: list[str] | None = None,
        recursive: bool = True,
    ) -> dict[str, Any]:
        """Route a workspace into KB, DB, and deterministic Graph surfaces."""
        selected = set(["kb", "db", "graph"] if surfaces is None else surfaces)
        unknown = selected - {"kb", "db", "graph"}
        if unknown:
            raise CapabilityError("ingest", f"unknown surfaces: {sorted(unknown)}")
        inventory = await self.workspace_inspect(path=path, recursive=recursive, include_hash=True)
        results: dict[str, Any] = {"path": inventory["path"], "inventory": inventory}
        results["disabled_surfaces"] = sorted(
            name for name in selected if getattr(self, f"_{name}") is None
        )
        if "kb" in selected and self._kb is not None:
            results["kb"] = await self.kb_add_path(path=path, recursive=recursive)
        if "db" in selected and self._db is not None:
            tables: list[dict[str, Any]] = []
            for item in inventory["files"]:
                if item.get("extension") not in TABLE_EXTS or item.get("status") != "supported":
                    continue
                try:
                    relative = item["relative_path"]
                    stem = re.sub(r"[^A-Za-z0-9_]+", "_", Path(relative).stem).strip("_") or "table"
                    prefix = f"ws_{stem[:28]}_{_hash(relative, inventory['path'])[:8]}"
                    tables.append(await self.db_import_path(
                        path=item["path"], table_prefix=prefix, if_exists="replace",
                    ))
                except (CapabilityError, OSError, ValueError) as exc:
                    tables.append({"source_file": item["path"], "error": str(exc)})
            results["db"] = {"tables": tables, "files_processed": len(tables)}
        if "graph" in selected and self._graph is not None:
            results["graph"] = await self.graph_build_lineage(path=path, recursive=recursive)
        results["surfaces"] = sorted(selected)
        return results

    # ------------------------------------------------------------- Graph

    async def graph_add_triples_from_text(
        self,
        *,
        text: str,
        max_triples: int = 30,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Use the LLM to extract (subject, relation, object) triples from
        free-form text, then upsert them into the graph store.

        The prompt asks for strict JSON; we tolerate minor formatting
        deviations (markdown fence wrappers etc.) but reject anything that
        doesn't parse.
        """
        if self._graph is None:
            raise CapabilityError("ingest", "Graph surface is disabled")
        if not text or not text.strip():
            raise CapabilityError("ingest", f"text is required")
        if not 1 <= max_triples <= 200:
            raise CapabilityError("ingest", "max_triples must be between 1 and 200")

        prompt = (
            "Extract knowledge graph triples from the user's text. "
            "Each triple is (subject, relation, object). Use concise "
            "noun phrases for entities and a short verb phrase for the "
            "relation. Prefer English relation names with snake_case "
            "(e.g. 'reports_to', 'works_on', 'located_in'). Keep entity "
            f"names in their original language. Cap at {max_triples} triples. "
            "Return STRICT JSON only — an array of objects with keys "
            '"subject", "relation", "object". Do not include any prose, '
            "no markdown fences. Example:\n"
            '[{"subject": "Alice", "relation": "leads", "object": "Search Team"}]\n\n'
            "Text:\n---\n" + text.strip() + "\n---"
        )

        raw = (await self._client.generate_text(
            prompt, model=self._model, max_tokens=2048, temperature=0.0,
        )).strip()

        triples = self._parse_triples_json(raw)
        if not triples:
            if source:
                reconcile = getattr(self._graph.store, "reconcile_source_triples", None)
                if source and callable(reconcile):
                    await reconcile(source, [])
                    persist = getattr(self._graph.store, "persist", None)
                    if callable(persist):
                        result = persist()
                        if hasattr(result, "__await__"):
                            await result
            return {
                "triples_added": 0,
                "source": source,
                "note": "model returned no parseable triples",
                "raw_response": raw[:500],
            }

        # Upsert into the graph store via the same path graph_upsert_triples uses.
        gt: list[GraphTriple] = [
            GraphTriple(
                subject=str(t["subject"]),
                relation=str(t["relation"]),
                object=str(t["object"]),
                source=source,
            )
            for t in triples[:max_triples]
        ]
        properties = {"_source_managed": True, "_source_path": source} if source else {}
        if properties:
            gt = [t.model_copy(update={"properties": properties}) for t in gt]
        reconcile = getattr(self._graph.store, "reconcile_source_triples", None)
        if source and callable(reconcile):
            await reconcile(source, gt)
        else:
            await self._graph.store.upsert_triples(gt)

        # Persist to disk so the new edges survive restart. NetworkX
        # store's persist is sync; other stores may make it a coroutine.
        persist = getattr(self._graph.store, "persist", None)
        if callable(persist):
            result = persist()
            # Tolerate either sync or async implementations.
            if hasattr(result, "__await__"):
                await result

        _log.info("graph_add_triples_from_text", extra={"count": len(gt)})
        return {
            "triples_added": len(gt),
            "source": source,
            "samples": [
                {"subject": t.subject, "relation": t.relation, "object": t.object}
                for t in gt[:8]
            ],
        }

    async def graph_add_path(
        self,
        *,
        path: str,
        recursive: bool = True,
        max_triples_per_file: int = 30,
    ) -> dict[str, Any]:
        """Extract and persist graph triples from one text file or directory.

        This is deliberately a bounded batch wrapper around
        ``graph_add_triples_from_text``. It keeps source paths on every edge so
        later retrieval can explain where a relationship came from.
        """
        if self._graph is None:
            raise CapabilityError("ingest", "Graph surface is disabled")
        if not path or not path.strip():
            raise CapabilityError("ingest", "path is required")
        if not 1 <= max_triples_per_file <= 200:
            raise CapabilityError("ingest", "max_triples_per_file must be between 1 and 200")

        resolved = _resolve_safe_path(path, self._allowed_roots)
        if resolved.is_file():
            if resolved.suffix.lower() not in _GRAPH_DOCUMENT_EXTS:
                raise CapabilityError(
                    "ingest",
                    f"unsupported extension '{resolved.suffix}'. Supported: {sorted(_GRAPH_DOCUMENT_EXTS)}",
                )
            candidates = [resolved]
        elif resolved.is_dir():
            iterator = resolved.rglob("*") if recursive else resolved.glob("*")
            candidates = sorted(
                item for item in iterator
                if item.is_file() and item.suffix.lower() in _GRAPH_DOCUMENT_EXTS
            )
        else:
            raise CapabilityError("ingest", f"path does not exist: {resolved}")

        processed: list[dict[str, Any]] = []
        skipped: list[str] = []
        total = 0
        for candidate in candidates:
            try:
                if candidate.suffix.lower() in _TEXT_EXTS:
                    content = candidate.read_text(encoding="utf-8", errors="replace")
                else:
                    content = extract_document(candidate).text
                if not content.strip():
                    skipped.append(f"{candidate}: empty")
                    continue
                # A document can be much larger than the model context.  Use
                # the same deterministic splitter as KB ingestion, but with
                # no overlap: overlap is useful for retrieval and would make
                # graph extraction duplicate relationships.  Each part gets
                # a distinct reconciliation key so a later part cannot
                # replace triples extracted from an earlier part.
                parts = _split_text(
                    content,
                    chunk_size=self._chunk_size,
                    chunk_overlap=0,
                )
                file_total = 0
                part_results: list[dict[str, Any]] = []
                for ordinal, part in enumerate(parts, 1):
                    remaining = max_triples_per_file - file_total
                    if remaining <= 0:
                        break
                    part_source = (
                        str(candidate)
                        if len(parts) == 1
                        else f"{candidate}#part={ordinal}"
                    )
                    result = await self.graph_add_triples_from_text(
                        text=part,
                        max_triples=remaining,
                        source=part_source,
                    )
                    part_results.append(result)
                    file_total += int(result.get("triples_added", 0))
                processed.append({
                    "path": str(candidate),
                    "source": str(candidate),
                    "triples_added": file_total,
                    "parts_processed": len(part_results),
                    "parts": part_results,
                })
                total += file_total
            except Exception as exc:
                # A malformed archive, missing parser, or model-side failure
                # should not prevent other files in the same directory from
                # being ingested.  CancelledError inherits BaseException and
                # therefore still propagates to the caller.
                _log.warning(
                    "graph_file_skipped",
                    extra={"path": str(candidate), "error": str(exc)},
                )
                skipped.append(f"{candidate}: {type(exc).__name__}: {exc}")

        return {
            "path": str(resolved),
            "files_processed": len(processed),
            "triples_added": total,
            "files": processed,
            "skipped": skipped[:20],
            "skipped_count": len(skipped),
        }

    @staticmethod
    def _parse_triples_json(raw: str) -> list[dict[str, str]]:
        """Be lenient: strip code fences, trailing prose, common LLM tics."""
        s = raw.strip()
        # Strip ```json ... ``` fences.
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s)
            s = re.sub(r"\s*```\s*$", "", s)
        # Find the first JSON array — sometimes the model adds preamble.
        m = re.search(r"\[.*\]", s, flags=re.DOTALL)
        if not m:
            return []
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        out: list[dict[str, str]] = []
        for item in arr if isinstance(arr, list) else []:
            if not isinstance(item, dict):
                continue
            if not all(k in item for k in ("subject", "relation", "object")):
                continue
            out.append(item)
        return out


# ============================================================ DI


def build_ingest_service(
    *,
    settings,
    kb: KBService | None,
    db: DBService | None,
    graph: GraphService | None,
    llm_client: TextModelClient,
) -> IngestService:
    return IngestService(
        kb=kb,
        db=db,
        graph=graph,
        llm_client=llm_client,
        llm_model=settings.llm.fallback_model or settings.llm.model,
        profile_data_dir=settings.data.data_dir,
        chunk_size=settings.retrieval.chunk_size,
        chunk_overlap=settings.retrieval.chunk_overlap,
    )


__all__ = ["IngestService", "build_ingest_service"]
