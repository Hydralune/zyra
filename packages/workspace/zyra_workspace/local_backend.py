from __future__ import annotations

import os
import secrets
import shutil
import stat
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .atomic import KeyedLockPool, atomic_write_bytes, atomic_write_json, file_identity, read_json_object
from .errors import WorkspaceError, WorkspaceErrorCode
from .file_state import WorkspaceFileStateRuntime, WorkspaceReadResult
from .models import (
    FileWritePrecondition,
    LocalWorkspaceLocation,
    WorkspaceBinding,
    WorkspaceCapabilities,
    WorkspaceKind,
    WorkspaceLease,
    WorkspaceLeaseState,
    WorkspaceLifecycleState,
    WorkspaceMount,
    WorkspaceOperation,
    WorkspaceQuota,
    WorkspaceReadMode,
    WorkspaceUsage,
    utc_now,
)
from .mounts import WorkspaceArtifactMount, WorkspaceMountLayout
from .paths import CanonicalWorkspacePath, WorkspacePathSafetyPolicy
from .quota import WorkspaceQuotaRuntime
from .store import WorkspaceBindingStore


WORKSPACE_MARKER_SCHEMA = "zyra.local-workspace.v1"


@dataclass(frozen=True, slots=True)
class WorkspaceAccessHandle:
    workspace_id: str
    task_id: str
    session_id: str
    worker_id: str
    lease_id: str
    owner_epoch: int
    capability_revision: int
    operations: tuple[WorkspaceOperation, ...]
    backend_id: str
    mount_kinds: tuple[WorkspaceKind, ...]
    issued_at: str = field(default_factory=utc_now)
    internal_root: Path = field(default=Path("."), repr=False, compare=False)
    fence_token: str = field(default="", repr=False, compare=False)

    def allows(self, operation: WorkspaceOperation) -> bool:
        return operation in self.operations

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_id": self.worker_id,
            "lease_id": self.lease_id,
            "owner_epoch": self.owner_epoch,
            "capability_revision": self.capability_revision,
            "operations": [item.value for item in self.operations],
            "backend_id": self.backend_id,
            "mount_kinds": [item.value for item in self.mount_kinds],
            "issued_at": self.issued_at,
            "physical_location_redacted": True,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceDirectoryEntry:
    name: str
    path: str
    kind: str
    size_bytes: int
    mtime_ns: int
    identity: str
    writable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "identity": self.identity,
            "writable": self.writable,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceWriteResult:
    workspace_id: str
    mount_kind: WorkspaceKind
    path: str
    bytes_written: int
    prior_bytes: int
    created: bool
    content_hash: str
    read_epoch: int
    owner_epoch: int
    completed_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "mount_kind": self.mount_kind.value,
            "path": self.path,
            "bytes_written": self.bytes_written,
            "prior_bytes": self.prior_bytes,
            "created": self.created,
            "content_hash": self.content_hash,
            "read_epoch": self.read_epoch,
            "owner_epoch": self.owner_epoch,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceDeleteResult:
    workspace_id: str
    mount_kind: WorkspaceKind
    path: str
    removed_files: int
    removed_directories: int
    removed_bytes: int
    completed_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "mount_kind": self.mount_kind.value,
            "path": self.path,
            "removed_files": self.removed_files,
            "removed_directories": self.removed_directories,
            "removed_bytes": self.removed_bytes,
            "completed_at": self.completed_at,
        }


class LocalWorkspaceBackend:
    """Zyra-owned local backend; never exposes its physical root in public projections."""

    def __init__(
        self,
        *,
        backend_id: str,
        data_root: str | Path,
        binding_store: WorkspaceBindingStore,
        quota_runtime: WorkspaceQuotaRuntime,
        enabled: bool = True,
        capabilities: WorkspaceCapabilities | None = None,
    ) -> None:
        self.backend_id = backend_id
        self.data_root = Path(data_root).resolve()
        self.binding_store = binding_store
        self.quota_runtime = quota_runtime
        self.enabled = bool(enabled)
        self.capabilities = capabilities or WorkspaceCapabilities(
            revision=1,
            symlink_support=False,
            metadata={"backend": "local", "physical_paths_public": False},
        )
        self.path_policy = WorkspacePathSafetyPolicy(service_root=self.data_root)
        self.mount_policy = WorkspaceArtifactMount()
        self.file_state = WorkspaceFileStateRuntime(binding_store)
        self._locks = KeyedLockPool()
        if self.enabled:
            self.data_root.mkdir(parents=True, exist_ok=True)

    def health(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "kind": "local",
            "enabled": self.enabled,
            "capability_revision": self.capabilities.revision,
            "capabilities": self.capabilities.to_dict(),
            "data_root_exists": self.data_root.is_dir() if self.enabled else False,
            "physical_paths_public": False,
        }

    def provision(self, binding: WorkspaceBinding, mounts: Iterable[WorkspaceMount]) -> Path:
        self._require_enabled()
        self._require_local_binding(binding)
        layout = WorkspaceMountLayout(workspace_id=binding.workspace_id, mounts=tuple(mounts))
        self.mount_policy.validate_layout(layout)
        root = self.physical_root(binding)
        with self._locks.acquire_many((binding.workspace_id,)):
            if root.exists():
                marker = self._read_marker(root)
                if marker.get("workspace_id") != binding.workspace_id:
                    raise WorkspaceError(
                        WorkspaceErrorCode.ALREADY_EXISTS,
                        "A different workspace already owns the selected local root.",
                        workspace_id=binding.workspace_id,
                        operation="provision_workspace",
                    )
                return root
            root.mkdir(parents=True, mode=0o700)
            try:
                self.mount_policy.materialize_directories(root, layout.mounts)
                marker = {
                    "schema": WORKSPACE_MARKER_SCHEMA,
                    "workspace_id": binding.workspace_id,
                    "backend_id": self.backend_id,
                    "owner_epoch": binding.owner_epoch,
                    "binding_revision": binding.binding_revision,
                    "location_token": binding.location.root_token,
                    "mounts": [
                        {
                            "mount_id": item.mount_id,
                            "kind": item.kind.value,
                            "relative_root": item.relative_root,
                            "access": item.access.value,
                        }
                        for item in layout.mounts
                    ],
                    "created_at": utc_now(),
                }
                atomic_write_json(root / ".zyra-workspace.json", marker)
                return root
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise

    def open(self, binding: WorkspaceBinding) -> Path:
        self._require_enabled()
        self._require_local_binding(binding)
        root = self.physical_root(binding)
        marker = self._read_marker(root)
        self._validate_marker(binding, marker)
        return root

    def access_handle(
        self,
        binding: WorkspaceBinding,
        lease: WorkspaceLease,
        *,
        fence_token: str,
    ) -> WorkspaceAccessHandle:
        root = self.open(binding)
        self.validate_lease(binding, lease, fence_token=fence_token)
        mounts = self.binding_store.get_mounts(binding.workspace_id)
        return WorkspaceAccessHandle(
            workspace_id=binding.workspace_id,
            task_id=binding.task_id,
            session_id=binding.session_id,
            worker_id=lease.worker_id,
            lease_id=lease.lease_id,
            owner_epoch=binding.owner_epoch,
            capability_revision=binding.capability_revision,
            operations=lease.operations,
            backend_id=self.backend_id,
            mount_kinds=tuple(item.kind for item in mounts),
            internal_root=root,
            fence_token=fence_token,
        )

    def validate_lease(
        self,
        binding: WorkspaceBinding,
        lease: WorkspaceLease,
        *,
        fence_token: str,
        operation: WorkspaceOperation | None = None,
    ) -> None:
        if lease.workspace_id != binding.workspace_id or lease.task_id != binding.task_id:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_OWNER_MISMATCH,
                "The workspace lease does not belong to the requested binding.",
                workspace_id=binding.workspace_id,
                operation=operation.value if operation else "validate_lease",
            )
        if lease.lease_id != binding.lease_id:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_OWNER_MISMATCH,
                "The workspace lease is no longer the binding's active lease.",
                workspace_id=binding.workspace_id,
                operation=operation.value if operation else "validate_lease",
            )
        if lease.state is not WorkspaceLeaseState.ACTIVE:
            code = (
                WorkspaceErrorCode.LEASE_EXPIRED
                if lease.state is WorkspaceLeaseState.EXPIRED
                else WorkspaceErrorCode.LEASE_REVOKED
            )
            raise WorkspaceError(
                code,
                "The workspace lease is not active.",
                workspace_id=binding.workspace_id,
                operation=operation.value if operation else "validate_lease",
                actual=lease.state.value,
            )
        if lease.expires_at:
            try:
                expiration = datetime.fromisoformat(lease.expires_at)
                if expiration.tzinfo is None:
                    expiration = expiration.replace(tzinfo=UTC)
                if expiration <= datetime.now(UTC):
                    raise WorkspaceError(
                        WorkspaceErrorCode.LEASE_EXPIRED,
                        "The workspace lease has expired.",
                        workspace_id=binding.workspace_id,
                        operation=operation.value if operation else "validate_lease",
                    )
            except ValueError as error:
                raise WorkspaceError(
                    WorkspaceErrorCode.STORE_CORRUPT,
                    "The workspace lease expiry timestamp is invalid.",
                    workspace_id=binding.workspace_id,
                    operation="validate_lease",
                ) from error
        if lease.owner_epoch != binding.owner_epoch:
            raise WorkspaceError(
                WorkspaceErrorCode.OWNER_EPOCH_STALE,
                "The workspace lease owner epoch is stale.",
                workspace_id=binding.workspace_id,
                operation=operation.value if operation else "validate_lease",
                expected=binding.owner_epoch,
                actual=lease.owner_epoch,
            )
        if lease.capability_revision != binding.capability_revision:
            raise WorkspaceError(
                WorkspaceErrorCode.CAPABILITY_STALE,
                "The workspace lease capability revision is stale.",
                workspace_id=binding.workspace_id,
                operation=operation.value if operation else "validate_lease",
            )
        if not secrets.compare_digest(binding.fence_token_hash, _fence_hash(fence_token)):
            raise WorkspaceError(
                WorkspaceErrorCode.FENCE_TOKEN_MISMATCH,
                "The workspace fencing token does not match the canonical binding.",
                workspace_id=binding.workspace_id,
                operation=operation.value if operation else "validate_lease",
            )
        if operation is not None and not lease.allows(operation):
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_REVOKED,
                "The workspace lease does not allow the requested operation.",
                workspace_id=binding.workspace_id,
                operation=operation.value,
            )
        if binding.lifecycle_state in {
            WorkspaceLifecycleState.CLEANING,
            WorkspaceLifecycleState.CLOSED,
            WorkspaceLifecycleState.DELETED,
            WorkspaceLifecycleState.CORRUPT,
            WorkspaceLifecycleState.RECOVERY_REQUIRED,
        }:
            raise WorkspaceError(
                WorkspaceErrorCode.BINDING_REVOKED,
                "The workspace lifecycle state rejects new lease operations.",
                workspace_id=binding.workspace_id,
                operation=operation.value if operation else "validate_lease",
                actual=binding.lifecycle_state.value,
            )
        if operation in {WorkspaceOperation.WRITE, WorkspaceOperation.DELETE} and binding.lifecycle_state in {
            WorkspaceLifecycleState.WRITE_FROZEN,
            WorkspaceLifecycleState.SNAPSHOTTING,
            WorkspaceLifecycleState.RESTORING,
        }:
            raise WorkspaceError(
                WorkspaceErrorCode.WRITE_FROZEN,
                "Workspace mutation is frozen during the current lifecycle phase.",
                workspace_id=binding.workspace_id,
                operation=operation.value,
                actual=binding.lifecycle_state.value,
            )

    def read(
        self,
        handle: WorkspaceAccessHandle,
        *,
        mount_kind: WorkspaceKind,
        path: str,
        mode: WorkspaceReadMode = WorkspaceReadMode.FULL,
        start: int = 0,
        length: int | None = None,
    ) -> WorkspaceReadResult:
        binding, lease = self._authorize(handle, WorkspaceOperation.READ)
        mount = self._mount(binding.workspace_id, mount_kind)
        root = self._mount_root(binding, mount)
        resolved = self.path_policy.resolve(
            root=root,
            logical_path=path,
            operation=WorkspaceOperation.READ,
            mount=mount,
            require_exists=False,
        )
        return self.file_state.read(
            workspace_id=binding.workspace_id,
            path=self._record_path(mount, resolved.logical_path),
            physical_path=resolved.physical_path,
            owner_epoch=binding.owner_epoch,
            lease_id=lease.lease_id,
            mode=mode,
            start=start,
            length=length,
            max_bytes=binding.quota.max_single_file_bytes,
            metadata={"mount_kind": mount.kind.value},
        )

    def write(
        self,
        handle: WorkspaceAccessHandle,
        *,
        mount_kind: WorkspaceKind,
        path: str,
        content: bytes,
        precondition: FileWritePrecondition,
        service: str = "worker",
    ) -> WorkspaceWriteResult:
        binding, lease = self._authorize(handle, WorkspaceOperation.WRITE)
        mount = self._mount(binding.workspace_id, mount_kind)
        self.mount_policy.authorize_service_operation(mount, WorkspaceOperation.WRITE, service=service)
        if len(content) > min(binding.quota.max_single_file_bytes, self.capabilities.max_single_write_bytes):
            raise WorkspaceError(
                WorkspaceErrorCode.SINGLE_FILE_LIMIT_EXCEEDED,
                "Workspace write content exceeds the active single-write limit.",
                workspace_id=binding.workspace_id,
                operation="write_file",
                path=path,
                expected=min(binding.quota.max_single_file_bytes, self.capabilities.max_single_write_bytes),
                actual=len(content),
            )
        root = self._mount_root(binding, mount)
        canonical, parts = self.path_policy.validate_logical_path(path, quota=mount.quota)
        if not parts:
            raise WorkspaceError(
                WorkspaceErrorCode.PATH_TRAVERSAL,
                "Workspace writes require a file path below the mount root.",
                workspace_id=binding.workspace_id,
                operation="write_file",
                path=path,
            )
        record_path = self._record_path(mount, canonical)
        if precondition.workspace_id != binding.workspace_id or precondition.path != record_path:
            raise WorkspaceError(
                WorkspaceErrorCode.READ_REQUIRED,
                "The workspace write precondition belongs to a different target.",
                workspace_id=binding.workspace_id,
                operation="write_file",
                path=canonical,
            )
        with self._locks.acquire_many((binding.workspace_id, f"path:{binding.workspace_id}:{record_path}")):
            missing_directories = self._missing_parent_count(root, parts[:-1])
            current_usage = self.binding_store.get_usage(binding.workspace_id)
            if current_usage.directory_count + missing_directories > binding.quota.max_directories:
                raise WorkspaceError(
                    WorkspaceErrorCode.QUOTA_EXCEEDED,
                    "Workspace parent creation would exceed the directory-count quota.",
                    workspace_id=binding.workspace_id,
                    operation="write_file",
                    path=canonical,
                    expected=binding.quota.max_directories,
                    actual=current_usage.directory_count + missing_directories,
                )
            self._ensure_parent(root, parts[:-1])
            resolved = self.path_policy.resolve(
                root=root,
                logical_path=canonical,
                operation=WorkspaceOperation.WRITE,
                mount=mount,
                require_exists=False,
            )
            validation = self.file_state.validate_write(precondition, physical_path=resolved.physical_path)
            validation.require_valid(workspace_id=binding.workspace_id, path=record_path)
            prior_bytes = validation.current_size if validation.path_exists else 0
            reservation = self.quota_runtime.reserve(
                workspace_id=binding.workspace_id,
                lease_id=lease.lease_id,
                owner_epoch=binding.owner_epoch,
                path=record_path,
                requested_bytes=max(0, len(content) - prior_bytes),
                creates_file=not validation.path_exists,
                metadata={"mount_kind": mount.kind.value},
            )
            try:
                self.path_policy.revalidate(resolved, root=root)
                atomic_write_bytes(
                    resolved.physical_path,
                    bytes(content),
                    expected_parent_identity=resolved.parent_identity,
                )
                committed = self.file_state.commit_write(
                    workspace_id=binding.workspace_id,
                    path=record_path,
                    physical_path=resolved.physical_path,
                    owner_epoch=binding.owner_epoch,
                    lease_id=lease.lease_id,
                    metadata={"mount_kind": mount.kind.value, "service": service},
                )
                self.quota_runtime.settle(
                    reservation.reservation_id,
                    actual_bytes=len(content),
                    prior_bytes=prior_bytes,
                    file_created=not validation.path_exists,
                )
                self.quota_runtime.reconcile_usage(
                    binding.workspace_id,
                    self.scan_usage(binding),
                )
                return WorkspaceWriteResult(
                    workspace_id=binding.workspace_id,
                    mount_kind=mount.kind,
                    path=canonical,
                    bytes_written=len(content),
                    prior_bytes=prior_bytes,
                    created=not validation.path_exists,
                    content_hash=committed.base_hash,
                    read_epoch=committed.read_epoch,
                    owner_epoch=binding.owner_epoch,
                )
            except Exception:
                try:
                    self.quota_runtime.abort(reservation.reservation_id, reason="write_failed")
                except WorkspaceError:
                    pass
                raise

    def write_after_read(
        self,
        handle: WorkspaceAccessHandle,
        *,
        mount_kind: WorkspaceKind,
        path: str,
        content: bytes,
        service: str = "worker",
    ) -> WorkspaceWriteResult:
        binding, lease = self._authorize(handle, WorkspaceOperation.WRITE)
        mount = self._mount(binding.workspace_id, mount_kind)
        canonical, _ = self.path_policy.validate_logical_path(path, quota=mount.quota)
        record_path = self._record_path(mount, canonical)
        record = self.binding_store.get_read_record(binding.workspace_id, record_path)
        if record is None:
            raise WorkspaceError(
                WorkspaceErrorCode.READ_REQUIRED,
                "Workspace write requires a prior full read of the same target.",
                workspace_id=binding.workspace_id,
                operation="write_file",
                path=canonical,
            )
        precondition = self.file_state.precondition_for_latest(
            workspace_id=binding.workspace_id,
            path=record_path,
            owner_epoch=binding.owner_epoch,
            lease_id=lease.lease_id,
            require_full_read=True,
            allow_create=record.file_identity == "absent",
            expected_absent=record.file_identity == "absent",
        )
        return self.write(
            handle,
            mount_kind=mount_kind,
            path=canonical,
            content=content,
            precondition=precondition,
            service=service,
        )

    def list_directory(
        self,
        handle: WorkspaceAccessHandle,
        *,
        mount_kind: WorkspaceKind,
        path: str = ".",
        maximum_entries: int = 10_000,
    ) -> tuple[WorkspaceDirectoryEntry, ...]:
        binding, _lease = self._authorize(handle, WorkspaceOperation.LIST)
        mount = self._mount(binding.workspace_id, mount_kind)
        root = self._mount_root(binding, mount)
        resolved = self.path_policy.resolve(
            root=root,
            logical_path=path,
            operation=WorkspaceOperation.LIST,
            mount=mount,
            require_exists=True,
            allow_root=True,
        )
        if not resolved.physical_path.is_dir() or resolved.physical_path.is_symlink():
            raise WorkspaceError(
                WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                "Workspace list targets must be real directories.",
                workspace_id=binding.workspace_id,
                operation="list_directory",
                path=resolved.logical_path,
            )
        entries: list[WorkspaceDirectoryEntry] = []
        with os.scandir(resolved.physical_path) as iterator:
            for entry in iterator:
                if len(entries) >= maximum_entries:
                    raise WorkspaceError(
                        WorkspaceErrorCode.QUOTA_EXCEEDED,
                        "Workspace directory listing exceeded its bounded result count.",
                        workspace_id=binding.workspace_id,
                        operation="list_directory",
                        path=resolved.logical_path,
                        expected=maximum_entries,
                    )
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    kind = "symlink"
                elif stat.S_ISDIR(info.st_mode):
                    kind = "directory"
                elif stat.S_ISREG(info.st_mode):
                    kind = "file"
                else:
                    kind = "special"
                logical = entry.name if resolved.logical_path == "." else f"{resolved.logical_path}/{entry.name}"
                entries.append(
                    WorkspaceDirectoryEntry(
                        name=entry.name,
                        path=logical,
                        kind=kind,
                        size_bytes=int(info.st_size) if kind == "file" else 0,
                        mtime_ns=int(info.st_mtime_ns),
                        identity=file_identity(entry.path),
                        writable=mount.access.value == "read_write",
                    )
                )
        return tuple(sorted(entries, key=lambda item: (item.kind != "directory", item.name.casefold(), item.name)))

    def delete(
        self,
        handle: WorkspaceAccessHandle,
        *,
        mount_kind: WorkspaceKind,
        path: str,
        recursive: bool = False,
    ) -> WorkspaceDeleteResult:
        binding, _lease = self._authorize(handle, WorkspaceOperation.DELETE)
        mount = self._mount(binding.workspace_id, mount_kind)
        root = self._mount_root(binding, mount)
        resolved = self.path_policy.resolve(
            root=root,
            logical_path=path,
            operation=WorkspaceOperation.DELETE,
            mount=mount,
            require_exists=True,
        )
        with self._locks.acquire_many((binding.workspace_id, f"path:{binding.workspace_id}:{path}")):
            self.path_policy.revalidate(resolved, root=root)
            files, directories, total_bytes = self._measure_tree(resolved.physical_path)
            if resolved.physical_path.is_dir():
                if not recursive:
                    resolved.physical_path.rmdir()
                else:
                    self._safe_remove_tree(resolved.physical_path, root=root)
            else:
                resolved.physical_path.unlink()
            self.file_state.invalidate(binding.workspace_id, paths=(self._record_path(mount, resolved.logical_path),))
            usage = self.scan_usage(binding)
            self.quota_runtime.reconcile_usage(binding.workspace_id, usage)
            return WorkspaceDeleteResult(
                workspace_id=binding.workspace_id,
                mount_kind=mount.kind,
                path=resolved.logical_path,
                removed_files=files,
                removed_directories=directories,
                removed_bytes=total_bytes,
            )

    def scan_usage(self, binding: WorkspaceBinding) -> WorkspaceUsage:
        root = self.open(binding)
        used_bytes = 0
        file_count = 0
        directory_count = 0
        symlink_count = 0
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in directories:
                path = current_path / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    symlink_count += 1
                else:
                    safe_directories.append(name)
                    directory_count += 1
            directories[:] = safe_directories
            for name in files:
                if current_path == root and name == ".zyra-workspace.json":
                    continue
                path = current_path / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    symlink_count += 1
                elif stat.S_ISREG(info.st_mode):
                    file_count += 1
                    used_bytes += int(info.st_size)
        return WorkspaceUsage(
            used_bytes=used_bytes,
            file_count=file_count,
            directory_count=directory_count,
            symlink_count=symlink_count,
        )

    def archive_and_cleanup_root(
        self,
        binding: WorkspaceBinding,
        *,
        archived_snapshot_id: str,
    ) -> str:
        self._require_enabled()
        if not archived_snapshot_id:
            raise WorkspaceError(
                WorkspaceErrorCode.CLEANUP_CONFLICT,
                "Local workspace cleanup requires an archive snapshot receipt.",
                workspace_id=binding.workspace_id,
                operation="cleanup_workspace",
            )
        root = self.open(binding)
        marker = self._read_marker(root)
        self._validate_marker(binding, marker)
        quarantine = self.data_root / ".cleanup" / f"{binding.workspace_id}-{secrets.token_hex(6)}"
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        with self._locks.acquire_many((binding.workspace_id,)):
            self._validate_marker(binding, self._read_marker(root))
            os.replace(root, quarantine)
            try:
                self._safe_remove_tree(quarantine, root=quarantine)
            except Exception as error:
                raise WorkspaceError(
                    WorkspaceErrorCode.CLEANUP_FAILED,
                    "The local workspace was quarantined but could not be fully removed.",
                    workspace_id=binding.workspace_id,
                    operation="cleanup_workspace",
                    retryable=True,
                    metadata={"quarantine_ref": quarantine.name, "exception_type": type(error).__name__},
                ) from error
        return quarantine.name

    def physical_root(self, binding: WorkspaceBinding) -> Path:
        self._require_local_binding(binding)
        location = binding.location
        assert isinstance(location, LocalWorkspaceLocation)
        candidate = self.data_root.joinpath(*Path(location.relative_root).parts).resolve()
        try:
            candidate.relative_to(self.data_root)
        except ValueError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                "The local workspace binding escaped the backend data root.",
                workspace_id=binding.workspace_id,
                operation="resolve_workspace_root",
            ) from error
        return candidate

    def mount_root(self, binding: WorkspaceBinding, kind: WorkspaceKind) -> Path:
        mount = self._mount(binding.workspace_id, kind)
        return self._mount_root(binding, mount)

    def _authorize(
        self,
        handle: WorkspaceAccessHandle,
        operation: WorkspaceOperation,
    ) -> tuple[WorkspaceBinding, WorkspaceLease]:
        binding = self.binding_store.require_binding(handle.workspace_id)
        lease = self.binding_store.get_lease(handle.lease_id)
        if lease is None:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_NOT_FOUND,
                "The workspace access handle references a missing lease.",
                workspace_id=handle.workspace_id,
                operation=operation.value,
            )
        if handle.owner_epoch != binding.owner_epoch or handle.capability_revision != binding.capability_revision:
            raise WorkspaceError(
                WorkspaceErrorCode.OWNER_EPOCH_STALE,
                "The workspace access handle is stale.",
                workspace_id=handle.workspace_id,
                operation=operation.value,
            )
        self.validate_lease(binding, lease, fence_token=handle.fence_token, operation=operation)
        if not handle.allows(operation):
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_REVOKED,
                "The workspace access handle does not allow this operation.",
                workspace_id=handle.workspace_id,
                operation=operation.value,
            )
        return binding, lease

    def _mount(self, workspace_id: str, kind: WorkspaceKind) -> WorkspaceMount:
        mounts = self.binding_store.get_mounts(workspace_id)
        for mount in mounts:
            if mount.kind is kind:
                return mount
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_FOUND,
            "The workspace mount kind is not available.",
            workspace_id=workspace_id,
            operation="resolve_mount",
            actual=kind.value,
        )

    def _mount_root(self, binding: WorkspaceBinding, mount: WorkspaceMount) -> Path:
        root = self.open(binding)
        candidate = root.joinpath(*Path(mount.relative_root).parts).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "The workspace mount escaped its binding root.",
                workspace_id=binding.workspace_id,
                operation="resolve_mount",
            ) from error
        if not candidate.is_dir() or candidate.is_symlink():
            raise WorkspaceError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "The workspace mount root is missing or unsafe.",
                workspace_id=binding.workspace_id,
                operation="resolve_mount",
                actual=mount.kind.value,
            )
        return candidate

    def _ensure_parent(self, root: Path, parts: tuple[str, ...]) -> None:
        current = root
        for part in parts:
            current = current / part
            if current.exists() or current.is_symlink():
                info = current.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise WorkspaceError(
                        WorkspaceErrorCode.SYMLINK_ESCAPE,
                        "Workspace parent components must be real directories.",
                        operation="create_parent",
                        path=str(part),
                    )
            else:
                current.mkdir(mode=0o700)

    @staticmethod
    def _missing_parent_count(root: Path, parts: tuple[str, ...]) -> int:
        current = root
        missing = 0
        for part in parts:
            current = current / part
            if not current.exists():
                missing += 1
        return missing

    def _read_marker(self, root: Path) -> dict[str, Any]:
        marker_path = root / ".zyra-workspace.json"
        try:
            return read_json_object(marker_path)
        except FileNotFoundError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.BINDING_REVOKED,
                "The local workspace ownership marker is missing.",
                operation="open_workspace",
            ) from error

    def _validate_marker(self, binding: WorkspaceBinding, marker: Mapping[str, Any]) -> None:
        expected = {
            "schema": WORKSPACE_MARKER_SCHEMA,
            "workspace_id": binding.workspace_id,
            "backend_id": self.backend_id,
            "location_token": binding.location.root_token,
        }
        mismatches = {
            key: {"expected": value, "actual": marker.get(key)}
            for key, value in expected.items()
            if marker.get(key) != value
        }
        if mismatches:
            raise WorkspaceError(
                WorkspaceErrorCode.BINDING_REVOKED,
                "The local workspace ownership marker does not match its binding.",
                workspace_id=binding.workspace_id,
                operation="open_workspace",
                metadata={"mismatches": mismatches},
            )

    def _require_local_binding(self, binding: WorkspaceBinding) -> None:
        if binding.backend_id != self.backend_id or not isinstance(binding.location, LocalWorkspaceLocation):
            raise WorkspaceError(
                WorkspaceErrorCode.BACKEND_LOCATION_MISMATCH,
                "The workspace binding does not belong to this local backend.",
                workspace_id=binding.workspace_id,
                operation="local_backend",
            )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise WorkspaceError(
                WorkspaceErrorCode.DISABLED,
                "The local workspace backend is disabled; no fallback root will be used.",
                operation="local_backend",
            )

    @staticmethod
    def _record_path(mount: WorkspaceMount, logical_path: str) -> str:
        return mount.relative_root if logical_path == "." else f"{mount.relative_root}/{logical_path}"

    @staticmethod
    def _measure_tree(path: Path) -> tuple[int, int, int]:
        if path.is_file():
            return 1, 0, int(path.stat().st_size)
        files = 0
        directories = 1
        total = 0
        for current, children, names in os.walk(path, followlinks=False):
            directories += len(children)
            for name in names:
                info = (Path(current) / name).lstat()
                if stat.S_ISREG(info.st_mode):
                    files += 1
                    total += int(info.st_size)
        return files, directories, total

    @staticmethod
    def _safe_remove_tree(path: Path, *, root: Path) -> None:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
        if resolved_path != resolved_root:
            try:
                resolved_path.relative_to(resolved_root)
            except ValueError as error:
                raise WorkspaceError(
                    WorkspaceErrorCode.CLEANUP_CONFLICT,
                    "Workspace cleanup attempted to escape its verified root.",
                    operation="remove_workspace_tree",
                ) from error
        for current, directories, files in os.walk(resolved_path, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in files:
                target = current_path / name
                target.unlink()
            for name in directories:
                target = current_path / name
                if target.is_symlink():
                    target.unlink()
                else:
                    target.rmdir()
        resolved_path.rmdir()


def _fence_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
