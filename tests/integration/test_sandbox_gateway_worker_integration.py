from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

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
from zyra_runtime.sandbox_gateway.integration_mcp import McpGatewayBoundary  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
