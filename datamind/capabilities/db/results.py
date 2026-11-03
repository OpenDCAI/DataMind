"""Stream complete query results to managed CSV artifacts, with bounded previews."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

from sqlalchemy import text
from datamind.core.errors import CapabilityError
from datamind.core.logging import current_context
from .query_contract import validate_columns, validate_final_rows, export_contract


async def export_csv(dialect, engine, cfg, root, sql):
    if root is None:
        raise CapabilityError('db', 'Managed query result directory is not configured')
    dialect._validate_readonly_sql(sql, cfg.row_limit)
    bounded = sql if cfg.row_limit is None else dialect._bound_query(sql, cfg.row_limit + 1)
    ctx = current_context()
    revision = ctx.extra.get('workspace_revision') if ctx else None
    result_id = uuid.uuid4().hex
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / ('.' + result_id + '.csv')
    target = root / (result_id + '.csv')
    stop = threading.Event()

    def run():
        count, preview, truncated = 0, [], False
        try:
            with engine.connect() as conn, temporary.open('w', encoding='utf-8', newline='') as handle:
                dialect._before_query(conn)
                result = conn.execution_options(stream_results=True).execute(text(bounded))
                columns = list(result.keys())
                validate_columns(columns)
                writer = csv.writer(handle)
                writer.writerow(columns)
                while rows := result.fetchmany(1000):
                    if stop.is_set():
                        raise TimeoutError('Query export cancelled')
                    for row in rows:
                        if cfg.row_limit is not None and count >= cfg.row_limit:
                            truncated = True
                            break
                        writer.writerow(row)
                        if len(preview) < 20:
                            preview.append(list(row))
                        count += 1
                    if truncated:
                        break
                validate_final_rows(count, truncated)
                handle.flush()
                os.fsync(handle.fileno())
            if stop.is_set():
                raise TimeoutError('Query export cancelled')
            sha = hashlib.sha256()
            with temporary.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    sha.update(block)
            contract = export_contract()
            os.replace(temporary, target)
            value = {'result_id': result_id, 'path': str(target.resolve()), 'columns': columns,
                     'row_count': count, 'truncated': truncated, 'complete': not truncated,
                     'sha256': sha.hexdigest(), 'workspace_revision': revision,
                     'profile': ctx.profile if ctx else None,
                     'request_id': ctx.extra.get('host_request_id') if ctx else None,
                     'request_scope': (ctx.extra.get('host_request_id') or ctx.extra.get('operation_id') or ctx.trace_id) if ctx else None,
                     **contract,
                     'preview': preview, 'preview_only': count > len(preview)}
            from datamind.workspace import _atomic_json
            _atomic_json(root / (result_id + '.json'), value)
            return value
        finally:
            temporary.unlink(missing_ok=True)
    task = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.wait_for(asyncio.shield(task), cfg.query_timeout_s)
    except BaseException:
        stop.set()
        # Consume worker errors after cancellation; incomplete artifacts are never published.
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        raise
