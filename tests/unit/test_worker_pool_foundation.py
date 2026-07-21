from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from zyra_scheduler.worker_pool import (
    AttemptState,
    BackendCapability,
    CancellationRequest,
    CapabilityRequirement,
    ExecutionOutcome,
    HeartbeatPolicy,
    InboxMessageState,
    LeaseFenced,
    LeaseState,
    ResourceVector,
    WorkerLocation,
    WorkerPoolError,
    WorkerPoolErrorCode,
    WorkerPoolFoundationRuntime,
    TakeoverReason,
)
from zyra_scheduler.worker_pool.models import parse_utc, utc_iso


SECRET = b"worker-pool-foundation-test-secret-32-bytes"


def _runtime(path: Path) -> WorkerPoolFoundationRuntime:
    return WorkerPoolFoundationRuntime(
        path,
        attestation_secret=SECRET,
        heartbeat_policy=HeartbeatPolicy(
            healthy_after_seconds=1,
            stale_after_seconds=2,
            lost_after_seconds=3,
            unrecoverable_after_seconds=4,
        ),
        default_lease_ttl_seconds=30,
    )


def _register(runtime: WorkerPoolFoundationRuntime, worker_id: str = "local-test-worker"):
    backend = BackendCapability(
        backend_id="test-sandbox",
        backend_kind="sandbox_gateway",
        enabled=True,
        healthy=True,
        capabilities=("agent_task", "code_execution", "artifact_return"),
        tool_ids=("read", "write", "shell"),
    )
    return runtime.register_local_worker(
        worker_id=worker_id,
        worker_kind="code-worker",
        backend=backend,
        capabilities=("agent_task", "code_execution", "artifact_return"),
        tool_ids=("read", "write", "shell"),
        resources=ResourceVector(cpu_cores=2, memory_mb=2048, process_slots=2),
    )


def _requirement() -> CapabilityRequirement:
    return CapabilityRequirement(
        required=("agent_task",),
        tool_ids=("read",),
        locations=(WorkerLocation.LOCAL,),
        resources=ResourceVector(cpu_cores=0.25, memory_mb=128, process_slots=1),
    )


def test_worker_lifecycle_lease_receipt_and_restart_restore(tmp_path: Path) -> None:
    store_path = tmp_path / "worker-pool.sqlite3"
    runtime = _runtime(store_path)
    registration = _register(runtime)
    runtime.heartbeat_local_worker(registration.worker.worker_id, sequence=1)

    acquisition = runtime.acquire_task(
        task_id="logical-task-1",
        run_id="run-1",
        owner_session_id="session-1",
        requirement=_requirement(),
        attempt_number=1,
        idempotency_key="lease-logical-task-1-attempt-1",
    )
    assert acquisition.worker.state.value == "busy"
    assert acquisition.attempt.task_id == "logical-task-1"
    assert acquisition.attempt.state is AttemptState.LEASED
    assert acquisition.lease.state is LeaseState.ACTIVE
    assert acquisition.lease.to_dict()["fence_token"] == ""

    started = runtime.leases.start_attempt(
        acquisition.lease.lease_id,
        worker_id=acquisition.worker.worker_id,
        fence_token=acquisition.lease.fence_token,
        fence_epoch=acquisition.lease.fence_epoch,
        backend_dispatch_id="dispatch-1",
    )
    assert started.state is AttemptState.RUNNING
    receipt = runtime.leases.complete(
        acquisition.lease.lease_id,
        worker_id=acquisition.worker.worker_id,
        fence_token=acquisition.lease.fence_token,
        fence_epoch=acquisition.lease.fence_epoch,
        outcome=ExecutionOutcome.SUCCEEDED,
        summary="real local attempt completed",
        artifact_refs=("artifact-local-1",),
    )
    assert receipt.outcome is ExecutionOutcome.SUCCEEDED
    assert runtime.store.require_worker(acquisition.worker.worker_id).state.value == "idle"

    restarted = _runtime(store_path)
    restored_worker = restarted.store.require_worker(registration.worker.worker_id)
    restored_attempts = restarted.store.list_attempts(task_id="logical-task-1")
    restored_receipts = restarted.store.receipts_for_task("logical-task-1")
    assert restored_worker.generation == registration.worker.generation
    assert len(restored_attempts) == 1
    assert restored_attempts[0].state is AttemptState.SUCCEEDED
    assert restored_receipts[0].receipt_id == receipt.receipt_id
    assert restarted.store.integrity_report()["ok"] is True


def test_lost_lease_creates_new_attempt_without_copying_logical_task_and_fences_old_owner(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "pool.sqlite3")
    _register(runtime)
    first = runtime.acquire_task(
        task_id="stable-logical-task",
        run_id="run-recovery",
        owner_session_id="session-a",
        requirement=_requirement(),
        attempt_number=1,
    )
    expired, lost = runtime.leases.expire(first.lease.lease_id, reason="edge heartbeat lost")
    assert expired.state is LeaseState.EXPIRED
    assert lost.state is AttemptState.LOST
    second = runtime.acquire_task(
        task_id="stable-logical-task",
        run_id="run-recovery",
        owner_session_id="session-b",
        requirement=_requirement(),
        attempt_number=2,
        recovery_reason="take over after heartbeat loss",
    )
    assert second.attempt.task_id == first.attempt.task_id
    assert second.attempt.attempt_id != first.attempt.attempt_id
    assert second.lease.lease_id != first.lease.lease_id
    assert second.attempt.parent_attempt_id == first.attempt.attempt_id
    assert [item.attempt_number for item in runtime.store.list_attempts(task_id="stable-logical-task")] == [1, 2]

    with pytest.raises(LeaseFenced):
        runtime.leases.complete(
            first.lease.lease_id,
            worker_id=first.worker.worker_id,
            fence_token=first.lease.fence_token,
            fence_epoch=first.lease.fence_epoch,
            outcome=ExecutionOutcome.SUCCEEDED,
            summary="stale owner must not commit",
        )


def test_drain_blocks_new_lease_and_cancellation_changes_physical_state(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "pool.sqlite3")
    registration = _register(runtime)
    acquisition = runtime.acquire_task(
        task_id="cancel-task",
        run_id="cancel-run",
        owner_session_id="cancel-session",
        requirement=_requirement(),
    )
    draining = runtime.lifecycle.begin_drain(registration.worker.worker_id, reason="maintenance")
    assert draining.state.value == "draining"
    with pytest.raises(WorkerPoolError) as error:
        runtime.acquire_task(
            task_id="blocked-by-drain",
            run_id="cancel-run",
            owner_session_id="cancel-session",
            requirement=_requirement(),
        )
    assert error.value.code is WorkerPoolErrorCode.RESOURCE_EXHAUSTED

    receipt = runtime.cancellation.cancel(
        CancellationRequest(
            task_id="cancel-task",
            run_id="cancel-run",
            reason="user cancelled",
        )
    )
    assert receipt.changed is True
    assert acquisition.lease.lease_id in receipt.cancelled_lease_ids
    assert runtime.store.get_lease(acquisition.lease.lease_id).state is LeaseState.CANCELLED
    assert runtime.store.get_attempt(acquisition.attempt.attempt_id).state is AttemptState.CANCELLED


def test_inbox_claim_requeue_ack_and_expired_claim_recovery(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "pool.sqlite3")
    worker = _register(runtime).worker
    envelope, wakeup = runtime.inbox.enqueue(
        worker_id=worker.worker_id,
        task_id="inbox-task",
        run_id="inbox-run",
        message_kind="task_assignment",
        payload={"logical_task_ref": "inbox-task", "attempt": 1},
    )
    assert wakeup is not None
    claimed = runtime.inbox.claim(worker.worker_id, claim_owner="worker-loop", limit=1)
    assert claimed[0].envelope_id == envelope.envelope_id
    requeued = runtime.inbox.requeue(
        envelope.envelope_id,
        claim_owner="worker-loop",
        reason="transient resource pressure",
    )
    assert requeued.state is InboxMessageState.REQUEUED
    assert any(
        item.operation == "wakeup_enqueued"
        and item.payload.get("envelope_id") == envelope.envelope_id
        for item in runtime.store.journal(task_id="inbox-task")
    )
    claimed_again = runtime.inbox.claim(worker.worker_id, claim_owner="worker-loop-2", limit=1)
    acknowledged = runtime.inbox.acknowledge(
        claimed_again[0].envelope_id,
        claim_owner="worker-loop-2",
    )
    assert acknowledged.state is InboxMessageState.ACKNOWLEDGED

    second, _ = runtime.inbox.enqueue(
        worker_id=worker.worker_id,
        task_id="inbox-task-2",
        run_id="inbox-run",
        message_kind="resume",
        payload={"resume": True},
    )
    runtime.inbox.claim(worker.worker_id, claim_owner="dead-process", limit=1)
    future = utc_iso(parse_utc(utc_iso()) + timedelta(minutes=2))
    recovered = runtime.inbox.recover_expired_claims(now=future)
    assert second.envelope_id in {item.envelope_id for item in recovered}


def test_heartbeat_sweep_marks_worker_lost_expires_lease_and_requests_recovery(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "pool.sqlite3")
    worker = _register(runtime).worker
    runtime.heartbeat_local_worker(worker.worker_id, sequence=1)
    acquisition = runtime.acquire_task(
        task_id="heartbeat-task",
        run_id="heartbeat-run",
        owner_session_id="heartbeat-session",
        requirement=_requirement(),
    )
    latest = runtime.store.require_worker(worker.worker_id)
    old = latest.advance(
        latest.state,
        last_heartbeat_at=utc_iso(parse_utc(utc_iso()) - timedelta(seconds=10)),
    )
    runtime.store.update_worker(
        old,
        expected_version=latest.version,
        operation="test_heartbeat_clock_advanced",
    )
    sweep = runtime.heartbeats.sweep()
    assert acquisition.lease.lease_id in sweep.expired_lease_ids
    assert "heartbeat-task" in sweep.recovery_task_ids
    assert runtime.store.require_worker(worker.worker_id).state.value == "lost"


def test_takeover_runtime_preserves_requirements_and_uses_new_physical_attempt(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "pool.sqlite3")
    _register(runtime, "local-worker-a")
    _register(runtime, "local-worker-b")
    first = runtime.acquire_task(
        task_id="takeover-logical-task",
        run_id="takeover-run",
        owner_session_id="takeover-session",
        requirement=_requirement(),
        preferred_worker_ids=("local-worker-a",),
    )
    runtime.leases.expire(first.lease.lease_id, reason="worker process disappeared")
    signal = runtime.takeover.signal_for_attempt(
        first.attempt.attempt_id,
        reason=TakeoverReason.WORKER_LOST,
        message="worker process disappeared",
    )
    plan = runtime.takeover.plan(signal)
    receipt = runtime.takeover.execute(plan)
    assert receipt.changed is True
    assert receipt.acquisition.attempt.task_id == first.attempt.task_id
    assert receipt.acquisition.attempt.attempt_number == 2
    assert receipt.acquisition.attempt.attempt_id != first.attempt.attempt_id
    assert receipt.acquisition.worker.worker_id == "local-worker-b"
