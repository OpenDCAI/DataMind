"""DataMind error hierarchy.

Every new-code error inherits from DataMindError so callers can filter at a
single root. Three subtypes cover the actionable distinctions:

    ConfigError          — bad settings, unknown registry name, validation fail.
                           Fix: change config / env / code. Not retriable.
    CapabilityError      — an internal capability (KB/Graph/DB/Memory) failed.
                           Fix: inspect `capability` and `cause`. Sometimes retriable.
    ExternalServiceError — a 3rd-party service (gateway, DB server, etc.)
                           returned an error or was unreachable. Often retriable.
"""
from __future__ import annotations


class DataMindError(Exception):
    """Base for everything raised by the new datamind/ stack."""


class ConfigError(DataMindError):
    """Invalid configuration, unknown provider name, missing required field."""


class CapabilityError(DataMindError):
    """An internal capability failed."""

    def __init__(self, capability: str, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(f"[{capability}] {message}")
        self.capability = capability
        self.__cause__ = cause


class ExternalServiceError(DataMindError):
    """A remote service (LLM gateway, DB, embedding API, ...) returned an error."""

    def __init__(
        self,
        service: str,
        message: str,
        *,
        status_code: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        suffix = f" (status={status_code})" if status_code is not None else ""
        super().__init__(f"[{service}] {message}{suffix}")
        self.service = service
        self.status_code = status_code
        self.__cause__ = cause


class FinalAnswerContractError(DataMindError):
    """The model could not produce a caller-compliant final answer.

    The raw provider output is intentionally omitted from the exception so a
    tool-result dump cannot leak through an error path.  Callers may retry the
    whole turn once because contract failures and exhausted finalization time
    can be stochastic.
    """

    retryable = True

    def __init__(self, reason: str, *, stop_reason: str) -> None:
        super().__init__(f"final answer contract failed ({stop_reason}): {reason}")
        self.reason = reason
        self.stop_reason = stop_reason
