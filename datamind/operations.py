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
        self.settings = settings
        self.profile = settings.data.profile
        self.path = settings.data.storage_dir / 'operations.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, revision TEXT, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS request_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS request_events_id ON request_events(request_id);
                CREATE TABLE IF NOT EXISTS skills (
                    operation TEXT NOT NULL, name TEXT NOT NULL, reference TEXT NOT NULL,
                    PRIMARY KEY(operation, name));
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(operations)')}
            for name in ('lease','request_id'):
                if name not in columns:db.execute('ALTER TABLE operations ADD COLUMN '+name+' TEXT')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def begin(self, *, session_id=None, revision=None, mode='read', lease=None, request_id=None):
        if mode not in {'read', 'write'}:
            raise ValueError('mode must be read or write')
        if revision and mode != 'read':
            raise ValueError('Published revisions are immutable; use a separate write operation')
        token = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO operations(id,session,revision,mode,lease,request_id) VALUES (?,?,?,?,?,?)',
                       (token, session_id or uuid.uuid4().hex, revision, mode, lease, request_id))
        return {'operation_id': token, 'profile': self.profile,
                'revision': revision, 'mode': mode}

    def load(self, token, *, renew=True):
        with self.connect() as db:
            row = db.execute('SELECT session, revision, mode, lease FROM operations WHERE id=?', (token,)).fetchone()
            if row is None:
                raise ValueError('Unknown operation in this profile')
            refs = {name: json.loads(ref) for name, ref in db.execute(
                'SELECT name, reference FROM skills WHERE operation=?', (token,))}
        if row[3] and renew:
            from datamind.workspace import Workspace
            Workspace(self.settings).renew_lease(row[3])
        ctx = RequestContext(session_id=row[0], profile=self.profile,
                             extra={'applied_skills': refs, 'workspace_revision': row[1]})
        return ctx, row[1], row[2]

    def close(self, token):
        with self.connect() as db:
            row=db.execute('SELECT lease FROM operations WHERE id=?',(token,)).fetchone()
            db.execute('DELETE FROM skills WHERE operation=?',(token,))
            db.execute('DELETE FROM operations WHERE id=?',(token,))
        if row and row[0]:
            from datamind.workspace import Workspace
            Workspace(self.settings).release_lease(row[0])

    def close_request(self, request_id):
        with self.connect() as db:
            tokens=[r[0] for r in db.execute('SELECT id FROM operations WHERE request_id=?',(request_id,))]
        for token in tokens:self.close(token)

    def save_skills(self, token, context):
        with self.connect() as db:
            for name, ref in context.extra.get('applied_skills', {}).items():
                db.execute('INSERT OR REPLACE INTO skills VALUES (?, ?, ?)',
                           (token, name, json.dumps(ref)))


    def record_event(self, request_id, payload):
        """Optional request-scoped transport trace; the host deletes it after delivery."""
        if request_id is None:
            return
        with self.connect() as db:
            db.execute('INSERT INTO request_events(request_id,payload) VALUES (?,?)',
                       (request_id,json.dumps(payload,ensure_ascii=False,default=str)))

    def take_events(self, request_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT payload FROM request_events WHERE request_id=? ORDER BY seq', (request_id,)).fetchall()
            db.execute('DELETE FROM request_events WHERE request_id=?',(request_id,))
        return [json.loads(row[0]) for row in rows]
