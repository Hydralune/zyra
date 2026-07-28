from __future__ import annotations

import copy
import json
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from zyra_evaluation.freeze_reporting.ablation import AblationMaterialBuilder
from zyra_evaluation.freeze_reporting.archive import EvidenceArchiveVerifier
from zyra_evaluation.freeze_reporting.canonical import (
    digest,
    file_digest,
    load_json,
)
from zyra_evaluation.freeze_reporting.compatibility import (
    CompatibilityMaterialBuilder,
)
from zyra_evaluation.freeze_reporting.cases import CaseStudyBuilder
from zyra_evaluation.freeze_reporting.evidence_index import (
    FreezeEvidenceIndexBuilder,
)
from zyra_evaluation.freeze_reporting.errors import FreezeEvidenceError
from zyra_evaluation.freeze_reporting.inputs import (
    FreezeInputSet,
    load_freeze_input_set,
)
from zyra_evaluation.freeze_reporting.langgraph import (
    LangGraphCorrectionMatrixBuilder,
)
from zyra_evaluation.freeze_reporting.ledger import (
    InternalizationLedgerBuilder,
    InternalizationLedgerVerifier,
)
from zyra_evaluation.freeze_reporting.pipeline import (
    FreezeEvidencePipeline,
    verify_freeze_output,
)
from zyra_evaluation.freeze_reporting.replay import ProjectionReplayVerifier
from zyra_evaluation.freeze_reporting.scoring import ScoreMatrixVerifier
from zyra_evaluation.freeze_reporting.value import ApplicationValueBuilder


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def head_commit() -> str:
    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


@pytest.fixture(scope="module")
def admitted_inputs() -> Iterator[FreezeInputSet]:
    inputs, receipt = load_freeze_input_set(
        REPOSITORY_ROOT,
        expected_commit=head_commit(),
        require_release=False,
    )
    assert receipt["release_input"] == "release-summary"
    assert len(receipt["members"]) >= 30
    yield inputs


@pytest.fixture(scope="module")
def built_output() -> Iterator[Path]:
    temporary_root = REPOSITORY_ROOT / ".tmp"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="m3-s03-01-pytest-",
        dir=temporary_root,
    ) as directory:
        output = Path(directory) / "output"
        receipt = FreezeEvidencePipeline(
            REPOSITORY_ROOT,
            target_commit=head_commit(),
            require_release=False,
        ).build(output)
        assert receipt["valid"] is True
        yield output


def test_real_formal_inputs_build_a_complete_100_point_index(
    admitted_inputs: FreezeInputSet,
) -> None:
    builder = FreezeEvidenceIndexBuilder(
        admitted_inputs,
        target_commit=head_commit(),
    )
    index = builder.build()
    receipt = builder.verify(index)

    assert index["score"]["verified"] == 100
    assert index["score"]["complete"] is True
    assert receipt["valid"] is True
    assert len(index["requirements"]) == 19
    assert "benchmark-campaign-store" in admitted_inputs.documents
    assert len(
        [
            input_id
            for input_id in admitted_inputs.documents
            if input_id.startswith("benchmark-source-archive-")
        ]
    ) == 6


def test_score_matrix_fails_when_a_requirement_is_removed(
    admitted_inputs: FreezeInputSet,
) -> None:
    index = FreezeEvidenceIndexBuilder(
        admitted_inputs,
        target_commit=head_commit(),
    ).build()
    invalid = copy.deepcopy(index)
    invalid["requirements"].pop(next(iter(invalid["requirements"])))

    with pytest.raises(FreezeEvidenceError) as captured:
        ScoreMatrixVerifier().verify(invalid)

    assert captured.value.code == "score-evidence-incomplete"
    assert any(
        item["code"] == "score-items-missing"
        for item in captured.value.blockers
    )


def test_evidence_index_fails_on_broken_direct_link(
    admitted_inputs: FreezeInputSet,
) -> None:
    builder = FreezeEvidenceIndexBuilder(
        admitted_inputs,
        target_commit=head_commit(),
    )
    invalid = copy.deepcopy(builder.build())
    entry = next(iter(invalid["requirements"].values()))
    entry["references"][0]["path"] = "missing/evidence.json"

    with pytest.raises(FreezeEvidenceError) as captured:
        builder.verify(invalid)

    assert captured.value.phase in {"score", "link", "index"}


def test_formal_pointer_schema_is_fail_closed(
    admitted_inputs: FreezeInputSet,
) -> None:
    pointer = admitted_inputs.document("formal-pointer")
    pointer["schema"] = "zyra.unsupported-pointer/v9"

    with pytest.raises(FreezeEvidenceError) as captured:
        FreezeInputSet(REPOSITORY_ROOT)._verify_formal_pointer(pointer)

    assert captured.value.code == "formal-pointer-schema-mismatch"


def test_reviewed_release_is_mandatory_for_product_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zyra_evaluation.freeze_reporting import inputs as input_module

    monkeypatch.setattr(
        input_module,
        "RELEASE_REQUIRED_MEMBERS",
        {
            Path(
                "docs/reviews/evidence/M3-S02B-02/definitely-missing.json"
            ): "release-summary"
        },
    )
    with pytest.raises(FreezeEvidenceError) as captured:
        load_freeze_input_set(
            REPOSITORY_ROOT,
            expected_commit=head_commit(),
            require_release=True,
        )

    assert captured.value.code == "release-input-missing"


def test_reviewed_release_fails_closed_on_forged_pass(
    admitted_inputs: FreezeInputSet,
) -> None:
    summary = admitted_inputs.document("release-summary")
    metadata = admitted_inputs.document("release-metadata")
    effective = admitted_inputs.document("release-effective-code")
    summary["release_pipeline"]["failed_gate_count"] = 1
    summary["release_pipeline"]["ready"] = False

    with pytest.raises(FreezeEvidenceError) as captured:
        FreezeInputSet(
            REPOSITORY_ROOT,
            expected_commit=head_commit(),
        )._verify_release(summary, metadata, effective)

    assert any(
        item["code"] == "release-pipeline-not-ready"
        for item in captured.value.blockers
    )


def test_role_ledger_has_concrete_active_paths_and_openclaw_exclusion(
    admitted_inputs: FreezeInputSet,
) -> None:
    ledger = InternalizationLedgerBuilder(admitted_inputs).build()
    active = [
        row
        for row in ledger["rows"]
        if row["role"]
        in {"primary_implementation", "supplementary_implementation"}
    ]
    openclaw = [
        row for row in ledger["rows"] if row["source_id"].lower() == "openclaw"
    ]

    assert active
    assert all(row["target_paths"] and row["test_paths"] for row in active)
    assert len(openclaw) == 1
    assert openclaw[0]["role"] == "excluded_forward_only"
    assert not openclaw[0]["target_paths"]


def test_openclaw_cannot_acquire_a_forward_runtime_obligation(
    admitted_inputs: FreezeInputSet,
) -> None:
    ledger = InternalizationLedgerBuilder(admitted_inputs).build()
    invalid = copy.deepcopy(ledger)
    row = next(
        item
        for item in invalid["rows"]
        if item["source_id"].lower() == "openclaw"
    )
    row["role"] = "supplementary_implementation"
    row["owner"] = "ForbiddenOwner"
    row["target_paths"] = [
        "packages/evaluation/zyra_evaluation/freeze_reporting/ledger.py"
    ]

    with pytest.raises(FreezeEvidenceError) as captured:
        InternalizationLedgerVerifier(REPOSITORY_ROOT).verify(invalid)

    codes = {item["code"] for item in captured.value.blockers}
    assert "ledger-openclaw-role-forbidden" in codes
    assert "ledger-openclaw-forward-obligation-forbidden" in codes


def test_langgraph_correction_keeps_only_narrow_resume_semantics() -> None:
    matrix = LangGraphCorrectionMatrixBuilder(REPOSITORY_ROOT).build()

    assert {
        row["mechanism"] for row in matrix["active_langgraph_semantics"]
    } == {
        "checkpoint-identity-lineage",
        "pending-committed-writes",
        "side-effect-fence",
        "exact-resume",
    }
    assert all(
        not row["production_owner"]
        and not row["target_paths"]
        and not row["test_paths"]
        for row in matrix["inactive_langgraph_subsystems"]
    )


def test_langgraph_inactive_subsystem_cannot_gain_owner() -> None:
    builder = LangGraphCorrectionMatrixBuilder(REPOSITORY_ROOT)
    invalid = copy.deepcopy(builder.build())
    invalid["inactive_langgraph_subsystems"][0][
        "production_owner"
    ] = "GenericGraphRuntime"

    with pytest.raises(FreezeEvidenceError) as captured:
        builder.verify(invalid)

    assert any(
        item["code"] == "langgraph-inactive-obligation-forbidden"
        for item in captured.value.blockers
    )


def test_application_value_separates_measurement_from_projection(
    admitted_inputs: FreezeInputSet,
) -> None:
    material = ApplicationValueBuilder(admitted_inputs).build()

    assert len(material["applications"]) == 2
    assert all(
        application["labor_savings_claimed"] is False
        for application in material["applications"]
    )
    assert material["adoption_calculator_contract"][
        "result_is_projection"
    ] is True


def test_application_value_rejects_unsupported_savings_claim(
    admitted_inputs: FreezeInputSet,
) -> None:
    builder = ApplicationValueBuilder(admitted_inputs)
    invalid = copy.deepcopy(builder.build())
    invalid["applications"][0]["labor_savings_claimed"] = True

    with pytest.raises(FreezeEvidenceError) as captured:
        builder.verify(invalid)

    assert any(
        item["code"] == "application-unsupported-savings-claim"
        for item in captured.value.blockers
    )


def test_compatibility_material_uses_actual_protected_providers(
    admitted_inputs: FreezeInputSet,
) -> None:
    material = CompatibilityMaterialBuilder(admitted_inputs).build()

    assert {row["provider_id"] for row in material["providers"]} == {
        "anthropic",
        "openai",
    }
    assert {row["model_id"] for row in material["models"]} == {
        "claude-sonnet-5",
        "gpt-5.5",
    }
    assert set(material["tiers"]) == {"local", "edge", "cloud"}


def test_case_material_separates_case_runs_from_protected_provider_receipts(
    admitted_inputs: FreezeInputSet,
) -> None:
    material = CaseStudyBuilder(admitted_inputs).build()
    compatibility = material["deployment_compatibility"]

    assert compatibility["same_run_as_case"] is False
    assert compatibility["case_runs_no_new_provider_call"] is True
    assert set(compatibility["tiers"]) == {"local", "edge", "cloud"}
    assert {row["provider_id"] for row in compatibility["providers"]} == {
        "anthropic",
        "openai",
    }


def test_ablation_material_preserves_raw_samples_and_all_variants(
    admitted_inputs: FreezeInputSet,
) -> None:
    material = AblationMaterialBuilder(admitted_inputs).build()

    assert material["raw_sample_count"] == 1554
    assert len(material["variants"]) == 7
    assert len(material["comparisons"]) > 0
    assert material["verification"]["valid"] is True


def test_projection_replay_detects_duplicate_reference_identity(
    admitted_inputs: FreezeInputSet,
) -> None:
    index = FreezeEvidenceIndexBuilder(
        admitted_inputs,
        target_commit=head_commit(),
    ).build()
    invalid = copy.deepcopy(index)
    entry = next(iter(invalid["requirements"].values()))
    entry["references"].append(copy.deepcopy(entry["references"][0]))

    with pytest.raises(FreezeEvidenceError) as captured:
        ProjectionReplayVerifier().verify_index_projection(invalid)

    assert any(
        item["code"] == "replay-index-reference-duplicate"
        for item in captured.value.blockers
    )


def test_full_pipeline_builds_report_archive_and_replay_receipts(
    built_output: Path,
) -> None:
    verification = verify_freeze_output(
        built_output,
        expected_commit=head_commit(),
    )
    generation = load_json(built_output / "generation-receipt.json")

    assert verification["valid"] is True
    assert verification["score"] == 100
    assert verification["replay_projection_count"] == 3
    assert verification["task_success_recomputed"] is False
    assert generation["final_freeze_claimed"] is False
    assert generation["release_required"] is False
    assert (built_output / "freeze-report.md").is_file()
    assert (built_output / "first-stage-evidence.zip").is_file()
    with zipfile.ZipFile(
        built_output / "first-stage-evidence.zip",
        mode="r",
    ) as archive:
        source_archives = [
            name
            for name in archive.namelist()
            if name.startswith("inputs/benchmark/source-archives/")
        ]
    assert len(source_archives) == 6


def test_archive_tamper_is_rejected(
    built_output: Path,
) -> None:
    source = built_output / "first-stage-evidence.zip"
    with tempfile.TemporaryDirectory(
        prefix="m3-s03-01-tamper-",
        dir=REPOSITORY_ROOT / ".tmp",
    ) as directory:
        tampered = Path(directory) / "tampered.zip"
        with zipfile.ZipFile(source, mode="r") as original:
            with zipfile.ZipFile(
                tampered,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as changed:
                for info in original.infolist():
                    payload = original.read(info.filename)
                    if info.filename == "generated/freeze-report.json":
                        payload += b"\n"
                    changed.writestr(info, payload)

        with pytest.raises(FreezeEvidenceError) as captured:
            EvidenceArchiveVerifier().verify(
                tampered,
                expected_commit=head_commit(),
            )

    assert captured.value.phase == "archive"


def test_output_verifier_rejects_disconnected_bad_index(
    built_output: Path,
) -> None:
    temporary_root = REPOSITORY_ROOT / ".tmp"
    with tempfile.TemporaryDirectory(
        prefix="m3-s03-01-disconnect-",
        dir=temporary_root,
    ) as directory:
        output = Path(directory) / "output"
        shutil.copytree(built_output, output)
        index_path = output / "generated" / "100-point-evidence-index.json"
        index = load_json(index_path)
        index["requirements"].pop(next(iter(index["requirements"])))
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        generation_path = output / "generation-receipt.json"
        generation = load_json(generation_path)
        row = next(
            item
            for item in generation["generated"]
            if item["path"] == "generated/100-point-evidence-index.json"
        )
        row["sha256"] = file_digest(index_path)
        row["bytes"] = index_path.stat().st_size
        projection = dict(generation)
        projection.pop("receipt_digest")
        generation["receipt_digest"] = digest(projection)
        generation_path.write_text(
            json.dumps(
                generation,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        with pytest.raises(FreezeEvidenceError) as captured:
            verify_freeze_output(output, expected_commit=head_commit())

    codes = {item["code"] for item in captured.value.blockers}
    assert "freeze-output-score-verification-failed" in codes
    assert "freeze-output-archive-projection-mismatch" in codes


def test_output_verifier_rejects_schema_mismatch(
    built_output: Path,
) -> None:
    receipt = load_json(built_output / "generation-receipt.json")
    receipt["schema"] = "zyra.unsupported-generation/v9"
    projection = dict(receipt)
    projection.pop("receipt_digest")
    receipt["receipt_digest"] = digest(projection)

    from zyra_evaluation.freeze_reporting.pipeline import FreezeOutputVerifier

    with pytest.raises(FreezeEvidenceError) as captured:
        FreezeOutputVerifier(
            built_output,
            expected_commit=head_commit(),
        ).verify(receipt=receipt)

    assert any(
        item["code"] == "freeze-output-receipt-schema-invalid"
        for item in captured.value.blockers
    )
