from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from .atomic import file_identity
from .errors import WorkspaceErrorCode, WorkspacePathError
from .models import WorkspaceKind, WorkspaceMount, WorkspaceMountAccess, WorkspaceOperation, WorkspaceQuota


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
WINDOWS_INVALID_CHARS = set('<>:"|?*')
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./")


@dataclass(frozen=True, slots=True)
class CanonicalWorkspacePath:
    logical_path: str
    relative_parts: tuple[str, ...]
    physical_path: Path
    root_identity: str
    parent_identity: str
    target_identity: str
    exists: bool
    operation: WorkspaceOperation
    mount_kind: WorkspaceKind
    mount_access: WorkspaceMountAccess
    collision_key: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "exists": self.exists,
            "operation": self.operation.value,
            "mount_kind": self.mount_kind.value,
            "mount_access": self.mount_access.value,
            "collision_key": self.collision_key,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SubmissionBoundary:
    allowed_roots: tuple[str, ...] = ("apps", "packages", "skills", "scripts", "tests", "docs")
    denied_roots: tuple[str, ...] = (
        ".git",
        ".hg",
        ".svn",
        ".cache",
        ".pytest_cache",
        "node_modules",
        "vendor",
        "vendor-runtimes",
        "source-pool",
        "runtime-sources",
    )
    allow_root_files: tuple[str, ...] = (
        "README.md",
        "LICENSE",
        "NOTICE",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "uv.lock",
    )
    case_sensitive: bool = False

    def evaluate(self, relative_path: str) -> tuple[bool, str]:
        normalized = relative_path.replace("\\", "/").strip("/")
        parts = PurePosixPath(normalized).parts
        if not parts:
            return False, "submission_root_mutation_rejected"
        key = parts[0] if self.case_sensitive else parts[0].casefold()
        denied = {item if self.case_sensitive else item.casefold() for item in self.denied_roots}
        if key in denied:
            return False, "submission_denied_root"
        allowed = {item if self.case_sensitive else item.casefold() for item in self.allowed_roots}
        if key in allowed:
            return True, "submission_allowed_root"
        if len(parts) == 1:
            root_files = {item if self.case_sensitive else item.casefold() for item in self.allow_root_files}
            if key in root_files:
                return True, "submission_allowed_root_file"
        return False, "submission_outside_allowed_roots"


class WorkspacePathSafetyPolicy:
    def __init__(
        self,
        *,
        service_root: str | Path,
        platform: str | None = None,
        reject_symlinks: bool = True,
        reject_hardlinks: bool = True,
        normalize_unicode: bool = True,
        casefold_collisions: bool | None = None,
        submission_boundary: SubmissionBoundary | None = None,
    ) -> None:
        self.service_root = Path(service_root).resolve()
        self.platform = (platform or os.name).lower()
        self.reject_symlinks = bool(reject_symlinks)
        self.reject_hardlinks = bool(reject_hardlinks)
        self.normalize_unicode = bool(normalize_unicode)
        self.casefold_collisions = (
            self.platform in {"nt", "windows"} if casefold_collisions is None else bool(casefold_collisions)
        )
        self.submission_boundary = submission_boundary

    def validate_logical_path(self, value: str, *, quota: WorkspaceQuota | None = None) -> tuple[str, tuple[str, ...]]:
        quota = quota or WorkspaceQuota()
        raw = str(value or "")
        if not raw or raw in {".", "./", ".\\"}:
            return ".", ()
        if CONTROL_CHARACTER_PATTERN.search(raw):
            raise self._error(WorkspaceErrorCode.PATH_TRAVERSAL, "Workspace paths cannot contain control characters.", raw)
        if raw.startswith(DEVICE_PREFIXES):
            raise self._error(WorkspaceErrorCode.DEVICE_PATH_REJECTED, "Windows device paths are not workspace paths.", raw)
        if raw.startswith(("\\\\", "//")):
            raise self._error(WorkspaceErrorCode.UNC_PATH_REJECTED, "UNC and network paths are rejected.", raw)
        if DRIVE_PATTERN.match(raw) or PureWindowsPath(raw).drive:
            raise self._error(WorkspaceErrorCode.ABSOLUTE_PATH_REJECTED, "Drive-qualified workspace paths are rejected.", raw)
        if raw.startswith(("/", "\\")):
            raise self._error(WorkspaceErrorCode.ABSOLUTE_PATH_REJECTED, "Workspace paths must be relative.", raw)
        parsed = urlparse(raw)
        if parsed.scheme and len(parsed.scheme) > 1:
            raise self._error(WorkspaceErrorCode.PATH_TRAVERSAL, "URI values are not workspace paths.", raw)

        normalized_separators = raw.replace("\\", "/")
        pure = PurePosixPath(normalized_separators)
        if pure.is_absolute() or any(part == ".." for part in pure.parts):
            raise self._error(WorkspaceErrorCode.PATH_TRAVERSAL, "Workspace path traversal is rejected.", raw)
        parts: list[str] = []
        for raw_part in pure.parts:
            if raw_part in {"", "."}:
                continue
            normalized = unicodedata.normalize("NFC", raw_part) if self.normalize_unicode else raw_part
            self._validate_component(normalized, raw)
            parts.append(normalized)
        if len(parts) > quota.max_path_depth:
            raise self._error(
                WorkspaceErrorCode.PATH_TRAVERSAL,
                "Workspace path exceeds the configured depth limit.",
                raw,
                expected=quota.max_path_depth,
                actual=len(parts),
            )
        canonical = "/".join(parts) if parts else "."
        if len(canonical.encode("utf-8")) > 4096:
            raise self._error(
                WorkspaceErrorCode.PATH_TRAVERSAL,
                "Workspace path exceeds the encoded path limit.",
                raw,
                expected=4096,
                actual=len(canonical.encode("utf-8")),
            )
        return canonical, tuple(parts)

    def resolve(
        self,
        *,
        root: str | Path,
        logical_path: str,
        operation: WorkspaceOperation,
        mount: WorkspaceMount,
        require_exists: bool = False,
        allow_root: bool = False,
        submission_check: bool = False,
    ) -> CanonicalWorkspacePath:
        workspace_root = Path(root).resolve()
        self._assert_within_service_root(workspace_root)
        canonical, parts = self.validate_logical_path(logical_path, quota=mount.quota)
        if not parts and not allow_root:
            raise self._error(WorkspaceErrorCode.PATH_TRAVERSAL, "The workspace root is not a file target.", logical_path)
        self._enforce_mount_access(mount, operation)
        if submission_check and self.submission_boundary is not None and canonical != ".":
            allowed, reason = self.submission_boundary.evaluate(canonical)
            if not allowed:
                raise self._error(
                    WorkspaceErrorCode.SUBMISSION_BOUNDARY_VIOLATION,
                    "Workspace path is outside the product submission boundary.",
                    canonical,
                    metadata={"reason": reason},
                )
        root_identity = file_identity(workspace_root)
        if root_identity == "absent":
            raise self._error(WorkspaceErrorCode.NOT_FOUND, "Workspace root does not exist.", str(workspace_root))
        self._validate_existing_ancestors(workspace_root, parts)
        physical = workspace_root.joinpath(*parts)
        try:
            physical.relative_to(workspace_root)
        except ValueError as error:
            raise self._error(WorkspaceErrorCode.PATH_OUTSIDE_ROOT, "Workspace path escaped its root.", canonical) from error
        exists = physical.exists() or physical.is_symlink()
        if require_exists and not exists:
            raise self._error(WorkspaceErrorCode.NOT_FOUND, "Workspace path does not exist.", canonical)
        if exists:
            info = physical.lstat()
            if self.reject_symlinks and stat.S_ISLNK(info.st_mode):
                raise self._error(WorkspaceErrorCode.SYMLINK_ESCAPE, "Workspace symlink targets are rejected.", canonical)
            if self.reject_hardlinks and stat.S_ISREG(info.st_mode) and int(info.st_nlink) > 1 and operation in {
                WorkspaceOperation.WRITE,
                WorkspaceOperation.DELETE,
                WorkspaceOperation.RESTORE,
            }:
                raise self._error(
                    WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                    "Mutable workspace files cannot have multiple hard links.",
                    canonical,
                    actual=int(info.st_nlink),
                )
        parent = physical if not parts else physical.parent
        return CanonicalWorkspacePath(
            logical_path=canonical,
            relative_parts=parts,
            physical_path=physical,
            root_identity=root_identity,
            parent_identity=file_identity(parent),
            target_identity=file_identity(physical),
            exists=exists,
            operation=operation,
            mount_kind=mount.kind,
            mount_access=mount.access,
            collision_key=self.collision_key(canonical),
            metadata={"submission_checked": submission_check, "root_token": mount.metadata.get("root_token", "")},
        )

    def revalidate(self, value: CanonicalWorkspacePath, *, root: str | Path) -> None:
        workspace_root = Path(root).resolve()
        actual_root_identity = file_identity(workspace_root)
        if actual_root_identity != value.root_identity:
            raise self._error(
                WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                "Workspace root identity changed after path preflight.",
                value.logical_path,
                expected=value.root_identity,
                actual=actual_root_identity,
            )
        self._validate_existing_ancestors(workspace_root, value.relative_parts)
        actual_parent_identity = file_identity(value.physical_path.parent if value.relative_parts else value.physical_path)
        if actual_parent_identity != value.parent_identity:
            raise self._error(
                WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                "Workspace parent directory identity changed after path preflight.",
                value.logical_path,
                expected=value.parent_identity,
                actual=actual_parent_identity,
            )

    def collision_key(self, logical_path: str) -> str:
        value = unicodedata.normalize("NFC", logical_path)
        return value.casefold() if self.casefold_collisions else value

    def assert_no_collisions(self, paths: Iterable[str]) -> None:
        observed: dict[str, str] = {}
        for path in paths:
            canonical, _ = self.validate_logical_path(path)
            key = self.collision_key(canonical)
            previous = observed.get(key)
            if previous is not None and previous != canonical:
                raise self._error(
                    WorkspaceErrorCode.PATH_TRAVERSAL,
                    "Workspace paths collide after Unicode normalization or platform case folding.",
                    canonical,
                    metadata={"conflicts_with": previous},
                )
            observed[key] = canonical

    def _validate_existing_ancestors(self, root: Path, parts: tuple[str, ...]) -> None:
        current = root
        for part in parts[:-1]:
            current = current / part
            if not current.exists() and not current.is_symlink():
                break
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise self._error(WorkspaceErrorCode.SYMLINK_ESCAPE, "Workspace ancestors cannot be symlinks.", str(current))
            if not stat.S_ISDIR(info.st_mode):
                raise self._error(WorkspaceErrorCode.PATH_TRAVERSAL, "Workspace path ancestor is not a directory.", str(current))
            resolved = current.resolve()
            try:
                resolved.relative_to(root)
            except ValueError as error:
                raise self._error(WorkspaceErrorCode.PATH_OUTSIDE_ROOT, "Workspace ancestor escaped the root.", str(current)) from error

    def _assert_within_service_root(self, workspace_root: Path) -> None:
        try:
            workspace_root.relative_to(self.service_root)
        except ValueError as error:
            raise self._error(
                WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                "Workspace root is outside the configured service root.",
                str(workspace_root),
            ) from error

    def _validate_component(self, component: str, original: str) -> None:
        if component in {".", ".."} or not component:
            raise self._error(WorkspaceErrorCode.PATH_TRAVERSAL, "Workspace path component is invalid.", original)
        if len(component.encode("utf-8")) > 255:
            raise self._error(WorkspaceErrorCode.PATH_TRAVERSAL, "Workspace path component is too long.", original)
        if component.endswith((" ", ".")):
            raise self._error(WorkspaceErrorCode.RESERVED_NAME_REJECTED, "Workspace components cannot end in a dot or space.", original)
        stem = component.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED_NAMES:
            raise self._error(WorkspaceErrorCode.RESERVED_NAME_REJECTED, "Windows reserved device names are rejected.", original)
        if any(character in WINDOWS_INVALID_CHARS for character in component):
            raise self._error(WorkspaceErrorCode.RESERVED_NAME_REJECTED, "Workspace component contains platform-reserved characters.", original)
        if ":" in component:
            raise self._error(WorkspaceErrorCode.RESERVED_NAME_REJECTED, "Alternate data stream syntax is rejected.", original)

    @staticmethod
    def _enforce_mount_access(mount: WorkspaceMount, operation: WorkspaceOperation) -> None:
        mutating = operation in {
            WorkspaceOperation.WRITE,
            WorkspaceOperation.DELETE,
            WorkspaceOperation.RESTORE,
            WorkspaceOperation.CLEANUP,
        }
        if mount.access is WorkspaceMountAccess.READ_ONLY and mutating:
            raise WorkspacePathError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "Workspace mount is read-only.",
                workspace_id=mount.workspace_id,
                operation=operation.value,
                path=mount.relative_root,
            )
        if mount.access is WorkspaceMountAccess.APPEND_ONLY and operation in {
            WorkspaceOperation.DELETE,
            WorkspaceOperation.RESTORE,
        }:
            raise WorkspacePathError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "Workspace append-only mount does not permit destructive operations.",
                workspace_id=mount.workspace_id,
                operation=operation.value,
                path=mount.relative_root,
            )

    @staticmethod
    def _error(
        code: WorkspaceErrorCode,
        message: str,
        path: str,
        *,
        expected: Any = None,
        actual: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> WorkspacePathError:
        return WorkspacePathError(
            code,
            message,
            operation="path_safety",
            path=path,
            expected=expected,
            actual=actual,
            metadata=metadata,
        )
