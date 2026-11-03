"""Lossless table adapters shared by direct and versioned workspace ingestion."""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from datamind.core.errors import CapabilityError

SQLITE_EXTS = {'.sqlite', '.sqlite3'}
TABLE_PIPELINE = 'tables-v2'


def normalize_columns(header):
    """Keep Unicode identifiers; preserve positional mappings for renamed columns."""
    columns, mapping, used = [], [], set()
    for index, raw in enumerate(header, 1):
        original = '' if raw is None else str(raw)
        name = original.strip()
        if not name or any(ord(c) < 32 for c in name) or name.casefold() == '_source_file' or name.casefold() in used:
            name = f'col_{index}'
        name = name[:64]
        base, suffix = name, 2
        while name.casefold() in used:
            tail = '_' + str(suffix)
            name = base[:64 - len(tail)] + tail
            suffix += 1
        used.add(name.casefold())
        columns.append(name)
        mapping.append({'ordinal': index, 'original_name': original, 'name': name})
    return columns, mapping


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def encode_cell(value):
    if isinstance(value, bytes):
        return {'$binary': base64.b64encode(value).decode('ascii')}
    return value


def decode_cell(value):
    if isinstance(value, dict) and set(value) == {'$binary'}:
        return base64.b64decode(value['$binary'], validate=True)
    return value


def _ijson():
    try:
        import ijson
    except ImportError as exc:
        raise CapabilityError('ingest', 'DataSpace table JSON requires the [dataspace] extra') from exc
    return ijson


def is_table_json(path):
    """Inspect the envelope without materializing a potentially large records array."""
    table, records = None, False
    try:
        with Path(path).open('rb') as handle:
            for prefix, event, value in _ijson().parse(handle, use_float=True):
                if prefix == 'table' and event == 'string':
                    table = value
                if prefix == 'records' and event == 'start_array':
                    records = True
                if table and records:
                    return True
        return False
    except (ValueError, OverflowError):
        return False


def is_structured(path, policy='standard'):
    path = Path(path)
    return path.suffix.lower() in SQLITE_EXTS or (
        policy == 'dataspace' and path.suffix.lower() == '.json' and is_table_json(path))


def _json_rows(path):
    with path.open('rb') as handle:
        for row in _ijson().items(handle, 'records.item', use_float=True):
            if not isinstance(row, dict):
                raise CapabilityError('ingest', 'Table JSON records must be objects')
            if any(isinstance(v, (dict, list)) for v in row.values()):
                raise CapabilityError('ingest', 'Table JSON nested values need an explicit schema')
            yield row


@dataclass
class TableInput:
    name: str
    header: list[str]
    types: list[str]
    rows: Iterable[list[Any]]
    primary_key: list[str]
    foreign_keys: list[dict]
    declared_types: list[str]
    typed: bool = False


def _kind(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 'INTEGER'
    if isinstance(value, int):
        if not -(2 ** 63) <= value < 2 ** 63:
            raise CapabilityError('ingest', 'Integer exceeds the supported signed 64-bit range')
        return 'INTEGER'
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CapabilityError('ingest', 'Non-finite numeric cell is unsupported')
        return 'REAL'
    if isinstance(value, bytes):
        return 'BLOB'
    return 'TEXT'


@contextmanager
def source_tables(path, *, policy='standard'):
    """Open source tables with bounded row iteration; callers must consume inside the context."""
    path = Path(path)
    with ExitStack() as stack:
        suffix = path.suffix.lower()
        if suffix in SQLITE_EXTS:
            con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            stack.callback(con.close)
            con.execute('PRAGMA query_only=ON')
            con.execute('BEGIN')
            entries = con.execute("SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if any(kind == 'view' or (ddl or '').lstrip().upper().startswith('CREATE VIRTUAL') for _, kind, ddl in entries):
                raise CapabilityError('ingest', 'SQLite views/virtual tables require explicit materialization before import')

            def sqlite_tables():
                for name, _, _ in entries:
                    info = con.execute('PRAGMA table_xinfo(' + quote(name) + ')').fetchall()
                    info = [c for c in info if c[6] != 1]
                    header = [c[1] for c in info]
                    # SQLite affinity is retained in the SQLite copy. Portable
                    # incremental materialization separately checks cell types.
                    declared = [c[2] for c in info]
                    kinds = []
                    for value in declared:
                        upper = value.upper()
                        kinds.append('INTEGER' if 'INT' in upper else 'TEXT' if any(t in upper for t in ('CHAR', 'CLOB', 'TEXT')) else 'REAL' if any(t in upper for t in ('REAL', 'FLOA', 'DOUB')) else 'BLOB' if not upper or 'BLOB' in upper else 'NUMERIC')
                    foreign = [{'id': f[0], 'sequence': f[1], 'table': f[2], 'from': f[3], 'to': f[4]} for f in con.execute('PRAGMA foreign_key_list(' + quote(name) + ')')]
                    cursor = con.execute('SELECT ' + ','.join(quote(c) for c in header) + ' FROM ' + quote(name))
                    primary = [c[1] for c in sorted(info, key=lambda c: c[5]) if c[5]]
                    yield TableInput(name, header, kinds, cursor, primary, foreign, declared, True)
            yield sqlite_tables()
        elif suffix == '.json' and policy == 'dataspace' and is_table_json(path):
            with path.open('rb') as handle:
                name = next(_ijson().items(handle, 'table'), None)
            header, kinds, large_integers = [], {}, set()
            for row in _json_rows(path):
                for key, value in row.items():
                    if key not in kinds:
                        header.append(key)
                        kinds[key] = set()
                    kind = _kind(value)
                    if isinstance(value, int) and abs(value) > 2 ** 53:
                        large_integers.add(key)
                    if kind:
                        kinds[key].add(kind)
            types = []
            for key in header:
                seen = kinds[key]
                if seen <= {'INTEGER', 'REAL'}:
                    types.append('BLOB' if 'REAL' in seen and key in large_integers else 'REAL' if 'REAL' in seen else 'INTEGER' if seen else 'BLOB')
                elif len(seen) <= 1:
                    types.append(next(iter(seen), 'BLOB'))
                else:
                    # No coercion of a numeric-looking identifier or mixed cells.
                    types.append('BLOB')
            rows = ([row.get(c) for c in header] for row in _json_rows(path))
            yield iter([TableInput(name, header, types, rows, [], [], types, True)])
        elif suffix in {'.csv', '.tsv'}:
            handle = stack.enter_context(path.open(encoding='utf-8-sig', errors='replace', newline=''))
            rows = csv.reader(handle, delimiter='\t' if suffix == '.tsv' else ',')
            header = next(rows, None)
            yield iter([] if header is None else [TableInput(path.stem, header, ['TEXT'] * len(header), rows, [], [], ['TEXT'] * len(header))])
        elif suffix == '.xlsx':
            import openpyxl
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
            stack.callback(book.close)
            def sheets():
                for sheet in book.worksheets:
                    rows = iter(sheet.values)
                    header = next(rows, None)
                    if header is not None:
                        yield TableInput(sheet.title, list(header), ['TEXT'] * len(header), rows, [], [], ['TEXT'] * len(header))
            yield sheets()
        elif suffix == '.xls':
            raise CapabilityError('ingest', 'legacy .xls import requires conversion to .xlsx')
        else:
            raise CapabilityError('ingest', f'Unsupported table input: {suffix}')


def table_name(source, name):
    source_id = 'file:' + hashlib.sha256(source.encode()).hexdigest()
    return 'ws_' + hashlib.sha256((source_id + name).encode()).hexdigest()[:24]


def materialize(engine, dialect, item, root, *, if_exists='replace'):
    """Load canonical rows using neutral parameter names, without changing source types."""
    from sqlalchemy import text, inspect
    from itertools import islice
    q = dialect._quote_ident
    cols = item['columns']
    types = item.get('types', ['TEXT'] * len(cols))
    if dialect.name == 'dolt':
        translation = {'INTEGER': 'BIGINT', 'REAL': 'DOUBLE', 'TEXT': 'LONGTEXT', 'BLOB': 'LONGBLOB', 'NUMERIC': 'DOUBLE'}
        if item.get('mixed_types') or 'NUMERIC' in types:
            raise CapabilityError('ingest', 'Mixed SQLite/JSON cell types require the SQLite snapshot backend')
        ddl = [translation[t] if item.get('typed') else t for t in types]
    else:
        ddl = types
    with engine.begin() as conn:
        if dialect.name == 'sqlite':
            conn.exec_driver_sql('PRAGMA query_only=OFF')
        exists = item['name'] in inspect(conn).get_table_names()
        if exists and if_exists == 'fail':
            raise CapabilityError('ingest', 'Table already exists: ' + item['name'])
        if exists and if_exists == 'replace':
            conn.execute(text('DROP TABLE ' + q(item['name'])))
            exists = False
        if not exists:
            conn.execute(text('CREATE TABLE ' + q(item['name']) + ' (' + ','.join(q(c) + ' ' + t for c, t in zip(cols, ddl)) + ')'))
        elif [c['name'] for c in inspect(conn).get_columns(item['name'])] != cols:
            raise CapabilityError('ingest', 'Append requires an identical table schema')
        statement = text('INSERT INTO ' + q(item['name']) + ' (' + ','.join(q(c) for c in cols) + ') VALUES (' + ','.join(':p' + str(i) for i in range(len(cols))) + ')')
        with (Path(root) / item['path']).open(encoding='utf-8', newline='') as handle:
            rows = csv.reader(handle)
            next(rows, None)
            while batch := list(islice(rows, 1000)):
                conn.execute(statement, [{f'p{i}': decode_cell(json.loads(v)) for i, v in enumerate(row)} for row in batch])


def table_catalog(tables):
    """The manifest exposes discovery metadata, never row caches or internal paths."""
    return {t['name']: {k: v for k, v in t.items() if k not in {'path', 'hash', 'text_records'}} for t in tables}


def table_records(item, tables):
    records = []
    for table in tables:
        columns = [{'name': m['original_name'], 'sql_name': m['name'], 'type': t} for m, t in zip(table['column_mapping'], table['types'])]
        records.append({'text': 'Table ' + table['sheet'] + ' (SQL: ' + table.get('native_table', table['name']) + (', database_id: ' + table['database_id'] if table.get('database_id') else '') + ')\n' + json.dumps(columns, ensure_ascii=False),
                        'metadata': {'kind': 'table_schema', 'table': table['name'], 'original_table': table['sheet'], **({'database_id': table['database_id']} if table.get('database_id') else {})}})
        records.extend(table.pop('text_records', []))
    return records


def table_graph(item, tables):
    """Only declared schema relationships are facts; matching column names are not foreign keys."""
    from datamind.core.protocols import GraphTriple, GraphEvidence
    source_id = 'file:' + hashlib.sha256(item['uri'].encode()).hexdigest()
    triples = []
    def add(subject, relation, obj, subject_type, object_type, locator):
        triples.append(GraphTriple(subject=subject, relation=relation, object=obj,
            subject_type=subject_type, object_type=object_type, source=item['uri'],
            evidence=[GraphEvidence(source_id=source_id, source_version=item['version'],
                locator={'uri': item['uri'], **locator}, method='explicit', pipeline_version=TABLE_PIPELINE)]).model_dump(mode='json'))
    for table in tables:
        node = 'table:' + table['name']
        loc = {'table': table['name'], 'original_table': table['sheet'],
               **({'database_id': table['database_id']} if table.get('database_id') else {})}
        add(source_id, 'has_table', node, 'file', 'table', loc)
        for m in table['column_mapping']:
            column = node + '#' + m['name']
            add(node, 'has_column', column, 'table', 'column', {**loc, 'column': m['name'], 'original_column': m['original_name']})
        for fk in table['foreign_keys']:
            target = next((t for t in tables if t['sheet'] == fk['table']), None)
            if target is None:
                continue
            src = next((m['name'] for m in table['column_mapping'] if m['original_name'] == fk['from']), None)
            original = fk['to']
            if original is None and fk['sequence'] < len(target['primary_key']):
                original = target['primary_key'][fk['sequence']]
            dest = next((m['name'] for m in target['column_mapping'] if m['original_name'] == original), None)
            if src is not None and dest is not None:
                add(node + '#' + src, 'references', 'table:' + target['name'] + '#' + dest,
                    'column', 'column', {**loc, 'foreign_key_id': fk['id'], 'sequence': fk['sequence']})
    return triples


def validate_sqlite_source(path):
    path = Path(path)
    wal = Path(str(path) + '-wal')
    if path.suffix.lower() in SQLITE_EXTS and wal.exists() and wal.stat().st_size:
        raise CapabilityError('ingest', 'SQLite has an active WAL; export a consistent standalone database before ingestion')


def import_json_table(db, path, *, table, if_exists='append'):
    """Stream a table envelope into a live DB, retaining nulls/types/duplicates."""
    import re
    from itertools import islice
    from sqlalchemy import text, inspect
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', table):
        raise CapabilityError('ingest', 'Invalid JSON table name')
    if if_exists not in {'append', 'replace', 'fail'}:
        raise CapabilityError('ingest', 'Invalid import mode')
    if db.dialect.name != 'sqlite':
        raise CapabilityError('ingest', 'Lossless table JSON import currently requires SQLite')
    q = db.dialect._quote_ident
    with source_tables(path, policy='dataspace') as tables:
        source = next(tables)
        columns, mapping = normalize_columns(source.header)
        if not columns:
            raise CapabilityError('ingest', 'Empty records do not provide a table schema')
        cols = columns + ['_source_file']
        kinds = source.types + ['TEXT']
        count = 0
        with db.engine.begin() as conn:
            conn.exec_driver_sql('PRAGMA query_only=OFF')
            exists = table in inspect(conn).get_table_names()
            if exists and if_exists == 'fail':
                raise CapabilityError('ingest', 'Table already exists: ' + table)
            if exists and if_exists == 'replace':
                conn.execute(text('DROP TABLE ' + q(table)))
                exists = False
            if not exists:
                conn.execute(text('CREATE TABLE ' + q(table) + ' (' + ','.join(q(c)+' '+t for c,t in zip(cols,kinds)) + ')'))
            elif [(c['name'], str(c['type'])) for c in inspect(conn).get_columns(table)] != list(zip(cols,kinds)):
                raise CapabilityError('ingest', 'Append requires identical column names and types')
            statement = text('INSERT INTO '+q(table)+' ('+','.join(q(c) for c in cols)+') VALUES ('+','.join(':p'+str(i) for i in range(len(cols)))+')')
            rows = iter(source.rows)
            while batch := list(islice(rows, 1000)):
                conn.execute(statement, [{**{'p'+str(i):v for i,v in enumerate(row)}, 'p'+str(len(columns)):str(path)} for row in batch])
                count += len(batch)
        if hasattr(db, 'record_table_metadata'):
            db.record_table_metadata(table, {'source': str(path), 'column_mapping': mapping})
        return {'table': table, 'original_table': source.name, 'columns': cols, 'types': kinds,
                'column_mapping': mapping, 'rows_inserted': count, 'source': str(path), 'if_exists': if_exists}
