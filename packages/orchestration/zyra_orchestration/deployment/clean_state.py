from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CleanStateRejected
from .models import digest, new_id, now_iso


_DENIED_NAMES = {
    ".git",
    ".venv",
    "node_modules",
    "vendor",
    "vendor-runtimes",
    "third_party",
}
_CLEANABLE_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    "artifacts",
    "build",
    "dist",
    "index",
    "replay",
    "state",
    "tmp",
}


@dataclass(frozen=True, slots=True)
class PathFingerprint:
    path: str
    exists: bool
    kind: str
    file_count: int
    directory_count: int
    total_bytes: int
    content_digest: str
    symlink_count: int
    newest_mtime_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "kind": self.kind,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "total_bytes": self.total_bytes,
            "content_digest": self.content_digest,
            "symlink_count": self.symlink_count,
            "newest_mtime_ns": self.newest_mtime_ns,
        }


class CleanStateManager:
    def __init__(
        self,
        *,
        project_root: Path | str,
        deployment_root: Path | str,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.deployment_root = Path(deployment_root).resolve()
        if self.deployment_root == self.project_root:
            raise CleanStateRejected(
                "clean_state_root_too_broad",
                "deployment clean-state root cannot be the project root",
                operation="initialize",
            )
        if self.project_root not in self.deployment_root.parents:
            raise CleanStateRejected(
                "clean_state_root_outside_project",
                "deployment clean-state root must remain inside the project",
                operation="initialize",
                details={
                    "project_root": str(self.project_root),
                    "deployment_root": str(self.deployment_root),
                },
            )
        self.deployment_root.mkdir(parents=True, exist_ok=True)

    def create_run_root(
        self,
        *,
        prefix: str = "semantic-health",
    ) -> Path:
        run_root = (self.deployment_root / "runs" / new_id(prefix)).resolve()
        self._require_descendant(run_root)
        run_root.mkdir(parents=True, exist_ok=False)
        marker = run_root / ".zyra-deployment-ephemeral"
        marker.write_text(
            f"created_at={now_iso()}\nproject_root={self.project_root}\n",
            encoding="utf-8",
        )
        for name in ("state", "artifacts", "workspace", "index", "logs"):
            (run_root / name).mkdir()
        return run_root

    def reset_run_root(self, run_root: Path | str) -> dict[str, Any]:
        target = Path(run_root).resolve()
        self._require_run_root(target)
        before = self.fingerprint(target)
        removed: list[str] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            if child.name == ".zyra-deployment-ephemeral":
                continue
            self._safe_remove(child)
            removed.append(child.name)
        for name in ("state", "artifacts", "workspace", "index", "logs"):
            (target / name).mkdir()
        after = self.fingerprint(target)
        return {
            "schema": "zyra.deployment-clean-state-reset/v1",
            "run_root": str(target),
            "removed": removed,
            "before": before.to_dict(),
            "after": after.to_dict(),
            "fresh": after.file_count == 1,
            "reset_at": now_iso(),
        }

    def remove_run_root(self, run_root: Path | str) -> dict[str, Any]:
        target = Path(run_root).resolve()
        self._require_run_root(target)
        before = self.fingerprint(target)
        self._safe_remove(target)
        return {
            "schema": "zyra.deployment-clean-state-removal/v1",
            "run_root": str(target),
            "removed": not target.exists(),
            "before": before.to_dict(),
            "removed_at": now_iso(),
        }

    def fingerprint(
        self,
        path: Path | str,
        *,
        maximum_files: int = 200_000,
    ) -> PathFingerprint:
        root = Path(path).resolve()
        if not root.exists():
            return PathFingerprint(
                path=str(root),
                exists=False,
                kind="missing",
                file_count=0,
                directory_count=0,
                total_bytes=0,
                content_digest=digest([]),
                symlink_count=0,
                newest_mtime_ns=0,
            )
        if root.is_file():
            stat_value = root.stat()
            return PathFingerprint(
                path=str(root),
                exists=True,
                kind="file",
                file_count=1,
                directory_count=0,
                total_bytes=stat_value.st_size,
                content_digest=self._file_digest(root),
                symlink_count=1 if root.is_symlink() else 0,
                newest_mtime_ns=stat_value.st_mtime_ns,
            )
        entries: list[tuple[str, str, int, int, str]] = []
        files = 0
        directories = 0
        total_bytes = 0
        symlinks = 0
        newest = 0
        stack = [root]
        while stack:
            current = stack.pop()
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                relative = child.relative_to(root).as_posix()
                try:
                    child_stat = child.lstat()
                except FileNotFoundError:
                    continue
                newest = max(newest, child_stat.st_mtime_ns)
                if stat.S_ISLNK(child_stat.st_mode):
                    symlinks += 1
                    entries.append((relative, "symlink", 0, child_stat.st_mtime_ns, ""))
                    continue
                if child.is_dir():
                    directories += 1
                    entries.append((relative, "directory", 0, child_stat.st_mtime_ns, ""))
                    stack.append(child)
                    continue
                files += 1
                if files > maximum_files:
                    raise CleanStateRejected(
                        "clean_state_fingerprint_file_limit",
                        "clean-state fingerprint exceeds maximum file count",
                        operation="fingerprint",
                        details={"path": str(root), "maximum_files": maximum_files},
                    )
                total_bytes += child_stat.st_size
                entries.append(
                    (
                        relative,
                        "file",
                        child_stat.st_size,
                        child_stat.st_mtime_ns,
                        self._file_digest(child),
                    )
                )
        return PathFingerprint(
            path=str(root),
            exists=True,
            kind="directory",
            file_count=files,
            directory_count=directories,
            total_bytes=total_bytes,
            content_digest=digest(entries),
            symlink_count=symlinks,
            newest_mtime_ns=newest,
        )

    def audit_residue(
        self,
        roots: Sequence[Path | str],
    ) -> dict[str, Any]:
        fingerprints = [self.fingerprint(path) for path in roots]
        blockers: list[str] = []
        for item in fingerprints:
            name = Path(item.path).name
            if item.exists and item.file_count and name in {
                ".cache",
                ".pytest_cache",
                ".ruff_cache",
                "replay",
            }:
                blockers.append(f"residue:{name}")
            if item.symlink_count:
                blockers.append(f"symlink-residue:{name}")
        return {
            "schema": "zyra.deployment-residue-audit/v1",
            "ready": not blockers,
            "fingerprints": [item.to_dict() for item in fingerprints],
            "blockers": blockers,
            "audited_at": now_iso(),
        }

    def _require_descendant(self, target: Path) -> None:
        resolved = target.resolve()
        if resolved == self.deployment_root or self.deployment_root not in resolved.parents:
            raise CleanStateRejected(
                "clean_state_target_invalid",
                "clean-state target must be a strict deployment-root descendant",
                operation="validate_target",
                details={"target": str(resolved)},
            )
        if any(part in _DENIED_NAMES for part in resolved.parts):
            raise CleanStateRejected(
                "clean_state_denied_path",
                "clean-state target crosses a protected project path",
                operation="validate_target",
                details={"target": str(resolved)},
            )

    def _require_run_root(self, target: Path) -> None:
        self._require_descendant(target)
        runs = (self.deployment_root / "runs").resolve()
        if runs not in target.parents:
            raise CleanStateRejected(
                "clean_state_not_run_root",
                "clean-state operation is limited to deployment run roots",
                operation="validate_run_root",
                details={"target": str(target)},
            )
        marker = target / ".zyra-deployment-ephemeral"
        if not marker.is_file():
            raise CleanStateRejected(
                "clean_state_marker_missing",
                "deployment run root lacks the deletion safety marker",
                operation="validate_run_root",
                details={"target": str(target)},
            )

    def _safe_remove(self, target: Path) -> None:
        resolved = target.resolve()
        self._require_descendant(resolved)
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
            return
        if target.is_dir():
            shutil.rmtree(target)

    @staticmethod
    def _file_digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        return "sha256:" + hasher.hexdigest()


__all__ = [
    "CleanStateManager",
    "PathFingerprint",
]
