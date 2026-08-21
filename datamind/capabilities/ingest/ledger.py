"""Idempotent receipt ledger for StoreAgent tool calls."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from datamind.core.contracts import (
    DataSurface,
    IngestReceipt,
    SourceRef,
    SurfaceWriteResult,
)
from datamind.core.tools import ToolRegistry, ToolSpec


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _directory_source(path: Path, *, tool_name: str) -> SourceRef:
    digest = hashlib.sha256()
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(child.relative_to(path)).encode("utf-8"))
        with child.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    hexdigest = digest.hexdigest()
    return SourceRef(
        source_id=f"directory:{hexdigest[:24]}",
        kind="file",
        uri=str(path),
        checksum=f"sha256:{hexdigest}",
        metadata={"tool": tool_name},
    )


def _source_from_call(spec: ToolSpec, args: dict[str, Any]) -> SourceRef:
    tool_name = spec.name
    path_value = args.get("path")
    if isinstance(path_value, str) and path_value:
        path = Path(path_value).expanduser().resolve(strict=False)
        if path.is_file():
            return SourceRef.from_file(path, tool=tool_name)
        if path.is_dir():
            return _directory_source(path, tool_name=tool_name)
        digest = _hash_text(str(path))
        return SourceRef(
            source_id=f"path:{digest[:24]}",
            kind="file",
            uri=str(path),
            metadata={"tool": tool_name, "unresolved": True},
        )

    source_path = spec.metadata.get("source_path")
    if isinstance(source_path, str):
        path = Path(source_path).expanduser().resolve(strict=False)
        if path.is_dir():
            return _directory_source(path, tool_name=tool_name)

    if tool_name == "kb_add_text":
        return SourceRef.from_text(str(args.get("text", "")), kind="text", tool=tool_name)
    if tool_name.startswith("graph_"):
        payload = str(args.get("text") or _canonical(args.get("triples", [])))
        return SourceRef.from_text(payload, kind="triples", tool=tool_name)
    if tool_name.startswith("skill_"):
        return SourceRef.from_text(
            str(args.get("body", "")),
            kind="skill",
            tool=tool_name,
            name=args.get("name"),
        )
    if tool_name.startswith("memory_"):
        payload = str(args.get("content") or args.get("item_id") or "")
        return SourceRef.from_text(payload, kind="memory", tool=tool_name)

    payload = _canonical(args)
    digest = _hash_text(payload)
    return SourceRef(
        source_id=f"records:{digest[:24]}",
        kind="records",
        checksum=f"sha256:{digest}",
        metadata={"tool": tool_name},
    )


def _items_written(result: Any) -> int:
    if not isinstance(result, dict):
        return 1 if result is not None else 0
    for key in (
        "chunks_added",
        "rows_inserted",
        "triples_added",
        "upserted",
        "total_embedded",
        "files_processed",
    ):
        value = result.get(key)
        if isinstance(value, int):
            return value
    return 1 if result else 0


class IngestLedger:
    """Append-only receipt log plus a compact successful-call index."""

    def __init__(self, *, storage_dir: Path, profile: str) -> None:
        self._storage_dir = Path(storage_dir)
        self._profile = profile
        self._state_path = self._storage_dir / "ingest_state.json"
        self._receipts_path = self._storage_dir / "ingest_receipts.jsonl"
        self._lock = asyncio.Lock()
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.is_file():
            return {"revision": 0, "successful": {}}
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"revision": 0, "successful": {}}
        state.setdefault("revision", 0)
        state.setdefault("successful", {})
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        temp = self._state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self._state_path)

    def _append(self, receipt: IngestReceipt) -> None:
        with self._receipts_path.open("a", encoding="utf-8") as fh:
            fh.write(receipt.model_dump_json() + "\n")

    @property
    def revision(self) -> int:
        return int(self._load_state()["revision"])

    async def execute(
        self,
        *,
        spec: ToolSpec,
        args: dict[str, Any],
        invoke: Callable[..., Awaitable[Any]],
    ) -> dict[str, Any]:
        """Invoke one write once and always return a structured receipt."""
        source = _source_from_call(spec, args)
        fingerprint = _hash_text(
            _canonical(
                {
                    "tool": spec.name,
                    "args": args,
                    "source_checksum": source.checksum,
                }
            )
        )
        surface = spec.surface
        if surface is None:
            raise ValueError(f"Store tool '{spec.name}' does not declare a surface")

        async with self._lock:
            state = self._load_state()
            previous = state["successful"].get(fingerprint)
            if previous:
                receipt = IngestReceipt(
                    profile=self._profile,
                    revision=int(state["revision"]),
                    source=source,
                    results=[
                        SurfaceWriteResult(
                            surface=surface,
                            operation=spec.name,
                            status="unchanged",
                            items_written=0,
                            details={"duplicate_of": previous},
                        )
                    ],
                )
                self._append(receipt)
                return receipt.model_dump(mode="json")

            try:
                result = await invoke(**args)
            except Exception as exc:  # receipt is retained even when a write fails
                receipt = IngestReceipt(
                    profile=self._profile,
                    revision=int(state["revision"]),
                    source=source,
                    results=[
                        SurfaceWriteResult(
                            surface=surface,
                            operation=spec.name,
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    ],
                )
                self._append(receipt)
                return receipt.model_dump(mode="json")

            state["revision"] = int(state["revision"]) + 1
            receipt = IngestReceipt(
                profile=self._profile,
                revision=int(state["revision"]),
                source=source,
                results=[
                    SurfaceWriteResult(
                        surface=surface,
                        operation=spec.name,
                        items_written=_items_written(result),
                        details=result if isinstance(result, dict) else {"result": result},
                    )
                ],
            )
            state["successful"][fingerprint] = receipt.receipt_id
            self._save_state(state)
            self._append(receipt)
            return receipt.model_dump(mode="json")


def with_receipts(registry: ToolRegistry, ledger: IngestLedger) -> ToolRegistry:
    """Wrap every StoreAgent tool so its result is an IngestReceipt."""
    wrapped = ToolRegistry()
    for original in (registry.get(name) for name in registry.names()):
        async def _handler(
            _spec: ToolSpec = original,
            **kwargs: Any,
        ) -> dict[str, Any]:
            # Apply handler defaults before fingerprinting so omitted defaults
            # and explicitly supplied defaults remain the same idempotent call.
            try:
                bound = inspect.signature(_spec.handler).bind_partial(**kwargs)
                bound.apply_defaults()
                normalized = dict(bound.arguments)
            except (TypeError, ValueError):
                normalized = dict(kwargs)
            return await ledger.execute(spec=_spec, args=normalized, invoke=_spec.handler)

        wrapped.add(
            ToolSpec(
                name=original.name,
                description=original.description,
                input_schema=original.input_schema,
                handler=_handler,
                metadata={**original.metadata, "receipt": True},
            )
        )
    return wrapped


__all__ = ["IngestLedger", "with_receipts"]
