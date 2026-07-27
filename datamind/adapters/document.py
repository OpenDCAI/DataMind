"""Deterministic in-memory document source used as a reference adapter."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import quote

from datamind.dataops import Evidence, ResultKind, Search
from datamind.kernel import (
    ExecutionContext,
    JsonObject,
    KernelValidationError,
    Provenance,
    SnapshotRef,
    SourceDescriptor,
    SourceKind,
    SourceRef,
    freeze_json_object,
)
from datamind.ports import SourceResult

_TOKEN_PATTERN = re.compile(r"\w+", flags=re.UNICODE)


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    content: str
    metadata: JsonObject = field(default_factory=freeze_json_object)

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, str) or not self.document_id.strip():
            raise KernelValidationError(
                "document_id must be a non-empty string"
            )
        if not isinstance(self.content, str) or not self.content.strip():
            raise KernelValidationError(
                "document content must be a non-empty string"
            )
        object.__setattr__(
            self,
            "metadata",
            freeze_json_object(self.metadata),
        )


@dataclass(frozen=True)
class DocumentHit:
    document_id: str
    content: str
    score: float
    metadata: JsonObject


class InMemoryDocumentSource:
    """Small lexical baseline that demonstrates the document Source Port."""

    def __init__(
        self,
        *,
        source_id: str,
        documents: Iterable[DocumentRecord],
        version: str = "1",
        display_name: str = "In-memory documents",
    ) -> None:
        self._documents = tuple(documents)
        if len({item.document_id for item in self._documents}) != len(
            self._documents
        ):
            raise KernelValidationError(
                "document ids must be unique within a source"
            )
        ref = SourceRef(source_id, SourceKind.DOCUMENT)
        self._descriptor = SourceDescriptor(
            ref=ref,
            display_name=display_name,
            capabilities=frozenset(("search",)),
            version=version,
            schema={
                "record": {
                    "document_id": "string",
                    "content": "string",
                    "metadata": "object",
                }
            },
            metadata={"adapter": "in_memory_document"},
        )

    @property
    def descriptor(self) -> SourceDescriptor:
        return self._descriptor

    async def execute(
        self,
        operation: Any,
        *,
        context: ExecutionContext,
    ) -> SourceResult[Any]:
        del context
        if not isinstance(operation, Search):
            raise KernelValidationError(
                "document source only supports Search"
            )

        matches = []
        for record in self._documents:
            if not self._matches_filters(record, operation.filters):
                continue
            score = self._score(operation.query, record.content)
            if score > 0:
                matches.append((score, record))
        matches.sort(key=lambda item: (-item[0], item[1].document_id))
        matches = matches[: operation.limit]

        snapshot = SnapshotRef(
            source=self.descriptor.ref,
            version=self.descriptor.version or "unversioned",
        )
        hits = []
        evidence = []
        provenance = []
        for score, record in matches:
            locator = "document://{}/{}".format(
                quote(self.descriptor.ref.source_id, safe=""),
                quote(record.document_id, safe=""),
            )
            origin = Provenance(
                source=self.descriptor.ref,
                locator=locator,
                snapshot=snapshot,
            )
            hit = DocumentHit(
                document_id=record.document_id,
                content=record.content,
                score=score,
                metadata=record.metadata,
            )
            hits.append(hit)
            evidence.append(
                Evidence(
                    kind=SourceKind.DOCUMENT,
                    content=record.content,
                    provenance=origin,
                    score=score,
                    metadata={
                        "document_id": record.document_id,
                        **dict(record.metadata),
                    },
                )
            )
            provenance.append(origin)

        return SourceResult(
            value=tuple(hits),
            result_kind=ResultKind.DOCUMENT_HITS,
            evidence=tuple(evidence),
            provenance=tuple(provenance),
            snapshots=(snapshot,),
        )

    @staticmethod
    def _matches_filters(
        record: DocumentRecord,
        filters: JsonObject,
    ) -> bool:
        return all(
            record.metadata.get(key) == expected
            for key, expected in filters.items()
        )

    @staticmethod
    def _score(query: str, content: str) -> float:
        normalized_query = query.casefold()
        normalized_content = content.casefold()
        query_terms = set(_TOKEN_PATTERN.findall(normalized_query))
        content_terms = set(_TOKEN_PATTERN.findall(normalized_content))
        if query_terms:
            lexical = len(query_terms & content_terms) / len(query_terms)
        else:
            lexical = 0.0
        phrase = 1.0 if normalized_query in normalized_content else 0.0
        return round(max(lexical, phrase), 8)
