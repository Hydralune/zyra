from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
P2_BASE_COMMIT = "e207b46ca690171139a718b8b85d808cb5a79c1e"


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
    discovered = shutil.which("bun")
    if discovered:
        return discovered
    executable = "bun.exe" if os.name == "nt" else "bun"
    local = ROOT / "node_modules" / ".bin" / executable
    if local.is_file():
        return str(local)
    raise ValueError(
        "bun is required for the final regression and was not found on PATH "
        "or in node_modules/.bin"
    )


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
                str(ROOT / "tests"),
            ),
            7200,
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
                str(output_root / "first-stage-final-freeze"),
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
    )
    value: dict[str, Any] = {
        "schema": "zyra.phase2-final-regression/v1",
        "slice_id": "P2-S06-03",
        "ready": ready,
        "target_commit": target_commit,
        "p2_base_commit": P2_BASE_COMMIT,
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
            )
        },
        "commands": commands,
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
    except (OSError, subprocess.SubprocessError, ValueError) as error:
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
