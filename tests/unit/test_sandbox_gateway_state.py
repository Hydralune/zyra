from __future__ import annotations

import tempfile
import threading
import time
import unittest
import multiprocessing
from pathlib import Path

from zyra_runtime.sandbox_gateway import (  # noqa: E402
    CallbackToolPermissionRuntimePort,
    CommandEffect,
    GatewayCommandEnvelope,
    GatewayLifecycleState,
    GatewayPermissionRelay,
    GatewayEventPort,
    GatewayStateStore,
    PermissionRuntimeDecision,
    SandboxGatewayError,
    SandboxLifecycle,
    SessionActorQueue,
    StructuredCommandPolicy,
)


def _emit_gateway_events(state_root: str, count: int, worker: int) -> None:
    store = GatewayStateStore(state_root)
    record = store.require_session("session-events")
    port = GatewayEventPort(store)
    for index in range(count):
        port.emit(
            record,
            "gateway_command_output",
            {"worker": worker, "index": index},
            causation_id=f"worker-{worker}-event-{index}",
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
    def test_event_sequence_allocation_is_atomic_across_processes_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = GatewayStateStore(tmpdir, event_history_limit=1_000)
            record = SandboxLifecycle(store).create(
                session_id="session-events",
                run_id="run-events",
                task_id="task-events",
                workspace_id="workspace-events",
                worker_id="worker-events",
                backend_id="backend-events",
            )
            processes = [
                multiprocessing.Process(
                    target=_emit_gateway_events,
                    args=(tmpdir, 20, worker),
                )
                for worker in range(4)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)

            events = GatewayStateStore(tmpdir).list_events(record.session_id)
            self.assertEqual([event.sequence for event in events], list(range(1, 81)))
            self.assertEqual(len({event.event_id for event in events}), 80)

            port = GatewayEventPort(GatewayStateStore(tmpdir))
            first = port.emit(
                record,
                "gateway_command_output",
                {"replay": True},
                causation_id="stable-replay",
            )
            replay = port.emit(
                record,
                "gateway_command_output",
                {"replay": True},
                causation_id="stable-replay",
            )
            self.assertEqual(first.event_id, replay.event_id)
            self.assertEqual(first.sequence, replay.sequence)

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

    def test_command_lease_renewal_preserves_fence_and_extends_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = GatewayStateStore(tmpdir)
            lifecycle = SandboxLifecycle(store, lease_seconds=30)
            created = lifecycle.create(
                session_id="session-renew",
                run_id="run-renew",
                task_id="task-renew",
                workspace_id="workspace-renew",
                worker_id="CodeWorkerRuntime",
                backend_id="test-backend",
            )
            lifecycle.prepare(created.session_id)
            ready = lifecycle.ready(created.session_id)
            _, lease = lifecycle.begin_command(
                ready.session_id,
                "command-renew",
                owner_id=ready.worker_id,
                fence_token="fence-renew",
            )

            renewed = lifecycle.renew_command(
                ready.session_id,
                lease.lease_id,
                owner_id=ready.worker_id,
                fence_token="fence-renew",
            )

            self.assertEqual(renewed.lease_id, lease.lease_id)
            self.assertEqual(renewed.owner_epoch, lease.owner_epoch)
            self.assertGreaterEqual(renewed.expires_at, lease.expires_at)
            persisted = store.require_session(ready.session_id)
            self.assertEqual(persisted.lease, renewed)
            self.assertEqual(persisted.metadata["lease_renewal_count"], 1)

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
