"""Immutable workspace revisions over the existing DataMind capability services.

The catalog is safe across processes on one host. Builds prepare private indexes;
publication uses compare-and-swap against their starting revision. Readers retain
one immutable revision across all tool calls. This local snapshot implementation
supports SQLite, NetworkX, and local Chroma/LanceDB; it is not a remote-store
snapshot coordinator. Source checkpoints avoid repeated model work after restart.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import replace
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from datamind.capabilities.ingest.formats import DOCUMENT_EXTS, TABLE_EXTS, extract_document, extract_tabular
from datamind.capabilities.kb.indexer import _hash, _split_text
from datamind.core.context import RequestContext
from datamind.core.errors import CapabilityError, ConfigError
from datamind.core.logging import bind_context
from datamind.core.protocols import GraphTriple


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Workspace:
    """A profile-scoped snapshot catalog. Pass separate settings per tenant."""

    def __init__(self, settings, *, builder=None):
        self.settings = settings.model_copy(deep=True)
        self.root = settings.data.storage_dir / 'workspace'
        self.root.mkdir(parents=True, exist_ok=True)
        self._catalog = self.root / 'catalog.sqlite3'
        self._builder = builder
        with self._connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY, parent TEXT, state TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS head (singleton INTEGER PRIMARY KEY, revision TEXT);
                INSERT OR IGNORE INTO head VALUES (1, NULL);
            ''')

    def _connect(self):
        return sqlite3.connect(self._catalog, timeout=30)

    @property
    def current_revision(self) -> str | None:
        with self._connect() as conn:
            return conn.execute('SELECT revision FROM head WHERE singleton=1').fetchone()[0]

    def status(self, revision: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute('SELECT id, parent, state, error FROM revisions WHERE id=?', (revision,)).fetchone()
        if row is None:
            raise CapabilityError('workspace', 'unknown revision')
        return dict(zip(('revision', 'parent', 'state', 'error'), row))

    def manifest(self, revision: str) -> dict[str, Any]:
        status = self.status(revision)
        if status['state'] != 'PUBLISHED':
            raise CapabilityError('workspace', 'revision was never published')
        return json.loads((self.root / 'revisions' / revision / 'manifest.json').read_text())

    def rollback(self, revision: str, *, expected_revision: str) -> dict[str, Any]:
        """Repoint to a retained complete snapshot without deleting history."""
        self.manifest(revision)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            head = conn.execute('SELECT revision FROM head WHERE singleton=1').fetchone()[0]
            if head != expected_revision:
                raise CapabilityError('workspace', 'publication conflict; current revision changed')
            conn.execute('UPDATE head SET revision=? WHERE singleton=1', (revision,))
        return {'workspace_revision': revision, 'previous_revision': head}

    def _settings_for(self, revision: str):
        settings = self.settings.model_copy(deep=True)
        settings.data.base_dir = self.root / 'revisions' / revision
        settings.kb.ingest_mode = 'direct'
        return settings

    async def _system(self, settings, surfaces):
        if self._builder is not None:
            return await self._builder(settings, enable=set(surfaces))
        from datamind.agent import build_datamind
        return await build_datamind(settings, enable=set(surfaces))

    def _inventory(self, source: Path) -> list[dict[str, str]]:
        if not source.is_dir():
            raise CapabilityError('workspace', 'workspace path must be a directory')
        inventory = []
        for path in sorted(source.rglob('*')):
            relative = path.relative_to(source)
            if path.is_relative_to(self.root) or any(part.startswith('.') for part in relative.parts):
                continue
            if not path.is_file() or path.suffix.lower() not in DOCUMENT_EXTS | TABLE_EXTS:
                continue
            if not path.resolve().is_relative_to(source):
                raise CapabilityError('workspace', f'symlink escapes workspace: {relative}')
            inventory.append({'relative_path': relative.as_posix(), 'uri': str(path.resolve()),
                              'version': 'sha256:' + _digest(path.read_bytes())})
        return inventory

    async def update(self, path: str, *, surfaces=('kb', 'graph', 'db'), relation_types=None) -> dict[str, Any]:
        """Prepare and publish one generation, retaining the previous one on failure.

        Cached complete-source results are reused. Index materialization is currently
        full-snapshot; compute reuse does not imply constant storage or write cost.
        """
        selected = set(surfaces)
        if not selected or not selected <= {'kb', 'graph', 'db'}:
            raise ConfigError('workspace surfaces must be a nonempty subset of kb, graph, db')
        if ('graph' in selected and self.settings.graph.backend != 'networkx') or (
            'db' in selected and (self.settings.db.dialect != 'sqlite' or self.settings.db.dsn)
        ):
            raise ConfigError('workspace snapshots currently require local NetworkX and SQLite without an external DSN')
        source = Path(path).expanduser().resolve()
        inventory = self._inventory(source)
        pipeline = {'version': 'workspace-v1', 'surfaces': sorted(selected),
                    'embedding': self.settings.embedding.model_dump(mode='json', exclude={'api_key'}),
                    'model': self.settings.llm.fallback_model or self.settings.llm.model,
                    'chunk_size': self.settings.retrieval.chunk_size,
                    'chunk_overlap': self.settings.retrieval.chunk_overlap,
                    'relation_types': relation_types}
        # Persist only the fingerprint; configuration can include private endpoints.
        pipeline_hash = _digest(json.dumps(pipeline, sort_keys=True, default=str).encode())
        revision = 'revision-' + uuid.uuid4().hex
        parent = self.current_revision
        with self._connect() as conn:
            conn.execute('INSERT INTO revisions VALUES (?, ?, ?, NULL)', (revision, parent, 'BUILDING'))
        settings = self._settings_for(revision)
        system = None
        reused = 0
        manifest_sources = []
        try:
            system = await self._system(settings, selected)
            ingest = system.services.ingest
            graph_triples = []
            for item in inventory:
                original = Path(item['uri'])
                snapshot = settings.data.data_dir / 'uploads' / item['relative_path']
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                raw = original.read_bytes()
                if 'sha256:' + _digest(raw) != item['version']:
                    raise CapabilityError('workspace', 'source changed during build; retry')
                snapshot.write_bytes(raw)
                source_id = 'file:' + _digest(item['uri'].encode())
                cache_key = _digest(json.dumps([source_id, item['version'], pipeline_hash]).encode())
                cache_path = self.root / 'checkpoints' / (cache_key + '.json')
                if cache_path.is_file():
                    prepared = json.loads(cache_path.read_text())
                    reused += 1
                else:
                    prepared = {'chunks': [], 'vectors': [], 'triples': [], 'tables': []}
                    is_table = original.suffix.lower() in TABLE_EXTS
                    text = extract_document(snapshot).text if original.suffix.lower() in DOCUMENT_EXTS else ''
                    if 'kb' in selected and text:
                        parts = _split_text(text, chunk_size=settings.retrieval.chunk_size,
                                            chunk_overlap=settings.retrieval.chunk_overlap)
                        prepared['chunks'] = [
                            {'id': _hash(part, item['uri'], ordinal=i), 'text': part,
                             'metadata': {'source': item['uri'], 'source_id': source_id,
                                          'source_version': item['version'], '_chunk_ordinal': i}}
                            for i, part in enumerate(parts)
                        ]
                        if parts:
                            prepared['vectors'] = await system.services.kb.embedding.embed_texts(parts)
                    if 'graph' in selected and text and not is_table:
                        parts = _split_text(text, chunk_size=settings.retrieval.chunk_size, chunk_overlap=0)
                        for i, part in enumerate(parts, 1):
                            triples = await ingest._extract_graph_triples(
                                text=part, max_triples=30, source=item['uri'],
                                source_version=item['version'], locator={'part': i},
                                relation_types=relation_types,
                            )
                            prepared['triples'].extend(t.model_dump(mode='json') for t in triples)
                    if 'db' in selected and is_table:
                        prepared['tables'] = extract_tabular(snapshot)
                    _atomic_json(cache_path, prepared)
                if prepared['chunks']:
                    chunks = prepared['chunks']
                    await system.services.kb.vector_store.add(
                        ids=[c['id'] for c in chunks], texts=[c['text'] for c in chunks],
                        embeddings=prepared['vectors'], metadatas=[c['metadata'] for c in chunks])
                graph_triples.extend(GraphTriple(**t) for t in prepared['triples'])
                for sheet, columns, rows in prepared['tables']:
                    if not rows:
                        continue
                    table = 'ws_' + _digest((source_id + sheet).encode())[:24]
                    safe_cols, safe_rows = ingest._read_from_records(rows, source_file=item['uri'])
                    await ingest._ingest_rows(table=table, rows=safe_rows, safe_cols=safe_cols,
                                              if_exists='replace', source_file=item['uri'])
                manifest_sources.append({**item, 'source_id': source_id,
                    'snapshot': str(snapshot.relative_to(settings.data.base_dir))})
            if 'graph' in selected:
                await system.services.graph.store.upsert_triples(graph_triples)
                await system.services.graph.store.persist()
            if 'kb' in selected:
                await system.services.kb.record_incremental_ingest()
            await system.aclose()
            system = None
            if self._inventory(source) != inventory:
                raise CapabilityError('workspace', 'workspace changed during build; retry')
            manifest = {'workspace_revision': revision, 'parent_revision': parent,
                        'surfaces': sorted(selected), 'pipeline_hash': pipeline_hash,
                        'sources': manifest_sources, 'reused_sources': reused}
            _atomic_json(settings.data.base_dir / 'manifest.json', manifest)
            with self._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT revision FROM head WHERE singleton=1').fetchone()[0]
                if current != parent:
                    raise CapabilityError('workspace', 'publication conflict; another build published first')
                conn.execute('UPDATE revisions SET state=? WHERE id=?', ('PUBLISHED', revision))
                conn.execute('UPDATE head SET revision=? WHERE singleton=1', (revision,))
            return manifest
        except BaseException as exc:
            with self._connect() as conn:
                conn.execute('UPDATE revisions SET state=?, error=? WHERE id=?',
                             ('FAILED', f'{type(exc).__name__}: {exc}', revision))
            raise
        finally:
            if system is not None:
                await system.aclose()

    @asynccontextmanager
    async def reader(self, revision: str | None = None):
        revision = revision or self.current_revision
        if revision is None:
            raise CapabilityError('workspace', 'no published revision')
        manifest = self.manifest(revision)
        settings = self._settings_for(revision)
        system = await self._system(settings, manifest['surfaces'])
        reader = WorkspaceReader(system, manifest, settings.data.base_dir)
        try:
            yield reader
        finally:
            await system.aclose()

    async def query(self, message: str, *, revision: str | None = None, **kwargs):
        async with self.reader(revision) as reader:
            return await reader.query(message, **kwargs)


class WorkspaceReader:
    """Read-only facade fixed to a retained generation, including raw evidence."""

    def __init__(self, system, manifest, root):
        self._system = system
        self.manifest = manifest
        self.revision = manifest['workspace_revision']
        mapping = {s['uri']: root / s['snapshot'] for s in manifest['sources']}
        raw = system.retrieve.tools.get('raw_file_read')
        original = raw.handler

        async def read_snapshot(path: str, **kwargs):
            resolved = mapping.get(path)
            if resolved is None:
                candidate = Path(path).expanduser().resolve()
                if candidate not in mapping.values():
                    raise CapabilityError('workspace', 'raw evidence must belong to the pinned revision')
                resolved = candidate
            return await original(path=str(resolved), **kwargs)
        system.retrieve.tools.replace(replace(raw, handler=read_snapshot))

    async def call(self, name: str, **kwargs):
        context = RequestContext.new(profile=self._system.profile)
        context.extra['workspace_revision'] = self.revision
        with bind_context(context):
            return await self._system.retrieve.tools.get(name).handler(**kwargs)

    async def query(self, message: str, **kwargs):
        context = RequestContext.new(profile=self._system.profile)
        context.extra['workspace_revision'] = self.revision
        with bind_context(context):
            result = await self._system.query(message, **kwargs)
        return {**result, 'workspace_revision': self.revision}
