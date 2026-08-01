from __future__ import annotations

import json
import shutil
import subprocess
import sys
from uuid import uuid4
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
IMPLEMENTATION_COMMIT = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=ROOT,
    capture_output=True,
    check=True,
    text=True,
).stdout.strip()


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
    value = json.loads(_prepared_manifest(tmp_path).read_text(encoding="utf-8"))
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


def _prepared_manifest(tmp_path: Path) -> Path:
    generated = ROOT / ".tmp" / f"preflight-test-{uuid4().hex}.json"
    relative = generated.relative_to(ROOT).as_posix()
    try:
        subprocess.run(
            [
                sys.executable,
                "scripts/release/prepare_phase2_final_preflight_manifest.py",
                "--target-commit",
                IMPLEMENTATION_COMMIT,
                "--output",
                relative,
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        selected = tmp_path / f"prepared-{uuid4().hex}.json"
        shutil.copyfile(generated, selected)
        return selected
    finally:
        generated.unlink(missing_ok=True)


def test_frozen_manifest_has_one_profile_two_domains_and_stable_id(
    tmp_path: Path,
) -> None:
    manifest = FrozenPreflightManifest.load(ROOT, _prepared_manifest(tmp_path))

    assert manifest.profile_version == "phase2_strongest_v1"
    assert manifest.preflight_id.startswith("preflight_")
    assert len(manifest.preflight_id) == 34
    assert manifest.execution_mode == "active_default_revalidation"
    assert set(manifest.evidence_bindings) == {
        "arg_designer",
        "card",
        "agentprune",
        "maas",
    }


def test_successful_preflight_revalidates_active_default(tmp_path: Path) -> None:
    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        _prepared_manifest(tmp_path),
        command_probe_runner=_command_result,
    )

    result = runner.run(implementation_commit=IMPLEMENTATION_COMMIT)

    assert result.passed is True
    assert result.report["status"] == "completed"
    assert result.report["resolver_before"] == "phase2_strongest_v1"
    assert result.report["resolver_after"] == "phase2_strongest_v1"
    assert result.activation_report["sealed_run_admission_eligible"] is True
    assert result.activation_report["default_activation_allowed"] is True
    assert (
        result.activation_report["conclusion"]
        == "phase2_strongest_v1_revalidated"
    )
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


def test_implementation_commit_mismatch_fails_closed(tmp_path: Path) -> None:
    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        _prepared_manifest(tmp_path),
        command_probe_runner=_command_result,
    )

    with pytest.raises(
        StrongestPreflightError,
        match="must equal the current Git HEAD",
    ):
        runner.run(implementation_commit="2" * 40)


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
    tmp_path: Path,
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
        _prepared_manifest(tmp_path),
        command_probe_runner=_command_result,
    )

    result = runner.run(implementation_commit=IMPLEMENTATION_COMMIT)

    assert result.passed is False
    assert result.report["hard_gates"]["no_policy_training"] is False
    assert result.activation_report["conclusion"] == "retain_phase1_baseline"


def test_failure_retention_disconnect_fails_gate(tmp_path: Path) -> None:
    def drops_one(probe):
        return _command_result(
            probe,
            retained=probe["probe_id"] != "strongest_runtime",
        )

    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        _prepared_manifest(tmp_path),
        command_probe_runner=drops_one,
    )

    result = runner.run(implementation_commit=IMPLEMENTATION_COMMIT)

    assert result.passed is False
    assert result.report["hard_gates"]["failure_retention"] is False
    assert result.report["failure_retention"]["failures_removed"] is True


def test_failed_probe_is_retained_and_blocks_admission(tmp_path: Path) -> None:
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
        _prepared_manifest(tmp_path),
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


def test_passing_probe_warning_is_retained_as_outlier(tmp_path: Path) -> None:
    def warns_once(probe):
        value = dict(_command_result(probe))
        if probe["probe_id"] == "strongest_runtime":
            value["stdout"] = "warnings summary\n1 warning"
        return value

    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        _prepared_manifest(tmp_path),
        command_probe_runner=warns_once,
    )

    result = runner.run(implementation_commit=IMPLEMENTATION_COMMIT)

    assert result.passed is True
    assert result.report["outliers"] == [
        {
            "receipt_id": "receipt_command_strongest_runtime",
            "status": "warning",
            "reason": "command emitted a retained warning summary",
        }
    ]


def test_frozen_result_directory_cannot_be_overwritten(tmp_path: Path) -> None:
    runner = StrongestPreflightRunner.from_manifest(
        ROOT,
        _prepared_manifest(tmp_path),
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
