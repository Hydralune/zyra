from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from zyra_runtime import LocalArtifactStore  # noqa: E402
from zyra_runtime.sandbox_gateway import (  # noqa: E402
    CallbackToolPermissionRuntimePort,
    CommandEffect,
    GatewayCommandEnvelope,
    GatewayEventPort,
    GatewayFileArtifactPort,
    GatewayFilePolicy,
    GatewayPatchPort,
    GatewayPermissionRelay,
    GatewayStateStore,
    LocalProcessSandboxBackend,
    PermissionRuntimeDecision,
    ProvenanceRegistry,
    QuarantineStore,
    ReceiptLedger,
    SandboxGatewayConfig,
    SandboxGatewayError,
    SandboxGatewayRuntime,
    SandboxLifecycle,
    StructuredCommandPolicy,
    token_digest,
)
from zyra_workspace import (  # noqa: E402
    WorkspaceEditPort,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
)


class SandboxGatewayRuntimeIntegrationTests(unittest.TestCase):
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
            run_id="run-gateway",
            task_id="task-gateway",
            session_id="session-gateway",
            worker_id="CodeWorkerRuntime",
            idempotency_key="create-gateway-workspace",
        )
        self.workspace_port = WorkspaceEditPort(
            self.manager,
            created.access,
            worker_id="CodeWorkerRuntime",
            run_id="run-gateway",
            task_id="task-gateway",
            node_id="node-gateway",
            artifact_store=LocalArtifactStore(self.root / "artifacts"),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def runtime(self, *, enabled: bool = True) -> SandboxGatewayRuntime:
        state = GatewayStateStore(self.root / "gateway-state")
        provenance = ProvenanceRegistry()
        quarantine = QuarantineStore(self.root / "gateway-state", state)
        file_policy = GatewayFilePolicy()
        artifact_port = GatewayFileArtifactPort(
            self.workspace_port,
            file_policy=file_policy,
            provenance_registry=provenance,
            quarantine_store=quarantine,
        )
        patch_port = GatewayPatchPort(
            self.workspace_port,
            file_policy=file_policy,
            provenance_registry=provenance,
        )

        def evaluate(request):
            return PermissionRuntimeDecision(
                allowed=True,
                effect=(
                    CommandEffect.ASK
                    if request.policy.effect is CommandEffect.ASK
                    else CommandEffect.ALLOW
                ),
                request_fingerprint=request.request_fingerprint,
                grant_material={
                    "grant_id": request.request_id,
                    "command_digest": request.envelope.identity_digest,
                },
                expires_at=time.time() + 120,
                reason="integration ToolPermissionRuntime grant",
            )

        def consume(request, decision, replay):
            return (
                decision.request_fingerprint == request.request_fingerprint
                and replay.identity_digest == request.envelope.identity_digest
            )

        permission = GatewayPermissionRelay(
            state,
            CallbackToolPermissionRuntimePort(
                evaluate=evaluate,
                validate_and_consume=consume,
                descriptor={"test_adapter": True},
            ),
        )
        return SandboxGatewayRuntime(
            SandboxGatewayConfig(
                state_root=self.root / "gateway-state",
                interactive=True,
                enabled=enabled,
                preserve_failed_patch_root=self.root / "failed-patches",
            ),
            state_store=state,
            lifecycle=SandboxLifecycle(state),
            backend=LocalProcessSandboxBackend(self.root / "gateway-state"),
            command_policy=StructuredCommandPolicy(),
            permission_relay=permission,
            event_port=GatewayEventPort(state),
            receipt_ledger=ReceiptLedger(state),
            artifact_port=artifact_port,
            patch_port=patch_port,
        )

    def test_real_command_reaches_backend_and_output_merges_through_workspace_port(self) -> None:
        runtime = self.runtime()
        record = runtime.create_session(
            session_id="session-gateway",
            run_id="run-gateway",
            task_id="task-gateway",
            workspace_id=self.workspace_port.workspace_id,
            worker_id="CodeWorkerRuntime",
        )
        access = self.workspace_port.current_access()
        command = GatewayCommandEnvelope.build(
            session_id=record.session_id,
            run_id=record.run_id,
            task_id=record.task_id,
            worker_id=record.worker_id,
            executable=sys.executable,
            argv=(
                "-c",
                "from pathlib import Path; "
                "Path('generated.txt').write_text('gateway output\\n', encoding='utf-8')",
            ),
            tool_use_id="tool-gateway-write",
            workspace_id=self.workspace_port.workspace_id,
            owner_epoch=access.owner_epoch,
            fence_digest=token_digest(access.fence_token),
            idempotency_key="gateway-command-write",
        )

        execution = runtime.execute(command)

        self.assertTrue(execution.ok)
        self.assertIsNotNone(execution.patch_receipt)
        self.assertEqual(
            self.workspace_port.read_text("generated.txt").text().splitlines(),
            ["gateway output"],
        )
        self.assertTrue(execution.permission_consumption_id)
        events = runtime.event_port.list(record.session_id)
        kinds = {item.kind.value for item in events}
        self.assertIn("gateway_command_started", kinds)
        self.assertIn("gateway_patch_committed", kinds)
        transactions = self.manager.integration_store.list_transactions(
            self.workspace_port.workspace_id
        )
        self.assertTrue(
            any(
                item.transaction_id
                in set(execution.patch_receipt.transaction_ids)
                for item in transactions
            )
        )
        closed = runtime.close_session(record.session_id)
        self.assertEqual(closed.state.value, "closed")

    def test_disabled_gateway_fails_closed_without_starting_backend(self) -> None:
        runtime = self.runtime(enabled=False)

        with self.assertRaises(SandboxGatewayError):
            runtime.create_session(
                session_id="session-disabled",
                run_id="run-gateway",
                task_id="task-gateway",
                workspace_id=self.workspace_port.workspace_id,
                worker_id="CodeWorkerRuntime",
            )
        self.assertFalse(
            (self.root / "gateway-state" / "sandbox-sessions").exists()
            and any(
                (self.root / "gateway-state" / "sandbox-sessions").iterdir()
            )
        )

    def test_restart_recovers_preparing_session_to_ready(self) -> None:
        runtime = self.runtime()
        record = runtime.lifecycle.create(
            session_id="session-recovery",
            run_id="run-gateway",
            task_id="task-gateway",
            workspace_id=self.workspace_port.workspace_id,
            worker_id="CodeWorkerRuntime",
            backend_id=runtime.backend.backend_id,
        )
        preparing = runtime.lifecycle.prepare(record.session_id)
        self.assertEqual(preparing.state.value, "preparing")

        restarted = self.runtime()
        recovered = restarted.recover_on_startup()

        match = next(item for item in recovered if item.session_id == record.session_id)
        self.assertEqual(match.state.value, "ready")
        event = next(
            item
            for item in restarted.event_port.list(record.session_id)
            if item.kind.value == "session_recovered"
        )
        self.assertEqual(event.run_id, "run-gateway")
        self.assertEqual(event.task_id, "task-gateway")
        self.assertEqual(event.payload["session_id"], record.session_id)
        self.assertTrue(str(event.payload["mutation_id"]).startswith("mutation-"))
        self.assertTrue(str(event.payload["lease_id"]).startswith("lease-"))
        self.assertEqual(event.payload["revision"], match.generation)
        self.assertTrue(event.payload["effect_committed"])


if __name__ == "__main__":
    unittest.main()
