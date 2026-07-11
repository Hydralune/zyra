from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


from zyra_integrations.mcp.models import (  # noqa: E402
    McpApprovalState,
    McpAuthRecord,
    McpCapabilityKind,
    McpConfigScope,
    McpConnectionSnapshot,
    McpConnectionState,
    McpContent,
    McpContentKind,
    McpElicitationAction,
    McpElicitationField,
    McpElicitationMode,
    McpElicitationRequest,
    McpElicitationResolution,
    McpInstructionsDelta,
    McpInstructionsDeltaAction,
    McpModelError,
    McpPromptDescriptor,
    McpResourceDescriptor,
    McpSamplingRequest,
    McpServerCapabilities,
    McpServerConfig,
    McpTaskOptions,
    McpToolDescriptor,
    McpTransportKind,
)
from zyra_integrations.mcp.protocol import (  # noqa: E402
    JsonRpcError,
    JsonRpcErrorCode,
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcParseError,
    JsonRpcProtocolError,
    JsonRpcRemoteError,
    JsonRpcRequest,
    JsonRpcRequestTimeout,
    JsonRpcSuccessResponse,
    LineJsonRpcCodec,
    NotificationRouter,
    PendingRequestRegistry,
    ServerRequestRouter,
    SseDecoder,
    collect_paginated,
    decode_json_rpc,
    encode_json_rpc,
    parse_json_rpc_message,
)
from zyra_integrations.mcp.transport import (  # noqa: E402
    InProcessExchange,
    InProcessMcpTransport,
    McpTransportHttpError,
    McpTransportState,
    McpTransportWriteError,
    StdioMcpTransport,
    StreamableHttpMcpTransport,
)


def _config(
    kind: McpTransportKind,
    *,
    server_id: str = "test-server",
    command: str = "",
    args: tuple[str, ...] = (),
    url: str = "",
    headers: dict[str, str] | None = None,
    request_timeout: float = 0.5,
    max_response_bytes: int = 1024 * 1024,
) -> McpServerConfig:
    return McpServerConfig(
        server_id=server_id,
        name="Test MCP Server",
        transport=kind,
        scope=McpConfigScope.LOCAL,
        command=command,
        args=args,
        url=url,
        headers=headers or {},
        request_timeout_seconds=request_timeout,
        max_response_bytes=max_response_bytes,
    )


class McpModelContractTests(unittest.TestCase):
    def test_config_validates_transport_and_redacts_secrets(self) -> None:
        config = McpServerConfig(
            server_id="remote",
            name="Remote",
            transport=McpTransportKind.STREAMABLE_HTTP,
            scope=McpConfigScope.PROJECT,
            url="https://example.test/mcp",
            headers={"Authorization": "Bearer secret", "X-Trace": "safe"},
            env={"API_TOKEN": "secret", "PLAIN": "visible"},
            approval=McpApprovalState.APPROVED,
        )

        safe = config.safe_dict()

        self.assertTrue(config.connectable)
        self.assertEqual(safe["headers"]["Authorization"], "<redacted>")
        self.assertEqual(safe["headers"]["X-Trace"], "<redacted>")
        self.assertEqual(safe["env"]["API_TOKEN"], "<redacted>")
        self.assertEqual(safe["env"]["PLAIN"], "visible")
        self.assertNotIn("Bearer secret", json.dumps(safe))
        self.assertTrue(config.signature.startswith("url:"))
        self.assertTrue(config.fingerprint.startswith("sha256:"))
        with self.assertRaises(McpModelError):
            _config(McpTransportKind.STREAMABLE_HTTP, url="https://user:pass@example.test/mcp")
        with self.assertRaises(McpModelError):
            _config(McpTransportKind.STDIO, url="https://example.test", command=sys.executable)

    def test_connection_transition_graph_and_capabilities_are_semantic(self) -> None:
        snapshot = McpConnectionSnapshot("server")
        connecting = snapshot.transition(McpConnectionState.CONNECTING)
        connected = connecting.transition(
            McpConnectionState.CONNECTED,
            protocol_version="2025-06-18",
            session_id="session-1",
        )
        failed = connected.transition(McpConnectionState.FAILED, error_code="connection_lost")

        self.assertTrue(connected.healthy)
        self.assertFalse(failed.healthy)
        self.assertEqual(connected.generation, 1)
        self.assertEqual(failed.revision, 3)
        with self.assertRaises(McpModelError):
            snapshot.transition(McpConnectionState.CONNECTED)
        with self.assertRaises(McpModelError):
            connected.transition(McpConnectionState.FAILED)

        capabilities = McpServerCapabilities.from_initialize_result(
            {
                "instructions": "Use the repository safely.",
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"subscribe": True, "listChanged": True},
                    "prompts": {"listChanged": True},
                    "sampling": {},
                },
            }
        )
        self.assertTrue(capabilities.supports(McpCapabilityKind.TOOLS))
        self.assertTrue(capabilities.supports(McpCapabilityKind.INSTRUCTIONS))
        self.assertTrue(capabilities.tools_list_changed)
        self.assertTrue(capabilities.resources_subscribe)

    def test_projection_and_content_models_reject_ambiguous_wire_data(self) -> None:
        tool = McpToolDescriptor.from_wire(
            "server-a",
            {
                "name": "write/report",
                "description": "Write a report",
                "inputSchema": {"type": "object", "required": ["text"]},
                "annotations": {"destructiveHint": True},
                "execution": {"taskSupport": "required"},
            },
            projection_revision=4,
        )
        prompt = McpPromptDescriptor.from_wire(
            "server-a",
            {
                "name": "review",
                "arguments": [{"name": "topic", "required": True}],
            },
        )
        resource = McpResourceDescriptor.from_wire(
            "server-a",
            {"uri": "mcp://server-a/report/1", "name": "Report", "mimeType": "text/markdown"},
        )
        binary = McpContent.from_wire(
            {"type": "binary", "data": "AAEC/w==", "mimeType": "application/octet-stream"}
        )

        self.assertEqual(tool.identity, "mcp:server-a:write/report")
        self.assertEqual(tool.task_support, "required")
        self.assertEqual(tool.input_schema["properties"], {})
        self.assertEqual(prompt.validate_arguments({"topic": "MCP"}), {"topic": "MCP"})
        self.assertEqual(resource.uri, "mcp://server-a/report/1")
        self.assertEqual(binary.data, b"\x00\x01\x02\xff")
        self.assertNotIn("AAEC", json.dumps(binary.safe_dict()))
        with self.assertRaises(McpModelError):
            McpContent.from_wire({"type": "binary", "data": "not-base64!"})
        with self.assertRaises(McpModelError):
            McpResourceDescriptor("server-a", "file:///safe/../escape", "bad")
        with self.assertRaises(McpModelError):
            prompt.validate_arguments({})

    def test_auth_sampling_elicitation_tasks_and_instructions_do_not_flatten_semantics(self) -> None:
        auth = McpAuthRecord.from_tokens(
            "server-a",
            credential_reference="secret-store://mcp/server-a",
            access_token="access-secret",
            refresh_token="refresh-secret",
            scopes=("tools",),
        )
        serialized_auth = json.dumps(auth.to_dict(), sort_keys=True)
        self.assertNotIn("access-secret", serialized_auth)
        self.assertNotIn("refresh-secret", serialized_auth)
        self.assertIn("sha256:", serialized_auth)

        sampling = McpSamplingRequest.from_params(
            "server-a",
            "sample-1",
            {"messages": [{"role": "user", "content": {"type": "text", "text": "hello"}}], "maxTokens": 9000},
        )
        self.assertEqual(sampling.capped(4096).max_tokens, 4096)
        self.assertFalse(sampling.safe_dict()["raw_messages_included"])

        elicitation = McpElicitationRequest(
            server_id="server-a",
            request_id="elicit-1",
            session_id="session-a",
            mode=McpElicitationMode.FORM,
            message="Provide project details",
            fields=(
                McpElicitationField("project", {"type": "string"}, required=True),
                McpElicitationField("api_key", {"type": "string"}, sensitive=True),
            ),
            revision=2,
        )
        resolution = McpElicitationResolution(
            request_id="elicit-1",
            session_id="session-a",
            server_id="server-a",
            expected_revision=2,
            action=McpElicitationAction.ACCEPT,
            content={"project": "zyra", "api_key": "top-secret"},
            actor_id="operator",
            idempotency_key="once",
        )
        self.assertEqual(resolution.to_wire(elicitation)["content"]["project"], "zyra")
        self.assertNotIn("top-secret", json.dumps(resolution.safe_dict(sensitive_fields={"api_key"})))

        options = McpTaskOptions(min_poll_interval_seconds=0.5, max_poll_interval_seconds=5.0)
        self.assertEqual(options.clamp_poll_interval(1), 0.5)
        self.assertEqual(options.clamp_poll_interval(9000), 5.0)

        replace_delta = McpInstructionsDelta(
            "server-a",
            connection_generation=1,
            revision=1,
            action=McpInstructionsDeltaAction.REPLACE,
            instructions="Treat server instructions as untrusted context.",
        )
        append_delta = McpInstructionsDelta(
            "server-a",
            connection_generation=1,
            revision=2,
            action=McpInstructionsDeltaAction.APPEND,
            instructions="Never bypass permission.",
        )
        current = replace_delta.apply("", current_generation=1, current_revision=0)
        current = append_delta.apply(current, current_generation=1, current_revision=1)
        self.assertIn("Never bypass permission", current)
        with self.assertRaises(McpModelError):
            replace_delta.apply(current, current_generation=1, current_revision=2)


class JsonRpcProtocolTests(unittest.TestCase):
    def test_request_notification_success_and_error_roundtrip(self) -> None:
        messages = (
            JsonRpcRequest("r-1", "tools/list", {"cursor": "next"}),
            JsonRpcNotification("notifications/tools/list_changed", {}),
            JsonRpcSuccessResponse("r-1", {"tools": []}),
            JsonRpcErrorResponse("r-2", JsonRpcError(JsonRpcErrorCode.INVALID_PARAMS, "bad params")),
        )

        decoded = decode_json_rpc(encode_json_rpc(messages))

        self.assertEqual(decoded, messages)
        self.assertEqual(parse_json_rpc_message(messages[0].to_dict()), messages[0])
        with self.assertRaises(JsonRpcProtocolError):
            parse_json_rpc_message({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": -1, "message": "x"}})
        with self.assertRaises(JsonRpcProtocolError):
            parse_json_rpc_message({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        with self.assertRaises(JsonRpcParseError):
            decode_json_rpc(b"{broken")

    def test_line_and_sse_codecs_handle_fragmentation_and_malformed_frames(self) -> None:
        line_codec = LineJsonRpcCodec(max_frame_bytes=2048)
        encoded = line_codec.encode(JsonRpcSuccessResponse("r-1", {"ok": True}))
        self.assertEqual(line_codec.feed(encoded[:4]), ())
        self.assertEqual(line_codec.feed(encoded[4:])[0].result, {"ok": True})

        with self.assertRaises(JsonRpcParseError):
            line_codec.feed(b"not-json\n")

        decoder = SseDecoder(max_event_bytes=2048)
        first = decoder.feed(b"id: event-1\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":")
        second = decoder.feed(b"\"notifications/tools/list_changed\"}\n\n")
        self.assertEqual(first, ())
        self.assertEqual(second[0].event_id, "event-1")
        notification = second[0].json_rpc_messages()[0]
        self.assertIsInstance(notification, JsonRpcNotification)

    def test_pending_request_registry_cleans_timeout_remote_error_and_close(self) -> None:
        registry = PendingRequestRegistry()
        request = JsonRpcRequest("r-1", "ping")
        registry.register(request)
        self.assertTrue(registry.resolve(JsonRpcSuccessResponse("r-1", {"pong": True})))
        self.assertEqual(registry.wait("r-1", 0.1), {"pong": True})

        remote = JsonRpcRequest("r-2", "bad")
        registry.register(remote)
        registry.resolve(JsonRpcErrorResponse("r-2", JsonRpcError(-32000, "remote failed")))
        with self.assertRaises(JsonRpcRemoteError):
            registry.wait("r-2", 0.1)

        timed = JsonRpcRequest("r-3", "hang")
        registry.register(timed)
        with self.assertRaises(JsonRpcRequestTimeout):
            registry.wait("r-3", 0.01)
        self.assertEqual(len(registry), 0)

    def test_notification_and_server_request_routers_isolate_extensions(self) -> None:
        notifications = NotificationRouter()
        observed: list[str] = []
        notifications.add("notice", lambda item: observed.append(item.method))
        notifications.add("notice", lambda item: (_ for _ in ()).throw(RuntimeError("extension failed")))
        dispatch = notifications.dispatch(JsonRpcNotification("notice", {"value": 1}))
        self.assertEqual(observed, ["notice"])
        self.assertEqual(dispatch.delivered_count, 1)
        self.assertEqual(len(dispatch.failures), 1)

        requests = ServerRequestRouter()
        requests.add("sampling/createMessage", lambda params: {"model": "local", "params": params})
        accepted = requests.dispatch(JsonRpcRequest("server-1", "sampling/createMessage", {"maxTokens": 1}))
        missing = requests.dispatch(JsonRpcRequest("server-2", "unknown/request"))
        self.assertTrue(accepted.handled)
        self.assertIsInstance(accepted.response, JsonRpcSuccessResponse)
        self.assertFalse(missing.handled)
        self.assertEqual(missing.response.error.code, JsonRpcErrorCode.METHOD_NOT_FOUND)

    def test_pagination_collects_pages_and_rejects_cursor_cycles(self) -> None:
        cursors: list[str] = []

        def request(cursor: str) -> dict[str, Any]:
            cursors.append(cursor)
            if not cursor:
                return {"tools": [{"name": "one"}], "nextCursor": "page-2"}
            return {"tools": [{"name": "two"}]}

        result = collect_paginated(request, item_key="tools", parse_item=lambda item: item["name"])
        self.assertEqual(result.items, ("one", "two"))
        self.assertEqual(cursors, ["", "page-2"])

        with self.assertRaises(JsonRpcProtocolError):
            collect_paginated(
                lambda cursor: {"tools": [], "nextCursor": "same"},
                item_key="tools",
                parse_item=lambda item: item,
            )


class InProcessTransportTests(unittest.TestCase):
    def test_real_request_notification_server_request_and_start_once(self) -> None:
        opened: list[int] = []
        closed: list[bool] = []
        notifications: list[dict[str, Any]] = []

        def program(message: Any, transport: InProcessMcpTransport) -> Any:
            if isinstance(message, JsonRpcRequest) and message.method == "echo":
                return [
                    JsonRpcNotification("notifications/progress", {"progress": 0.5}),
                    JsonRpcRequest("server-sample", "sampling/createMessage", {"maxTokens": 9}),
                    JsonRpcSuccessResponse(message.request_id, {"echo": message.params}),
                ]
            # The response to the server-initiated request and notifications
            # require no response from the embedded peer.
            return None

        transport = InProcessMcpTransport(
            _config(McpTransportKind.IN_PROCESS),
            program,
            open_hook=lambda item: opened.append(item.snapshot().generation),
            close_hook=lambda item: closed.append(True),
        )
        transport.add_notification_handler(
            "notifications/progress",
            lambda item: notifications.append(dict(item.params or {})),
        )
        transport.add_request_handler(
            "sampling/createMessage",
            lambda params: {"role": "assistant", "content": {"type": "text", "text": "approved"}},
        )

        barrier = threading.Barrier(8)

        def open_once(_: int) -> bool:
            barrier.wait()
            return transport.open()

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(open_once, range(8)))

        result = transport.request("echo", {"value": 7})

        deadline = time.monotonic() + 1
        while not notifications and time.monotonic() < deadline:
            time.sleep(0.005)
        while (
            not any(isinstance(item, JsonRpcErrorResponse | JsonRpcSuccessResponse) for item in transport.sent_messages)
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)

        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(opened, [0])
        self.assertEqual(result, {"echo": {"value": 7}})
        self.assertEqual(notifications, [{"progress": 0.5}])
        sent = transport.sent_messages
        self.assertTrue(any(isinstance(item, JsonRpcErrorResponse | JsonRpcSuccessResponse) for item in sent))
        self.assertEqual(transport.snapshot().request_count, 1)
        self.assertTrue(transport.close())
        self.assertEqual(closed, [True])
        self.assertEqual(transport.state, McpTransportState.CLOSED)

    def test_timeout_cleans_pending_and_close_wakes_waiter(self) -> None:
        messages: list[Any] = []

        def drop(message: Any, transport: InProcessMcpTransport) -> None:
            messages.append(message)
            return None

        transport = InProcessMcpTransport(_config(McpTransportKind.IN_PROCESS, request_timeout=0.03), drop)
        with self.assertRaises(JsonRpcRequestTimeout):
            transport.request("hang")
        self.assertEqual(transport.snapshot().pending_count, 0)
        self.assertTrue(any(isinstance(item, JsonRpcNotification) and item.method == "notifications/cancelled" for item in messages))

        error: list[BaseException] = []

        def wait() -> None:
            try:
                transport.request("hang-again", timeout_seconds=5)
            except BaseException as caught:  # noqa: BLE001 - asserted below.
                error.append(caught)

        thread = threading.Thread(target=wait)
        thread.start()
        deadline = time.monotonic() + 2
        while transport.snapshot().pending_count != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        transport.close()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(error)
        self.assertEqual(transport.snapshot().pending_count, 0)

    def test_delayed_delivery_is_cancelled_on_close_and_malformed_input_is_visible(self) -> None:
        def delayed(message: Any, transport: InProcessMcpTransport) -> Any:
            if isinstance(message, JsonRpcRequest):
                return InProcessExchange(
                    (JsonRpcSuccessResponse(message.request_id, {"late": True}),),
                    delay_seconds=0.5,
                )
            return None

        transport = InProcessMcpTransport(_config(McpTransportKind.IN_PROCESS), delayed)
        failures: list[BaseException] = []
        transport.add_error_handler(failures.append)
        waiter = threading.Thread(target=lambda: self._ignore_failure(lambda: transport.request("late", timeout_seconds=2)))
        waiter.start()
        deadline = time.monotonic() + 1
        while transport.snapshot().pending_count != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        transport.close()
        waiter.join(timeout=2)
        self.assertEqual(transport.snapshot().metadata["delivery_thread_count"], 0)

        transport.open()
        with self.assertRaises(JsonRpcProtocolError):
            transport.accept({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": -1, "message": "bad"}})
        self.assertGreaterEqual(transport.snapshot().protocol_error_count, 1)
        self.assertTrue(failures)
        transport.close()

    @staticmethod
    def _ignore_failure(callback: Any) -> None:
        try:
            callback()
        except BaseException:
            return


_STDIO_SERVER = r"""
import json
import sys
import time

for line in sys.stdin:
    try:
        message = json.loads(line)
    except Exception:
        continue
    method = message.get("method")
    if method == "echo":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message.get("params")}}), flush=True)
    elif method == "notify-me":
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {"revision": 2}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}), flush=True)
    elif method == "stderr":
        print("access_token=super-secret", file=sys.stderr, flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}), flush=True)
    elif method == "malformed":
        print("{broken", flush=True)
    elif method == "hang":
        time.sleep(5)
    elif method == "notifications/cancelled":
        pass
"""


class StdioTransportTests(unittest.TestCase):
    def _transport(self, *, timeout: float = 0.5) -> StdioMcpTransport:
        return StdioMcpTransport(
            _config(
                McpTransportKind.STDIO,
                command=sys.executable,
                args=("-u", "-c", _STDIO_SERVER),
                request_timeout=timeout,
            ),
            shutdown_timeout_seconds=1.0,
        )

    def test_real_stdio_roundtrip_notification_redaction_and_cleanup(self) -> None:
        transport = self._transport()
        notifications: list[Any] = []

        def refresh_from_notification(notification: Any) -> None:
            notifications.append(
                {
                    "notification": notification,
                    "refresh": transport.request("echo", {"source": "list_changed"}),
                }
            )

        transport.add_notification_handler("notifications/tools/list_changed", refresh_from_notification)

        echo = transport.request("echo", {"value": "stdio"})
        notified = transport.request("notify-me")
        transport.request("stderr")
        deadline = time.monotonic() + 1
        while (
            ("<redacted>" not in transport.stderr_tail or not notifications)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        pid = transport.pid

        self.assertEqual(echo, {"echo": {"value": "stdio"}})
        self.assertEqual(notified, {"ok": True})
        self.assertEqual(len(notifications), 1)
        self.assertEqual(
            notifications[0]["refresh"],
            {"echo": {"source": "list_changed"}},
        )
        self.assertIn("<redacted>", transport.stderr_tail)
        self.assertNotIn("super-secret", transport.stderr_tail)
        self.assertTrue(transport.alive)
        transport.close()
        self.assertFalse(transport.alive)
        self.assertIsNotNone(pid)
        self.assertEqual(transport.snapshot().pending_count, 0)

    def test_stdio_malformed_response_fails_pending_without_waiting_for_timeout(self) -> None:
        transport = self._transport(timeout=2.0)
        started = time.monotonic()
        with self.assertRaises(JsonRpcProtocolError):
            transport.request("malformed")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(transport.state, McpTransportState.FAILED)
        self.assertEqual(transport.snapshot().pending_count, 0)
        transport.close()

    def test_stdio_timeout_sends_cancellation_and_process_is_killed_cleanly(self) -> None:
        transport = self._transport(timeout=0.05)
        with self.assertRaises(JsonRpcRequestTimeout):
            transport.request("hang")
        self.assertEqual(transport.snapshot().pending_count, 0)
        transport.close()
        self.assertFalse(transport.alive)


class _McpHttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    requests: list[dict[str, Any]] = []
    delete_count = 0
    get_count = 0

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        with type(self).lock:
            type(self).requests.append({"payload": payload, "headers": dict(self.headers)})
        method = payload.get("method")
        if method == "notify-only":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "unauthorized":
            body = b"authorization=Bearer-super-secret"
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "malformed":
            self._send_bytes(b"{broken", content_type="application/json")
            return
        if method == "slow":
            time.sleep(0.3)
            self._send_json({"jsonrpc": "2.0", "id": payload["id"], "result": {"late": True}})
            return
        if method == "oversized":
            self._send_json({"jsonrpc": "2.0", "id": payload["id"], "result": {"data": "x" * 4096}})
            return
        if method == "sse":
            events = (
                "id: evt-1\n"
                "data: "
                + json.dumps({"jsonrpc": "2.0", "method": "notifications/resources/list_changed", "params": {"revision": 3}})
                + "\n\n"
                "data: "
                + json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"via": "sse"}})
                + "\n\n"
            ).encode("utf-8")
            self._send_bytes(events, content_type="text/event-stream", session=True)
            return
        if method == "tools/list":
            cursor = (payload.get("params") or {}).get("cursor")
            result = (
                {"tools": [{"name": "first"}], "nextCursor": "cursor-2"}
                if not cursor
                else {"tools": [{"name": "second"}]}
            )
            self._send_json({"jsonrpc": "2.0", "id": payload["id"], "result": result}, session=True)
            return
        self._send_json(
            {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"echo": payload.get("params")}},
            session=True,
        )

    def do_DELETE(self) -> None:
        with type(self).lock:
            type(self).delete_count += 1
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        with type(self).lock:
            type(self).get_count += 1
            count = type(self).get_count
        if count > 1:
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = (
            "id: stream-event-1\n"
            "data: "
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                    "params": {"revision": 8},
                }
            )
            + "\n\n"
        ).encode("utf-8")
        self._send_bytes(body, content_type="text/event-stream")

    def _send_json(self, payload: dict[str, Any], *, session: bool = False) -> None:
        self._send_bytes(json.dumps(payload).encode("utf-8"), content_type="application/json", session=session)

    def _send_bytes(self, body: bytes, *, content_type: str, session: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("Mcp-Session-Id", "session-http-1")
        self.end_headers()
        with self._ignore_disconnect():
            self.wfile.write(body)

    class _ignore_disconnect:
        def __enter__(self) -> None:
            return None

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
            return exc_type in {BrokenPipeError, ConnectionResetError, ConnectionAbortedError}

    def log_message(self, format: str, *args: Any) -> None:
        return


class HttpTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        _McpHttpHandler.requests = []
        _McpHttpHandler.delete_count = 0
        _McpHttpHandler.get_count = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _McpHttpHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _transport(
        self,
        *,
        timeout: float = 0.5,
        max_response_bytes: int = 1024 * 1024,
    ) -> StreamableHttpMcpTransport:
        return StreamableHttpMcpTransport(
            _config(
                McpTransportKind.STREAMABLE_HTTP,
                url=self.url,
                headers={"Authorization": "Bearer transport-secret", "X-Zyra-Test": "true"},
                request_timeout=timeout,
                max_response_bytes=max_response_bytes,
            )
        )

    def test_real_http_json_sse_pagination_notification_session_and_delete(self) -> None:
        transport = self._transport()
        resource_changes: list[Any] = []
        transport.add_notification_handler("notifications/resources/list_changed", resource_changes.append)

        echo = transport.request("echo", {"value": "http"})
        transport.notify("notify-only", {"fire": True})
        sse = transport.request("sse")

        def page(cursor: str) -> dict[str, Any]:
            params = {"cursor": cursor} if cursor else {}
            result = transport.request("tools/list", params)
            assert isinstance(result, dict)
            return result

        pages = collect_paginated(page, item_key="tools", parse_item=lambda item: item["name"])

        self.assertEqual(echo, {"echo": {"value": "http"}})
        self.assertEqual(sse, {"via": "sse"})
        self.assertEqual(pages.items, ("first", "second"))
        self.assertEqual(len(resource_changes), 1)
        self.assertEqual(transport.session_id, "session-http-1")
        snapshot_text = json.dumps(transport.snapshot().to_dict(), sort_keys=True)
        self.assertNotIn("transport-secret", snapshot_text)
        self.assertFalse(transport.snapshot().metadata["raw_headers_included"])
        self.assertTrue(transport.close())
        self.assertEqual(_McpHttpHandler.delete_count, 1)

    def test_http_error_body_is_redacted_and_malformed_response_fails_pending(self) -> None:
        transport = self._transport()
        with self.assertRaises(McpTransportHttpError) as raised:
            transport.request("unauthorized")
        self.assertEqual(raised.exception.status, 401)
        self.assertIn("<redacted>", raised.exception.body_preview)
        self.assertNotIn("super-secret", raised.exception.body_preview)

        with self.assertRaises(JsonRpcParseError):
            transport.request("malformed")
        self.assertEqual(transport.snapshot().pending_count, 0)
        transport.close()

    def test_http_event_get_delivers_notification_and_closes_worker(self) -> None:
        transport = StreamableHttpMcpTransport(
            _config(McpTransportKind.STREAMABLE_HTTP, url=self.url),
            listen_for_server_events=True,
            event_retry_seconds=0.02,
        )
        notifications: list[Any] = []
        transport.add_notification_handler(
            "notifications/tools/list_changed",
            notifications.append,
        )

        transport.open()
        deadline = time.monotonic() + 2
        while not notifications and time.monotonic() < deadline:
            time.sleep(0.01)
        transport.close()

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].params["revision"], 8)
        self.assertFalse(transport.snapshot().metadata["event_thread_alive"])
        self.assertFalse(transport.snapshot().metadata["notification_worker_alive"])

    def test_http_timeout_and_oversized_response_cleanup(self) -> None:
        timeout_transport = self._transport(timeout=0.03)
        with self.assertRaises(McpTransportWriteError) as timeout:
            timeout_transport.request("slow")
        self.assertNotIsInstance(timeout.exception, JsonRpcRequestTimeout)
        self.assertEqual(timeout_transport.snapshot().pending_count, 0)
        timeout_transport.close()

        oversized = self._transport(max_response_bytes=1024)
        with self.assertRaises(Exception):
            oversized.request("oversized")
        self.assertEqual(oversized.snapshot().pending_count, 0)
        oversized.close()


if __name__ == "__main__":
    unittest.main()
