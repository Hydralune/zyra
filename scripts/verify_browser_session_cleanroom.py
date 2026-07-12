from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COPY_DIRECTORIES = ("apps", "packages", "skills", "scripts", "tests")
COPY_FILES = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "pytest.ini")
IGNORED = {".git", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "__pycache__", "artifacts", "tmp", "vendor", "vendor-runtimes"}
SOURCE_REPOSITORIES = ("browser-use", "claude-code-best", "opencode", "OpenHands", "agentscope", "agent-framework", "hermes-agent", "langgraph", "openclaw", "oh-my-pi")


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED or name.endswith((".pyc", ".pyo", ".sqlite", ".sqlite3"))}


def build_clean_copy(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for name in COPY_DIRECTORIES:
        source = PROJECT_ROOT / name
        if source.is_dir():
            shutil.copytree(source, destination / name, ignore=_ignore)
    for name in COPY_FILES:
        source = PROJECT_ROOT / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    forbidden = [name for name in IGNORED if (destination / name).exists()]
    if forbidden:
        raise RuntimeError(f"clean browser copy contains excluded roots: {forbidden}")


def clean_environment(clean_root: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ZYRA_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    state = clean_root / ".clean-state"
    state.mkdir()
    environment.update({
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ZYRA_SQLITE_PATH": str(state / "zyra.sqlite3"),
        "ZYRA_EVENT_LOG": str(state / "events.jsonl"),
        "ZYRA_ARTIFACT_ROOT": str(state / "artifacts"),
        "ZYRA_BROWSER_STATE": str(state / "browser-state"),
        "ZYRA_PERMISSION_STATE": str(state / "permission-state.json"),
    })
    return environment


def run_cleanroom(*, python: Path, clean_root: Path) -> list[dict[str, object]]:
    commands = (
        [str(python), "-m", "unittest", "tests.unit.test_browser_session_runtime_foundation"],
        [str(python), "-m", "unittest", "tests.integration.test_browser_session_productization_foundation"],
        [str(python), "scripts/smoke_browser_session_foundation.py"],
        [str(python), "scripts/verify_submission_boundary.py"],
    )
    environment = clean_environment(clean_root)
    forbidden = [str(PROJECT_ROOT.parent / name) for name in SOURCE_REPOSITORIES]
    results: list[dict[str, object]] = []
    for command in commands:
        completed = subprocess.run(command, cwd=clean_root, env=environment, capture_output=True, text=True, timeout=240, check=False)
        combined = completed.stdout + "\n" + completed.stderr
        leaks = [value for value in forbidden if value.casefold() in combined.casefold()]
        if leaks:
            raise RuntimeError(f"browser cleanroom exposed source-repository paths: {leaks}")
        result = {"command": command[1:], "returncode": completed.returncode, "stdout_tail": completed.stdout[-2000:], "stderr_tail": completed.stderr[-2000:]}
        results.append(result)
        if completed.returncode != 0:
            raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify browser session productization in a source-free clean copy.")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="zyra-browser-cleanroom-") as directory:
        clean_root = Path(directory) / "zyra"
        build_clean_copy(clean_root)
        results = run_cleanroom(python=args.python.resolve(), clean_root=clean_root)
        print(json.dumps({"ok": True, "source_repositories_present": False, "commands": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
