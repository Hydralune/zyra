from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from tests.experiment_support import experiment_request, experiment_runtime
from zyra_evaluation.experiment_runtime import (
    EvidenceBundleVerifier,
    ExperimentError,
    M2ExitPortfolioVerifier,
    SourceRoleExitAuditor,
    requirement_definitions,
)


def test_formal_matrix_statistics_requirements_and_bundle(
    tmp_path: Path,
) -> None:
    runtime = experiment_runtime(tmp_path)
    try:
        admitted = runtime.create(experiment_request())
        assert admitted.phase.value == "admitted"
        assert len(admitted.variants) == 7
        assert len(admitted.cells) == 21
        assert {item.variant_id for item in admitted.variants} == {
            "single_agent",
            "static_full_connect_multi_agent",
            "dynamic_heterogeneous_swarm",
            "no_scheduler",
            "no_memory_compact",
            "no_recovery",
            "no_low_entropy_communication",
        }
        completed = runtime.start(admitted.experiment_id, wait=True, timeout=180)
        assert completed.phase.value == "succeeded", completed.failure
        assert all(item.phase.value == "succeeded" for item in completed.cells)

        status = runtime.status(completed.experiment_id)
        report = runtime.report(completed.experiment_id)
        bundle = runtime.bundle(completed.experiment_id)
        reverified = runtime.verify(completed.experiment_id)
        assert status["sample_count"] == 21 * 32
        assert status["sample_status_counts"] == {"observed": 21 * 32}
        assert report["statistics"]["raw_sample_count"] == 21 * 32
        assert report["statistics"]["p50_present_count"] == 7 * 32
        assert report["statistics"]["p95_present_count"] == 7 * 32
        assert report["statistics"]["dispersion_present_count"] == 7 * 32
        assert report["statistics"]["confidence_present_count"] == 7 * 32
        assert report["requirements"]["score_total"] == 100
        assert report["requirements"]["score_verified"] == 100
        assert report["requirements"]["verified_count"] == len(
            requirement_definitions()
        )
        assert all(
            row["verified"] is True for row in report["requirements"]["rows"]
        )
        assert report["reviewer_navigation"]["backend_log_required"] is False
        assert report["method"]["non_claims"] == [
            "No authenticated provider/model CLI was invoked.",
            "No new external model request was made.",
            "M2 controlled executions are not relabeled as new cloud/model dispatch.",
            "Prior M1 local/edge/cloud and provider/model receipts remain external frozen evidence.",
        ]
        assert bundle["verification"]["valid"] is True
        assert reverified["valid"] is True

        summaries = {
            (item["metric"], item["variant_id"]): item
            for item in report["statistics"]["summaries"]
        }
        assert (
            summaries[("fault_recovery_rate", "dynamic_heterogeneous_swarm")][
                "p50"
            ]
            > summaries[("fault_recovery_rate", "no_recovery")]["p50"]
        )
        assert (
            summaries[("memory_write_count", "dynamic_heterogeneous_swarm")][
                "p50"
            ]
            > summaries[("memory_write_count", "no_memory_compact")]["p50"]
        )
        assert (
            summaries[
                (
                    "communication_delivery_count",
                    "no_low_entropy_communication",
                )
            ]["p50"]
            > summaries[
                (
                    "communication_delivery_count",
                    "dynamic_heterogeneous_swarm",
                )
            ]["p50"]
        )

        tampered = tmp_path / "tampered.zip"
        shutil.copyfile(bundle["bundle_path"], tampered)
        with zipfile.ZipFile(tampered, "a") as archive:
            archive.writestr(
                "report/final-report.json",
                b'{"schema":"tampered"}',
            )
        receipt = EvidenceBundleVerifier().verify(tampered)
        assert receipt["valid"] is False
        assert receipt["findings"]
    finally:
        runtime.close(wait=True)


@pytest.mark.parametrize(
    ("flag", "expected_error"),
    (
        ("ablation", "experiment_ablation_verifier_disabled"),
        ("metric", "experiment_metric_aggregator_disabled"),
    ),
)
def test_disabling_exit_owner_fails_closed(
    tmp_path: Path,
    flag: str,
    expected_error: str,
) -> None:
    runtime = experiment_runtime(
        tmp_path,
        suffix=flag,
        enable_ablation_verifier=flag != "ablation",
        enable_metric_aggregator=flag != "metric",
    )
    try:
        admitted = runtime.create(
            experiment_request(title=f"disabled {flag} test")
        )
        completed = runtime.start(admitted.experiment_id, wait=True, timeout=180)
        assert completed.phase.value == "failed"
        assert completed.failure
        assert completed.failure["error"] == expected_error
        assert completed.failure["fallback"] is False
        with pytest.raises(ExperimentError):
            runtime.report(completed.experiment_id)
    finally:
        runtime.close(wait=True)


def test_evidence_verifier_disable_and_source_roles_fail_closed(
    tmp_path: Path,
) -> None:
    enabled = experiment_runtime(tmp_path, suffix="evidence-source")
    try:
        admitted = enabled.create(experiment_request())
        completed = enabled.start(admitted.experiment_id, wait=True, timeout=180)
        assert completed.phase.value == "succeeded", completed.failure
        store = enabled.store
        enabled.close(wait=True)
        disabled = type(enabled)(
            project_root=enabled.project_root,
            store=store,
            artifact_root=enabled.artifact_root,
            allowed_source_roots=(enabled.project_root, tmp_path),
            enable_evidence_verifier=False,
            auto_reconcile=False,
        )
        try:
            with pytest.raises(ExperimentError) as error:
                disabled.bundle(completed.experiment_id)
            assert error.value.code == "experiment_evidence_verifier_disabled"
            assert error.value.response()["fallback"] is False
        finally:
            disabled.close(wait=True)
    finally:
        enabled.close(wait=True)

    source_audit = SourceRoleExitAuditor(
        project_root=Path(__file__).resolve().parents[2]
    ).require_valid()
    assert source_audit["openclaw"] == "excluded_forward_only"
    assert source_audit["runtime_dependency_scan"]["valid"] is True
    assert [row for row in source_audit["rows"] if row["active"]] == [
        next(
            row
            for row in source_audit["rows"]
            if row["source"] == "zyra"
        )
    ]


def test_frozen_exit_portfolio_verifies_and_tamper_fails(
    tmp_path: Path,
) -> None:
    portfolio = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "reviews"
        / "evidence"
        / "M2-S05-03"
        / "M2-exit-competition-evidence-bundle.zip"
    )
    if not portfolio.is_file():
        pytest.skip("Formal evidence portfolio is generated after implementation commit.")
    verifier = M2ExitPortfolioVerifier()
    assert verifier.require_valid(portfolio)["valid"] is True
    tampered = tmp_path / "tampered-portfolio.zip"
    shutil.copyfile(portfolio, tampered)
    with zipfile.ZipFile(tampered, "a") as archive:
        archive.writestr(
            "portfolio/final-exit-report.json",
            b'{"schema":"tampered"}',
        )
    assert verifier.verify(tampered)["valid"] is False
