from __future__ import annotations

"""Run a self-contained MCP lifecycle, permission and artifact smoke check."""

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for package in ("core", "runtime", "integrations", "workers"):
    value = str(ROOT / "packages" / package)
    if value not in sys.path:
        sys.path.insert(0, value)

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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zyra-mcp-smoke-") as directory:
        root = Path(directory)
        fake_server = ROOT / "tests" / "support" / "fake_mcp_server.py"
        fake_state_path = root / "fake-server-state.json"
        runtime = McpClientRuntime.from_paths(
            state_path=root / "mcp-state.json",
            artifact_root=root / "artifacts",
        )
        runtime.add_server(
            "smoke",
            {
                "server_id": "smoke",
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-u", str(fake_server), "--state", str(fake_state_path)],
                "request_timeout_seconds": 5.0,
            },
        )
        connected = runtime.connect_server(
            "smoke",
            run_id="smoke-run",
            task_id="smoke-task",
            node_id="smoke-node",
            session_id="smoke-session",
        )
        if not connected.connected:
            raise RuntimeError(f"MCP smoke connect failed: {connected.snapshot.to_dict()}")

        base = ToolExecutionContext.for_workspace(
            root / "workspace",
            root / "artifacts",
        )
        projection = runtime.worker_projection(
            base,
            run_id="smoke-run",
            task_id="smoke-task",
            node_id="smoke-node",
        )
        tool_name = projection.bundle.tool_names[0]
        spec = projection.context.registry.get(tool_name)
        if spec is None:
            raise RuntimeError("projected MCP tool was not registered")
        call = ToolCall(
            run_id="smoke-run",
            task_id="smoke-task",
            node_id="smoke-node",
            tool_name=tool_name,
            tool_call_id="smoke-tool-call",
            arguments={"message": "permission-gated-success"},
        )

        denied = ToolExecutor(projection.context).execute(call)
        initial_fake_state = json.loads(fake_state_path.read_text(encoding="utf-8"))
        if denied.ok or denied.error != "permission_required" or initial_fake_state["tool_calls"].get("echo", 0):
            raise RuntimeError("MCP execution crossed the side-effect boundary without an exact grant")

        permission = ToolPermissionRuntime.for_session(
            session_id="smoke-session",
            state_path=root / "permission-state.json",
            workspace_root=projection.context.workspace_root,
        )
        materialization = ToolRegistryRuntime(projection.context.registry).materialize(
            worker_request_id="smoke-worker-request",
            session_id="smoke-session",
            workspace_root=projection.context.workspace_root,
        )
        scheduler = ToolLoopScheduler(materialization.to_registry())
        plan = scheduler.plan_turn(
            run_id=call.run_id,
            task_id=call.task_id,
            node_id=call.node_id,
            worker_request_id="smoke-worker-request",
            turn_index=1,
            steps=[
                {
                    "tool_name": call.tool_name,
                    "tool_call_id": call.tool_call_id,
                    "arguments": dict(call.arguments),
                }
            ],
        )

        def tool_context(turn_id: str) -> ToolUseContext:
            return ToolUseContext.for_turn(
                run_id=call.run_id,
                task_id=call.task_id,
                node_id=call.node_id,
                worker_request_id="smoke-worker-request",
                session_id="smoke-session",
                turn_id=turn_id,
                turn_index=1,
                materialization=materialization,
            )

        execution = ToolExecutionRuntime(
            projection.context,
            scheduler=scheduler,
            budget_runtime=ToolResultBudgetRuntime(max_result_chars=8_000),
            permission_runtime=permission,
        )
        first_receipt = execution.execute_batch(
            plan.batches[0],
            tool_context=tool_context("smoke-turn-ask"),
            max_workers=1,
        )[0]
        pending_values = permission.request_queue.pending(session_id="smoke-session")
        if first_receipt.bounded_result.ok or len(pending_values) != 1:
            raise RuntimeError("MCP permission runtime did not ask before execution")
        pending = pending_values[0]
        resolved = permission.resolve(
            PermissionResolutionResponse(
                request_id=pending.request_id,
                session_id=pending.session_id,
                tool_use_id=pending.tool_use_id,
                tool_identity=pending.tool_identity,
                arguments_digest=pending.arguments_digest,
                request_fingerprint=pending.request_fingerprint,
                scope=pending.scope,
                effect=PermissionEffect.ALLOW,
                actor_id="smoke-operator",
                expected_revision=pending.revision,
                channel="test",
                idempotency_key="smoke-exact-approval",
            )
        )
        if not resolved.accepted:
            raise RuntimeError(f"MCP smoke approval failed: {resolved.to_dict()}")
        success = execution.execute_batch(
            plan.batches[0],
            tool_context=tool_context("smoke-turn-approved"),
            max_workers=1,
        )[0].bounded_result
        replay = execution.execute_batch(
            plan.batches[0],
            tool_context=tool_context("smoke-turn-replay"),
            max_workers=1,
        )[0].bounded_result
        fake_state = json.loads(fake_state_path.read_text(encoding="utf-8"))
        remote_tool_calls = fake_state["tool_calls"].get("echo", 0)
        if not success.ok or remote_tool_calls != 1:
            raise RuntimeError(f"MCP tool did not execute exactly once: {success}")
        if replay.ok or replay.error != "permission_required" or remote_tool_calls != 1:
            raise RuntimeError("MCP exact approval was reusable without another decision")

        constraints = runtime.prepare_worker_constraints({}, session_id="smoke-session")
        resource = runtime.read_resource(
            "smoke",
            "memo://live/status",
            run_id="smoke-run",
            task_id="smoke-task",
            node_id="smoke-node",
        )
        prompt = runtime.get_prompt("smoke", "welcome", {"name": "smoke"})
        report = {
            "ok": True,
            "transport": "stdio",
            "server_pid": fake_state["pid"],
            "connection": connected.snapshot.to_dict(),
            "projected_tool": tool_name,
            "permission_denial": denied.error,
            "tool_result": success.summary,
            "remote_tool_calls": remote_tool_calls,
            "grant_replay": replay.error,
            "instruction_delta_count": len(constraints["mcp_instruction_deltas"]),
            "resource_projection_count": len(resource["projections"]),
            "prompt_message_count": len(prompt["messages"]),
            "health": runtime.connection_runtime.health(),
        }
        runtime.connection_runtime.close_all()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
