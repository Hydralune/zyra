from __future__ import annotations

"""Deterministic tree capture and private copy utilities.

The functions in this module operate only on roots already resolved by the
workspace manager.  They never produce public physical locations.  Tree
manifests use logical paths, content hashes and repository-scope labels so the
merge runtime can reason about parent/child changes without making Git a
second canonical state owner.
"""

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .atomic import atomic_write_bytes, file_identity, sha256_file
from .errors import WorkspaceError, WorkspaceErrorCode
from .integration_models import TreeEntry, TreeEntryKind, WorkspaceTreeManifest
from .models import new_workspace_id
from .paths import WorkspacePathSafetyPolicy


DEFAULT_EXCLUDED_NAMES = frozenset(
    {
        ".zyra-workspace.json",
        ".git",
        ".DS_Store",
        "Thumbs.db",
        "__pycache__",
    }
)


@dataclass(frozen=True, slots=True)
class TreeScanLimits:
    max_entries: int = 250_000
    max_total_bytes: int = 8 * 1024 * 1024 * 1024
    max_single_file_bytes: int = 512 * 1024 * 1024
    reject_symlinks: bool = True
    reject_hardlinks: bool = True

    def __post_init__(self) -> None:
        if self.max_entries < 1 or self.max_total_bytes < 1 or self.max_single_file_bytes < 1:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace tree scan limits must be positive.",
                operation="validate_tree_scan_limits",
            )


@dataclass(frozen=True, slots=True)
class TreeCopyResult:
    file_count: int
    directory_count: int
    total_bytes: int
    source_manifest_id: str
    target_manifest_id: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "total_bytes": self.total_bytes,
            "source_manifest_id": self.source_manifest_id,
            "target_manifest_id": self.target_manifest_id,
            "verified": self.verified,
            "physical_location_redacted": True,
        }


@dataclass(frozen=True, slots=True)
class TreeChange:
    logical_path: str
    before: TreeEntry | None
    after: TreeEntry | None

    @property
    def kind(self) -> str:
        if self.before is None:
            return "added"
        if self.after is None:
            return "deleted"
        if self.before.kind is not self.after.kind:
            return "type_changed"
        return "modified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "kind": self.kind,
            "before": self.before.to_dict() if self.before is not None else None,
            "after": self.after.to_dict() if self.after is not None else None,
        }


def scan_workspace_tree(
    root: str | Path,
    *,
    workspace_id: str,
    owner_epoch: int,
    repository_roots: Iterable[str] = (),
    source: str = "workspace",
    limits: TreeScanLimits | None = None,
    excluded_names: Iterable[str] = DEFAULT_EXCLUDED_NAMES,
) -> WorkspaceTreeManifest:
    selected_limits = limits or TreeScanLimits()
    selected_root = Path(root).resolve()
    if not selected_root.is_dir() or selected_root.is_symlink():
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_LOCATION,
            "Workspace tree capture requires a real directory root.",
            workspace_id=workspace_id,
            operation="scan_workspace_tree",
        )
    exclusions = frozenset(str(item) for item in excluded_names)
    repositories = tuple(sorted(set(_normalize_repository_root(item) for item in repository_roots)))
    entries: list[TreeEntry] = []
    observed_entries = 0
    observed_bytes = 0
    root_identity = file_identity(selected_root)
    for current, directories, files in os.walk(selected_root, topdown=True, followlinks=False):
        current_path = Path(current)
        _assert_inside(current_path, selected_root)
        safe_directories: list[str] = []
        for name in sorted(directories, key=lambda item: (item.casefold(), item)):
            candidate = current_path / name
            info = candidate.lstat()
            if name in exclusions:
                continue
            if stat.S_ISLNK(info.st_mode):
                if selected_limits.reject_symlinks:
                    raise WorkspaceError(
                        WorkspaceErrorCode.SYMLINK_ESCAPE,
                        "Workspace tree capture rejects directory symlinks.",
                        workspace_id=workspace_id,
                        operation="scan_workspace_tree",
                        path=_logical(candidate, selected_root),
                    )
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise WorkspaceError(
                    WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                    "Workspace tree contains a non-directory traversal component.",
                    workspace_id=workspace_id,
                    operation="scan_workspace_tree",
                    path=_logical(candidate, selected_root),
                )
            safe_directories.append(name)
            observed_entries += 1
            _assert_entry_limit(observed_entries, selected_limits, workspace_id)
            entries.append(
                TreeEntry(
                    logical_path=_logical(candidate, selected_root),
                    kind=TreeEntryKind.DIRECTORY,
                    mode=stat.S_IMODE(info.st_mode),
                    mtime_ns=int(info.st_mtime_ns),
                    repository_root=_repository_for(_logical(candidate, selected_root), repositories),
                )
            )
        directories[:] = safe_directories
        for name in sorted(files, key=lambda item: (item.casefold(), item)):
            if name in exclusions:
                continue
            candidate = current_path / name
            info = candidate.lstat()
            observed_entries += 1
            _assert_entry_limit(observed_entries, selected_limits, workspace_id)
            logical_path = _logical(candidate, selected_root)
            if stat.S_ISLNK(info.st_mode):
                if selected_limits.reject_symlinks:
                    raise WorkspaceError(
                        WorkspaceErrorCode.SYMLINK_ESCAPE,
                        "Workspace tree capture rejects file symlinks.",
                        workspace_id=workspace_id,
                        operation="scan_workspace_tree",
                        path=logical_path,
                    )
                continue
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceError(
                    WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                    "Workspace tree capture rejects devices, sockets, and pipes.",
                    workspace_id=workspace_id,
                    operation="scan_workspace_tree",
                    path=logical_path,
                )
            if selected_limits.reject_hardlinks and int(info.st_nlink) > 1:
                raise WorkspaceError(
                    WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                    "Workspace tree capture rejects hard-linked files.",
                    workspace_id=workspace_id,
                    operation="scan_workspace_tree",
                    path=logical_path,
                    actual={"link_count": int(info.st_nlink)},
                )
            size = int(info.st_size)
            if size > selected_limits.max_single_file_bytes:
                raise WorkspaceError(
                    WorkspaceErrorCode.SINGLE_FILE_LIMIT_EXCEEDED,
                    "Workspace tree file exceeds the bounded capture limit.",
                    workspace_id=workspace_id,
                    operation="scan_workspace_tree",
                    path=logical_path,
                    expected=selected_limits.max_single_file_bytes,
                    actual=size,
                )
            observed_bytes += size
            if observed_bytes > selected_limits.max_total_bytes:
                raise WorkspaceError(
                    WorkspaceErrorCode.QUOTA_EXCEEDED,
                    "Workspace tree exceeds the bounded capture byte limit.",
                    workspace_id=workspace_id,
                    operation="scan_workspace_tree",
                    expected=selected_limits.max_total_bytes,
                    actual=observed_bytes,
                )
            before_identity = file_identity(candidate)
            content_hash = sha256_file(candidate)
            after_info = candidate.lstat()
            after_identity = file_identity(candidate)
            if (
                before_identity != after_identity
                or int(after_info.st_size) != size
                or int(after_info.st_mtime_ns) != int(info.st_mtime_ns)
            ):
                raise WorkspaceError(
                    WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                    "Workspace file changed while its tree manifest was captured.",
                    workspace_id=workspace_id,
                    operation="scan_workspace_tree",
                    path=logical_path,
                )
            entries.append(
                TreeEntry(
                    logical_path=logical_path,
                    kind=TreeEntryKind.FILE,
                    content_hash=content_hash,
                    size=size,
                    mode=stat.S_IMODE(info.st_mode),
                    mtime_ns=int(info.st_mtime_ns),
                    repository_root=_repository_for(logical_path, repositories),
                )
            )
    if file_identity(selected_root) != root_identity:
        raise WorkspaceError(
            WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
            "Workspace root changed while its tree manifest was captured.",
            workspace_id=workspace_id,
            operation="scan_workspace_tree",
        )
    return WorkspaceTreeManifest(
        manifest_id="",
        workspace_id=workspace_id,
        owner_epoch=owner_epoch,
        entries=tuple(entries),
        source=source,
    )


def copy_workspace_tree(
    source: str | Path,
    target: str | Path,
    *,
    workspace_id: str,
    owner_epoch: int,
    repository_roots: Iterable[str] = (),
    limits: TreeScanLimits | None = None,
    allow_existing_empty: bool = True,
) -> TreeCopyResult:
    selected_limits = limits or TreeScanLimits()
    source_root = Path(source).resolve()
    target_root = Path(target).resolve()
    if target_root == source_root or target_root in source_root.parents or source_root in target_root.parents:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_LOCATION,
            "Workspace copy source and target must be disjoint.",
            workspace_id=workspace_id,
            operation="copy_workspace_tree",
        )
    if target_root.exists():
        if not allow_existing_empty or any(target_root.iterdir()):
            raise WorkspaceError(
                WorkspaceErrorCode.ALREADY_EXISTS,
                "Workspace copy target is not empty.",
                workspace_id=workspace_id,
                operation="copy_workspace_tree",
            )
    else:
        target_root.mkdir(parents=True)
    source_manifest = scan_workspace_tree(
        source_root,
        workspace_id=workspace_id,
        owner_epoch=owner_epoch,
        repository_roots=repository_roots,
        source="copy_source",
        limits=selected_limits,
    )
    by_path = source_manifest.by_path()
    for entry in source_manifest.entries:
        destination = target_root.joinpath(*Path(entry.logical_path).parts)
        _assert_inside(destination, target_root)
        if entry.kind is TreeEntryKind.DIRECTORY:
            destination.mkdir(parents=True, exist_ok=True)
            _safe_chmod(destination, entry.mode)
            continue
        source_path = source_root.joinpath(*Path(entry.logical_path).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = source_path.read_bytes()
        if len(content) != entry.size or hashlib_sha256(content) != entry.content_hash:
            raise WorkspaceError(
                WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                "Workspace copy source changed after manifest capture.",
                workspace_id=workspace_id,
                operation="copy_workspace_tree",
                path=entry.logical_path,
            )
        atomic_write_bytes(destination, content, mode=entry.mode)
        _safe_chmod(destination, entry.mode)
    target_manifest = scan_workspace_tree(
        target_root,
        workspace_id=workspace_id,
        owner_epoch=owner_epoch,
        repository_roots=repository_roots,
        source="copy_target",
        limits=selected_limits,
    )
    source_projection = _content_projection(source_manifest)
    target_projection = _content_projection(target_manifest)
    if source_projection != target_projection:
        raise WorkspaceError(
            WorkspaceErrorCode.RESTORE_CONFLICT,
            "Workspace copy verification did not reproduce the source tree.",
            workspace_id=workspace_id,
            operation="copy_workspace_tree",
            expected=source_manifest.manifest_id,
            actual=target_manifest.manifest_id,
        )
    return TreeCopyResult(
        file_count=sum(1 for item in source_manifest.entries if item.kind is TreeEntryKind.FILE),
        directory_count=sum(1 for item in source_manifest.entries if item.kind is TreeEntryKind.DIRECTORY),
        total_bytes=sum(item.size for item in source_manifest.entries if item.kind is TreeEntryKind.FILE),
        source_manifest_id=source_manifest.manifest_id,
        target_manifest_id=target_manifest.manifest_id,
        verified=True,
    )


def diff_manifests(
    before: WorkspaceTreeManifest,
    after: WorkspaceTreeManifest,
) -> tuple[TreeChange, ...]:
    before_by_path = before.by_path()
    after_by_path = after.by_path()
    changes: list[TreeChange] = []
    for path in sorted(set(before_by_path) | set(after_by_path)):
        prior = before_by_path.get(path)
        latest = after_by_path.get(path)
        if _entry_content_identity(prior) == _entry_content_identity(latest):
            continue
        changes.append(TreeChange(logical_path=path, before=prior, after=latest))
    return tuple(changes)


def read_tree_file(root: str | Path, entry: TreeEntry, *, max_bytes: int) -> bytes:
    if entry.kind is not TreeEntryKind.FILE:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_ARGUMENT,
            "Workspace tree entry is not a file.",
            operation="read_tree_file",
            path=entry.logical_path,
        )
    if entry.size > max_bytes:
        raise WorkspaceError(
            WorkspaceErrorCode.SINGLE_FILE_LIMIT_EXCEEDED,
            "Workspace merge file exceeds its bounded read limit.",
            operation="read_tree_file",
            path=entry.logical_path,
            expected=max_bytes,
            actual=entry.size,
        )
    selected_root = Path(root).resolve()
    target = selected_root.joinpath(*Path(entry.logical_path).parts).resolve()
    _assert_inside(target, selected_root)
    content = target.read_bytes()
    if len(content) != entry.size or hashlib_sha256(content) != entry.content_hash:
        raise WorkspaceError(
            WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
            "Workspace tree file no longer matches its manifest.",
            operation="read_tree_file",
            path=entry.logical_path,
        )
    return content


def safe_remove_private_tree(path: str | Path, *, allowed_root: str | Path) -> None:
    selected = Path(path).resolve()
    root = Path(allowed_root).resolve()
    _assert_inside(selected, root)
    if selected == root:
        raise WorkspaceError(
            WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
            "Private tree cleanup cannot remove its owning root.",
            operation="remove_private_workspace_tree",
        )
    if not selected.exists():
        return
    for current, directories, files in os.walk(selected, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink():
                candidate.unlink()
            else:
                _make_writable(candidate)
                candidate.unlink()
        for name in directories:
            candidate = current_path / name
            if candidate.is_symlink():
                candidate.unlink()
            else:
                _make_writable(candidate)
                candidate.rmdir()
    _make_writable(selected)
    selected.rmdir()


def _content_projection(manifest: WorkspaceTreeManifest) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            item.logical_path,
            item.kind.value,
            item.content_hash,
            item.size,
            item.repository_root,
        )
        for item in manifest.entries
    )


def _entry_content_identity(entry: TreeEntry | None) -> tuple[Any, ...]:
    if entry is None:
        return ("absent",)
    return (entry.kind.value, entry.content_hash, entry.size, entry.repository_root)


def _normalize_repository_root(value: Any) -> str:
    text = str(value or ".").replace("\\", "/").strip("/")
    return text or "."


def _repository_for(logical_path: str, repositories: tuple[str, ...]) -> str:
    selected = "."
    selected_depth = -1
    path_parts = Path(logical_path).parts
    for repository in repositories:
        if repository == ".":
            if selected_depth < 0:
                selected = "."
                selected_depth = 0
            continue
        repository_parts = Path(repository).parts
        if len(repository_parts) <= len(path_parts) and path_parts[: len(repository_parts)] == repository_parts:
            if len(repository_parts) > selected_depth:
                selected = repository
                selected_depth = len(repository_parts)
    return selected


def _logical(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _assert_inside(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise WorkspaceError(
            WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
            "Workspace tree operation escaped its private root.",
            operation="validate_workspace_tree_path",
        ) from error


def _assert_entry_limit(count: int, limits: TreeScanLimits, workspace_id: str) -> None:
    if count > limits.max_entries:
        raise WorkspaceError(
            WorkspaceErrorCode.QUOTA_EXCEEDED,
            "Workspace tree capture exceeded its entry limit.",
            workspace_id=workspace_id,
            operation="scan_workspace_tree",
            expected=limits.max_entries,
            actual=count,
        )


def _safe_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(int(mode) & 0o777)
    except OSError:
        pass


def _make_writable(path: Path) -> None:
    try:
        path.chmod(stat.S_IMODE(path.lstat().st_mode) | stat.S_IWUSR)
    except OSError:
        pass


def hashlib_sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


__all__ = [
    "DEFAULT_EXCLUDED_NAMES",
    "TreeChange",
    "TreeCopyResult",
    "TreeScanLimits",
    "copy_workspace_tree",
    "diff_manifests",
    "read_tree_file",
    "safe_remove_private_tree",
    "scan_workspace_tree",
]
