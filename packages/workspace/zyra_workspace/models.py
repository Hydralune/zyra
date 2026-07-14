from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence, TypeAlias

from .errors import WorkspaceError, WorkspaceErrorCode


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_workspace_id(prefix: str = "ws") -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def require_identifier(name: str, value: str) -> str:
    text = str(value or "").strip()
    if not IDENTIFIER_PATTERN.fullmatch(text):
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_IDENTIFIER,
            f"{name} is not a valid workspace identifier.",
            operation="validate_identifier",
            actual=text,
            metadata={"field": name},
        )
    return text


class WorkspaceBackendKind(StrEnum):
    LOCAL = "local"
    CONTAINER = "container"
    EDGE_MOUNT = "edge_mount"
    CLOUD_OBJECT = "cloud_object"


class WorkspaceKind(StrEnum):
    TASK = "task"
    ARTIFACT = "artifact"
    DOWNLOAD = "download"
    TEMP = "temp"


class WorkspaceLifecycleState(StrEnum):
    REQUESTED = "requested"
    CREATING = "creating"
    READY = "ready"
    OPEN = "open"
    WRITE_FROZEN = "write_frozen"
    SNAPSHOTTING = "snapshotting"
    RESTORING = "restoring"
    CLEANING = "cleaning"
    CLOSED = "closed"
    CORRUPT = "corrupt"
    RECOVERY_REQUIRED = "recovery_required"
    DELETED = "deleted"


class WorkspaceLeaseState(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    EXPIRED = "expired"
    REVOKED = "revoked"
    RELEASED = "released"


class WorkspaceOperation(StrEnum):
    CREATE = "create"
    OPEN = "open"
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    LIST = "list"
    MOUNT = "mount"
    SNAPSHOT = "snapshot"
    RESTORE = "restore"
    CLEANUP = "cleanup"
    REBIND = "rebind"
    GIT_QUERY = "git_query"
    QUOTA_QUERY = "quota_query"


class WorkspaceMountAccess(StrEnum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"
    APPEND_ONLY = "append_only"


class WorkspaceReadMode(StrEnum):
    FULL = "full"
    RANGE = "range"
    METADATA = "metadata"


class WorkspaceDirtyOwner(StrEnum):
    USER_BASELINE = "user_baseline"
    AGENT_PATCH = "agent_patch"
    OTHER_WORKER = "other_worker"
    GENERATED_ARTIFACT = "generated_artifact"
    UNKNOWN = "unknown"


class WorkspaceDirtyKind(StrEnum):
    MODIFIED = "modified"
    ADDED = "added"
    DELETED = "deleted"
    RENAMED = "renamed"
    UNTRACKED = "untracked"
    IGNORED = "ignored"
    CONFLICTED = "conflicted"
    SUBMODULE = "submodule"


class SnapshotState(StrEnum):
    PREPARING = "preparing"
    COMMITTED = "committed"
    CORRUPT = "corrupt"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class SnapshotEntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


class RecoveryState(StrEnum):
    DETECTED = "detected"
    PLANNED = "planned"
    STARTED = "started"
    RESTORED = "restored"
    ROLLED_BACK = "rolled_back"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class IsolationCapability(StrEnum):
    DIRECTORY_COPY = "directory_copy"
    REFLINK = "reflink"
    BLOCK_CLONE = "block_clone"
    FILESYSTEM_SNAPSHOT = "filesystem_snapshot"
    GIT_WORKTREE = "git_worktree"
    OVERLAY = "overlay"


@dataclass(frozen=True, slots=True)
class LocalWorkspaceLocation:
    relative_root: str
    root_token: str = "workspace-data"
    platform: str = ""
    kind: WorkspaceBackendKind = field(default=WorkspaceBackendKind.LOCAL, init=False)

    def __post_init__(self) -> None:
        relative = str(self.relative_root or "").replace("\\", "/").strip("/")
        if not relative or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_LOCATION,
                "Local workspace locations must be root-relative and traversal-free.",
                operation="validate_location",
                actual=self.relative_root,
            )
        object.__setattr__(self, "relative_root", relative)
        object.__setattr__(self, "root_token", require_identifier("root_token", self.root_token))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "relative_root": self.relative_root,
            "root_token": self.root_token,
            "platform": self.platform,
        }


@dataclass(frozen=True, slots=True)
class ContainerWorkspaceLocation:
    container_id: str
    mount_id: str
    container_path: str
    runtime_id: str = ""
    kind: WorkspaceBackendKind = field(default=WorkspaceBackendKind.CONTAINER, init=False)

    def __post_init__(self) -> None:
        require_identifier("container_id", self.container_id)
        require_identifier("mount_id", self.mount_id)
        if not str(self.container_path).startswith("/") or ".." in PurePosixPath(self.container_path).parts:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_LOCATION,
                "Container workspace locations require an absolute traversal-free container path.",
                operation="validate_location",
                actual=self.container_path,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "container_id": self.container_id,
            "mount_id": self.mount_id,
            "container_path": self.container_path,
            "runtime_id": self.runtime_id,
        }


@dataclass(frozen=True, slots=True)
class EdgeMountWorkspaceLocation:
    edge_node_id: str
    mount_id: str
    opaque_path_ref: str
    transport: str = "gateway"
    kind: WorkspaceBackendKind = field(default=WorkspaceBackendKind.EDGE_MOUNT, init=False)

    def __post_init__(self) -> None:
        require_identifier("edge_node_id", self.edge_node_id)
        require_identifier("mount_id", self.mount_id)
        require_identifier("opaque_path_ref", self.opaque_path_ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "edge_node_id": self.edge_node_id,
            "mount_id": self.mount_id,
            "opaque_path_ref": self.opaque_path_ref,
            "transport": self.transport,
        }


@dataclass(frozen=True, slots=True)
class CloudObjectWorkspaceLocation:
    provider_id: str
    bucket_ref: str
    prefix_ref: str
    region: str = ""
    versioning: bool = True
    kind: WorkspaceBackendKind = field(default=WorkspaceBackendKind.CLOUD_OBJECT, init=False)

    def __post_init__(self) -> None:
        require_identifier("provider_id", self.provider_id)
        require_identifier("bucket_ref", self.bucket_ref)
        prefix = str(self.prefix_ref or "").strip("/")
        if not prefix or ".." in PurePosixPath(prefix).parts:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_LOCATION,
                "Cloud workspace prefixes must be non-empty and traversal-free.",
                operation="validate_location",
                actual=self.prefix_ref,
            )
        object.__setattr__(self, "prefix_ref", prefix)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "provider_id": self.provider_id,
            "bucket_ref": self.bucket_ref,
            "prefix_ref": self.prefix_ref,
            "region": self.region,
            "versioning": self.versioning,
        }


WorkspaceLocation: TypeAlias = (
    LocalWorkspaceLocation
    | ContainerWorkspaceLocation
    | EdgeMountWorkspaceLocation
    | CloudObjectWorkspaceLocation
)


def workspace_location_from_dict(value: Mapping[str, Any]) -> WorkspaceLocation:
    kind = WorkspaceBackendKind(str(value.get("kind") or ""))
    if kind is WorkspaceBackendKind.LOCAL:
        return LocalWorkspaceLocation(
            relative_root=str(value.get("relative_root") or ""),
            root_token=str(value.get("root_token") or "workspace-data"),
            platform=str(value.get("platform") or ""),
        )
    if kind is WorkspaceBackendKind.CONTAINER:
        return ContainerWorkspaceLocation(
            container_id=str(value.get("container_id") or ""),
            mount_id=str(value.get("mount_id") or ""),
            container_path=str(value.get("container_path") or ""),
            runtime_id=str(value.get("runtime_id") or ""),
        )
    if kind is WorkspaceBackendKind.EDGE_MOUNT:
        return EdgeMountWorkspaceLocation(
            edge_node_id=str(value.get("edge_node_id") or ""),
            mount_id=str(value.get("mount_id") or ""),
            opaque_path_ref=str(value.get("opaque_path_ref") or ""),
            transport=str(value.get("transport") or "gateway"),
        )
    return CloudObjectWorkspaceLocation(
        provider_id=str(value.get("provider_id") or ""),
        bucket_ref=str(value.get("bucket_ref") or ""),
        prefix_ref=str(value.get("prefix_ref") or ""),
        region=str(value.get("region") or ""),
        versioning=bool(value.get("versioning", True)),
    )


@dataclass(frozen=True, slots=True)
class WorkspaceCapabilities:
    revision: int = 1
    readable: bool = True
    writable: bool = True
    snapshot: bool = True
    restore: bool = True
    cleanup: bool = True
    atomic_replace: bool = True
    random_access: bool = True
    symlink_support: bool = False
    git_read_only: bool = True
    isolation: tuple[IsolationCapability, ...] = (IsolationCapability.DIRECTORY_COPY,)
    max_path_bytes: int = 4096
    max_single_write_bytes: int = 128 * 1024 * 1024
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace capability revision must be positive.",
                operation="validate_capabilities",
            )
        if self.max_path_bytes < 256 or self.max_single_write_bytes < 1:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace capability limits are invalid.",
                operation="validate_capabilities",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "readable": self.readable,
            "writable": self.writable,
            "snapshot": self.snapshot,
            "restore": self.restore,
            "cleanup": self.cleanup,
            "atomic_replace": self.atomic_replace,
            "random_access": self.random_access,
            "symlink_support": self.symlink_support,
            "git_read_only": self.git_read_only,
            "isolation": [item.value for item in self.isolation],
            "max_path_bytes": self.max_path_bytes,
            "max_single_write_bytes": self.max_single_write_bytes,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceQuota:
    max_bytes: int = 1024 * 1024 * 1024
    max_files: int = 100_000
    max_directories: int = 20_000
    max_single_file_bytes: int = 128 * 1024 * 1024
    max_path_depth: int = 64
    max_snapshots: int = 32
    max_snapshot_bytes: int = 4 * 1024 * 1024 * 1024
    include_generated: bool = True

    def __post_init__(self) -> None:
        numeric = (
            self.max_bytes,
            self.max_files,
            self.max_directories,
            self.max_single_file_bytes,
            self.max_path_depth,
            self.max_snapshots,
            self.max_snapshot_bytes,
        )
        if any(item < 1 for item in numeric):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace quota values must be positive.",
                operation="validate_quota",
            )
        if self.max_single_file_bytes > self.max_bytes:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "The single-file quota cannot exceed the workspace byte quota.",
                operation="validate_quota",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_bytes": self.max_bytes,
            "max_files": self.max_files,
            "max_directories": self.max_directories,
            "max_single_file_bytes": self.max_single_file_bytes,
            "max_path_depth": self.max_path_depth,
            "max_snapshots": self.max_snapshots,
            "max_snapshot_bytes": self.max_snapshot_bytes,
            "include_generated": self.include_generated,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceUsage:
    used_bytes: int = 0
    file_count: int = 0
    directory_count: int = 0
    symlink_count: int = 0
    reserved_bytes: int = 0
    snapshot_bytes: int = 0
    snapshot_count: int = 0
    scanned_at: str = field(default_factory=utc_now)
    revision: int = 0

    @property
    def accounted_bytes(self) -> int:
        return self.used_bytes + self.reserved_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_bytes": self.used_bytes,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "symlink_count": self.symlink_count,
            "reserved_bytes": self.reserved_bytes,
            "snapshot_bytes": self.snapshot_bytes,
            "snapshot_count": self.snapshot_count,
            "scanned_at": self.scanned_at,
            "revision": self.revision,
            "accounted_bytes": self.accounted_bytes,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    workspace_id: str
    run_id: str
    task_id: str
    session_id: str
    backend_id: str
    backend_kind: WorkspaceBackendKind
    workspace_kind: WorkspaceKind
    location: WorkspaceLocation
    capability_revision: int
    lease_id: str
    owner_epoch: int
    fence_token_hash: str
    lifecycle_state: WorkspaceLifecycleState
    quota: WorkspaceQuota = field(default_factory=WorkspaceQuota)
    binding_revision: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    deadline_at: str = ""
    active_snapshot_id: str = ""
    recovery_record_id: str = ""
    write_frozen_reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("workspace_id", "run_id", "task_id", "session_id", "backend_id", "lease_id"):
            require_identifier(name, getattr(self, name))
        if self.backend_kind is not self.location.kind:
            raise WorkspaceError(
                WorkspaceErrorCode.BACKEND_LOCATION_MISMATCH,
                "Workspace backend kind does not match its typed location.",
                workspace_id=self.workspace_id,
                operation="validate_binding",
                expected=self.backend_kind.value,
                actual=self.location.kind.value,
            )
        if self.capability_revision < 1 or self.owner_epoch < 1 or self.binding_revision < 1:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace binding revisions and owner epoch must be positive.",
                workspace_id=self.workspace_id,
                operation="validate_binding",
            )
        if not SHA256_PATTERN.fullmatch(self.fence_token_hash):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace fence token hashes must be full SHA-256 digests.",
                workspace_id=self.workspace_id,
                operation="validate_binding",
            )

    def with_state(self, state: WorkspaceLifecycleState, **updates: Any) -> "WorkspaceBinding":
        return replace(
            self,
            lifecycle_state=state,
            binding_revision=self.binding_revision + 1,
            updated_at=utc_now(),
            **updates,
        )

    def to_dict(self, *, include_location: bool = True) -> dict[str, Any]:
        result = {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "backend_id": self.backend_id,
            "backend_kind": self.backend_kind.value,
            "workspace_kind": self.workspace_kind.value,
            "capability_revision": self.capability_revision,
            "lease_id": self.lease_id,
            "owner_epoch": self.owner_epoch,
            "fence_token_hash": self.fence_token_hash,
            "lifecycle_state": self.lifecycle_state.value,
            "quota": self.quota.to_dict(),
            "binding_revision": self.binding_revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deadline_at": self.deadline_at,
            "active_snapshot_id": self.active_snapshot_id,
            "recovery_record_id": self.recovery_record_id,
            "write_frozen_reason": self.write_frozen_reason,
            "metadata": dict(self.metadata),
        }
        if include_location:
            result["location"] = self.location.to_dict()
        else:
            result["location"] = {
                "kind": self.location.kind.value,
                "opaque": True,
            }
        return result


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    lease_id: str
    workspace_id: str
    run_id: str
    task_id: str
    session_id: str
    worker_id: str
    owner_epoch: int
    fence_token_hash: str
    capability_revision: int
    state: WorkspaceLeaseState = WorkspaceLeaseState.ACTIVE
    issued_at: str = field(default_factory=utc_now)
    renewed_at: str = field(default_factory=utc_now)
    expires_at: str = ""
    operations: tuple[WorkspaceOperation, ...] = (
        WorkspaceOperation.READ,
        WorkspaceOperation.WRITE,
        WorkspaceOperation.LIST,
        WorkspaceOperation.SNAPSHOT,
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("lease_id", "workspace_id", "run_id", "task_id", "session_id", "worker_id"):
            require_identifier(name, getattr(self, name))
        if self.owner_epoch < 1 or self.capability_revision < 1:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace lease revisions must be positive.",
                workspace_id=self.workspace_id,
                operation="validate_lease",
            )
        if not SHA256_PATTERN.fullmatch(self.fence_token_hash):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace lease fence token hash is invalid.",
                workspace_id=self.workspace_id,
                operation="validate_lease",
            )

    def allows(self, operation: WorkspaceOperation) -> bool:
        return self.state is WorkspaceLeaseState.ACTIVE and operation in self.operations

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_id": self.worker_id,
            "owner_epoch": self.owner_epoch,
            "fence_token_hash": self.fence_token_hash,
            "capability_revision": self.capability_revision,
            "state": self.state.value,
            "issued_at": self.issued_at,
            "renewed_at": self.renewed_at,
            "expires_at": self.expires_at,
            "operations": [item.value for item in self.operations],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceMount:
    mount_id: str
    workspace_id: str
    kind: WorkspaceKind
    relative_root: str
    access: WorkspaceMountAccess
    owner_epoch: int
    created_at: str = field(default_factory=utc_now)
    quota: WorkspaceQuota = field(default_factory=WorkspaceQuota)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier("mount_id", self.mount_id)
        require_identifier("workspace_id", self.workspace_id)
        path = str(self.relative_root or "").replace("\\", "/").strip("/")
        if not path or ".." in PurePosixPath(path).parts:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace mount roots must be traversal-free relative paths.",
                workspace_id=self.workspace_id,
                operation="validate_mount",
                actual=self.relative_root,
            )
        object.__setattr__(self, "relative_root", path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mount_id": self.mount_id,
            "workspace_id": self.workspace_id,
            "kind": self.kind.value,
            "relative_root": self.relative_root,
            "access": self.access.value,
            "owner_epoch": self.owner_epoch,
            "created_at": self.created_at,
            "quota": self.quota.to_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FileReadRecord:
    workspace_id: str
    path: str
    read_epoch: int
    mode: WorkspaceReadMode
    base_hash: str
    base_mtime_ns: int
    base_size: int
    file_identity: str
    ranges: tuple[tuple[int, int], ...] = ()
    fully_read: bool = False
    owner_epoch: int = 1
    lease_id: str = ""
    recorded_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier("workspace_id", self.workspace_id)
        if self.read_epoch < 1 or self.owner_epoch < 1:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "File read epochs must be positive.",
                workspace_id=self.workspace_id,
                operation="validate_read_record",
                path=self.path,
            )
        if self.base_hash and not SHA256_PATTERN.fullmatch(self.base_hash):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "File read base hashes must be full SHA-256 digests.",
                workspace_id=self.workspace_id,
                operation="validate_read_record",
                path=self.path,
            )

    def covers(self, start: int, end: int) -> bool:
        if self.fully_read:
            return True
        return any(left <= start and right >= end for left, right in self.ranges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "path": self.path,
            "read_epoch": self.read_epoch,
            "mode": self.mode.value,
            "base_hash": self.base_hash,
            "base_mtime_ns": self.base_mtime_ns,
            "base_size": self.base_size,
            "file_identity": self.file_identity,
            "ranges": [list(item) for item in self.ranges],
            "fully_read": self.fully_read,
            "owner_epoch": self.owner_epoch,
            "lease_id": self.lease_id,
            "recorded_at": self.recorded_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FileWritePrecondition:
    workspace_id: str
    path: str
    expected_read_epoch: int
    expected_base_hash: str
    expected_base_mtime_ns: int
    expected_file_identity: str
    expected_owner_epoch: int
    lease_id: str
    require_full_read: bool = True
    allow_create: bool = False
    expected_absent: bool = False
    requested_ranges: tuple[tuple[int, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "path": self.path,
            "expected_read_epoch": self.expected_read_epoch,
            "expected_base_hash": self.expected_base_hash,
            "expected_base_mtime_ns": self.expected_base_mtime_ns,
            "expected_file_identity": self.expected_file_identity,
            "expected_owner_epoch": self.expected_owner_epoch,
            "lease_id": self.lease_id,
            "require_full_read": self.require_full_read,
            "allow_create": self.allow_create,
            "expected_absent": self.expected_absent,
            "requested_ranges": [list(item) for item in self.requested_ranges],
        }


@dataclass(frozen=True, slots=True)
class DirtyPathRecord:
    workspace_id: str
    path: str
    kind: WorkspaceDirtyKind
    owner: WorkspaceDirtyOwner
    baseline_hash: str = ""
    current_hash: str = ""
    worker_id: str = ""
    operation_id: str = ""
    detected_at: str = field(default_factory=utc_now)
    nested_repository_id: str = ""
    generated_reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "path": self.path,
            "kind": self.kind.value,
            "owner": self.owner.value,
            "baseline_hash": self.baseline_hash,
            "current_hash": self.current_hash,
            "worker_id": self.worker_id,
            "operation_id": self.operation_id,
            "detected_at": self.detected_at,
            "nested_repository_id": self.nested_repository_id,
            "generated_reason": self.generated_reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class NestedRepositoryRef:
    repository_id: str
    relative_root: str
    git_dir_ref: str
    head_ref: str
    head_commit: str
    baseline_tree: str
    dirty: bool
    parent_repository_id: str = ""
    discovered_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier("repository_id", self.repository_id)
        path = str(self.relative_root or ".").replace("\\", "/")
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Nested repository roots must remain workspace relative.",
                operation="validate_nested_repository",
                path=path,
            )
        object.__setattr__(self, "relative_root", path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "relative_root": self.relative_root,
            "git_dir_ref": self.git_dir_ref,
            "head_ref": self.head_ref,
            "head_commit": self.head_commit,
            "baseline_tree": self.baseline_tree,
            "dirty": self.dirty,
            "parent_repository_id": self.parent_repository_id,
            "discovered_at": self.discovered_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceBaseline:
    baseline_id: str
    workspace_id: str
    owner_epoch: int
    root_tree_hash: str
    repository_refs: tuple[NestedRepositoryRef, ...]
    dirty_paths: tuple[DirtyPathRecord, ...]
    captured_at: str = field(default_factory=utc_now)
    synthetic_tree_ref: str = ""
    snapshot_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_id": self.baseline_id,
            "workspace_id": self.workspace_id,
            "owner_epoch": self.owner_epoch,
            "root_tree_hash": self.root_tree_hash,
            "repository_refs": [item.to_dict() for item in self.repository_refs],
            "dirty_paths": [item.to_dict() for item in self.dirty_paths],
            "captured_at": self.captured_at,
            "synthetic_tree_ref": self.synthetic_tree_ref,
            "snapshot_id": self.snapshot_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    relative_path: str
    kind: SnapshotEntryKind
    content_hash: str
    size_bytes: int
    mtime_ns: int
    mode: int
    blob_ref: str = ""
    link_target: str = ""
    file_identity: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        path = str(self.relative_path or ".").replace("\\", "/")
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Snapshot entries must use workspace-relative paths.",
                operation="validate_snapshot_entry",
                path=path,
            )
        object.__setattr__(self, "relative_path", path)
        if self.content_hash and not SHA256_PATTERN.fullmatch(self.content_hash):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Snapshot content hashes must be full SHA-256 digests.",
                operation="validate_snapshot_entry",
                path=path,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "kind": self.kind.value,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "mode": self.mode,
            "blob_ref": self.blob_ref,
            "link_target": self.link_target,
            "file_identity": self.file_identity,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    snapshot_id: str
    workspace_id: str
    owner_epoch: int
    lease_id: str
    state: SnapshotState
    manifest_hash: str
    root_tree_hash: str
    entries: tuple[SnapshotEntry, ...]
    parent_snapshot_id: str = ""
    baseline_id: str = ""
    created_at: str = field(default_factory=utc_now)
    committed_at: str = ""
    total_bytes: int = 0
    file_count: int = 0
    directory_count: int = 0
    dirty_paths: tuple[DirtyPathRecord, ...] = ()
    repository_refs: tuple[NestedRepositoryRef, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier("snapshot_id", self.snapshot_id)
        require_identifier("workspace_id", self.workspace_id)
        if self.owner_epoch < 1:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Snapshot owner epoch must be positive.",
                workspace_id=self.workspace_id,
                operation="validate_snapshot",
            )
        if self.manifest_hash and not SHA256_PATTERN.fullmatch(self.manifest_hash):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Snapshot manifests require a full SHA-256 digest.",
                workspace_id=self.workspace_id,
                operation="validate_snapshot",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "workspace_id": self.workspace_id,
            "owner_epoch": self.owner_epoch,
            "lease_id": self.lease_id,
            "state": self.state.value,
            "manifest_hash": self.manifest_hash,
            "root_tree_hash": self.root_tree_hash,
            "entries": [item.to_dict() for item in self.entries],
            "parent_snapshot_id": self.parent_snapshot_id,
            "baseline_id": self.baseline_id,
            "created_at": self.created_at,
            "committed_at": self.committed_at,
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "dirty_paths": [item.to_dict() for item in self.dirty_paths],
            "repository_refs": [item.to_dict() for item in self.repository_refs],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceRecoveryRecord:
    recovery_id: str
    workspace_id: str
    state: RecoveryState
    reason: str
    source_snapshot_id: str = ""
    rollback_snapshot_id: str = ""
    owner_epoch_before: int = 0
    owner_epoch_after: int = 0
    detected_at: str = field(default_factory=utc_now)
    started_at: str = ""
    completed_at: str = ""
    conflict_paths: tuple[str, ...] = ()
    quarantine_ref: str = ""
    outcome: str = ""
    retryable: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovery_id": self.recovery_id,
            "workspace_id": self.workspace_id,
            "state": self.state.value,
            "reason": self.reason,
            "source_snapshot_id": self.source_snapshot_id,
            "rollback_snapshot_id": self.rollback_snapshot_id,
            "owner_epoch_before": self.owner_epoch_before,
            "owner_epoch_after": self.owner_epoch_after,
            "detected_at": self.detected_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "conflict_paths": list(self.conflict_paths),
            "quarantine_ref": self.quarantine_ref,
            "outcome": self.outcome,
            "retryable": self.retryable,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceOperationReceipt:
    receipt_id: str
    workspace_id: str
    operation: WorkspaceOperation
    ok: bool
    owner_epoch: int
    binding_revision: int
    lease_id: str
    started_at: str
    completed_at: str
    causation_id: str = ""
    idempotency_key: str = ""
    snapshot_id: str = ""
    paths: tuple[str, ...] = ()
    bytes_changed: int = 0
    state_before: str = ""
    state_after: str = ""
    error_code: str = ""
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "workspace_id": self.workspace_id,
            "operation": self.operation.value,
            "ok": self.ok,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "lease_id": self.lease_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
            "snapshot_id": self.snapshot_id,
            "paths": list(self.paths),
            "bytes_changed": self.bytes_changed,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "error_code": self.error_code,
            "message": self.message,
            "metadata": dict(self.metadata),
        }


def quota_from_dict(value: Mapping[str, Any]) -> WorkspaceQuota:
    defaults = WorkspaceQuota()
    return WorkspaceQuota(
        max_bytes=int(value.get("max_bytes", defaults.max_bytes)),
        max_files=int(value.get("max_files", defaults.max_files)),
        max_directories=int(value.get("max_directories", defaults.max_directories)),
        max_single_file_bytes=int(value.get("max_single_file_bytes", defaults.max_single_file_bytes)),
        max_path_depth=int(value.get("max_path_depth", defaults.max_path_depth)),
        max_snapshots=int(value.get("max_snapshots", defaults.max_snapshots)),
        max_snapshot_bytes=int(value.get("max_snapshot_bytes", defaults.max_snapshot_bytes)),
        include_generated=bool(value.get("include_generated", defaults.include_generated)),
    )


def binding_from_dict(value: Mapping[str, Any]) -> WorkspaceBinding:
    return WorkspaceBinding(
        workspace_id=str(value.get("workspace_id") or ""),
        run_id=str(value.get("run_id") or ""),
        task_id=str(value.get("task_id") or ""),
        session_id=str(value.get("session_id") or ""),
        backend_id=str(value.get("backend_id") or ""),
        backend_kind=WorkspaceBackendKind(str(value.get("backend_kind") or "local")),
        workspace_kind=WorkspaceKind(str(value.get("workspace_kind") or "task")),
        location=workspace_location_from_dict(_mapping(value.get("location"))),
        capability_revision=int(value.get("capability_revision") or 1),
        lease_id=str(value.get("lease_id") or ""),
        owner_epoch=int(value.get("owner_epoch") or 1),
        fence_token_hash=str(value.get("fence_token_hash") or ""),
        lifecycle_state=WorkspaceLifecycleState(str(value.get("lifecycle_state") or "requested")),
        quota=quota_from_dict(_mapping(value.get("quota"))),
        binding_revision=int(value.get("binding_revision") or 1),
        created_at=str(value.get("created_at") or utc_now()),
        updated_at=str(value.get("updated_at") or utc_now()),
        deadline_at=str(value.get("deadline_at") or ""),
        active_snapshot_id=str(value.get("active_snapshot_id") or ""),
        recovery_record_id=str(value.get("recovery_record_id") or ""),
        write_frozen_reason=str(value.get("write_frozen_reason") or ""),
        metadata=dict(_mapping(value.get("metadata"))),
    )


def lease_from_dict(value: Mapping[str, Any]) -> WorkspaceLease:
    operation_values = _sequence(value.get("operations"))
    default_operations = WorkspaceLease.__dataclass_fields__["operations"].default
    return WorkspaceLease(
        lease_id=str(value.get("lease_id") or ""),
        workspace_id=str(value.get("workspace_id") or ""),
        run_id=str(value.get("run_id") or ""),
        task_id=str(value.get("task_id") or ""),
        session_id=str(value.get("session_id") or ""),
        worker_id=str(value.get("worker_id") or ""),
        owner_epoch=int(value.get("owner_epoch") or 1),
        fence_token_hash=str(value.get("fence_token_hash") or ""),
        capability_revision=int(value.get("capability_revision") or 1),
        state=WorkspaceLeaseState(str(value.get("state") or "active")),
        issued_at=str(value.get("issued_at") or utc_now()),
        renewed_at=str(value.get("renewed_at") or utc_now()),
        expires_at=str(value.get("expires_at") or ""),
        operations=(
            tuple(WorkspaceOperation(str(item)) for item in operation_values)
            if operation_values
            else default_operations
        ),
        metadata=dict(_mapping(value.get("metadata"))),
    )


def mount_from_dict(value: Mapping[str, Any]) -> WorkspaceMount:
    return WorkspaceMount(
        mount_id=str(value.get("mount_id") or ""),
        workspace_id=str(value.get("workspace_id") or ""),
        kind=WorkspaceKind(str(value.get("kind") or "task")),
        relative_root=str(value.get("relative_root") or ""),
        access=WorkspaceMountAccess(str(value.get("access") or "read_write")),
        owner_epoch=int(value.get("owner_epoch") or 1),
        created_at=str(value.get("created_at") or utc_now()),
        quota=quota_from_dict(_mapping(value.get("quota"))),
        metadata=dict(_mapping(value.get("metadata"))),
    )


def read_record_from_dict(value: Mapping[str, Any]) -> FileReadRecord:
    return FileReadRecord(
        workspace_id=str(value.get("workspace_id") or ""),
        path=str(value.get("path") or ""),
        read_epoch=int(value.get("read_epoch") or 1),
        mode=WorkspaceReadMode(str(value.get("mode") or "metadata")),
        base_hash=str(value.get("base_hash") or ""),
        base_mtime_ns=int(value.get("base_mtime_ns") or 0),
        base_size=int(value.get("base_size") or 0),
        file_identity=str(value.get("file_identity") or ""),
        ranges=tuple((int(item[0]), int(item[1])) for item in _sequence(value.get("ranges")) if len(item) == 2),
        fully_read=bool(value.get("fully_read", False)),
        owner_epoch=int(value.get("owner_epoch") or 1),
        lease_id=str(value.get("lease_id") or ""),
        recorded_at=str(value.get("recorded_at") or utc_now()),
        metadata=dict(_mapping(value.get("metadata"))),
    )


def dirty_record_from_dict(value: Mapping[str, Any]) -> DirtyPathRecord:
    return DirtyPathRecord(
        workspace_id=str(value.get("workspace_id") or ""),
        path=str(value.get("path") or ""),
        kind=WorkspaceDirtyKind(str(value.get("kind") or "modified")),
        owner=WorkspaceDirtyOwner(str(value.get("owner") or "unknown")),
        baseline_hash=str(value.get("baseline_hash") or ""),
        current_hash=str(value.get("current_hash") or ""),
        worker_id=str(value.get("worker_id") or ""),
        operation_id=str(value.get("operation_id") or ""),
        detected_at=str(value.get("detected_at") or utc_now()),
        nested_repository_id=str(value.get("nested_repository_id") or ""),
        generated_reason=str(value.get("generated_reason") or ""),
        metadata=dict(_mapping(value.get("metadata"))),
    )


def repository_ref_from_dict(value: Mapping[str, Any]) -> NestedRepositoryRef:
    return NestedRepositoryRef(
        repository_id=str(value.get("repository_id") or ""),
        relative_root=str(value.get("relative_root") or "."),
        git_dir_ref=str(value.get("git_dir_ref") or ""),
        head_ref=str(value.get("head_ref") or ""),
        head_commit=str(value.get("head_commit") or ""),
        baseline_tree=str(value.get("baseline_tree") or ""),
        dirty=bool(value.get("dirty", False)),
        parent_repository_id=str(value.get("parent_repository_id") or ""),
        discovered_at=str(value.get("discovered_at") or utc_now()),
        metadata=dict(_mapping(value.get("metadata"))),
    )


def snapshot_entry_from_dict(value: Mapping[str, Any]) -> SnapshotEntry:
    return SnapshotEntry(
        relative_path=str(value.get("relative_path") or "."),
        kind=SnapshotEntryKind(str(value.get("kind") or "file")),
        content_hash=str(value.get("content_hash") or ""),
        size_bytes=int(value.get("size_bytes") or 0),
        mtime_ns=int(value.get("mtime_ns") or 0),
        mode=int(value.get("mode") or 0),
        blob_ref=str(value.get("blob_ref") or ""),
        link_target=str(value.get("link_target") or ""),
        file_identity=str(value.get("file_identity") or ""),
        metadata=dict(_mapping(value.get("metadata"))),
    )


def snapshot_from_dict(value: Mapping[str, Any]) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        snapshot_id=str(value.get("snapshot_id") or ""),
        workspace_id=str(value.get("workspace_id") or ""),
        owner_epoch=int(value.get("owner_epoch") or 1),
        lease_id=str(value.get("lease_id") or ""),
        state=SnapshotState(str(value.get("state") or "preparing")),
        manifest_hash=str(value.get("manifest_hash") or ""),
        root_tree_hash=str(value.get("root_tree_hash") or ""),
        entries=tuple(snapshot_entry_from_dict(_mapping(item)) for item in _sequence(value.get("entries"))),
        parent_snapshot_id=str(value.get("parent_snapshot_id") or ""),
        baseline_id=str(value.get("baseline_id") or ""),
        created_at=str(value.get("created_at") or utc_now()),
        committed_at=str(value.get("committed_at") or ""),
        total_bytes=int(value.get("total_bytes") or 0),
        file_count=int(value.get("file_count") or 0),
        directory_count=int(value.get("directory_count") or 0),
        dirty_paths=tuple(dirty_record_from_dict(_mapping(item)) for item in _sequence(value.get("dirty_paths"))),
        repository_refs=tuple(repository_ref_from_dict(_mapping(item)) for item in _sequence(value.get("repository_refs"))),
        metadata=dict(_mapping(value.get("metadata"))),
    )


def recovery_from_dict(value: Mapping[str, Any]) -> WorkspaceRecoveryRecord:
    return WorkspaceRecoveryRecord(
        recovery_id=str(value.get("recovery_id") or ""),
        workspace_id=str(value.get("workspace_id") or ""),
        state=RecoveryState(str(value.get("state") or "detected")),
        reason=str(value.get("reason") or ""),
        source_snapshot_id=str(value.get("source_snapshot_id") or ""),
        rollback_snapshot_id=str(value.get("rollback_snapshot_id") or ""),
        owner_epoch_before=int(value.get("owner_epoch_before") or 0),
        owner_epoch_after=int(value.get("owner_epoch_after") or 0),
        detected_at=str(value.get("detected_at") or utc_now()),
        started_at=str(value.get("started_at") or ""),
        completed_at=str(value.get("completed_at") or ""),
        conflict_paths=tuple(str(item) for item in _sequence(value.get("conflict_paths"))),
        quarantine_ref=str(value.get("quarantine_ref") or ""),
        outcome=str(value.get("outcome") or ""),
        retryable=bool(value.get("retryable", False)),
        metadata=dict(_mapping(value.get("metadata"))),
    )


def usage_from_dict(value: Mapping[str, Any]) -> WorkspaceUsage:
    return WorkspaceUsage(
        used_bytes=int(value.get("used_bytes") or 0),
        file_count=int(value.get("file_count") or 0),
        directory_count=int(value.get("directory_count") or 0),
        symlink_count=int(value.get("symlink_count") or 0),
        reserved_bytes=int(value.get("reserved_bytes") or 0),
        snapshot_bytes=int(value.get("snapshot_bytes") or 0),
        snapshot_count=int(value.get("snapshot_count") or 0),
        scanned_at=str(value.get("scanned_at") or utc_now()),
        revision=int(value.get("revision") or 0),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else ()


def combine_ranges(ranges: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    ordered = sorted((max(0, int(left)), max(0, int(right))) for left, right in ranges if int(right) >= int(left))
    combined: list[tuple[int, int]] = []
    for left, right in ordered:
        if not combined or left > combined[-1][1]:
            combined.append((left, right))
            continue
        combined[-1] = (combined[-1][0], max(combined[-1][1], right))
    return tuple(combined)
