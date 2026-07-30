from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from zyra_evaluation.policy_benchmark import (
    BASELINE_PROFILE,
    FrozenPreflightManifest,
    StrongestPreflightError,
    StrongestPreflightRunner,
    canonical_digest,
    compute_preflight_id,
)
from zyra_evaluation.policy_benchmark import preflight as preflight_module


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "config" / "phase2" / "strongest-preflight.json"
IMPLEMENTATION_COMMIT = "1" * 40


def _command_result(
    _probe,
    *,
    status: str = "passed",
    retained: bool = True,
):
    return {
        "status": status,
        "exit_code": 0 if status == "passed" else 1,
        "reason": "" if status == "passed" else "controlled failure",
        "stdout": "controlled preflight probe",
        "stderr": "",
        "retained": retained,
    }


def _rewrite_manifest(
    tmp_path: Path,
    mutate,
) -> Path:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mutate(value)
    value["preflight_id"] = compute_preflight_id(value)
    digest_payload = dict(value)
    digest_payload.pop("manifest_digest", None)
    value["manifest_digest"] = canonical_digest(digest_payload)
    path = tmp_path / "strongest-preflight.json"
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def test_frozen_manifest_has_one_profile_two_domains_and_stable_id() -> None:
    manifest = FrozenPreflightManifest.load(ROOT, MANIFEST)

    assert manifest.profile_version == "phase2_strongest_v1"
    assert manifest.preflight_id == "preflight_c34f22170d1b29f60696e895"
    assert set(manifest.evidence_bindings) == {
        "arg_designer",
        "card",
        "agentprune",
        "maas",
    }


def test_successful_preflight_admits_sealed_runs_without_activating_default() -> None:
    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        MANIFEST,
        command_probe_runner=_command_result,
    )

    result = runner.run(implementation_commit=IMPLEMENTATION_COMMIT)

    assert result.passed is True
    assert result.report["status"] == "completed"
    assert result.report["resolver_before"] == BASELINE_PROFILE
    assert result.report["resolver_after"] == BASELINE_PROFILE
    assert result.activation_report["sealed_run_admission_eligible"] is True
    assert result.activation_report["default_activation_allowed"] is False
    assert result.activation_report["conclusion"] == "admit_to_P2-S06-02"
    assert set(result.readiness_report["mechanism_statuses"].values()) == {
        "deterministic_ready"
    }
    assert result.readiness_report["readiness_stage"] == "activation_ready"
    assert result.report["training_sample_count"] == 0


def test_readiness_enforcement_disconnect_is_rejected(tmp_path: Path) -> None:
    path = _rewrite_manifest(
        tmp_path,
        lambda value: value["evidence_bindings"][0].update(
            {"status": "evidence_only"}
        ),
    )

    with pytest.raises(
        StrongestPreflightError,
        match="not implementation_validated deterministic_ready",
    ):
        FrozenPreflightManifest.load(ROOT, path)


def test_determinism_check_disconnect_is_rejected(tmp_path: Path) -> None:
    path = _rewrite_manifest(
        tmp_path,
        lambda value: value["required_checks"].remove("determinism"),
    )

    with pytest.raises(
        StrongestPreflightError,
        match="Required checks were removed",
    ):
        FrozenPreflightManifest.load(ROOT, path)


def test_no_training_audit_disconnect_fails_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = preflight_module.run_no_training_audit

    def disconnected(root, config):
        value = dict(original(root, config))
        value["passed"] = False
        value["findings"] = [
            {
                "code": "audit-disconnected",
                "path": "scripts/phase2-validation",
                "detail": "controlled mutation",
            }
        ]
        return value

    monkeypatch.setattr(
        preflight_module,
        "run_no_training_audit",
        disconnected,
    )
    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        MANIFEST,
        command_probe_runner=_command_result,
    )

    result = runner.run(implementation_commit=IMPLEMENTATION_COMMIT)

    assert result.passed is False
    assert result.report["hard_gates"]["no_policy_training"] is False
    assert result.activation_report["conclusion"] == "retain_phase1_baseline"


def test_failure_retention_disconnect_fails_gate() -> None:
    def drops_one(probe):
        return _command_result(
            probe,
            retained=probe["probe_id"] != "strongest_runtime",
        )

    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        MANIFEST,
        command_probe_runner=drops_one,
    )

    result = runner.run(implementation_commit=IMPLEMENTATION_COMMIT)

    assert result.passed is False
    assert result.report["hard_gates"]["failure_retention"] is False
    assert result.report["failure_retention"]["failures_removed"] is True


def test_failed_probe_is_retained_and_blocks_admission() -> None:
    def fails_runtime(probe):
        return _command_result(
            probe,
            status=(
                "failed"
                if probe["probe_id"] == "strongest_runtime"
                else "passed"
            ),
        )

    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        MANIFEST,
        command_probe_runner=fails_runtime,
    )

    result = runner.run(implementation_commit=IMPLEMENTATION_COMMIT)

    assert result.report["status"] == "failed"
    assert result.report["hard_gates"]["local_isolated_integration"] is False
    assert result.report["failure_retention"]["failed_receipt_count"] == 1
    assert result.report["failed_receipt_refs"]
    assert result.report["outliers"][0]["receipt_id"] == (
        "receipt_command_strongest_runtime"
    )
    assert result.activation_report["sealed_run_admission_eligible"] is False


def test_frozen_result_directory_cannot_be_overwritten(tmp_path: Path) -> None:
    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        MANIFEST,
        command_probe_runner=_command_result,
    )
    output = ROOT / ".tmp" / f"preflight-unit-{tmp_path.name}"
    try:
        first = runner.run(
            implementation_commit=IMPLEMENTATION_COMMIT,
            output_directory=output,
        )

        assert first.inventory["files"]
        with pytest.raises(StrongestPreflightError, match="cannot be overwritten"):
            runner.run(
                implementation_commit=IMPLEMENTATION_COMMIT,
                output_directory=output,
            )
    finally:
        shutil.rmtree(output, ignore_errors=True)
