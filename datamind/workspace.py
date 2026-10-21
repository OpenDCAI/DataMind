"""Immutable workspace revisions over the existing DataMind capability services.

The catalog is safe across processes on one host. Builds prepare private indexes;
publication uses compare-and-swap against their starting revision. Readers retain
one immutable revision across all tool calls. Incremental storage shares source
objects, source-scoped graph assertions, LanceDB versions and Dolt table trees.
Legacy local snapshots remain readable and can be migrated explicitly.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import replace
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from datamind.capabilities.ingest.formats import DOCUMENT_EXTS, TABLE_EXTS, extract_document, extract_tabular
from datamind.capabilities.kb.indexer import _hash, _split_text
from datamind.core.context import RequestContext
from datamind.core.errors import CapabilityError, ConfigError, ExternalServiceError
from datamind.core.logging import bind_context, current_context
from datamind.core.protocols import GraphTriple, GraphEvidence
from datamind.core.recovery import recover_read, recovery_summary
from datamind.workspace_catalog import WorkspaceCatalog


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json(path: Path, value: Any, *, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
    finally:
        temporary.unlink(missing_ok=True)


class Workspace(WorkspaceCatalog):
    """A profile-scoped snapshot catalog. Pass separate settings per tenant."""

    def __init__(self, settings, *, builder=None):
        self.settings = settings.model_copy(deep=True)
        self.root = (settings.data.storage_dir / 'workspace').resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._catalog = self.root / 'catalog.sqlite3'
        self._builder = builder
        with self._connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY, parent TEXT, state TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS head (singleton INTEGER PRIMARY KEY, revision TEXT, epoch INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO head (singleton, revision) VALUES (1, NULL);
            ''')
            if 'epoch' not in {row[1] for row in conn.execute('PRAGMA table_info(head)')}:
                conn.execute('ALTER TABLE head ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0')
        self._init_catalog()

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

    def rollback(self, revision: str, *, expected_revision: str | None = None) -> dict[str, Any]:
        """Repoint to a retained complete snapshot without deleting history."""
        with self._lock():
            return self._rollback(revision, expected_revision=expected_revision)

    def _rollback(self, revision, *, expected_revision):
        self.manifest(revision)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            head = conn.execute('SELECT revision FROM head WHERE singleton=1').fetchone()[0]
            if expected_revision is not None and head != expected_revision:
                raise CapabilityError('workspace', 'publication conflict; current revision changed')
            conn.execute('UPDATE head SET revision=?, epoch=epoch+1 WHERE singleton=1', (revision,))
        return {'workspace_revision': revision, 'previous_revision': head}

    def _settings_for(self, revision: str):
        settings = self.settings.model_copy(deep=True)
        settings.data.base_dir = self.root / 'revisions' / revision
        settings.kb.ingest_mode = 'direct'
        return settings

    async def _system(self, settings, surfaces):
        if self._builder is not None:
            return await self._builder(settings, enable=set(surfaces))
        from datamind.data_plane import build_data_plane
        return await build_data_plane(settings, enable=set(surfaces))

    async def migrate(self):
        from datamind.workspace_migration import migrate
        return await migrate(self)

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
            sha = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''): sha.update(block)
            inventory.append({'relative_path': relative.as_posix(), 'uri': str(path.resolve()),
                              'version': 'sha256:' + sha.hexdigest()})
        return inventory

    async def update(self, path: str, *, surfaces=('kb', 'graph', 'db'), relation_types=None, max_triples_per_file=200, graph_sources=None) -> dict[str, Any]:
        """Publish a generation and report logical compute/materialization costs.

        Metrics are per attempt, including unsuccessful attempts. Provider usage
        is reported only when available; absent token usage is not a zero cost.
        """
        metrics = {"sources_total": 0, "sources_reused": 0, "sources_computed": 0,
                   "sources_completed": 0, "embedding_calls": 0, "embedding_texts": 0,
                   "graph_extraction_calls": 0, "parsed_table_files": 0,
                   "inventory_ms": 0., "setup_ms": 0., "source_copy_ms": 0.,
                   "checkpoint_read_ms": 0., "checkpoint_write_ms": 0.,
                   "source_compute_ms": 0., "materialization_ms": 0., "publication_ms": 0.}
        parent = current_context()
        ctx = replace(parent, extra=dict(parent.extra)) if parent else RequestContext.new(profile=self.settings.data.profile)
        ctx.extra.update(nested_model_usage=[], embedding_usage=[])
        started = time.monotonic()
        try:
            from datamind.workspace_incremental import incremental_enabled, update_incremental
            update = update_incremental if incremental_enabled(self, surfaces) else Workspace._update
            with bind_context(ctx):
                result = await update(self, path, surfaces=surfaces, relation_types=relation_types,
                    max_triples_per_file=max_triples_per_file, metrics=metrics, graph_sources=graph_sources)
            metrics["status"] = "published"
            if self.settings.workspace.auto_gc:
                try:metrics["retention_cleanup"] = await self.collect(dry_run=False,compact=self.retention['mode']!='all')
                except Exception as exc:metrics["cleanup_error"] = type(exc).__name__
            return {**result, "metrics": metrics}
        except BaseException as exc:
            metrics.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            metrics["total_ms"] = round((time.monotonic() - started) * 1000, 3)
            usage = ctx.extra.get("nested_model_usage", [])
            embed_usage = ctx.extra.get("embedding_usage", [])
            metrics["model_usage_records"] = usage
            metrics["embedding_usage_records"] = embed_usage
            metrics["reported_model_input_tokens"] = sum(x.get("input_tokens", 0) for x in usage) if usage else None
            metrics["reported_model_output_tokens"] = sum(x.get("output_tokens", 0) for x in usage) if usage else None
            metrics["reported_embedding_tokens"] = sum(x.get("total_tokens", 0) for x in embed_usage) if embed_usage else None
            if parent is not None:
                parent.extra.setdefault("nested_model_usage", []).extend(usage)
                parent.extra.setdefault("embedding_usage", []).extend(embed_usage)
            revision = metrics.get('workspace_revision')
            if revision:
                # GC cannot remove a revision between checking it and persisting
                # metrics; a metrics write must never resurrect a pruned directory.
                with self._lock():
                    report = self.root / 'revisions' / revision / 'build_metrics.json'
                    if report.parent.is_dir():
                        try: _atomic_json(report, metrics)
                        except OSError: metrics['metrics_persisted'] = False

    async def _update(self, path: str, *, surfaces, relation_types, max_triples_per_file, metrics, graph_sources=None) -> dict[str, Any]:
        """Prepare and publish one generation, retaining the previous one on failure.

        Cached complete-source results are reused. Index materialization is currently
        full-snapshot; compute reuse does not imply constant storage or write cost.
        """
        if not 1 <= max_triples_per_file <= 10000:
            raise ConfigError('max_triples_per_file must be between 1 and 10000')
        selected = set(surfaces)
        if not selected or not selected <= {'kb', 'graph', 'db'}:
            raise ConfigError('workspace surfaces must be a nonempty subset of kb, graph, db')
        if ('graph' in selected and self.settings.graph.backend != 'networkx') or (
            'db' in selected and (self.settings.db.dialect != 'sqlite' or self.settings.db.dsn)
        ):
            raise ConfigError('workspace snapshots currently require local NetworkX and SQLite without an external DSN')
        source = Path(path).expanduser().resolve()
        stage_started = time.monotonic()
        inventory = await asyncio.to_thread(self._inventory, source)
        metrics['inventory_ms'] = (time.monotonic() - stage_started) * 1000
        metrics['sources_total'] = len(inventory)
        external = None
        if graph_sources is not None:
            external = {}
            expected = {item['uri']: item for item in inventory
                        if Path(item['uri']).suffix.lower() in DOCUMENT_EXTS - TABLE_EXTS}
            for entry in graph_sources:
                uri = entry['uri']
                if uri not in expected or uri in external or entry['version'] != expected[uri]['version']:
                    raise ConfigError('Graph source must match the prepared inventory and content hash')
                triples = [GraphTriple(**value) for value in entry['triples']]
                if len(triples) > max_triples_per_file:
                    raise ConfigError('Too many graph triples for source')
                for triple in triples:
                    if relation_types is not None and triple.relation not in relation_types:
                        raise ConfigError('Graph relation is outside the requested vocabulary')
                    triple.source = uri
                    triple.evidence = [GraphEvidence(
                        source_id='file:' + _digest(uri.encode()), source_version=entry['version'],
                        locator={'uri': uri}, method='extracted', pipeline_version='codex-explicit-v1')]
                external[uri] = {'triples': [t.model_dump(mode='json') for t in triples],
                                 'truncated': bool(entry.get('truncated', False))}
            if set(external) != set(expected):
                raise ConfigError('Supply triples (including explicit empty lists) for every document source')
        pipeline = {'version': 'workspace-v1', 'surfaces': sorted(selected),
                    'embedding': self.settings.embedding.model_dump(mode='json', exclude={'api_key'}),
                    'model': self.settings.llm.fallback_model or self.settings.llm.model,
                    'chunk_size': self.settings.retrieval.chunk_size,
                    'chunk_overlap': self.settings.retrieval.chunk_overlap,
                    'relation_types': relation_types, 'max_triples_per_file': max_triples_per_file}
        pipeline['graph_extraction'] = 'codex-explicit-v1' if external is not None else 'legacy-model'
        context = current_context()
        applied_skills = list(context.extra.get('applied_skills', {}).values()) if context else []
        pipeline['applied_skills'] = applied_skills
        # Persist only the fingerprint; configuration can include private endpoints.
        pipeline_hash = _digest(json.dumps(pipeline, sort_keys=True, default=str).encode())
        revision = 'revision-' + uuid.uuid4().hex
        metrics['workspace_revision'] = revision
        with self._connect() as conn:
            parent, parent_epoch = conn.execute('SELECT revision, epoch FROM head WHERE singleton=1').fetchone()
            conn.execute('INSERT INTO revisions VALUES (?, ?, ?, NULL)', (revision, parent, 'BUILDING'))
        settings = self._settings_for(revision)
        system = None
        reused = 0
        manifest_sources = []
        truncated_sources = []
        bindings = {}
        try:
            stage_started = time.monotonic()
            system = await self._system(settings, selected)
            metrics['setup_ms'] = (time.monotonic() - stage_started) * 1000
            ingest = system.services.ingest
            (settings.data.data_dir / 'uploads').mkdir(parents=True, exist_ok=True)
            graph_triples = []
            for item in inventory:
                stage_started = time.monotonic()
                original = Path(item['uri'])
                snapshot = settings.data.data_dir / 'uploads' / item['relative_path']
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                raw = await asyncio.to_thread(original.read_bytes)
                if 'sha256:' + _digest(raw) != item['version']:
                    raise CapabilityError('workspace', 'source changed during build; retry')
                snapshot.write_bytes(raw)
                metrics['source_copy_ms'] += (time.monotonic() - stage_started) * 1000
                source_id = 'file:' + _digest(item['uri'].encode())
                cache_key = _digest(json.dumps([source_id, item['version'], pipeline_hash,
                    external.get(item['uri']) if external is not None else None], sort_keys=True).encode())
                cache_path = self.root / 'checkpoints' / (cache_key + '.json')
                stage_started = time.monotonic()
                if cache_path.is_file():
                    prepared = json.loads(cache_path.read_text())
                    reused += 1
                    metrics['sources_reused'] += 1
                    metrics['checkpoint_read_ms'] += (time.monotonic() - stage_started) * 1000
                else:
                    prepared = {'chunks': [], 'vectors': [], 'triples': [], 'tables': [], 'truncated': False}
                    is_table = original.suffix.lower() in TABLE_EXTS
                    text = (await asyncio.to_thread(extract_document, snapshot)).text if original.suffix.lower() in DOCUMENT_EXTS else ''
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
                            metrics['embedding_calls'] += 1
                            metrics['embedding_texts'] += len(parts)
                            prepared['vectors'] = await system.services.kb.embedding.embed_texts(parts)
                    if 'graph' in selected and external is not None:
                        prepared.update(external.get(item['uri'], {'triples': [], 'truncated': False}))
                    if 'graph' in selected and external is None and text and not is_table:
                        parts = _split_text(text, chunk_size=settings.retrieval.chunk_size, chunk_overlap=0)
                        for i, part in enumerate(parts, 1):
                            remaining = max_triples_per_file - len(prepared['triples'])
                            if remaining <= 0:
                                prepared['truncated'] = True
                                break
                            metrics['graph_extraction_calls'] += 1
                            triples = await ingest._extract_graph_triples(
                                text=part, max_triples=min(30, remaining), source=item['uri'],
                                source_version=item['version'], locator={'part': i},
                                relation_types=relation_types,
                            )
                            prepared['triples'].extend(t.model_dump(mode='json') for t in triples)
                            if len(triples) >= min(30, remaining):
                                prepared['truncated'] = True
                    if 'db' in selected and is_table:
                        metrics['parsed_table_files'] += 1
                        prepared['tables'] = await asyncio.to_thread(extract_tabular, snapshot)
                    metrics['source_compute_ms'] += (time.monotonic() - stage_started) * 1000
                    metrics['sources_computed'] += 1
                    stage_started = time.monotonic()
                    _atomic_json(cache_path, prepared, overwrite=False)
                    prepared = json.loads(cache_path.read_text())
                    metrics['checkpoint_write_ms'] += (time.monotonic() - stage_started) * 1000
                stage_started = time.monotonic()
                if prepared.get('truncated'):
                    truncated_sources.append(item['uri'])
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
                    'snapshot': str(snapshot.relative_to(settings.data.base_dir)),
                    'checkpoint': str(cache_path.relative_to(self.root))})
                metrics['sources_completed'] += 1
                metrics['materialization_ms'] += (time.monotonic() - stage_started) * 1000
            stage_started = time.monotonic()
            if 'graph' in selected:
                await system.services.graph.store.upsert_triples(graph_triples)
                await system.services.graph.store.persist()
            if 'kb' in selected:
                await system.services.kb.record_incremental_ingest()
            if 'kb' in selected:
                bindings['kb'] = {'provider': settings.kb.vector_store,
                    'path': str(settings.data.storage_dir.relative_to(settings.data.base_dir)),
                    'version': getattr(system.services.kb.vector_store, 'latest_version', None)}
            if 'graph' in selected:
                graph_path = settings.data.storage_dir / 'graph.json'
                bindings['graph'] = {'provider': 'networkx',
                    'path': str(graph_path.relative_to(settings.data.base_dir)),
                    'sha256': _digest(graph_path.read_bytes())}
            if 'db' in selected:
                db_path = Path(system.services.db.engine.url.database)
                bindings['db'] = {'provider': 'sqlite',
                    'path': str(db_path.relative_to(settings.data.base_dir))}
            await system.aclose()
            system = None
            metrics['materialization_ms'] += (time.monotonic() - stage_started) * 1000
            stage_started = time.monotonic()
            if await asyncio.to_thread(self._inventory, source) != inventory:
                raise CapabilityError('workspace', 'workspace changed during build; retry')
            manifest = {'workspace_revision': revision, 'parent_revision': parent, 'source_root': str(source), 'parent_epoch': parent_epoch,
                        'surfaces': sorted(selected), 'pipeline_hash': pipeline_hash,
                        'sources': manifest_sources, 'reused_sources': reused,
                        'snapshots': bindings, 'truncated_sources': truncated_sources,
                        'applied_skills': applied_skills}
            _atomic_json(settings.data.base_dir / 'manifest.json', manifest)
            with self._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current, epoch = conn.execute('SELECT revision, epoch FROM head WHERE singleton=1').fetchone()
                if (current, epoch) != (parent, parent_epoch):
                    raise CapabilityError('workspace', 'publication conflict; another build published first')
                conn.execute('UPDATE revisions SET state=? WHERE id=?', ('PUBLISHED', revision))
                conn.execute('UPDATE head SET revision=?, epoch=epoch+1 WHERE singleton=1', (revision,))
            metrics['publication_ms'] = (time.monotonic() - stage_started) * 1000
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
    async def reader(self, revision: str | None = None, *, read_retries: int = 1, enable=None):
        token,revision=self.acquire_lease(revision)
        if revision is None:
            raise CapabilityError('workspace', 'no published revision')
        system=None
        heartbeat=asyncio.create_task(self.keepalive(token))
        try:
            manifest=self.manifest(revision)
            selected=set(manifest['surfaces']) if enable is None else set(enable)
            if not selected<=set(manifest['surfaces']):
                raise CapabilityError('workspace','requested surface is absent from the pinned revision')
            if manifest.get('format_version')==2:
                from datamind.workspace_incremental import reader_system
                system,settings=await reader_system(self,manifest,selected)
                root=self.root
            else:
                settings=self._settings_for(revision)
                if manifest.get('snapshots',{}).get('kb'):
                    settings.kb.vector_store=manifest['snapshots']['kb']['provider']
                if manifest.get('snapshots',{}).get('db'):
                    settings.db.dialect=manifest['snapshots']['db']['provider'];settings.db.dsn=None
                settings.graph.backend='networkx'
                system=await self._system(settings,selected)
                root=settings.data.base_dir
            reader=WorkspaceReader(system,manifest,root,read_retries=read_retries,
                timeout_s=settings.agent.wall_clock_timeout_s)
            reader._system_settings=self.settings
            reader._lease=(self,token)
            yield reader
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat,return_exceptions=True)
            try:
                if system is not None: await system.aclose()
            finally: self.release_lease(token)


    async def query(self, message: str, *, revision: str | None = None, read_retries: int = 1, **kwargs):
        async with self.reader(revision, read_retries=read_retries) as reader:
            return await reader.query(message, **kwargs)


class WorkspaceReader:
    """Read-only facade fixed to a retained generation, including raw evidence."""

    def __init__(self, system, manifest, root, *, read_retries=1, timeout_s=300.):
        if not 0 <= read_retries <= 5:
            raise ValueError("read_retries must be between 0 and 5")
        self._system = system
        self._timeout = timeout_s
        self.manifest = manifest
        self.revision = manifest['workspace_revision']
        mapping = {s['uri']: root / s['snapshot'] for s in manifest['sources']}
        tools = system.read_tools
        system.tools = tools
        self.tools = tools
        raw = tools.get('raw_file_read')
        original = raw.handler

        async def read_snapshot(path: str, **kwargs):
            resolved = mapping.get(path)
            if resolved is None:
                candidate = Path(path).expanduser().resolve()
                if candidate not in mapping.values():
                    raise CapabilityError('workspace', 'raw evidence must belong to the pinned revision')
                resolved = candidate
            return await original(path=str(resolved), **kwargs)
        tools.replace(replace(raw, handler=read_snapshot))

        inspect = tools.get('workspace_inspect')
        original_inspect = inspect.handler
        source_root = Path(manifest['source_root'])
        snapshot_root = root / 'data' / 'profiles' / system.profile / 'uploads'

        async def inspect_snapshot(path: str, **kwargs):
            candidate = Path(path).expanduser().resolve()
            if manifest.get('format_version')==2:
                if candidate!=source_root and not candidate.is_relative_to(source_root):
                    raise CapabilityError('workspace','inspection must belong to the pinned revision')
                items=[{'path':s['uri'],'relative_path':s['relative_path'],'sha256':s['version'].removeprefix('sha256:')}
                    for s in manifest['sources'] if Path(s['uri']).is_relative_to(candidate) or Path(s['uri'])==candidate]
                limit=kwargs.get('max_files',2000)
                return {'path':str(candidate),'files':items[:limit],'count':len(items[:limit]),'truncated':len(items)>limit,
                        'workspace_revision':self.revision}

            if candidate.is_relative_to(source_root):
                candidate = snapshot_root / candidate.relative_to(source_root)
            elif not candidate.is_relative_to(snapshot_root):
                raise CapabilityError('workspace', 'inspection must belong to the pinned revision')
            return await original_inspect(path=str(candidate), **kwargs)
        tools.replace(replace(inspect, handler=inspect_snapshot))

        if 'kb_search_at_version' in tools:
            historical = tools.get('kb_search_at_version')
            original_historical = historical.handler
            pinned = manifest['snapshots'].get('kb', {}).get('version')

            async def search_pinned(version: int, **kwargs):
                if version != pinned:
                    raise CapabilityError('workspace', 'select a workspace revision instead of another backend version')
                return await original_historical(version=version, **kwargs)
            tools.replace(replace(historical, handler=search_pinned))

        for name in tools.names():
            spec = tools.get(name)
            original_handler = spec.handler

            async def retry_read(_handler=original_handler, _name=name, **kwargs):
                return await recover_read(lambda: _handler(**kwargs), component=_name,
                                          retries=read_retries, manage_provider_retries=True)
            tools.replace(replace(spec, handler=retry_read))

    async def call(self, name: str, **kwargs):
        if hasattr(self,'_lease'):
            self._lease[0].renew_lease(self._lease[1])
        parent = current_context()
        if parent is not None:
            parent.extra.setdefault('recovery_events', [])
        context = replace(parent, extra=dict(parent.extra)) if parent else RequestContext.new(profile=self._system.profile)
        context.extra['workspace_revision'] = self.revision
        context.extra['query_deadline'] = time.monotonic() + self._timeout
        with bind_context(context):
            return await self._system.call(name, kwargs, context=context, read_only=True)

    async def query(self, message: str, **kwargs):
        context = RequestContext.new(profile=self._system.profile)
        context.extra['workspace_revision'] = self.revision
        context.extra['query_deadline'] = time.monotonic() + self._timeout
        with bind_context(context):
            async with asyncio.timeout(self._timeout):
                from datamind.host import CodexHost
                settings = self._system_settings
                host = CodexHost(settings, revision=self.revision, runtime_env=kwargs.pop('runtime_env', None))
                try:
                    result = await host.query(message, **kwargs)
                finally:
                    await host.aclose()
        recovery = result.get('recovery') or recovery_summary(context.extra.get('recovery_events', []))
        return {**result, 'workspace_revision': self.revision,
                'truncated_sources': self.manifest.get('truncated_sources', []),
                'workspace_read_retries': recovery['retries'],
                'recovery': recovery}
