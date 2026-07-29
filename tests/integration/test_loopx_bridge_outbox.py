from __future__ import annotations

import json
from pathlib import Path

import pytest

from zyra_integrations.loopx.bridge import (
    LoopXBridgeObservability,
    OutboxState,
    SyncStatus,
)
from zyra_orchestration.graph_custody import GraphConflictStrategy
from zyra_runtime import LocalArtifactStore
from zyra_runtime.runtime_events import RuntimeEventQuery, RuntimeEventSpineBridge

from tests.integration.loopx_bridge_helpers import (
    PROJECT_ROOT,
    bridge_runtime,
    committed_graph_mutation,
    install_loopx,
    valid_update,
)


def test_only_successful_canonical_commit_enters_durable_outbox(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outbox, _, _, _ = bridge_runtime(workspace, install_loopx(workspace))
    custody, committed = committed_graph_mutation(tmp_path / "canonical")

    accepted = outbox.enqueue_after_commit(
        run_id="run-loopx-bridge",
        task_id="task-loopx-bridge",
        canonical_commit=committed,
        update=valid_update(),
    )
    replay = outbox.enqueue_after_commit(
        run_id="run-loopx-bridge",
        task_id="task-loopx-bridge",
        canonical_commit=committed,
        update=valid_update(),
    )
    assert accepted.sequence == replay.sequence
    assert outbox.checkpoint()["counts"]["pending"] == 1

    stale = custody.branch(
        "graph-loopx-bridge",
        branch_id="stale-conflict",
        actor_id="loopx-bridge-test",
        causation_id="cause-stale",
    )
    stale.set_metadata("requirement_revision", "stale")
    winner = custody.branch(
        "graph-loopx-bridge",
        branch_id="winner",
        actor_id="loopx-bridge-test",
        causation_id="cause-winner",
    )
    winner.set_metadata("requirement_revision", "r2")
    assert custody.commit(winner.build()).receipt.committed
    rejected_commit = custody.commit(
        stale.build(),
        strategy=GraphConflictStrategy.SERIALIZE,
    )
    assert rejected_commit.receipt.committed is False
    with pytest.raises(Exception) as rejected:
        outbox.enqueue_after_commit(
            run_id="run-loopx-bridge",
            task_id="task-loopx-bridge",
            canonical_commit=rejected_commit,
            update=valid_update(goal_id="goal-rejected"),
        )
    assert getattr(rejected.value, "code", "") == "loopx_canonical_commit_required"
    assert len(outbox.list_records()) == 1


def test_real_loopx_apply_is_idempotent_and_emits_causal_event_artifact(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    installed = install_loopx(workspace)
    event_spine = RuntimeEventSpineBridge.create(
        database_path=workspace / ".zyra" / "events.sqlite3",
        artifact_root=workspace / ".zyra" / "event-artifacts",
        workspace_root=PROJECT_ROOT,
    )
    artifact_store = LocalArtifactStore(workspace / ".zyra" / "artifacts")
    observability = LoopXBridgeObservability(
        artifact_store=artifact_store,
        event_spine=event_spine,
    )
    try:
        outbox, _, runtime, dispatcher = bridge_runtime(
            workspace,
            installed,
            observability=observability,
        )
        _, committed = committed_graph_mutation(tmp_path / "canonical")
        record = outbox.enqueue_after_commit(
            run_id="run-loopx-bridge",
            task_id="task-loopx-bridge",
            canonical_commit=committed,
            update=valid_update(),
        )
        first = dispatcher.dispatch()
        assert first[0].status is SyncStatus.APPLIED
        assert first[0].event_id
        assert first[0].artifact_id
        assert outbox.get(record.sequence).state is OutboxState.ACKED

        events = runtime.load_events("goal-bridge")
        event_ids = [item["event_id"] for item in events]
        assert len(event_ids) == len(set(event_ids))
        assert sum(item["event_type"] == "quota_spent" for item in events) == 1
        state = runtime.read_private_state("goal-bridge")
        assert state["quota"]["spent_slots"] == 1
        assert state["continuation_allowed"] is True
        assert state["interaction_contract"]["cli_channel"]["spend_allowed_now"] is False
        assert Path(first[0].apply_receipt["module_origin"]).is_relative_to(
            Path(installed["install_root"])
        )

        page = event_spine.query(
            RuntimeEventQuery(task_id="task-loopx-bridge", limit=50)
        )
        sync_events = [
            item for item in page.events if item.event_type == "runtime.audit.finding"
        ]
        assert len(sync_events) == 1
        assert sync_events[0].canonical["metadata"]["source_causation_id"] == (
            "cause-r1"
        )
        assert sync_events[0].canonical["metadata"]["sync_status"] == "applied"
        assert sync_events[0].correlation_id == "run-loopx-bridge"
        assert first[0].artifact_id in {
            reference.artifact_id for reference in sync_events[0].artifact_refs
        }
        artifact_files = list((workspace / ".zyra" / "artifacts").rglob("*.json"))
        assert len(artifact_files) == 1
        sync_payload = json.loads(artifact_files[0].read_text(encoding="utf-8"))
        assert sync_payload["schema"] == "zyra.loopx-sync-status/v1"
        assert sync_payload["status"] == "applied"
        assert sync_payload["canonical_commit"]["commit_id"]
        assert sync_payload["last_validated_receipt"]["spend_allowed"] is True
        assert sync_payload["claim_conflict"] is None
        assert sync_payload["quota_exhausted"] is False
        assert sync_payload["degraded_reason"] == ""

        assert dispatcher.dispatch() == ()
        assert len(runtime.load_events("goal-bridge")) == len(events)
    finally:
        event_spine.close()


@pytest.mark.parametrize(
    "failed_gate",
    [
        "validation_passed",
        "permission_allowed",
        "lease_valid",
        "budget_allowed",
    ],
)
def test_each_zyra_gate_prevents_loopx_quota_spend(
    tmp_path: Path,
    failed_gate: str,
) -> None:
    workspace = tmp_path / f"workspace-{failed_gate}"
    outbox, _, runtime, dispatcher = bridge_runtime(
        workspace,
        install_loopx(workspace),
    )
    _, committed = committed_graph_mutation(
        tmp_path / f"canonical-{failed_gate}",
        graph_id=f"graph-{failed_gate}",
    )
    update = valid_update(
        goal_id=f"goal-{failed_gate}",
        todo_id=f"todo_{failed_gate}",
    )
    update["validation"][failed_gate] = False
    outbox.enqueue_after_commit(
        run_id="run-loopx-bridge",
        task_id=f"task-{failed_gate}",
        canonical_commit=committed,
        update=update,
    )

    receipt = dispatcher.dispatch()[0]
    assert receipt.apply_receipt["spend_applied_slots"] == 0
    assert receipt.apply_receipt["spend_rejected_slots"] == 1
    assert failed_gate.partition("_")[0] in (
        receipt.apply_receipt["degraded_reason"]
    )
    assert runtime.read_private_state(f"goal-{failed_gate}")["quota"][
        "spent_slots"
    ] == 0


def test_workspace_isolation_validation_before_spend_and_disabled_route(
    tmp_path: Path,
) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    outbox_a, _, runtime_a, dispatcher_a = bridge_runtime(
        workspace_a,
        install_loopx(workspace_a),
    )
    outbox_b, _, runtime_b, dispatcher_b = bridge_runtime(
        workspace_b,
        install_loopx(workspace_b),
    )
    custody_a, commit_a = committed_graph_mutation(
        tmp_path / "canonical-a",
        graph_id="graph-loopx-a",
    )
    _, commit_b = committed_graph_mutation(
        tmp_path / "canonical-b",
        graph_id="graph-loopx-b",
    )
    outbox_a.enqueue_after_commit(
        run_id="run-loopx-bridge",
        task_id="task-loopx-a",
        canonical_commit=commit_a,
        update=valid_update(
            goal_id="shared-goal",
            claimant="workspace-a-controller",
            gates_passed=False,
        ),
    )
    outbox_b.enqueue_after_commit(
        run_id="run-loopx-bridge",
        task_id="task-loopx-b",
        canonical_commit=commit_b,
        update=valid_update(
            goal_id="shared-goal",
            claimant="workspace-b-controller",
        ),
    )

    disabled = dispatcher_a.dispatch(enabled=False)
    assert disabled[0].status is SyncStatus.SYNC_DEGRADED
    assert disabled[0].degraded_reason == "bridge_disabled"
    assert outbox_a.checkpoint()["counts"]["pending"] == 1
    assert not (workspace_a / ".zyra" / "loopx" / "state" / "private").exists()
    assert custody_a.current("graph-loopx-a").revision == 1
    assert custody_a.current("graph-loopx-a").metadata[
        "requirement_revision"
    ] == "r1"

    result_a = dispatcher_a.dispatch()
    result_b = dispatcher_b.dispatch()
    assert result_a[0].apply_receipt["spend_applied_slots"] == 0
    assert result_a[0].apply_receipt["spend_rejected_slots"] == 1
    assert result_b[0].apply_receipt["spend_applied_slots"] == 1
    state_a = runtime_a.read_private_state("shared-goal")
    state_b = runtime_b.read_private_state("shared-goal")
    assert state_a["workspace_id"] != state_b["workspace_id"]
    assert state_a["quota"]["spent_slots"] == 0
    assert state_b["quota"]["spent_slots"] == 1
    assert (
        state_a["todo_projection"]["agent_todos"]["items"][0]["claimed_by"]
        == "workspace-a-controller"
    )
    assert (
        state_b["todo_projection"]["agent_todos"]["items"][0]["claimed_by"]
        == "workspace-b-controller"
    )
