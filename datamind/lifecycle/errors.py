"""Errors raised by explicit source catalog operations."""
from datamind.kernel import KernelError


class SourceCatalogError(KernelError):
    """Base error for source registration and lookup."""


class DuplicateSourceError(SourceCatalogError, ValueError):
    """A logical source id is already registered."""


class UnknownSourceError(SourceCatalogError, LookupError):
    """A requested logical source is not registered."""
