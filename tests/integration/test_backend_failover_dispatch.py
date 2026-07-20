from __future__ import annotations

import concurrent.futures
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_scheduler.backend_registry import (
    ActiveDispatchRegistry,
    BackendControlRuntime,
    BackendDefinition,
    BackendDispatchError,
    BackendDispatchJournal,
    BackendFailureKind,
    BackendKind,
    BackendLocation,
    BackendRegistry,
    BackendRegistryStore,
    BackendResourceLimits,
    BackendSelectionRequest,
    RemoteBackendControlClient,
    WorkerDispatchRouter,
    WorkspacePolicy,
)
from zyra_workers.backend_dispatch_service import (
    BackendDispatchHttpServer,
    BackendDispatchServiceRuntime,
    BackendOperationRegistry,
    BackendServiceConfig,
)


class BackendFailoverDispatchIntegrationTests(unittest.TestCase):
    def test_real_http_failover_changes_backend_and_preserves_provider_route(self) -> None:
        with _EdgeFixture(include_unavailable_primary=True) as fixture:
            result = fixture.router.dispatch_payload(
                fixture.request(preferred_backend_id="edge-primary"),
                operation_name="echo",
                payload={"message": "real-edge-result", "nonce": time.time_ns()},
                idempotency_key="real-http-failover",
            )

            self.assertEqual(result.value["message"], "real-edge-result")
            self.assertEqual(result.value["backend_id"], "edge-alternate")
            self.assertTrue(result.backend_changed)
            self.assertEqual(
                [attempt.backend_id for attempt in result.attempts],
                ["edge-primary", "edge-alternate"],
            )
            self.assertEqual(
                {attempt.provider_route_changed for attempt in result.attempts},
                {False},
            )
            self.assertEqual(
                {envelope.provider_route_id for envelope in fixture.observed_envelopes},
                {"provider-route-pinned"},
            )
            self.assertEqual(
                {envelope.m0_execution_ref for envelope in fixture.observed_envelopes},
                {"worker_request:m0-edge"},
            )
            self.assertIsNone(result.final_lease.physical_worker_lease_ref)

            journal = BackendDispatchJournal(fixture.store)
            replay = journal.replay_plan(result.session.session_id)
            self.assertEqual(replay.original_backend_id, "edge-primary")
            self.assertEqual(replay.final_backend_id, "edge-alternate")
            self.assertEqual(replay.original_provider_route_id, "provider-route-pinned")
            self.assertEqual(replay.final_provider_route_id, "provider-route-pinned")
            self.assertEqual(replay.m0_execution_ref, "worker_request:m0-edge")
            self.assertIsNotNone(replay.materialization)
            self.assertGreaterEqual(len(replay.records), 7)
            verification = journal.verify(result.session.session_id)
            self.assertTrue(verification["valid"])
            self.assertTrue(verification["backend_changed"])
            self.assertFalse(verification["provider_route_changed"])
            self.assertTrue(
                any(
                    event.event_type == "backend.failover.committed"
                    and event.payload["backend_changed"] is True
                    and event.payload["provider_route_changed"] is False
                    for event in result.events
                )
            )

    def test_control_cancel_interrupts_pending_remote_dispatch(self) -> None:
        with _EdgeFixture(include_unavailable_primary=False) as fixture:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    fixture.router.dispatch_payload,
                    fixture.request(preferred_backend_id="edge-alternate"),
                    operation_name="wait-for-cancel",
                    payload={"poll_seconds": 0.01},
                    idempotency_key="remote-control-cancel",
                )
                _wait_until(
                    lambda: bool(fixture.server.runtime.records(active_only=True))
                    and bool(fixture.active.snapshot()),
                    timeout=5.0,
                )
                receipt = BackendControlRuntime(
                    fixture.store,
                    active=fixture.active,
                ).cancel_task(
                    run_id="run-edge",
                    task_id="task-edge",
                    reason="integration cancellation",
                    requested_by="test-control",
                    idempotency_key="cancel:run-edge:task-edge",
                )
                self.assertTrue(receipt.effective)
                self.assertEqual(len(receipt.interrupted_session_ids), 1)
                with self.assertRaises(BackendDispatchError) as raised:
                    future.result(timeout=5.0)

            self.assertEqual(raised.exception.kind, BackendFailureKind.DISPATCH_ABORTED)
            records = fixture.server.runtime.records()
            self.assertEqual(len(records), 1)
            _wait_until(lambda: records[0].state.value == "cancelled", timeout=5.0)
            remote = RemoteBackendControlClient(fixture.alternate_definition)
            status = remote.status(records[0].dispatch_id)
            self.assertEqual(status.state, "cancelled")
            self.assertFalse(status.output_observed)
            recovery = fixture.store.recovery_inputs(run_id="run-edge", task_id="task-edge")
            self.assertTrue(any(item.reason_code == "control_cancel" for item in recovery))
            session = fixture.store.dispatch_sessions(run_id="run-edge", task_id="task-edge")[-1]
            replay = BackendDispatchJournal(fixture.store).replay_plan(session.session_id)
            self.assertIn(BackendFailureKind.DISPATCH_ABORTED.value, replay.failure_kinds)

    def test_remote_control_drain_and_resume_change_worker_acceptance(self) -> None:
        with _EdgeFixture(include_unavailable_primary=False) as fixture:
            client = RemoteBackendControlClient(fixture.alternate_definition)
            initial = client.health()
            self.assertTrue(initial.ok)
            self.assertTrue(initial.accepting)
            self.assertIn("echo", initial.operations)

            drained = client.drain("maintenance window")
            self.assertTrue(drained.effective)
            self.assertFalse(client.health().accepting)
            with self.assertRaises(BackendDispatchError) as raised:
                fixture.router.dispatch_payload(
                    fixture.request(preferred_backend_id="edge-alternate"),
                    operation_name="echo",
                    payload={"message": "must-not-run"},
                    idempotency_key="dispatch-while-drained",
                )
            self.assertEqual(raised.exception.kind, BackendFailureKind.BACKEND_UNAVAILABLE)

            resumed = client.resume()
            self.assertTrue(resumed.effective)
            self.assertTrue(client.health().accepting)

    def test_workspace_root_replacement_quarantines_backend_reuse(self) -> None:
        with _LocalFixture() as fixture:
            original = fixture.workspace

            def replace_workspace(_envelope):
                original.rmdir()
                original.mkdir()
                return {"changed": True}

            with self.assertRaises(BackendDispatchError) as raised:
                fixture.router.dispatch_callable(
                    fixture.request,
                    replace_workspace,
                    idempotency_key="workspace-root-replaced",
                )
            self.assertEqual(raised.exception.kind, BackendFailureKind.WORKSPACE_CORRUPT)
            self.assertTrue(raised.exception.output_observed)
            self.assertTrue(
                fixture.store.workspace_is_quarantined(
                    str(fixture.workspace),
                    backend_id="local-worker",
                    worker_id="CodeWorkerRuntime",
                )
            )
            with self.assertRaises(BackendDispatchError) as selection_error:
                fixture.registry.acquire(fixture.request)
            self.assertEqual(selection_error.exception.kind, BackendFailureKind.BACKEND_UNAVAILABLE)


class _EdgeFixture:
    def __init__(self, *, include_unavailable_primary: bool) -> None:
        self.include_unavailable_primary = include_unavailable_primary
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.artifacts = self.root / "artifacts"
        self.workspace.mkdir()
        self.artifacts.mkdir()
        self.observed_envelopes = []
        operations = BackendOperationRegistry()

        def echo(context, payload):
            context.check_cancelled()
            self.observed_envelopes.append(context.envelope)
            return {
                "message": payload.get("message"),
                "backend_id": context.envelope.backend_id,
                "provider_route_id": context.envelope.provider_route_id,
                "m0_execution_ref": context.envelope.m0_execution_ref,
            }

        def wait_for_cancel(context, payload):
            poll = float(payload.get("poll_seconds") or 0.01)
            while True:
                context.check_cancelled()
                time.sleep(poll)

        operations.register("echo", echo)
        operations.register("wait-for-cancel", wait_for_cancel)
        runtime = BackendDispatchServiceRuntime(
            BackendServiceConfig(
                backend_id="edge-alternate",
                runtime_worker="CodeWorkerRuntime",
                maximum_concurrency=2,
                capabilities=("code-change",),
                workspace_roots=(str(self.workspace),),
                artifact_roots=(str(self.artifacts),),
            ),
            operations,
        )
        self.server = BackendDispatchHttpServer(runtime).start()
        self.store = BackendRegistryStore(self.root / "backend.sqlite3")
        self.registry = BackendRegistry(
            self.store,
            lease_seconds=10.0,
            attempt_limit=3,
            failure_threshold=1,
        )
        if include_unavailable_primary:
            self.registry.register(
                self._definition(
                    "edge-primary",
                    _unused_endpoint(),
                    priority=100,
                )
            )
        self.alternate_definition = self._definition(
            "edge-alternate",
            self.server.endpoint,
            priority=10,
        )
        self.registry.register(self.alternate_definition)
        self.active = ActiveDispatchRegistry()
        self.router = WorkerDispatchRouter(self.registry, active=self.active)

    def _definition(self, backend_id: str, endpoint: str, *, priority: int) -> BackendDefinition:
        return BackendDefinition(
            backend_id=backend_id,
            display_name=backend_id,
            kind=BackendKind.EDGE_HTTP,
            location=BackendLocation.EDGE,
            runtime_worker="CodeWorkerRuntime",
            capabilities=("code-change",),
            endpoint=endpoint,
            health_endpoint=f"{endpoint}/health",
            workspace_policy=WorkspacePolicy(require_existing=True),
            limits=BackendResourceLimits(
                maximum_concurrency=2,
                turn_timeout_seconds=4.0,
                connect_timeout_seconds=0.25,
                health_timeout_seconds=0.25,
            ),
            priority=priority,
            metadata={"allowed_hosts": ("127.0.0.1",)},
        )

    def request(self, *, preferred_backend_id: str) -> BackendSelectionRequest:
        return BackendSelectionRequest(
            run_id="run-edge",
            task_id="task-edge",
            node_id="node-edge",
            runtime_worker="CodeWorkerRuntime",
            preferred_backend_id=preferred_backend_id,
            required_capabilities=("code-change",),
            allowed_locations=(BackendLocation.EDGE,),
            excluded_backend_ids=(),
            workspace_root=str(self.workspace),
            artifact_root=str(self.artifacts),
            provider_route_id="provider-route-pinned",
            provider_route_checksum="provider-route-checksum",
            provider_catalog_revision=11,
            provider_credential_version=4,
            provider_credential_fingerprint="sha256:credential-safe",
            provider_transport_id="openai-chat-sse",
            m0_execution_ref="worker_request:m0-edge",
            turn_id="turn-edge",
        )

    def __enter__(self) -> _EdgeFixture:
        return self

    def __exit__(self, *_: object) -> None:
        self.store.close()
        self.server.close()
        self.temporary.cleanup()


class _LocalFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.artifacts = root / "artifacts"
        self.workspace.mkdir()
        self.artifacts.mkdir()
        self.store = BackendRegistryStore(root / "backend.sqlite3")
        self.registry = BackendRegistry(self.store, attempt_limit=2)
        self.registry.register(
            BackendDefinition(
                backend_id="local-worker",
                display_name="local-worker",
                kind=BackendKind.LOCAL_PROCESS,
                location=BackendLocation.LOCAL,
                runtime_worker="CodeWorkerRuntime",
                capabilities=("code-change",),
                workspace_policy=WorkspacePolicy(require_existing=True),
                limits=BackendResourceLimits(turn_timeout_seconds=2.0),
                priority=100,
            )
        )
        self.router = WorkerDispatchRouter(self.registry)
        self.request = BackendSelectionRequest(
            run_id="run-workspace",
            task_id="task-workspace",
            node_id="node-workspace",
            runtime_worker="CodeWorkerRuntime",
            preferred_backend_id="local-worker",
            required_capabilities=("code-change",),
            allowed_locations=(BackendLocation.LOCAL,),
            excluded_backend_ids=(),
            workspace_root=str(self.workspace),
            artifact_root=str(self.artifacts),
            provider_route_id="provider-route-workspace",
            provider_route_checksum="route-workspace-checksum",
            provider_catalog_revision=5,
            provider_credential_version=2,
            provider_credential_fingerprint="sha256:workspace-safe",
            provider_transport_id="anthropic-sse",
            m0_execution_ref="worker_request:m0-workspace",
            turn_id="turn-workspace",
        )

    def __enter__(self) -> _LocalFixture:
        return self

    def __exit__(self, *_: object) -> None:
        self.store.close()
        self.temporary.cleanup()


def _unused_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _wait_until(predicate, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


if __name__ == "__main__":
    unittest.main()
