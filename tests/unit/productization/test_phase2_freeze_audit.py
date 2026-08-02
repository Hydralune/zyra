from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
PRODUCTIZATION_ROOT = ROOT / "packages" / "productization"
if str(PRODUCTIZATION_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION_ROOT))

from zyra_productization.release.phase2_freeze import (
    Phase2FreezeAuditor,
    Phase2FreezeError,
    canonical_digest,
    inspect_release_archive,
)
from zyra_productization.release import phase2_freeze
from zyra_productization.release.worktree import inspect_worktree


def _archive(
    path: Path,
    *,
    release_member: str = "zyra-release/packages/core/module.py",
    wheel_member: str = "zyra_core/module.py",
    sbom_path: str = "packages/core/module.py",
    source_commit: str = "a" * 40,
) -> Path:
    wheel_buffer = io.BytesIO()
    with zipfile.ZipFile(wheel_buffer, "w") as wheel:
        wheel.writestr(wheel_member, b"pass\n")
    sbom = json.dumps(
        {
            "bomFormat": "CycloneDX",
            "components": [{"name": "zyra", "path": sbom_path}],
        }
    ).encode()
    release_manifest = json.dumps(
        {
            "schema": "zyra.release-manifest/v1",
            "release_id": "unit-test",
            "source_commit": source_commit,
        }
    ).encode()
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in (
            (release_member, b"pass\n"),
            (
                "zyra-release/release/wheels/zyra.whl",
                wheel_buffer.getvalue(),
            ),
            ("zyra-release/release/sbom.cdx.json", sbom),
            ("zyra-release/release/manifest.json", release_manifest),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_release_archive_audit_accepts_clean_source_wheel_and_sbom(
    tmp_path: Path,
) -> None:
    result = inspect_release_archive(_archive(tmp_path / "release.tar.gz"))

    assert result["ready"] is True
    assert result["source_archive_vendor_root_count"] == 0
    assert result["release_vendor_root_count"] == 0
    assert result["wheel_vendor_root_count"] == 0
    assert result["sbom_vendor_root_count"] == 0
    assert result["release_manifest"]["source_commit"] == "a" * 40


def test_release_archive_audit_rejects_vendor_roots_in_every_boundary(
    tmp_path: Path,
) -> None:
    result = inspect_release_archive(
        _archive(
            tmp_path / "release.tar.gz",
            release_member="zyra-release/vendor/source.py",
            wheel_member="vendor/runtime.py",
            sbom_path="vendor/component.py",
        )
    )

    assert result["ready"] is False
    assert result["release_vendor_root_count"] == 1
    assert result["wheel_vendor_root_count"] == 1
    assert result["sbom_vendor_root_count"] == 1


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_worktree_boundary_allows_generated_evidence_but_rejects_source(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Zyra Test")
    _git(tmp_path, "config", "user.email", "zyra@example.invalid")
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "source.py")
    _git(tmp_path, "commit", "-m", "baseline")
    head = _git(tmp_path, "rev-parse", "HEAD")

    generated = (
        tmp_path / "docs" / "evidence" / "phase2" / "receipt.json"
    )
    generated.parent.mkdir(parents=True)
    generated.write_text("{}\n", encoding="utf-8")
    allowed = inspect_worktree(tmp_path, expected_head=head)
    assert allowed["ready"] is True
    assert allowed["ignored_generated_entry_count"] == 1

    source.write_text("VALUE = 2\n", encoding="utf-8")
    dirty = inspect_worktree(tmp_path, expected_head=head)
    assert dirty["ready"] is False
    assert dirty["tracked_dirty_entries"]


def test_worktree_boundary_rejects_untracked_source(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Zyra Test")
    _git(tmp_path, "config", "user.email", "zyra@example.invalid")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "baseline")
    head = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "unexpected.py").write_text("pass\n", encoding="utf-8")

    receipt = inspect_worktree(tmp_path, expected_head=head)
    assert receipt["ready"] is False
    assert receipt["unexpected_untracked_entries"] == ["?? unexpected.py"]


def test_worktree_boundary_allows_non_ascii_generated_path(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Zyra Test")
    _git(tmp_path, "config", "user.email", "zyra@example.invalid")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "baseline")
    head = _git(tmp_path, "rev-parse", "HEAD")
    generated = (
        tmp_path
        / "docs"
        / "evidence"
        / "phase2"
        / "competition-workspace"
        / "比赛要求追踪矩阵.md"
    )
    generated.parent.mkdir(parents=True)
    generated.write_text("generated\n", encoding="utf-8")

    receipt = inspect_worktree(tmp_path, expected_head=head)

    assert receipt["ready"] is True
    assert receipt["ignored_generated_entry_count"] == 1
    assert receipt["unexpected_untracked_entries"] == []


def test_final_regression_audit_rejects_command_or_cwd_substitution(
    tmp_path: Path,
) -> None:
    auditor = Phase2FreezeAuditor(ROOT)
    target = "a" * 40
    expected = auditor._expected_regression_commands(
        output_root=tmp_path,
        target=target,
        loopx_base_checkout=tmp_path / "loopx-base",
    )
    commands = [
        {
            "command_id": command_id,
            "argv": list(argv),
            "cwd": str(ROOT),
        }
        for command_id, argv in expected.items()
    ]
    assert auditor._regression_command_policy_blockers(
        commands,
        output_root=tmp_path,
        target=target,
        loopx_base_checkout=tmp_path / "loopx-base",
    ) == []

    commands[0]["argv"] = [sys.executable, "-c", "pass"]
    commands[1]["cwd"] = ""
    blockers = auditor._regression_command_policy_blockers(
        commands,
        output_root=tmp_path,
        target=target,
        loopx_base_checkout=tmp_path / "loopx-base",
    )
    assert "command_argv:python-full-regression" in blockers
    assert "command_cwd:typescript-runtime-regression" in blockers


def test_resume_delta_audit_rejects_production_change(monkeypatch) -> None:
    auditor = Phase2FreezeAuditor(ROOT)
    source = "a" * 40
    target = "b" * 40
    first = phase2_freeze.FINAL_REGRESSION_RESUME_REQUIRED_FIRST_COMMIT

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            commit = arguments[-1]
            return f"{commit} {source if commit == first else first}"
        if arguments[:3] == ("diff", "--name-status", "--no-renames"):
            return "M\tpackages/runtime/production.py"
        if arguments[:2] == ("rev-parse", f"{source}^{{tree}}"):
            return "c" * 40
        if arguments[:2] == ("rev-parse", f"{target}^{{tree}}"):
            return "d" * 40
        raise AssertionError(arguments)

    monkeypatch.setattr(auditor, "_git", fake_git)
    monkeypatch.setattr(
        phase2_freeze.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"forbidden-diff"),
    )

    audit = auditor._resume_delta_audit(
        {},
        source_target=source,
        target=target,
    )

    assert audit["ready"] is False
    assert "forbidden_target_change:M\tpackages/runtime/production.py" in audit[
        "blockers"
    ]


def test_resume_delta_audit_rejects_wrong_chain_before_any_diff(
    monkeypatch,
) -> None:
    auditor = Phase2FreezeAuditor(ROOT)
    source = "a" * 40
    target = "d" * 40
    first = phase2_freeze.FINAL_REGRESSION_RESUME_REQUIRED_FIRST_COMMIT

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            commit = arguments[-1]
            return f"{commit} {source if commit == first else 'e' * 40}"
        raise AssertionError("invalid exact chain reached a diff or tree query")

    monkeypatch.setattr(auditor, "_git", fake_git)
    monkeypatch.setattr(
        phase2_freeze.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid exact chain reached subprocess diff")
        ),
    )

    audit = auditor._resume_delta_audit(
        {},
        source_target=source,
        target=target,
    )

    assert "target_not_exact_two_commit_remediation_chain" in audit["blockers"]
    assert audit["commit_path_changes"] == []


def test_resume_delta_audit_rejects_transient_mode_change(monkeypatch) -> None:
    auditor = Phase2FreezeAuditor(ROOT)
    source = "a" * 40
    target = "d" * 40
    first = phase2_freeze.FINAL_REGRESSION_RESUME_REQUIRED_FIRST_COMMIT
    changed_path = phase2_freeze.FINAL_REGRESSION_RESUME_ALLOWED_PATHS[0]
    cumulative = "\n".join(
        f"M\t{path}"
        for path in phase2_freeze.FINAL_REGRESSION_RESUME_ALLOWED_PATHS
    )

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            commit = arguments[-1]
            return f"{commit} {source if commit == first else first}"
        if arguments[:3] == ("diff", "--name-status", "--no-renames"):
            return cumulative if arguments[-2:] == (source, target) else f"M\t{changed_path}"
        if arguments[0] == "ls-tree":
            revision = arguments[1]
            path = arguments[-1]
            mode = "100755" if revision == first and path == changed_path else "100644"
            return f"{mode} blob {'c' * 40}\t{path}"
        if arguments[:2] == ("rev-parse", f"{source}^{{tree}}"):
            return "e" * 40
        if arguments[:2] == ("rev-parse", f"{target}^{{tree}}"):
            return "f" * 40
        raise AssertionError(arguments)

    monkeypatch.setattr(auditor, "_git", fake_git)
    monkeypatch.setattr(
        phase2_freeze.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"bounded-diff"),
    )

    audit = auditor._resume_delta_audit(
        {},
        source_target=source,
        target=target,
    )

    assert any(
        blocker.startswith("remediation_commit_mode_or_type:")
        for blocker in audit["blockers"]
    )


def test_option_like_resume_source_is_rejected_before_git(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auditor = Phase2FreezeAuditor(ROOT)

    def forbidden(*args, **kwargs):
        raise AssertionError("Git or subprocess must not run for an invalid commit id")

    monkeypatch.setattr(auditor, "_git", forbidden)
    monkeypatch.setattr(phase2_freeze.subprocess, "run", forbidden)
    target = "b" * 40
    result = auditor._resume_receipt_ready(
        tmp_path / "missing.json",
        {"source_target_commit": "--output=G:/forbidden"},
        target=target,
        actual_receipt_sha256="a" * 64,
        expected_receipt_sha256="a" * 64,
    )

    assert result["ready"] is False
    assert result["blockers"] == ["source_target_commit"]
    with pytest.raises(Phase2FreezeError, match="full lowercase commit"):
        auditor._resume_delta_audit(
            {},
            source_target="--output=G:/forbidden",
            target=target,
        )


def test_final_regression_reuse_requires_external_digest_and_live_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auditor = Phase2FreezeAuditor(ROOT)
    target = _git(ROOT, "rev-parse", "HEAD")
    target_tree = _git(ROOT, "rev-parse", f"{target}^{{tree}}")
    loopx_base = tmp_path.parent / f"{tmp_path.name}-loopx-base"
    boundary = {
        "schema": "zyra.release-worktree-boundary/v1",
        "ready": True,
        "head_commit": target,
        "head_tree": target_tree,
        "expected_head": target,
        "head_matches": True,
        "tracked_dirty_entries": [],
        "unexpected_untracked_entries": [],
        "allowed_generated_roots": [],
        "ignored_generated_entry_count": 0,
        "ignored_generated_paths_digest": "ignored",
    }
    boundary["boundary_digest"] = canonical_digest(boundary)
    monkeypatch.setattr(
        phase2_freeze,
        "inspect_worktree",
        lambda root, *, expected_head: dict(boundary),
    )
    monkeypatch.setattr(
        auditor,
        "_loopx_base_boundary_audit",
        lambda supplied, checkout: {
            "ready": True,
            "supplied_matches": True,
            "observed": dict(supplied),
        },
    )
    commands = []
    for command_id, argv in auditor._expected_regression_commands(
        output_root=tmp_path,
        target=target,
        loopx_base_checkout=loopx_base,
    ).items():
        stdout = tmp_path / f"{command_id}.stdout.log"
        stderr = tmp_path / f"{command_id}.stderr.log"
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        commands.append(
            {
                "command_id": command_id,
                "argv": list(argv),
                "cwd": str(ROOT),
                "stdout": stdout.name,
                "stderr": stderr.name,
                "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "returncode": 0,
                "ready": True,
            }
        )
    policy_path = ROOT / "config" / "release-python-tests.json"
    receipt = {
        "schema": "zyra.phase2-final-regression/v1",
        "target_commit": target,
        "ready": True,
        "loopx_base_checkout": str(loopx_base),
        "loopx_base_boundary_before": {"ready": True},
        "loopx_base_boundary_after": {"ready": True},
        "passed_count": len(commands),
        "failed_count": 0,
        "commands": commands,
        "python_test_policy": {
            "path": "config/release-python-tests.json",
            "sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        },
        "worktree_boundary_before": dict(boundary),
        "worktree_boundary_after": dict(boundary),
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    receipt_path = tmp_path / "final-regression.json"
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    external_digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()

    ready = auditor.verify_final_regression_receipt(
        receipt_path,
        target_commit=target,
        expected_sha256=external_digest,
    )
    assert ready["ready"] is True
    assert ready["external_receipt_digest_matches"] is True
    with pytest.raises(Phase2FreezeError, match="external SHA-256 anchor"):
        auditor.verify_final_regression_receipt(
            receipt_path,
            target_commit=target,
            expected_sha256="",
        )

    dirty_boundary = dict(boundary)
    dirty_boundary["ready"] = False
    dirty_boundary["tracked_dirty_entries"] = [" M production.py"]
    dirty_boundary.pop("boundary_digest")
    dirty_boundary["boundary_digest"] = canonical_digest(dirty_boundary)
    monkeypatch.setattr(
        phase2_freeze,
        "inspect_worktree",
        lambda root, *, expected_head: dict(dirty_boundary),
    )
    dirty = auditor.verify_final_regression_receipt(
        receipt_path,
        target_commit=target,
        expected_sha256=external_digest,
    )
    assert dirty["ready"] is False
    assert "current_target_source_boundary" in dirty["blockers"]


def test_preflight_command_audit_rejects_manifest_probe_substitution() -> None:
    auditor = Phase2FreezeAuditor(ROOT)
    probe = {
        "probe_id": "probe-a",
        "argv": ["-m", "pytest", "tests/unit/example.py"],
        "timeout_seconds": 120,
        "categories": ["integration"],
        "required": True,
        "isolated": True,
        "external_cost": False,
    }
    receipt = {
        "probe_id": "probe-a",
        "receipt_id": "receipt_command_probe-a",
        "argv": [sys.executable, *probe["argv"]],
        "cwd": str(ROOT),
        "timeout_seconds": 120,
        "categories": ["integration"],
        "required": True,
        "isolated": True,
        "external_cost": False,
        "status": "passed",
        "exit_code": 0,
        "retained": True,
    }
    assert auditor._preflight_command_blockers((probe,), (receipt,)) == []
    receipt["argv"] = [sys.executable, "-c", "pass"]
    assert auditor._preflight_command_blockers((probe,), (receipt,)) == [
        "preflight_command_argv:probe-a"
    ]


def test_preflight_audit_rejects_coherently_resigned_manifest_probe() -> None:
    auditor = Phase2FreezeAuditor(ROOT)
    target = _git(ROOT, "rev-parse", "HEAD")
    manifest = auditor._expected_preflight_manifest(
        target=target,
        frozen_at="2026-08-01T00:00:00+00:00",
    )
    assert auditor._preflight_manifest_binding_blockers(
        manifest=manifest,
        target=target,
    ) == []

    tampered = json.loads(json.dumps(manifest))
    probe = tampered["command_probes"][0]
    probe["argv"] = ["-c", "pass"]
    identity = {
        key: tampered.get(key)
        for key in (
            "frozen_inputs",
            "evidence_bindings",
            "supporting_evidence",
            "required_checks",
            "command_probes",
            "prohibited_operations",
        )
    }
    tampered["preflight_id"] = (
        "preflight_" + canonical_digest(identity)[:24]
    )
    tampered.pop("manifest_digest")
    tampered["manifest_digest"] = canonical_digest(tampered)
    coherent_receipt = {
        "probe_id": probe["probe_id"],
        "receipt_id": f"receipt_command_{probe['probe_id']}",
        "argv": [sys.executable, *probe["argv"]],
        "cwd": str(ROOT),
        "timeout_seconds": probe["timeout_seconds"],
        "categories": list(probe["categories"]),
        "required": probe["required"],
        "isolated": probe["isolated"],
        "external_cost": probe["external_cost"],
        "status": "passed",
        "exit_code": 0,
        "retained": True,
    }
    assert auditor._preflight_command_blockers(
        (probe,),
        (coherent_receipt,),
    ) == []
    assert auditor._preflight_manifest_binding_blockers(
        manifest=tampered,
        target=target,
    ) == ["tracked_preflight_manifest_binding"]


def test_preflight_activation_audit_recomputes_active_semantics() -> None:
    report = {
        "preflight_id": "preflight-test",
        "profile_family": "topology_policy",
        "profile_version": "phase2_strongest_v1",
        "report_digest": "report-digest",
        "status": "completed",
        "execution_mode": "active_default_revalidation",
        "hard_gate_order": ["gate-a"],
        "hard_gates": {"gate-a": True},
        "resolver_before": "phase2_strongest_v1",
        "resolver_after": "phase2_strongest_v1",
    }
    readiness = {
        "report_digest": "readiness-digest",
        "mechanisms": {
            name: {
                "readiness_stage": "activation_ready",
                "status": "deterministic_ready",
            }
            for name in ("arg_designer", "card", "agentprune", "maas")
        },
    }
    refs = ("raw-receipts.json#receipt-a",)
    passed = sorted(
        [
            "preflight_status",
            "hard_gate:gate-a",
            "resolver_retention",
            *[
                f"readiness:{name}"
                for name in ("arg_designer", "card", "agentprune", "maas")
            ],
        ]
    )
    activation = {
        "schema": "zyra.strongest-preflight-activation-report/v1",
        "preflight_id": "preflight-test",
        "profile_family": "topology_policy",
        "profile_version": "phase2_strongest_v1",
        "preflight_report_digest": "report-digest",
        "readiness_report_digest": "readiness-digest",
        "sealed_run_admission_eligible": True,
        "default_activation_allowed": True,
        "conclusion": "phase2_strongest_v1_revalidated",
        "blockers": [],
        "passed_gates": passed,
        "readiness": {
            name: {"stage": "activation_ready", "status": "deterministic_ready"}
            for name in sorted(("arg_designer", "card", "agentprune", "maas"))
        },
        "resolver_before": "phase2_strongest_v1",
        "resolver_after": "phase2_strongest_v1",
        "raw_receipt_refs": list(refs),
        "activation_semantics": {
            "scope": "final_phase2_strongest_v1_revalidation",
            "normal_resolver_mutated": False,
            "default_profile_activated": True,
            "explicit_activation_transition_required_later": False,
            "baseline_retained_until_transition": False,
        },
    }
    activation["activation_report_digest"] = canonical_digest(activation)
    assert Phase2FreezeAuditor._preflight_activation_blockers(
        report=report,
        readiness=readiness,
        activation=activation,
        raw_receipt_refs=refs,
    ) == []
    activation["activation_semantics"]["default_profile_activated"] = False
    unsigned = dict(activation)
    unsigned.pop("activation_report_digest")
    activation["activation_report_digest"] = canonical_digest(unsigned)
    assert Phase2FreezeAuditor._preflight_activation_blockers(
        report=report,
        readiness=readiness,
        activation=activation,
        raw_receipt_refs=refs,
    ) == ["preflight_activation_recompute"]
