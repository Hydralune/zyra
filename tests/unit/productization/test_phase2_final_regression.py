from __future__ import annotations

import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "release" / "run_phase2_final_regression.py"
SPEC = importlib.util.spec_from_file_location(
    "run_phase2_final_regression",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_final_python_regression_is_scoped_to_zyra_tests(
    tmp_path: Path,
) -> None:
    commands = MODULE._command_specs(
        output_root=tmp_path,
        python="python",
        bun="bun",
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
