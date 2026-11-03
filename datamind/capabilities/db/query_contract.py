"""Request-scoped query intent and answer-column contracts, shared by all DB tools.

The model interprets the user's request. This module checks the declared shape;
it never infers desired columns from SQL, reference answers, or task identifiers.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import csv
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from filelock import FileLock
from jsonschema import Draft202012Validator
from datamind.core.errors import CapabilityError
from datamind.core.logging import current_context

INTENT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'purpose': {'type': 'string', 'enum': ['explore', 'verify', 'answer']},
        'question': {'type': 'string', 'minLength': 1, 'description': 'The user requirement or intermediate question this query answers; not a copy of the SQL.'},
        'return_columns': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                           'items': {'type': 'string', 'minLength': 1},
                           'description': 'Exact ordered SQL output labels, including aliases for computed expressions. Exclude fields used only in filters/joins/order.'},
        'schema_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$',
                      'description': 'Required for purpose=answer: active ID returned by db_answer_schema.'},
    }, 'required': ['purpose', 'question', 'return_columns'],
}
ANSWER_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'question': {'type': 'string', 'minLength': 1},
        'columns': {'type': 'array', 'minItems': 1, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'name': {'type': 'string', 'minLength': 1},
                           'requirement': {'type': 'string', 'minLength': 1,
                                           'description': 'Why the user requests this output, as distinct from a filter or evidence field.'}},
            'required': ['name', 'requirement']}},
        'row_shape': {'type': 'string', 'enum': ['scalar', 'records'],
                      'description': 'scalar requires exactly one final data row; records retains all rows and duplicates.'},
        'revision_reason': {'type': 'string', 'minLength': 1,
                            'description': 'Required when changing an existing answer definition. Revisions invalidate previous final exports.'},
    }, 'required': ['question', 'columns', 'row_shape'],
}
_active: ContextVar[dict | None] = ContextVar('datamind_query_contract', default=None)
MANAGED_ANSWER_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'result_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$',
        'description': 'Exact result_id from a successful purpose=answer db_export_sql call in this request; never a failure message or invented ID.'}},
    'required': ['result_id'],
}


def _error(message):
    raise CapabilityError('db', 'Query contract: ' + message)


def identity():
    ctx = current_context()
    if ctx is None:
        _error('a RequestContext is required for an answer schema')
    scope = ctx.extra.get('host_request_id') or ctx.extra.get('operation_id') or ctx.trace_id
    return ctx.profile, scope


class AnswerSchemas:
    def __init__(self, result_dir, *, read_only=False):
        if result_dir is None:
            _error('managed result storage is not configured')
        self.root = Path(result_dir)
        self.path = self.root / 'answer_schemas.sqlite3'
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                _error('answer schema storage is missing; declare the answer and create a fresh export')
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + '.schema.lock', timeout=30), self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS answer_schemas ('
                       'id TEXT PRIMARY KEY, profile TEXT NOT NULL, scope TEXT NOT NULL, '
                       'revision INTEGER NOT NULL, payload TEXT NOT NULL, UNIQUE(profile,scope,revision))')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30) if self.read_only else sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def define(self, question, columns, row_shape, revision_reason=None):
        values = {'question': question, 'columns': columns, 'row_shape': row_shape}
        Draft202012Validator(ANSWER_SCHEMA).validate(values | ({'revision_reason': revision_reason} if revision_reason else {}))
        labels = [x['name'] for x in columns]
        if len(set(labels)) != len(labels) or any(not x.strip() for x in labels):
            _error('answer column labels must be nonempty and unique')
        profile, scope = identity()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT payload FROM answer_schemas WHERE profile=? AND scope=? ORDER BY revision DESC LIMIT 1', (profile, scope)).fetchone()
            old = json.loads(previous[0]) if previous else None
            if old and all(old[k] == v for k, v in values.items()):
                return old
            if old and not (revision_reason or '').strip():
                _error('changing answer columns requires revision_reason tied to the user request; do not widen a schema just to match SQL')
            result = {**values, 'schema_id': uuid.uuid4().hex, 'profile': profile, 'scope': scope,
                      'revision': old['revision'] + 1 if old else 1,
                      'revision_reason': revision_reason, 'previous_schema_id': old['schema_id'] if old else None}
            db.execute('INSERT INTO answer_schemas VALUES (?,?,?,?,?)',
                       (result['schema_id'], profile, scope, result['revision'], json.dumps(result, ensure_ascii=False)))
        return result

    def load(self, schema_id, *, profile=None, scope=None):
        if profile is None or scope is None:
            profile, scope = identity()
        with self.connect() as db:
            row = db.execute('SELECT payload FROM answer_schemas WHERE id=? AND profile=? AND scope=?', (schema_id, profile, scope)).fetchone()
            latest = db.execute('SELECT id FROM answer_schemas WHERE profile=? AND scope=? ORDER BY revision DESC LIMIT 1', (profile, scope)).fetchone()
        if row is None:
            _error('unknown answer schema in this profile/request')
        if latest[0] != schema_id:
            _error('answer schema was revised; use its current version and export again')
        return json.loads(row[0])


def _check_sql_projection(sql, dialect, expected, purpose):
    from sqlglot import parse, exp
    try:
        parsed = parse(sql, read='mysql' if dialect == 'dolt' else dialect)
        if len(parsed) != 1 or parsed[0] is None or not isinstance(parsed[0], exp.Query):
            _error('intent applies to a single read-only SELECT')
        outputs = parsed[0].selects
    except CapabilityError:
        raise
    except Exception as exc:
        _error('cannot validate SQL projection: ' + str(exc))
    wildcard = any(isinstance(e, exp.Star) or isinstance(e, exp.Column) and e.is_star for e in outputs)
    if wildcard:
        if purpose == 'answer':
            _error('final answers must explicitly select the requested columns; SELECT * is not accepted')
        return  # Cursor metadata expands exploration stars before any rows are fetched.
    actual = [e.alias_or_name for e in outputs]
    if any(not label for label in actual):
        _error('give computed expressions explicit AS aliases matching return_columns')
    if actual != expected:
        _error(f'SQL projects {actual!r}, but intent declares {expected!r}; correct the SQL or the intermediate intent, not the answer requirement')


@contextmanager
def query_guard(db, intent, *, sql=None, dialect_name=None):
    ctx = current_context()
    if intent is None:
        if ctx is not None and ctx.extra.get('query_contract_required'):
            _error('declare intent with purpose, question and return_columns before executing a query')
        # Low-level Python callers retain compatibility; agent transports require intent.
        token = _active.set(None)
        try:
            yield None
        finally:
            _active.reset(token)
        return
    Draft202012Validator(INTENT_SCHEMA).validate(intent)
    expected = list(intent['return_columns'])
    if any(not x.strip() for x in expected):
        _error('return_columns must not contain empty labels')
    state = {'intent': dict(intent), 'answer_schema': None, 'columns_checked': False}
    if intent['purpose'] == 'answer':
        if not intent.get('schema_id'):
            _error('declare the answer with db_answer_schema before an answer query/export')
        schema = AnswerSchemas(db.result_dir).load(intent['schema_id'])
        if expected != [x['name'] for x in schema['columns']]:
            _error('answer return_columns must exactly match the declared answer schema')
        state['answer_schema'] = schema
    if sql is not None:
        _check_sql_projection(sql, dialect_name or db.dialect.name, expected, intent['purpose'])
    token = _active.set(state)
    try:
        yield state
    finally:
        _active.reset(token)


def validate_columns(columns):
    state = _active.get()
    if state is not None:
        expected = state['intent']['return_columns']
        if list(columns) != expected:
            _error(f'actual database columns {list(columns)!r} do not match {expected!r}; no result is accepted')
        state['columns_checked'] = True


def validate_final_rows(count, truncated):
    state = _active.get()
    schema = state['answer_schema'] if state else None
    if schema:
        if truncated:
            _error('a final answer cannot be a truncated result')
        if schema['row_shape'] == 'scalar' and count != 1:
            _error(f'scalar answer requires one data row, got {count}')


def export_contract():
    state = _active.get()
    if state is None:
        return {'answer_validated': False}
    if not state['columns_checked']:
        _error('export did not validate actual database columns')
    schema = state['answer_schema']
    return {'query_intent': state['intent'], 'answer_validated': schema is not None,
            'answer_schema_id': schema['schema_id'] if schema else None,
            'answer_schema_revision': schema['revision'] if schema else None}


def validate_answer_export(result_dir, result_id, *, profile, scope):
    """Generic final-file gate: schema, scope, bytes, exact header and row count."""
    if not isinstance(result_id, str) or not re.fullmatch(r'[a-f0-9]{32}', result_id):
        _error('invalid managed export ID')
    try:
        return _validate_answer_export(result_dir, result_id, profile=profile, scope=scope)
    except FileNotFoundError:
        _error('export file or metadata is missing; use the exact result_id from a successful db_export_sql in this request, or export again')
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeError, csv.Error, sqlite3.DatabaseError):
        _error('export metadata or CSV is malformed; create a fresh validated export')


def _validate_answer_export(result_dir, result_id, *, profile, scope):
    root = Path(result_dir)
    meta = json.loads((root / (result_id + '.json')).read_text())
    if not isinstance(meta, dict):
        _error('export metadata is malformed; create a fresh validated export')
    if not meta.get('answer_validated') or not meta.get('complete') or meta.get('truncated'):
        _error('final submission requires a complete purpose=answer export')
    if meta.get('profile') != profile or meta.get('request_scope') != scope:
        _error('export belongs to another profile/request')
    schema = AnswerSchemas(root, read_only=True).load(meta.get('answer_schema_id'), profile=profile, scope=scope)
    if meta.get('answer_schema_revision') != schema['revision']:
        _error('export schema version mismatch')
    source = root / (result_id + '.csv')
    sha = hashlib.sha256()
    with source.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            sha.update(block)
    if sha.hexdigest() != meta['sha256']:
        _error('export hash mismatch')
    expected = [x['name'] for x in schema['columns']]
    with source.open(encoding='utf-8', newline='') as f:
        rows = csv.reader(f)
        if next(rows, None) != expected or meta['columns'] != expected:
            _error('final CSV header does not match the answer schema')
        count = 0
        for row in rows:
            if len(row) != len(expected):
                _error('inconsistent final CSV row width')
            count += 1
    if count != meta['row_count'] or schema['row_shape'] == 'scalar' and count != 1:
        _error('final CSV row count mismatch')
    return meta


class ManagedAnswerExportValidator:
    """Reusable CodexHost final_validator for complete request-bound SQL CSVs."""
    def __init__(self, result_dir, profile):
        self.result_dir, self.profile = Path(result_dir), profile

    def __call__(self, value, *, request_id, workspace_revision):
        Draft202012Validator(MANAGED_ANSWER_SCHEMA).validate(value)
        meta = validate_answer_export(self.result_dir, value['result_id'], profile=self.profile, scope=request_id)
        if meta.get('workspace_revision') != workspace_revision:
            _error('export belongs to a different workspace revision; query and export in the current request')
