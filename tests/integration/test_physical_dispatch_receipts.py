from __future__ import annotations

from dataclasses import replace

import pytest

from zyra_orchestration.topology_policy.contracts import FrozenDict
from zyra_orchestration.deployment.errors import redact
from zyra_scheduler.dispatch_evidence import (
    PhysicalDispatchReceiptValidator,
    PhysicalDispatchTask,
)
from zyra_scheduler.worker_pool.models import AttemptState, LeaseState, parse_utc

from tests.integration.physical_dispatch_harness import build_physical_harness


def test_deployment_redaction_preserves_usage_but_removes_credentials() -> None:
    value = redact(
        {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
            "access_token": "provider-secret",
            "password": "provider-secret",
        }
    )

    assert value["prompt_tokens"] == 12
    assert value["completion_tokens"] == 3
    assert value["total_tokens"] == 15
    assert value["access_token"] == "<redacted>"
    assert value["password"] == "<redacted>"


@pytest.mark.parametrize("location", ("local", "edge"))
def test_real_process_dispatch_receipt_closes_lane_gate(
    tmp_path,
    location: str,
) -> None:
    harness = build_physical_harness(tmp_path, location=location)
    try:
        result = harness.execute()
        assert len(result.attempt_receipts) == 1
        assert len(harness.physical_port.receipts) == 1
        receipt = harness.physical_port.receipts[0]
        validation = harness.physical_port.validation_reports[0]
        identity = dict(receipt.physical_identity)
        runtime = dict(receipt.runtime_evidence)
        attempt = result.attempt_receipts[0]

        assert validation.real_gate_closed is True
        assert validation.blockers == ()
        assert identity["location"] == location
        observed_health = harness.clients[location].health()
        assert int(identity["pid"]) == int(observed_health["pid"])
        assert int(identity["pid"]) != identity["orchestrator_pid"]
        assert identity["failure_boundary_id"]
        assert runtime["network_endpoint"] == harness.processes[location].endpoint
        assert receipt.lease_id == attempt.lease_id
        assert receipt.physical_attempt_id == attempt.attempt_id
        assert receipt.call_receipt.digest
        assert receipt.artifact_ref.digest
        assert receipt.verifier_ref.digest
        assert attempt.physical_dispatch_receipt_digest == receipt.digest
        assert (
            parse_utc(attempt.lease_acquired_at)
            <= parse_utc(attempt.attempt_started_at)
            <= parse_utc(attempt.call_started_at)
            <= parse_utc(attempt.call_finished_at)
        )
        assert (
            harness.pool.store.require_lease(attempt.lease_id).state
            is LeaseState.RELEASED
        )
        assert (
            harness.pool.store.require_attempt(attempt.attempt_id).state
            is AttemptState.SUCCEEDED
        )
        if location == "local":
            assert identity["terminal_id"]
            assert receipt.provider_evidence == FrozenDict()
        else:
            assert identity["independent_process"] is True
            assert runtime["network_namespace"].startswith("host-loopback:")
    finally:
        harness.close()


def test_simulated_or_label_only_receipt_cannot_close_gate(tmp_path) -> None:
    harness = build_physical_harness(tmp_path, location="local")
    try:
        harness.execute()
        receipt = harness.physical_port.receipts[0]
        validator = PhysicalDispatchReceiptValidator()

        simulated = replace(receipt, simulated=True)
        assert validator.validate(simulated).real_gate_closed is False
        assert "not_simulated" in validator.validate(simulated).blockers

        edge_label_only = replace(
            receipt,
            physical_identity=FrozenDict(
                {**dict(receipt.physical_identity), "location": "edge"}
            ),
        )
        reroute = validator.validate_reroute(receipt, edge_label_only)
        assert reroute.valid is False
        assert reroute.placement_changed is True
        assert reroute.physical_identity_changed is False
        assert "physical_identity_unchanged" in reroute.blockers
    finally:
        harness.close()


def test_local_only_privacy_never_starts_cloud_runtime(tmp_path) -> None:
    harness = build_physical_harness(
        tmp_path,
        location="local",
        privacy_class="local-only",
        allowed_placements=("local",),
    )
    try:
        harness.execute()
        receipt = harness.physical_port.receipts[0]
        assert receipt.privacy_class == "local-only"
        assert receipt.allowed_placements == ("local",)
        assert receipt.physical_identity["location"] == "local"
        assert "cloud" not in harness.processes
        assert receipt.privacy_evidence["payload_redacted"] is True
    finally:
        harness.close()

    with pytest.raises(ValueError, match="must be local-only"):
        PhysicalDispatchTask(
            run_id="run",
            task_id="task",
            payload=FrozenDict({"sensitive": True}),
            privacy_class="local-only",
            allowed_placements=("local", "cloud"),
            permission_ref="permission://allowed",
        )
