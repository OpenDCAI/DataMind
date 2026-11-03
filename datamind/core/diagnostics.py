"""Request-scoped diagnostics. Never retain API credentials or image payloads."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os

from filelock import FileLock

_settings = ContextVar('diagnostic_settings', default=None)


@contextmanager
def diagnostic_scope(settings):
    token = _settings.set(settings)
    try:
        yield
    finally:
        _settings.reset(token)


def redact(value, settings=None):
    secrets = set()
    def collect(item):
        if hasattr(item, 'get_secret_value'):
            secrets.add(item.get_secret_value())
        elif hasattr(item, 'model_dump'):
            collect(item.model_dump(mode='python'))
        elif isinstance(item, dict):
            for child in item.values(): collect(child)
        elif isinstance(item, (list, tuple)):
            for child in item: collect(child)
    collect(settings or _settings.get())
    for key, secret in os.environ.items():
        if key.endswith(('_KEY', '_TOKEN', '_SECRET')):
            secrets.add(secret)
    text = json.dumps(value, ensure_ascii=False, default=str)
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
    return json.loads(text)


def exception_detail(exc, settings=None):
    seen = set()
    def walk(error):
        if id(error) in seen:
            return {'type': type(error).__name__, 'message': '(already shown)'}
        seen.add(id(error))
        result = {'type': type(error).__name__, 'message': str(error)}
        if isinstance(getattr(error, 'details', None), dict):
            result['details'] = error.details
        if isinstance(error, BaseExceptionGroup):
            result['exceptions'] = [walk(child) for child in error.exceptions]
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        if cause is not None: result['cause'] = walk(cause)
        return result
    return redact(walk(exc), settings)


def record_diagnostic(kind, payload, settings=None):
    settings = settings or _settings.get()
    if settings is None or not settings.logging.event_trace:
        return
    record = redact({'time': datetime.now(timezone.utc).isoformat(),
                     'request_id': os.environ.get('DATAMIND_REQUEST_ID'),
                     'kind': kind, **payload}, settings)
    try:
        directory = settings.log_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'diagnostics.jsonl'
        with FileLock(str(path) + '.lock'), path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass  # A diagnostic write must not change tool execution.
