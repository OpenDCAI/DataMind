"""Document loading + chunking + embedding into a VectorStore.

Input modes (auto-detected from profile directory layout):

    data/profiles/<profile>/
        chunks/*.jsonl        -> pre-chunked JSON lines, each object must have
                                 {"id": str, "text": str, "source"?: str,
                                  "metadata"?: dict}.
        documents/**          -> raw .txt / .md / .pdf (future).
        *.txt / *.md          -> root-level raw docs.

Both modes may coexist — pre-chunked data is loaded first, then raw docs
are chunked on the fly. All resulting chunks land in a single vector
collection per profile.

Chunking is simple, greedy, character-based with configurable overlap.
For anything fancier we'll plug in another splitter via the same function
signature later.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from datamind.core.logging import get_logger
from datamind.core.protocols import EmbeddingProvider, VectorStore

_log = get_logger("kb.indexer")

# Extensions treated as raw text documents.
_TEXT_EXTS = {".txt", ".md", ".markdown"}


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    source: str | None = None
    metadata: dict[str, Any] | None = None


def _hash(text: str, source: str | None) -> str:
    h = hashlib.sha1()
    if source:
        h.update(source.encode("utf-8", errors="ignore"))
        h.update(b"\x00")
    h.update(text.encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]


def _split_text(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """Greedy splitter: break on paragraphs first, then on size.

    Tries to respect natural boundaries (blank lines / sentences) when
    they fall inside the size window. Not fancy; deterministic; good
    enough for the default.
    """
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be < chunk_size")
    if len(text) <= chunk_size:
        return [text.strip()] if text.strip() else []

    # Prefer paragraph breaks; fall back to sentence breaks; then hard cut.
    paras = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    buf = ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if len(buf) + len(p) + 2 <= chunk_size:
            buf = f"{buf}\n\n{p}" if buf else p
            continue
        if buf:
            chunks.append(buf)
        if len(p) > chunk_size:
            # Hard-split long paragraph with overlap
            i = 0
            step = chunk_size - chunk_overlap
            while i < len(p):
                chunks.append(p[i : i + chunk_size])
                i += step
            buf = ""
        else:
            buf = p
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


def _iter_pre_chunked(chunks_dir: Path) -> Iterable[Chunk]:
    for jsonl in sorted(chunks_dir.glob("*.jsonl")):
        with jsonl.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    _log.warning(
                        "bad_jsonl_line",
                        extra={"file": str(jsonl), "line": lineno, "err": str(exc)},
                    )
                    continue
                text = (obj.get("text") or "").strip()
                if not text:
                    continue
                source = obj.get("source") or jsonl.name
                cid = str(obj.get("id") or _hash(text, source))
                meta = obj.get("metadata") or {}
                if not isinstance(meta, dict):
                    meta = {"_raw_metadata": str(meta)}
                meta.setdefault("_origin", "pre_chunked")
                yield Chunk(id=cid, text=text, source=source, metadata=meta)


def _iter_raw_documents(
    data_dir: Path,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> Iterable[Chunk]:
    # Anything not inside "chunks/" subdirs — walk the tree.
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _TEXT_EXTS:
            continue
        # Skip reserved subdirs (used by other capabilities).
        parts = path.relative_to(data_dir).parts
        if parts and parts[0] in {"chunks", "triplets", "tables", "images"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _log.warning("read_fail", extra={"path": str(path), "err": str(exc)})
            continue
        source = str(path.relative_to(data_dir))
        for seg in _split_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap):
            yield Chunk(
                id=_hash(seg, source),
                text=seg,
                source=source,
                metadata={"_origin": "raw"},
            )


async def build_index(
    *,
    data_dir: Path,
    vector_store: VectorStore,
    embedding: EmbeddingProvider,
    chunk_size: int,
    chunk_overlap: int,
    batch_size: int = 64,
) -> dict[str, int]:
    """Load, chunk, embed, and upsert everything in `data_dir` into the store.

    Returns a stats dict: {pre_chunked, raw_chunks, total_embedded}.
    """
    data_dir = Path(data_dir)
    stats = {"pre_chunked": 0, "raw_chunks": 0, "total_embedded": 0}

    chunks: list[Chunk] = []

    chunks_dir = data_dir / "chunks"
    if chunks_dir.is_dir():
        for c in _iter_pre_chunked(chunks_dir):
            chunks.append(c)
            stats["pre_chunked"] += 1

    if data_dir.is_dir():
        for c in _iter_raw_documents(
            data_dir, chunk_size=chunk_size, chunk_overlap=chunk_overlap
        ):
            chunks.append(c)
            stats["raw_chunks"] += 1

    if not chunks:
        _log.warning("no_docs_to_index", extra={"data_dir": str(data_dir)})
        return stats

    # Embed + upsert in batches to bound memory.
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c.text for c in batch]
        vectors = await embedding.embed_texts(texts)
        if len(vectors) != len(batch):
            raise ValueError(
                f"embedding count mismatch: expected {len(batch)}, got {len(vectors)}"
            )
        expected_dimension = int(getattr(embedding, "dimension", 0) or 0)
        for offset, vector in enumerate(vectors):
            if expected_dimension and len(vector) != expected_dimension:
                raise ValueError(
                    f"embedding dimension mismatch at item {i + offset}: "
                    f"expected {expected_dimension}, got {len(vector)}"
                )
            if any(not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in vector):
                raise ValueError(f"non-finite embedding at item {i + offset}")
        await vector_store.add(
            ids=[c.id for c in batch],
            texts=texts,
            embeddings=vectors,
            metadatas=[
                {**(c.metadata or {}), "source": c.source or ""}
                for c in batch
            ],
        )
        stats["total_embedded"] += len(batch)

    _log.info("index_built", extra=stats)
    return stats


def corpus_fingerprint(data_dir: Path) -> str:
    """Hash the indexable corpus deterministically without loading it all."""
    root = Path(data_dir)
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if path.suffix.lower() not in _TEXT_EXTS | {".jsonl"}:
            continue
        if path.suffix.lower() == ".jsonl" and (not rel.parts or rel.parts[0] != "chunks"):
            continue
        digest.update(str(rel).encode("utf-8"))
        digest.update(b"\x00")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\x00")
    return digest.hexdigest()


def build_index_manifest(
    *,
    data_dir: Path,
    embedding_provider: str,
    embedding_model: str,
    dimension: int,
    chunk_size: int,
    chunk_overlap: int,
) -> dict[str, Any]:
    fields = {
        "schema_version": 1,
        "embedding_provider": embedding_provider,
        "embedding_model": embedding_model,
        "dimension": dimension,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "corpus_hash": corpus_fingerprint(data_dir),
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    fields["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return fields


def write_manifest_atomic(path: Path, manifest: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


async def list_documents(data_dir: Path) -> list[dict[str, Any]]:
    """Shallow listing of everything under the profile's data dir."""
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(data_dir.rglob("*")):
        if not p.is_file():
            continue
        parts = p.relative_to(data_dir).parts
        kind = parts[0] if parts and parts[0] in {"chunks", "triplets", "tables", "images"} else "docs"
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        out.append(
            {
                "path": str(p.relative_to(data_dir)),
                "kind": kind,
                "size": size,
                "ext": p.suffix.lower(),
            }
        )
    return out
