"""State lifecycle, pinned execution, and thin Engine API tests."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from datamind.adapters import (
    DOCUMENT_ARTIFACT_MEDIA_TYPE,
    DocumentRecord,
    InMemoryArtifactStore,
    InMemoryDocumentSource,
)
from datamind.adapters.audit import InMemoryTraceStore
from datamind.dataops import Search
from datamind.engine import Engine
from datamind.kernel import (
    ArtifactChange,
    ArtifactIntegrityError,
    ArtifactManifest,
    ArtifactRef,
    ChangeKind,
    ChangeSet,
    ExecutionContext,
    IdempotencyConflictError,
    KernelValidationError,
    SnapshotRef,
    SnapshotSet,
    SnapshotUnavailableError,
    SourceKind,
    SourceRef,
    UnsupportedSyncError,
    VersionConflictError,
    sha256_checksum,
)
from datamind.lifecycle import (
    LifecycleManager,
    SourceCatalog,
    change_set_from_json,
    change_set_to_json,
    manifest_from_json,
    manifest_to_json,
)


class LifecycleValueTests(unittest.TestCase):
    def test_snapshot_identity_excludes_observation_time(self) -> None:
        source = SourceRef("policy-kb", SourceKind.DOCUMENT)
        first = SnapshotRef(
            source=source,
            version="v1",
            checksum="a" * 64,
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        second = SnapshotRef(
            source=source,
            version="v1",
            checksum="a" * 64,
            observed_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(first, second)
        self.assertTrue(first.same_version_as(second))

    def test_snapshot_set_has_one_binding_per_logical_source(self) -> None:
        source = SourceRef("policy-kb", SourceKind.DOCUMENT)
        first = SnapshotRef(source=source, version="v1")
        second = SnapshotRef(source=source, version="v2")

        with self.assertRaises(KernelValidationError):
            SnapshotSet((first, second))

        bindings = SnapshotSet((first,)).with_snapshot(second)
        self.assertEqual(bindings.require(source), second)

    def test_manifest_and_change_set_have_lossless_versioned_codecs(
        self,
    ) -> None:
        source = SourceRef("policy-kb", SourceKind.DOCUMENT)
        content = b'{"content":"policy","document_id":"travel"}'
        manifest = ArtifactManifest(
            ref=ArtifactRef("travel", "v2"),
            source=source,
            checksum=sha256_checksum(content),
            locator="memory://policy-kb/travel/v2",
            media_type=DOCUMENT_ARTIFACT_MEDIA_TYPE,
            data_schema={"document_id": "string"},
            lineage=(ArtifactRef("travel", "v1"),),
        )
        change_set = ChangeSet(
            source=source,
            base_version="source-v1",
            changes=(
                ArtifactChange(
                    ChangeKind.UPDATE,
                    manifest.ref,
                    manifest,
                ),
            ),
            idempotency_key="policy-update-v2",
            change_set_id="change-policy-v2",
        )

        self.assertEqual(
            manifest_from_json(manifest_to_json(manifest)),
            manifest,
        )
        self.assertEqual(
            change_set_from_json(change_set_to_json(change_set)),
            change_set,
        )

    def test_manifest_requires_a_real_sha256_checksum(self) -> None:
        with self.assertRaises(KernelValidationError):
            ArtifactManifest(
                ref=ArtifactRef("travel", "v1"),
                source=SourceRef("policy-kb", SourceKind.DOCUMENT),
                checksum="not-a-checksum",
                locator="memory://travel/v1",
            )


class LifecycleExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.source = InMemoryDocumentSource(
            source_id="policy-kb",
            version="policy-v1",
            documents=(
                DocumentRecord(
                    document_id="travel-policy",
                    content=(
                        "Policy legacy-meal-limit-one-hundred applies."
                    ),
                    metadata={"department": "sales"},
                ),
            ),
        )
        self.catalog = SourceCatalog()
        self.catalog.register(self.source)
        self.artifacts = InMemoryArtifactStore()
        self.lifecycle = LifecycleManager(self.catalog, self.artifacts)
        self.trace_store = InMemoryTraceStore()
        self.engine = Engine(
            self.catalog,
            lifecycle=self.lifecycle,
            trace_store=self.trace_store,
            replay_artifact_store=self.trace_store,
        )

    def document_update(
        self,
        *,
        base_version: str,
        artifact_version: str,
        content: str,
        idempotency_key: str,
    ) -> ChangeSet:
        return self.document_change(
            kind=ChangeKind.UPDATE,
            base_version=base_version,
            document_id="travel-policy",
            artifact_version=artifact_version,
            content=content,
            idempotency_key=idempotency_key,
        )

    def document_change(
        self,
        *,
        kind: ChangeKind,
        base_version: str,
        document_id: str,
        artifact_version: str,
        idempotency_key: str,
        content: str = "",
    ) -> ChangeSet:
        ref = ArtifactRef(document_id, artifact_version)
        if kind is ChangeKind.DELETE:
            change = ArtifactChange(kind, ref)
            return ChangeSet(
                source=self.source.descriptor.ref,
                base_version=base_version,
                changes=(change,),
                idempotency_key=idempotency_key,
            )
        payload = json.dumps(
            {
                "document_id": document_id,
                "content": content,
                "metadata": {"department": "sales"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        manifest = ArtifactManifest(
            ref=ref,
            source=self.source.descriptor.ref,
            checksum=sha256_checksum(payload),
            locator="memory://policy-kb/{}/{}".format(
                document_id,
                artifact_version,
            ),
            media_type=DOCUMENT_ARTIFACT_MEDIA_TYPE,
            lineage=(ArtifactRef(document_id, base_version),),
        )
        self.artifacts.put(manifest, payload)
        return ChangeSet(
            source=self.source.descriptor.ref,
            base_version=base_version,
            changes=(
                ArtifactChange(kind, ref, manifest),
            ),
            idempotency_key=idempotency_key,
        )

    async def search(
        self,
        *,
        snapshots: SnapshotSet = SnapshotSet(),
        trace_id: str = "trace-search",
    ):
        return await self.engine.execute(
            Search(
                source=self.source.descriptor.ref,
                query="policy meal limit",
            ),
            context=ExecutionContext(
                request_id="request-search",
                trace_id=trace_id,
                snapshots=snapshots,
            ),
        )

    async def test_sync_keeps_old_snapshot_queryable(self) -> None:
        old_snapshot = await self.source.current_snapshot()
        change_set = self.document_update(
            base_version=old_snapshot.version,
            artifact_version="artifact-v2",
            content="Policy current-meal-limit-fifty applies.",
            idempotency_key="sync-policy-v2",
        )

        receipt = await self.engine.sync(change_set)
        latest = await self.search(trace_id="trace-latest")
        historical = await self.search(
            snapshots=SnapshotSet((old_snapshot,)),
            trace_id="trace-historical",
        )

        self.assertEqual(receipt.previous_snapshot, old_snapshot)
        self.assertNotEqual(receipt.snapshot.version, old_snapshot.version)
        self.assertIn("current-meal-limit-fifty", latest.value[0].content)
        self.assertIn(
            "legacy-meal-limit-one-hundred",
            historical.value[0].content,
        )
        self.assertTrue(
            historical.snapshots[0].same_version_as(old_snapshot)
        )

    async def test_missing_pin_fails_before_source_execution(self) -> None:
        unavailable = SnapshotRef(
            source=self.source.descriptor.ref,
            version="missing-version",
        )

        with self.assertRaises(SnapshotUnavailableError):
            await self.search(
                snapshots=SnapshotSet((unavailable,)),
                trace_id="trace-missing",
            )

    async def test_add_delete_and_historical_snapshot_are_explicit(
        self,
    ) -> None:
        initial = await self.source.current_snapshot()
        addition = self.document_change(
            kind=ChangeKind.ADD,
            base_version=initial.version,
            document_id="security-policy",
            artifact_version="security-v1",
            content="Security policy requires hardware keys.",
            idempotency_key="add-security-policy",
        )
        added = await self.engine.sync(addition)
        deletion = self.document_change(
            kind=ChangeKind.DELETE,
            base_version=added.snapshot.version,
            document_id="security-policy",
            artifact_version="security-v1",
            idempotency_key="delete-security-policy",
        )
        await self.engine.sync(deletion)
        operation = Search(
            source=self.source.descriptor.ref,
            query="security hardware keys",
        )

        latest = await self.engine.execute(
            operation,
            context=ExecutionContext.new(),
        )
        historical = await self.engine.execute(
            operation,
            context=ExecutionContext.new(
                snapshots=SnapshotSet((added.snapshot,))
            ),
        )

        self.assertEqual(latest.value, ())
        self.assertEqual(
            historical.value[0].document_id,
            "security-policy",
        )

    async def test_sync_is_idempotent_and_rejects_key_reuse(self) -> None:
        old_snapshot = await self.source.current_snapshot()
        change_set = self.document_update(
            base_version=old_snapshot.version,
            artifact_version="artifact-v2",
            content="Policy current-meal-limit-fifty applies.",
            idempotency_key="sync-policy-v2",
        )

        first = await self.engine.sync(change_set)
        second = await self.engine.sync(change_set)

        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first.snapshot, second.snapshot)

        conflicting = self.document_update(
            base_version=first.snapshot.version,
            artifact_version="artifact-v3",
            content="Policy current-meal-limit-seventy applies.",
            idempotency_key=change_set.idempotency_key,
        )
        with self.assertRaises(IdempotencyConflictError):
            await self.engine.sync(conflicting)

    async def test_sync_rejects_stale_base_version(self) -> None:
        old_snapshot = await self.source.current_snapshot()
        first = self.document_update(
            base_version=old_snapshot.version,
            artifact_version="artifact-v2",
            content="Policy current-meal-limit-fifty applies.",
            idempotency_key="sync-policy-v2",
        )
        await self.engine.sync(first)
        stale = self.document_update(
            base_version=old_snapshot.version,
            artifact_version="artifact-v3",
            content="Policy current-meal-limit-seventy applies.",
            idempotency_key="sync-policy-v3",
        )

        with self.assertRaises(VersionConflictError):
            await self.engine.sync(stale)

    async def test_trace_records_pins_and_replay_survives_new_state(self) -> None:
        old_snapshot = await self.source.current_snapshot()
        original = await self.search(
            snapshots=SnapshotSet((old_snapshot,)),
            trace_id="trace-pinned",
        )
        change_set = self.document_update(
            base_version=old_snapshot.version,
            artifact_version="artifact-v2",
            content="Policy current-meal-limit-fifty applies.",
            idempotency_key="sync-policy-v2",
        )
        await self.engine.sync(change_set)

        replayed = await self.engine.replay("trace-pinned")
        trace = await self.trace_store.get("trace-pinned")

        self.assertEqual(replayed, original)
        self.assertEqual(
            trace.events[0].details["snapshot_pins"][0]["version"],
            old_snapshot.version,
        )

    async def test_engine_requires_an_explicit_lifecycle_port(self) -> None:
        engine = Engine(self.catalog)
        old_snapshot = await self.source.current_snapshot()
        change_set = self.document_update(
            base_version=old_snapshot.version,
            artifact_version="artifact-v2",
            content="Policy current-meal-limit-fifty applies.",
            idempotency_key="sync-policy-v2",
        )

        with self.assertRaises(UnsupportedSyncError):
            await engine.sync(change_set)

    async def test_artifact_store_rejects_checksum_mismatch(self) -> None:
        payload = b'{"document_id":"travel-policy"}'
        manifest = ArtifactManifest(
            ref=ArtifactRef("travel-policy", "corrupt-v1"),
            source=self.source.descriptor.ref,
            checksum="0" * 64,
            locator="memory://corrupt",
            media_type=DOCUMENT_ARTIFACT_MEDIA_TYPE,
        )

        with self.assertRaises(ArtifactIntegrityError):
            self.artifacts.put(manifest, payload)


if __name__ == "__main__":
    unittest.main()
