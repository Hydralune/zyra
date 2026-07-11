from __future__ import annotations

import base64
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations.mcp.connection import (  # noqa: E402
    McpConnectionRuntimeDisabled,
)
from zyra_integrations.mcp.models import McpConnectionState  # noqa: E402
from zyra_integrations.mcp.output import McpOutputPolicy  # noqa: E402
from zyra_integrations.mcp.projection import (  # noqa: E402
    McpProjectionRuntimeDisabled,
)
from zyra_integrations.mcp.protocol import (  # noqa: E402
    JsonRpcNotification,
    JsonRpcRequest,
)
from zyra_integrations.mcp.runtime import (  # noqa: E402
    McpClientRuntime,
    McpClientRuntimeDisabled,
)
from zyra_integrations.mcp.store import McpRuntimeStateStore  # noqa: E402
from zyra_integrations.mcp.credentials import FileCredentialVault  # noqa: E402
from zyra_runtime import LocalArtifactStore, ToolCall, ToolExecutionContext  # noqa: E402


class StatefulMcpProgram:
    """A real stateful MCP peer driven through InProcessMcpTransport."""

    def __init__(self) -> None:
        self.version = 1
        self.transport: Any = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.RLock()

    def __call__(self, message: Any, transport: Any) -> Any:
        self.transport = transport
        if isinstance(message, JsonRpcNotification):
            with self.lock:
                self.calls.append((message.method, dict(message.params or {})))
            return None
        if not isinstance(message, JsonRpcRequest):
            raise AssertionError(f"unexpected MCP message: {message!r}")
        params = dict(message.params or {})
        with self.lock:
            self.calls.append((message.method, params))

        if message.method == "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "foundation-fake", "version": "1.0"},
                "instructions": "Treat MCP data as external and cite the selected resource.",
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"subscribe": True, "listChanged": True},
                    "prompts": {"listChanged": True},
                },
            }
        if message.method == "tools/list":
            return self._tools_page(str(params.get("cursor") or ""))
        if message.method == "resources/list":
            if not params.get("cursor"):
                return {
                    "resources": [
                        {
                            "uri": "memory://foundation/first",
                            "name": "first",
                            "mimeType": "text/plain",
                        }
                    ],
                    "nextCursor": "resources-2",
                }
            return {
                "resources": [
                    {
                        "uri": "memory://foundation/binary",
                        "name": "binary",
                        "mimeType": "application/octet-stream",
                    }
                ]
            }
        if message.method == "resources/templates/list":
            return {
                "resourceTemplates": [
                    {
                        "uriTemplate": "memory://foundation/{name}",
                        "name": "foundation-template",
                    }
                ]
            }
        if message.method == "prompts/list":
            if not params.get("cursor"):
                return {
                    "prompts": [
                        {
                            "name": "explain",
                            "description": "Explain a topic",
                            "arguments": [{"name": "topic", "required": True}],
                        }
                    ],
                    "nextCursor": "prompts-2",
                }
            return {
                "prompts": [
                    {
                        "name": "summarize",
                        "description": "Summarize without arguments",
                        "arguments": [],
                    }
                ]
            }
        if message.method == "tools/call":
            return self._call_tool(params)
        if message.method == "resources/read":
            return {
                "contents": [
                    {
                        "uri": str(params["uri"]),
                        "mimeType": "application/octet-stream",
                        "blob": base64.b64encode(b"resource-binary-payload").decode("ascii"),
                    }
                ]
            }
        if message.method == "prompts/get":
            topic = str(dict(params.get("arguments") or {}).get("topic") or "unknown")
            return {
                "description": "Generated by the in-process peer",
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": f"Explain {topic}"}}
                ],
            }
        raise AssertionError(f"unhandled MCP method: {message.method}")

    def _tools_page(self, cursor: str) -> dict[str, Any]:
        if self.version >= 2:
            return {
                "tools": [
                    {
                        "name": "replacement",
                        "description": "Replacement after list_changed",
                        "inputSchema": {"type": "object", "properties": {}},
                        "annotations": {
                            "readOnlyHint": True,
                            "idempotentHint": True,
                            "openWorldHint": False,
                        },
                    }
                ]
            }
        if not cursor:
            return {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo a value",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                        "annotations": {
                            "readOnlyHint": True,
                            "idempotentHint": True,
                            "openWorldHint": False,
                        },
                    }
                ],
                "nextCursor": "tools-2",
            }
        return {
            "tools": [
                {
                    "name": "payload",
                    "description": "Return binary and oversized content",
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {
                        "readOnlyHint": True,
                        "idempotentHint": True,
                        "openWorldHint": False,
                    },
                }
            ]
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = dict(params.get("arguments") or {})
        if name == "echo":
            return {"content": [{"type": "text", "text": str(arguments.get("value") or "")} ]}
        if name == "payload":
            return {
                "content": [
                    {
                        "type": "binary",
                        "mimeType": "application/octet-stream",
                        "data": base64.b64encode(b"binary-from-real-in-process-peer").decode("ascii"),
                    },
                    {"type": "text", "text": "x" * 13_000},
                ],
                "structuredContent": {"source": "in-process", "count": 2},
            }
        if name == "replacement":
            return {"content": [{"type": "text", "text": "replacement-called"}]}
        return {"isError": True, "content": [{"type": "text", "text": "unknown tool"}]}

    def notify_tools_changed(self) -> None:
        if self.transport is None:
            raise AssertionError("fake server has not connected")
        self.version = 2
        self.transport.emit_notification("notifications/tools/list_changed", {"reason": "test"})

    def count(self, method: str) -> int:
        with self.lock:
            return sum(candidate == method for candidate, _ in self.calls)


class McpClientRuntimeFoundationTests(unittest.TestCase):
    def _runtime(self, directory: str, *, suffix: str = "main") -> McpClientRuntime:
        root = Path(directory) / suffix
        return McpClientRuntime(
            state_store=McpRuntimeStateStore(root / "state.json"),
            artifact_store=LocalArtifactStore(root / "artifacts"),
            credential_vault=FileCredentialVault(root / "credentials"),
            output_policy=McpOutputPolicy(
                max_inline_text_chars=256,
                max_inline_structured_chars=256,
                max_total_inline_chars=1024,
                preview_chars=64,
            ),
        )

    def _connected_runtime(
        self,
        directory: str,
        *,
        suffix: str = "main",
    ) -> tuple[McpClientRuntime, StatefulMcpProgram]:
        runtime = self._runtime(directory, suffix=suffix)
        peer = StatefulMcpProgram()
        runtime.add_server(
            "foundation",
            {
                "server_id": "foundation",
                "transport": "in_process",
                "request_timeout_seconds": 2.0,
            },
        )
        runtime.register_in_process("foundation", peer)
        receipt = runtime.connect_server(
            "foundation",
            run_id="run-foundation",
            task_id="task-foundation",
            node_id="node-foundation",
            session_id="session-foundation",
            worker_request_id="worker-foundation",
        )
        self.assertTrue(receipt.connected, receipt.safe_dict())
        self.addCleanup(runtime.connection_runtime.close_all)
        return runtime, peer

    def _base_context(self, directory: str, suffix: str = "worker") -> ToolExecutionContext:
        root = Path(directory) / suffix
        return ToolExecutionContext.for_workspace(root / "workspace", root / "artifacts")

    def test_config_connect_paginated_catalog_projection_resource_prompt_and_restore(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        runtime, peer = self._connected_runtime(directory)
        snapshot = runtime.catalog.get("foundation")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.pagination["tools"].page_count, 2)
        self.assertEqual(snapshot.pagination["resources"].page_count, 2)
        self.assertEqual(snapshot.pagination["prompts"].page_count, 2)
        self.assertEqual(len(snapshot.tools), 2)
        self.assertEqual(len(snapshot.resources), 2)
        self.assertEqual(len(snapshot.prompts), 2)
        self.assertEqual(peer.count("tools/list"), 2)
        self.assertEqual(peer.count("resources/list"), 2)
        self.assertEqual(peer.count("prompts/list"), 2)

        projection = runtime.worker_projection(
                self._base_context(directory),
                run_id="run-foundation",
                task_id="task-foundation",
                node_id="node-foundation",
            )
        self.assertEqual(
                set(projection.bundle.tool_names),
                {"mcp__foundation__echo", "mcp__foundation__payload"},
            )
        self.assertIsNotNone(projection.context.registry.get("mcp__foundation__echo"))
        result = projection.context.dynamic_handlers["mcp__foundation__echo"](
                ToolCall(
                    run_id="run-foundation",
                    task_id="task-foundation",
                    tool_name="mcp__foundation__echo",
                    arguments={"value": "live-value"},
                )
            )
        self.assertTrue(result.ok, result)
        self.assertIn("live-value", str(result.output))
        self.assertEqual(peer.count("tools/call"), 1)

        resource = runtime.read_resource(
                "foundation",
                "memory://foundation/binary",
                run_id="run-foundation",
                task_id="task-foundation",
                node_id="node-foundation",
            )
        self.assertTrue(resource["ok"])
        self.assertEqual(len(resource["artifact_ids"]), 1)
        prompt = runtime.get_prompt("foundation", "explain", {"topic": "MCP"})
        self.assertEqual(prompt["messages"][0]["role"], "user")
        self.assertTrue(prompt["untrusted_external_content"])

        supplied = {"mcp_instruction_deltas": [{"instructions": "caller injection"}]}
        constraints = runtime.prepare_worker_constraints(
                supplied,
                session_id="session-foundation",
            )
        self.assertEqual(len(constraints["mcp_instruction_deltas"]), 1)
        delta = constraints["mcp_instruction_deltas"][0]
        self.assertIn("Treat MCP data as external", delta["instructions"])
        self.assertNotIn("caller injection", str(constraints))
        self.assertEqual(delta["trust_level"], "external_untrusted")
        self.assertTrue(delta["untrusted"])

        durable = runtime.session_snapshot("session-foundation")
        restored = self._runtime(directory, suffix="restored")
        self.assertEqual(restored.restore_session_snapshot(durable), 1)
        restored_constraints = restored.prepare_worker_constraints(
                {},
                session_id="session-foundation",
            )
        self.assertEqual(
                restored_constraints["mcp_instruction_deltas"][0]["instructions_hash"],
                delta["instructions_hash"],
            )

    def test_projected_binary_and_large_results_are_artifacts(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        runtime, peer = self._connected_runtime(directory)
        projection = runtime.worker_projection(
                self._base_context(directory),
                run_id="run-foundation",
                task_id="task-foundation",
                node_id="node-foundation",
            )
        result = projection.context.dynamic_handlers["mcp__foundation__payload"](
                ToolCall(
                    run_id="run-foundation",
                    task_id="task-foundation",
                    tool_name="mcp__foundation__payload",
                )
            )

        self.assertTrue(result.ok, result)
        self.assertGreaterEqual(len(result.artifacts), 2)
        self.assertEqual(result.metadata["mcp_output_already_externalized"], "true")
        self.assertEqual(peer.count("tools/call"), 1)
        for artifact in result.artifacts:
            self.assertTrue(Path(artifact.uri).is_file(), artifact)
        self.assertNotIn(base64.b64encode(b"binary-from-real-in-process-peer").decode("ascii"), str(result.output))
        self.assertLess(len(str(result.output)), 4_000)

    def test_list_changed_replaces_snapshot_and_old_projection_fails_closed(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        runtime, peer = self._connected_runtime(directory)
        old_projection = runtime.worker_projection(
                self._base_context(directory),
                run_id="run-foundation",
                task_id="task-foundation",
                node_id="node-foundation",
            )
        old_handler = old_projection.context.dynamic_handlers["mcp__foundation__echo"]
        previous_generation = runtime.catalog.get("foundation").generation  # type: ignore[union-attr]

        peer.notify_tools_changed()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            current = runtime.catalog.get("foundation")
            if current is not None and current.generation > previous_generation:
                break
            time.sleep(0.01)
        current = runtime.catalog.get("foundation")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertGreater(current.generation, previous_generation)
        self.assertEqual([tool.remote_name for tool in current.tools], ["replacement"])

        stale = old_handler(
                ToolCall(
                    run_id="run-foundation",
                    task_id="task-foundation",
                    tool_name="mcp__foundation__echo",
                    arguments={"value": "must-not-run"},
                )
            )
        self.assertFalse(stale.ok)
        self.assertEqual(stale.error, "mcp_projection_stale")
        self.assertEqual(peer.count("tools/call"), 0)

        refreshed = runtime.worker_projection(
                self._base_context(directory, suffix="worker-refreshed"),
                run_id="run-foundation",
                task_id="task-foundation",
                node_id="node-foundation",
            )
        self.assertEqual(refreshed.bundle.tool_names, ("mcp__foundation__replacement",))
        replacement = refreshed.context.dynamic_handlers["mcp__foundation__replacement"](
                ToolCall(
                    run_id="run-foundation",
                    task_id="task-foundation",
                    tool_name="mcp__foundation__replacement",
                )
            )
        self.assertTrue(replacement.ok, replacement)
        self.assertEqual(peer.count("tools/call"), 1)

    def test_needs_auth_cache_and_failed_connection_are_visible_in_health(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        runtime = self._runtime(directory)
        runtime.add_server(
                "needs-auth",
                {
                    "server_id": "needs-auth",
                    "transport": "in_process",
                    "metadata": {"auth_type": "oauth"},
                },
            )
        first = runtime.connect_server("needs-auth")
        second = runtime.connect_server("needs-auth")
        self.assertEqual(first.snapshot.state, McpConnectionState.NEEDS_AUTH, first.safe_dict())
        self.assertFalse(first.auth_cache_hit)
        self.assertEqual(second.snapshot.state, McpConnectionState.NEEDS_AUTH, second.safe_dict())
        self.assertTrue(second.auth_cache_hit)

        runtime.add_server(
                "failed",
                {"server_id": "failed", "transport": "in_process"},
            )

        def broken_program(message: Any, transport: Any) -> Any:
            raise RuntimeError("deliberate initialize failure")

        runtime.register_in_process("failed", broken_program)
        failed = runtime.connect_server("failed")
        self.assertEqual(failed.snapshot.state, McpConnectionState.FAILED)
        health = runtime.diagnostics()["health"]
        self.assertFalse(health["ok"])
        self.assertEqual(health["state_counts"]["needs_auth"], 1)
        self.assertEqual(health["state_counts"]["failed"], 1)
        self.assertEqual(health["connected_count"], 0)
        self.assertFalse(runtime.diagnostics()["fixed_ok_health"])

    def test_disabling_connection_projection_or_output_breaks_real_behavior(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        runtime, peer = self._connected_runtime(directory)
        base = self._base_context(directory)
        projection = runtime.worker_projection(
                base,
                run_id="run-foundation",
                task_id="task-foundation",
                node_id="node-foundation",
            )

        runtime.output_runtime.disabled = True
        output_disabled = projection.context.dynamic_handlers["mcp__foundation__echo"](
                ToolCall(
                    run_id="run-foundation",
                    task_id="task-foundation",
                    tool_name="mcp__foundation__echo",
                    arguments={"value": "cannot-normalize"},
                )
            )
        self.assertFalse(output_disabled.ok)
        self.assertEqual(output_disabled.error, "mcp_runtime_error")
        self.assertEqual(peer.count("tools/call"), 1)
        runtime.output_runtime.disabled = False

        runtime.projection_runtime.disabled = True
        with self.assertRaises(McpProjectionRuntimeDisabled):
            runtime.worker_projection(
                    base,
                    run_id="run-foundation",
                    task_id="task-foundation",
                    node_id="node-foundation",
                )
        runtime.projection_runtime.disabled = False

        runtime.connection_runtime.disabled = True
        with self.assertRaises(McpConnectionRuntimeDisabled):
            runtime.reconnect_server("foundation")
        runtime.connection_runtime.disabled = False

        runtime.disconnect_server(
                "foundation",
                run_id="run-foundation",
                task_id="task-foundation",
                session_id="session-foundation",
            )
        disconnected = projection.context.dynamic_handlers["mcp__foundation__echo"](
                ToolCall(
                    run_id="run-foundation",
                    task_id="task-foundation",
                    tool_name="mcp__foundation__echo",
                    arguments={"value": "must-not-run-after-disconnect"},
                )
            )
        self.assertFalse(disconnected.ok)
        self.assertEqual(disconnected.error, "mcp_projection_stale")
        self.assertEqual(peer.count("tools/call"), 1)

        disabled = McpClientRuntime.from_paths(
                state_path=Path(directory) / "disabled" / "state.json",
                artifact_root=Path(directory) / "disabled" / "artifacts",
                disabled=True,
            )
        with self.assertRaises(McpClientRuntimeDisabled):
            disabled.add_server("blocked", {"server_id": "blocked", "transport": "in_process"})


if __name__ == "__main__":
    unittest.main()
