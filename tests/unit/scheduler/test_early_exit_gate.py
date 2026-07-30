from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zyra_orchestration.topology_policy import (
    ContractHeader,
    FrozenDict,
    StableArtifactRef,
    canonical_digest,
)
from zyra_scheduler.operator_policy import (
    DeterministicEarlyExitGate,
    EarlyExitGateConfig,
    ExitDecision,
    ExitEligibilitySnapshot,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
NOW = "2026-07-30T08:00:00Z"
FRESH_UNTIL = "2026-07-30T08:05:00Z"


def _config() -> EarlyExitGateConfig:
    return EarlyExitGateConfig.load(
        PROJECT_ROOT / "config" / "phase2" / "maas-early-exit.json"
    )


def _header(config: EarlyExitGateConfig) -> ContractHeader:
    return ContractHeader(
        contract_id="exit-eligibility-unit",
        created_at=NOW,
        source_event_id="event-exit-unit",
        correlation_id="correlation-exit-unit",
        causation_id="proposal-exit-unit",
        mechanism_id=config.mechanism_id,
        mechanism_version=config.mechanism_version,
        input_version="zyra.operator-selection-proposal/v1",
        idempotency_key="exit-eligibility:unit",
        configuration_digest=config.digest,
    )


def _snapshot(**overrides: object) -> ExitEligibilitySnapshot:
    config = _config()
    artifact = StableArtifactRef(
        ref_id="artifact-deliverable",
        uri="artifact://run/task/artifact-deliverable",
        digest="a" * 64,
        media_type="text/plain",
    )
    values = {
        "header": _header(config),
        "snapshot_id": "exit-eligibility-unit",
        "run_id": "run-exit-unit",
        "task_id": "task-exit-unit",
        "policy_input_digest": "b" * 64,
        "proposal_id": "proposal-exit-unit",
        "proposal_digest": "c" * 64,
        "requirement_revision": "requirement-r1",
        "observed_at": NOW,
        "expires_at": FRESH_UNTIL,
        "expected_obligation_ids": ("produce_artifact", "verify_artifact"),
        "observed_obligation_ids": ("produce_artifact", "verify_artifact"),
        "unresolved_critical_obligation_ids": (),
        "obligation_owner_ref": "task-state://run-exit-unit/task-exit-unit",
        "obligation_owner_digest": "d" * 64,
        "required_artifact_ids": ("deliverable",),
        "verified_artifact_refs": (artifact,),
        "invalid_artifact_ids": (),
        "artifact_owner_ref": "artifact-store://run-exit-unit/task-exit-unit",
        "artifact_owner_digest": "e" * 64,
        "permission_pending_ids": (),
        "permission_owner_ref": "permission-queue://session-exit-unit",
        "permission_owner_digest": "f" * 64,
        "side_effect_required_ids": ("effect-deliverable",),
        "side_effect_pending_ids": (),
        "side_effect_unknown_ids": (),
        "side_effect_owner_ref": "recovery-side-effect-store://run/task",
        "side_effect_owner_digest": "1" * 64,
        "minimum_operator_refs": ("worker:implement@v1", "worker:verify@v1"),
        "executed_operator_refs": ("worker:implement@v1", "worker:verify@v1"),
        "minimum_verification_refs": ("final_verifier",),
        "executed_verification_refs": ("final_verifier",),
        "execution_owner_ref": "task-decisions://run/task",
        "execution_owner_digest": "2" * 64,
        "final_verifier_ref": "verifier://final/receipt",
        "final_verifier_digest": "3" * 64,
        "final_verifier_passed": True,
        "final_verifier_fresh_until": FRESH_UNTIL,
        "checkpoint_ref": "checkpoint-r1",
        "checkpoint_digest": "4" * 64,
        "checkpoint_requirement_revision": "requirement-r1",
        "continuity_ref": "continuity://receipt-r1",
        "continuity_digest": "5" * 64,
        "continuity_passed": True,
        "candidate_confidence": 0.95,
        "estimated_avoided_operator_count": 2,
        "estimated_avoided_tokens": 100,
        "estimated_avoided_cost_usd": 0.02,
        "owner_refs": FrozenDict(
            {
                "task": "task-state://run/task",
                "artifact": "artifact-store://run/task",
                "permission": "permission-queue://session",
                "side_effect": "recovery-side-effect-store://run/task",
                "checkpoint": "checkpoint-r1",
                "continuity": "continuity-r1",
                "verifier": "verifier-r1",
                "execution": "task-decisions://run/task",
            }
        ),
    }
    values.update(overrides)
    return ExitEligibilitySnapshot(**values)


def _failed(receipt: object) -> set[str]:
    return {
        item.condition_id
        for item in receipt.conditions
        if item.passed is False
    }


def test_truth_table_all_hard_conditions_pass_before_exit() -> None:
    gate = DeterministicEarlyExitGate(_config())
    receipt = gate.evaluate(_snapshot(), evaluated_at=NOW)
    assert receipt.decision is ExitDecision.EXIT
    assert receipt.failed_conditions == ()
    assert receipt.avoided_operator_count == 2
    assert receipt.avoided_tokens == 100
    assert receipt.verifier_refs == (
        "continuity://receipt-r1",
        "verifier://final/receipt",
    )


@pytest.mark.parametrize(
    ("overrides", "condition"),
    [
        (
            {
                "observed_at": "2026-07-30T07:55:00Z",
                "expires_at": "2026-07-30T07:59:59Z",
                "final_verifier_fresh_until": NOW,
            },
            "snapshot_fresh",
        ),
        (
            {
                "owner_refs": FrozenDict(
                    {
                        "task": "task-state://run/task",
                        "artifact": "artifact-store://run/task",
                    }
                )
            },
            "canonical_owner_evidence_complete",
        ),
        ({"final_verifier_passed": False}, "final_verifier_passed"),
        (
            {"final_verifier_fresh_until": "2026-07-30T07:59:59Z"},
            "final_verifier_passed",
        ),
        (
            {"final_verifier_fresh_until": "2026-07-30T08:05:01Z"},
            "final_verifier_passed",
        ),
        (
            {
                "verified_artifact_refs": (),
                "invalid_artifact_ids": ("deliverable",),
            },
            "required_artifacts_complete",
        ),
        (
            {"permission_pending_ids": ("permission-request-1",)},
            "permission_settled",
        ),
        (
            {"side_effect_pending_ids": ("effect-deliverable",)},
            "side_effects_settled",
        ),
        (
            {"side_effect_unknown_ids": ("effect-hidden",)},
            "side_effects_settled",
        ),
        (
            {"unresolved_critical_obligation_ids": ("produce_artifact",)},
            "critical_obligations_resolved",
        ),
        (
            {"observed_obligation_ids": ("verify_artifact",)},
            "critical_obligations_resolved",
        ),
        (
            {"executed_operator_refs": ("worker:implement@v1",)},
            "minimum_operator_path_executed",
        ),
        (
            {"checkpoint_requirement_revision": "requirement-r0"},
            "checkpoint_requirement_current",
        ),
        (
            {"continuity_passed": False},
            "memory_continuity_passed",
        ),
        (
            {"candidate_confidence": 0.2},
            "confidence_and_cost_benefit",
        ),
        (
            {"estimated_avoided_operator_count": 0},
            "confidence_and_cost_benefit",
        ),
        (
            {"estimated_avoided_tokens": 0},
            "confidence_and_cost_benefit",
        ),
    ],
)
def test_each_hard_condition_independently_forces_continue(
    overrides: dict[str, object],
    condition: str,
) -> None:
    receipt = DeterministicEarlyExitGate(_config()).evaluate(
        _snapshot(**overrides),
        evaluated_at=NOW,
    )
    assert receipt.decision is ExitDecision.CONTINUE
    assert condition in _failed(receipt)
    assert receipt.avoided_operator_count == 0


def test_disabled_gate_continues_even_when_all_owner_evidence_passes() -> None:
    receipt = DeterministicEarlyExitGate(_config()).evaluate(
        _snapshot(),
        enabled=False,
        evaluated_at=NOW,
    )
    assert receipt.decision is ExitDecision.CONTINUE
    assert _failed(receipt) == {"early_exit_enabled"}


def test_missing_or_forged_snapshot_fields_fail_closed() -> None:
    gate = DeterministicEarlyExitGate(_config())
    original = _snapshot().to_dict()

    missing = {
        **original,
        "payload": {
            key: value
            for key, value in original["payload"].items()
            if key != "permission_pending_ids"
        },
    }
    missing_receipt = gate.evaluate(missing, evaluated_at=NOW)
    assert missing_receipt.decision is ExitDecision.CONTINUE
    assert missing_receipt.failed_conditions == ("eligibility_snapshot_valid",)

    forged = {
        **original,
        "payload": {
            **original["payload"],
            "final_verifier_passed": False,
        },
    }
    forged["digest"] = canonical_digest(
        {
            key: value
            for key, value in forged.items()
            if key != "digest"
        }
    )
    forged_receipt = gate.evaluate(forged, evaluated_at=NOW)
    assert forged_receipt.decision is ExitDecision.CONTINUE
    assert "final_verifier_passed" in forged_receipt.failed_conditions

    type_confused = {
        **original,
        "payload": {
            **original["payload"],
            "final_verifier_passed": "true",
        },
    }
    type_confused["digest"] = canonical_digest(
        {
            key: value
            for key, value in type_confused.items()
            if key != "digest"
        }
    )
    confused_receipt = gate.evaluate(type_confused, evaluated_at=NOW)
    assert confused_receipt.decision is ExitDecision.CONTINUE
    assert confused_receipt.failed_conditions == (
        "eligibility_snapshot_valid",
    )


def test_adversarial_hidden_obligation_verifier_and_side_effect_mutations_fail() -> None:
    gate = DeterministicEarlyExitGate(_config())
    original = _snapshot().to_dict()
    pending_side_effect = _snapshot(
        side_effect_pending_ids=("effect-deliverable",)
    ).to_dict()
    mutations = (
        {
            **original,
            "payload": {
                **original["payload"],
                "expected_obligation_ids": ("verify_artifact",),
                "observed_obligation_ids": ("verify_artifact",),
            },
        },
        {
            **original,
            "payload": {
                **original["payload"],
                "final_verifier_digest": "0" * 64,
            },
        },
        {
            **pending_side_effect,
            "payload": {
                **pending_side_effect["payload"],
                "side_effect_pending_ids": (),
            },
        },
    )
    # These represent an attacker hiding owner evidence without possessing the
    # canonical snapshot digest. The strict parser rejects every mutation.
    assert all(
        gate.evaluate(item, evaluated_at=NOW).decision is ExitDecision.CONTINUE
        for item in mutations
    )
