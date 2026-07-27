"""Explicit, side-effect-free source catalog."""
from __future__ import annotations

from types import MappingProxyType
from typing import Dict, Mapping, Tuple

from datamind.kernel import (
    KernelValidationError,
    SourceDescriptor,
    SourceKind,
    SourceRef,
)
from datamind.ports import DataSource

from .errors import DuplicateSourceError, UnknownSourceError


class SourceCatalog:
    """Map stable source identities to injected adapter instances."""

    def __init__(self) -> None:
        self._sources: Dict[str, DataSource] = {}

    def register(self, source: DataSource) -> None:
        descriptor = getattr(source, "descriptor", None)
        execute = getattr(source, "execute", None)
        if not isinstance(descriptor, SourceDescriptor):
            raise KernelValidationError(
                "registered source must expose a SourceDescriptor"
            )
        if not callable(execute):
            raise KernelValidationError(
                "registered source must expose async execute()"
            )
        source_id = descriptor.ref.source_id
        if source_id in self._sources:
            raise DuplicateSourceError(
                "source {!r} is already registered".format(source_id)
            )
        self._sources[source_id] = source

    def get(self, source: SourceRef) -> DataSource:
        registered = self._sources.get(source.source_id)
        if registered is None:
            raise UnknownSourceError(
                "source {!r} is not registered".format(source.source_id)
            )
        if registered.descriptor.ref != source:
            raise UnknownSourceError(
                "source {!r} is registered with kind {}, not {}".format(
                    source.source_id,
                    registered.descriptor.ref.kind.value,
                    source.kind.value,
                )
            )
        return registered

    def describe(self, source: SourceRef) -> SourceDescriptor:
        return self.get(source).descriptor

    def discover(
        self,
        kinds: Tuple[SourceKind, ...] = (),
    ) -> Tuple[SourceDescriptor, ...]:
        selected = frozenset(kinds)
        return tuple(
            source.descriptor
            for source_id, source in sorted(self._sources.items())
            if not selected or source.descriptor.ref.kind in selected
        )

    def descriptors(self) -> Mapping[str, SourceDescriptor]:
        return MappingProxyType(
            {
                source_id: source.descriptor
                for source_id, source in self._sources.items()
            }
        )

    def __len__(self) -> int:
        return len(self._sources)

    def __contains__(self, source_id: str) -> bool:
        return source_id in self._sources

