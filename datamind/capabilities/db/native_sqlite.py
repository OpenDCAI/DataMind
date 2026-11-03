"""Registered SQLite sources: consistent snapshots or trusted immutable inputs.

Registration preserves the database itself. No rows are copied into the managed
SQL store. DataSpace references are never owned or removed by workspace GC.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager, closing
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from datamind.core.errors import CapabilityError
from .providers.sqlite import SQLiteDialect

PIPELINE = 'native-sqlite-v1'


def sha_file(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            sha.update(block)
    return sha.hexdigest()


def source_version(path, *, reference=False):
    path = Path(path)
    wal = Path(str(path) + '-wal')
    journal = Path(str(path) + '-journal')
    if journal.exists() and journal.stat().st_size:
        raise CapabilityError('db', 'SQLite has a rollback journal; finish recovery before registration')
    if wal.exists() and wal.stat().st_size:
        if reference:
            raise CapabilityError('db', 'DataSpace SQLite references require a standalone database without a nonempty WAL')
        return 'sha256:' + hashlib.sha256((sha_file(path) + ':' + sha_file(wal)).encode()).hexdigest()
    return 'sha256:' + sha_file(path)


def prepare_source(original, target, item, *, reference=False, timeout_s=30):
    """Pin original bytes, or use SQLite backup to include committed WAL data."""
    original, target = Path(original).resolve(), Path(target)
    # DataSpace inputs are immutable by contract and were inventoried once.
    if reference:
        return original
    before = source_version(original, reference=reference)
    if before != item['version']:
        raise CapabilityError('db', 'SQLite source changed before registration; rebuild the workspace')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.sqlite-')
    os.close(fd)
    deadline = time.monotonic() + timeout_s
    try:
        def progress(*_):
            if time.monotonic() > deadline:
                raise CapabilityError('db', 'SQLite snapshot backup timed out')
        with closing(sqlite3.connect(original.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination, pages=256, progress=progress)
                destination.execute('PRAGMA journal_mode=DELETE')
        if source_version(original) != before:
            raise CapabilityError('db', 'SQLite source changed during backup; retry')
        os.replace(temporary, target)
        return target
    finally:
        Path(temporary).unlink(missing_ok=True)


def inspect_database(path, item, *, reference=False):
    """Read schema objects and declared relations without materializing rows."""
    from datamind.capabilities.ingest.tables import table_name, quote
    database_id = 'sqlite_' + hashlib.sha256(item['uri'].encode()).hexdigest()[:24]
    tables = []
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as con:
        objects = [dict(zip(('type', 'name', 'table', 'sql'), row)) for row in con.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name')]
        # Shadow tables remain in the file; expose their owning virtual table.
        shadows = {row[1] for row in con.execute('PRAGMA table_list') if row[2] == 'shadow'}
        for obj in objects:
            name = obj['name']
            if obj['type'] not in {'table', 'view'} or name.startswith('sqlite_') or name in shadows:
                continue
            error = None
            try:
                info = con.execute('PRAGMA table_xinfo(' + quote(name) + ')').fetchall()
                foreign = [{'id': r[0], 'sequence': r[1], 'table': r[2], 'from': r[3], 'to': r[4],
                            'on_update': r[5], 'on_delete': r[6], 'match': r[7]}
                           for r in con.execute('PRAGMA foreign_key_list(' + quote(name) + ')')]
            except sqlite3.Error as exc:
                info, foreign, error = [], [], str(exc)
            mapping = [{'ordinal': i + 1, 'original_name': c[1], 'name': c[1]} for i, c in enumerate(info)]
            tables.append({'name': table_name(item['uri'], name), 'sheet': name,
                           'database_id': database_id, 'native_table': name, 'native_sqlite': True,
                           'object_type': 'virtual' if (obj['sql'] or '').lstrip().upper().startswith('CREATE VIRTUAL') else obj['type'],
                           'sql': obj['sql'], 'columns': [c[1] for c in info], 'types': [c[2] for c in info],
                           'declared_types': [c[2] for c in info], 'column_mapping': mapping,
                           'column_definitions': [{'name': c[1], 'type': c[2], 'not_null': bool(c[3]),
                                                   'default': c[4], 'primary_key_order': c[5], 'hidden': c[6]} for c in info],
                           'primary_key': [c[1] for c in sorted(info, key=lambda c: c[5]) if c[5]],
                           'foreign_keys': foreign, 'source': item['uri'], 'source_version': item['version'],
                           'rows': None, 'schema_error': error})
    return {'database_id': database_id, 'source_uri': item['uri'], 'source_version': item['version'],
            'sha256': item['version'].removeprefix('sha256:') if reference else sha_file(path),
            'version': item['version'], 'schema_objects': objects, 'tables': tables}


def registration(path, item, *, reference, revision=None, schema=None):
    entry = dict(schema or inspect_database(path, item, reference=reference))
    entry.update(path=str(Path(path).resolve()), storage='reference' if reference else 'snapshot',
                 workspace_revision=revision)
    return entry


def _file_state(path):
    """Invalidate cached checks on replacement, writes, or SQLite sidecar changes."""
    def state(candidate, *, optional=False):
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            if optional:
                return None
            raise
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    return (state(path), state(Path(str(path) + '-wal'), optional=True),
            state(Path(str(path) + '-journal'), optional=True))


@lru_cache(maxsize=128)
def _validate_file(path, expected, state):
    if source_version(path, reference=True) != 'sha256:' + expected:
        raise CapabilityError('db', 'Registered SQLite changed; rebuild the workspace before querying')
    if _file_state(path) != state:
        raise CapabilityError('db', 'Registered SQLite changed during validation; retry')


def validate_entry(entry):
    """Reuse a successful hash check while filesystem identity and metadata match."""
    path = Path(entry['path']).resolve()
    try:
        _validate_file(path, entry['sha256'], _file_state(path))
    except OSError as exc:
        raise CapabilityError('db', 'Registered SQLite is missing; rebuild or restore its original source') from exc


def databases(db):
    # A pinned reader must never fall back to the live registry.
    pinned = getattr(db, 'native_databases', None)
    if pinned is not None:
        return pinned
    path = getattr(db, 'native_registry_path', None)
    return json.loads(path.read_text()) if path and path.is_file() else {}


@asynccontextmanager
async def database_target(db, database_id=None, database_ids=None):
    """Keep existing managed queries unchanged unless a database ID is selected."""
    if database_id is None and not database_ids:
        yield db
        return
    registry = databases(db)
    selected = list(dict.fromkeys(([database_id] if database_id else []) + (database_ids or [])))
    invalid = [key for key in selected if key not in registry]
    if invalid:
        available = [{'database_id': key, 'source_uri': entry.get('source_uri')}
                     for key, entry in sorted(registry.items())[:20]]
        details = {'code': 'unknown_database_id', 'invalid_database_ids': invalid,
                   'available_databases': available, 'available_count': len(registry),
                   'available_truncated': len(registry) > len(available),
                   'hint': 'Copy an exact database_id from these records or db_list_databases in this operation; '
                           'do not shorten or reconstruct it. For managed imported tables omit database_id. '
                           'Correct the tool arguments; re-importing the same file does not repair a mistyped ID.'}
        raise CapabilityError('db', 'Unknown database_id. ' + json.dumps(details, ensure_ascii=False), details=details)
    entries = []
    for key in selected:
        entries.append(registry[key])
    if len(database_ids or []) > 10:
        raise CapabilityError('db', 'SQLite supports at most 10 attached databases per query')
    verify_sources = not getattr(db, 'native_reference', False)
    if verify_sources:
        for entry in entries:
            await asyncio.to_thread(validate_entry, entry)
    if database_id:
        path = registry[database_id]['path']
    elif db.dialect.name == 'sqlite' and getattr(db, 'engine', None) is not None:
        path = db.engine.url.database
        if not path or not Path(path).is_file():
            raise CapabilityError('db', 'Create the managed database or select a native database_id first')
    else:
        raise CapabilityError('db', 'Cross-file SQLite joins require a SQLite primary database')
    from .service import DBService

    def connect():
        uri = Path(path).resolve().as_uri() + ('?mode=ro&immutable=1' if database_id else '?mode=ro')
        con = sqlite3.connect(uri, uri=True)
        try:
            for entry in entries:
                if entry['database_id'] != database_id:
                    con.execute('ATTACH DATABASE ? AS "' + entry['database_id'] + '"',
                                (Path(entry['path']).as_uri() + '?mode=ro&immutable=1',))
        except BaseException:
            con.close()
            raise
        con.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + db.db_cfg.query_timeout_s
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        con.enable_load_extension(False)
        # SQL guards plus SQLite enforce reads, including queries through views.
        con.set_authorizer(lambda action, arg1, arg2, *_: sqlite3.SQLITE_DENY
                           if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
                           or action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in {'load_extension', 'writefile', 'readfile'}
                           else sqlite3.SQLITE_OK)
        return con

    engine = create_engine('sqlite://', creator=connect, poolclass=NullPool)
    service = DBService(dialect=SQLiteDialect(), engine=engine, db_cfg=db.db_cfg,
                        llm_client=getattr(db, '_llm', None), llm_model=getattr(db, '_model', None),
                        result_dir=db.result_dir)
    try:
        yield service
        if verify_sources:
            for entry in entries:
                await asyncio.to_thread(validate_entry, entry)
    finally:
        await service.aclose()


def register_import(db, path, *, if_exists):
    from filelock import FileLock
    from datamind.workspace import _atomic_json
    registry_path = db.native_registry_path
    if registry_path is None:
        raise CapabilityError('db', 'Native SQLite imports require a configured registry directory')
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    reference = getattr(db, 'native_reference', False)
    item = {'uri': str(Path(path).resolve()), 'version': source_version(path, reference=reference)}
    database_id = 'sqlite_' + hashlib.sha256(item['uri'].encode()).hexdigest()[:24]
    with FileLock(str(registry_path) + '.lock'):
        registry = databases(db)
        old = registry.get(database_id)
        if old and if_exists != 'replace':
            raise CapabilityError('db', 'SQLite is already registered; use if_exists=replace to publish a new snapshot')
        target = registry_path.parent / 'sqlite_sources' / database_id / (item['version'][7:] + '.sqlite')
        snapshot = prepare_source(path, target, item, reference=reference)
        entry = registration(snapshot, item, reference=reference)
        registry[database_id] = entry
        _atomic_json(registry_path, registry)
    return {'source_file': item['uri'], 'database_id': database_id, 'storage': entry['storage'],
            'source_version': item['version'], 'sha256': entry['sha256'], 'tables_processed': len(entry['tables']),
            'tables': [{'table': t['native_table'], 'database_id': database_id, 'columns': t['columns'],
                        'column_mapping': t['column_mapping'], 'rows_inserted': 0} for t in entry['tables']]}


def unregister_import(db, path):
    """Remove a native live registration, never the caller-owned input file."""
    from filelock import FileLock
    from datamind.workspace import _atomic_json
    registry_path = getattr(db, 'native_registry_path', None)
    if registry_path is None or not registry_path.exists():
        return None
    with FileLock(str(registry_path) + '.lock'):
        registry = databases(db)
        keys = [key for key, entry in registry.items() if entry['source_uri'] == str(path)]
        if not keys:
            return None
        tables = [t['native_table'] for key in keys for t in registry[key]['tables']]
        for key in keys:
            del registry[key]
        _atomic_json(registry_path, registry)
    return {'file': str(path), 'database_ids': keys, 'tables': tables, 'status': 'unregistered',
            'source_deleted': False, 'rows_deleted': 0, 'rows_deleted_by_table': {}, 'commit_hash': None}
