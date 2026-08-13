from __future__ import annotations

import json
import os
import threading
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from apps.api.zyra_api import main as api_main
from zyra_core import PlanNodeStatus
from zyra_orchestration import ensure_default_graph
from zyra_scheduler.worker_pool import ExecutionOutcome


def test_api_owned_local_worker_heartbeat_refresh_is_monotonic(
    tmp_path: Path,
) -> None:
    with _api(tmp_path):
        pool_api = api_main.get_worker_pool_api()
        registration = pool_api.ensure_default_local_worker()
        worker_id = registration.worker.worker_id
        before = pool_api.pool.store.latest_heartbeat(worker_id)
        assert before is not None

        pool_api.refresh_owned_local_worker_heartbeat(worker_id)

        after = pool_api.pool.store.latest_heartbeat(worker_id)
        assert after is not None
        assert after.sequence == before.sequence + 1
        assert after.process_identity == f"local-pid-{os.getpid()}"


def test_api_owned_successor_heartbeat_refreshes_on_task_reentry_and_settlement(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        task = _post(
            base_url,
            "/tasks",
            {"goal": "Route through one API-owned successor.", "auto_run": False},
        )["task"]
        state = api_main.get_store().load_task(task["task_id"])
        assert state is not None
        pool_api = api_main.get_worker_pool_api()
        successor_id = "api-owned-successor"
        pool_api.ensure_default_local_worker(worker_id=successor_id)

        original = dict(state.metadata["worker_pool"])
        pool_api.pool.leases.cancel(
            original["lease_id"],
            reason="controlled successor regression setup",
        )
        pool_api.reconcile_task_graph_binding(
            state,
            reason="controlled successor regression setup",
            actor_id="test-worker-pool",
            causation_id="test-api-owned-successor",
        )
        acquisition = pool_api.acquire_for_task(
            state,
            payload={
                "preferred_worker_ids": [successor_id],
                "excluded_worker_ids": ["local-code-worker"],
                "idempotency_key": "test-api-owned-successor-acquire",
            },
        )
        assert acquisition.worker.worker_id == successor_id
        before_reentry = pool_api.pool.store.latest_heartbeat(successor_id)
        assert before_reentry is not None

        assert pool_api.ensure_task_lease(state) is None

        after_reentry = pool_api.pool.store.latest_heartbeat(successor_id)
        assert after_reentry is not None
        assert after_reentry.sequence == before_reentry.sequence + 1

        receipt = pool_api.finalize_task(
            state,
            success=True,
            summary="API-owned successor settled",
        )

        after_settlement = pool_api.pool.store.latest_heartbeat(successor_id)
        assert receipt is not None
        assert after_settlement is not None
        assert after_settlement.sequence == after_reentry.sequence + 1


def test_api_owned_successor_refreshes_immediately_before_e03_gate(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        task = _post(
            base_url,
            "/tasks",
            {"goal": "Authorize one successor dispatch.", "auto_run": False},
        )["task"]
        state = api_main.get_store().load_task(task["task_id"])
        assert state is not None
        pool_api = api_main.get_worker_pool_api()
        successor_id = "api-owned-e03-successor"
        pool_api.ensure_default_local_worker(worker_id=successor_id)
        before = pool_api.pool.store.latest_heartbeat(successor_id)
        assert before is not None
        projection = {
            "task_id": f"{state.task_id}-child",
            "worker_id": successor_id,
        }

        with patch.object(
            pool_api.integration.execution_gate,
            "authorize_projection",
            return_value={"authorized": True},
        ) as authorize:
            result = api_main._authorize_typescript_agent_physical_execution(
                state,
                tool_name="Agent",
                arguments={
                    "task_id": projection["task_id"],
                    "physical_dispatch": projection,
                },
                parent_session_id=f"task:{state.task_id}",
            )

        after = pool_api.pool.store.latest_heartbeat(successor_id)
        assert result == {"authorized": True}
        assert after is not None
        assert after.sequence == before.sequence + 1
        authorize.assert_called_once()


def test_typescript_agent_context_budget_preserves_a_real_work_phase() -> None:
    assert api_main.TYPESCRIPT_AGENT_QUERY_CONTEXT_BUDGET_CHARS == 400_000
    assert api_main.TYPESCRIPT_AGENT_QUERY_CONTEXT_BUDGET_CHARS >= 10 * 32_000


def test_api_owned_edge_worker_heartbeat_refresh_is_monotonic(
    tmp_path: Path,
) -> None:
    with _api(tmp_path):
        pool_api = api_main.get_worker_pool_api()
        worker_id, _adapter = api_main.ensure_api_edge_worker(pool_api)
        before = pool_api.pool.store.latest_heartbeat(worker_id)
        assert before is not None

        assert api_main.refresh_api_edge_worker_heartbeat(pool_api, worker_id)

        after = pool_api.pool.store.latest_heartbeat(worker_id)
        assert after is not None
        assert after.sequence > before.sequence
        assert after.process_identity == before.process_identity


def test_parent_worker_pool_cancel_releases_active_subagent_descendants(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        task = _post(
            base_url,
            "/tasks",
            {"goal": "Cancel the complete physical task tree.", "auto_run": False},
        )["task"]
        state = api_main.get_store().load_task(task["task_id"])
        assert state is not None
        child_task_id = f"{state.task_id}-child"
        child, _public = api_main._acquire_subagent_physical_dispatch(
            state,
            task_id=child_task_id,
            owner_session_id=f"task:{state.task_id}",
            idempotency_key=f"test-descendant:{state.task_id}",
        )
        assert child["worker_id"] == "local-code-worker"

        cancelled = _post(
            base_url,
            f"/tasks/{state.task_id}/worker-pool-cancel",
            {
                "reason": "controlled parent tree cancellation",
                "idempotency_key": f"test-parent-cancel:{state.task_id}",
            },
        )

        pool = api_main.get_worker_pool_api().pool
        lease = pool.store.require_lease(child["lease_id"])
        binding = api_main.get_worker_pool_api().integration.repository.get_binding(
            child["integration_binding_id"]
        )
        assert cancelled["descendant_cleanup_ok"] is True
        assert len(cancelled["descendant_controls"]) == 1
        assert lease.state.value == "cancelled"
        assert binding is not None and binding.terminal


def test_task_api_uses_physical_lease_dynamic_graph_projection_and_real_cancel(tmp_path: Path) -> None:
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {"goal": "Exercise the 07A worker pool main path.", "auto_run": False},
        )
        task = created["task"]
        task_id = task["task_id"]
        physical = task["metadata"]["worker_pool"]
        assert physical["attempt_number"] == 1
        assert physical["worker_id"] == "local-code-worker"
        assert physical["graph_ref"]["revision"] >= 2

        pool = _get(base_url, "/worker-pool")
        leases = _get(base_url, f"/worker-pool/leases?task_id={task_id}")
        graph = _get(base_url, f"/worker-pool/graphs/graph:{task_id}")
        assert pool["custody"]["physical_attempt"] == "WorkerLeaseManager"
        assert any(item["lease_id"] == physical["lease_id"] for item in leases["leases"])
        execute_nodes = [
            item
            for item in graph["snapshot"]["nodes"]
            if item["metadata"].get("stage") == "execute"
        ]
        assert execute_nodes[0]["worker_lease_ref"] == physical["lease_id"]

        cancelled = _post(
            base_url,
            f"/tasks/{task_id}/cancel",
            {"reason": "verify physical cancellation"},
        )
        assert physical["lease_id"] in cancelled["worker_pool_control"]["cancelled_lease_ids"]
        after = _get(base_url, f"/worker-pool/leases?task_id={task_id}")
        selected = next(item for item in after["leases"] if item["lease_id"] == physical["lease_id"])
        assert selected["state"] == "cancelled"
        terminal_graph = _get(
            base_url,
            f"/worker-pool/graphs/graph:{task_id}",
        )
        terminal_execute = next(
            item
            for item in terminal_graph["snapshot"]["nodes"]
            if item["metadata"].get("stage") == "execute"
        )
        assert terminal_execute["state"] == "cancelled"


def test_task_cancel_survives_stale_graph_reconciliation(tmp_path: Path) -> None:
    with _api(tmp_path) as base_url:
        task = _post(
            base_url,
            "/tasks",
            {
                "goal": "Cancel even when the graph projection is concurrently stale.",
                "auto_run": False,
            },
        )["task"]
        pool_api = api_main.get_worker_pool_api()

        with patch.object(
            pool_api,
            "reconcile_task_graph_binding",
            side_effect=RuntimeError("controlled stale graph projection"),
        ):
            status, cancelled = _post_with_status(
                base_url,
                f"/tasks/{task['task_id']}/cancel",
                {"reason": "benchmark deadline elapsed"},
            )

        assert status == 200
        assert cancelled["task"]["status"] == "cancelled"
        assert cancelled["worker_pool_control"]["phase"] == "applied"
        assert cancelled["worker_pool_cancel_errors"] == [
            {
                "stage": "task_graph_reconciliation",
                "error": "controlled stale graph projection",
                "exception_type": "RuntimeError",
            }
        ]
        stored = api_main.get_store().load_task(task["task_id"])
        assert stored is not None and stored.status.value == "cancelled"


def test_expired_task_rebind_terminalizes_old_graph_before_new_binding(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        task = _post(
            base_url,
            "/tasks",
            {"goal": "Rebind one expired physical reservation.", "auto_run": False},
        )["task"]
        state = api_main.get_store().load_task(task["task_id"])
        assert state is not None
        pool_api = api_main.get_worker_pool_api()
        old = dict(state.metadata["worker_pool"])
        pool_api.pool.leases.expire(
            old["lease_id"],
            reason="controlled checkpoint expiry",
        )

        acquisition = pool_api.ensure_task_lease(
            state,
            payload={"idempotency_key": "expired-checkpoint-rebind"},
        )

        assert acquisition is not None
        assert acquisition.lease.lease_id != old["lease_id"]
        assert any(
            item["lease_id"] == old["lease_id"]
            and item["state"] == "cancelled"
            for item in state.metadata["worker_pool_graph_terminal_history"]
        )
        current = pool_api.graph_custody.current(
            state.metadata["dynamic_graph_id"]
        )
        rebound = next(
            node
            for node in current.nodes
            if node.physical_attempt_ref == acquisition.attempt.attempt_id
        )
        assert rebound.worker_lease_ref == acquisition.lease.lease_id
        assert rebound.state.value == "leased"
        old_snapshot = next(
            snapshot
            for snapshot in pool_api.graph_custody.store.list_snapshots(
                state.metadata["dynamic_graph_id"]
            )
            if any(
                node.physical_attempt_ref == old["attempt_id"]
                and node.worker_lease_ref == old["lease_id"]
                and node.state.value == "cancelled"
                for node in snapshot.nodes
            )
        )
        assert old_snapshot.commit_id != current.commit_id


def test_task_resume_recovers_graph_successor_missing_from_task_checkpoint(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        task = _post(
            base_url,
            "/tasks",
            {"goal": "Recover a successor committed before TaskState.", "auto_run": False},
        )["task"]
        state = api_main.get_store().load_task(task["task_id"])
        assert state is not None
        pool_api = api_main.get_worker_pool_api()
        prior = dict(state.metadata["worker_pool"])
        pool_api.pool.leases.expire(
            prior["lease_id"],
            reason="controlled prior reservation expiry",
        )
        pool_api.reconcile_task_graph_binding(
            state,
            reason="controlled prior reservation expiry",
            actor_id="test-worker-pool",
            causation_id="test-prior-reservation-expiry",
        )
        successor = pool_api.ensure_task_lease(
            state,
            payload={"idempotency_key": "successor-before-task-checkpoint"},
        )
        assert successor is not None
        successor_projection = dict(state.metadata["worker_pool"])

        # Model a crash after WorkerPool + graph commit but before TaskState
        # persisted the successor projection.
        state.metadata["worker_pool"] = prior

        fenced_lease_id = api_main._fence_pending_task_reservation(
            pool_api,
            state,
            reason="resume after process loss",
        )

        assert fenced_lease_id == successor.lease.lease_id
        assert state.metadata["worker_pool"]["attempt_id"] == successor.attempt.attempt_id
        assert state.metadata["worker_pool"]["lease_id"] == successor.lease.lease_id
        assert state.metadata["worker_pool"] != prior
        assert state.metadata["worker_pool"]["graph_ref"]["revision"] >= successor_projection[
            "graph_ref"
        ]["revision"]
        current = pool_api.graph_custody.current(state.metadata["dynamic_graph_id"])
        rebound = next(
            node
            for node in current.nodes
            if node.physical_attempt_ref == successor.attempt.attempt_id
        )
        assert rebound.worker_lease_ref == successor.lease.lease_id
        assert rebound.state.value == "cancelled"


def test_task_resume_recovers_multi_hop_graph_successor(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        task = _post(
            base_url,
            "/tasks",
            {"goal": "Recover several durable successors.", "auto_run": False},
        )["task"]
        state = api_main.get_store().load_task(task["task_id"])
        assert state is not None
        pool_api = api_main.get_worker_pool_api()
        original = dict(state.metadata["worker_pool"])

        for index in range(2):
            current = dict(state.metadata["worker_pool"])
            pool_api.pool.leases.expire(
                current["lease_id"],
                reason=f"controlled reservation expiry {index}",
            )
            pool_api.reconcile_task_graph_binding(
                state,
                reason=f"controlled reservation expiry {index}",
                actor_id="test-worker-pool",
                causation_id=f"test-multi-hop-expiry-{index}",
            )
            successor = pool_api.ensure_task_lease(
                state,
                payload={
                    "idempotency_key": f"multi-hop-successor-{index}",
                },
            )
            assert successor is not None

        newest = dict(state.metadata["worker_pool"])
        assert newest["attempt_number"] == original["attempt_number"] + 2
        assert pool_api._attempt_descends_from(
            attempt_id=newest["attempt_id"],
            ancestor_attempt_id=original["attempt_id"],
            task_id=state.task_id,
            run_id=state.run_id,
        )
        assert not pool_api._attempt_descends_from(
            attempt_id=newest["attempt_id"],
            ancestor_attempt_id="attempt_unrelated_sibling",
            task_id=state.task_id,
            run_id=state.run_id,
        )
        assert not pool_api._attempt_descends_from(
            attempt_id=newest["attempt_id"],
            ancestor_attempt_id=original["attempt_id"],
            task_id="task_from_another_lineage",
            run_id=state.run_id,
        )

        # Model two WorkerPool + GraphStateCustody commits followed by process
        # loss before either newer attempt reached the TaskState checkpoint.
        state.metadata["worker_pool"] = original

        fenced_lease_id = api_main._fence_pending_task_reservation(
            pool_api,
            state,
            reason="resume after multi-hop process loss",
        )

        assert fenced_lease_id == newest["lease_id"]
        assert state.metadata["worker_pool"]["attempt_id"] == newest["attempt_id"]
        assert state.metadata["worker_pool"]["lease_id"] == newest["lease_id"]
        current_graph = pool_api.graph_custody.current(
            state.metadata["dynamic_graph_id"]
        )
        rebound = next(
            node
            for node in current_graph.nodes
            if node.physical_attempt_ref == newest["attempt_id"]
        )
        assert rebound.worker_lease_ref == newest["lease_id"]
        assert rebound.state.value == "cancelled"


def test_explicit_resume_reopens_recoverable_failed_execution() -> None:
    state, _created = api_main.make_task_created_event(
        "Resume a failed physical execution from its durable recovery plan."
    )
    ensure_default_graph(state)
    state.status = PlanNodeStatus.RUNNING
    execute = next(
        node
        for node in state.plan_nodes.values()
        if node.metadata.get("stage") == "execute"
    )
    for node in state.plan_nodes.values():
        stage = str(node.metadata.get("stage") or "")
        if stage in {"plan", "route"} or node.node_id == state.root_node_id:
            node.status = PlanNodeStatus.COMPLETED
        elif stage == "execute":
            node.status = PlanNodeStatus.BLOCKED
        else:
            node.status = PlanNodeStatus.BLOCKED
    execute.metadata["worker_error"] = "Provider route renewal forked."
    execute.metadata["recovery_plan"] = {"plan_id": "recovery-resume-1"}
    state.metadata["last_recovery_plan"] = {
        "plan_id": "recovery-resume-1",
        "failure_signal_id": "failure-resume-1",
        "actions": ["local_replan", "explain_failure"],
        "affected_node_ids": [execute.node_id],
        "node_id": execute.node_id,
        "can_continue": True,
    }
    state.metadata["physical_execution_recovery_passes"] = 2
    state.metadata["physical_execution_failure_receipt"] = {"outcome": "rejected"}

    receipt = api_main._prepare_task_for_explicit_resume(
        state,
        {"resume_invocation_id": "resume-invocation-1"},
    )

    assert receipt is not None
    assert receipt["action"] == "replan"
    assert receipt["reopened_node_ids"] == [execute.node_id]
    assert state.status == PlanNodeStatus.PENDING
    assert "physical_execution_recovery_passes" not in state.metadata
    assert "physical_execution_failure_receipt" not in state.metadata
    assert "worker_error" not in execute.metadata
    assert execute.assigned_worker_id is None
    recovery_session = state.metadata["recovery_continuation_session"]
    assert recovery_session["session_id"] == receipt["recovery_session_id"]
    assert recovery_session["resume_invocation_id"] == "resume-invocation-1"
    assert recovery_session["persisted_custody_token"] is False
    assert state.metadata["runtime_hints"]["session_id"] == receipt[
        "recovery_session_id"
    ]
    assert {
        str(node.metadata.get("stage") or ""): node.status
        for node in state.plan_nodes.values()
        if node.metadata.get("stage")
    } == {
        "plan": PlanNodeStatus.COMPLETED,
        "route": PlanNodeStatus.PENDING,
        "execute": PlanNodeStatus.PENDING,
        "verify": PlanNodeStatus.PENDING,
        "finalize": PlanNodeStatus.PENDING,
    }


def test_explicit_resume_rotates_custody_for_active_execution() -> None:
    state, _created = api_main.make_task_created_event(
        "Resume an in-flight physical execution after its owner process exited."
    )
    ensure_default_graph(state)
    state.status = PlanNodeStatus.RUNNING
    state.metadata["execution_in_flight"] = {
        "schema": "zyra.task-execution-in-flight/v1",
        "receipt_id": "receipt-from-lost-owner",
    }
    state.metadata["runtime_hints"] = {"session_id": "spent-session"}
    before = {
        node.node_id: (node.status, node.assigned_worker_id)
        for node in state.plan_nodes.values()
    }

    first = api_main._prepare_task_for_explicit_resume(
        state,
        {"resume_invocation_id": "resume-active-1"},
    )
    repeated = api_main._prepare_task_for_explicit_resume(
        state,
        {"resume_invocation_id": "resume-active-1"},
    )
    second = api_main._prepare_task_for_explicit_resume(
        state,
        {"resume_invocation_id": "resume-active-2"},
    )

    assert first is not None
    assert repeated is not None
    assert second is not None
    assert first["action"] == "reacquire"
    assert first["reopened_node_ids"] == []
    assert first["changed"] is False
    assert repeated["recovery_session_id"] == first["recovery_session_id"]
    assert second["recovery_session_id"] != first["recovery_session_id"]
    assert state.metadata["runtime_hints"]["session_id"] == second[
        "recovery_session_id"
    ]
    assert state.metadata["recovery_continuation_session"][
        "persisted_custody_token"
    ] is False
    assert {
        node.node_id: (node.status, node.assigned_worker_id)
        for node in state.plan_nodes.values()
    } == before


def test_first_pending_task_run_does_not_create_recovery_session() -> None:
    state, _created = api_main.make_task_created_event(
        "Run a newly created task for the first time."
    )
    ensure_default_graph(state)

    receipt = api_main._prepare_task_for_explicit_resume(
        state,
        {"resume_invocation_id": "first-run"},
    )

    assert receipt is None
    assert "recovery_continuation_session" not in state.metadata


def test_normal_task_finalize_replays_lease_and_graph_success(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        task = _post(
            base_url,
            "/tasks",
            {"goal": "Finalize one physical task.", "auto_run": False},
        )["task"]
        state = api_main.get_store().load_task(task["task_id"])
        assert state is not None
        pool_api = api_main.get_worker_pool_api()

        first = pool_api.finalize_task(
            state,
            success=True,
            summary="controlled physical success",
        )
        state.metadata["worker_pool_receipt"] = {
            **dict(first or {}),
            "physical_dispatch_receipt": {
                "schema_version": "zyra.physical-dispatch-receipt/v2",
                "digest": "dispatch-digest",
            },
            "physical_dispatch_validation": {"real_gate_closed": True},
        }
        with pytest.raises(
            (RuntimeError, ValueError),
            match="physical dispatch|digest|contract",
        ):
            pool_api.finalize_task(
                state,
                success=True,
                summary="controlled physical success",
            )
        state.metadata["worker_pool_receipt"] = dict(first or {})
        replayed = pool_api.finalize_task(
            state,
            success=True,
            summary="controlled physical success",
        )

        assert first is not None
        assert replayed is not None
        assert replayed["receipt_id"] == first["receipt_id"]
        assert "physical_dispatch_receipt" not in replayed
        assert "physical_dispatch_validation" not in replayed
        graph = pool_api.graph_custody.current(
            state.metadata["dynamic_graph_id"]
        )
        completed = next(
            node
            for node in graph.nodes
            if node.worker_lease_ref == state.metadata["worker_pool"]["lease_id"]
        )
        assert completed.state.value == "succeeded"
        assert completed.metadata["physical_attempt_outcome_ref"] == first[
            "receipt_id"
        ]
        matching_history = [
            item
            for item in state.metadata["worker_pool_graph_terminal_history"]
            if item["lease_id"] == state.metadata["worker_pool"]["lease_id"]
        ]
        assert len(matching_history) == 1
        assert matching_history[0]["state"] == "succeeded"
        with pytest.raises(RuntimeError, match="terminal state conflicts"):
            pool_api.cancel_task_graph_binding(
                state,
                reason="controlled opposite terminal replay",
                actor_id="test-worker-pool",
                causation_id="opposite-terminal-state",
            )
        with pytest.raises(
            RuntimeError,
            match="terminal outcome receipt conflicts",
        ):
            pool_api.complete_task_graph_binding(
                state,
                succeeded=True,
                reason="controlled mismatched outcome replay",
                outcome_ref="execution_receipt_wrong",
                actor_id="test-worker-pool",
                causation_id="mismatched-outcome-ref",
            )


@pytest.mark.parametrize(
    "outcome",
    (ExecutionOutcome.CANCELLED, ExecutionOutcome.FENCED),
)
def test_receipt_backed_cancellation_replays_exact_graph_outcome_ref(
    tmp_path: Path,
    outcome: ExecutionOutcome,
) -> None:
    with _api(tmp_path) as base_url:
        task = _post(
            base_url,
            "/tasks",
            {
                "goal": "Reconcile receipt-backed physical cancellation.",
                "auto_run": False,
            },
        )["task"]
        state = api_main.get_store().load_task(task["task_id"])
        assert state is not None
        pool_api = api_main.get_worker_pool_api()
        projection = state.metadata["worker_pool"]
        lease = pool_api.pool.store.require_lease(projection["lease_id"])
        pool_api.pool.leases.start_attempt(
            lease.lease_id,
            worker_id=lease.worker_id,
            fence_token=lease.fence_token,
            fence_epoch=lease.fence_epoch,
            backend_dispatch_id=f"controlled-{outcome.value}",
        )
        receipt = pool_api.pool.leases.complete(
            lease.lease_id,
            worker_id=lease.worker_id,
            fence_token=lease.fence_token,
            fence_epoch=lease.fence_epoch,
            outcome=outcome,
            summary=f"controlled {outcome.value} outcome",
        )

        first = pool_api.reconcile_task_graph_binding(
            state,
            reason=f"reconcile {outcome.value}",
            actor_id="test-worker-pool",
            causation_id=f"receipt-backed-{outcome.value}",
        )
        replayed = pool_api.reconcile_task_graph_binding(
            state,
            reason=f"replay {outcome.value}",
            actor_id="test-worker-pool",
            causation_id=f"receipt-backed-{outcome.value}",
        )

        assert first is not None
        assert replayed is not None
        graph = pool_api.graph_custody.current(
            state.metadata["dynamic_graph_id"]
        )
        terminal = next(
            node
            for node in graph.nodes
            if node.worker_lease_ref == lease.lease_id
        )
        assert terminal.state.value == "cancelled"
        assert terminal.metadata["physical_attempt_outcome_ref"] == (
            receipt.receipt_id
        )


def test_subagent_api_admits_through_typescript_omp_gate_before_child_execution(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {"goal": "Run one E03 child under the 07A physical worker pool.", "auto_run": False},
        )
        task_id = created["task"]["task_id"]
        payload = {
            "prompt": "Return a deterministic completion without tools.",
            "execution_mode": "foreground",
            "subagent_task_id": "physical-e03-child",
            "idempotency_key": "physical-e03-child-create",
            "request_id": "physical-e03-child-request",
        }
        first_status, suspended = _post_with_status(
            base_url,
            f"/tasks/{task_id}/subagents",
            payload,
        )
        assert first_status == 409
        assert suspended["error"] == "permission_suspended"
        assert suspended["physical_receipt"] is None
        spawned = _approve_and_retry_subagent(
            base_url,
            parent=created["task"],
            payload=payload,
            suspended=suspended,
        )
        assert spawned["canonical_agent_owner"] == "typescript"
        assert spawned["record"]["status"] == "completed", json.dumps(
            spawned["record"], ensure_ascii=False, sort_keys=True
        )
        physical = spawned["physical_worker"]
        receipt = spawned["physical_receipt"]
        assert physical["typescript_dispatch_gate"] == "typescript.OmpWorkerDispatchRuntime"
        assert physical["logical_task_not_duplicated"] is True
        assert receipt["outcome"] == "succeeded"
        assert receipt["metadata"]["dispatch_gate"] == "typescript.OmpWorkerDispatchRuntime"

        leases = _get(base_url, "/worker-pool/leases?task_id=physical-e03-child")["leases"]
        selected = next(item for item in leases if item["lease_id"] == physical["lease_id"])
        assert selected["state"] == "released"
        journal = _get(base_url, "/worker-pool/journal?limit=500")["records"]
        operations = [
            item["operation"]
            for item in journal
            if item.get("task_id") == "physical-e03-child"
        ]
        assert (
            operations.index("attempt_started")
            < operations.index("worker_execution_authorized")
            < operations.index("execution_receipt_committed")
        )


def test_subagent_api_fails_closed_and_records_failed_receipt_when_omp_gate_is_disabled(
    tmp_path: Path,
) -> None:
    previous = os.environ.get("ZYRA_OMP_WORKER_CONTROL_DISABLED")
    os.environ["ZYRA_OMP_WORKER_CONTROL_DISABLED"] = "1"
    try:
        with _api(tmp_path) as base_url:
            created = _post(
                base_url,
                "/tasks",
                {"goal": "Prove the OMP gate cannot be bypassed.", "auto_run": False},
            )
            task_id = created["task"]["task_id"]
            payload = {
                "prompt": "This child must not execute.",
                "execution_mode": "foreground",
                "subagent_task_id": "disabled-omp-child",
                "idempotency_key": "disabled-omp-child-create",
                "request_id": "disabled-omp-child-request",
            }
            first_status, suspended = _post_with_status(
                base_url,
                f"/tasks/{task_id}/subagents",
                payload,
            )
            assert first_status == 409
            spawned = _approve_and_retry_subagent(
                base_url,
                parent=created["task"],
                payload=payload,
                suspended=suspended,
            )
            assert spawned["record"]["status"] == "failed"
            assert "omp_worker_control_disabled" in str(spawned["record"]), json.dumps(
                spawned["record"], ensure_ascii=False, sort_keys=True
            )
            assert spawned["physical_receipt"]["outcome"] == "failed"
            assert spawned["physical_receipt"]["metadata"]["dispatch_gate"] == (
                "typescript.OmpWorkerDispatchRuntime"
            )
    finally:
        if previous is None:
            os.environ.pop("ZYRA_OMP_WORKER_CONTROL_DISABLED", None)
        else:
            os.environ["ZYRA_OMP_WORKER_CONTROL_DISABLED"] = previous


def test_subagent_api_lease_store_disable_blocks_before_typescript_execution(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executions: list[str] = []

    def forbidden_execution(*_args: Any, **_kwargs: Any) -> Any:
        executions.append("typescript-child-started")
        raise AssertionError("TypeScript child executed without canonical lease admission")

    monkeypatch.setattr(api_main, "_run_typescript_agent_request", forbidden_execution)
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {"goal": "Disable the canonical lease before child execution.", "auto_run": False},
        )
        task_id = created["task"]["task_id"]
        monkeypatch.setenv("ZYRA_WORKER_LEASE_STORE_DISABLED", "1")
        status, rejected = _post_with_status(
            base_url,
            f"/tasks/{task_id}/subagents",
            {
                "prompt": "This child operation must never start.",
                "execution_mode": "foreground",
                "subagent_task_id": "lease-disabled-child",
                "idempotency_key": "lease-disabled-child-create",
                "request_id": "lease-disabled-child-request",
            },
        )
        assert status == 409
        assert rejected["error"] == "subagent_worker_pool_acquisition_failed"
        assert "lease store is disabled" in rejected["message"]
        assert executions == []
        assert api_main.get_typescript_agent_port().records(parent_task_id=task_id) == ()
        assert _get(base_url, "/worker-pool/leases?task_id=lease-disabled-child")["leases"] == []


def test_subagent_execution_gate_disable_blocks_before_code_worker_run(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executions: list[str] = []

    def forbidden_run(*_args: Any, **_kwargs: Any) -> Any:
        executions.append("code-worker-run")
        raise AssertionError("CodeWorkerRuntime ran while its canonical execution gate was disabled")

    monkeypatch.setattr(api_main.CodeWorkerRuntime, "run", forbidden_run)
    monkeypatch.setenv("ZYRA_WORKER_EXECUTION_GATE_DISABLED", "1")
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {"goal": "Execution gate must run after lease admission but before worker code.", "auto_run": False},
        )
        status, rejected = _post_with_status(
            base_url,
            f"/tasks/{created['task']['task_id']}/subagents",
            {
                "prompt": "This code worker operation must never start.",
                "execution_mode": "foreground",
                "subagent_task_id": "execution-gate-disabled-child",
                "idempotency_key": "execution-gate-disabled-create",
                "request_id": "execution-gate-disabled-request",
            },
        )
        assert status == 409
        assert rejected["error"] == "typescript_subagent_execution_failed"
        assert "execution gate is disabled" in rejected["message"]
        assert executions == []
        leases = _get(
            base_url,
            "/worker-pool/leases?task_id=execution-gate-disabled-child",
        )["leases"]
        assert len(leases) == 1
        assert leases[0]["state"] == "released"
        assert rejected["physical_receipt"]["outcome"] == "failed"


def test_parent_cancel_fences_permission_suspended_child_physical_lease(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {"goal": "Cancel a child between physical admission and logical creation.", "auto_run": False},
        )
        parent_id = created["task"]["task_id"]
        status, suspended = _post_with_status(
            base_url,
            f"/tasks/{parent_id}/subagents",
            {
                "prompt": "Suspend for approval so the physical lease remains in flight.",
                "execution_mode": "foreground",
                "subagent_task_id": "suspended-child-physical-lease",
                "idempotency_key": "suspended-child-create",
                "request_id": "suspended-child-request",
            },
        )
        assert status == 409
        assert suspended["error"] == "permission_suspended"
        before = _get(
            base_url,
            "/worker-pool/leases?task_id=suspended-child-physical-lease",
        )["leases"]
        assert len(before) == 1 and before[0]["state"] == "active"

        cancelled = _post(
            base_url,
            f"/tasks/{parent_id}/cancel",
            {"reason": "parent cancelled before logical child creation committed"},
        )
        child_controls = cancelled["cancelled_physical_children"]
        assert any(
            item["task_id"] == "suspended-child-physical-lease"
            and item["effect"]["old_fence_blocks_late_commit"] is True
            for item in child_controls
        )
        after = _get(
            base_url,
            "/worker-pool/leases?task_id=suspended-child-physical-lease",
        )["leases"]
        assert after[0]["state"] == "cancelled"
        assert cancelled["subagent_cancel_errors"] == []


def test_subagent_fanout_maps_each_child_to_one_physical_attempt_and_receipt(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {"goal": "Run bounded OMP fanout under canonical physical leases.", "auto_run": False},
        )
        payload = {
            "shared_context": "Return deterministic no-tool completions.",
            "items": [
                {"task_id": "physical-fanout-a", "prompt": "child A", "background": False},
                {"task_id": "physical-fanout-b", "prompt": "child B", "background": False},
            ],
            "failure_policy": "collect",
            "maximum_concurrency": 2,
            "idempotency_key": "physical-fanout-create",
            "request_id": "physical-fanout-request",
        }
        first_status, suspended = _post_with_status(
            base_url,
            f"/tasks/{created['task']['task_id']}/subagents/fanout",
            payload,
        )
        assert first_status == 409
        assert suspended["error"] == "permission_suspended"
        assert suspended["physical_receipts"] == [None, None]

        completed = _approve_and_retry_subagent(
            base_url,
            parent=created["task"],
            payload=payload,
            suspended=suspended,
            route="subagents/fanout",
            # The product route deliberately drains foreground children
            # serially through one stdio provider-stream supervisor.  Preserve
            # the physical lease/receipt assertions without imposing the
            # single-child HTTP deadline on a two-child transaction.
            request_timeout=180,
        )
        assert [item["task_id"] for item in completed["physical_workers"]] == [
            "physical-fanout-a",
            "physical-fanout-b",
        ]
        assert [item["outcome"] for item in completed["physical_receipts"]] == [
            "succeeded",
            "succeeded",
        ], json.dumps(completed["physical_receipts"], ensure_ascii=False, sort_keys=True)
        for task_id in ("physical-fanout-a", "physical-fanout-b"):
            leases = _get(base_url, f"/worker-pool/leases?task_id={task_id}")["leases"]
            assert len(leases) == 1
            assert leases[0]["state"] == "released"


def test_subagent_slash_controls_mutate_e03_owner_and_tasks_remains_sealed_readable(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {
                "goal": "Exercise canonical slash control over one durable child.",
                "auto_run": False,
            },
        )
        parent = created["task"]
        task_id = parent["task_id"]
        spawn_payload = {
            "prompt": "Wait in the durable background queue for operator control.",
            "execution_mode": "background",
            "agent_type": "explore",
            "subagent_task_id": "slash-controlled-child",
            "idempotency_key": "slash-controlled-child-create",
            "request_id": "slash-controlled-child-request",
            "defer_background_drain": True,
        }
        spawn_status, suspended = _post_with_status(
            base_url,
            f"/tasks/{task_id}/subagents",
            spawn_payload,
        )
        assert spawn_status == 409
        assert suspended["error"] == "permission_suspended", json.dumps(
            {
                "error": suspended.get("error"),
                "worker_result": suspended.get("worker_result"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        spawned = _approve_and_retry_subagent(
            base_url,
            parent=parent,
            payload=spawn_payload,
            suspended=suspended,
        )
        command_headers = {
            "Authorization": (
                "Bearer "
                + suspended["permission_session"]["session_custody_token"]
            )
        }
        record = _get(base_url, f"/tasks/{task_id}/subagents")["subagents"][0]
        assert record["status"] in {"created", "queued", "running"}, record["error"]
        assert record["canonical_logical_owner"] == (
            "typescript.E03AgentControlCoordinator"
        )
        assert record["physical_lease_id"]
        assert record["physical_attempt_id"]
        assert record["physical_binding_id"]

        tasks_status, tasks_view = _post_with_status(
            base_url,
            f"/tasks/{task_id}/commands",
            {
                "text": "/tasks",
                "request_id": "tasks-sealed-read-request",
                "command_id": "tasks-sealed-read-command",
                "idempotency_key": "tasks-sealed-read-idempotency",
                "sealed": True,
                "competition_mode": "sealed_autonomous",
            },
        )
        assert tasks_status == 201
        tasks_data = tasks_view["command_result"]["data"]
        assert tasks_data["canonical_logical_owner"] == (
            "typescript.E03AgentControlCoordinator"
        )
        assert tasks_data["durable_owner"] == "SubagentTaskStore"
        assert tasks_data["fixture_projection"] is False
        assert tasks_data["python_logical_fallback"] is False
        assert tasks_data["tasks"][0]["task_id"] == record["task_id"]
        assert tasks_data["tasks"][0]["status"] in {"created", "queued", "running"}
        root_children = next(
            item
            for item in tasks_data["hierarchy"]
            if item["parent_id"] == task_id
        )
        assert record["task_id"] in root_children["child_ids"]

        sealed_command = _subagent_command(
            "steer",
            record,
            nonce="sealed-steer-nonce",
            owner_idempotency_key="sealed-steer-owner-idempotency",
            instruction="This instruction must never reach the child.",
            reason="sealed direct slash attempt",
        )
        sealed_parent = _post(
            base_url,
            "/tasks",
            {
                "goal": "Canonical sealed parent rejects manual agent mutation.",
                "auto_run": False,
                "sealed": True,
                "competition_mode": "sealed_autonomous",
            },
        )["task"]
        sealed_status, sealed = _post_with_status(
            base_url,
            f"/tasks/{sealed_parent['task_id']}/commands",
            {
                "text": sealed_command,
                "request_id": "sealed-agents-request",
                "command_id": "sealed-agents-command",
                "idempotency_key": "sealed-agents-transport-idempotency",
            },
            headers=command_headers,
        )
        assert sealed_status == 409
        assert sealed["command_result"]["error"]["code"] == "permission_denied"
        assert sealed["intervention_counted"] is True
        assert sealed["human_intervention_count"] == 0
        unchanged = _get(base_url, f"/tasks/{task_id}/subagents")["subagents"][0]
        assert unchanged["revision"] == record["revision"]
        assert unchanged["messages"] == []

        steer_instruction = "Re-check the bounded verification evidence."
        steer_nonce = "interactive-steer-nonce"
        steer_owner_idempotency = "interactive-steer-owner-idempotency"
        steer_command = _subagent_command(
            "steer",
            record,
            nonce=steer_nonce,
            owner_idempotency_key=steer_owner_idempotency,
            instruction=steer_instruction,
            reason="operator changed the local verification priority",
        )
        steer_status, steered = _post_with_status(
            base_url,
            f"/tasks/{task_id}/commands",
            {
                "text": steer_command,
                "request_id": "agents-steer-request",
                "command_id": "agents-steer-command",
                "idempotency_key": "agents-steer-transport-idempotency",
            },
            headers=command_headers,
        )
        assert steer_status == 201, steered["command_result"].get("error")
        steer_data = steered["command_result"]["data"]
        steer_receipt = steer_data["receipt"]
        assert steer_data["state_owner"] == "SubagentTaskStore"
        assert steer_data["canonical_logical_owner"] == (
            "typescript.E03AgentControlCoordinator"
        )
        assert steer_data["python_logical_fallback"] is False
        assert steer_receipt["nonce"] == steer_nonce
        assert steer_receipt["idempotency_key"] == steer_owner_idempotency
        assert steer_receipt["owner"] == "SubagentTaskStore"
        assert steer_receipt["expected_revision"] == record["revision"]
        assert steer_receipt["physical_lease_id"] == record["physical_lease_id"]
        assert steer_receipt["physical_attempt_id"] == record["physical_attempt_id"]
        assert steer_receipt["physical_binding_id"] == record["physical_binding_id"]
        assert steer_receipt["committed_revision"] > record["revision"]
        assert steer_data["replayed"] is False
        assert any(
            item["body"] == steer_instruction
            and item["idempotencyKey"] == steer_owner_idempotency
            for item in steer_data["task"]["messages"]
        )

        replay_status, replayed = _post_with_status(
            base_url,
            f"/tasks/{task_id}/commands",
            {
                "text": steer_command,
                "request_id": "agents-steer-replay-request",
                "command_id": "agents-steer-replay-command",
                "idempotency_key": "agents-steer-replay-transport-idempotency",
            },
            headers=command_headers,
        )
        assert replay_status == 201, replayed["command_result"].get("error")
        replay_data = replayed["command_result"]["data"]
        assert replay_data["replayed"] is True
        assert replay_data["receipt"]["nonce"] == steer_nonce
        assert replay_data["receipt"]["idempotency_key"] == steer_owner_idempotency
        assert replay_data["revision"] == steer_data["revision"]
        assert len(
            [
                item
                for item in replay_data["task"]["messages"]
                if item["idempotencyKey"] == steer_owner_idempotency
            ]
        ) == 1

        nonce_rebound_status, nonce_rebound = _post_with_status(
            base_url,
            f"/tasks/{task_id}/commands",
            {
                "text": _subagent_command(
                    "steer",
                    record,
                    nonce=steer_nonce,
                    owner_idempotency_key="different-owner-idempotency",
                    instruction="This nonce rebound must not commit.",
                    reason="adversarial nonce rebound",
                ),
                "request_id": "agents-steer-nonce-rebound-request",
                "command_id": "agents-steer-nonce-rebound-command",
                "idempotency_key": "agents-steer-nonce-rebound-transport",
            },
            headers=command_headers,
        )
        assert nonce_rebound_status == 409
        assert "nonce" in str(nonce_rebound["command_result"]["error"]).lower()

        idempotency_rebound_status, idempotency_rebound = _post_with_status(
            base_url,
            f"/tasks/{task_id}/commands",
            {
                "text": _subagent_command(
                    "steer",
                    record,
                    nonce="different-steer-nonce",
                    owner_idempotency_key=steer_owner_idempotency,
                    instruction="This idempotency rebound must not commit.",
                    reason="adversarial idempotency rebound",
                ),
                "request_id": "agents-steer-idempotency-rebound-request",
                "command_id": "agents-steer-idempotency-rebound-command",
                "idempotency_key": "agents-steer-idempotency-rebound-transport",
            },
            headers=command_headers,
        )
        assert idempotency_rebound_status == 409
        assert "idempotency" in str(
            idempotency_rebound["command_result"]["error"]
        ).lower()

        stale_lease_record = {
            **replay_data["task"],
            "physical_lease_id": "lease-stale-control",
        }
        stale_lease_status, stale_lease = _post_with_status(
            base_url,
            f"/tasks/{task_id}/commands",
            {
                "text": _subagent_command(
                    "steer",
                    stale_lease_record,
                    nonce="stale-lease-nonce",
                    owner_idempotency_key="stale-lease-idempotency",
                    instruction="This stale lease must not commit.",
                    reason="adversarial stale physical lease",
                ),
                "request_id": "agents-steer-stale-lease-request",
                "command_id": "agents-steer-stale-lease-command",
                "idempotency_key": "agents-steer-stale-lease-transport",
            },
            headers=command_headers,
        )
        assert stale_lease_status == 409
        assert "lease" in str(stale_lease["command_result"]["error"]).lower()

        kill_record = replay_data["task"]
        kill_nonce = "interactive-kill-nonce"
        kill_owner_idempotency = "interactive-kill-owner-idempotency"
        kill_status, killed = _post_with_status(
            base_url,
            f"/tasks/{task_id}/commands",
            {
                "text": _subagent_command(
                    "kill",
                    kill_record,
                    nonce=kill_nonce,
                    owner_idempotency_key=kill_owner_idempotency,
                    reason="operator fenced the obsolete child",
                ),
                "request_id": "agents-kill-request",
                "command_id": "agents-kill-command",
                "idempotency_key": "agents-kill-transport-idempotency",
            },
            headers=command_headers,
        )
        assert kill_status == 201, killed["command_result"].get("error")
        kill_data = killed["command_result"]["data"]
        assert kill_data["task"]["status"] == "killed"
        assert kill_data["receipt"]["nonce"] == kill_nonce
        assert kill_data["receipt"]["idempotency_key"] == kill_owner_idempotency
        assert kill_data["receipt"]["owner"] == "SubagentTaskStore"
        assert kill_data["receipt"]["physical_lease_id"] == record["physical_lease_id"]
        assert kill_data["receipt"]["physical_binding_id"] == record["physical_binding_id"]
        assert kill_data["physical_worker_control"]["phase"] == "applied"
        assert kill_data["physical_worker_control"]["effect"][
            "old_fence_blocks_late_commit"
        ] is True

        after_status, after_view = _post_with_status(
            base_url,
            f"/tasks/{task_id}/commands",
            {
                "text": "/tasks",
                "request_id": "tasks-after-kill-request",
                "command_id": "tasks-after-kill-command",
                "idempotency_key": "tasks-after-kill-idempotency",
                "sealed": True,
                "competition_mode": "sealed_autonomous",
            },
        )
        assert after_status == 201
        after_data = after_view["command_result"]["data"]
        assert after_data["lifecycle_counts"]["killed"] == 1
        assert after_data["terminal_task_ids"] == [record["task_id"]]


@contextmanager
def _api(root: Path) -> Iterator[str]:
    previous = {
        name: os.environ.get(name)
        for name in (
            "ZYRA_SQLITE_PATH",
            "ZYRA_EVENT_LOG",
            "ZYRA_TOOL_WORKSPACE",
            "ZYRA_ARTIFACT_ROOT",
            "ZYRA_WORKER_POOL_STORE",
            "ZYRA_GRAPH_STATE_STORE",
            "ZYRA_WORKSPACE_STORE",
            "ZYRA_PERMISSION_STORE",
            "ZYRA_PERMISSION_STATE",
            "ZYRA_MCP_STATE",
            "ZYRA_SUBAGENT_STATE",
            "ZYRA_CONTROL_STATE",
            "ZYRA_E02_API_PERMISSION_MODE",
        )
    }
    os.environ.update(
        {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(root / "tool-workspace"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_WORKER_POOL_STORE": str(root / "worker-pool.sqlite3"),
            "ZYRA_GRAPH_STATE_STORE": str(root / "graph-state.sqlite3"),
            "ZYRA_WORKSPACE_STORE": str(root / "workspace.sqlite3"),
            "ZYRA_PERMISSION_STORE": str(root / "permission-compat.json"),
            "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
            "ZYRA_MCP_STATE": str(root / "mcp-state.json"),
            "ZYRA_SUBAGENT_STATE": str(root / "subagent-state.json"),
            "ZYRA_CONTROL_STATE": str(root / "control-state"),
            "ZYRA_E02_API_PERMISSION_MODE": "default",
        }
    )
    api_main._WORKER_POOL_API = None
    api_main._WORKER_POOL_RUNTIME = None
    api_main._WORKER_POOL_KEY = None
    api_main.reset_control_runtime()
    api_main.reset_subagent_runtime()
    api_main.reset_mcp_runtime()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_main.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        api_main._WORKER_POOL_API = None
        api_main._WORKER_POOL_RUNTIME = None
        api_main._WORKER_POOL_KEY = None
        api_main.reset_control_runtime()
        api_main.reset_subagent_runtime()
        api_main.reset_mcp_runtime()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _subagent_command(
    action: str,
    record: dict[str, Any],
    *,
    nonce: str,
    owner_idempotency_key: str,
    reason: str,
    instruction: str = "",
) -> str:
    binding = ";".join(
        (
            f"nonce={nonce}",
            f"idempotency={owner_idempotency_key}",
            f"revision={record['revision']}",
            f"attempt={record['attempt']}",
            f"lease={record['physical_lease_id']}",
            "owner=typescript.E03AgentControlCoordinator",
            f"parent={record['parent_task_id']}",
        )
    )
    bound_reason = f"{reason} [zyra-control:{binding}]"
    if action == "steer":
        return (
            f'/agents steer "{record["task_id"]}" '
            f'--instruction "{instruction}" --reason "{bound_reason}"'
        )
    return f'/agents kill "{record["task_id"]}" --reason "{bound_reason}"'


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_with_status(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _get_with_headers(
    base_url: str,
    path: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url}{path}", headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _approve_and_retry_subagent(
    base_url: str,
    *,
    parent: dict[str, Any],
    payload: dict[str, Any],
    suspended: dict[str, Any],
    expected_status: int = 201,
    route: str = "subagents",
    request_timeout: float = 60,
) -> dict[str, Any]:
    session = suspended["permission_session"]
    token = session["session_custody_token"]
    session_id = session["session_id"]
    headers = {"Authorization": f"Bearer {token}"}
    pending = _get_with_headers(
        base_url,
        (
            "/permissions/requests"
            f"?session_id={session_id}&run_id={parent['run_id']}"
            f"&task_id={parent['task_id']}&pending_only=true"
        ),
        headers,
    )["requests"]["items"]
    assert len(pending) == 1
    status, resolved = _post_with_status(
        base_url,
        f"/permissions/requests/{pending[0]['request_id']}/resolve",
        {
            "session_id": session_id,
            "run_id": parent["run_id"],
            "task_id": parent["task_id"],
            "effect": "allow",
            "idempotency_key": (
                f"approve-{payload.get('subagent_task_id') or payload.get('request_id') or 'agent'}"
            ),
        },
        headers=headers,
    )
    assert status == 200, resolved
    retry_status, retry = _post_with_status(
        base_url,
        f"/tasks/{parent['task_id']}/{route}",
        {**payload, "session_id": session_id},
        headers=headers,
        timeout=request_timeout,
    )
    metadata = retry.get("worker_result", {}).get("metadata", {})
    diagnostic = {
        "error": retry.get("error"),
        "message": retry.get("message"),
        "typescript_runtime_error": metadata.get("typescript_runtime_error"),
        "typescript_runtime_error_message": metadata.get("typescript_runtime_error_message"),
        "worker_metadata": metadata,
        "record_status": (retry.get("record") or {}).get("status"),
        "record_error": (retry.get("record") or {}).get("error"),
        "worker_summary": retry.get("worker_result", {}).get("summary"),
    }
    assert retry_status == expected_status, json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
    return retry
