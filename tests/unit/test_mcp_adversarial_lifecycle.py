from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations.mcp.capabilities import (  # noqa: E402
    McpCapabilityCatalog,
    McpResourcePromptRuntime,
)
from zyra_integrations.mcp.connection import (  # noqa: E402
    MCP_PROTOCOL_VERSION,
    McpConnectionRuntime,
)
from zyra_integrations.mcp.credentials import FileCredentialVault  # noqa: E402
from zyra_integrations.mcp.elicitation import McpElicitationQueue  # noqa: E402
from zyra_integrations.mcp.models import (  # noqa: E402
    McpApprovalState,
    McpConfigScope,
    McpConnectionSnapshot,
    McpConnectionState,
    McpElicitationAction,
    McpElicitationResolution,
    McpElicitationStatus,
    McpServerConfig,
    McpTaskOptions,
    McpTransportKind,
)
from zyra_integrations.mcp.output import McpOutputBudgetRuntime  # noqa: E402
from zyra_integrations.mcp.protocol import (  # noqa: E402
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
)
from zyra_integrations.mcp.runtime import McpClientRuntime, in_process_server_config  # noqa: E402
from zyra_integrations.mcp.sampling import McpSamplingRuntime, SamplingPolicy  # noqa: E402
from zyra_integrations.mcp.store import McpRuntimeStateStore  # noqa: E402
from zyra_integrations.mcp.tasks import McpTaskCancelled  # noqa: E402
from zyra_integrations.mcp.transport import (  # noqa: E402
    InProcessMcpTransport,
    McpTransportReadError,
    StreamableHttpMcpTransport,
)
from zyra_runtime import LocalArtifactStore, ToolExecutionContext  # noqa: E402


def _config(server_id: str, *, transport: McpTransportKind = McpTransportKind.IN_PROCESS) -> McpServerConfig:
    return McpServerConfig(
        server_id=server_id,
        name=server_id,
        transport=transport,
        scope=McpConfigScope.DYNAMIC,
        url="https://mcp.test/rpc" if transport is McpTransportKind.STREAMABLE_HTTP else "",
        approval=McpApprovalState.NOT_REQUIRED,
        connect_timeout_seconds=1.0,
        request_timeout_seconds=2.0,
    )


def _connection(
    directory: str,
    *,
    transport_factory: Any = None,
    state_store: McpRuntimeStateStore | None = None,
    sampling_runtime: McpSamplingRuntime | None = None,
    elicitation_timeout_seconds: float = 0.5,
) -> tuple[McpConnectionRuntime, McpCapabilityCatalog, McpElicitationQueue]:
    catalog = McpCapabilityCatalog()
    output = McpOutputBudgetRuntime(LocalArtifactStore(Path(directory) / "artifacts"))
    resource = McpResourcePromptRuntime(catalog, output)
    queue = McpElicitationQueue(state_store=state_store)
    runtime = McpConnectionRuntime(
        catalog=catalog,
        resource_runtime=resource,
        state_store=state_store,
        elicitation_queue=queue,
        sampling_runtime=sampling_runtime,
        transport_factory=transport_factory,
        elicitation_timeout_seconds=elicitation_timeout_seconds,
    )
    return runtime, catalog, queue


def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true before timeout")


class _BytesResponse:
    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.offset = 0
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self.body) - self.offset
        selected = self.body[self.offset : self.offset + amount]
        self.offset += len(selected)
        return selected

    read1 = read

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_BytesResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class _BlockingSseResponse(_BytesResponse):
    def __init__(
        self,
        request_id: Any,
        response_seen: threading.Event,
        *,
        server_request_id: str,
        server_method: str,
    ) -> None:
        super().__init__(b"", content_type="text/event-stream")
        self.request_id = request_id
        self.response_seen = response_seen
        self.server_request_id = server_request_id
        self.server_method = server_method
        self.index = 0

    def read1(self, _amount: int = -1) -> bytes:
        self.index += 1
        if self.index == 1:
            message = {
                "jsonrpc": "2.0",
                "id": self.server_request_id,
                "method": self.server_method,
                "params": (
                    {"messages": [{"role": "user", "content": "hello"}], "maxTokens": 8}
                    if self.server_method == "sampling/createMessage"
                    else {
                        "requestId": "http-elicit-1",
                        "message": "confirm",
                        "requestedSchema": {"type": "object", "properties": {}},
                    }
                ),
            }
            return f"data: {json.dumps(message)}\n\n".encode()
        if self.index == 2:
            if not self.response_seen.wait(1.5):
                raise TimeoutError("concurrent server-request response POST was not observed")
            result = {"jsonrpc": "2.0", "id": self.request_id, "result": {"roundtrip": True}}
            return f"data: {json.dumps(result)}\n\n".encode()
        return b""

    read = read1


class _ConcurrentSseOpener:
    def __init__(self, server_method: str) -> None:
        self.server_method = server_method
        self.server_request_id = (
            "server-sampling-1"
            if server_method == "sampling/createMessage"
            else "server-elicitation-1"
        )
        self.response_seen = threading.Event()
        self.server_response: Mapping[str, Any] | None = None
        self.lock = threading.Lock()

    def open(self, request: Any, *, timeout: float) -> _BytesResponse:
        payload = json.loads(bytes(request.data).decode())
        if payload.get("method") == "sse-roundtrip":
            return _BlockingSseResponse(
                payload["id"],
                self.response_seen,
                server_request_id=self.server_request_id,
                server_method=self.server_method,
            )
        if payload.get("id") == self.server_request_id and "method" not in payload:
            with self.lock:
                self.server_response = payload
            self.response_seen.set()
            return _BytesResponse(status=202)
        raise AssertionError(f"unexpected HTTP MCP payload: {payload}")


class _NegotiationOpener:
    def __init__(self, negotiated: str) -> None:
        self.negotiated = negotiated
        self.requests: list[tuple[str, dict[str, str]]] = []

    def open(self, request: Any, *, timeout: float) -> _BytesResponse:
        if request.data is None:
            return _BytesResponse(status=200)
        payload = json.loads(bytes(request.data).decode())
        method = str(payload.get("method") or "<response>")
        headers = {str(key).casefold(): str(value) for key, value in request.header_items()}
        self.requests.append((method, headers))
        if method == "initialize":
            result = {
                "protocolVersion": self.negotiated,
                "serverInfo": {"name": "negotiation-peer", "version": "1"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {"tools": []}
        elif "id" not in payload:
            return _BytesResponse(status=202)
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result}).encode()
        return _BytesResponse(body)


class _TaskPeer:
    def __init__(self, generation: int, *, cancel_on_generation: bool = False) -> None:
        self.generation = generation
        self.cancel_on_generation = cancel_on_generation
        self.methods: list[str] = []

    def __call__(self, message: Any, _transport: Any) -> Any:
        if isinstance(message, JsonRpcNotification):
            return None
        if not isinstance(message, JsonRpcRequest):
            return None
        self.methods.append(message.method)
        if message.method == "initialize":
            return {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "serverInfo": {"name": "task-peer", "version": str(self.generation)},
                "capabilities": {"tools": {}},
            }
        if message.method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "long_job",
                        "inputSchema": {"type": "object", "properties": {}},
                        "execution": {"taskSupport": "required"},
                    }
                ]
            }
        if message.method == "tools/call":
            if self.generation != 1:
                raise AssertionError("tools/call was replayed after reconnect")
            return {"task": {"taskId": "remote-task-1", "status": "working", "pollInterval": 1}}
        if message.method == "tasks/get":
            if self.generation == 1:
                raise McpTransportReadError("first carrier lost during poll")
            return {"task": {"taskId": "remote-task-1", "status": "completed", "pollInterval": 1}}
        if message.method == "tasks/result":
            if self.generation == 2:
                raise McpTransportReadError("second carrier lost before result")
            return {"content": [{"type": "text", "text": "finished-on-new-carrier"}]}
        if message.method == "tasks/cancel":
            return {"task": {"taskId": "remote-task-1", "status": "cancelled"}}
        raise AssertionError(message.method)


class _ElicitationPeer:
    def __init__(self) -> None:
        self.sequence = 0
        self.pending: dict[str, Any] = {}
        self.wire_responses: list[Mapping[str, Any]] = []
        self.lock = threading.RLock()

    def __call__(self, message: Any, _transport: Any) -> Any:
        if isinstance(message, JsonRpcNotification):
            return None
        if isinstance(message, JsonRpcSuccessResponse) and str(message.request_id).startswith("server-elicit-"):
            with self.lock:
                self.wire_responses.append(dict(message.result or {}))
                original = self.pending.pop(str(message.request_id))
            return JsonRpcSuccessResponse(original, {"continued": True})
        if not isinstance(message, JsonRpcRequest):
            return None
        if message.method == "initialize":
            return {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "serverInfo": {"name": "elicitation-peer", "version": "1"},
                "capabilities": {},
            }
        if message.method == "probe":
            with self.lock:
                self.sequence += 1
                sequence = self.sequence
                server_request_id = f"server-elicit-{sequence}"
                self.pending[server_request_id] = message.request_id
            return JsonRpcRequest(
                server_request_id,
                "elicitation/create",
                {
                    "requestId": f"control-elicit-{sequence}",
                    "message": "Choose a value",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"choice": {"type": "string"}},
                        "required": ["choice"],
                    },
                },
            )
        raise AssertionError(message.method)


class McpAdversarialLifecycleTests(unittest.TestCase):
    def test_streamable_http_server_request_uses_concurrent_response_post(self) -> None:
        cases = {
            "sampling/createMessage": lambda _params: {
                "role": "assistant",
                "content": {"type": "text", "text": "sampled"},
            },
            "elicitation/create": lambda _params: {"action": "cancel"},
        }
        for server_method, handler in cases.items():
            with self.subTest(server_method=server_method):
                opener = _ConcurrentSseOpener(server_method)
                transport = StreamableHttpMcpTransport(
                    _config(f"http-{server_method}", transport=McpTransportKind.STREAMABLE_HTTP),
                    opener=opener,  # type: ignore[arg-type]
                    terminate_session=False,
                )
                transport.add_request_handler(server_method, handler)
                result: dict[str, Any] = {}
                failure: list[BaseException] = []

                def invoke() -> None:
                    try:
                        result.update(transport.request("sse-roundtrip"))  # type: ignore[arg-type]
                    except BaseException as error:  # noqa: BLE001 - assertion captures worker failures.
                        failure.append(error)

                thread = threading.Thread(target=invoke, daemon=True)
                thread.start()
                thread.join(timeout=2.0)
                transport.close()

                self.assertFalse(thread.is_alive(), "HTTP POST/SSE server request deadlocked the response POST")
                self.assertFalse(failure, failure)
                self.assertEqual(result, {"roundtrip": True})
                self.assertIsNotNone(opener.server_response)
                assert opener.server_response is not None
                self.assertEqual(opener.server_response["id"], opener.server_request_id)
                if server_method == "sampling/createMessage":
                    self.assertEqual(opener.server_response["result"]["role"], "assistant")
                else:
                    self.assertEqual(opener.server_response["result"], {"action": "cancel"})

    def test_negotiated_version_updates_http_headers_and_unknown_version_fails(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        opener = _NegotiationOpener("2025-03-26")

        def factory(config: McpServerConfig) -> StreamableHttpMcpTransport:
            return StreamableHttpMcpTransport(
                config,
                opener=opener,  # type: ignore[arg-type]
                terminate_session=False,
            )

        runtime, catalog, _ = _connection(directory, transport_factory=factory)
        receipt = runtime.connect(_config("http-version", transport=McpTransportKind.STREAMABLE_HTTP))
        self.addCleanup(runtime.close_all)

        self.assertTrue(receipt.connected, receipt.safe_dict())
        self.assertEqual(receipt.snapshot.protocol_version, "2025-03-26")
        self.assertEqual(opener.requests[0][0], "initialize")
        self.assertEqual(opener.requests[0][1]["Mcp-protocol-version".casefold()], MCP_PROTOCOL_VERSION)
        for method, headers in opener.requests[1:]:
            with self.subTest(method=method):
                self.assertEqual(headers["Mcp-protocol-version".casefold()], "2025-03-26")

        def unknown_peer(message: Any, _transport: Any) -> Any:
            if isinstance(message, JsonRpcNotification):
                return None
            if isinstance(message, JsonRpcRequest) and message.method == "initialize":
                return {"protocolVersion": "2099-01-01", "serverInfo": {}, "capabilities": {"tools": {}}}
            raise AssertionError(getattr(message, "method", message))

        unknown_runtime, unknown_catalog, _ = _connection(directory)
        unknown_runtime.register_in_process("unknown-version", unknown_peer)
        failed = unknown_runtime.connect(_config("unknown-version"))
        self.addCleanup(unknown_runtime.close_all)
        self.assertIs(failed.snapshot.state, McpConnectionState.FAILED)
        self.assertIn("unsupported protocolVersion", failed.snapshot.error_message)
        self.assertIsNone(unknown_catalog.get("unknown-version"))

    def test_client_capabilities_are_truthful_and_sampling_appears_after_callback_reconnect(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        sampling = McpSamplingRuntime(SamplingPolicy(enabled=True))
        seen: list[Mapping[str, Any]] = []

        def peer(message: Any, _transport: Any) -> Any:
            if isinstance(message, JsonRpcNotification):
                return None
            assert isinstance(message, JsonRpcRequest)
            if message.method == "initialize":
                seen.append(dict(message.params["capabilities"]))
                return {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {"name": "caps", "version": "1"},
                    "capabilities": {"tools": {}},
                }
            if message.method == "tools/list":
                return {"tools": []}
            raise AssertionError(message.method)

        runtime, _, _ = _connection(directory, sampling_runtime=sampling)
        runtime.register_in_process("caps", peer)
        first = runtime.connect(_config("caps"))
        self.assertTrue(first.connected)
        self.assertNotIn("roots", seen[0])
        self.assertEqual(seen[0]["elicitation"], {})
        self.assertNotIn("form", seen[0]["elicitation"])
        self.assertNotIn("url", seen[0]["elicitation"])
        self.assertNotIn("sampling", seen[0])

        sampling.register_callback("caps", lambda _request, _context: {"content": "ok", "model": "test"})
        second = runtime.reconnect("caps")
        self.addCleanup(runtime.close_all)
        self.assertTrue(second.connected)
        self.assertEqual(seen[1]["sampling"], {})
        self.assertNotIn("roots", seen[1])

        # The product facade starts with sampling disabled.  Registering a
        # callback is the explicit operator opt-in and must make the truthful
        # capability advertisable for the next connection handshake.
        facade = McpClientRuntime.from_paths(
            state_path=Path(directory) / "facade-state.json",
            artifact_root=Path(directory) / "facade-artifacts",
        )
        self.assertFalse(facade.sampling_runtime.policy.enabled)
        facade.register_sampling_callback(
            "facade-caps",
            lambda _request, _context: {"content": "ok", "model": "test"},
        )
        self.assertTrue(facade.sampling_runtime.policy.enabled)
        self.assertTrue(facade.sampling_runtime.advertised("facade-caps"))

    def test_long_task_poll_result_and_cancel_follow_replaced_transports(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        peers: list[_TaskPeer] = []

        def factory(config: McpServerConfig) -> InProcessMcpTransport:
            peer = _TaskPeer(len(peers) + 1)
            peers.append(peer)
            return InProcessMcpTransport(config, peer)

        runtime, _, _ = _connection(directory, transport_factory=factory)
        runtime.task_runtime.options = McpTaskOptions(
            min_poll_interval_seconds=0.001,
            max_poll_interval_seconds=0.002,
        )
        self.assertTrue(runtime.connect(_config("task-swap")).connected)
        result = runtime.call_tool("task-swap", "long_job", {})
        self.addCleanup(runtime.close_all)

        self.assertEqual(len(peers), 3)
        self.assertEqual(sum(peer.methods.count("tools/call") for peer in peers), 1)
        self.assertIn("tasks/get", peers[0].methods)
        self.assertIn("tasks/get", peers[1].methods)
        self.assertIn("tasks/result", peers[1].methods)
        self.assertIn("tasks/result", peers[2].methods)
        self.assertEqual(result["_meta"]["zyraMcp"]["taskPollCount"], 1)
        self.assertEqual(result["content"][0]["text"], "finished-on-new-carrier")

        cancel_peers: list[_TaskPeer] = []
        holder: dict[str, McpConnectionRuntime] = {}

        def cancel_factory(config: McpServerConfig) -> InProcessMcpTransport:
            peer = _TaskPeer(len(cancel_peers) + 1)
            cancel_peers.append(peer)
            if len(cancel_peers) == 2:
                holder["runtime"].task_runtime.cancel("task-cancel", "remote-task-1")
            return InProcessMcpTransport(config, peer)

        cancel_runtime, _, _ = _connection(directory, transport_factory=cancel_factory)
        holder["runtime"] = cancel_runtime
        cancel_runtime.task_runtime.options = McpTaskOptions(
            min_poll_interval_seconds=0.001,
            max_poll_interval_seconds=0.002,
        )
        self.assertTrue(cancel_runtime.connect(_config("task-cancel")).connected)
        with self.assertRaises(McpTaskCancelled):
            cancel_runtime.call_tool("task-cancel", "long_job", {})
        self.addCleanup(cancel_runtime.close_all)
        self.assertEqual(sum(peer.methods.count("tools/call") for peer in cancel_peers), 1)
        self.assertNotIn("tasks/cancel", cancel_peers[0].methods)
        self.assertIn("tasks/cancel", cancel_peers[1].methods)

    def test_durable_health_hydrates_failed_and_connected_as_reconnect_required(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        state_path = Path(directory) / "state.json"
        writer = McpRuntimeStateStore(state_path)
        writer.set_connection(
            "failed-server",
            McpConnectionSnapshot(
                server_id="failed-server",
                state=McpConnectionState.FAILED,
                revision=3,
                generation=1,
                error_code="carrier_lost",
                error_message="persisted failure",
            ),
        )
        writer.set_connection(
            "was-connected",
            McpConnectionSnapshot(
                server_id="was-connected",
                state=McpConnectionState.CONNECTED,
                revision=4,
                generation=2,
                protocol_version=MCP_PROTOCOL_VERSION,
            ),
        )

        runtime = McpClientRuntime.from_paths(
            state_path=state_path,
            artifact_root=Path(directory) / "runtime-artifacts",
            credential_root=Path(directory) / "credentials",
        )
        self.addCleanup(runtime.connection_runtime.close_all)
        health = runtime.connection_runtime.health()

        self.assertEqual(health["server_count"], 2)
        self.assertEqual(health["connected_count"], 0)
        self.assertFalse(health["ok"])
        self.assertIs(runtime.connection_runtime.snapshot("failed-server").state, McpConnectionState.FAILED)
        restored = runtime.connection_runtime.snapshot("was-connected")
        self.assertIs(restored.state, McpConnectionState.RECONNECTING)
        self.assertEqual(restored.metadata["restore_reason"], "live_transport_not_restorable")
        self.assertEqual(runtime.catalog.safe_dict()["tool_count"], 0)

    def test_reconnect_and_terminal_loss_withdraw_catalog_before_worker_projection(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())

        def normal_peer(message: Any, _transport: Any) -> Any:
            if isinstance(message, JsonRpcNotification):
                return None
            assert isinstance(message, JsonRpcRequest)
            if message.method == "initialize":
                return {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {"name": "loss-peer", "version": "1"},
                    "capabilities": {"tools": {}},
                }
            if message.method == "tools/list":
                return {"tools": [{"name": "old_tool", "inputSchema": {"type": "object"}}]}
            if message.method == "tools/call":
                return {"content": [{"type": "text", "text": "old"}]}
            raise AssertionError(message.method)

        runtime = McpClientRuntime.from_paths(
            state_path=Path(directory) / "runtime-state.json",
            artifact_root=Path(directory) / "runtime-artifacts",
            credential_root=Path(directory) / "runtime-credentials",
        )
        runtime.register_in_process("loss", normal_peer)
        runtime.add_server("loss", in_process_server_config("loss"))
        self.assertTrue(runtime.connect_server("loss").connected)
        base = ToolExecutionContext.for_workspace(
            Path(directory) / "workspace-before",
            Path(directory) / "tool-artifacts-before",
        )
        before = runtime.worker_projection(base, run_id="run-loss", task_id="task-loss", node_id="node-loss")
        self.assertIn("mcp__loss__old_tool", before.bundle.tool_names)

        started = threading.Event()
        release = threading.Event()

        def blocking_peer(message: Any, transport: Any) -> Any:
            if isinstance(message, JsonRpcRequest) and message.method == "initialize":
                started.set()
                if not release.wait(1.5):
                    raise TimeoutError("test did not release reconnect initialize")
            return normal_peer(message, transport)

        runtime.register_in_process("loss", blocking_peer, replace_existing=True)
        reconnect_result: list[Any] = []
        reconnect_thread = threading.Thread(
            target=lambda: reconnect_result.append(runtime.reconnect_server("loss")),
            daemon=True,
        )
        reconnect_thread.start()
        self.assertTrue(started.wait(1.0))
        self.assertIsNone(runtime.catalog.get("loss"))
        during = runtime.worker_projection(
            ToolExecutionContext.for_workspace(
                Path(directory) / "workspace-during",
                Path(directory) / "tool-artifacts-during",
            ),
            run_id="run-loss",
            task_id="task-loss",
            node_id="node-loss",
        )
        self.assertNotIn("mcp__loss__old_tool", during.bundle.tool_names)
        release.set()
        reconnect_thread.join(timeout=2.0)
        self.assertFalse(reconnect_thread.is_alive())
        self.assertTrue(reconnect_result[0].connected)

        handle = runtime.connection_runtime._handle("loss")  # noqa: SLF001 - terminal carrier regression.
        assert isinstance(handle.transport, InProcessMcpTransport)
        handle.transport._handle_read_error(  # noqa: SLF001 - simulate a carrier EOF.
            McpTransportReadError("carrier disappeared"),
            terminal=True,
        )
        _wait_until(
            lambda: runtime.connection_runtime.snapshot("loss").state is McpConnectionState.FAILED
        )
        self.addCleanup(runtime.connection_runtime.close_all)
        self.assertIsNone(runtime.catalog.get("loss"))
        self.assertNotIn("loss", runtime.state_store.read_state()["catalogs"])
        after = runtime.worker_projection(
            ToolExecutionContext.for_workspace(
                Path(directory) / "workspace-after",
                Path(directory) / "tool-artifacts-after",
            ),
            run_id="run-loss",
            task_id="task-loss",
            node_id="node-loss",
        )
        self.assertNotIn("mcp__loss__old_tool", after.bundle.tool_names)

    def test_elicitation_waits_for_exact_accept_cancel_and_bounded_timeout(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        peer = _ElicitationPeer()
        runtime, _, queue = _connection(directory, elicitation_timeout_seconds=0.15)
        runtime.register_in_process("elicit", peer)
        self.assertTrue(runtime.connect(_config("elicit"), session_id="session-elicit").connected)
        handle = runtime._connected_handle("elicit")  # noqa: SLF001 - exercises installed server handlers.
        assert handle.transport is not None

        def probe_and_resolve(action: McpElicitationAction, sequence: int) -> Mapping[str, Any]:
            result: dict[str, Any] = {}
            failures: list[BaseException] = []

            def invoke() -> None:
                try:
                    result.update(handle.transport.request("probe", {"sequence": sequence}, timeout_seconds=1.0))
                except BaseException as error:  # noqa: BLE001
                    failures.append(error)

            thread = threading.Thread(target=invoke, daemon=True)
            thread.start()
            _wait_until(lambda: queue.get("elicit", "session-elicit", f"control-elicit-{sequence}") is not None)
            record = queue.get("elicit", "session-elicit", f"control-elicit-{sequence}")
            assert record is not None
            queue.resolve(
                McpElicitationResolution(
                    request_id=record.request.request_id,
                    session_id=record.request.session_id,
                    server_id=record.request.server_id,
                    expected_revision=record.revision,
                    action=action,
                    content={"choice": "accepted"} if action is McpElicitationAction.ACCEPT else {},
                    actor_id="operator-1",
                    idempotency_key=f"resolution-{sequence}",
                )
            )
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures, failures)
            return result

        self.assertEqual(probe_and_resolve(McpElicitationAction.ACCEPT, 1), {"continued": True})
        self.assertEqual(peer.wire_responses[0], {"action": "accept", "content": {"choice": "accepted"}})
        self.assertEqual(probe_and_resolve(McpElicitationAction.CANCEL, 2), {"continued": True})
        self.assertEqual(peer.wire_responses[1], {"action": "cancel"})

        timeout_result: dict[str, Any] = {}
        timeout_thread = threading.Thread(
            target=lambda: timeout_result.update(
                handle.transport.request("probe", {"sequence": 3}, timeout_seconds=1.0)
            ),
            daemon=True,
        )
        timeout_thread.start()
        timeout_thread.join(timeout=1.0)
        self.addCleanup(runtime.close_all)
        self.assertFalse(timeout_thread.is_alive())
        self.assertEqual(timeout_result, {"continued": True})
        self.assertEqual(peer.wire_responses[2], {"action": "cancel"})
        timed_out = queue.get("elicit", "session-elicit", "control-elicit-3")
        assert timed_out is not None
        self.assertIs(timed_out.status, McpElicitationStatus.CANCELLED)
        self.assertEqual(timed_out.terminal_reason, "bounded control response timeout")


if __name__ == "__main__":
    unittest.main()
