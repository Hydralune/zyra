from __future__ import annotations

from dataclasses import replace

import pytest

from zyra_orchestration.topology_policy.contracts import FrozenDict
from zyra_orchestration.deployment.errors import redact
from zyra_orchestration.deployment.provider_dispatch import (
    DEEPSEEK_MODEL_ID,
    DEEPSEEK_PROVIDER_ID,
    GLM_52_MODEL_ID,
    KIMI_MODEL_ID,
    KIMI_PROVIDER_ID,
    PROVIDER_PRIORITY,
    ZHIPU_PROVIDER_ID,
    _LIVE_PROFILES,
)
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


def test_kimi_pricing_has_conservative_nonzero_usd_budget_normalization() -> None:
    profile = _LIVE_PROFILES[(KIMI_PROVIDER_ID, KIMI_MODEL_ID)]

    assert profile.pricing_currency == "CNY"
    assert profile.normalized_input_usd_per_million == 6.5
    assert profile.normalized_cached_input_usd_per_million == 1.3
    assert profile.normalized_output_usd_per_million == 27
    assert profile.normalized_pricing_source == (
        "zyra://pricing/conservative-cny-as-usd-upper-bound"
    )


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


def test_phase2_operator_workload_executes_on_selected_physical_node(
    tmp_path,
) -> None:
    operator_ref = "tool:produce-tool@1"
    payload = {
        "schema": "zyra.production-physical-operator-task/v1",
        "run_id": "run-physical-operator",
        "task_id": "task-physical-operator",
        "goal": "Produce and verify a deterministic artifact.",
        "requirement_revision": "requirement-revision-1",
        "operator_ref": operator_ref,
        "operator": {
            "operator_ref": operator_ref,
            "operator_type": "tool",
            "profile_digest": "operator-profile-digest",
            "capabilities": ["artifact-production", "verification"],
            "input_contract": ["goal"],
            "output_contract": ["artifact", "verification"],
        },
        "layer_index": 1,
        "candidate_set_digest": "candidate-set-digest",
        "policy_input_digest": "policy-input-digest",
        "operator_idempotency_key": "physical-operator-layer-1",
    }
    harness = build_physical_harness(
        tmp_path,
        location="local",
        operation="phase2-operator-execution",
        dispatch_payload=payload,
    )
    try:
        harness.execute()
        receipt = harness.physical_port.receipts[0]
        validation = harness.physical_port.validation_reports[0]
        signals = dict(receipt.input_signals)

        assert validation.real_gate_closed is True
        assert signals["workload_operation"] == "phase2-operator-execution"
        assert signals["operator_ref"] == operator_ref
        assert signals["layer_index"] == 1
        assert signals["task_payload_digest"] == receipt.privacy_evidence[
            "payload_digest"
        ]
        assert signals["operator_execution_digest"]
        assert signals["operator_adapter_id"] == (
            "tool.produce-tool.deterministic"
        )
        assert signals["domain_effect_performed"] is True
        assert signals["output_contract_fulfilled"] is True
        assert str(signals["operator_execution_digest"]).removeprefix(
            "sha256:"
        )
        assert signals["leased_worker_process_identity"] == receipt.physical_identity[
            "failure_boundary_id"
        ]
        assert receipt.simulated is False
        assert receipt.semantic_only is False
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


def test_physical_dispatch_defaults_to_glm() -> None:
    task = PhysicalDispatchTask(
        run_id="run",
        task_id="task",
        payload=FrozenDict({"marker": True}),
        privacy_class="internal",
        allowed_placements=("cloud",),
        permission_ref="permission://allowed",
    )

    assert task.provider_id == "zhipu"
    assert task.model_id == "glm-5.2"


def test_physical_dispatch_priority_uses_deepseek_flash_before_kimi() -> None:
    assert DEEPSEEK_MODEL_ID == "deepseek-v4-flash"
    assert PROVIDER_PRIORITY == (
        (ZHIPU_PROVIDER_ID, GLM_52_MODEL_ID),
        (DEEPSEEK_PROVIDER_ID, DEEPSEEK_MODEL_ID),
        (KIMI_PROVIDER_ID, KIMI_MODEL_ID),
    )
