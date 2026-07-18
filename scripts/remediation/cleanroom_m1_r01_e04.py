"""Reproduce an exact E04 candidate in a detached clean worktree."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO / "docs" / "reviews" / "evidence" / "M1-R01-v4" / "execution-04" / "cleanroom-result.json"
BUN = REPO / "node_modules" / "bun" / "bin" / "bun.exe"
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"


def command(args: list[str], cwd: Path, timeout: int = 600, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    process = subprocess.run(
        args, cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True,
        timeout=timeout, env={**os.environ, "NO_COLOR": "1", **(env or {})}, check=False,
    )
    return {
        "command": args,
        "exit_code": process.returncode,
        "duration_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 3),
        "stdout_tail": process.stdout[-12000:],
        "stderr_tail": process.stderr[-12000:],
        "passed": process.returncode == 0,
    }


def git(args: list[str], cwd: Path = REPO, check: bool = True) -> str:
    result = command(["git", *args], cwd, timeout=120)
    if check and not result["passed"]:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result["stdout_tail"].strip()


def git_status_paths(cwd: Path) -> list[str]:
    process = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr)
    return process.stdout.splitlines()


def safe_remove(path: Path, root: Path) -> None:
    resolved = path.resolve()
    permitted = root.resolve()
    if resolved == permitted or permitted not in resolved.parents:
        raise RuntimeError(f"unsafe cleanroom removal target: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    candidate = args.candidate or git(["rev-parse", "HEAD"])
    tree = git(["rev-parse", f"{candidate}^{{tree}}"])
    temp_root = (REPO / ".tmp").resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    clean = (temp_root / f"e04-cleanroom-{candidate[:12]}").resolve()
    if clean == REPO.resolve() or temp_root not in clean.parents:
        raise RuntimeError(f"unsafe cleanroom root: {clean}")
    if clean.exists():
        git(["worktree", "remove", "--force", str(clean)], check=False)
        safe_remove(clean, temp_root)
    git(["worktree", "add", "--detach", str(clean), candidate])
    results: list[dict[str, Any]] = []
    removed: list[str] = []
    try:
        for relative in ("vendor", "vendor-runtimes", "source-pool", "runtime-sources", "node_modules", "dist", ".cache", ".tmp"):
            target = clean / relative
            if target.exists():
                safe_remove(target, clean)
                removed.append(relative)
        cache = clean / ".tmp" / "bun-cache"
        state = clean / ".tmp" / "e04-state"
        cache.mkdir(parents=True, exist_ok=True)
        environment = {
            "BUN_INSTALL_CACHE_DIR": str(cache),
            "npm_config_cache": str(cache),
            "ZYRA_BUN_EXECUTABLE": str(BUN),
            "ZYRA_E03_STATE_ROOT": str(state),
        }
        commands = [
            [str(BUN), "install", "--frozen-lockfile"],
            [str(BUN), "run", "typecheck"],
            [str(BUN), "run", "build"],
            [str(BUN), "test", "packages/runtime/claude-runtime/test", "packages/integrations/claude-mcp/test"],
            [str(PYTHON), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", ".tmp/e04-clean-pytest", "tests/integration/test_e04_candidate_closure.py", "tests/integration/test_e01_typescript_runtime_cutover.py", "tests/integration/test_code_worker_clean_productized_runtime.py", "tests/integration/test_workspace_worker_gateway.py"],
            [str(PYTHON), "scripts/remediation/probe_m1_r01_e04.py", "all"],
        ]
        for value in commands:
            result = command(value, clean, timeout=900, env=environment)
            results.append(result)
            if not result["passed"]:
                break
        all_dirty_paths = git_status_paths(clean)
        expected_removed_prefixes = tuple(
            f"{relative}/"
            for relative in removed
            if relative in {"vendor", "vendor-runtimes", "source-pool", "runtime-sources"}
        )
        expected_removed_dirty_paths = [
            line
            for line in all_dirty_paths
            if line[3:].replace("\\", "/").startswith(expected_removed_prefixes)
        ]
        dirty_paths = [
            line for line in all_dirty_paths if line not in expected_removed_dirty_paths
        ]
        forbidden_exists = [
            relative for relative in ("vendor", "vendor-runtimes", "source-pool", "runtime-sources")
            if (clean / relative).exists()
        ]
        report = {
            "schema_version": "4.0",
            "execution_id": "E04",
            "record_type": "cleanroom_result",
            "candidate_commit": candidate,
            "candidate_tree": tree,
            "cleanroom_root": str(clean),
            "removed_preexisting_paths": removed,
            "expected_removed_dirty_paths": expected_removed_dirty_paths,
            "forbidden_exists": forbidden_exists,
            "dirty_paths": dirty_paths,
            "commands": results,
            "passed": len(results) == len(commands) and all(result["passed"] for result in results) and not forbidden_exists and not dirty_paths,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.output.resolve().write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if report["passed"] else 1
    finally:
        git(["worktree", "remove", "--force", str(clean)], check=False)
        if clean.exists():
            safe_remove(clean, temp_root)
        git(["worktree", "prune"], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
