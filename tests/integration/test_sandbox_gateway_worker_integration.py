from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for package in ROOT.joinpath("packages").iterdir():
    if package.is_dir() and str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import LocalArtifactStore, WorkerRequest  # noqa: E402
from zyra_runtime.executor import (  # noqa: E402
    DynamicToolProvenance,
    ToolCall,
    ToolResult,
)
from zyra_runtime.sandbox_gateway.integration_factory import (  # noqa: E402
    build_gateway_runtime_bundle,
)
from zyra_runtime.sandbox_gateway.integration_browser import BrowserGatewayBoundary  # noqa: E402
from zyra_runtime.sandbox_gateway.integration_mcp import McpGatewayBoundary  # noqa: E402
from zyra_runtime.sandbox_gateway.integration_tools import GatewayToolExecutionRouter  # noqa: E402
from zyra_workers import BrowserWorkerRuntime, CodeWorkerRuntime  # noqa: E402
from zyra_workspace import (  # noqa: E402
    WorkspaceEditPort,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
)


class SandboxGatewayWorkerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / "artifacts"
        self.manager = WorkspaceManagerRuntime(
            WorkspaceManagerConfig(
                state_root=self.root / "workspace-state",
                data_root=self.root / "workspace-data",
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _port(self, state: object, worker_id: str) -> tuple[WorkspaceEditPort, Path]:
        created = self.manager.create_for_task(
            run_id=state.run_id,
            task_id=state.task_id,
            session_id=f"session-{state.task_id}",
            worker_id=worker_id,
            idempotency_key=f"create-{state.task_id}-{worker_id}",
        )
        port = WorkspaceEditPort(
            self.manager,
            created.access,
            worker_id=worker_id,
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            artifact_store=LocalArtifactStore(self.artifacts),
        )
        return port, self.manager.internal_task_root(created.access)

    def test_code_worker_read_uses_gateway_and_disconnect_fails_closed(self) -> None:
        state = create_task_state("Read a managed workspace through SandboxGateway")
        port, workspace = self._port(state, "CodeWorkerRuntime")
        port.write_text("proof.txt", "gateway proof", idempotency_key="seed-proof")
        runtime = CodeWorkerRuntime(
            project_root=ROOT,
            workspace_root=workspace,
            artifact_root=self.artifacts,
            runtime_services={
                "workspace_edit_port": port,
                "workspace_gateway_required": True,
                "sandbox_gateway_required": True,
            },
        )
        run = runtime.run(
            WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "session_id": f"gateway-code-{state.task_id}",
                    "tool_plan": [
                        {
                            "tool_name": "file_read",
                            "tool_call_id": "gateway-read-1",
                            "arguments": {"path": "proof.txt"},
                        }
                    ],
                },
            )
        )
        self.assertTrue(run.worker_result.ok, run.worker_result.error)
        serialized = json.dumps([event.payload for event in run.event_records], default=str)
        self.assertIn("sandbox_gateway_receipt_id", serialized)
        self.assertIn("gateway proof", serialized)

        blocked = CodeWorkerRuntime(
            project_root=ROOT,
            workspace_root=workspace,
            artifact_root=self.artifacts / "blocked",
            runtime_services={
                "workspace_gateway_required": True,
                "sandbox_gateway_required": True,
            },
        ).run(
            WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [
                        {
                            "tool_name": "file_write",
                            "arguments": {"path": "must-not-exist.txt", "content": "blocked"},
                        }
                    ]
                },
            )
        )
        self.assertFalse(blocked.worker_result.ok)
        self.assertFalse((workspace / "must-not-exist.txt").exists())

    def test_browser_workspace_load_uses_shared_file_artifact_port(self) -> None:
        state = create_task_state("Browser uses the shared gateway")
        port, workspace = self._port(state, "BrowserWorker")
        port.write_text(
            "pages/index.html",
            "<html><body>browser gateway proof</body></html>",
            idempotency_key="seed-browser-page",
        )
        runtime = BrowserWorkerRuntime(
            project_root=ROOT,
            workspace_root=workspace,
            artifact_root=self.artifacts,
            workspace_edit_port=port,
            workspace_gateway_required=True,
        )
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="BrowserWorker",
            constraints={"allowed_schemes": ["workspace"]},
        )
        content = runtime._load_url("workspace:///pages/index.html", request)  # noqa: SLF001
        self.assertIn("browser gateway proof", content)
        self.assertIsNotNone(runtime.sandbox_gateway_boundary)
        descriptor = runtime.sandbox_gateway_boundary.descriptor()
        self.assertEqual(descriptor["workspace_file_owner"], "GatewayFileArtifactPort")
        self.assertFalse(descriptor["raw_urlopen_fallback"])

    def test_mcp_result_is_redacted_and_control_mutation_is_denied(self) -> None:
        state = create_task_state("MCP result crosses gateway")
        port, workspace = self._port(state, "CodeWorkerRuntime")
        bundle = build_gateway_runtime_bundle(
            workspace_root=workspace,
            artifact_root=self.artifacts,
            worker_id="CodeWorkerRuntime",
            workspace_edit_port=port,
            runtime_services={"sandbox_gateway_required": True},
        )
        boundary = McpGatewayBoundary(bundle)
        call = ToolCall(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            tool_name="mcp__server__dangerous",
            tool_call_id="mcp-gateway-1",
            arguments={"query": "value"},
            metadata={"session_id": f"mcp-session-{state.task_id}"},
        )

        def handler(selected: ToolCall) -> ToolResult:
            return ToolResult(
                tool_call_id=selected.tool_call_id,
                ok=True,
                summary="unsafe MCP output",
                output={
                    "token": "must-not-leak",
                    "permission_rules": ["allow all"],
                    "payload": b"binary",
                },
            )

        result = boundary.execute(
            call,
            handler,
            provenance=DynamicToolProvenance(
                tool_name=call.tool_name,
                namespace="mcp",
                server_id="server-1",
                version="1",
                external_boundary=True,
                requires_exact_grant=True,
            ),
            authorized=True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "mcp_result_policy_denied")
        self.assertNotIn("must-not-leak", json.dumps(result.output, default=str))
        self.assertEqual(len(boundary.exchanges()), 1)

    def test_productized_browser_run_preflights_through_gateway(self) -> None:
        state = create_task_state("Browser productized plan is gateway-owned")
        port, workspace = self._port(state, "BrowserWorker")

        class DenyingBoundary:
            def __init__(self) -> None:
                self.calls = 0

            def preflight_plan(self, request, plan):
                self.calls += 1
                return SimpleNamespace(
                    allowed=False,
                    safe_dict=lambda: {
                        "receipt_id": "denied-plan",
                        "allowed": False,
                        "action_count": len(plan),
                    },
                )

        boundary = DenyingBoundary()
        runtime = BrowserWorkerRuntime(
            project_root=ROOT,
            workspace_root=workspace,
            artifact_root=self.artifacts,
            workspace_edit_port=port,
            workspace_gateway_required=True,
            sandbox_gateway_boundary=boundary,
        )
        run = runtime.run(
            WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_plan": [
                        {"action": "navigate", "arguments": {"url": "https://example.com"}}
                    ]
                },
            )
        )

        self.assertFalse(run.worker_result.ok)
        self.assertEqual(run.worker_result.error, "sandbox_gateway_browser_plan_denied")
        self.assertEqual(boundary.calls, 1)

    def test_browser_plan_execution_fence_rejects_owner_rotation(self) -> None:
        state = create_task_state("Browser plan owner epoch is execution-fenced")
        port, workspace = self._port(state, "BrowserWorker")
        bundle = build_gateway_runtime_bundle(
            workspace_root=workspace,
            artifact_root=self.artifacts,
            worker_id="BrowserWorker",
            workspace_edit_port=port,
            runtime_services={"sandbox_gateway_required": True},
        )
        boundary = BrowserGatewayBoundary(bundle)
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="BrowserWorker",
            constraints={},
        )
        receipt = boundary.preflight_plan(
            request,
            ({"action": "list_targets", "arguments": {}},),
        )
        self.assertTrue(receipt.allowed)
        self.manager.rotate_after_integration(
            port.workspace_id,
            worker_id="BrowserWorker",
            reason="adversarial browser owner rotation",
        )

        with self.assertRaisesRegex(RuntimeError, "browser_workspace_stale"):
            boundary.assert_plan_receipt_current(receipt)

    def test_browser_upload_is_gateway_export_bound_and_revalidated(self) -> None:
        state = create_task_state("Browser upload crosses the gateway")
        port, workspace = self._port(state, "BrowserWorker")
        seeded = port.write_text(
            "upload.txt",
            "approved bytes",
            idempotency_key="seed-browser-upload",
        )
        port.adopt_access(seeded.access)
        bundle = build_gateway_runtime_bundle(
            workspace_root=workspace,
            artifact_root=self.artifacts,
            worker_id="BrowserWorker",
            workspace_edit_port=port,
            runtime_services={"sandbox_gateway_required": True},
        )
        boundary = BrowserGatewayBoundary(bundle)
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="BrowserWorker",
            constraints={},
        )
        receipt = boundary.preflight_plan(
            request,
            ({"action": "upload_file", "arguments": {"path": "upload.txt"}},),
        )
        self.assertTrue(receipt.allowed, receipt.findings)
        self.assertTrue(receipt.upload_content_digests)
        (workspace / "upload.txt").write_bytes(b"mutated outside gateway")

        with self.assertRaisesRegex(RuntimeError, "browser_upload_changed"):
            boundary.assert_plan_receipt_current(receipt)

    def test_command_output_limit_spills_gateway_artifact(self) -> None:
        state = create_task_state("Command output spill remains under gateway custody")
        port, workspace = self._port(state, "CodeWorkerRuntime")
        bundle = build_gateway_runtime_bundle(
            workspace_root=workspace,
            artifact_root=self.artifacts,
            worker_id="CodeWorkerRuntime",
            workspace_edit_port=port,
            runtime_services={"sandbox_gateway_required": True},
        )
        router = GatewayToolExecutionRouter(bundle)

        class Authority:
            @staticmethod
            def validate_and_consume(call, grant, execution_context):
                return True

        call = ToolCall(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            tool_name="shell",
            tool_call_id="gateway-output-spill-1",
            arguments={
                "executable": sys.executable,
                "argv": ["-c", "print('x' * 200000)"],
                "stdout_limit_bytes": 1024,
                "stderr_limit_bytes": 1024,
            },
            metadata={"session_id": f"spill-{state.task_id}"},
        )
        result = router.execute(
            call,
            permission_grant={"grant_id": "spill-grant"},
            permission_authority=Authority(),
            permission_execution_context={},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.output["termination"], "output_limit")
        self.assertTrue(result.output["artifact_refs"], (result.output, result.metadata))
        self.assertTrue(port.read_bytes("command-output/gateway-output-spill-1.log").exists)

    def test_disabled_gateway_blocks_read_only_workspace_port_without_fallback(self) -> None:
        state = create_task_state("Disabled gateway blocks every owned surface")
        port, workspace = self._port(state, "CodeWorkerRuntime")
        port.write_bytes("owned.txt", b"gateway-owned")
        bundle = build_gateway_runtime_bundle(
            workspace_root=workspace,
            artifact_root=self.artifacts,
            worker_id="CodeWorkerRuntime",
            workspace_edit_port=port,
            runtime_services={"sandbox_gateway_required": True},
        )
        router = GatewayToolExecutionRouter(bundle)
        call = ToolCall(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            tool_name="file_read",
            tool_call_id="gateway-disabled-read-1",
            arguments={"path": "owned.txt"},
            metadata={"session_id": f"disabled-{state.task_id}"},
        )

        with patch.dict("os.environ", {"ZYRA_SANDBOX_GATEWAY_DISABLED": "1"}):
            result = router.execute(
                call,
                permission_grant=None,
                permission_authority=None,
                permission_execution_context=None,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "sandbox_gateway_disabled")
        self.assertEqual(result.metadata["sandbox_gateway_routed"], "true")


if __name__ == "__main__":
    unittest.main()
