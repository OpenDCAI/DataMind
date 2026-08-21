"""Ingest capability — agent-driven additions to KB / DB / Graph."""
from .service import IngestService, build_ingest_service
from .tools import build_ingest_tools
from .ledger import IngestLedger, with_receipts

__all__ = [
    "IngestService",
    "build_ingest_service",
    "build_ingest_tools",
    "IngestLedger",
    "with_receipts",
]
