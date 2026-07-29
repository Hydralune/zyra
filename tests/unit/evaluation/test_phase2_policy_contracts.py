from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from zyra_evaluation.policy_benchmark import (
    ContractViolation,
    MechanismReadinessStatus,
    Phase2PolicyContractBundle,
    compute_frozen_gate_digest,
    parse_mechanism_status,
    validate_activation_contract,
    validate_source_role_registry,
)


ROOT = Path(__file__).resolve().parents[3]
CONFIG_ROOT = ROOT / "config" / "phase2"


def _load(name: str) -> dict[str, object]:
    return json.loads((CONFIG_ROOT / name).read_text(encoding="utf-8"))


def _refresh_gate_digest(contract: dict[str, object]) -> None:
    contract["frozen_gate_digest"] = compute_frozen_gate_digest(contract)


def _copy_contracts(target: Path) -> Path:
    target.mkdir(parents=True)
    for source in CONFIG_ROOT.iterdir():
        if source.is_file():
            shutil.copy2(source, target / source.name)
    return target


def test_production_bundle_validates_and_remains_inactive() -> None:
    report = Phase2PolicyContractBundle.load(ROOT).validate()

    assert report.valid is True
    assert report.source_roles["topology_primary"] == ["arg_designer"]
    assert report.source_roles["topology_supplementary"] == ["card", "agentprune"]
    assert report.activation_gates["hard_gate_count"] >= 20
    assert report.activation_gates["strongest_activation_eligible"] is False
    assert set(report.activation_gates["strongest_activation_blockers"]) == {
        "loopx",
        "arg_designer",
        "card",
        "agentprune",
        "maas",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("deterministic_ready", MechanismReadinessStatus.DETERMINISTIC_READY),
        ("evidence_only", MechanismReadinessStatus.EVIDENCE_ONLY),
        ("unavailable", MechanismReadinessStatus.UNAVAILABLE),
    ],
)
def test_mechanism_status_parser_accepts_only_frozen_values(
    raw: str,
    expected: MechanismReadinessStatus,
) -> None:
    assert parse_mechanism_status(raw) is expected

    with pytest.raises(ContractViolation, match="Unsupported mechanism"):
        parse_mechanism_status("ready")


@pytest.mark.parametrize("blocking_status", ["evidence_only", "unavailable"])
def test_non_ready_mechanism_cannot_enter_strongest_default(
    blocking_status: str,
) -> None:
    contract = _load("activation-gates.yaml")
    mechanisms = contract["readiness"]["mechanisms"]  # type: ignore[index]
    for mechanism in mechanisms:
        mechanism["status"] = "deterministic_ready"
    mechanisms[2]["status"] = blocking_status
    strongest = next(
        profile
        for profile in contract["profiles"]  # type: ignore[union-attr]
        if profile["profile_id"] == "phase2_strongest_v1"
    )
    strongest["lifecycle"] = "default"
    strongest["activation_state"] = "active"
    _refresh_gate_digest(contract)

    with pytest.raises(ContractViolation) as failure:
        validate_activation_contract(contract)

    assert failure.value.code == "strongest-profile-readiness-failed"


def test_training_entry_dataset_checkpoint_and_learned_parameter_fail_closed() -> None:
    for field, value in (
        ("runtime_entry_points", "policy-gradient"),
        ("datasets", "policy-training-dataset"),
        ("checkpoints", "learned-router.ckpt"),
        ("mutable_learned_parameters", "routing_weight"),
    ):
        contract = _load("activation-gates.yaml")
        contract["no_policy_training"][field].append(value)  # type: ignore[index]
        _refresh_gate_digest(contract)

        with pytest.raises(ContractViolation) as failure:
            validate_activation_contract(contract)

        assert failure.value.code == "no-policy-training-artifact-present"


@pytest.mark.parametrize(
    "field",
    ["unit", "direction", "sample_unit", "empty_sample_semantics"],
)
def test_metric_missing_required_semantics_is_invalid(field: str) -> None:
    contract = _load("activation-gates.yaml")
    del contract["metrics"][0][field]  # type: ignore[index]
    _refresh_gate_digest(contract)

    with pytest.raises(ContractViolation):
        validate_activation_contract(contract)


def test_frozen_threshold_change_requires_superseding_adr() -> None:
    contract = _load("activation-gates.yaml")
    contract["metrics"][0]["threshold"] = 0.5  # type: ignore[index]

    with pytest.raises(ContractViolation) as failure:
        validate_activation_contract(contract)

    assert failure.value.code == "frozen-gate-modified-without-adr"


def test_every_hard_gate_traces_to_requirements_and_evidence_contracts() -> None:
    contract = _load("activation-gates.yaml")
    hard_metrics = [
        metric for metric in contract["metrics"] if metric["hard_gate"] is True
    ]
    assert hard_metrics
    assert all(metric["requirement_ids"] for metric in hard_metrics)
    assert all(metric["evidence_contract_ids"] for metric in hard_metrics)

    hard_metrics[0]["requirement_ids"] = []
    _refresh_gate_digest(contract)
    with pytest.raises(ContractViolation) as failure:
        validate_activation_contract(contract)
    assert failure.value.code == "hard-gate-traceability-missing"


def test_source_language_and_migration_mode_are_machine_auditable() -> None:
    registry = _load("source-roles.yaml")
    summary = validate_source_role_registry(registry)
    assert summary["source_count"] == 9

    invalid = copy.deepcopy(registry)
    invalid["capability_domains"][1]["entries"][0]["source_languages"] = []
    with pytest.raises(ContractViolation) as failure:
        validate_source_role_registry(invalid)
    assert failure.value.code == "source-language-custody-missing"

    invalid = copy.deepcopy(registry)
    invalid["capability_domains"][1]["entries"][0]["migration_mode"] = ""
    with pytest.raises(ContractViolation):
        validate_source_role_registry(invalid)


def test_semantically_valid_registry_tamper_is_stopped_by_digest(
    tmp_path: Path,
) -> None:
    copied = _copy_contracts(tmp_path / "phase2")
    source_roles = copied / "source-roles.yaml"
    source_roles.write_text(
        source_roles.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractViolation) as failure:
        Phase2PolicyContractBundle.load(ROOT, config_root=copied).validate()

    assert failure.value.code == "contract-digest-mismatch"


def test_missing_registry_stops_contract_resolution(tmp_path: Path) -> None:
    copied = _copy_contracts(tmp_path / "phase2")
    (copied / "state-owners.yaml").unlink()

    with pytest.raises(ContractViolation) as failure:
        Phase2PolicyContractBundle.load(ROOT, config_root=copied)

    assert failure.value.code == "contract-file-missing"
