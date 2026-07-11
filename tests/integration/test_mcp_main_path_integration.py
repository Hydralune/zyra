from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT,
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations.mcp.runtime import McpClientRuntime  # noqa: E402
from zyra_runtime import (  # noqa: E402
    ToolCall,
    ToolExecutionContext,
    ToolExecutionRuntime,
    ToolExecutor,
    ToolLoopScheduler,
    ToolRegistryRuntime,
    ToolResultBudgetRuntime,
    ToolUseContext,
)
from zyra_runtime.permission.models import (  # noqa: E402
    PermissionEffect,
    PermissionResolutionResponse,
)
from zyra_runtime.permission.runtime import ToolPermissionRuntime  # noqa: E402


FAKE_SERVER = ROOT / "tests" / "support" / "fake_mcp_server.py"


class McpMainPathIntegrationTests(unittest.TestCase):
    def _runtime(self, root: Path, *, server_id: str = "live-stdio") -> tuple[McpClientRuntime, Path]:
        fake_state = root / f"{server_id}-peer-state.json"
        runtime = McpClientRuntime.from_paths(
            state_path=root / f"{server_id}-runtime-state.json",
            artifact_root=root / "mcp-artifacts",
            credential_root=root / "mcp-credentials",
        )
        runtime.add_server(
            server_id,
            {
                "server_id": server_id,
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-u", str(FAKE_SERVER), "--state", str(fake_state)],
                "request_timeout_seconds": 5.0,
            },
        )
        receipt = runtime.connect_server(
            server_id,
            run_id="run-main-path",
            task_id="task-main-path",
            node_id="node-main-path",
            session_id="session-main-path",
            worker_request_id="worker-main-path",
        )
        self.assertTrue(receipt.connected, receipt.safe_dict())
        self.addCleanup(runtime.connection_runtime.close_all)
        return runtime, fake_state

    @staticmethod
    def _tool_name(projection: Any, remote_name: str) -> str:
        matches = [name for name in projection.bundle.tool_names if name.endswith(f"__{remote_name}")]
        if len(matches) != 1:
            raise AssertionError(f"expected one projected {remote_name!r} tool, got {matches!r}")
        return matches[0]

    def _execute_with_exact_permission(
        self,
        *,
        root: Path,
        runtime: McpClientRuntime,
        remote_name: str,
        arguments: dict[str, Any] | None = None,
        suffix: str,
    ) -> tuple[Any, Any, Any]:
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        base = ToolExecutionContext.for_workspace(workspace, root / "tool-artifacts")
        projection = runtime.worker_projection(
            base,
            run_id="run-main-path",
            task_id="task-main-path",
            node_id="node-main-path",
        )
        tool_name = self._tool_name(projection, remote_name)
        call = ToolCall(
            run_id="run-main-path",
            task_id="task-main-path",
            node_id="node-main-path",
            tool_name=tool_name,
            tool_call_id=f"mcp-{suffix}-call",
            arguments=arguments or {},
        )

        direct = ToolExecutor(projection.context).execute(call)
        self.assertFalse(direct.ok)
        self.assertEqual(direct.error, "permission_required")

        permission = ToolPermissionRuntime.for_session(
            session_id=f"session-main-path-{suffix}",
            state_path=root / "permission-state.json",
            workspace_root=workspace,
        )
        materialization = ToolRegistryRuntime(projection.context.registry).materialize(
            worker_request_id="worker-main-path",
            session_id=f"session-main-path-{suffix}",
            workspace_root=workspace,
        )
        scheduler = ToolLoopScheduler(materialization.to_registry())
        plan = scheduler.plan_turn(
            run_id=call.run_id,
            task_id=call.task_id,
            node_id=call.node_id,
            worker_request_id="worker-main-path",
            turn_index=1,
            steps=[
                {
                    "tool_name": tool_name,
                    "tool_call_id": call.tool_call_id,
                    "arguments": dict(call.arguments),
                }
            ],
        )
        context = ToolUseContext.for_turn(
            run_id=call.run_id,
            task_id=call.task_id,
            node_id=call.node_id,
            worker_request_id="worker-main-path",
            session_id=f"session-main-path-{suffix}",
            turn_id=f"turn-{suffix}",
            turn_index=1,
            materialization=materialization,
        )
        execution = ToolExecutionRuntime(
            projection.context,
            scheduler=scheduler,
            budget_runtime=ToolResultBudgetRuntime(max_result_chars=1_500),
            permission_runtime=permission,
        )
        first = execution.execute_request(plan.requests[0], context)
        self.assertFalse(first.raw_result.ok)
        self.assertEqual(first.raw_result.error, "permission_required")
        pending = permission.request_queue.pending(
            session_id=f"session-main-path-{suffix}"
        )
        self.assertEqual(len(pending), 1)
        request = pending[0]
        resolution = permission.resolve(
            PermissionResolutionResponse(
                request_id=request.request_id,
                session_id=request.session_id,
                tool_use_id=request.tool_use_id,
                tool_identity=request.tool_identity,
                arguments_digest=request.arguments_digest,
                request_fingerprint=request.request_fingerprint,
                scope=request.scope,
                effect=PermissionEffect.ALLOW,
                actor_id="mcp-main-path-test",
                expected_revision=request.revision,
                channel="user",
                idempotency_key=f"allow-{suffix}",
            )
        )
        self.assertTrue(resolution.accepted, resolution.to_dict())
        second = execution.execute_request(plan.requests[0], context)
        self.assertTrue(second.raw_result.ok, (second.raw_result, second.permission))
        self.assertEqual(
            second.raw_result.metadata.get("permission_execution_grant_consumed"),
            "true",
        )
        replay = execution.execute_request(plan.requests[0], context)
        self.assertFalse(replay.raw_result.ok)
        self.assertEqual(replay.raw_result.error, "permission_required")
        return second, projection, replay

    def test_stdio_permission_tool_artifact_resource_prompt_and_event_causality(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zyra-mcp-main-path-") as directory:
            root = Path(directory)
            runtime, fake_state_path = self._runtime(root)

            echo, _, _ = self._execute_with_exact_permission(
                root=root,
                runtime=runtime,
                remote_name="echo",
                arguments={"message": "permission-gated live result"},
                suffix="echo",
            )
            self.assertIn("permission-gated live result", str(echo.raw_result.output))

            payload, _, _ = self._execute_with_exact_permission(
                root=root,
                runtime=runtime,
                remote_name="payload",
                suffix="payload",
            )
            self.assertGreaterEqual(len(payload.raw_result.artifacts), 2)
            for artifact in payload.raw_result.artifacts:
                self.assertTrue(Path(artifact.uri).is_file(), artifact)
            serialized_output = json.dumps(payload.raw_result.output, default=str)
            self.assertNotIn("enlyYS1tY3AtYmluYXJ5", serialized_output)
            self.assertLess(len(serialized_output), 5_000)

            resource = runtime.read_resource(
                "live-stdio",
                "memo://live/status",
                run_id="run-main-path",
                task_id="task-main-path",
                node_id="node-main-path",
            )
            self.assertTrue(resource["ok"], resource)
            self.assertTrue(resource["projections"])
            prompt = runtime.get_prompt("live-stdio", "welcome", {"name": "Zyra"})
            self.assertEqual(prompt["messages"][0]["role"], "user")
            self.assertTrue(prompt["untrusted_external_content"])

            fake_state = json.loads(fake_state_path.read_text(encoding="utf-8"))
            self.assertEqual(fake_state["tool_calls"]["echo"], 1)
            self.assertEqual(fake_state["tool_calls"]["payload"], 1)

            events = [
                {
                    "run_id": event.run_id,
                    "task_id": event.task_id,
                    "node_id": event.node_id,
                    "event_type": str(event.event_type),
                    "payload": event.payload,
                }
                for event in runtime.drain_events(run_id="run-main-path")
            ]
            event_text = json.dumps(events, sort_keys=True)
            self.assertIn("mcp_tool_result", event_text)
            self.assertIn("run-main-path", event_text)
            self.assertIn("task-main-path", event_text)
            self.assertNotIn("private sampling prompt", event_text)

    def test_sampling_default_deny_long_task_refresh_and_compact_restore_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zyra-mcp-lifecycle-") as directory:
            root = Path(directory)
            runtime, fake_state_path = self._runtime(root)

            sampling, old_projection, _ = self._execute_with_exact_permission(
                root=root,
                runtime=runtime,
                remote_name="sampling_probe",
                suffix="sampling",
            )
            self.assertIn("sampling denied", str(sampling.raw_result.output).casefold())
            fake_state = json.loads(fake_state_path.read_text(encoding="utf-8"))
            self.assertEqual(fake_state["sampling_requests"], 1)
            self.assertEqual(fake_state["sampling_denied"], 1)
            self.assertEqual(fake_state["sampling_allowed"], 0)

            task, _, _ = self._execute_with_exact_permission(
                root=root,
                runtime=runtime,
                remote_name="long_job",
                suffix="long-task",
            )
            self.assertIn("long task completed", str(task.raw_result.output).casefold())
            fake_state = json.loads(fake_state_path.read_text(encoding="utf-8"))
            self.assertEqual(fake_state["tool_calls"]["long_job"], 1)
            self.assertGreaterEqual(fake_state["methods"].get("tasks/get", 0), 1)
            self.assertEqual(fake_state["methods"].get("tasks/result", 0), 1)

            old_snapshot = runtime.catalog.get("live-stdio")
            self.assertIsNotNone(old_snapshot)
            assert old_snapshot is not None
            expand, _, _ = self._execute_with_exact_permission(
                root=root,
                runtime=runtime,
                remote_name="expand_catalog",
                suffix="expand",
            )
            self.assertTrue(expand.raw_result.ok)
            deadline = time.monotonic() + 3.0
            refreshed = runtime.catalog.get("live-stdio")
            while (
                refreshed is not None
                and refreshed.generation <= old_snapshot.generation
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
                refreshed = runtime.catalog.get("live-stdio")
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreater(refreshed.generation, old_snapshot.generation)
            self.assertIn("inspect", {tool.remote_name for tool in refreshed.tools})
            self.assertIn("expanded-status", {resource.name for resource in refreshed.resources})
            self.assertIn("expanded", {prompt.remote_name for prompt in refreshed.prompts})

            old_echo_name = self._tool_name(old_projection, "echo")
            stale = old_projection.context.dynamic_handlers[old_echo_name](
                ToolCall(
                    run_id="run-main-path",
                    task_id="task-main-path",
                    node_id="node-main-path",
                    tool_name=old_echo_name,
                    tool_call_id="stale-call",
                    arguments={"message": "must not execute"},
                )
            )
            self.assertFalse(stale.ok)
            self.assertEqual(stale.error, "mcp_projection_stale")

            constraints = runtime.prepare_worker_constraints({}, session_id="session-main-path")
            deltas = constraints["mcp_instruction_deltas"]
            self.assertEqual(len(deltas), 1)
            self.assertEqual(deltas[0]["trust_level"], "external_untrusted")
            snapshot = runtime.session_snapshot("session-main-path")
            restored = McpClientRuntime.from_paths(
                state_path=root / "restored-runtime-state.json",
                artifact_root=root / "restored-artifacts",
                credential_root=root / "restored-credentials",
            )
            self.addCleanup(restored.connection_runtime.close_all)
            self.assertEqual(restored.restore_session_snapshot(snapshot), 1)
            restored_constraints = restored.prepare_worker_constraints({}, session_id="session-main-path")
            self.assertEqual(len(restored_constraints["mcp_instruction_deltas"]), 1)
            self.assertEqual(
                restored_constraints["mcp_instruction_deltas"][0]["instructions_hash"],
                deltas[0]["instructions_hash"],
            )

    def test_needs_auth_cache_is_distinct_from_generic_connection_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zyra-mcp-auth-") as directory:
            root = Path(directory)
            runtime = McpClientRuntime.from_paths(
                state_path=root / "auth-runtime-state.json",
                artifact_root=root / "auth-artifacts",
                credential_root=root / "auth-credentials",
            )
            runtime.add_server(
                "needs-auth",
                {
                    "server_id": "needs-auth",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [
                        "-u",
                        str(FAKE_SERVER),
                        "--state",
                        str(root / "auth-peer-state.json"),
                        "--require-auth",
                    ],
                    "metadata": {"auth_type": "oauth"},
                },
            )
            first = runtime.connect_server("needs-auth", session_id="auth-session")
            second = runtime.connect_server("needs-auth", session_id="auth-session")
            self.addCleanup(runtime.connection_runtime.close_all)
            self.assertEqual(first.snapshot.state.value, "needs_auth")
            self.assertFalse(first.auth_cache_hit)
            self.assertEqual(second.snapshot.state.value, "needs_auth")
            self.assertTrue(second.auth_cache_hit)
            diagnostics = runtime.diagnostics()
            self.assertEqual(diagnostics["health"]["state_counts"]["needs_auth"], 1)
            self.assertEqual(diagnostics["health"]["state_counts"].get("failed", 0), 0)


if __name__ == "__main__":
    unittest.main()
