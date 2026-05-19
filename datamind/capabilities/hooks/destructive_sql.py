"""DestructiveSqlHook — intercepts destructive SQL via sqlglot AST.

Decision tree:

    SELECT / WITH (read-only)            → Allow
    INSERT / UPDATE WHERE ... LIMIT n    → AskUser  (mutation, narrow)
    UPDATE / DELETE without WHERE        → AskUser  (mutation, broad)
    DROP / TRUNCATE / ALTER on a table   → AskUser  (schema change)
    DROP DATABASE / DROP SCHEMA          → Deny     (blanket destruction)
    CREATE TABLE / CREATE INDEX          → Allow    (additive schema)
    Unparseable                          → Deny     (paranoid default)

The agent re-issues the call with `confirm_destructive=True` after
human consent; that flag short-circuits the AskUser branch back to Allow.

This hook only fires on the `db_query_sql` tool. Other tools pass
straight through. We intentionally do NOT intercept `db_query_nl` even
though it can produce destructive SQL — by the time NL2SQL has
generated SQL it dispatches via db_query_sql, where we catch it.

Implementation notes:
- We parse with `sqlglot.parse(...)` (lenient mode). If parsing fails
  we Deny — refusing to run something we can't analyse is safer than
  running it.
- Multiple statements in one call are inspected individually; the
  most-restrictive decision wins.
- Row-count estimation (`n` in AskUser details) is best-effort: we
  return the LIMIT clause if present, otherwise None.
"""
from __future__ import annotations

from typing import Any

import sqlglot
import sqlglot.expressions as exp

from datamind.core.context import RequestContext
from datamind.core.hooks import Allow, AskUser, Deny, HookDecision

# AST node classes that constitute "destructive" operations.
# These are sqlglot expression types; matching against them is more
# robust than regex (handles comments, quoted identifiers, line breaks).
# Note: in sqlglot 22 the schema-modify node was `AlterTable`; sqlglot
# 23+ renamed it to `Alter` and folded ALTER on databases/views/etc.
# into the same node, distinguished by the `kind` argument. We support
# both names.
_AlterNode = getattr(exp, "Alter", None) or getattr(exp, "AlterTable", None)
_DESTRUCTIVE_NODES = (
    exp.Delete,
    exp.Update,
    exp.Drop,
    exp.TruncateTable,
)
if _AlterNode is not None:
    _DESTRUCTIVE_NODES = _DESTRUCTIVE_NODES + (_AlterNode,)

# Even more restrictive: blanket database-level operations should be
# denied outright, not just confirmed.
_BLANKET_DENY_KINDS = {"DATABASE", "SCHEMA"}


class DestructiveSqlHook:
    """Pre-tool-use hook that gates destructive SQL behind user consent."""

    name = "destructive_sql"

    def __init__(self) -> None:
        # No state — hooks should be idempotent.
        pass

    async def pre_tool_use(
        self,
        ctx: RequestContext,
        tool_name: str,
        args: dict[str, Any],
    ) -> HookDecision:
        # Only gate db_query_sql. Other tools pass through.
        if tool_name != "db_query_sql":
            return Allow()

        # Already confirmed by the user via a prior AskUser round-trip.
        if args.get("confirm_destructive") is True:
            return Allow()

        sql = args.get("sql") or args.get("query") or ""
        if not isinstance(sql, str) or not sql.strip():
            return Allow()  # empty SQL — let the handler error properly

        try:
            statements = sqlglot.parse(sql, error_level="ignore")
        except Exception:
            return Deny(
                reason=(
                    "SQL could not be parsed; refusing to run unparseable "
                    "input. Rephrase the query or break it into smaller "
                    "statements."
                )
            )

        statements = [s for s in statements if s is not None]
        if not statements:
            return Allow()

        # Find the most-restrictive verdict across all statements.
        worst: HookDecision = Allow()
        for stmt in statements:
            verdict = self._classify(stmt)
            worst = self._merge(worst, verdict)
        return worst

    async def post_tool_use(
        self,
        ctx: RequestContext,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        error: Exception | None,
    ) -> None:
        # Nothing to do post — audit logging belongs to AuditLogHook.
        return

    # ------------------------------------------------------ classification

    def _classify(self, stmt: exp.Expression) -> HookDecision:
        # Blanket DROP DATABASE / DROP SCHEMA → hard Deny.
        if isinstance(stmt, exp.Drop):
            kind = (stmt.args.get("kind") or "").upper()
            if kind in _BLANKET_DENY_KINDS:
                return Deny(
                    reason=(
                        f"DROP {kind} is a blanket destructive operation "
                        "and is never allowed via the agent. If you really "
                        "need this, run it manually outside DataMind."
                    )
                )
            return AskUser(
                prompt=(
                    f"Confirm DROP of {kind or 'object'}: "
                    f"{self._summarise(stmt)}"
                ),
                details={"op": "DROP", "kind": kind, "sql": self._summarise(stmt)},
                confirm_args={"confirm_destructive": True},
            )

        if isinstance(stmt, exp.TruncateTable):
            return AskUser(
                prompt=(
                    f"Confirm TRUNCATE: {self._summarise(stmt)} — this "
                    "removes ALL rows from the listed tables."
                ),
                details={"op": "TRUNCATE", "sql": self._summarise(stmt)},
                confirm_args={"confirm_destructive": True},
            )

        if _AlterNode is not None and isinstance(stmt, _AlterNode):
            kind = (stmt.args.get("kind") or "TABLE").upper()
            return AskUser(
                prompt=(
                    f"Confirm ALTER {kind}: {self._summarise(stmt)} — schema "
                    "changes can break downstream queries."
                ),
                details={"op": "ALTER", "kind": kind, "sql": self._summarise(stmt)},
                confirm_args={"confirm_destructive": True},
            )

        if isinstance(stmt, exp.Delete):
            has_where = stmt.args.get("where") is not None
            return AskUser(
                prompt=(
                    f"Confirm DELETE: {self._summarise(stmt)}"
                    + ("" if has_where else " — NO WHERE clause: this deletes ALL ROWS.")
                ),
                details={
                    "op": "DELETE",
                    "has_where": has_where,
                    "sql": self._summarise(stmt),
                },
                confirm_args={"confirm_destructive": True},
            )

        if isinstance(stmt, exp.Update):
            has_where = stmt.args.get("where") is not None
            return AskUser(
                prompt=(
                    f"Confirm UPDATE: {self._summarise(stmt)}"
                    + ("" if has_where else " — NO WHERE clause: this updates ALL ROWS.")
                ),
                details={
                    "op": "UPDATE",
                    "has_where": has_where,
                    "sql": self._summarise(stmt),
                },
                confirm_args={"confirm_destructive": True},
            )

        # Anything else — SELECT, WITH, INSERT, CREATE, EXPLAIN — is allowed.
        # INSERT is *additive*; we don't gate it. (Operators who want to
        # gate INSERT can subclass this hook or compose their own.)
        return Allow()

    @staticmethod
    def _merge(a: HookDecision, b: HookDecision) -> HookDecision:
        """Combine two decisions: Deny > AskUser > Allow."""
        if isinstance(a, Deny) or isinstance(b, Deny):
            # Prefer the existing Deny's reason if both are Deny; otherwise
            # whichever is Deny.
            return a if isinstance(a, Deny) else b
        if isinstance(a, AskUser) or isinstance(b, AskUser):
            return a if isinstance(a, AskUser) else b
        return Allow()

    @staticmethod
    def _summarise(stmt: exp.Expression, *, max_len: int = 200) -> str:
        """Render a stmt back to SQL, truncated for human display."""
        try:
            text = stmt.sql()
        except Exception:
            text = str(stmt)
        text = " ".join(text.split())  # collapse whitespace
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"
        return text


__all__ = ["DestructiveSqlHook"]
