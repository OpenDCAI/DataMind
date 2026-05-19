"""Built-in hook implementations for v0.3 Phase 8.

Three production hooks ship in this package:

    DestructiveSqlHook     — parse SQL on db_query_sql, AskUser for
                             DROP/DELETE/UPDATE/TRUNCATE/ALTER, Deny
                             for blanket DROP DATABASE etc.
    PathAllowlistHook      — refuse paths outside the profile data dir
                             on kb_add_file / kb_add_path / db_import_csv
    AuditLogHook           — append every (pre-decision, post-result)
                             pair to storage/<profile>/audit.jsonl, with
                             a Merkle-style hash chain so tampering is
                             detectable

Each hook is independent; HookChain composes them in registration order.
The order in `default_hooks()` reflects the recommended deployment:
PathAllowlistHook first (cheapest), DestructiveSqlHook next (parses SQL),
AuditLogHook last (its post-stage runs regardless of pre decisions, so
denied / asks-user calls still get logged).
"""
from __future__ import annotations

from datamind.core.hooks import HookChain

from .audit import AuditLogHook
from .destructive_sql import DestructiveSqlHook
from .path_allowlist import PathAllowlistHook


def default_hooks(
    *,
    audit_path,
    path_roots,
) -> HookChain:
    """Build the recommended HookChain for production deployments.

    Order: cheapest → most expensive. AuditLogHook is last so its
    post-hook captures every prior decision (Allow/Deny/AskUser).
    """
    return HookChain([
        PathAllowlistHook(roots=path_roots),
        DestructiveSqlHook(),
        AuditLogHook(audit_path=audit_path),
    ])


__all__ = [
    "AuditLogHook",
    "DestructiveSqlHook",
    "PathAllowlistHook",
    "default_hooks",
]
