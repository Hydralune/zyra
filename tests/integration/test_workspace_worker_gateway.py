from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "workspace",
):
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import LocalArtifactStore, WorkerRequest  # noqa: E402
from zyra_runtime.permission.models import (  # noqa: E402
    PermissionEffect,
    PermissionResolutionResponse,
)
from zyra_runtime.permission.control_plane import PermissionControlPlane  # noqa: E402
from zyra_runtime.permission.custody import PermissionSessionCustodyBinding  # noqa: E402
from zyra_runtime.permission.request_queue import PermissionRequestQueue  # noqa: E402
from zyra_runtime.permission.store import PermissionStateStore  # noqa: E402
from zyra_runtime.permission.transports import PermissionTransportKind  # noqa: E402
from zyra_workers import BrowserWorkerRuntime, CodeWorkerRuntime  # noqa: E402
from zyra_workspace import (  # noqa: E402
    WorkspaceEditPort,
    WorkspaceIsolationRuntime,
    WorkspaceKind,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
)


class WorkspaceWorkerGatewayIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = WorkspaceManagerRuntime(
            WorkspaceManagerConfig(
                state_root=self.root / "state",
                data_root=self.root / "data",
                lease_ttl_seconds=300,
            )
        )
        self.artifact_root = self.root / "artifacts"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_port(self, *, task_id: str, worker_id: str) -> WorkspaceEditPort:
        created = self.manager.create_for_task(
            run_id=f"run-{task_id}",
            task_id=task_id,
            session_id=f"session-{task_id}",
            worker_id=worker_id,
            idempotency_key=f"create-{task_id}",
        )
        return WorkspaceEditPort(
            self.manager,
            created.access,
            worker_id=worker_id,
            run_id=f"run-{task_id}",
            task_id=task_id,
            node_id=f"node-{task_id}",
            artifact_store=LocalArtifactStore(self.artifact_root),
        )

    def _approve_pending(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        custody_token: str,
    ) -> None:
        state_store = PermissionStateStore(self.artifact_root / ".permission" / "state.json")
        queue = PermissionRequestQueue(state_store, session_id=session_id)
        pending = queue.pending()
        self.assertEqual(len(pending), 1)
        request = pending[0]
        response = PermissionResolutionResponse(
                request_id=request.request_id,
                session_id=request.session_id,
                tool_use_id=request.tool_use_id,
                tool_identity=request.tool_identity,
                arguments_digest=request.arguments_digest,
                request_fingerprint=request.request_fingerprint,
                scope=request.scope,
                effect=PermissionEffect.ALLOW,
                actor_id="workspace-gateway-test",
                expected_revision=request.revision,
                channel="api",
                idempotency_key=idempotency_key,
        )
        plane = PermissionControlPlane(state_store)
        authority = plane.authority_from_custody(
            binding=PermissionSessionCustodyBinding(
                session_id=request.session_id,
                run_id=request.run_id,
                task_id=request.task_id,
                workspace_root=request.scope.workspace_root,
            ),
            custody_token=custody_token,
            actor_id="workspace-gateway-test",
            channel=PermissionTransportKind.API,
        )
        outcome = plane.resolve(authority, request.request_id, response.to_dict())
        self.assertTrue(outcome.ok, outcome.to_dict())

    def test_code_worker_mutations_use_workspace_transactions_and_missing_gateway_fails_closed(self) -> None:
        state = create_task_state("Mutate a managed task workspace through CodeWorker.")
        port = self._create_port(task_id=state.task_id, worker_id="CodeWorkerRuntime")
        workspace_root = self.manager.internal_task_root(port.current_access())
        runtime = CodeWorkerRuntime(
            project_root=ROOT,
            workspace_root=workspace_root,
            artifact_root=self.artifact_root,
            runtime_services={
                "workspace_edit_port": port,
                "workspace_isolation_runtime": WorkspaceIsolationRuntime(
                    self.manager,
                    artifact_store=LocalArtifactStore(self.artifact_root),
                ),
                "workspace_gateway_required": True,
            },
        )
        def run_approved(session_suffix: str, tool_calls: list[dict[str, object]]):
            session_id = f"workspace-gateway-{state.task_id}-{session_suffix}"
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={"session_id": session_id, "query_turns": [tool_calls]},
            )
            suspended = runtime.run(request)
            self.assertEqual(suspended.worker_result.error, "permission_suspended")
            custody_token = suspended.session_custody_token
            self.assertTrue(custody_token)
            self._approve_pending(
                session_id=session_id,
                idempotency_key=f"workspace-gateway-approval-{session_suffix}",
                custody_token=custody_token,
            )
            completed = runtime.run(
                WorkerRequest(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    worker_name=request.worker_name,
                    constraints={
                        **request.constraints,
                        "session_custody_token": custody_token,
                    },
                )
            )
            self.assertTrue(completed.worker_result.ok, completed.worker_result.error)
            return completed

        write_run = run_approved(
            "write",
            [{
                "tool_name": "file_write",
                "tool_call_id": "workspace-gateway-write-1",
                "arguments": {
                    "path": "src/gateway.txt",
                    "content": "gateway one\n",
                    "idempotency_key": "code-write-gateway",
                },
            }],
        )
        edit_run = run_approved(
            "edit",
            [
                {
                    "tool_name": "file_edit",
                    "tool_call_id": "workspace-gateway-edit-1",
                    "arguments": {
                        "path": "src/gateway.txt",
                        "old": "one",
                        "new": "two",
                        "idempotency_key": "code-edit-gateway",
                    },
                },
                {
                    "tool_name": "file_read",
                    "tool_call_id": "workspace-gateway-read-1",
                    "arguments": {"path": "src/gateway.txt"},
                },
            ],
        )

        self.assertEqual(port.read_text("src/gateway.txt").text(), "gateway two\n")
        transactions = self.manager.integration_store.list_transactions(port.workspace_id)
        committed = [item for item in transactions if item.ok]
        self.assertEqual(len(committed), 2)
        self.assertTrue(all(item.snapshot_id for item in committed))
        self.assertTrue(all(item.owner_epoch_after > item.owner_epoch_before for item in committed))
        self.assertIn("sandbox_gateway_receipt_id", serialized_events := json.dumps(
            [item.payload for run in (write_run, edit_run) for item in run.event_records],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ))
        self.assertNotIn(str(workspace_root), serialized_events)

        blocked_state = create_task_state("Prove raw CodeWorker mutation fallback is disconnected.")
        blocked_port = self._create_port(task_id=blocked_state.task_id, worker_id="CodeWorkerRuntime")
        blocked_root = self.manager.internal_task_root(blocked_port.current_access())
        blocked_runtime = CodeWorkerRuntime(
            project_root=ROOT,
            workspace_root=blocked_root,
            artifact_root=self.artifact_root,
            runtime_services={"workspace_gateway_required": True},
        )
        blocked_session_id = f"workspace-gateway-blocked-{blocked_state.task_id}"
        blocked_request = WorkerRequest(
            run_id=blocked_state.run_id,
            task_id=blocked_state.task_id,
            node_id=blocked_state.root_node_id,
            worker_name="CodeWorkerRuntime",
            constraints={
                "session_id": blocked_session_id,
                "query_turns": [[{
                    "tool_name": "file_write",
                    "tool_call_id": "workspace-gateway-blocked-write-1",
                    "arguments": {"path": "must-not-exist.txt", "content": "raw fallback"},
                }]],
            },
        )
        blocked_pending = blocked_runtime.run(blocked_request)
        self.assertEqual(blocked_pending.worker_result.error, "permission_suspended")
        blocked_custody = blocked_pending.session_custody_token
        self._approve_pending(
            session_id=blocked_session_id,
            idempotency_key="workspace-gateway-blocked-approval",
            custody_token=blocked_custody,
        )
        blocked = blocked_runtime.run(
            WorkerRequest(
                run_id=blocked_request.run_id,
                task_id=blocked_request.task_id,
                node_id=blocked_request.node_id,
                worker_name=blocked_request.worker_name,
                constraints={
                    **blocked_request.constraints,
                    "session_custody_token": blocked_custody,
                },
            )
        )
        self.assertFalse(blocked.worker_result.ok)
        self.assertFalse((blocked_root / "must-not-exist.txt").exists())
        blocked_results = [
            event.payload["tool_result"]
            for event in blocked.event_records
            if isinstance(event.payload, dict) and "tool_result" in event.payload
        ]
        self.assertEqual(blocked_results[-1]["error"], "sandbox_gateway_unavailable")

    def test_browser_workspace_url_uses_gateway_and_missing_gateway_fails_closed(self) -> None:
        state = create_task_state("Read a managed page through BrowserWorker.")
        port = self._create_port(task_id=state.task_id, worker_id="BrowserWorker")
        write = port.write_text(
            "pages/index.html",
            "<html><title>Gateway</title><body>managed browser page</body></html>",
            idempotency_key="browser-page",
        )
        workspace_root = self.manager.internal_task_root(write.access)
        runtime = BrowserWorkerRuntime(
            project_root=ROOT,
            workspace_root=workspace_root,
            artifact_root=self.artifact_root,
            workspace_edit_port=port,
            workspace_gateway_required=True,
        )
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="BrowserWorker",
            constraints={
                "browser_backend": "static",
                "browser_plan": [
                    {"action": "open_url", "arguments": {"url": "workspace:///pages/index.html"}},
                    {"action": "extract_text"},
                ],
                "allowed_schemes": ["workspace"],
            },
        )

        run = runtime.run(request)

        self.assertTrue(run.worker_result.ok)
        self.assertTrue(
            any(
                "managed browser page"
                in str((event.payload.get("browser_result") or {}).get("output") or "")
                for event in run.event_records
            )
        )
        blocked_runtime = BrowserWorkerRuntime(
            project_root=ROOT,
            workspace_root=workspace_root,
            artifact_root=self.artifact_root,
            workspace_gateway_required=True,
        )
        blocked = blocked_runtime.run(request)
        self.assertFalse(blocked.worker_result.ok)
        self.assertIn(
            blocked.worker_result.error,
            {"browser_action_failed", "browser_action_permission_unavailable"},
        )
        with self.assertRaisesRegex(RuntimeError, "sandbox_gateway_unavailable"):
            blocked_runtime._load_url("workspace:///pages/index.html", request)  # noqa: SLF001

    def test_download_mount_write_is_journaled_idempotent_and_path_free(self) -> None:
        port = self._create_port(task_id="download-task", worker_id="BrowserWorker")
        first = port.write_bytes(
            "report.bin",
            b"download bytes",
            mount_kind=WorkspaceKind.DOWNLOAD,
            idempotency_key="browser-download-report",
            causation_id="browser-download-step",
        )
        owner_epoch = first.access.owner_epoch
        replay = port.write_bytes(
            "report.bin",
            b"download bytes",
            mount_kind=WorkspaceKind.DOWNLOAD,
            idempotency_key="browser-download-report",
            causation_id="browser-download-step",
        )

        self.assertTrue(first.ok)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(replay.transaction.transaction_id, first.transaction.transaction_id)
        self.assertEqual(replay.access.owner_epoch, owner_epoch)
        self.assertEqual(
            port.read_bytes("report.bin", mount_kind=WorkspaceKind.DOWNLOAD).content,
            b"download bytes",
        )
        serialized = json.dumps(first.to_public_dict(), sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(first.access.fence_token, serialized)


if __name__ == "__main__":
    unittest.main()
