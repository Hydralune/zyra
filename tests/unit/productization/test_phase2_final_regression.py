from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace
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
LOOPX_UPGRADE_SCRIPT = (
    ROOT / "scripts" / "release" / "verify_loopx_cross_version_upgrade.py"
)
LOOPX_UPGRADE_SPEC = importlib.util.spec_from_file_location(
    "verify_loopx_cross_version_upgrade",
    LOOPX_UPGRADE_SCRIPT,
)
assert LOOPX_UPGRADE_SPEC is not None and LOOPX_UPGRADE_SPEC.loader is not None
LOOPX_UPGRADE_MODULE = importlib.util.module_from_spec(LOOPX_UPGRADE_SPEC)
LOOPX_UPGRADE_SPEC.loader.exec_module(LOOPX_UPGRADE_MODULE)

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
    first = MODULE.RESUME_REQUIRED_FIRST_COMMIT

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            commit = arguments[-1]
            return f"{commit} {source if commit == first else first}"
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
    first = MODULE.RESUME_REQUIRED_FIRST_COMMIT
    only_path = MODULE.RESUME_ALLOWED_PATHS[0]

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            commit = arguments[-1]
            return f"{commit} {source if commit == first else first}"
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


def test_resume_delta_rejects_forbidden_intermediate_commit_change(
    monkeypatch,
) -> None:
    source = "a" * 40
    intermediate = MODULE.RESUME_REQUIRED_FIRST_COMMIT
    target = "c" * 40
    allowed_statuses = "\n".join(
        f"M\t{path}" for path in MODULE.RESUME_ALLOWED_PATHS
    )

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            commit = arguments[-1]
            parent = source if commit == intermediate else intermediate
            return f"{commit} {parent}"
        if arguments[:3] == ("diff", "--name-status", "--no-renames"):
            if arguments[-2:] == (source, intermediate):
                return "M\tpackages/runtime/temporary-production-change.py"
            return allowed_statuses
        if arguments[0] == "ls-tree":
            path = arguments[-1]
            return f"100644 blob {'d' * 40}\t{path}"
        raise AssertionError(arguments)

    monkeypatch.setattr(MODULE, "_git", fake_git)
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"bounded-diff"),
    )

    with pytest.raises(ValueError, match="forbidden change"):
        MODULE._resume_target_delta(
            source_target=source,
            target_commit=target,
        )


def test_resume_delta_rejects_wrong_exact_chain_before_diff(
    monkeypatch,
) -> None:
    source = "a" * 40
    target = "d" * 40
    first = MODULE.RESUME_REQUIRED_FIRST_COMMIT

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            commit = arguments[-1]
            parent = source if commit == first else "e" * 40
            return f"{commit} {parent}"
        raise AssertionError("exact-chain rejection must precede diff calls")

    monkeypatch.setattr(MODULE, "_git", fake_git)

    with pytest.raises(ValueError, match="exact two-commit remediation chain"):
        MODULE._resume_target_delta(
            source_target=source,
            target_commit=target,
        )


def test_resume_delta_accepts_exact_two_commit_blob_chain(monkeypatch) -> None:
    source = "a" * 40
    first = MODULE.RESUME_REQUIRED_FIRST_COMMIT
    target = "d" * 40
    split = len(MODULE.RESUME_ALLOWED_PATHS) // 2
    first_paths = MODULE.RESUME_ALLOWED_PATHS[:split]
    second_paths = MODULE.RESUME_ALLOWED_PATHS[split:]

    def statuses(paths: tuple[str, ...]) -> str:
        return "\n".join(f"M\t{path}" for path in paths)

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            commit = arguments[-1]
            return f"{commit} {source if commit == first else first}"
        if arguments[:3] == ("diff", "--name-status", "--no-renames"):
            pair = arguments[-2:]
            if pair == (source, target):
                return statuses(MODULE.RESUME_ALLOWED_PATHS)
            if pair == (source, first):
                return statuses(first_paths)
            if pair == (first, target):
                return statuses(second_paths)
        if arguments[0] == "ls-tree":
            revision = arguments[1]
            path = arguments[-1]
            blob = {source: "b", first: "c", target: "d"}[revision] * 40
            return f"100644 blob {blob}\t{path}"
        if arguments[:2] == ("rev-parse", f"{source}^{{tree}}"):
            return "e" * 40
        if arguments[:2] == ("rev-parse", f"{target}^{{tree}}"):
            return "f" * 40
        raise AssertionError(arguments)

    monkeypatch.setattr(MODULE, "_git", fake_git)
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            stdout=(" ".join(command[-2:])).encode()
        ),
    )

    delta = MODULE._resume_target_delta(
        source_target=source,
        target_commit=target,
    )

    assert delta["commit_chain"] == [first, target]
    assert delta["required_first_commit"] == first
    assert len(delta["commit_path_changes"]) == 2
    assert all(item["blob_transitions"] for item in delta["commit_path_changes"])
    assert all(item["diff_sha256"] for item in delta["commit_path_changes"])


def test_resume_delta_rejects_transient_mode_change(monkeypatch) -> None:
    source = "a" * 40
    first = MODULE.RESUME_REQUIRED_FIRST_COMMIT
    target = "d" * 40
    changed_path = MODULE.RESUME_ALLOWED_PATHS[0]
    allowed_statuses = "\n".join(
        f"M\t{path}" for path in MODULE.RESUME_ALLOWED_PATHS
    )

    def fake_git(*arguments: str) -> str:
        if arguments[:3] == ("rev-list", "--parents", "-n"):
            commit = arguments[-1]
            return f"{commit} {source if commit == first else first}"
        if arguments[:3] == ("diff", "--name-status", "--no-renames"):
            if arguments[-2:] == (source, target):
                return allowed_statuses
            return f"M\t{changed_path}"
        if arguments[0] == "ls-tree":
            revision = arguments[1]
            path = arguments[-1]
            mode = "100755" if revision == first and path == changed_path else "100644"
            return f"{mode} blob {'e' * 40}\t{path}"
        raise AssertionError(arguments)

    monkeypatch.setattr(MODULE, "_git", fake_git)
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"bounded-diff"),
    )

    with pytest.raises(ValueError, match="mode or object type"):
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


def test_cross_version_phase_cleans_owned_deployment_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    output = tmp_path / "phase.json"
    observed: dict[str, Path] = {}
    monkeypatch.setenv("ZYRA_DEPLOYMENT_STATE_ROOT", "preexisting-sentinel")

    def fake_baseline(selected_checkout: Path, workspace: Path) -> dict[str, object]:
        assert selected_checkout == checkout.resolve()
        assert workspace == (tmp_path / "workspace").resolve()
        deployment_root = Path(os.environ["ZYRA_DEPLOYMENT_STATE_ROOT"])
        assert checkout.resolve() in deployment_root.parents
        (deployment_root / "owned.sqlite3").write_bytes(b"owned")
        observed["deployment_root"] = deployment_root
        return {"schema": "test-phase"}

    monkeypatch.setattr(LOOPX_UPGRADE_MODULE, "_baseline_phase", fake_baseline)
    arguments = SimpleNamespace(
        checkout=str(checkout),
        workspace=str(tmp_path / "workspace"),
        phase="baseline",
        baseline=None,
        restart_index=0,
        phase_output=str(output),
    )

    assert LOOPX_UPGRADE_MODULE._run_phase(arguments) == 0
    assert not observed["deployment_root"].exists()
    assert os.environ["ZYRA_DEPLOYMENT_STATE_ROOT"] == "preexisting-sentinel"
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["deployment_state_isolation"]["cleaned"] is True


def test_cross_version_phase_cleans_deployment_state_after_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    observed: dict[str, Path] = {}
    monkeypatch.setenv("ZYRA_DEPLOYMENT_STATE_ROOT", "preexisting-sentinel")

    def failing_baseline(
        selected_checkout: Path,
        workspace: Path,
    ) -> dict[str, object]:
        deployment_root = Path(os.environ["ZYRA_DEPLOYMENT_STATE_ROOT"])
        (deployment_root / "owned.sqlite3").write_bytes(b"owned")
        observed["deployment_root"] = deployment_root
        raise RuntimeError("expected phase failure")

    monkeypatch.setattr(
        LOOPX_UPGRADE_MODULE,
        "_baseline_phase",
        failing_baseline,
    )
    arguments = SimpleNamespace(
        checkout=str(checkout),
        workspace=str(tmp_path / "workspace"),
        phase="baseline",
        baseline=None,
        restart_index=0,
        phase_output=str(tmp_path / "phase.json"),
    )

    with pytest.raises(RuntimeError, match="expected phase failure"):
        LOOPX_UPGRADE_MODULE._run_phase(arguments)
    assert not observed["deployment_root"].exists()
    assert os.environ["ZYRA_DEPLOYMENT_STATE_ROOT"] == "preexisting-sentinel"


def test_loopx_resume_result_requires_three_distinct_owned_isolations(
    tmp_path: Path,
) -> None:
    target = "d" * 40
    base = (tmp_path / "base").resolve()
    base.mkdir()
    target_paths = (
        ROOT / f".nonexistent-loopx-isolation-{tmp_path.name}-1",
        ROOT / f".nonexistent-loopx-isolation-{tmp_path.name}-2",
    )

    def phase(checkout: Path, path: Path, commit: str) -> dict[str, object]:
        return {
            "commit": commit,
            "checkout": str(checkout),
            "deployment_state_isolation": {
                "strategy": "owned_checkout_temporary_directory",
                "checkout": str(checkout),
                "path": str(path),
                "cleaned": True,
            },
        }

    receipt = {
        "schema": "zyra.loopx-cross-version-upgrade/v1",
        "ready": True,
        "base_commit": MODULE.LOOPX_CROSS_VERSION_BASE_COMMIT,
        "target_commit": target,
        "baseline": phase(
            base,
            base / ".nonexistent-loopx-isolation-base",
            MODULE.LOOPX_CROSS_VERSION_BASE_COMMIT,
        ),
        "target_restarts": [
            phase(ROOT, target_paths[0], target),
            phase(ROOT, target_paths[1], target),
        ],
        "invariants": {
            "semantic_state_preserved": True,
            "cursor_monotonic": True,
            "duplicate_claim": False,
            "duplicate_spend": False,
            "duplicate_interaction": False,
            "duplicate_canonical_commit": False,
            "historical_install_preserved_and_ignored": True,
            "new_install_or_extraction": False,
            "independent_target_restart_count": 2,
            "checkout_runtime_state_cleaned": True,
            "checkout_runtime_state_paths_distinct": True,
        },
    }
    path = tmp_path / "loopx.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")

    assert MODULE._loopx_resume_result_ready(
        path,
        target_commit=target,
        loopx_base_checkout=base,
    )
    receipt["target_restarts"][1]["deployment_state_isolation"]["path"] = str(
        target_paths[0]
    )
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert not MODULE._loopx_resume_result_ready(
        path,
        target_commit=target,
        loopx_base_checkout=base,
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
