"""Immutable input records for graph assertions that did not come from a file."""
import hashlib
import os
import tempfile
from pathlib import Path


def persist_input(data_dir: Path, content: str, suffix: str) -> tuple[Path, str]:
    return persist_bytes(data_dir, content.encode('utf-8'), suffix)


def persist_bytes(data_dir: Path, raw: bytes, suffix: str) -> tuple[Path, str]:
    digest = hashlib.sha256(raw).hexdigest()
    root = data_dir / 'triplets' / 'evidence'
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest + suffix)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('stored graph evidence failed integrity verification')
        return path, 'sha256:' + digest
    fd, temporary = tempfile.mkstemp(dir=root, prefix='.evidence-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path, 'sha256:' + digest
