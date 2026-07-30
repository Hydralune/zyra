from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from zyra_scheduler import (
    OperatorCatalog,
    OperatorPlacementError,
    OperatorPlacementLeaseConfig,
)
from zyra_scheduler.worker_pool.errors import LeaseFenced
from zyra_scheduler.worker_pool.models import LeaseState

from tests.integration.operator_placement_harness import (
    ROOT,
    build_harness,
    execute_harness,
)


def test_permission_rejected_candidate_never_receives_operator_lease(
    tmp_path,
) -> None:
    harness = build_harness(tmp_path, single_layer=True)
    restricted_policy = replace(
        harness.policy,
        allowed_permissions=("worker.dispatch",),
    )
    restricted_proposal = replace(
        harness.proposal,
        input_snapshot_digest=restricted_policy.digest,
    )

    # OperatorSelectionProposal has a computed digest, so replace only the
    # snapshot binding; ResourceScheduler then rejects the unresolved tool
    # permission and the integration takes its explicit baseline lane.
    restricted_result = SimpleNamespace(
        mode=harness.selector_result.mode,
        readiness=harness.selector_result.readiness,
        proposal=restricted_proposal,
        scheduler_input=restricted_proposal.scheduler_input().bind_task(
            run_id=harness.task.run_id,
            task_id=harness.task.task_id,
        ),
        degraded=False,
        degraded_reason="",
    )

    result = harness.runtime.execute_task(
        task=harness.task,
        policy_input=restricted_policy,
        selector_result=restricted_result,
        catalog=harness.catalog,
        adaptive_depth=harness.adaptive,
        eligibility_port=harness.eligibility,
    )

    assert result.mode == "baseline"
    assert result.placement.route_mode == "baseline"
    leases = harness.pool.store.list_leases()
    assert leases
    assert all(
        lease.metadata.get("operator_ref") != "tool:produce-tool@1"
        for lease in leases
    )


def test_revoked_lease_fails_before_operator_side_effect(tmp_path) -> None:
    harness = build_harness(tmp_path, revoke_in_prepare=True)
    harness.runtime.config = replace(
        harness.runtime.config,
        maximum_recovery_attempts=0,
    )

    with pytest.raises(LeaseFenced):
        execute_harness(harness)

    assert harness.call_port.side_effect_count == 0
    leases = harness.pool.store.list_leases()
    assert leases
    assert any(lease.state is LeaseState.CANCELLED for lease in leases)
    assert harness.task.metadata["recovery_plans"]


def test_catalog_drift_after_lease_fails_closed(tmp_path) -> None:
    harness = build_harness(tmp_path, single_layer=True)
    current = harness.catalog.entries[0]
    drifted = OperatorCatalog(
        entries=(replace(current, revoked=True),),
        source_versions=harness.catalog.source_versions,
        generation=harness.catalog.generation + 1,
        built_at=harness.catalog.built_at,
    )
    harness.runtime.catalog_provider = lambda: drifted

    with pytest.raises(
        OperatorPlacementError,
        match="operator catalog changed after lease acquisition",
    ):
        execute_harness(harness)

    assert harness.call_port.side_effect_count == 0
    assert harness.task.metadata["recovery_plans"]


def test_worker_failure_uses_existing_recovery_owner_and_reroutes(tmp_path) -> None:
    harness = build_harness(
        tmp_path,
        single_layer=True,
        b_first=True,
        fail_first_worker="worker-b",
    )

    result = execute_harness(harness)

    assert result.placement.selected_manifest_id == "worker-a"
    assert result.recovery_plan_refs
    assert result.attempt_receipts[0].worker_id == "worker-a"
    assert result.attempt_receipts[0].recovery_plan_refs
    assert harness.call_port.side_effect_count == 1
    recovery = harness.task.metadata["last_recovery_plan"]
    assert recovery["metadata"]["failed_worker"] == "worker-b"
    assert "reroute_worker" in recovery["actions"]
    placement_workers = [
        event.payload["operator_placement"]["selected_manifest_id"]
        for event in result.events
        if event.payload.get("schema")
        == "zyra.operator-placement-decision/v1"
    ]
    assert placement_workers[:2] == ["worker-b", "worker-a"]


def test_lease_gate_disable_is_fail_closed(tmp_path) -> None:
    harness = build_harness(tmp_path, single_layer=True)
    harness.runtime.config = replace(
        OperatorPlacementLeaseConfig.load(
            ROOT / "config" / "phase2" / "operator-placement-lease.json"
        ),
        require_execution_fence=False,
    )

    with pytest.raises(
        OperatorPlacementError,
        match="lease gate disabled",
    ):
        execute_harness(harness)

    assert harness.call_port.side_effect_count == 0


def test_uncertain_external_call_outcome_is_not_retried(tmp_path) -> None:
    harness = build_harness(
        tmp_path,
        single_layer=True,
        fail_after_side_effect=True,
    )

    with pytest.raises(
        OperatorPlacementError,
        match="automatic retry is unsafe",
    ):
        execute_harness(harness)

    assert harness.call_port.side_effect_count == 1
    assert len(harness.pool.store.list_leases()) == 1
    assert harness.task.metadata["recovery_plans"]
