"""Persist tool-call context without retaining async clients between MCP calls.

Operation IDs are unguessable capabilities within a trusted profile. They are
not an authentication mechanism: enterprise hosts must bind tenant identity.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from datamind.core.context import RequestContext


class OperationStore:
    def __init__(self, settings):
        settings.ensure_dirs()
        self.profile = settings.data.profile
        self.path = settings.data.storage_dir / 'operations.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, revision TEXT, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skills (
                    operation TEXT NOT NULL, name TEXT NOT NULL, reference TEXT NOT NULL,
                    PRIMARY KEY(operation, name));
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def begin(self, *, session_id=None, revision=None, mode='read'):
        if mode not in {'read', 'write'}:
            raise ValueError('mode must be read or write')
        if revision and mode != 'read':
            raise ValueError('Published revisions are immutable; use a separate write operation')
        token = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO operations VALUES (?, ?, ?, ?)',
                       (token, session_id or uuid.uuid4().hex, revision, mode))
        return {'operation_id': token, 'profile': self.profile,
                'revision': revision, 'mode': mode}

    def load(self, token):
        with self.connect() as db:
            row = db.execute('SELECT session, revision, mode FROM operations WHERE id=?', (token,)).fetchone()
            if row is None:
                raise ValueError('Unknown operation in this profile')
            refs = {name: json.loads(ref) for name, ref in db.execute(
                'SELECT name, reference FROM skills WHERE operation=?', (token,))}
        ctx = RequestContext(session_id=row[0], profile=self.profile,
                             extra={'applied_skills': refs, 'workspace_revision': row[1]})
        return ctx, row[1], row[2]

    def save_skills(self, token, context):
        with self.connect() as db:
            for name, ref in context.extra.get('applied_skills', {}).items():
                db.execute('INSERT OR REPLACE INTO skills VALUES (?, ?, ?)',
                           (token, name, json.dumps(ref)))
