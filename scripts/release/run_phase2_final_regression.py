from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PRODUCTIZATION_ROOT = ROOT / "packages" / "productization"
if str(PRODUCTIZATION_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION_ROOT))

from zyra_productization.release.worktree import (
    WorktreeBoundaryError,
    inspect_worktree,
    require_worktree_boundary,
)

P2_BASE_COMMIT = "e207b46ca690171139a718b8b85d808cb5a79c1e"
PYTHON_TEST_POLICY_PATH = ROOT / "config" / "release-python-tests.json"
FIRST_STAGE_FREEZE_ROOT = (
    ROOT / "docs" / "reviews" / "evidence" / "M3-S03-02" / "final-freeze"
)
TYPESCRIPT_RUNTIME_TEST_ROOTS = (
    "packages/commands/test",
    "packages/integrations/claude-mcp/test",
    "packages/memory/curator-state-machine/test",
    "packages/memory/retrieval-algorithms/test",
    "packages/memory/skill-memory-runtime/test",
    "packages/runtime/claude-runtime/test",
    "packages/runtime/provider-control-plane/test",
    "packages/runtime/runtime-event-spine/test",
    "packages/runtime/sandbox-gateway-control/test",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _resolve_bun() -> str:
    executable = "bun.exe" if os.name == "nt" else "bun"
    local = ROOT / "node_modules" / ".bin" / executable
    if local.is_file():
        return str(local)
    discovered = shutil.which("bun")
    if discovered:
        return discovered
    raise ValueError(
        "bun is required for the final regression and was not found on PATH "
        "or in node_modules/.bin"
    )


def _source_python_path() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    entries = (
        pyproject.get("tool", {})
        .get("setuptools", {})
        .get("packages", {})
        .get("find", {})
        .get("where", ())
    )
    if not isinstance(entries, list) or not entries:
        raise ValueError("project Python package roots are missing")
    roots = tuple(
        (ROOT / entry).resolve()
        for entry in entries
        if isinstance(entry, str) and (ROOT / entry).is_dir()
    )
    if len(roots) != len(entries):
        raise ValueError("a declared project Python package root is missing")
    return os.pathsep.join(str(path) for path in roots)


def _python_test_policy() -> dict[str, Any]:
    try:
        payload = json.loads(PYTHON_TEST_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"release Python test policy is unavailable: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("release Python test policy must be an object")
    if payload.get("schema") != "zyra.release-python-test-policy/v1":
        raise ValueError("release Python test policy schema is unsupported")
    for field in ("roots", "ignore_files", "deselect_nodeids"):
        values = payload.get(field)
        if (
            not isinstance(values, list)
            or any(not isinstance(item, str) or not item.strip() for item in values)
            or len(values) != len(set(values))
        ):
            raise ValueError(f"release Python test policy field is invalid: {field}")
    if not str(payload.get("debt_owner") or "").strip():
        raise ValueError("release Python test policy has no debt owner")
    if len(str(payload.get("reason") or "").strip()) < 24:
        raise ValueError("release Python test policy has no bounded rationale")
    for relative in (*payload["roots"], *payload["ignore_files"]):
        candidate = (ROOT / relative).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError as error:
            raise ValueError(
                f"release Python test policy path escapes the target: {relative}"
            ) from error
        if not candidate.exists():
            raise ValueError(
                f"release Python test policy path is missing: {relative}"
            )
    for nodeid in payload["deselect_nodeids"]:
        test_path, separator, _selection = nodeid.partition("::")
        if not separator or not (ROOT / test_path).is_file():
            raise ValueError(
                f"release Python test policy node id is invalid: {nodeid}"
            )
    return payload


def _python_test_arguments() -> tuple[str, ...]:
    policy = _python_test_policy()
    arguments: list[str] = []
    arguments.extend(
        f"--ignore={ROOT / relative}" for relative in policy["ignore_files"]
    )
    arguments.extend(
        f"--deselect={nodeid}" for nodeid in policy["deselect_nodeids"]
    )
    arguments.extend(str(ROOT / relative) for relative in policy["roots"])
    return tuple(arguments)


def _command_specs(
    *,
    output_root: Path,
    python: str,
    bun: str,
) -> tuple[tuple[str, tuple[str, ...], float], ...]:
    basetemp = output_root / "pytest"
    return (
        (
            "python-full-regression",
            (
                python,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(basetemp),
                *_python_test_arguments(),
            ),
            7200,
        ),
        (
            "typescript-runtime-regression",
            (bun, "test", *TYPESCRIPT_RUNTIME_TEST_ROOTS),
            3600,
        ),
        ("typescript-typecheck", (bun, "run", "typecheck"), 1200),
        ("web-typecheck", (bun, "run", "typecheck:web"), 1200),
        ("web-tests", (bun, "run", "test:web"), 1800),
        ("web-build", (bun, "run", "build:web"), 1800),
        ("phase1-m1", (python, "scripts/verify_m1.py"), 1800),
        ("phase1-m2", (python, "scripts/verify_m2.py"), 1800),
        ("phase1-m3", (python, "scripts/verify_m3.py"), 1800),
        (
            "phase1-final-freeze",
            (
                python,
                "scripts/verify_first_stage.py",
                "--output",
                str(FIRST_STAGE_FREEZE_ROOT),
            ),
            1800,
        ),
        (
            "phase2-policy-contracts",
            (
                python,
                "scripts/verify_phase2_policy_contracts.py",
                "--target-commit",
                _head(),
                "--require-strongest-active",
                "--output",
                str(output_root / "phase2-policy-contracts.json"),
            ),
            300,
        ),
        (
            "internalization-ledger",
            (
                python,
                "scripts/verify_internalization_ledger.py",
                "--base",
                P2_BASE_COMMIT,
                "--json",
            ),
            600,
        ),
        (
            "loopx-offline-runtime",
            (python, "scripts/release/verify_loopx_runtime.py"),
            600,
        ),
        (
            "loopx-cross-version-restart",
            (
                python,
                "scripts/release/verify_loopx_cross_version_upgrade.py",
            ),
            900,
        ),
    )


def run_regression(
    *,
    target_commit: str,
    output_root: Path,
) -> dict[str, Any]:
    observed = _head()
    if observed != target_commit:
        raise ValueError(
            f"target commit mismatch: expected {target_commit}, observed {observed}"
        )
    boundary_before = require_worktree_boundary(
        ROOT,
        expected_head=target_commit,
    )
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    logs = output_root / "logs"
    logs.mkdir()
    bun = _resolve_bun()
    environment = {
        **os.environ,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "BUN_INSTALL_CACHE_DIR": str(output_root / "cache" / "bun"),
        "PIP_CACHE_DIR": str(output_root / "cache" / "pip"),
        "UV_CACHE_DIR": str(output_root / "cache" / "uv"),
        "ZYRA_STATE_ROOT": str(output_root / "state"),
        "PYTHONPATH": _source_python_path(),
    }
    commands: list[dict[str, Any]] = []
    for command_id, command, timeout in _command_specs(
        output_root=output_root,
        python=sys.executable,
        bun=bun,
    ):
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        stdout_path = logs / f"{command_id}.stdout.log"
        stderr_path = logs / f"{command_id}.stderr.log"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        commands.append(
            {
                "command_id": command_id,
                "argv": list(command),
                "cwd": str(ROOT),
                "returncode": completed.returncode,
                "ready": completed.returncode == 0,
                "duration_ms": round(
                    (time.monotonic() - started) * 1000,
                    3,
                ),
                "stdout": stdout_path.relative_to(output_root).as_posix(),
                "stdout_sha256": _sha256(stdout_path),
                "stderr": stderr_path.relative_to(output_root).as_posix(),
                "stderr_sha256": _sha256(stderr_path),
                "stdout_tail": completed.stdout[-4096:],
                "stderr_tail": completed.stderr[-4096:],
            }
        )
        if completed.returncode:
            break
    boundary_after = inspect_worktree(ROOT, expected_head=target_commit)
    ready = (
        len(commands)
        == len(
            _command_specs(
                output_root=output_root,
                python=sys.executable,
                bun=bun,
            )
        )
        and all(item["ready"] for item in commands)
        and boundary_after["ready"]
    )
    value: dict[str, Any] = {
        "schema": "zyra.phase2-final-regression/v1",
        "slice_id": "P2-S06-03",
        "ready": ready,
        "target_commit": target_commit,
        "p2_base_commit": P2_BASE_COMMIT,
        "python_test_policy": {
            "path": PYTHON_TEST_POLICY_PATH.relative_to(ROOT).as_posix(),
            "sha256": _sha256(PYTHON_TEST_POLICY_PATH),
            "debt_owner": _python_test_policy()["debt_owner"],
            "reason": _python_test_policy()["reason"],
            "ignored_file_count": len(_python_test_policy()["ignore_files"]),
            "deselected_nodeid_count": len(
                _python_test_policy()["deselect_nodeids"]
            ),
        },
        "typescript_runtime_test_roots": list(TYPESCRIPT_RUNTIME_TEST_ROOTS),
        "explicit_environment": {
            key: environment[key]
            for key in (
                "PYTHONNOUSERSITE",
                "PYTHONDONTWRITEBYTECODE",
                "PIP_DISABLE_PIP_VERSION_CHECK",
                "BUN_INSTALL_CACHE_DIR",
                "PIP_CACHE_DIR",
                "UV_CACHE_DIR",
                "ZYRA_STATE_ROOT",
                "PYTHONPATH",
            )
        },
        "commands": commands,
        "worktree_boundary_before": boundary_before,
        "worktree_boundary_after": boundary_after,
        "passed_count": sum(item["ready"] for item in commands),
        "failed_count": sum(not item["ready"] for item in commands),
    }
    unsigned = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    value["receipt_digest"] = hashlib.sha256(unsigned).hexdigest()
    receipt = output_root / "final-regression.json"
    receipt.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Phase 2 final target regression and freeze receipts."
    )
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = run_regression(
            target_commit=arguments.target_commit,
            output_root=ROOT / arguments.output_root,
        )
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        WorktreeBoundaryError,
    ) as error:
        print(
            json.dumps(
                {
                    "schema": "zyra.phase2-final-regression-error/v1",
                    "ready": False,
                    "error": str(error),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(run())
