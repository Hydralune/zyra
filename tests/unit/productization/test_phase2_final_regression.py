from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "release" / "run_phase2_final_regression.py"
SPEC = importlib.util.spec_from_file_location(
    "run_phase2_final_regression",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FREEZE_SCRIPT = ROOT / "scripts" / "audit" / "verify_phase2_freeze.py"
FREEZE_SPEC = importlib.util.spec_from_file_location(
    "verify_phase2_freeze",
    FREEZE_SCRIPT,
)
assert FREEZE_SPEC is not None and FREEZE_SPEC.loader is not None
FREEZE_MODULE = importlib.util.module_from_spec(FREEZE_SPEC)
FREEZE_SPEC.loader.exec_module(FREEZE_MODULE)

from zyra_productization.release.phase2_freeze import Phase2FreezeAuditor


def test_final_python_regression_is_scoped_to_zyra_tests(
    tmp_path: Path,
) -> None:
    commands = MODULE._command_specs(
        output_root=tmp_path,
        python="python",
        bun="bun",
        loopx_base_checkout=tmp_path / "base",
        target_commit="a" * 40,
    )
    python_full = next(
        command
        for command_id, command, _timeout in commands
        if command_id == "python-full-regression"
    )

    assert python_full[-2:] == (
        str(ROOT / "tests" / "unit"),
        str(ROOT / "tests" / "integration"),
    )
    policy = MODULE._python_test_policy()
    assert policy["debt_owner"] == "M3-03"
    assert all(
        f"--ignore={ROOT / relative}" in python_full
        for relative in policy["ignore_files"]
    )
    typescript = next(
        command
        for command_id, command, _timeout in commands
        if command_id == "typescript-runtime-regression"
    )
    assert tuple(typescript[2:]) == MODULE.TYPESCRIPT_RUNTIME_TEST_ROOTS
    ledger = next(
        command
        for command_id, command, _timeout in commands
        if command_id == "internalization-ledger"
    )
    assert "--json" in ledger
    assert "--fail-on-warning" not in ledger
    first_stage_freeze = next(
        command
        for command_id, command, _timeout in commands
        if command_id == "phase1-final-freeze"
    )
    assert first_stage_freeze[-1] == str(MODULE.FIRST_STAGE_FREEZE_ROOT)
    assert not Path(first_stage_freeze[-1]).is_relative_to(tmp_path)
    assert (Path(first_stage_freeze[-1]) / "final-freeze-index.json").is_file()


def test_bun_resolution_accepts_the_frozen_local_install(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: None)
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    executable = "bun.exe" if os.name == "nt" else "bun"
    local = tmp_path / "node_modules" / ".bin" / executable
    local.parent.mkdir(parents=True)
    local.write_bytes(b"bun")

    assert MODULE._resolve_bun() == str(local)


def test_final_regression_python_path_is_bound_to_target_sources() -> None:
    roots = tuple(Path(item) for item in MODULE._source_python_path().split(os.pathsep))

    assert roots
    assert all(path.is_relative_to(ROOT) for path in roots)
    assert ROOT / "packages" / "evaluation" in roots
    assert ROOT / "packages" / "productization" in roots


def test_first_stage_freeze_command_matches_release_auditor(
    tmp_path: Path,
) -> None:
    python = str(Path(sys.executable).resolve())
    runner_commands = {
        command_id: command
        for command_id, command, _timeout in MODULE._command_specs(
            output_root=tmp_path,
            python=python,
            bun="bun",
            loopx_base_checkout=tmp_path / "base",
            target_commit="a" * 40,
        )
    }
    auditor_commands = Phase2FreezeAuditor(ROOT)._expected_regression_commands(
        output_root=tmp_path,
        target="a" * 40,
        loopx_base_checkout=tmp_path / "base",
    )

    assert runner_commands["phase1-final-freeze"] == auditor_commands[
        "phase1-final-freeze"
    ]
    assert runner_commands["loopx-cross-version-restart"] == auditor_commands[
        "loopx-cross-version-restart"
    ]


def test_loopx_cross_version_command_is_fully_parameterized(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    output = tmp_path / "output"
    commands = {
        command_id: command
        for command_id, command, _timeout in MODULE._command_specs(
            output_root=output,
            python="python",
            bun="bun",
            loopx_base_checkout=base,
            target_commit="a" * 40,
        )
    }

    assert commands["loopx-cross-version-restart"] == (
        "python",
        "scripts/release/verify_loopx_cross_version_upgrade.py",
        "--base-checkout",
        str(base.resolve()),
        "--target-checkout",
        str(ROOT),
        "--workspace",
        str(output / "loopx-cross-version-workspace"),
        "--output",
        str(output / "loopx-cross-version-upgrade.json"),
    )


def test_resume_delta_rejects_any_production_change(monkeypatch) -> None:
    source = "a" * 40
    target = "b" * 40

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            return f"{target} {source}"
        if arguments[:3] == ("diff", "--name-status", "--no-renames"):
            return "M\tpackages/runtime/production.py"
        raise AssertionError(arguments)

    monkeypatch.setattr(MODULE, "_git", fake_git)

    with pytest.raises(ValueError, match="forbidden change"):
        MODULE._resume_target_delta(
            source_target=source,
            target_commit=target,
        )


def test_resume_delta_requires_every_control_plane_file(monkeypatch) -> None:
    source = "a" * 40
    target = "b" * 40
    only_path = MODULE.RESUME_ALLOWED_PATHS[0]

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            return f"{target} {source}"
        if arguments[:3] == ("diff", "--name-status", "--no-renames"):
            return f"M\t{only_path}"
        if arguments[:2] == ("ls-tree", source):
            return f"100644 blob {'c' * 40}\t{only_path}"
        if arguments[:2] == ("ls-tree", target):
            return f"100644 blob {'d' * 40}\t{only_path}"
        raise AssertionError(arguments)

    monkeypatch.setattr(MODULE, "_git", fake_git)

    with pytest.raises(ValueError, match="every required control-plane file"):
        MODULE._resume_target_delta(
            source_target=source,
            target_commit=target,
        )


def test_resume_source_requires_full_external_sha(tmp_path: Path) -> None:
    receipt = tmp_path / "source.json"
    receipt.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="full external SHA-256"):
        MODULE._validate_resume_source(
            source_receipt=receipt,
            source_receipt_sha256="short",
            target_commit="b" * 40,
            bun="bun",
        )


def test_loopx_base_boundary_rejects_ignored_bytecode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "phase2@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Phase 2 Test"],
        cwd=tmp_path,
        check=True,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.txt", ".gitignore"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(MODULE, "LOOPX_CROSS_VERSION_BASE_COMMIT", head)
    monkeypatch.setattr(
        MODULE,
        "_trusted_git_common_dir",
        lambda: MODULE._git_common_dir(tmp_path),
    )

    assert MODULE._loopx_base_boundary(tmp_path)["ready"] is True
    (tmp_path / "ignored.pyc").write_bytes(b"untrusted bytecode")
    dirty = MODULE._loopx_base_boundary(tmp_path)
    assert dirty["ready"] is False
    assert dirty["tracked_untracked_and_ignored_clean"] is False
    assert dirty["status_entry_count"] == 1


def test_loopx_base_boundary_rejects_foreign_common_dir_before_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trusted = tmp_path / "trusted"
    foreign = tmp_path / "foreign"
    trusted.mkdir()
    foreign.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=trusted, check=True)
    subprocess.run(["git", "init", "-q"], cwd=foreign, check=True)
    trusted_common = MODULE._git_common_dir(trusted)
    real_run = subprocess.run

    def guarded_run(arguments, *args, **kwargs):
        if "status" in arguments or "hash-object" in arguments:
            raise AssertionError("foreign repository reached a worktree command")
        return real_run(arguments, *args, **kwargs)

    monkeypatch.setattr(MODULE, "_trusted_git_common_dir", lambda: trusted_common)
    monkeypatch.setattr(MODULE.subprocess, "run", guarded_run)

    boundary = MODULE._loopx_base_boundary(foreign)
    assert boundary["ready"] is False
    assert boundary["trusted_common_dir_matches"] is False
    assert boundary["rejected_before_worktree_commands"] is True


def test_final_regression_roots_must_be_disjoint(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be disjoint"):
        MODULE._require_disjoint_roots(tmp_path / "evidence", tmp_path)


def test_freeze_cli_passes_required_regression_sha(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubAuditor:
        def __init__(self, root: Path) -> None:
            assert root == ROOT

        def audit(self, **values: object) -> dict[str, object]:
            captured.update(values)
            return {"ready": True}

    monkeypatch.setattr(FREEZE_MODULE, "Phase2FreezeAuditor", StubAuditor)
    common = [
        "--target-commit",
        "a" * 40,
        "--release-root",
        "release",
        "--sealed-root",
        "sealed",
        "--preflight-root",
        "preflight",
        "--regression-receipt",
        "regression.json",
        "--custody-report",
        "custody.json",
        "--contract-report",
        "contract.json",
        "--output-root",
        "output",
    ]
    with pytest.raises(SystemExit):
        FREEZE_MODULE.run(common)

    assert FREEZE_MODULE.run(
        [
            *common,
            "--regression-receipt-sha256",
            "b" * 64,
        ]
    ) == 0
    assert captured["regression_receipt_sha256"] == "b" * 64
