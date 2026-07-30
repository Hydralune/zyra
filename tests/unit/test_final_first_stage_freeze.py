from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from zyra_evaluation.final_freeze.common import object_with_digest
from zyra_evaluation.final_freeze.critical_review import CriticalReviewEngine
from zyra_evaluation.final_freeze.handoff import (
    HandoffLedger,
    ResidualClassifier,
)
from zyra_evaluation.final_freeze.navigation import (
    EvidenceNavigationBuilder,
    EvidenceNavigationVerifier,
    RunbookEntry,
)
from zyra_evaluation.final_freeze.orchestrator import (
    FinalFreezeIdentity,
    FinalFreezeOrchestrator,
    FinalFreezeVerifier,
)
from zyra_evaluation.final_freeze.rehearsal import (
    DrillDefinition,
    RehearsalCommand,
    RehearsalPlan,
    RehearsalPolicy,
    RehearsalReceiptVerifier,
    RehearsalRunner,
)
from zyra_evaluation.final_freeze.schedule import (
    ChangeAdmissionPolicy,
    ReleaseSchedule,
)
from zyra_evaluation.final_freeze.submission import (
    DualReviewVerifier,
    ReviewerAttestation,
    SubmissionCandidateBuilder,
    SubmissionCandidateVerifier,
    SubmissionManifestBuilder,
    SubmissionMaterial,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STAGE_BASELINE = "944846fd484b465b3c4e2b4ec87565752b4baf67"
SLICE_BASELINE = "98a001a44f2e506c0ef0144e912c3ba55699f11b"
FINAL_S03_REVIEW_TARGET = "8825722359e2ca30998d42e9fccf4a35ee307f31"
RETROSPECTIVE_RUNTIME_FIX = "3c32dddf4cb81f9a00cf84bcae8e7fc6150297ed"
RETROSPECTIVE_S03_BASELINE = "6b928d96f9181acf94eb9e84cb662df39feb9be3"
S03_OUTPUT = (
    REPOSITORY_ROOT
    / "docs"
    / "reviews"
    / "evidence"
    / "M3-S03-01"
    / "generated-110e0a0a"
)
CURRENT_S03_OUTPUT = (
    REPOSITORY_ROOT
    / "docs"
    / "reviews"
    / "evidence"
    / "M3-S03-01"
    / "generated-88b88e05"
)


def head_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def evidence_commit() -> str:
    return subprocess.run(
        [
            "git",
            "log",
            "-n",
            "1",
            "--format=%H",
            "--",
            "docs/reviews/evidence/M3-S03-01/verification-summary.json",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def test_current_formal_evidence_members_have_canonical_lf_checkout_bytes() -> None:
    pointer = json.loads(
        (
            REPOSITORY_ROOT
            / "docs"
            / "reviews"
            / "evidence"
            / "M3-S02A-02"
            / "formal-current.json"
        ).read_text(encoding="utf-8")
    )
    evidence_root = Path(str(pointer["relative_evidence_root"]))
    members = sorted(
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in (REPOSITORY_ROOT / evidence_root).glob("*.json")
    )

    completed = subprocess.run(
        ["git", "check-attr", "eol", "--", *members],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    attributes = {
        line.rsplit(": eol: ", 1)[0]: line.rsplit(": eol: ", 1)[1]
        for line in completed.stdout.splitlines()
    }

    assert members
    assert attributes == {path: "lf" for path in members}


def test_historical_s03_evidence_is_blocked_by_current_summary_identity() -> None:
    receipt = CriticalReviewEngine(
        REPOSITORY_ROOT,
        S03_OUTPUT,
        expected_evidence_commit=SLICE_BASELINE,
        stage_baseline_commit=STAGE_BASELINE,
        review_target_commit=FINAL_S03_REVIEW_TARGET,
    ).review()

    assert receipt["verdict"] == "BLOCKED"
    assert receipt["blocking"] is True
    assert receipt["review_scope"] == {
        "kind": "M3-03-increment-only",
        "stage_baseline_commit": STAGE_BASELINE,
        "review_target_commit": FINAL_S03_REVIEW_TARGET,
        "protected_history_reopened": False,
        "inherited_evidence_reverified": True,
    }
    codes = {
        finding["code"]
        for finding in receipt["findings"]["findings"]
        if finding["severity"] == "blocker"
    }
    assert "evidence-target-identity-mismatch" in codes
    assert receipt["identities"]["replay_projection_count"] == 3


def test_current_s03_evidence_passes_incremental_critical_review() -> None:
    target = evidence_commit()
    receipt = CriticalReviewEngine(
        REPOSITORY_ROOT,
        CURRENT_S03_OUTPUT,
        expected_evidence_commit=target,
        stage_baseline_commit=RETROSPECTIVE_S03_BASELINE,
        review_target_commit=target,
    ).review()

    assert receipt["verdict"] == "PASS"
    assert receipt["blocking"] is False
    assert receipt["classification"]["valid_for_freeze"] is True
    assert receipt["identities"]["s03_target_commit"] == (
        "88b88e05aad14e1091f4536bcead02037622408f"
    )


def test_historical_s03_evidence_does_not_admit_later_runtime_changes() -> None:
    receipt = CriticalReviewEngine(
        REPOSITORY_ROOT,
        S03_OUTPUT,
        expected_evidence_commit=SLICE_BASELINE,
        stage_baseline_commit=STAGE_BASELINE,
        review_target_commit=RETROSPECTIVE_RUNTIME_FIX,
    ).review()

    codes = {
        finding["code"]
        for finding in receipt["findings"]["findings"]
        if finding["severity"] == "blocker"
    }
    assert receipt["verdict"] == "BLOCKED"
    assert "m3-03-runtime-boundary-change" in codes


def test_critical_review_detects_tampered_archive(tmp_path: Path) -> None:
    copied = tmp_path / "evidence"
    shutil.copytree(S03_OUTPUT, copied)
    generation = json.loads(
        (copied / "generation-receipt.json").read_text(encoding="utf-8")
    )
    archive = copied / generation["archive"]["path"]
    with archive.open("ab") as stream:
        stream.write(b"tamper")

    receipt = CriticalReviewEngine(
        REPOSITORY_ROOT,
        copied,
        expected_evidence_commit=SLICE_BASELINE,
        stage_baseline_commit=STAGE_BASELINE,
        review_target_commit=head_commit(),
    ).review()

    codes = {
        finding["code"]
        for finding in receipt["findings"]["findings"]
        if finding["severity"] == "blocker"
    }
    assert receipt["verdict"] == "BLOCKED"
    assert "archive-byte-tamper" in codes


def test_default_schedule_uses_frozen_submission_dates() -> None:
    schedule = ReleaseSchedule.default(recorded_at="2026-07-28T00:00:00Z")
    receipt = schedule.evaluate(now="2026-07-28T00:00:00Z")
    due_dates = {
        gate["gate_id"]: gate["due_at"]
        for gate in receipt["gates"]
    }

    assert receipt["submission_date"] == "2026-09-15"
    assert due_dates["rc1"].startswith("2026-09-01")
    assert due_dates["clean-rehearsal"].startswith("2026-09-08")
    assert due_dates["submission-lock"].startswith("2026-09-12")
    assert due_dates["submit"].startswith("2026-09-15")


def test_schedule_requires_evidence_and_dependency_order() -> None:
    schedule = ReleaseSchedule.default(recorded_at="2026-07-28T00:00:00Z")
    schedule = schedule.transition("stage-freeze", "ready")
    schedule = schedule.transition("stage-freeze", "running")
    with pytest.raises(ValueError, match="requires evidence"):
        schedule.transition("stage-freeze", "passed")

    schedule = schedule.transition(
        "stage-freeze",
        "passed",
        evidence_paths=(
            "docs/reviews/evidence/M3-S03-02/final-freeze-receipt.json",
        ),
    )
    schedule = schedule.transition("rc1", "ready")
    assert schedule.evaluate(now="2026-08-01T00:00:00Z")["valid"] is True


def test_overdue_schedule_gate_blocks_release() -> None:
    receipt = ReleaseSchedule.default(
        recorded_at="2026-07-28T00:00:00Z"
    ).evaluate(now="2026-09-16T00:00:00Z")

    assert receipt["valid"] is False
    assert any(
        finding["code"] == "overdue-blocking-gate"
        for finding in receipt["findings"]["findings"]
    )


def test_change_admission_rejects_post_freeze_feature() -> None:
    receipt = ChangeAdmissionPolicy().evaluate(
        {
            "change_id": "feature-after-freeze",
            "summary": "add a new demo mode",
            "category": "feature",
            "risk": "low",
            "owner": "product-owner",
            "requested_at": "2026-09-09T00:00:00Z",
            "evidence_paths": ["docs/plans/feature.md"],
            "affects_submission": False,
            "rollback_plan": "revert the feature commit",
        }
    )

    assert receipt["decision"] == "reject"
    assert receipt["findings"]["valid"] is False


def create_submission_source(root: Path) -> list[SubmissionMaterial]:
    values = {
        "src/main.py": "print('zyra')\n",
        "README.md": "# Zyra\n",
        "evidence/result.json": '{"valid":true}\n',
        "docs/runbook.md": "# Runbook\n",
    }
    for relative, content in values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return [
        SubmissionMaterial(
            source_path="src/main.py",
            archive_path="src/main.py",
            role="source",
        ),
        SubmissionMaterial(
            source_path="README.md",
            archive_path="README.md",
            role="documentation",
        ),
        SubmissionMaterial(
            source_path="evidence/result.json",
            archive_path="evidence/result.json",
            role="evidence",
        ),
        SubmissionMaterial(
            source_path="docs/runbook.md",
            archive_path="docs/runbook.md",
            role="runbook",
        ),
    ]


def test_submission_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    materials = create_submission_source(repository)
    commit = "a" * 40
    manifest = SubmissionManifestBuilder(
        repository,
        target_commit=commit,
    ).require_valid(materials)
    output = tmp_path / "candidate"
    SubmissionCandidateBuilder(repository).build(
        output,
        manifest,
        email_recipients=("submit@example.com",),
        email_subject="Zyra first-stage submission",
        instructions=(
            "attach the exact named archive",
            "compare the attachment checksum before sending",
        ),
    )

    verified = SubmissionCandidateVerifier().verify(output)
    assert verified["valid"] is True
    assert verified["archive"]["member_count"] == 4

    archive = output / manifest["archive_name"]
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr("unexpected.txt", b"tamper")
    tampered = SubmissionCandidateVerifier().verify(output)
    assert tampered["valid"] is False
    assert tampered["archive"]["valid"] is False
    codes = {
        finding["code"]
        for finding in tampered["findings"]["findings"]
    }
    assert "unexpected-archive-member" in codes
    assert "checksum-mismatch" in codes


def test_submission_verifier_rejects_self_consistent_duplicate_manifest_member(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest = SubmissionManifestBuilder(
        repository,
        target_commit="d" * 40,
    ).require_valid(create_submission_source(repository))
    output = tmp_path / "candidate"
    SubmissionCandidateBuilder(repository).build(
        output,
        manifest,
        email_recipients=("submit@example.com",),
        email_subject="Zyra first-stage submission",
        instructions=("verify checksums",),
    )

    manifest_path = output / "submission-manifest.json"
    changed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed_manifest["members"].append(
        copy.deepcopy(changed_manifest["members"][0])
    )
    changed_manifest["member_count"] += 1
    changed_manifest["total_bytes"] += changed_manifest["members"][0][
        "size_bytes"
    ]
    changed_manifest["role_counts"][
        changed_manifest["members"][0]["role"]
    ] += 1
    changed_manifest.pop("digest")
    changed_manifest = object_with_digest(changed_manifest)
    write_json(manifest_path, changed_manifest)

    email_path = output / "submission-email-checklist.json"
    email = json.loads(email_path.read_text(encoding="utf-8"))
    email["manifest_digest"] = changed_manifest["digest"]
    email.pop("digest")
    email = object_with_digest(email)
    write_json(email_path, email)

    receipt_path = output / "candidate-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["manifest_digest"] = changed_manifest["digest"]
    receipt["email_checklist_digest"] = email["digest"]
    receipt.pop("digest")
    write_json(receipt_path, object_with_digest(receipt))

    checksum_path = output / "SHA256SUMS"
    checksum_names = [
        line.split("  ", 1)[1]
        for line in checksum_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    checksum_path.write_text(
        "".join(
            f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  "
            f"{name}\n"
            for name in checksum_names
        ),
        encoding="utf-8",
    )

    verified = SubmissionCandidateVerifier().verify(output)
    codes = {
        finding["code"]
        for finding in verified["findings"]["findings"]
    }
    assert verified["valid"] is False
    assert verified["archive"]["valid"] is False
    assert "duplicate-manifest-member" in codes


def test_manifest_rejects_path_escape_and_missing_roles(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    builder = SubmissionManifestBuilder(repository, target_commit="b" * 40)
    with pytest.raises(ValueError):
        builder.build(
            [
                {
                    "source_path": "../outside.txt",
                    "archive_path": "outside.txt",
                    "role": "source",
                    "required": True,
                }
            ]
        )
    (repository / "source.py").write_text("pass\n", encoding="utf-8")
    receipt = builder.build(
        [
            SubmissionMaterial(
                source_path="source.py",
                archive_path="source.py",
                role="source",
            )
        ]
    )
    codes = {
        finding["code"]
        for finding in receipt["findings"]["findings"]
    }
    assert "missing-submission-role" in codes


def reviewer(
    identity: str,
    email: str,
    role: str,
    manifest_digest: str,
) -> ReviewerAttestation:
    return ReviewerAttestation(
        reviewer_id=identity,
        reviewer_email=email,
        role=role,
        reviewed_at="2026-09-12T00:00:00Z",
        manifest_digest=manifest_digest,
        decision="approve",
        checks=(
            "manifest",
            "checksums",
            "clean-rehearsal",
            "email-checklist",
        ),
    )


def test_dual_review_requires_distinct_identities_and_roles() -> None:
    manifest_digest = "c" * 64
    approved = DualReviewVerifier().verify(
        [
            reviewer(
                "technical-reviewer",
                "tech@example.com",
                "technical",
                manifest_digest,
            ),
            reviewer(
                "submission-reviewer",
                "submit@example.com",
                "submission",
                manifest_digest,
            ),
        ],
        manifest_digest=manifest_digest,
    )
    duplicate = DualReviewVerifier().verify(
        [
            reviewer(
                "same-reviewer",
                "same@example.com",
                "technical",
                manifest_digest,
            ),
            reviewer(
                "same-reviewer",
                "same@example.com",
                "technical",
                manifest_digest,
            ),
        ],
        manifest_digest=manifest_digest,
    )

    assert approved["valid"] is True
    assert duplicate["valid"] is False


def passing_rehearsal_plan(commit: str) -> RehearsalPlan:
    drills = []
    for kind in RehearsalPolicy.REQUIRED_KINDS:
        command = RehearsalCommand(
            command_id=f"{kind}-command",
            argv=(
                sys.executable,
                "-c",
                f"print('{kind}:ok')",
            ),
            cwd=".",
            timeout_seconds=30,
            must_contain=(f"{kind}:ok",),
        )
        drills.append(
            DrillDefinition(
                drill_id=f"{kind}-drill",
                kind=kind,
                owner="release-owner",
                description=f"exercise {kind}",
                commands=(command,),
                required=True,
            )
        )
    return RehearsalPlan(
        target_commit=commit,
        created_at="2026-07-28T00:00:00Z",
        drills=tuple(drills),
        clean_state_required=True,
    )


def test_rehearsal_runner_covers_required_failure_modes() -> None:
    plan = passing_rehearsal_plan(head_commit())
    policy = RehearsalPolicy().validate(plan, REPOSITORY_ROOT)
    receipt = RehearsalRunner(REPOSITORY_ROOT).run(
        plan,
        require_clean=False,
    )
    verified = RehearsalReceiptVerifier().verify(
        receipt,
        expected_commit=head_commit(),
    )

    assert policy["valid"] is True
    assert receipt["valid"] is True
    assert receipt["required_count"] == 7
    assert receipt["required_passed"] == 7
    assert verified["valid"] is True


def test_rehearsal_policy_rejects_root_source_dependency() -> None:
    plan = passing_rehearsal_plan(head_commit())
    bad_command = RehearsalCommand(
        command_id="bad-root-source-command",
        argv=("python", "../claude-code-best/script.py"),
        cwd=".",
        timeout_seconds=30,
    )
    bad_drill = DrillDefinition(
        drill_id="bad-drill",
        kind="clean-install",
        owner="release-owner",
        description="invalid external dependency",
        commands=(bad_command,),
    )
    changed = RehearsalPlan(
        target_commit=plan.target_commit,
        created_at=plan.created_at,
        drills=(*plan.drills[1:], bad_drill),
    )

    receipt = RehearsalPolicy().validate(changed, REPOSITORY_ROOT)
    assert receipt["valid"] is False
    assert any(
        finding["code"] == "root-source-runtime-dependency"
        for finding in receipt["findings"]["findings"]
    )


def test_navigation_is_backed_by_real_evidence_index() -> None:
    entries = []
    for index, kind in enumerate(
        sorted(EvidenceNavigationBuilder.REQUIRED_KINDS),
        start=1,
    ):
        entries.append(
            RunbookEntry(
                entry_id=f"entry-{index}",
                title=f"{kind} entry",
                kind=kind,
                path="README.md",
                purpose=f"navigate {kind}",
                acceptance=("the target remains reachable",),
                requirement_ids=("REQ-APP-001",),
            )
        )
    builder = EvidenceNavigationBuilder(REPOSITORY_ROOT, S03_OUTPUT)
    navigation = builder.build(entries)
    verification = EvidenceNavigationVerifier().verify(
        navigation,
        repository_root=REPOSITORY_ROOT,
    )

    assert navigation["valid"] is True
    assert verification["valid"] is True
    assert set(navigation["covered_kinds"]) == (
        EvidenceNavigationBuilder.REQUIRED_KINDS
    )


def test_default_navigation_targets_existing_inherited_evidence() -> None:
    entries = EvidenceNavigationBuilder(
        REPOSITORY_ROOT,
        S03_OUTPUT,
    ).default_entries()
    inherited = [
        entry
        for entry in entries
        if entry.entry_id != "submission"
    ]

    assert inherited
    assert all(
        (REPOSITORY_ROOT / entry.path).is_file()
        for entry in inherited
    )


def test_first_stage_blocker_cannot_be_moved_to_handoff() -> None:
    residual = {
        "item_id": "missing-clean-install",
        "title": "clean installation does not work",
        "description": "the release cannot start from a clean environment",
        "source": "critical-review",
        "severity": "blocker",
        "impacts": ["clean-install"],
        "runtime_reachable": True,
        "affects_submission": True,
        "security_relevant": False,
        "data_loss_possible": False,
        "workaround": "",
        "evidence_paths": ["docs/reviews/evidence/failure.json"],
        "suggested_owner": "release-owner",
    }
    decision = ResidualClassifier().classify(residual, rank=1)
    ledger = HandoffLedger().build([residual])

    assert decision.classification == "first-stage-blocker"
    assert ledger["valid"] is False
    assert ledger["classification_counts"]["first-stage-blocker"] == 1


def test_ci_and_optimization_residuals_are_executable_handoff() -> None:
    residuals = [
        {
            "item_id": "long-soak",
            "title": "extend soak duration",
            "description": "move the long-duration check into scheduled CI",
            "source": "review",
            "severity": "warning",
            "impacts": ["long-duration-soak"],
            "runtime_reachable": False,
            "affects_submission": False,
            "security_relevant": False,
            "data_loss_possible": False,
            "workaround": "current bounded soak passes",
            "evidence_paths": ["docs/reviews/soak.json"],
            "suggested_owner": "ci-owner",
        },
        {
            "item_id": "faster-report",
            "title": "optimize report rendering",
            "description": "reduce optional report rendering time",
            "source": "review",
            "severity": "observation",
            "impacts": ["report-speed"],
            "runtime_reachable": False,
            "affects_submission": False,
            "security_relevant": False,
            "data_loss_possible": False,
            "workaround": "current report completes within release budget",
            "evidence_paths": ["docs/reviews/report-timing.json"],
            "suggested_owner": "evaluation-owner",
        },
    ]
    receipt = HandoffLedger().build(residuals)
    verified = HandoffLedger().verify(receipt)

    assert receipt["valid"] is True
    assert receipt["classification_counts"] == {
        "first-stage-blocker": 0,
        "ci-hardening": 1,
        "future-optimization": 1,
    }
    assert verified["valid"] is True


def passing_components(commit: str) -> dict[str, dict[str, object]]:
    manifest_digest = "d" * 64
    critical = object_with_digest(
        {
            "schema": "zyra.final-critical-review/v1",
            "generated_at": "2026-07-28T00:00:00Z",
            "review_scope": {
                "kind": "M3-03-increment-only",
                "stage_baseline_commit": STAGE_BASELINE,
                "review_target_commit": commit,
                "protected_history_reopened": False,
                "inherited_evidence_reverified": True,
            },
            "evidence_output": "docs/reviews/evidence/M3-S03-01/output",
            "identities": {},
            "checks": {},
            "findings": {
                "valid": True,
                "counts": {
                    "blocker": 0,
                    "warning": 0,
                    "observation": 0,
                    "total": 0,
                },
                "findings": [],
                "digest": "e" * 64,
            },
            "classification": {
                "first_stage_must_fix": [],
                "second_stage_ci_hardening": [],
                "pure_optimization": [],
                "first_stage_deferred": False,
                "valid_for_freeze": True,
            },
            "blocking": False,
            "verdict": "PASS",
        },
        digest_key="review_digest",
    )
    schedule = object_with_digest(
        {
            "schema": "zyra.final-freeze.release-schedule.v1",
            "valid": True,
            "evaluated_at": "2026-07-28T00:00:00Z",
            "submission_date": "2026-09-15",
            "days_to_submission": 49,
            "require_all_passed": False,
            "gates": [
                {
                    "gate_id": gate_id,
                    "due_at": f"{due_date}T00:00:00Z",
                    "blocking": True,
                }
                for gate_id, due_date in (
                    ("stage-freeze", "2026-07-28"),
                    ("rc1", "2026-09-01"),
                    ("clean-rehearsal", "2026-09-08"),
                    ("dual-review", "2026-09-12"),
                    ("submission-lock", "2026-09-12"),
                    ("submit", "2026-09-15"),
                )
            ],
            "findings": {"valid": True},
        }
    )
    rehearsal = object_with_digest(
        {
            "schema": "zyra.final-freeze.rehearsal-receipt.v1",
            "valid": True,
            "started_from_commit": commit,
            "target_commit": commit,
            "repository": {"dirty": False},
            "environment": {},
            "policy_digest": "f" * 64,
            "drill_count": 7,
            "required_count": 7,
            "required_passed": 7,
            "drills": [
                {
                    "drill_id": f"{kind}-drill",
                    "kind": kind,
                    "required": True,
                    "valid": True,
                }
                for kind in (
                    "clean-install",
                    "offline-startup",
                    "process-restart",
                    "provider-failure",
                    "semantic-health",
                    "package-build",
                    "submission-verification",
                )
            ],
            "findings": {"valid": True},
            "completed_at": "2026-07-28T00:00:00Z",
        }
    )
    submission = object_with_digest(
        {
            "schema": "zyra.final-freeze.submission-verification.v1",
            "valid": True,
            "candidate_directory": "candidate",
            "manifest_digest": manifest_digest,
            "receipt_digest": "a" * 64,
            "email_digest": "b" * 64,
            "archive": {"valid": True},
            "checksums": {"valid": True},
            "findings": {"valid": True},
        }
    )
    dual_review = object_with_digest(
        {
            "schema": "zyra.final-freeze.dual-review.v1",
            "valid": True,
            "manifest_digest": manifest_digest,
            "attestations": [
                reviewer(
                    "technical-reviewer",
                    "technical@example.com",
                    "technical",
                    manifest_digest,
                ).to_dict(),
                reviewer(
                    "submission-reviewer",
                    "submission@example.com",
                    "submission",
                    manifest_digest,
                ).to_dict(),
            ],
            "reviewer_count": 2,
            "role_count": 2,
            "findings": {"valid": True},
        }
    )
    navigation = object_with_digest(
        {
            "schema": "zyra.final-freeze.evidence-navigation.v1",
            "valid": True,
            "entry_count": 8,
            "covered_kinds": [
                "ablation",
                "causal-trace",
                "default-entry",
                "fault-recovery",
                "final-artifact",
                "live-case",
                "semantic-health",
                "submission",
            ],
            "covered_requirement_ids": [],
            "entries": [],
            "findings": {"valid": True},
        }
    )
    handoff = object_with_digest(
        {
            "schema": "zyra.final-freeze.handoff-ledger.v1",
            "valid": True,
            "residual_count": 0,
            "classification_counts": {
                "first-stage-blocker": 0,
                "ci-hardening": 0,
                "future-optimization": 0,
            },
            "decisions": [],
            "findings": {"valid": True},
        }
    )
    return {
        "critical_review": critical,
        "schedule": schedule,
        "rehearsal": rehearsal,
        "submission": submission,
        "dual_review": dual_review,
        "navigation": navigation,
        "handoff": handoff,
    }


def freeze_identity(commit: str) -> FinalFreezeIdentity:
    return FinalFreezeIdentity(
        stage_baseline_commit=STAGE_BASELINE,
        slice_baseline_commit=SLICE_BASELINE,
        implementation_commit=commit,
        evidence_commit=None,
        s03_evidence_commit=SLICE_BASELINE,
        freeze_output_path=(
            "docs/reviews/evidence/M3-S03-01/generated-110e0a0a"
        ),
    )


def test_final_orchestrator_requires_every_gate_component() -> None:
    commit = head_commit()
    components = passing_components(commit)
    missing = dict(components)
    missing.pop("rehearsal")

    passed = FinalFreezeOrchestrator().evaluate(
        freeze_identity(commit),
        components,
    )
    blocked = FinalFreezeOrchestrator().evaluate(
        freeze_identity(commit),
        missing,
    )

    assert passed["verdict"] == "PASS"
    assert passed["final_freeze_claimed"] is True
    assert blocked["verdict"] == "BLOCKED"
    assert any(
        finding["code"] == "freeze-component-missing"
        for finding in blocked["findings"]["findings"]
    )


def test_final_orchestrator_rejects_tampered_component() -> None:
    commit = head_commit()
    components = passing_components(commit)
    components["submission"]["candidate_directory"] = "changed"

    receipt = FinalFreezeOrchestrator().evaluate(
        freeze_identity(commit),
        components,
    )

    assert receipt["verdict"] == "BLOCKED"
    assert any(
        finding["code"] == "freeze-component-digest-mismatch"
        for finding in receipt["findings"]["findings"]
    )


def test_written_final_freeze_is_independently_verified_and_tamper_evident(
    tmp_path: Path,
) -> None:
    commit = head_commit()
    components = passing_components(commit)
    output = tmp_path / "final-freeze"
    generation = FinalFreezeOrchestrator().write(
        output,
        freeze_identity(commit),
        components,
    )

    assert generation["valid"] is True
    assert FinalFreezeVerifier().verify(output)["valid"] is True

    critical_path = output / "critical-review.json"
    critical = json.loads(critical_path.read_text(encoding="utf-8"))
    critical["verdict"] = "BLOCKED"
    write_json(critical_path, critical)

    tampered = FinalFreezeVerifier().verify(output)
    assert tampered["valid"] is False
    assert any(
        finding["category"] == "tamper"
        for finding in tampered["findings"]["findings"]
    )
