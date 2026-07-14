from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from zyra_runtime.sandbox_gateway import (  # noqa: E402
    CallbackToolPermissionRuntimePort,
    CommandEffect,
    GatewayCommandEnvelope,
    GatewayLifecycleState,
    GatewayPermissionRelay,
    GatewayStateStore,
    PermissionRuntimeDecision,
    SandboxGatewayError,
    SandboxLifecycle,
    SessionActorQueue,
    StructuredCommandPolicy,
)


def envelope() -> GatewayCommandEnvelope:
    return GatewayCommandEnvelope.build(
        session_id="session-state",
        run_id="run-state",
        task_id="task-state",
        worker_id="CodeWorkerRuntime",
        executable="rg",
        argv=("needle", "."),
        tool_use_id="tool-state",
    )


class SandboxGatewayStateTests(unittest.TestCase):
    def test_lifecycle_persists_across_store_restart_and_fences_old_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = GatewayStateStore(tmpdir)
            lifecycle = SandboxLifecycle(store, lease_seconds=30)
            created = lifecycle.create(
                session_id="session-state",
                run_id="run-state",
                task_id="task-state",
                workspace_id="workspace-state",
                worker_id="CodeWorkerRuntime",
                backend_id="test-backend",
            )
            lifecycle.prepare(created.session_id)
            ready = lifecycle.ready(created.session_id)
            busy, lease = lifecycle.begin_command(
                ready.session_id,
                "command-state",
                owner_id=ready.worker_id,
                fence_token="fence-state",
            )

            restarted = GatewayStateStore(tmpdir)
            persisted = restarted.require_session(ready.session_id)

            self.assertEqual(persisted.state, GatewayLifecycleState.BUSY)
            self.assertEqual(persisted.lease.lease_id, lease.lease_id)
            with self.assertRaises(SandboxGatewayError):
                SandboxLifecycle(restarted).assert_lease(
                    persisted.session_id,
                    lease.lease_id,
                    owner_id=ready.worker_id,
                    fence_token="wrong-fence",
                )
            finished = SandboxLifecycle(restarted).finish_command(
                persisted.session_id,
                lease.lease_id,
                owner_id=ready.worker_id,
                fence_token="fence-state",
            )
            self.assertEqual(finished.state, GatewayLifecycleState.READY)

    def test_permission_binding_is_exact_single_use_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = GatewayStateStore(tmpdir)
            calls = {"evaluate": 0, "consume": 0}

            def evaluate(request):
                calls["evaluate"] += 1
                return PermissionRuntimeDecision(
                    allowed=True,
                    effect=CommandEffect.ALLOW,
                    request_fingerprint=request.request_fingerprint,
                    grant_material={"grant_id": "grant-state"},
                    expires_at=time.time() + 60,
                    reason="test ToolPermissionRuntime grant",
                )

            def consume(request, decision, replay):
                calls["consume"] += 1
                return (
                    request.request_fingerprint
                    == decision.request_fingerprint
                    and replay.identity_digest == request.envelope.identity_digest
                )

            relay = GatewayPermissionRelay(
                store,
                CallbackToolPermissionRuntimePort(
                    evaluate=evaluate,
                    validate_and_consume=consume,
                ),
            )
            command = envelope()
            policy = StructuredCommandPolicy().evaluate(command)
            ticket = relay.issue(
                command,
                policy,
                interactive=True,
                sealed=False,
            )
            consumed = relay.consume(ticket, command)

            self.assertTrue(consumed.consumed)
            self.assertEqual(calls, {"evaluate": 1, "consume": 1})
            restarted = GatewayStateStore(tmpdir)
            self.assertTrue(
                restarted.require_permission_binding(
                    ticket.binding.binding_id
                ).consumed
            )
            with self.assertRaises(SandboxGatewayError):
                relay.consume(ticket, command)
            self.assertEqual(
                calls["consume"],
                1,
                "durable replay rejection must happen before authority callback",
            )

    def test_session_actor_queue_serializes_same_session_not_other_sessions(self) -> None:
        queue = SessionActorQueue()
        order: list[str] = []
        first_entered = threading.Event()
        release = threading.Event()

        def first() -> None:
            with queue.enter("same"):
                order.append("first-start")
                first_entered.set()
                release.wait(2)
                order.append("first-end")

        def second() -> None:
            first_entered.wait(2)
            with queue.enter("same"):
                order.append("second")

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        second_thread.start()
        first_entered.wait(2)
        with queue.enter("other"):
            order.append("other")
        release.set()
        first_thread.join(2)
        second_thread.join(2)

        self.assertLess(order.index("other"), order.index("first-end"))
        self.assertGreater(order.index("second"), order.index("first-end"))

    def test_state_compare_and_swap_rejects_stale_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = GatewayStateStore(tmpdir)
            lifecycle = SandboxLifecycle(store)
            created = lifecycle.create(
                session_id="session-cas",
                run_id="run-cas",
                task_id="task-cas",
                workspace_id="workspace-cas",
                worker_id="worker-cas",
                backend_id="backend-cas",
            )
            prepared = lifecycle.prepare(created.session_id)
            stale = created.transition(GatewayLifecycleState.FAILED)

            with self.assertRaises(SandboxGatewayError):
                store.save_session(stale, expected_generation=created.generation)
            self.assertEqual(
                store.require_session(created.session_id).generation,
                prepared.generation,
            )


if __name__ == "__main__":
    unittest.main()
