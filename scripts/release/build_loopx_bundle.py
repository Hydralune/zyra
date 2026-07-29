from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS_ROOT = PROJECT_ROOT / "packages" / "integrations"
PRODUCTIZATION_ROOT = PROJECT_ROOT / "packages" / "productization"
for package_root in (INTEGRATIONS_ROOT, PRODUCTIZATION_ROOT):
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))

from zyra_integrations.loopx.install.manifest import (
    LOOPX_SOURCE_COMMIT,
    LOOPX_VERSION,
    PACKAGE_LOCK_SCHEMA,
    SOURCE_MANIFEST_SCHEMA,
    sha256_file,
    source_manifest_digest,
    stable_digest,
)
from zyra_productization.release.bundle import DeterministicArchiveWriter
from zyra_productization.release.policy import DEFAULT_RELEASE_POLICY
from zyra_productization.release.wheel import DeterministicWheelBuilder


SOURCE_DIRECTORY_INPUTS = (
    ".github",
    "apps",
    "docs",
    "examples",
    "loopx",
    "man",
    "scripts",
    "skills",
)
SOURCE_FILE_INPUTS = (
    "CONTRIBUTOR_TASKS.md",
    "LICENSE",
    "README.md",
    "pyproject.toml",
)
SLICE_BASE_COMMIT = "f9d9558cb67560ad88c52058ffb1644bf21d2773"
BLOCKED_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
}
BLOCKED_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the deterministic LoopX wheel/source bundle and package lock "
            "from the exact approved upstream commit."
        )
    )
    parser.add_argument(
        "--source-root",
        default=str(PROJECT_ROOT.parent / "long-horizon-systems" / "loopx"),
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--output", default="")
    return parser


def _git(source_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _git_executable_paths(source_root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(source_root), "ls-files", "--stage", "-z"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("git ls-files --stage failed")
    executable: set[str] = set()
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0]
        if mode == b"100755":
            executable.add(raw_path.decode("utf-8").replace("\\", "/"))
    return executable


def _selected_files(source_root: Path) -> Iterable[Path]:
    for name in SOURCE_DIRECTORY_INPUTS:
        root = source_root / name
        if not root.is_dir():
            raise RuntimeError(f"required LoopX source directory is missing: {root}")
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(source_root).as_posix().encode("utf-8"),
        ):
            relative = path.relative_to(source_root)
            if any(part in BLOCKED_NAMES for part in relative.parts):
                continue
            if path.suffix.casefold() in BLOCKED_SUFFIXES:
                continue
            yield path
    for name in SOURCE_FILE_INPUTS:
        path = source_root / name
        if not path.is_file():
            raise RuntimeError(f"required LoopX source file is missing: {path}")
        yield path


def _copy_snapshot(
    source_root: Path,
    destination: Path,
    *,
    executable_paths: set[str],
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in _selected_files(source_root):
        relative = source.relative_to(source_root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        target = destination.joinpath(*Path(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        executable = relative in executable_paths
        target.chmod(0o755 if executable else 0o644)
        files.append(
            {
                "path": relative,
                "sha256": sha256_file(target),
                "size": target.stat().st_size,
                "executable": executable,
            }
        )
    return sorted(files, key=lambda item: str(item["path"]).encode("utf-8"))


def build(
    *,
    source_root: Path,
    project_root: Path,
    output_path: Path | None,
) -> dict[str, Any]:
    source_root = source_root.resolve()
    project_root = project_root.resolve()
    commit = _git(source_root, "rev-parse", "HEAD")
    if commit != LOOPX_SOURCE_COMMIT:
        raise RuntimeError(
            f"LoopX source commit mismatch: expected {LOOPX_SOURCE_COMMIT}, got {commit}"
        )
    status = _git(source_root, "status", "--porcelain=v1", "--untracked-files=no")
    if status:
        raise RuntimeError("LoopX tracked source worktree must be clean")
    with (source_root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    project = pyproject.get("project")
    if not isinstance(project, dict) or project.get("version") != LOOPX_VERSION:
        raise RuntimeError("LoopX pyproject version does not match the frozen version")
    executable_paths = _git_executable_paths(source_root)
    asset_root = (
        project_root
        / "packages"
        / "integrations"
        / "zyra_integrations"
        / "loopx"
        / "install"
        / "assets"
    )
    asset_root.mkdir(parents=True, exist_ok=True)
    wheel_target = asset_root / f"loopx-{LOOPX_VERSION}-py3-none-any.whl"
    source_target = asset_root / f"loopx-{LOOPX_VERSION}-source.tar.gz"
    lock_target = project_root / "config" / "loopx" / "package-lock.json"
    with tempfile.TemporaryDirectory(prefix="zyra-loopx-bundle-") as raw:
        temporary = Path(raw)
        archive_input = temporary / "archive-input"
        snapshot = archive_input / f"loopx-{LOOPX_VERSION}"
        snapshot.mkdir(parents=True)
        files = _copy_snapshot(
            source_root,
            snapshot,
            executable_paths=executable_paths,
        )
        source_digest = source_manifest_digest(files)
        wheel_output = temporary / "wheel"
        wheel_receipt = DeterministicWheelBuilder(
            snapshot,
            policy=DEFAULT_RELEASE_POLICY,
        ).build(wheel_output)
        wheel_source = wheel_output / str(wheel_receipt["path"])
        archive_output = temporary / source_target.name
        archive_receipt = DeterministicArchiveWriter(
            archive_input,
            policy=DEFAULT_RELEASE_POLICY,
        ).write_tar_gz(
            archive_output,
            archive_root="loopx-source",
        )
        shutil.copyfile(wheel_source, wheel_target)
        shutil.copyfile(archive_output, source_target)
    wheel_target.chmod(0o644)
    source_target.chmod(0o644)
    relative_wheel = wheel_target.relative_to(project_root).as_posix()
    relative_source = source_target.relative_to(project_root).as_posix()
    source_manifest = {
        "schema": SOURCE_MANIFEST_SCHEMA,
        "source_commit": LOOPX_SOURCE_COMMIT,
        "package_version": LOOPX_VERSION,
        "archive_prefix": f"loopx-source/loopx-{LOOPX_VERSION}",
        "build_inputs": {
            "directories": list(SOURCE_DIRECTORY_INPUTS),
            "files": list(SOURCE_FILE_INPUTS),
            "excluded_names": sorted(BLOCKED_NAMES),
            "excluded_suffixes": sorted(BLOCKED_SUFFIXES),
        },
        "file_count": len(files),
        "source_digest": source_digest,
        "files": files,
    }
    lock: dict[str, Any] = {
        "schema": PACKAGE_LOCK_SCHEMA,
        "slice": {
            "slice_id": "P2-S01-01",
            "base_commit": SLICE_BASE_COMMIT,
        },
        "package": {
            "name": "loopx",
            "version": LOOPX_VERSION,
            "source_commit": LOOPX_SOURCE_COMMIT,
            "source_role": "supplementary_implementation",
            "migration_mode": "pinned_package_integration",
            "language": "python",
            "requires_python": ">=3.11",
            "runtime_dependencies": [],
            "runtime_dependency_imports": [],
            "entry_point": "loopx = loopx.cli:main",
            "state_owner": "loopx_private_control",
            "line_bucket": "runtime-assets/vendor-like",
        },
        "source_manifest": source_manifest,
        "artifacts": {
            "wheel": {
                "kind": "python-wheel",
                "path": relative_wheel,
                "sha256": sha256_file(wheel_target),
                "size": wheel_target.stat().st_size,
                "entry_count": int(wheel_receipt["entry_count"]),
                "wheel_source_digest": str(wheel_receipt["source_digest"]),
            },
            "source_bundle": {
                "kind": "upstream-install-snapshot",
                "path": relative_source,
                "sha256": sha256_file(source_target),
                "size": source_target.stat().st_size,
                "archive_type": str(archive_receipt["archive_type"]),
            },
        },
        "profiles": {
            "linux_wsl_upstream_semantics": {
                "artifact": "source_bundle",
                "source_digest": source_digest,
                "network_access": False,
                "user_home_write": False,
                "system_path_write": False,
                "project_local_roots": [
                    "bin",
                    "releases",
                    "skills",
                    "manual",
                    "runtime",
                    "state",
                    "profile",
                ],
                "disabled_by_default": [
                    "user_skill",
                    "slash_commands",
                    "claude_adapter",
                    "tmux_dashboard",
                    "canary",
                ],
            },
            "windows_release_offline_wheel": {
                "artifact": "wheel",
                "source_digest": source_digest,
                "network_access": False,
                "user_home_write": False,
                "system_path_write": False,
                "project_local_roots": [
                    "site-packages",
                    "bin",
                    "runtime",
                    "state",
                ],
                "disabled_by_default": [
                    "posix_shell_installer",
                    "user_skill",
                    "slash_commands",
                    "claude_adapter",
                    "tmux_dashboard",
                    "canary",
                ],
            },
        },
        "doctor": {
            "schema": "zyra.loopx-doctor-metadata/v1",
            "required_deep_checks": [
                "package-identity",
                "offline-artifacts",
                "profile-boundary",
                "release-independence",
                "import-and-cli-entry",
            ],
            "required_identity": {
                "version": LOOPX_VERSION,
                "source_commit": LOOPX_SOURCE_COMMIT,
                "source_digest": source_digest,
                "entry_point": "loopx = loopx.cli:main",
            },
            "workspace_state_contract": "{workspace}/.zyra/loopx/state",
            "user_level_writes_disabled": True,
        },
        "release": {
            "packaged_runtime": True,
            "supplementary": True,
            "offline": True,
            "source_repository_required_at_runtime": False,
            "default_disabled_noncompetition_surfaces": [
                "user_skill",
                "slash_commands",
                "claude_adapter",
                "tmux_dashboard",
                "canary",
            ],
        },
        "build": {
            "builder": "scripts/release/build_loopx_bundle.py",
            "source_date_epoch": DEFAULT_RELEASE_POLICY.source_date_epoch,
            "reproducible": True,
        },
    }
    lock["lock_digest"] = stable_digest(lock)
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    lock_target.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    result = {
        "schema": "zyra.loopx-bundle-build/v1",
        "ready": True,
        "source_root": str(source_root),
        "source_commit": LOOPX_SOURCE_COMMIT,
        "version": LOOPX_VERSION,
        "source_digest": source_digest,
        "source_file_count": len(files),
        "wheel": lock["artifacts"]["wheel"],
        "source_bundle": lock["artifacts"]["source_bundle"],
        "package_lock": str(lock_target),
        "package_lock_digest": lock["lock_digest"],
        "profiles": sorted(lock["profiles"]),
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return result


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    result = build(
        source_root=Path(arguments.source_root),
        project_root=Path(arguments.project_root),
        output_path=Path(arguments.output).resolve() if arguments.output else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
