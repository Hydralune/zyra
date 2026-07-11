from __future__ import annotations

"""Run MCP evidence from a source-repository-free, cache-free Zyra copy."""

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
COPY_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "pytest.ini",
)
IGNORED_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "artifacts",
    "tmp",
    "vendor",
    "vendor-runtimes",
}
SOURCE_REPOSITORIES = (
    "agent-framework",
    "agentscope",
    "browser-use",
    "claude-code-best",
    "hermes-agent",
    "langgraph",
    "opencode",
    "openclaw",
    "OpenHands",
)


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_NAMES or name.endswith((".pyc", ".pyo"))}


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
    forbidden = [name for name in IGNORED_NAMES if (destination / name).exists()]
    if forbidden:
        raise RuntimeError(f"clean copy contains excluded roots: {forbidden}")


def clean_environment(clean_root: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ZYRA_") and key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    state_root = clean_root / ".clean-state"
    state_root.mkdir()
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ZYRA_SQLITE_PATH": str(state_root / "zyra.sqlite3"),
            "ZYRA_EVENT_LOG": str(state_root / "events.jsonl"),
            "ZYRA_ARTIFACT_ROOT": str(state_root / "artifacts"),
            "ZYRA_MCP_STATE": str(state_root / "mcp-state.json"),
            "ZYRA_PERMISSION_STATE": str(state_root / "permission-state.json"),
            "ZYRA_PERMISSION_STORE": str(state_root / "permissions.json"),
        }
    )
    return environment


def run_cleanroom(*, python: Path, clean_root: Path) -> list[dict[str, object]]:
    commands = (
        [str(python), "-m", "unittest", "tests.integration.test_mcp_main_path_integration"],
        [str(python), "scripts/smoke_mcp_runtime.py"],
        [str(python), "scripts/verify_submission_boundary.py"],
    )
    environment = clean_environment(clean_root)
    forbidden_fragments = [
        str(PROJECT_ROOT.parent / repository)
        for repository in SOURCE_REPOSITORIES
    ]
    results: list[dict[str, object]] = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=clean_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        combined = completed.stdout + "\n" + completed.stderr
        leaked = [fragment for fragment in forbidden_fragments if fragment.casefold() in combined.casefold()]
        if leaked:
            raise RuntimeError(f"cleanroom command exposed source-repository paths: {leaked}")
        result = {
            "command": command[1:],
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
        results.append(result)
        if completed.returncode != 0:
            raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify MCP integration in a clean Zyra copy.")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="zyra-mcp-cleanroom-") as directory:
        clean_root = Path(directory) / "zyra"
        build_clean_copy(clean_root)
        results = run_cleanroom(python=args.python.resolve(), clean_root=clean_root)
        report = {
            "ok": True,
            "clean_root": str(clean_root),
            "source_repositories_present": False,
            "commands": results,
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
