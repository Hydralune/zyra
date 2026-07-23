from __future__ import annotations

import os
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

from zyra_orchestration.graph_custody import (
    DynamicTopologyRuntime,
    GraphNode,
    GraphStateCustody,
    GraphStateStore,
)
from zyra_scheduler.worker_pool import (
    AdmissionPhase,
    AdmissionPolicy,
    AdmissionPorts,
    BackendCapability,
    BackendRegistryHealthAdapter,
    CapabilityAttestor,
    ControlKind,
    ControlPhase,
    DispatchAdmissionRequest,
    DispatchMode,
    ExecutionOutcome,
    LeaseFenced,
    LeaseState,
    ResourceVector,
    RouteHealth,
    SchedulerDispatchContext,
    WorkerLifecycleState,
    WorkerLocation,
    WakeupState,
    WorkerPoolError,
    WorkerPoolFoundationRuntime,
    WorkerPoolIntegrationRuntime,
    YieldKind,
    make_foreign_refs,
)
from zyra_scheduler.worker_pool.models import parse_utc, stable_digest, utc_iso
from zyra_workers.edge_pool import (
    EdgeWorkerGatewayRuntime,
    EdgeWorkerProcessConnector,
    EdgeWorkerRegistrationRuntime,
    IntegratedEdgeExecutionAdapter,
)
from zyra_api.worker_pool_api import WorkerPoolApiService


SECRET = b"worker-pool-integration-secret-32-bytes"


def _package_roots() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[2]
    return tuple(path for path in (root / "packages").iterdir() if path.is_dir())


def _custody(path: Path, *, run_id: str = "run-integration") -> GraphStateCustody:
    store = GraphStateStore(path)
    store.initialize()
    custody = GraphStateCustody(store)
    custody.create(graph_id_value="graph-integration", run_id=run_id)
    topology = DynamicTopologyRuntime(custody)
    assert topology.add_node(
        "graph-integration",
        GraphNode(node_id="root", role="coordinator", capabilities=("plan",)),
        actor_id="test-bootstrap",
        causation_id="test-bootstrap",
    ).receipt.committed
    return custody


def _pool(path: Path, *, ttl: float = 30.0) -> WorkerPoolFoundationRuntime:
    return WorkerPoolFoundationRuntime(
        path,
        attestation_secret=SECRET,
        default_lease_ttl_seconds=ttl,
    )


def _register_local(
    pool: WorkerPoolFoundationRuntime,
    worker_id: str,
    *,
    slots: int = 4,
) -> None:
    pool.register_local_worker(
        worker_id=worker_id,
        worker_kind="code-worker",
        backend=BackendCapability(
            backend_id=f"sandbox-{worker_id}",
            backend_kind="sandbox_gateway",
            enabled=True,
            healthy=True,
            capabilities=("agent_task", "code_execution", "artifact_return"),
            tool_ids=("read", "write", "shell"),
        ),
        capabilities=("agent_task", "code_execution", "artifact_return"),
        tool_ids=("read", "write", "shell"),
        resources=ResourceVector(cpu_cores=2, memory_mb=2048, process_slots=slots),
    )
    pool.heartbeat_local_worker(worker_id, sequence=1)


def _runtime(tmp_path: Path, *, workers: tuple[str, ...] = ("worker-a",)) -> WorkerPoolIntegrationRuntime:
    pool = _pool(tmp_path / "pool.sqlite3")
    for worker_id in workers:
        _register_local(pool, worker_id)
    custody = _custody(tmp_path / "graph.sqlite3")
    return WorkerPoolIntegrationRuntime(
        pool,
        custody,
        policy=AdmissionPolicy(
            maximum_active_per_session=3,
            maximum_active_per_run=8,
            maximum_active_per_worker=4,
            default_lease_ttl_seconds=30,
            maximum_lease_ttl_seconds=120,
        ),
    )


def _add_node(runtime: WorkerPoolIntegrationRuntime, task_id: str) -> str:
    node_id = f"runtime-{task_id}"
    result = runtime.add_runtime_node_for_requirement(
        graph_id="graph-integration",
        logical_task_id=task_id,
        role="code_worker",
        capabilities=("agent_task", "code_execution"),
        connect_from=("root",),
        workspace_ref=f"workspace:{task_id}",
        causation_id=f"requirement:{task_id}",
        node_id=node_id,
    )
    assert result["version_ref"]["revision"] >= 3
    return node_id


def _context(
    runtime: WorkerPoolIntegrationRuntime,
    task_id: str,
    *,
    session_id: str = "session-integration",
    locations: tuple[WorkerLocation, ...] = (WorkerLocation.LOCAL,),
    edge_only: bool = False,
    preferred: tuple[str, ...] = (),
    attempt: int = 1,
    run_id: str = "run-integration",
) -> SchedulerDispatchContext:
    node_id = _add_node(runtime, task_id)
    graph_ref = runtime.topology.version_ref("graph-integration")
    return SchedulerDispatchContext(
        task_id=task_id,
        run_id=run_id,
        owner_session_id=session_id,
        logical_attempt=attempt,
        logical_task_revision=attempt,
        graph_id=graph_ref.graph_id,
        graph_revision=graph_ref.revision,
        graph_node_id=node_id,
        workspace_ref=f"workspace:{task_id}",
        gateway_ref="sandbox-gateway",
        backend_route_id="edge-sandbox" if edge_only else "local-code-worker",
        required_capabilities=("edge_execution",) if edge_only else ("agent_task",),
        locations=locations,
        resources=ResourceVector(process_slots=1, memory_mb=64),
        preferred_worker_ids=preferred,
        execution_mode=DispatchMode.BACKGROUND,
        edge_only=edge_only,
        lease_ttl_seconds=30,
        idempotency_key=f"admit:{task_id}:{attempt}",
        causation_id=f"schedule:{task_id}",
        correlation_id=session_id,
    )


def _admit(runtime: WorkerPoolIntegrationRuntime, task_id: str, **kwargs: Any):
    context = _context(runtime, task_id, **kwargs)
    plan, result = runtime.scheduler.admit(context)
    lease = runtime.pool.store.require_lease(result.binding.lease_id)
    return context, plan, result, lease


def _dispatch_projection(runtime: WorkerPoolIntegrationRuntime, binding_id: str) -> dict[str, Any]:
    binding = runtime.repository.get_binding(binding_id)
    assert binding is not None
    lease = runtime.pool.store.require_lease(binding.lease_id)
    worker = runtime.pool.store.require_worker(binding.worker_id)
    manifest = runtime.pool.store.latest_manifest(worker.worker_id)
    assert manifest is not None
    unsigned = {
        "schema": "zyra.worker-pool-dispatch/v1",
        "required": True,
        "canonical_owner": "python.WorkerPoolStore",
        "projection_owner": "typescript.OmpWorkerDispatchRuntime",
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "attempt": binding.attempt_number,
        "lease_id": binding.lease_id,
        "worker_id": binding.worker_id,
        "backend_id": binding.backend_id,
        "fence_epoch": binding.fence_epoch,
        "manifest_digest": manifest.digest,
        "concurrency_limit": manifest.resource_capacity.process_slots,
        "lease_state": lease.state.value,
        "logical_task_not_duplicated": True,
        "integration_binding_id": binding.binding_id,
        "graph_ref": binding.foreign_refs.graph.to_dict(),
        "workspace_ref": binding.foreign_refs.workspace.to_dict(),
        "gateway_ref": binding.foreign_refs.gateway.to_dict(),
        "route_ref": binding.foreign_refs.backend_route.to_dict(),
    }
    return {**unsigned, "projection_digest": stable_digest(unsigned)}


def test_scheduler_admission_drain_wake_progress_and_typed_yield_change_real_state(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _, plan, admission, lease = _admit(runtime, "task-active", preferred=("worker-a",))
    assert plan.accepted is True
    assert admission.binding.worker_id == "worker-a"
    assert runtime.graph_custody.current("graph-integration").node_map[
        "runtime-task-active"
    ].worker_lease_ref == lease.lease_id

    started = runtime.start(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        backend_dispatch_id="backend-dispatch-active",
    )
    progressed = runtime.progress(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        sequence=1,
        payload={"step": 1, "nested": {"tokens": [1, 2]}},
        force_renewal=True,
    )
    assert started.attempt_state == "running"
    assert progressed.renewal is not None
    assert progressed.renewal.disposition.value == "renewed"
    assert progressed.renewal.next_deadline_at >= progressed.renewal.prior_deadline_at
    renewed_deadline = runtime.pool.store.require_lease(lease.lease_id).deadline_at
    repeated = runtime.renewal.renew_if_due(
        progressed.binding,
        fence_token=lease.fence_token,
        progress_sequence=progressed.binding.progress_sequence,
        force=True,
    )
    assert repeated.disposition.value == "not_due"
    assert runtime.pool.store.require_lease(lease.lease_id).deadline_at == renewed_deadline

    drained = runtime.control.submit_and_apply(
        ControlKind.DRAIN,
        claim_owner="test-control",
        actor_id="operator",
        reason="rolling maintenance keeps current work alive",
        idempotency_key="drain:worker-a:1",
        worker_id="worker-a",
    )
    assert drained.phase is ControlPhase.APPLIED
    assert runtime.pool.store.require_worker("worker-a").state is WorkerLifecycleState.DRAINING
    assert runtime.pool.store.require_lease(lease.lease_id).state is LeaseState.DRAINING
    with pytest.raises((WorkerPoolError, ValueError), match="no worker|no registered|capacity"):
        _admit(runtime, "task-blocked", preferred=("worker-a",))

    # The already running attempt can report progress and settle while draining.
    second_progress = runtime.progress(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        sequence=2,
        payload={"step": 2, "draining": True},
    )
    assert second_progress.accepted is True
    outcome = runtime.complete(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        outcome=ExecutionOutcome.SUCCEEDED,
        summary="existing task drained cleanly",
        artifact_refs=("artifact://task-active/result",),
        result_payload={"result": "done"},
    )
    assert outcome.binding.phase is AdmissionPhase.SUCCEEDED
    assert outcome.typed_yield is not None
    assert outcome.typed_yield.kind is YieldKind.RESULT
    assert runtime.pool.store.require_lease(lease.lease_id).state is LeaseState.RELEASED

    woke = runtime.control.submit_and_apply(
        ControlKind.WAKE,
        claim_owner="test-control",
        actor_id="operator",
        reason="maintenance completed",
        idempotency_key="wake:worker-a:1",
        worker_id="worker-a",
    )
    assert woke.phase is ControlPhase.APPLIED
    assert runtime.pool.store.require_worker("worker-a").accepting_leases is True
    _, _, replacement, _ = _admit(runtime, "task-after-wake", preferred=("worker-a",))
    assert replacement.binding.worker_id == "worker-a"


def test_execution_gate_rechecks_canonical_state_and_rejects_resigned_mutations(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime = _runtime(tmp_path)
    _, _, admission, lease = _admit(runtime, "task-execution-gate")
    runtime.start(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        backend_dispatch_id="backend-execution-gate",
    )
    projection = _dispatch_projection(runtime, admission.binding.binding_id)
    authorized = runtime.execution_gate.authorize_projection(
        projection,
        expected_task_id="task-execution-gate",
        expected_run_id="run-integration",
        expected_session_id="session-integration",
    )
    assert authorized["lease_id"] == lease.lease_id
    assert authorized["commit_still_requires_fence"] is True
    assert any(
        item.operation == "worker_execution_authorized"
        for item in runtime.pool.store.journal(task_id="task-execution-gate")
    )

    mutations = {
        "lease_id": "lease_forged",
        "worker_id": "worker-forged",
        "fence_epoch": lease.fence_epoch + 1,
        "manifest_digest": "0" * 64,
        "integration_binding_id": "binding_forged",
    }
    for field, value in mutations.items():
        forged = dict(projection)
        forged[field] = value
        forged.pop("projection_digest", None)
        forged["projection_digest"] = stable_digest(forged)
        with pytest.raises(WorkerPoolError, match="projection|binding"):
            runtime.execution_gate.authorize_projection(
                forged,
                expected_task_id="task-execution-gate",
                expected_run_id="run-integration",
                expected_session_id="session-integration",
            )

    runtime.control.submit_cancel(
        task_id="task-execution-gate",
        run_id="run-integration",
        reason="pending cancellation must win before code starts",
        actor_id="operator",
        idempotency_key="execution-gate-pending-cancel",
    )
    with pytest.raises(WorkerPoolError, match="pending durable control"):
        runtime.execution_gate.authorize_projection(
            projection,
            expected_task_id="task-execution-gate",
            expected_run_id="run-integration",
            expected_session_id="session-integration",
        )

    monkeypatch.setenv("ZYRA_WORKER_EXECUTION_GATE_DISABLED", "1")
    with pytest.raises(WorkerPoolError, match="execution gate is disabled"):
        runtime.execution_gate.authorize_projection(
            projection,
            expected_task_id="task-execution-gate",
            expected_run_id="run-integration",
        )


def test_graceful_stop_survives_restart_and_converges_after_final_lease(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _, _, admission, lease = _admit(runtime, "task-graceful-stop")
    runtime.start(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        backend_dispatch_id="backend-graceful-stop",
    )
    admitted_projection = _dispatch_projection(runtime, admission.binding.binding_id)
    stopped = runtime.control.submit_and_apply(
        ControlKind.STOP,
        claim_owner="stop-control",
        actor_id="operator",
        reason="rolling shutdown after in-flight work",
        idempotency_key="stop-worker-a-after-drain",
        worker_id="worker-a",
    )
    assert stopped.phase is ControlPhase.APPLIED
    assert stopped.effect["stop_after_drain"] is True
    assert runtime.pool.store.require_worker("worker-a").state is WorkerLifecycleState.DRAINING
    assert runtime.pool.store.require_lease(lease.lease_id).state is LeaseState.DRAINING
    assert runtime.repository.get_binding(admission.binding.binding_id).phase is AdmissionPhase.DRAINING

    restarted_pool = _pool(tmp_path / "pool.sqlite3")
    graph_store = GraphStateStore(tmp_path / "graph.sqlite3")
    graph_store.initialize()
    restarted = WorkerPoolIntegrationRuntime(
        restarted_pool,
        GraphStateCustody(graph_store),
    )
    authorized = restarted.execution_gate.authorize_projection(
        admitted_projection,
        expected_task_id="task-graceful-stop",
        expected_run_id="run-integration",
        expected_session_id="session-integration",
    )
    assert authorized["lease_state"] == "draining"
    restarted.complete(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        outcome=ExecutionOutcome.SUCCEEDED,
        summary="in-flight work completed before shutdown",
    )
    final_worker = restarted.pool.store.require_worker("worker-a")
    assert final_worker.state is WorkerLifecycleState.STOPPED
    assert "stop_after_drain" not in final_worker.metadata
    with pytest.raises((WorkerPoolError, ValueError), match="no worker|capacity|registered"):
        _admit(restarted, "task-after-stop", preferred=("worker-a",))


def test_wake_dispatch_is_target_scoped_acknowledged_after_accept_and_fail_closed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime = _runtime(tmp_path, workers=("worker-a", "worker-b"))
    runtime.pool.lifecycle.park("worker-a")
    runtime.pool.lifecycle.park("worker-b")
    _, wake_a = runtime.pool.inbox.enqueue(
        worker_id="worker-a",
        task_id="wake-task-a",
        run_id="run-integration",
        message_kind="resume",
        payload={"task_id": "wake-task-a"},
    )
    _, wake_b = runtime.pool.inbox.enqueue(
        worker_id="worker-b",
        task_id="wake-task-b",
        run_id="run-integration",
        message_kind="resume",
        payload={"task_id": "wake-task-b"},
    )
    assert wake_a is not None and wake_b is not None

    class RecordingWakePort:
        def __init__(self) -> None:
            self.dispatched: list[str] = []

        def is_session_active(self, worker_id: str) -> bool:
            return False

        def dispatch(self, wakeup: Any) -> dict[str, Any]:
            self.dispatched.append(wakeup.wakeup_id)
            return {"accepted": True, "task_id": wakeup.task_id}

    port = RecordingWakePort()
    runtime.control.wake_execution = port
    command = runtime.control.submit_and_apply(
        ControlKind.WAKE,
        claim_owner="wake-control",
        actor_id="operator",
        reason="resume only worker-a",
        idempotency_key="wake-target-worker-a",
        worker_id="worker-a",
    )
    assert command.phase is ControlPhase.APPLIED
    assert command.effect["dispatched_wakeup_ids"] == [wake_a.wakeup_id]
    assert port.dispatched == [wake_a.wakeup_id]
    assert runtime.pool.store.list_wakeups(worker_id="worker-a")[0].state is WakeupState.DISPATCHED
    assert runtime.pool.store.list_wakeups(worker_id="worker-b")[0].state is WakeupState.QUEUED
    assert runtime.pool.store.require_worker("worker-a").state is WorkerLifecycleState.IDLE
    assert runtime.pool.store.require_worker("worker-b").state is WorkerLifecycleState.PARKED

    monkeypatch.setenv("ZYRA_WORKER_WAKE_DISPATCH_DISABLED", "1")
    blocked = runtime.control.submit_and_apply(
        ControlKind.WAKE,
        claim_owner="wake-control",
        actor_id="operator",
        reason="disabled port must not consume worker-b wake",
        idempotency_key="wake-target-worker-b-disabled",
        worker_id="worker-b",
    )
    assert blocked.phase is ControlPhase.FAILED
    assert "no enabled execution dispatch port" in blocked.error
    assert runtime.pool.store.list_wakeups(worker_id="worker-b")[0].state is WakeupState.QUEUED
    assert runtime.pool.store.require_worker("worker-b").state is WorkerLifecycleState.PARKED


def test_park_revive_retain_live_lease_and_terminal_attempt_cannot_be_parked(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _, _, admission, lease = _admit(runtime, "task-park")
    runtime.start(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        backend_dispatch_id="backend-park",
    )
    parked = runtime.control.submit_and_apply(
        ControlKind.PARK,
        claim_owner="park-control",
        actor_id="operator",
        reason="yield foreground while preserving the physical reservation",
        idempotency_key="park:task-park:1",
        task_id="task-park",
        binding_id=admission.binding.binding_id,
    )
    assert parked.phase is ControlPhase.APPLIED
    assert runtime.repository.get_binding(admission.binding.binding_id).phase is AdmissionPhase.PARKED
    assert runtime.pool.store.require_lease(lease.lease_id).state is LeaseState.ACTIVE
    with pytest.raises(WorkerPoolError, match="revived before it can start"):
        runtime.start(
            admission.binding.binding_id,
            fence_token=lease.fence_token,
            backend_dispatch_id="backend-park-bypass",
        )

    revived = runtime.control.submit_and_apply(
        ControlKind.REVIVE,
        claim_owner="park-control",
        actor_id="operator",
        reason="foreground permit became available",
        idempotency_key="revive:task-park:1",
        task_id="task-park",
        binding_id=admission.binding.binding_id,
    )
    assert revived.phase is ControlPhase.APPLIED
    assert runtime.repository.get_binding(admission.binding.binding_id).phase is AdmissionPhase.DISPATCHED

    runtime.control.submit_and_apply(
        ControlKind.CANCEL,
        claim_owner="park-control",
        actor_id="operator",
        reason="cancel after revive",
        idempotency_key="cancel:task-park:1",
        task_id="task-park",
        run_id="run-integration",
        binding_id=admission.binding.binding_id,
    )
    invalid = runtime.control.submit_and_apply(
        ControlKind.PARK,
        claim_owner="park-control",
        actor_id="operator",
        reason="terminal attempt cannot be parked",
        idempotency_key="park:task-park:terminal",
        task_id="task-park",
        binding_id=admission.binding.binding_id,
    )
    assert invalid.phase is ControlPhase.FAILED
    assert "live dispatched physical binding" in invalid.error


def test_cancel_is_durable_blocks_completion_and_recovery_handoff_names_07c_owner(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    class BrokenProcessProjection:
        def cancel(self, task_id: str, *, reason: str) -> dict[str, Any]:
            raise RuntimeError(f"process projection unavailable for {task_id}: {reason}")

        def park(self, task_id: str, *, reason: str) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError((task_id, reason))

        def revive(self, task_id: str) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError(task_id)

    runtime.control.projection_control = BrokenProcessProjection()
    _, _, admission, lease = _admit(runtime, "task-cancel")
    runtime.start(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        backend_dispatch_id="backend-cancel",
    )
    pending = runtime.control.submit_cancel(
        task_id="task-cancel",
        run_id="run-integration",
        reason="operator cancelled a running tool loop",
        actor_id="operator",
        idempotency_key="cancel:task-cancel:1",
    )
    assert pending.phase is ControlPhase.PENDING
    report = runtime.control.recover_pending(claim_owner="control-recovery")
    applied = next(item for item in report.applied if item.command_id == pending.command_id)
    assert applied.effect["cancellation"]["changed"] is True
    assert "process projection unavailable" in applied.effect["typescript_projection_error"]
    assert runtime.pool.store.require_lease(lease.lease_id).state is LeaseState.CANCELLED
    assert runtime.repository.get_binding(admission.binding.binding_id).phase is AdmissionPhase.CANCELLED
    with pytest.raises((LeaseFenced, WorkerPoolError)):
        runtime.complete(
            admission.binding.binding_id,
            fence_token=lease.fence_token,
            outcome=ExecutionOutcome.SUCCEEDED,
            summary="cancelled work must not commit",
        )

    handoff = runtime.recovery_handoff.build(task_id="task-cancel")
    assert handoff.canonical_pool_revision == runtime.pool.store.revision
    assert handoff.to_dict()["consumer"] == "M1-S07C.RecoveryPlanner"
    m2 = runtime.projection.handoff(task_id="task-cancel", limit=1000)
    assert m2.events
    assert m2.custody["canonical_state_owner"] == "python.WorkerPoolStore"
    assert runtime.projection.causal_chain("task-cancel")
    api = WorkerPoolApiService(runtime.pool, runtime.graph_custody)
    response = api.route_get(
        ("worker-pool", "recovery-handoff"),
        {"task_id": "task-cancel"},
    )
    assert response is not None and response.status.value == 200
    assert response.body["consumer"] == "M1-S07C.RecoveryPlanner"


def test_targeted_binding_cancel_uses_child_task_custody_not_parent_route_task(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _, _, admission, lease = _admit(runtime, "child-task")
    runtime.start(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        backend_dispatch_id="backend-child",
    )

    command = runtime.control.submit_and_apply(
        ControlKind.CANCEL,
        claim_owner="parent-fanout-cleanup",
        actor_id="parent-fanout-cleanup",
        reason="release one completed fanout child",
        idempotency_key="cancel:parent-route:child-binding",
        task_id="parent-route-task",
        run_id="run-integration",
        lease_id=lease.lease_id,
        binding_id=admission.binding.binding_id,
    )

    assert command.phase is ControlPhase.APPLIED
    assert command.effect["cancellation"]["task_id"] == "child-task"
    assert command.effect["cancellation"]["cancelled_lease_ids"] == [lease.lease_id]
    assert runtime.pool.store.require_lease(lease.lease_id).state is LeaseState.CANCELLED
    assert runtime.repository.get_binding(admission.binding.binding_id).phase is AdmissionPhase.CANCELLED


def test_checkpoint_restart_restores_active_draining_and_pending_control_without_process_registry(
    tmp_path: Path,
) -> None:
    pool_path = tmp_path / "pool.sqlite3"
    graph_path = tmp_path / "graph.sqlite3"
    runtime = _runtime(tmp_path)
    _, _, admission, lease = _admit(runtime, "task-restart")
    runtime.start(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        backend_dispatch_id="backend-before-restart",
    )
    runtime.control.submit_and_apply(
        ControlKind.DRAIN,
        claim_owner="before-restart",
        actor_id="operator",
        reason="drain survives process restart",
        idempotency_key="drain:restart:1",
        worker_id="worker-a",
    )
    pending = runtime.control.submit_cancel(
        task_id="task-restart",
        run_id="run-integration",
        reason="pending cancel survives process restart",
        actor_id="operator",
        idempotency_key="cancel:restart:1",
    )
    initial_checkpoint = runtime.checkpoints.create(
        run_id="run-integration",
        graph_ids=("graph-integration",),
        foreign_checkpoint_refs=(
            {"owner": "logical-task-checkpoint", "checkpoint_id": "logical-checkpoint-1"},
        ),
    )
    assert runtime.checkpoints.restore(initial_checkpoint.checkpoint_id).exact is True
    runtime.progress(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        sequence=1,
        payload={"after_checkpoint": True},
    )
    with pytest.raises(WorkerPoolError, match="cannot be restored exactly"):
        runtime.checkpoints.restore(initial_checkpoint.checkpoint_id)
    checkpoint = runtime.checkpoints.create(
        run_id="run-integration",
        graph_ids=("graph-integration",),
        foreign_checkpoint_refs=(
            {"owner": "logical-task-checkpoint", "checkpoint_id": "logical-checkpoint-2"},
        ),
        previous_checkpoint_id=initial_checkpoint.checkpoint_id,
    )

    restarted_pool = _pool(pool_path)
    restarted_store = GraphStateStore(graph_path)
    restarted_store.initialize()
    restarted = WorkerPoolIntegrationRuntime(
        restarted_pool,
        GraphStateCustody(restarted_store),
    )
    recovered = restarted.startup_recover(run_id="run-integration")
    assert recovered.process_registry_restored is False
    assert pending.command_id in recovered.recovered_control_ids
    assert recovered.restore is not None and recovered.restore.exact is True
    assert recovered.restore.replay_digest == runtime.checkpoints.restore(
        checkpoint.checkpoint_id
    ).replay_digest
    assert restarted.pool.store.require_worker("worker-a").state is WorkerLifecycleState.DRAINING
    assert restarted.pool.store.require_lease(lease.lease_id).state is LeaseState.DRAINING
    assert restarted.graph_custody.current("graph-integration").node_map[
        "runtime-task-restart"
    ].worker_lease_ref == lease.lease_id
    applied = restarted.control.recover_pending(claim_owner="after-restart")
    assert pending.command_id in {item.command_id for item in applied.applied}
    assert restarted.pool.store.require_lease(lease.lease_id).state is LeaseState.CANCELLED


def test_nested_alias_attack_single_yield_and_randomized_replay_are_deterministic(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, workers=("worker-a", "worker-b"))
    mutable_signal: dict[str, Any] = {
        "signalId": "memory-route-1",
        "priority": "high",
        "payload": {
            "preferred_worker_ids": ["worker-b"],
            "allowed_locations": ["local"],
            "reason": "06C prefers the warm worker",
        },
    }
    context = _context(runtime, "task-alias", preferred=("worker-a", "worker-b"))
    plan = runtime.scheduler.plan(context, memory_signals=(mutable_signal,))
    digest = plan.plan_digest
    mutable_signal["payload"]["preferred_worker_ids"].append("worker-a")
    mutable_signal["payload"]["allowed_locations"][0] = "cloud"
    assert plan.plan_digest == digest
    assert plan.directives[0].preferred_worker_ids == ("worker-b",)

    _, admission = runtime.scheduler.admit(
        context,
        memory_signals=(
            {
                "signalId": "memory-route-1",
                "priority": "high",
                "payload": {
                    "preferred_worker_ids": ["worker-b"],
                    "allowed_locations": ["local"],
                },
            },
        ),
    )
    lease = runtime.pool.store.require_lease(admission.binding.lease_id)
    runtime.start(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        backend_dispatch_id="backend-alias",
    )
    payload = {"nested": {"values": [1, {"stable": True}]}}
    first = runtime.yield_once(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        kind=YieldKind.RESULT,
        summary="one immutable result",
        payload=payload,
    )
    payload["nested"]["values"][1]["stable"] = False
    assert first.payload["nested"]["values"][1]["stable"] is True
    replay = runtime.yield_once(
        admission.binding.binding_id,
        fence_token=lease.fence_token,
        kind=YieldKind.RESULT,
        summary="one immutable result",
        payload={"nested": {"values": [1, {"stable": True}]}},
    )
    assert replay.yield_id == first.yield_id
    with pytest.raises(WorkerPoolError, match="second typed yield"):
        runtime.yield_once(
            admission.binding.binding_id,
            fence_token=lease.fence_token,
            kind=YieldKind.RESULT,
            summary="different result",
            payload={"nested": {"values": [2]}},
        )

    bindings = list(runtime.repository.list_bindings())
    controls = list(runtime.repository.list_controls())
    graph_refs = [runtime.topology.version_ref("graph-integration").to_dict()]
    expected = runtime.checkpoints.replay_digest(
        bindings=bindings,
        controls=controls,
        draining_worker_ids=(),
        graph_refs=graph_refs,
    )
    for seed in range(20):
        random.Random(seed).shuffle(bindings)
        random.Random(seed + 100).shuffle(controls)
        assert runtime.checkpoints.replay_digest(
            bindings=bindings,
            controls=controls,
            draining_worker_ids=(),
            graph_refs=reversed(graph_refs),
        ) == expected

    graph_revision = runtime.topology.version_ref("graph-integration").revision
    binding_version = runtime.repository.get_binding(admission.binding.binding_id).version
    replay_plan, replay_admission = runtime.scheduler.admit(
        context,
        memory_signals=(
            {
                "signalId": "memory-route-1",
                "priority": "high",
                "payload": {
                    "preferred_worker_ids": ["worker-b"],
                    "allowed_locations": ["local"],
                },
            },
        ),
    )
    assert replay_plan.plan_digest
    assert replay_admission.reused is True
    assert runtime.topology.version_ref("graph-integration").revision == graph_revision
    assert runtime.repository.get_binding(admission.binding.binding_id).version == binding_version


def test_session_capacity_and_disable_matrix_fail_closed_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, workers=("worker-a", "worker-b"))
    runtime.policy = AdmissionPolicy(maximum_active_per_session=1)
    runtime.admission.policy = runtime.policy
    runtime.admission.capacity.policy = runtime.policy

    rollback_context = _context(runtime, "binding-rollback", session_id="rollback-session")
    original_insert_binding = runtime.repository.insert_binding

    def reject_binding(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("injected integration binding write failure")

    monkeypatch.setattr(runtime.repository, "insert_binding", reject_binding)
    with pytest.raises(RuntimeError, match="injected integration binding write failure"):
        runtime.scheduler.admit(rollback_context)
    assert runtime.pool.store.latest_attempt("binding-rollback") is None
    assert not tuple(
        lease for lease in runtime.pool.store.list_leases() if lease.task_id == "binding-rollback"
    )
    monkeypatch.setattr(runtime.repository, "insert_binding", original_insert_binding)

    missing_context = _context(runtime, "missing-graph-node", session_id="missing-node-session")
    missing_plan = runtime.scheduler.plan(missing_context)
    with pytest.raises(WorkerPoolError, match="unknown dynamic graph node"):
        runtime.admit(missing_plan.request, graph_node_id="runtime-node-does-not-exist")
    assert runtime.pool.store.latest_attempt("missing-graph-node") is None

    _, _, first, _ = _admit(runtime, "capacity-one", session_id="session-one")
    assert first.binding.owner_session_id == "session-one"
    with pytest.raises((WorkerPoolError, ValueError), match="no worker|capacity"):
        _admit(runtime, "capacity-two", session_id="session-one")

    monkeypatch.setenv("ZYRA_WORKER_POOL_INTEGRATION_DISABLED", "1")
    with pytest.raises(WorkerPoolError, match="disabled"):
        _admit(runtime, "disabled-integration", session_id="session-two")
    monkeypatch.delenv("ZYRA_WORKER_POOL_INTEGRATION_DISABLED")

    monkeypatch.setenv("ZYRA_WORKER_LEASE_STORE_DISABLED", "1")
    with pytest.raises(WorkerPoolError, match="lease store is disabled") as disabled_store:
        _admit(runtime, "disabled-lease-store", session_id="session-store")
    assert disabled_store.value.detail.metadata["failure_owner"] == "WorkerPoolStore"
    monkeypatch.delenv("ZYRA_WORKER_LEASE_STORE_DISABLED")

    monkeypatch.setenv("ZYRA_DYNAMIC_GRAPH_COMMIT_DISABLED", "1")
    with pytest.raises(WorkerPoolError, match="disabled"):
        runtime.add_runtime_node_for_requirement(
            graph_id="graph-integration",
            logical_task_id="disabled-graph",
            role="worker",
            capabilities=("agent_task",),
        )
    monkeypatch.delenv("ZYRA_DYNAMIC_GRAPH_COMMIT_DISABLED")

    # An edge-only request is rejected even though two local workers exist.
    context = _context(
        runtime,
        "edge-without-edge",
        session_id="session-edge",
        locations=(WorkerLocation.EDGE,),
        edge_only=True,
    )
    with pytest.raises((WorkerPoolError, ValueError), match="no worker|edge"):
        runtime.scheduler.admit(context)
    local_context = _context(runtime, "memory-location-conflict", session_id="session-memory")
    rejected = runtime.scheduler.plan(
        local_context,
        memory_signals=(
            {
                "signalId": "memory-cloud-only",
                "payload": {"allowed_locations": "cloud"},
            },
        ),
    )
    assert rejected.accepted is False
    assert any("empty intersection" in reason for reason in rejected.reasons)
    assert runtime.invariants.custody_map()["langgraph_production_owner"] is False


class _MissingLogicalTaskStore:
    def exists(self, task_id: str, *, revision: int) -> bool:
        return False


def test_logical_task_disconnect_and_atomic_session_quota_fail_closed(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path / "pool.sqlite3")
    _register_local(pool, "worker-a", slots=8)
    custody = _custody(tmp_path / "graph.sqlite3")
    disconnected = WorkerPoolIntegrationRuntime(
        pool,
        custody,
        admission_ports=AdmissionPorts(logical_tasks=_MissingLogicalTaskStore()),
    )
    with pytest.raises(WorkerPoolError, match="logical task store"):
        disconnected.scheduler.admit(_context(disconnected, "missing-logical-task"))

    runtime = WorkerPoolIntegrationRuntime(
        pool,
        custody,
        policy=AdmissionPolicy(
            maximum_active_per_session=1,
            maximum_active_per_run=8,
            maximum_active_per_worker=8,
        ),
    )
    requests = tuple(
        runtime.scheduler.plan(
            _context(runtime, f"concurrent-{index}", session_id="atomic-session")
        ).request
        for index in range(4)
    )
    barrier = threading.Barrier(len(requests))

    def admit(request: DispatchAdmissionRequest) -> str:
        barrier.wait(timeout=10)
        try:
            return runtime.admission.admit(request).binding.lease_id
        except WorkerPoolError as error:
            return error.code.value

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        outcomes = tuple(executor.map(admit, requests))
    successful = tuple(item for item in outcomes if item.startswith("lease_"))
    assert len(successful) == 1
    assert outcomes.count("resource_exhausted") == len(requests) - 1
    active = pool.store.list_leases(states=(LeaseState.ACTIVE, LeaseState.DRAINING))
    assert len(tuple(item for item in active if item.owner_session_id == "atomic-session")) == 1


def test_m2_projection_cursor_filters_interleaved_runs_without_skipping(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    start = runtime.pool.store.journal_head_sequence()
    _admit(runtime, "cursor-primary-a", session_id="cursor-a")
    _admit(
        runtime,
        "cursor-other",
        session_id="cursor-other",
        run_id="run-other",
    )
    _admit(runtime, "cursor-primary-b", session_id="cursor-b")

    after = start
    events = []
    for _ in range(100):
        handoff = runtime.projection.handoff(
            after_sequence=after,
            limit=2,
            run_id="run-integration",
        )
        events.extend(handoff.events)
        if not handoff.has_more:
            break
        assert handoff.cursor.journal_sequence > after
        after = handoff.cursor.journal_sequence
    else:  # pragma: no cover - explicit infinite pagination guard.
        raise AssertionError("worker projection cursor did not converge")

    assert events
    assert all(item.run_id in {"", "run-integration"} for item in events)
    task_ids = {item.task_id for item in events}
    assert "cursor-primary-a" in task_ids
    assert "cursor-primary-b" in task_ids
    assert "cursor-other" not in task_ids
    with pytest.raises(ValueError, match="checksummed cursor"):
        runtime.projection.resume(
            {"journal_sequence": start, "pool_revision": 0, "graph_revisions": {}},
            run_id="run-integration",
        )


def test_heartbeat_loss_updates_05d_health_persists_07c_evidence_and_blocks_route(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path / "pool.sqlite3")
    _register_local(pool, "worker-a")
    custody = _custody(tmp_path / "graph.sqlite3")
    backend_health = BackendRegistryHealthAdapter(tmp_path / "backend.sqlite3")
    api = WorkerPoolApiService(
        pool,
        custody,
        backend_health=backend_health,
    )
    runtime = api.integration
    try:
        _, _, admission, lease = _admit(runtime, "task-heartbeat-route")
        runtime.start(
            admission.binding.binding_id,
            fence_token=lease.fence_token,
            backend_dispatch_id="backend-heartbeat-route",
        )
        current = pool.store.require_worker("worker-a")
        stale = current.advance(
            current.state,
            last_heartbeat_at=utc_iso(parse_utc(utc_iso()) - timedelta(seconds=60)),
        )
        pool.store.update_worker(
            stale,
            expected_version=current.version,
            operation="test_force_worker_heartbeat_loss",
        )

        response = api.route_post(("worker-pool", "health", "sweep"), {})
        assert response is not None and response.status is HTTPStatus.OK
        assert response.body["failures"] == []
        assert response.body["signals"][0]["route_id"] == "local-code-worker"
        assert response.body["signals"][0]["status"] == "unavailable"
        assert response.body["backend_receipts"][0]["changed"] is True
        assert backend_health.health("local-code-worker") is RouteHealth.UNAVAILABLE
        assert pool.store.require_lease(lease.lease_id).state is LeaseState.EXPIRED
        assert runtime.repository.get_binding(admission.binding.binding_id).phase is AdmissionPhase.LOST

        recovery = runtime.repository.list_recovery(task_id="task-heartbeat-route")
        assert len(recovery) == 1
        assert recovery[0].signal_kind == "worker_lost"
        assert recovery[0].disposition.value == "reassign"
        handoff = runtime.recovery_handoff.build(task_id="task-heartbeat-route")
        assert any(item.signal_kind.value == "lease_expired" for item in handoff.evidence)
        assert any(item.signal_kind.value == "worker_lost" for item in handoff.evidence)

        # A fresh physical worker is otherwise capable, but 05D route health
        # remains canonical and prevents a silent fallback around the loss.
        _register_local(pool, "worker-b")
        rejected = runtime.scheduler.plan(
            _context(runtime, "task-route-blocked", preferred=("worker-b",))
        )
        assert rejected.accepted is False
        assert rejected.candidates
        assert all(item.route_health is RouteHealth.UNAVAILABLE for item in rejected.candidates)

        # Recovery is also explicit and reversible: the lost worker must pass
        # through lifecycle start and a fresh attested heartbeat.  Its durable
        # LOST binding supplies route correlation without creating a mirror.
        pool.lifecycle.start("worker-a")
        pool.heartbeat_local_worker("worker-a", sequence=2)
        restored = runtime.health_bridge.sweep()
        assert restored.backend_receipts[0].status is RouteHealth.HEALTHY
        assert backend_health.health("local-code-worker") is RouteHealth.HEALTHY
        accepted = runtime.scheduler.plan(
            _context(runtime, "task-route-restored", preferred=("worker-b",))
        )
        assert accepted.accepted is True
    finally:
        api.close()


def _register_edge(
    pool: WorkerPoolFoundationRuntime,
    tmp_path: Path,
    worker_id: str,
) -> tuple[EdgeWorkerProcessConnector, EdgeWorkerRegistrationRuntime, IntegratedEdgeExecutionAdapter]:
    connector = EdgeWorkerProcessConnector(
        worker_id=worker_id,
        secret=SECRET,
        python_executable=sys.executable,
        package_roots=_package_roots(),
        startup_timeout_seconds=10,
        request_timeout_seconds=10,
    )
    registration = EdgeWorkerRegistrationRuntime(
        connector,
        pool.lifecycle,
        pool.heartbeats,
        attestor=CapabilityAttestor(SECRET),
    )
    registration.register(
        backend=BackendCapability(
            backend_id=f"edge-sandbox-{worker_id}",
            backend_kind="sandbox_gateway",
            enabled=True,
            healthy=True,
            capabilities=("edge_execution", "artifact_return", "agent_task"),
            tool_ids=("edge.echo_artifact", "edge.hash_artifact", "edge.json_transform"),
        ),
        resources=ResourceVector(cpu_cores=1, memory_mb=512, process_slots=2),
    )
    gateway = EdgeWorkerGatewayRuntime(
        connector,
        pool.leases,
        artifact_root=tmp_path / "artifacts",
    )
    return connector, registration, IntegratedEdgeExecutionAdapter(gateway, registration)


def test_real_edge_only_execution_lost_worker_failover_and_old_fence_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _pool(tmp_path / "pool.sqlite3")
    custody = _custody(tmp_path / "graph.sqlite3")
    first_connector, first_registration, first_adapter = _register_edge(
        pool, tmp_path, "edge-worker-a"
    )
    second_connector, second_registration, second_adapter = _register_edge(
        pool, tmp_path, "edge-worker-b"
    )
    runtime = WorkerPoolIntegrationRuntime(
        pool,
        custody,
        edge_execution=first_adapter,
        policy=AdmissionPolicy(maximum_active_per_session=4, maximum_active_per_worker=2),
    )
    try:
        context = _context(
            runtime,
            "edge-live-task",
            locations=(WorkerLocation.EDGE,),
            edge_only=True,
            preferred=("edge-worker-a",),
        )
        _, first = runtime.scheduler.admit(context)
        first_lease = pool.store.require_lease(first.binding.lease_id)
        monkeypatch.setenv("ZYRA_EDGE_POOL_DISABLED", "1")
        with pytest.raises(WorkerPoolError, match="cannot use a local fallback"):
            runtime.execute_edge(
                first.binding.binding_id,
                fence_token=first_lease.fence_token,
                payload={"operation": "echo_artifact", "content": "must not fallback"},
            )
        monkeypatch.delenv("ZYRA_EDGE_POOL_DISABLED")
        edge_receipt = runtime.execute_edge(
            first.binding.binding_id,
            fence_token=first_lease.fence_token,
            payload={
                "operation": "echo_artifact",
                "input_payload": {"content": "edge attempt one"},
                "artifact_name": "attempt-one.txt",
                "job_id": "edge-job-attempt-one",
            },
        )
        assert edge_receipt.accepted is True
        assert Path(edge_receipt.result["artifact_path"]).read_text(encoding="utf-8") == "edge attempt one"
        assert edge_receipt.result["telemetry"]["worker_process_pid"] != os.getpid()

        first_connector.kill()
        successor_request = DispatchAdmissionRequest(
            task_id="edge-live-task",
            run_id="run-integration",
            owner_session_id="session-integration",
            logical_attempt=2,
            attempt_number=2,
            requirement=context and runtime.scheduler.plan(context).request.requirement,
            foreign_refs=make_foreign_refs(
                task_id="edge-live-task",
                task_revision=2,
                backend_route_id="edge-sandbox",
                workspace_ref="workspace:edge-live-task",
                gateway_ref="sandbox-gateway",
                graph_id="graph-integration",
                graph_revision=runtime.topology.version_ref("graph-integration").revision,
            ),
            execution_mode=DispatchMode.BACKGROUND,
            preferred_worker_ids=("edge-worker-b",),
            excluded_worker_ids=("edge-worker-a",),
            edge_only=True,
            idempotency_key="edge-failover:2",
            recovery_reason="edge worker process was lost",
            metadata={"graph_node_id": "runtime-edge-live-task"},
        )
        failover = runtime.failover(
            first.binding.binding_id,
            successor_request=successor_request,
            failure_reason="edge worker process was lost",
            graph_node_id="runtime-edge-live-task",
        )
        assert failover.failed_binding.phase is AdmissionPhase.SUPERSEDED
        assert failover.successor.binding.worker_id == "edge-worker-b"
        assert failover.recovery.metadata["local_fallback_used"] is False
        replay_revision = runtime.topology.version_ref("graph-integration").revision
        replayed_failover = runtime.failover(
            first.binding.binding_id,
            successor_request=successor_request,
            failure_reason="edge worker process was lost",
            graph_node_id="runtime-edge-live-task",
        )
        assert replayed_failover.successor.reused is True
        assert replayed_failover.successor.binding.binding_id == failover.successor.binding.binding_id
        assert runtime.topology.version_ref("graph-integration").revision == replay_revision
        with pytest.raises((LeaseFenced, WorkerPoolError)):
            runtime.complete(
                first.binding.binding_id,
                fence_token=first_lease.fence_token,
                outcome=ExecutionOutcome.SUCCEEDED,
                summary="stale edge attempt must not commit",
            )

        runtime.edge_execution = second_adapter
        successor = failover.successor.binding
        successor_lease = pool.store.require_lease(successor.lease_id)
        second_receipt = runtime.execute_edge(
            successor.binding_id,
            fence_token=successor_lease.fence_token,
            payload={
                "operation": "echo_artifact",
                "input_payload": {"content": "edge attempt two wins"},
                "artifact_name": "attempt-two.txt",
                "job_id": "edge-job-attempt-two",
            },
        )
        completed = runtime.complete(
            successor.binding_id,
            fence_token=successor_lease.fence_token,
            outcome=ExecutionOutcome.SUCCEEDED,
            summary="successor edge attempt committed",
            artifact_refs=second_receipt.artifact_refs,
            gateway_receipt_ref=second_receipt.gateway_action_ref,
            result_payload={"winner": successor.attempt_id},
        )
        assert completed.binding.phase is AdmissionPhase.SUCCEEDED
        assert pool.store.receipts_for_task("edge-live-task")[-1].attempt_id == successor.attempt_id
        assert runtime.invariants.assert_safe(task_id="edge-live-task").ok is True
        handoff = runtime.recovery_handoff.build(task_id="edge-live-task")
        assert any(item.signal_kind.value == "lease_expired" for item in handoff.evidence)
    finally:
        first_registration.stop()
        second_registration.stop()
