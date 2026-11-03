"""Bounded, observable read recovery. State is task/request scoped, never global."""
from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from .errors import CapabilityError, ConfigError, ExternalServiceError
from .logging import current_context, get_logger

T = TypeVar("T")
_log = get_logger("read_recovery")


def is_transient(exc: Exception) -> bool:
    """Do not mask auth, invalid responses, bad filters, or local path errors."""
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (ConfigError, ValueError, PermissionError, FileNotFoundError)):
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {408, 429} or exc.response.status_code >= 500
        if isinstance(exc, ExternalServiceError) and exc.status_code is not None:
            return exc.status_code in {408, 429} or exc.status_code >= 500
        if isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)):
            return True
        if isinstance(exc, OSError) and not isinstance(exc, (CapabilityError, ExternalServiceError)):
            return True
        cause = exc.__cause__
        if not isinstance(cause, Exception):
            return False
        exc = cause
    return False


@dataclass
class _Budget:
    limit: int
    used: int = 0
    managed: bool = True


_BUDGET: contextvars.ContextVar[_Budget | None] = contextvars.ContextVar("read_retry_budget", default=None)


def read_retry_budget_active() -> bool:
    budget = _BUDGET.get()
    return budget is not None and budget.managed


def recovery_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "degraded": any(e.get("degraded", False) for e in events),
        "retries": sum(e.get("retries", 0) for e in events),
        "fallbacks": sum(e.get("degraded", False) for e in events),
        "events": list(events),
    }


async def recover_read(
    operation: Callable[[], Awaitable[T]],
    *,
    component: str | Callable[[], str],
    retries: int = 0,
    attempt_timeout_s: float | None = None,
    fallback: Callable[[], Awaitable[T]] | None = None,
    fallback_method: str = "fallback",
    manage_provider_retries: bool = False,
) -> T:
    """Nested readers share a retry budget; the outer query supplies a deadline.

    A vector attempt timeout may trigger lexical fallback. Cancellation and the
    overall query deadline never trigger extra work after the budget has expired.
    Events contain types/status only, not exception strings or user data.
    """
    budget = _BUDGET.get()
    token = None
    if budget is None:
        budget = _Budget(retries, managed=manage_provider_retries or retries > 0 or fallback is not None)
        token = _BUDGET.set(budget)
    ctx = current_context()
    deadline = ctx.extra.get("query_deadline") if ctx else None
    started = time.monotonic()
    event: dict[str, Any] = {"attempts": 0, "retries": 0, "degraded": False,
                             "method": "primary", "outcome": "error", "retry_wait_ms": 0.0}
    failure = None

    def remaining() -> float | None:
        if deadline is None:
            return None
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("query deadline exhausted")
        return left

    try:
        while True:
            left = remaining()
            timeout = attempt_timeout_s
            if left is not None:
                timeout = left if timeout is None else min(timeout, left)
            event["attempts"] += 1
            try:
                async with asyncio.timeout(timeout):
                    result = await operation()
                remaining()
                event["outcome"] = "ok"
                return result
            except Exception as exc:
                event["error_type"] = type(exc).__name__
                event["status_code"] = getattr(exc, "status_code", None)
                if not is_transient(exc):
                    raise
                remaining()
                if budget.used < budget.limit:
                    delay = 0.05 * 2 ** budget.used
                    left = remaining()
                    if left is not None and delay >= left:
                        raise TimeoutError("query deadline exhausted") from exc
                    budget.used += 1
                    event["retries"] += 1
                    await asyncio.sleep(delay)
                    event["retry_wait_ms"] += delay * 1000
                    continue
                if fallback is None:
                    raise
                fallback_started = time.monotonic()
                event["method"] = fallback_method
                try:
                    async with asyncio.timeout(remaining()):
                        result = await fallback()
                    remaining()
                except BaseException:
                    # The fallback has failed too; don't retry the whole nested
                    # operation and don't report this as successful degradation.
                    budget.used = budget.limit
                    raise
                finally:
                    event["fallback_ms"] = round((time.monotonic() - fallback_started) * 1000, 3)
                event.update(degraded=True, outcome="ok")
                return result
    except asyncio.CancelledError:
        event["outcome"] = "cancelled"
        raise
    except Exception as exc:
        failure = exc
        raise
    finally:
        event["component"] = component() if callable(component) else component
        event["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        if ctx is not None:
            event["workspace_revision"] = ctx.extra.get("workspace_revision")
            ctx.extra.setdefault("recovery_events", []).append(event)
            if failure is not None:
                # Preserve the original exception type while exposing sanitized
                # diagnostics to callers/experiment runners on failed reads.
                try:
                    failure.datamind_recovery = recovery_summary(ctx.extra["recovery_events"])
                except (AttributeError, TypeError):
                    pass
        _log.info("read_recovery", extra={"recovery": event})
        if token is not None:
            _BUDGET.reset(token)
