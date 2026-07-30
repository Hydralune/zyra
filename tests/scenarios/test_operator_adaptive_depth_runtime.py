from __future__ import annotations

from zyra_scheduler import ExitDecision
from zyra_scheduler.recovery_runtime import SideEffectFenceRuntime
from zyra_scheduler.worker_pool.models import LeaseState

from tests.integration.operator_placement_harness import (
    build_harness,
    execute_harness,
)


def test_selector_and_early_exit_flags_produce_distinct_runtime_behavior(
    tmp_path,
) -> None:
    enabled_harness = build_harness(tmp_path / "enabled")
    disabled_harness = build_harness(tmp_path / "disabled")

    enabled = execute_harness(enabled_harness, early_exit_enabled=True)
    disabled = execute_harness(disabled_harness, early_exit_enabled=False)

    assert enabled.adaptive_result is not None
    assert disabled.adaptive_result is not None
    assert enabled.adaptive_result.exited_early is True
    assert disabled.adaptive_result.exited_early is False
    assert len(enabled.attempt_receipts) == 2
    assert len(disabled.attempt_receipts) == 4
    assert (
        enabled.adaptive_result.cost_receipt.actual_tokens
        < disabled.adaptive_result.cost_receipt.actual_tokens
    )
    assert (
        enabled.adaptive_result.cost_receipt.actual_cost_usd
        < disabled.adaptive_result.cost_receipt.actual_cost_usd
    )
    assert enabled.adaptive_result.final_decision.decision is ExitDecision.EXIT
    assert disabled.adaptive_result.final_decision.decision is ExitDecision.CONTINUE


def test_early_exit_occurs_only_after_layer_leases_are_released(tmp_path) -> None:
    harness = build_harness(tmp_path)
    result = execute_harness(harness)

    assert result.adaptive_result is not None
    assert result.adaptive_result.exited_early is True
    assert all(
        harness.pool.store.require_lease(item.lease_id).state
        is LeaseState.RELEASED
        for item in result.attempt_receipts
    )
    final_event = next(
        item
        for item in reversed(result.adaptive_result.events)
        if item.payload.get("schema") == "zyra.adaptive-depth-outcome-event/v1"
    )
    assert final_event.payload["task_completed"] is True
    assert final_event.payload["verifier_passed"] is True


def test_pending_side_effect_prevents_exit_and_executes_full_plan(tmp_path) -> None:
    harness = build_harness(tmp_path)
    pending, _ = SideEffectFenceRuntime(harness.recovery_store).reserve(
        run_id=harness.task.run_id,
        task_id=harness.task.task_id,
        operation="pending-external-write",
        request={"path": "pending.txt"},
        idempotency_key="pending-external-write-r1",
    )

    result = execute_harness(harness)

    assert result.adaptive_result is not None
    assert result.adaptive_result.exited_early is False
    assert len(result.attempt_receipts) == 4
    assert pending.fence_key in (
        result.adaptive_result.eligibility_snapshots[-1].side_effect_pending_ids
    )
    assert (
        "side_effects_settled"
        in result.adaptive_result.final_decision.failed_conditions
    )
