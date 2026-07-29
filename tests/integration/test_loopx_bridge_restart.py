from __future__ import annotations

from pathlib import Path

import pytest

from zyra_integrations.loopx.bridge import OutboxState, SyncStatus

from tests.integration.loopx_bridge_helpers import (
    bridge_runtime,
    committed_graph_mutation,
    install_loopx,
    valid_update,
)


class SimulatedProcessCrash(BaseException):
    pass


def test_restart_after_apply_before_ack_replays_without_duplicate_side_effect(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    installed = install_loopx(workspace)
    outbox, _, runtime, dispatcher = bridge_runtime(workspace, installed)
    _, committed = committed_graph_mutation(tmp_path / "canonical")
    record = outbox.enqueue_after_commit(
        run_id="run-loopx-bridge",
        task_id="task-loopx-restart",
        canonical_commit=committed,
        update=valid_update(limit_slots=4, spend_slots=1),
    )

    with pytest.raises(SimulatedProcessCrash):
        dispatcher.dispatch(
            after_apply=lambda _record, _receipt: (_ for _ in ()).throw(
                SimulatedProcessCrash()
            )
        )
    crashed = outbox.get(record.sequence)
    assert crashed is not None
    assert crashed.state is OutboxState.INFLIGHT
    first_events = runtime.load_events("goal-bridge")
    assert sum(item["event_type"] == "quota_spent" for item in first_events) == 1

    restarted_outbox, _, restarted_runtime, restarted = bridge_runtime(
        workspace,
        installed,
    )
    recovered = restarted.dispatch()
    assert recovered[0].status is SyncStatus.IDEMPOTENT_REPLAY
    assert restarted_outbox.get(record.sequence).state is OutboxState.ACKED
    assert restarted_outbox.get(record.sequence).attempts == 2
    replayed_events = restarted_runtime.load_events("goal-bridge")
    assert len(replayed_events) == len(first_events)
    assert sum(item["event_type"] == "quota_spent" for item in replayed_events) == 1
    assert restarted_runtime.read_private_state("goal-bridge")["quota"][
        "spent_slots"
    ] == 1


def test_claim_conflict_is_dead_lettered_without_worker_lease_or_spend(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    installed = install_loopx(workspace)
    outbox, _, runtime, dispatcher = bridge_runtime(workspace, installed)
    custody, first_commit = committed_graph_mutation(tmp_path / "canonical")
    first = outbox.enqueue_after_commit(
        run_id="run-loopx-bridge",
        task_id="task-loopx-conflict",
        canonical_commit=first_commit,
        update=valid_update(
            claimant="first-loopx-controller",
            spend_slots=0,
        ),
    )
    assert dispatcher.dispatch()[0].status is SyncStatus.APPLIED
    assert outbox.get(first.sequence).state is OutboxState.ACKED
    events_before = runtime.load_events("goal-bridge")

    builder = custody.branch(
        "graph-loopx-bridge",
        branch_id="requirement-r2",
        actor_id="loopx-bridge-test",
        causation_id="cause-r2",
        correlation_id="run-loopx-bridge",
    )
    builder.set_metadata("requirement_revision", "r2")
    second_commit = custody.commit(builder.build())
    conflicting = outbox.enqueue_after_commit(
        run_id="run-loopx-bridge",
        task_id="task-loopx-conflict",
        canonical_commit=second_commit,
        update=valid_update(
            claimant="second-loopx-controller",
            spend_slots=1,
        ),
    )
    result = dispatcher.dispatch()
    assert result[0].status is SyncStatus.CLAIM_CONFLICT
    assert result[0].apply_receipt["claim_conflict"]["worker_lease_changed"] is False
    assert outbox.get(conflicting.sequence).state is OutboxState.DEAD_LETTER
    assert runtime.load_events("goal-bridge") == events_before
    state = runtime.read_private_state("goal-bridge")
    assert state["quota"]["spent_slots"] == 0
    assert state.get("worker_lease") is None

    replayed = dispatcher.replay_dead_letter(conflicting.sequence)
    assert replayed.state is OutboxState.PENDING
    repeated_conflict = dispatcher.dispatch()
    assert repeated_conflict[0].status is SyncStatus.CLAIM_CONFLICT
    assert outbox.get(conflicting.sequence).state is OutboxState.DEAD_LETTER


def test_loopx_unavailable_is_explicitly_degraded_and_canonical_graph_survives(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    installed = install_loopx(workspace)
    broken_receipt = dict(installed)
    broken_receipt["module_root"] = "missing-pinned-module"
    outbox, _, _, dispatcher = bridge_runtime(
        workspace,
        broken_receipt,
        max_attempts=2,
    )
    custody, committed = committed_graph_mutation(tmp_path / "canonical")
    record = outbox.enqueue_after_commit(
        run_id="run-loopx-bridge",
        task_id="task-loopx-degraded",
        canonical_commit=committed,
        update=valid_update(),
    )

    first = dispatcher.dispatch(limit=1)
    assert first[0].status is SyncStatus.SYNC_DEGRADED
    assert first[0].error_code == "loopx_runtime_unavailable"
    assert outbox.get(record.sequence).state is OutboxState.PENDING
    assert custody.current("graph-loopx-bridge").revision == 1
    assert custody.current("graph-loopx-bridge").metadata[
        "requirement_revision"
    ] == "r1"

    second = dispatcher.dispatch(limit=1)
    assert second[0].status is SyncStatus.DEAD_LETTER
    assert outbox.get(record.sequence).state is OutboxState.DEAD_LETTER
    checkpoint = outbox.checkpoint()
    assert checkpoint["counts"]["dead_letter"] == 1
    assert checkpoint["max_sequence"] == record.sequence
    assert custody.current("graph-loopx-bridge").revision == 1
