"""Reference adapters for DataMind's stable source ports."""
from .document import (
    DocumentHit,
    DocumentRecord,
    InMemoryDocumentSource,
)
from .sqlite import SQLiteReadSource, SQLiteTable

__all__ = [
    "DocumentHit",
    "DocumentRecord",
    "InMemoryDocumentSource",
    "SQLiteReadSource",
    "SQLiteTable",
]
