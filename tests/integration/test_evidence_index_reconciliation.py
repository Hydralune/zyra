from __future__ import annotations

import json
import subprocess
from pathlib import Path

from zyra_productization.release import Phase2BaselineVerifier
from zyra_productization.release.integrity import sha256_file, stable_digest


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    project = tmp_path / "zyra"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.email", "phase2@example.invalid")
    _git(project, "config", "user.name", "Phase 2 Test")
    evidence = project / "evidence.json"
    _write_json(evidence, {"schema": "evidence/v1", "target_commit": ""})
    _git(project, "add", "evidence.json")
    _git(project, "commit", "-m", "fixture")
    commit = _git(project, "rev-parse", "HEAD")
    tree = _git(project, "rev-parse", "HEAD^{tree}")
    _write_json(
        evidence,
        {"schema": "evidence/v1", "target_commit": commit},
    )
    _git(project, "add", "evidence.json")
    _git(project, "commit", "-m", "bind fixture")
    commit = _git(project, "rev-parse", "HEAD")
    tree = _git(project, "rev-parse", "HEAD^{tree}")
    _write_json(
        evidence,
        {"schema": "evidence/v1", "target_commit": commit},
    )
    _git(project, "add", "evidence.json")
    _git(project, "commit", "--amend", "--no-edit")
    commit = _git(project, "rev-parse", "HEAD")
    tree = _git(project, "rev-parse", "HEAD^{tree}")
    # The binding value is evidence metadata rather than the current commit to
    # avoid a self-referential Git object.  It must still name an ancestor.
    parent = _git(project, "rev-parse", "HEAD^")
    _write_json(
        evidence,
        {"schema": "evidence/v1", "target_commit": parent},
    )
    _git(project, "add", "evidence.json")
    _git(project, "commit", "--amend", "--no-edit")
    commit = _git(project, "rev-parse", "HEAD")
    tree = _git(project, "rev-parse", "HEAD^{tree}")

    reference = {
        "id": "evidence",
        "scope": "project",
        "path": "evidence.json",
        "kind": "evidence_index",
        "sha256": sha256_file(evidence),
        "size_bytes": evidence.stat().st_size,
        "source_commit": parent,
        "json_bindings": [
            {"pointer": "/target_commit", "expected": parent},
        ],
    }
    lineage = [
        {"role": role, "commit": parent}
        for role in (
            "benchmark_implementation",
            "first_stage_report_evidence",
            "first_stage_final_freeze_evidence",
            "first_stage_report",
        )
    ]
    manifest: dict[str, object] = {
        "schema": "zyra.phase2-baseline-manifest/v1",
        "slice_id": "P2-S00-01",
        "identity": {
            "p2_base_commit": commit,
            "p2_base_tree": tree,
        },
        "lineage": lineage,
        "mechanism_profile": {
            "profile_id": "phase2_strongest_v1",
            "activation_state": "baseline_frozen_not_activated",
        },
        "training_policy": {
            "training_allowed": False,
            "evidence_reuse": "read_only",
        },
        "references": [reference],
        "evidence_inventory": {
            "classification": "read_only_evidence_inventory",
            "training_eligible": False,
            "reference_ids": ["evidence"],
            "evidence_volume": {
                "semantic_label": "evidence_volume_only",
                "training_sample_count": 0,
            },
        },
    }
    manifest["manifest_digest"] = stable_digest(manifest)
    manifest_path = project / "phase2-baseline-manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path, manifest


def test_phase2_baseline_reconciles_all_digests_and_commit_bindings(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _fixture(tmp_path)
    receipt = Phase2BaselineVerifier(
        manifest_path.parent,
        workspace_root=tmp_path,
    ).verify(manifest_path)
    assert receipt["valid"] is True
    assert receipt["finding_count"] == 0
    assert receipt["reference_count"] == 1


def test_phase2_baseline_tamper_and_mismatched_evidence_commit_fail_closed(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _fixture(tmp_path)
    evidence = manifest_path.parent / "evidence.json"
    evidence.write_text(
        evidence.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    verifier = Phase2BaselineVerifier(
        manifest_path.parent,
        workspace_root=tmp_path,
    )
    tampered = verifier.verify(manifest_path)
    assert tampered["valid"] is False
    assert {
        finding["code"] for finding in tampered["findings"]
    } >= {
        "baseline_reference_digest_mismatch",
        "baseline_reference_size_mismatch",
    }

    _write_json(
        evidence,
        {"schema": "evidence/v1", "target_commit": "f" * 40},
    )
    reference = manifest["references"][0]
    assert isinstance(reference, dict)
    reference["sha256"] = sha256_file(evidence)
    reference["size_bytes"] = evidence.stat().st_size
    manifest["manifest_digest"] = stable_digest(
        {
            key: value
            for key, value in manifest.items()
            if key != "manifest_digest"
        }
    )
    _write_json(manifest_path, manifest)
    mismatch = verifier.verify(manifest_path)
    assert mismatch["valid"] is False
    assert "baseline_reference_binding_mismatch" in {
        finding["code"] for finding in mismatch["findings"]
    }
