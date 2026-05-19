"""PathAllowlistHook — refuse filesystem paths outside allow-listed roots.

Catches the canonical "agent escapes the sandbox via a crafted path"
class of attacks: `../../../etc/passwd`, symlinks pointing out of the
profile dir, raw absolute paths to a customer's home.

Tools intercepted:
    kb_add_file       — `path` argument
    kb_add_path       — `path` argument
    db_import_csv     — `path` argument

Other tools are not gated by this hook (their args are not paths).

The check uses `Path.resolve(strict=False)` so symlinks are followed
before the prefix comparison. If the resolved path is not relative to
ANY of the allowed roots → Deny with a helpful message.

This hook duplicates the legacy `_resolve_safe_path` check inside
`IngestService` on purpose: the inline check stays as defense-in-depth,
while this hook is the *advertised* policy boundary that audit logs and
operator dashboards reference. Removing the inline check would lose
that defense in single-process deployments where someone constructs
IngestService directly without an attached HookChain.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from datamind.core.context import RequestContext
from datamind.core.hooks import Allow, Deny, HookDecision

# Tools whose `path` argument we gate. Add-only — accidentally gating a
# tool that doesn't carry a path is a bug, not a security issue (the
# arg lookup just returns None and we Allow).
_PATH_TOOLS = {
    "kb_add_file": "path",
    "kb_add_path": "path",
    "db_import_csv": "path",
}


class PathAllowlistHook:
    """Refuse paths outside `roots`. Roots are resolved at construction."""

    name = "path_allowlist"

    def __init__(self, roots: Iterable[Path | str]) -> None:
        self._roots: list[Path] = []
        for r in roots:
            p = Path(r).expanduser().resolve(strict=False)
            self._roots.append(p)
        if not self._roots:
            raise ValueError(
                "PathAllowlistHook needs at least one root; an empty "
                "allow-list would deny every path-bearing tool call."
            )

    @property
    def roots(self) -> list[Path]:
        # Returned for diagnostics / audit. Caller should not mutate.
        return list(self._roots)

    async def pre_tool_use(
        self,
        ctx: RequestContext,
        tool_name: str,
        args: dict[str, Any],
    ) -> HookDecision:
        path_arg = _PATH_TOOLS.get(tool_name)
        if path_arg is None:
            return Allow()  # not a path-bearing tool

        raw = args.get(path_arg)
        if not raw:
            # Empty / missing path — let the handler raise its own error.
            return Allow()

        try:
            resolved = Path(raw).expanduser().resolve(strict=False)
        except Exception as exc:  # noqa: BLE001 — pathological input
            return Deny(reason=f"Path could not be resolved: {exc!r}")

        for root in self._roots:
            try:
                resolved.relative_to(root)
                return Allow()  # under at least one allowed root
            except ValueError:
                continue

        pretty = ", ".join(str(r) for r in self._roots)
        return Deny(
            reason=(
                f"Path '{resolved}' is outside the allowed roots ({pretty}). "
                "Move the file under one of these directories or have an "
                "operator extend the allow-list."
            )
        )

    async def post_tool_use(
        self,
        ctx: RequestContext,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        error: Exception | None,
    ) -> None:
        return  # path allowlist is a pre-check only


__all__ = ["PathAllowlistHook"]
