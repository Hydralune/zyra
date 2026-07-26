from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from zyra_runtime import LocalArtifactStore  # noqa: E402
from zyra_runtime.sandbox_gateway import (  # noqa: E402
    ArchivePolicy,
    ArtifactProvenance,
    FileArtifactRequest,
    GatewayFileArtifactPort,
    GatewayFilePolicy,
    GatewayEventPort,
    GatewayPatchPort,
    GatewayPatchSet,
    GatewayStateStore,
    GatewaySessionRecord,
    PatchMutation,
    PatchOperation,
    ProvenanceKind,
    ProvenanceRegistry,
    QuarantineStore,
    SandboxGatewayError,
    TrustLevel,
)
from zyra_workspace import (  # noqa: E402
    WorkspaceEditPort,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
)


class SandboxGatewayArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = WorkspaceManagerRuntime(
            WorkspaceManagerConfig(
                state_root=self.root / "workspace-state",
                data_root=self.root / "workspace-data",
                lease_ttl_seconds=300,
            )
        )
        created = self.manager.create_for_task(
            run_id="run-artifact",
            task_id="task-artifact",
            session_id="session-artifact",
            worker_id="CodeWorkerRuntime",
            idempotency_key="create-artifact-workspace",
        )
        self.port = WorkspaceEditPort(
            self.manager,
            created.access,
            worker_id="CodeWorkerRuntime",
            run_id="run-artifact",
            task_id="task-artifact",
            node_id="node-artifact",
            artifact_store=LocalArtifactStore(self.root / "artifacts"),
        )
        self.state_store = GatewayStateStore(self.root / "gateway-state")
        self.state_store.create_session(
            GatewaySessionRecord.create(
                session_id="session-artifact",
                run_id="run-artifact",
                task_id="task-artifact",
                workspace_id=self.port.workspace_id,
                worker_id="CodeWorkerRuntime",
                backend_id="test-backend",
            )
        )
        self.event_port = GatewayEventPort(self.state_store)
        self.provenance = ProvenanceRegistry()
        self.quarantine = QuarantineStore(
            self.root / "gateway-state",
            self.state_store,
        )
        self.file_policy = GatewayFilePolicy()
        self.artifact_port = GatewayFileArtifactPort(
            self.port,
            file_policy=self.file_policy,
            provenance_registry=self.provenance,
            quarantine_store=self.quarantine,
            event_port=self.event_port,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def trusted(self, content: bytes = b"") -> ArtifactProvenance:
        return ArtifactProvenance.build(
            kind=ProvenanceKind.GENERATED,
            trust=TrustLevel.TRUSTED,
            source_id="test-generator",
            content_digest_value="",
        )

    def test_trusted_artifact_commits_through_workspace_transaction(self) -> None:
        request = FileArtifactRequest.build(
            session_id="session-artifact",
            logical_path="outputs/report.txt",
            content="gateway artifact\n",
            content_type="text/plain",
            provenance=self.trusted(),
            idempotency_key="trusted-artifact",
        )

        receipt = self.artifact_port.commit(request)

        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.transaction_id)
        self.assertGreater(receipt.owner_epoch_after, receipt.owner_epoch_before)
        self.assertEqual(
            self.artifact_port.read("outputs/report.txt"),
            b"gateway artifact\n",
        )
        transactions = self.manager.integration_store.list_transactions(
            self.port.workspace_id
        )
        self.assertTrue(
            any(item.transaction_id == receipt.transaction_id for item in transactions)
        )
        event = self.event_port.list("session-artifact")[-1]
        self.assertEqual(event.kind.value, "artifact.created")
        self.assertEqual(event.payload["digest"], request.content_digest)
        self.assertEqual(
            event.payload["request_id"],
            receipt.metadata["canonical_request_id"],
        )
        self.assertEqual(
            event.payload["mutation_id"],
            receipt.metadata["canonical_mutation_id"],
        )
        self.assertEqual(
            event.payload["artifact_id"],
            receipt.metadata["canonical_artifact_id"],
        )
        self.assertNotEqual(event.payload["request_id"], "[REDACTED]")
        self.assertNotEqual(event.payload["mutation_id"], "[REDACTED]")
        self.assertEqual(event.payload["revision"], receipt.owner_epoch_after)
        self.assertTrue(event.payload["effect_committed"])
        self.assertEqual(receipt.event_refs, (event.event_id,))

    def test_untrusted_control_file_is_quarantined_without_workspace_write(self) -> None:
        untrusted = ArtifactProvenance.build(
            kind=ProvenanceKind.WEB,
            trust=TrustLevel.UNTRUSTED,
            source_id="https://untrusted.example/policy",
            untrusted_instructions=True,
        )
        request = FileArtifactRequest.build(
            session_id="session-artifact",
            logical_path=".codex/settings.json",
            content='{"permission":"allow"}',
            content_type="application/json",
            provenance=untrusted,
            idempotency_key="untrusted-control",
        )

        receipt = self.artifact_port.commit(request)

        self.assertFalse(receipt.ok)
        self.assertTrue(receipt.quarantined)
        self.assertEqual(
            self.quarantine.read(receipt.quarantine_id),
            request.content,
        )
        self.assertFalse(self.port.read_bytes(".codex/settings.json").exists)

    def test_archive_traversal_is_rejected_before_any_entry_write(self) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("safe.txt", "safe")
            archive.writestr("../outside.txt", "escape")

        with self.assertRaises(SandboxGatewayError):
            ArchivePolicy().inspect(stream.getvalue(), filename="payload.zip")
        self.assertFalse(self.port.read_bytes("safe.txt").exists)

    def test_patch_preflights_all_mutations_and_uses_workspace_edit_port(self) -> None:
        existing = self.port.write_text(
            "existing.txt",
            "before\n",
            idempotency_key="patch-existing",
        )
        self.port.adopt_access(existing.access)
        current = self.port.current_access()
        patch_port = GatewayPatchPort(
            self.port,
            file_policy=self.file_policy,
            provenance_registry=self.provenance,
        )
        patch = GatewayPatchSet.build(
            session_id="session-artifact",
            mutations=(
                PatchMutation.create(
                    PatchOperation.EDIT,
                    "existing.txt",
                    content="after\n",
                ),
                PatchMutation.create(
                    PatchOperation.CREATE,
                    "created.txt",
                    content="created\n",
                ),
            ),
            base_workspace_id=self.port.workspace_id,
            base_owner_epoch=current.owner_epoch,
            reason="artifact test patch",
            idempotency_key="artifact-test-patch",
            command_id="command-artifact",
        )

        receipt = patch_port.apply(patch)

        self.assertTrue(receipt.ok)
        self.assertEqual(self.port.read_text("existing.txt").text(), "after\n")
        self.assertEqual(self.port.read_text("created.txt").text(), "created\n")
        self.assertEqual(len(receipt.transaction_ids), 2)

    def test_stale_patch_epoch_and_disabled_ports_fail_without_raw_fallback(self) -> None:
        current = self.port.current_access()
        patch_port = GatewayPatchPort(
            self.port,
            file_policy=self.file_policy,
            provenance_registry=self.provenance,
        )
        stale = GatewayPatchSet.build(
            session_id="session-artifact",
            mutations=(
                PatchMutation.create(
                    PatchOperation.CREATE,
                    "must-not-exist.txt",
                    content="blocked",
                ),
            ),
            base_workspace_id=self.port.workspace_id,
            base_owner_epoch=current.owner_epoch + 1,
            reason="stale",
            idempotency_key="stale-patch",
        )
        with self.assertRaises(SandboxGatewayError):
            patch_port.apply(stale)
        disabled = GatewayFileArtifactPort(
            self.port,
            file_policy=self.file_policy,
            provenance_registry=self.provenance,
            quarantine_store=self.quarantine,
            enabled=False,
        )
        request = FileArtifactRequest.build(
            session_id="session-artifact",
            logical_path="must-not-exist.txt",
            content="blocked",
            content_type="text/plain",
            provenance=self.trusted(),
        )
        with self.assertRaises(SandboxGatewayError):
            disabled.commit(request)
        self.assertFalse(self.port.read_bytes("must-not-exist.txt").exists)

    def test_file_transfer_revalidates_owner_epoch_after_permission(self) -> None:
        observed = self.port.current_access()
        request = FileArtifactRequest.build(
            session_id="session-artifact",
            logical_path="must-not-commit-after-rotation.txt",
            content="stale transfer",
            content_type="text/plain",
            provenance=self.trusted(),
            expected_workspace_id=observed.workspace_id,
            expected_owner_epoch=observed.owner_epoch,
            idempotency_key="stale-file-transfer",
        )
        self.manager.rotate_after_integration(
            observed.workspace_id,
            worker_id="CodeWorkerRuntime",
            reason="adversarial rotation after permission",
        )

        with self.assertRaises(SandboxGatewayError) as raised:
            self.artifact_port.commit(request)

        self.assertEqual(raised.exception.code.value, "workspace_stale")
        root = self.manager.internal_task_root(
            self.manager.acquire_for_worker(
                task_id="task-artifact",
                session_id="session-artifact",
                worker_id="CodeWorkerRuntime",
            )
        )
        self.assertFalse((root / "must-not-commit-after-rotation.txt").exists())


if __name__ == "__main__":
    unittest.main()
