from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations.mcp.credentials import FileCredentialVault  # noqa: E402
from zyra_integrations.mcp.elicitation import (  # noqa: E402
    McpElicitationConflict,
    McpElicitationQueue,
)
from zyra_integrations.mcp.models import (  # noqa: E402
    McpElicitationAction,
    McpElicitationField,
    McpElicitationMode,
    McpElicitationRequest,
    McpElicitationResolution,
    McpElicitationStatus,
    McpTaskOptions,
    McpTaskSnapshot,
    McpTaskStatus,
)
from zyra_integrations.mcp.protocol import (  # noqa: E402
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
)
from zyra_integrations.mcp.runtime import McpClientRuntime  # noqa: E402
from zyra_integrations.mcp.store import McpRuntimeStateStore  # noqa: E402
from zyra_integrations.mcp.tasks import (  # noqa: E402
    McpTaskCancelled,
    McpTaskLifecycleRuntime,
    McpTaskOutcomeUnknown,
)
from zyra_runtime import LocalArtifactStore  # noqa: E402


class StatefulTaskPort:
    """Stateful request port that executes the task protocol, not canned mocks."""

    def __init__(
        self,
        *,
        task_id: str = "remote-task-1",
        fail_first_poll: bool = False,
        fail_create: bool = False,
    ) -> None:
        self.task_id = task_id
        self.fail_first_poll = fail_first_poll
        self.fail_create = fail_create
        self.polls = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cancelled = False

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        selected = dict(params or {})
        self.calls.append((method, selected))
        if method == "tools/call":
            if self.fail_create:
                raise ConnectionError("connection dropped after create write")
            return {
                "task": {
                    "taskId": self.task_id,
                    "status": "working",
                    "pollInterval": 1,
                }
            }
        if method == "tasks/get":
            self.polls += 1
            if self.fail_first_poll and self.polls == 1:
                raise ConnectionError("transient poll disconnect")
            status = "completed" if self.polls >= (2 if not self.fail_first_poll else 2) else "working"
            return {
                "taskId": self.task_id,
                "status": status,
                "pollInterval": 1,
            }
        if method == "tasks/result":
            return {
                "content": [{"type": "text", "text": f"result:{self.task_id}"}],
                "isError": False,
            }
        if method == "tasks/cancel":
            self.cancelled = True
            return {"taskId": self.task_id, "status": "cancelled"}
        raise AssertionError(f"unexpected task method {method!r}")

    def count(self, method: str) -> int:
        return sum(candidate == method for candidate, _ in self.calls)


class MutableUtcClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class SamplingServerProgram:
    """In-process MCP peer that sends an unsolicited server sampling request."""

    def __init__(self) -> None:
        self.transport: Any = None
        self.initialize_params: dict[str, Any] = {}
        self.server_responses: list[JsonRpcSuccessResponse | JsonRpcErrorResponse] = []
        self.lock = threading.RLock()

    def __call__(self, message: Any, transport: Any) -> Any:
        self.transport = transport
        if isinstance(message, JsonRpcRequest):
            if message.method != "initialize":
                raise AssertionError(f"unexpected client request {message.method!r}")
            self.initialize_params = dict(message.params or {})
            return {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "sampling-control-peer", "version": "1"},
                "capabilities": {},
            }
        if isinstance(message, JsonRpcNotification):
            return None
        if isinstance(message, (JsonRpcSuccessResponse, JsonRpcErrorResponse)):
            with self.lock:
                self.server_responses.append(message)
            return None
        raise AssertionError(f"unexpected transport message {message!r}")

    def request_sampling(self) -> None:
        if self.transport is None:
            raise AssertionError("sampling peer is not connected")
        self.transport.emit(
            JsonRpcRequest(
                "sampling-request-1",
                "sampling/createMessage",
                {
                    "requestId": "sampling-request-1",
                    "sessionId": "session-sampling",
                    "messages": [
                        {"role": "user", "content": {"type": "text", "text": "consume tokens"}}
                    ],
                    "maxTokens": 128,
                },
            )
        )

    def response(self) -> JsonRpcSuccessResponse | JsonRpcErrorResponse | None:
        with self.lock:
            return self.server_responses[-1] if self.server_responses else None


class McpTaskLifecycleTests(unittest.TestCase):
    @staticmethod
    def _runtime(
        *,
        reconnect: Any = None,
        state_store: Any = None,
    ) -> McpTaskLifecycleRuntime:
        return McpTaskLifecycleRuntime(
            options=McpTaskOptions(
                default_ttl_ms=30_000,
                max_wait_seconds=5,
                min_poll_interval_seconds=0.001,
                max_poll_interval_seconds=0.002,
            ),
            state_store=state_store,
            reconnect=reconnect,
            sleeper=lambda seconds: None,
        )

    def test_create_poll_result_persists_remote_task_identity(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        state = McpRuntimeStateStore(Path(directory) / "state.json")
        port = StatefulTaskPort()
        receipt = self._runtime(state_store=state).execute_tool(
            request=port,
            server_id="task-server",
            tool_name="long-operation",
            arguments={"input": "real"},
            use_task=True,
            run_id="run-task",
            task_id="task-task",
        )

        self.assertEqual(receipt.task_id, "remote-task-1")
        self.assertEqual(receipt.tool_call_count, 1)
        self.assertEqual(receipt.poll_count, 2)
        self.assertEqual(receipt.reconnect_count, 0)
        self.assertEqual(port.count("tools/call"), 1)
        self.assertEqual(port.count("tasks/get"), 2)
        self.assertEqual(port.count("tasks/result"), 1)
        self.assertIn("result:remote-task-1", str(receipt.result))
        persisted = state.get("tasks.task-server.remote-task-1")
        self.assertEqual(persisted["status"], "completed")
        create_params = port.calls[0][1]
        self.assertEqual(create_params["task"]["ttl"], 30_000)

    def test_cancel_is_forwarded_to_remote_before_any_poll(self) -> None:
        port = StatefulTaskPort(task_id="remote-cancel")
        runtime = self._runtime()
        runtime.cancel("task-server", "remote-cancel")

        with self.assertRaises(McpTaskCancelled):
            runtime.execute_tool(
                request=port,
                server_id="task-server",
                tool_name="destructive-operation",
                arguments={},
                use_task=True,
            )

        self.assertTrue(port.cancelled)
        self.assertEqual(port.count("tools/call"), 1)
        self.assertEqual(port.count("tasks/cancel"), 1)
        self.assertEqual(port.count("tasks/get"), 0)

    def test_known_task_reconnects_and_resume_never_reissues_remote_tools_call(self) -> None:
        reconnects: list[str] = []
        port = StatefulTaskPort(task_id="remote-reconnect", fail_first_poll=True)
        receipt = self._runtime(reconnect=reconnects.append).execute_tool(
            request=port,
            server_id="task-server",
            tool_name="long-operation",
            arguments={"once": True},
            use_task=True,
        )

        self.assertEqual(reconnects, ["task-server"])
        self.assertEqual(receipt.reconnect_count, 1)
        self.assertEqual(port.count("tools/call"), 1)
        self.assertEqual(port.count("tasks/get"), 2)

        resume_port = StatefulTaskPort(task_id="known-task")
        known = McpTaskSnapshot(
            server_id="task-server",
            task_id="known-task",
            tool_name="long-operation",
            status=McpTaskStatus.WORKING,
            revision=4,
            poll_interval_ms=1,
        )
        resumed = self._runtime().resume(request=resume_port, snapshot=known)
        self.assertEqual(resumed.task_id, "known-task")
        self.assertEqual(resumed.tool_call_count, 0)
        self.assertEqual(resume_port.count("tools/call"), 0)
        self.assertGreaterEqual(resume_port.count("tasks/get"), 1)
        self.assertEqual(resume_port.count("tasks/result"), 1)

    def test_unknown_create_outcome_is_never_replayed_or_reconnected(self) -> None:
        reconnects: list[str] = []
        port = StatefulTaskPort(fail_create=True)
        with self.assertRaises(McpTaskOutcomeUnknown):
            self._runtime(reconnect=reconnects.append).execute_tool(
                request=port,
                server_id="task-server",
                tool_name="side-effecting-create",
                arguments={"value": 1},
                use_task=True,
            )

        self.assertEqual(port.count("tools/call"), 1)
        self.assertEqual(port.count("tasks/get"), 0)
        self.assertEqual(reconnects, [])


class McpElicitationQueueTests(unittest.TestCase):
    @staticmethod
    def _request(
        request_id: str,
        *,
        server_id: str = "elicitation-server",
        expires_at: str = "",
    ) -> McpElicitationRequest:
        return McpElicitationRequest(
            server_id=server_id,
            request_id=request_id,
            session_id="elicitation-session",
            mode=McpElicitationMode.FORM,
            message="Provide deployment information",
            fields=(
                McpElicitationField("region", {"type": "string"}, required=True),
                McpElicitationField("replicas", {"type": "integer"}, required=True),
                McpElicitationField("api_token", {"type": "string"}, required=True, sensitive=True),
            ),
            expires_at=expires_at,
            revision=1,
        )

    def test_schema_sensitive_sink_and_idempotent_resolution(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        state_path = Path(directory) / "elicitation-state.json"
        state = McpRuntimeStateStore(state_path)
        secrets: list[tuple[str, str, Any]] = []
        outbound: list[dict[str, Any]] = []

        def sensitive_sink(server_id: str, name: str, value: Any) -> str:
            secrets.append((server_id, name, value))
            return "credential://elicitation/token-1"

        queue = McpElicitationQueue(
            state_store=state,
            sensitive_sink=sensitive_sink,
            response_sink=lambda request, value: outbound.append(dict(value)),
        )
        record = queue.enqueue(self._request("request-1"))

        invalid = McpElicitationResolution(
            request_id="request-1",
            session_id="elicitation-session",
            server_id="elicitation-server",
            expected_revision=record.revision,
            action=McpElicitationAction.ACCEPT,
            content={"region": "cn-east", "replicas": "three", "api_token": "not-stored"},
            actor_id="operator-1",
            idempotency_key="resolution-invalid",
        )
        with self.assertRaises(McpElicitationConflict):
            queue.resolve(invalid)
        self.assertEqual(secrets, [])
        self.assertEqual(queue.get("elicitation-server", "elicitation-session", "request-1").status, McpElicitationStatus.PENDING)  # type: ignore[union-attr]

        accepted = McpElicitationResolution(
            request_id="request-1",
            session_id="elicitation-session",
            server_id="elicitation-server",
            expected_revision=record.revision,
            action=McpElicitationAction.ACCEPT,
            content={"region": "cn-east", "replicas": 3, "api_token": "raw-secret-token"},
            actor_id="operator-1",
            idempotency_key="resolution-1",
        )
        first = queue.resolve(accepted)
        second = queue.resolve(accepted)

        self.assertEqual(first.record.status, McpElicitationStatus.RESOLVED)
        self.assertEqual(second.wire_response, first.wire_response)
        self.assertEqual(len(secrets), 1)
        self.assertEqual(secrets[0], ("elicitation-server", "api_token", "raw-secret-token"))
        self.assertEqual(len(outbound), 1)
        self.assertEqual(
            first.wire_response["content"]["api_token"],  # type: ignore[index]
            {"credentialReference": "credential://elicitation/token-1"},
        )
        self.assertEqual(first.wire_response["content"]["replicas"], 3)  # type: ignore[index]
        self.assertEqual(first.safe_dict()["wire_response"]["content"], "<redacted>")  # type: ignore[index]
        persisted = state_path.read_text(encoding="utf-8")
        self.assertNotIn("raw-secret-token", persisted)
        self.assertNotIn("not-stored", persisted)
        self.assertNotIn("raw-secret-token", json.dumps(first.record.safe_dict()))

    def test_idempotency_collision_cannot_cross_request_waiters_and_exact_retry_replays(self) -> None:
        credential_references = {
            "secret-r1": "credential://elicitation/r1",
            "secret-r2": "credential://elicitation/r2",
        }
        queue = McpElicitationQueue(
            sensitive_sink=lambda _server, _name, value: credential_references[str(value)],
        )
        r1 = queue.enqueue(self._request("request-r1"))
        r2 = queue.enqueue(self._request("request-r2"))
        waiter_results: dict[str, Mapping[str, Any]] = {}
        waiter_errors: list[BaseException] = []

        def wait(name: str, request_id: str) -> None:
            try:
                waiter_results[name] = queue.wait_for_response(
                    server_id="elicitation-server",
                    session_id="elicitation-session",
                    request_id=request_id,
                    timeout_seconds=2.0,
                )
            except BaseException as error:  # pragma: no cover - asserted below
                waiter_errors.append(error)

        wait_r1 = threading.Thread(target=wait, args=("r1", "request-r1"), daemon=True)
        wait_r2 = threading.Thread(target=wait, args=("r2", "request-r2"), daemon=True)
        wait_r1.start()
        wait_r2.start()
        time.sleep(0.02)
        self.assertTrue(wait_r1.is_alive())
        self.assertTrue(wait_r2.is_alive())

        content_r1 = {"region": "edge", "replicas": 1, "api_token": "secret-r1"}
        first_resolution = McpElicitationResolution(
            request_id="request-r1",
            session_id="elicitation-session",
            server_id="elicitation-server",
            expected_revision=r1.revision,
            action=McpElicitationAction.ACCEPT,
            content=content_r1,
            actor_id="operator-1",
            idempotency_key="shared-resolution-key",
        )
        first = queue.resolve(first_resolution)
        wait_r1.join(timeout=1.0)
        self.assertFalse(wait_r1.is_alive())
        self.assertEqual(waiter_results["r1"], first.wire_response)

        # This is the original failure mode: a second live waiter must not
        # receive r1's response merely because its caller reused the key.
        collision = McpElicitationResolution(
            request_id="request-r2",
            session_id="elicitation-session",
            server_id="elicitation-server",
            expected_revision=r2.revision,
            action=McpElicitationAction.ACCEPT,
            content={"region": "cloud", "replicas": 2, "api_token": "secret-r2"},
            actor_id="operator-1",
            idempotency_key="shared-resolution-key",
        )
        with self.assertRaisesRegex(McpElicitationConflict, "idempotency key"):
            queue.resolve(collision)
        self.assertEqual(
            queue.get("elicitation-server", "elicitation-session", "request-r2").status,  # type: ignore[union-attr]
            McpElicitationStatus.PENDING,
        )
        self.assertTrue(wait_r2.is_alive())

        # Canonical object-key ordering is irrelevant for an otherwise exact
        # retry, which returns the original terminal record and wire response.
        exact_retry = McpElicitationResolution(
            request_id="request-r1",
            session_id="elicitation-session",
            server_id="elicitation-server",
            expected_revision=r1.revision,
            action=McpElicitationAction.ACCEPT,
            content={"api_token": "secret-r1", "replicas": 1, "region": "edge"},
            actor_id="operator-1",
            idempotency_key="shared-resolution-key",
        )
        retried = queue.resolve(exact_retry)
        self.assertEqual(retried.record, first.record)
        self.assertEqual(retried.wire_response, first.wire_response)

        # Same identity with changed canonical content (and therefore a
        # different fingerprint) is also a collision, not an idempotent retry.
        changed_content = McpElicitationResolution(
            request_id="request-r1",
            session_id="elicitation-session",
            server_id="elicitation-server",
            expected_revision=r1.revision,
            action=McpElicitationAction.ACCEPT,
            content={"region": "other", "replicas": 1, "api_token": "secret-r1"},
            actor_id="operator-1",
            idempotency_key="shared-resolution-key",
        )
        with self.assertRaises(McpElicitationConflict):
            queue.resolve(changed_content)

        # Every authority/input component participates in the fingerprint.
        # Looking up the cache happens before record lookup so even a collision
        # that points at a different identity is deterministically a conflict.
        bound_variants = {
            "server_id": replace(exact_retry, server_id="other-server"),
            "session_id": replace(exact_retry, session_id="other-session"),
            "request_id": replace(exact_retry, request_id="request-r2"),
            "expected_revision": replace(exact_retry, expected_revision=r1.revision + 1),
            "action": replace(exact_retry, action=McpElicitationAction.CANCEL, content={}),
            "actor_id": replace(exact_retry, actor_id="operator-2"),
        }
        for field_name, candidate in bound_variants.items():
            with self.subTest(bound_field=field_name):
                with self.assertRaises(McpElicitationConflict):
                    queue.resolve(candidate)

        second = queue.resolve(
            McpElicitationResolution(
                request_id="request-r2",
                session_id="elicitation-session",
                server_id="elicitation-server",
                expected_revision=r2.revision,
                action=McpElicitationAction.ACCEPT,
                content={"region": "cloud", "replicas": 2, "api_token": "secret-r2"},
                actor_id="operator-1",
                idempotency_key="r2-resolution-key",
            )
        )
        wait_r2.join(timeout=1.0)
        self.assertFalse(wait_r2.is_alive())
        self.assertEqual(waiter_results["r2"], second.wire_response)
        self.assertNotEqual(waiter_results["r1"], waiter_results["r2"])
        self.assertEqual(waiter_errors, [])

    def test_expiry_and_disconnect_are_terminal_and_server_scoped(self) -> None:
        clock = MutableUtcClock(datetime(2026, 7, 11, 1, 0, tzinfo=UTC))
        queue = McpElicitationQueue(now=clock)
        expiring = self._request(
            "expiring",
            expires_at=(clock.value + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
        )
        queue.enqueue(expiring)
        queue.enqueue(self._request("disconnect"))
        queue.enqueue(self._request("other", server_id="other-server"))

        clock.advance(timedelta(seconds=11))
        expired = queue.expire_due()
        self.assertEqual([item.request.request_id for item in expired], ["expiring"])
        self.assertEqual(expired[0].status, McpElicitationStatus.EXPIRED)

        cancelled = queue.cancel_for_server("elicitation-server", reason="transport disconnected")
        self.assertEqual([item.request.request_id for item in cancelled], ["disconnect"])
        self.assertEqual(cancelled[0].status, McpElicitationStatus.CANCELLED)
        self.assertEqual(cancelled[0].terminal_reason, "transport disconnected")
        other = queue.get("other-server", "elicitation-session", "other")
        self.assertIsNotNone(other)
        self.assertEqual(other.status, McpElicitationStatus.PENDING)  # type: ignore[union-attr]
        self.assertEqual(queue.cancel_for_server("elicitation-server"), ())


class McpSamplingControlTests(unittest.TestCase):
    def test_unsolicited_sampling_without_callback_fails_closed_over_transport(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        runtime = McpClientRuntime(
            state_store=McpRuntimeStateStore(root / "state.json"),
            artifact_store=LocalArtifactStore(root / "artifacts"),
            credential_vault=FileCredentialVault(root / "credentials"),
        )
        self.addCleanup(runtime.connection_runtime.close_all)
        peer = SamplingServerProgram()
        runtime.add_server(
            "sampling-control",
            {"server_id": "sampling-control", "transport": "in_process"},
        )
        runtime.register_in_process("sampling-control", peer)
        receipt = runtime.connect_server("sampling-control")
        self.assertTrue(receipt.connected, receipt.safe_dict())
        advertised = dict(peer.initialize_params.get("capabilities") or {})
        self.assertNotIn("sampling", advertised)

        peer.request_sampling()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and peer.response() is None:
            time.sleep(0.01)
        response = peer.response()

        self.assertIsNotNone(response)
        self.assertIsInstance(response, JsonRpcSuccessResponse)
        assert isinstance(response, JsonRpcSuccessResponse)
        self.assertEqual(response.request_id, "sampling-request-1")
        self.assertEqual(response.result["error"]["code"], "default_deny")  # type: ignore[index]
        self.assertIn("not completed", response.result["error"]["message"])  # type: ignore[index]
        diagnostics = runtime.sampling_runtime.safe_diagnostics()
        self.assertFalse(diagnostics["policy"]["enabled"])
        self.assertEqual(diagnostics["advertised_servers"], [])
        self.assertFalse(diagnostics["has_default_callback"])


if __name__ == "__main__":
    unittest.main()
