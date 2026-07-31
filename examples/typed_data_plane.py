"""Minimal deterministic DataMind execution over a document source."""
from __future__ import annotations

import asyncio

from datamind import Engine
from datamind.adapters import DocumentRecord, InMemoryDocumentSource
from datamind.dataops import Search
from datamind.kernel import ExecutionContext
from datamind.lifecycle import SourceCatalog


async def main() -> None:
    source = InMemoryDocumentSource(
        source_id="policy-kb",
        version="policy-v1",
        documents=(
            DocumentRecord(
                document_id="travel-policy",
                content="Meals are reimbursable up to 100 dollars.",
                metadata={"department": "sales"},
            ),
            DocumentRecord(
                document_id="security-policy",
                content="Hardware keys are required for production access.",
                metadata={"department": "security"},
            ),
        ),
    )
    catalog = SourceCatalog()
    catalog.register(source)
    engine = Engine(catalog)

    result = await engine.execute(
        Search(
            source=source.descriptor.ref,
            query="meals reimbursable",
            filters={"department": "sales"},
        ),
        context=ExecutionContext.new(),
    )

    for hit in result.value:
        print("{}: {:.3f} — {}".format(
            hit.document_id,
            hit.score,
            hit.content,
        ))
    print("trace:", result.trace_id)
    print("snapshot:", result.snapshots[0].version)


if __name__ == "__main__":
    asyncio.run(main())
