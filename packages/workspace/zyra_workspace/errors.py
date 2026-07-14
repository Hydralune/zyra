from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class WorkspaceErrorCode(StrEnum):
    INVALID_ARGUMENT = "workspace_invalid_argument"
    INVALID_IDENTIFIER = "workspace_invalid_identifier"
    INVALID_LOCATION = "workspace_invalid_location"
    BACKEND_LOCATION_MISMATCH = "workspace_backend_location_mismatch"
    PATH_OUTSIDE_ROOT = "workspace_path_outside_root"
    PATH_TRAVERSAL = "workspace_path_traversal"
    ABSOLUTE_PATH_REJECTED = "workspace_absolute_path_rejected"
    UNC_PATH_REJECTED = "workspace_unc_path_rejected"
    DEVICE_PATH_REJECTED = "workspace_device_path_rejected"
    RESERVED_NAME_REJECTED = "workspace_reserved_name_rejected"
    SYMLINK_ESCAPE = "workspace_symlink_escape"
    REPARSE_POINT_ESCAPE = "workspace_reparse_point_escape"
    MOUNT_BOUNDARY_VIOLATION = "workspace_mount_boundary_violation"
    SUBMISSION_BOUNDARY_VIOLATION = "workspace_submission_boundary_violation"
    NOT_FOUND = "workspace_not_found"
    ALREADY_EXISTS = "workspace_already_exists"
    DISABLED = "workspace_backend_disabled"
    BACKEND_UNAVAILABLE = "workspace_backend_unavailable"
    BINDING_STALE = "workspace_binding_stale"
    BINDING_REVOKED = "workspace_binding_revoked"
    CAPABILITY_STALE = "workspace_capability_revision_stale"
    LEASE_NOT_FOUND = "workspace_lease_not_found"
    LEASE_EXPIRED = "workspace_lease_expired"
    LEASE_REVOKED = "workspace_lease_revoked"
    LEASE_OWNER_MISMATCH = "workspace_lease_owner_mismatch"
    FENCE_TOKEN_MISMATCH = "workspace_fence_token_mismatch"
    OWNER_EPOCH_STALE = "workspace_owner_epoch_stale"
    WRITE_FROZEN = "workspace_write_frozen"
    QUOTA_EXCEEDED = "workspace_quota_exceeded"
    FILE_COUNT_EXCEEDED = "workspace_file_count_exceeded"
    SINGLE_FILE_LIMIT_EXCEEDED = "workspace_single_file_limit_exceeded"
    RESERVATION_NOT_FOUND = "workspace_quota_reservation_not_found"
    RESERVATION_EXPIRED = "workspace_quota_reservation_expired"
    READ_REQUIRED = "workspace_read_before_write_required"
    READ_INCOMPLETE = "workspace_full_read_required"
    READ_EPOCH_STALE = "workspace_read_epoch_stale"
    BASE_HASH_STALE = "workspace_base_hash_stale"
    BASE_MTIME_STALE = "workspace_base_mtime_stale"
    FILE_IDENTITY_CHANGED = "workspace_file_identity_changed"
    SNAPSHOT_NOT_FOUND = "workspace_snapshot_not_found"
    SNAPSHOT_CORRUPT = "workspace_snapshot_corrupt"
    SNAPSHOT_INCOMPLETE = "workspace_snapshot_incomplete"
    SNAPSHOT_OWNER_MISMATCH = "workspace_snapshot_owner_mismatch"
    RESTORE_CONFLICT = "workspace_restore_conflict"
    RESTORE_FAILED = "workspace_restore_failed"
    CLEANUP_CONFLICT = "workspace_cleanup_conflict"
    CLEANUP_FAILED = "workspace_cleanup_failed"
    DIRTY_STATE_CONFLICT = "workspace_dirty_state_conflict"
    NESTED_REPOSITORY_CONFLICT = "workspace_nested_repository_conflict"
    GIT_NOT_AVAILABLE = "workspace_git_not_available"
    GIT_QUERY_REJECTED = "workspace_git_query_rejected"
    GIT_QUERY_FAILED = "workspace_git_query_failed"
    STORE_CORRUPT = "workspace_store_corrupt"
    STORE_REVISION_CONFLICT = "workspace_store_revision_conflict"
    STORE_IO_FAILURE = "workspace_store_io_failure"
    IDEMPOTENCY_CONFLICT = "workspace_idempotency_conflict"
    OPERATION_IN_PROGRESS = "workspace_operation_in_progress"
    INVALID_TRANSITION = "workspace_invalid_transition"
    RECOVERY_REQUIRED = "workspace_recovery_required"
    INTERNAL = "workspace_internal_error"


@dataclass(frozen=True, slots=True)
class WorkspaceErrorDetail:
    code: WorkspaceErrorCode
    message: str
    retryable: bool = False
    workspace_id: str = ""
    operation: str = ""
    path: str = ""
    expected: Any = None
    actual: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "workspace_id": self.workspace_id,
            "operation": self.operation,
            "path": self.path,
            "expected": self.expected,
            "actual": self.actual,
            "metadata": dict(self.metadata),
        }


class WorkspaceError(RuntimeError):
    def __init__(
        self,
        code: WorkspaceErrorCode,
        message: str,
        *,
        retryable: bool = False,
        workspace_id: str = "",
        operation: str = "",
        path: str = "",
        expected: Any = None,
        actual: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = WorkspaceErrorDetail(
            code=code,
            message=message,
            retryable=retryable,
            workspace_id=workspace_id,
            operation=operation,
            path=path,
            expected=expected,
            actual=actual,
            metadata=dict(metadata or {}),
        )

    @property
    def code(self) -> WorkspaceErrorCode:
        return self.detail.code

    @property
    def retryable(self) -> bool:
        return self.detail.retryable

    def to_dict(self) -> dict[str, Any]:
        return self.detail.to_dict()


class WorkspacePathError(WorkspaceError):
    pass


class WorkspaceBindingError(WorkspaceError):
    pass


class WorkspaceLeaseError(WorkspaceError):
    pass


class WorkspaceQuotaError(WorkspaceError):
    pass


class WorkspaceSnapshotError(WorkspaceError):
    pass


class WorkspaceStoreError(WorkspaceError):
    pass


class WorkspaceGitError(WorkspaceError):
    pass


def error_from_exception(
    error: BaseException,
    *,
    workspace_id: str = "",
    operation: str = "",
) -> WorkspaceError:
    if isinstance(error, WorkspaceError):
        return error
    if isinstance(error, FileNotFoundError):
        return WorkspaceError(
            WorkspaceErrorCode.NOT_FOUND,
            "The requested workspace resource was not found.",
            workspace_id=workspace_id,
            operation=operation,
            path=str(getattr(error, "filename", "") or ""),
        )
    if isinstance(error, PermissionError):
        return WorkspaceError(
            WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
            "The operating system denied access to the workspace resource.",
            workspace_id=workspace_id,
            operation=operation,
            path=str(getattr(error, "filename", "") or ""),
        )
    if isinstance(error, OSError):
        return WorkspaceError(
            WorkspaceErrorCode.STORE_IO_FAILURE,
            "The workspace operation failed at the filesystem boundary.",
            retryable=True,
            workspace_id=workspace_id,
            operation=operation,
            path=str(getattr(error, "filename", "") or ""),
            metadata={"errno": getattr(error, "errno", None), "exception_type": type(error).__name__},
        )
    return WorkspaceError(
        WorkspaceErrorCode.INTERNAL,
        "The workspace operation failed.",
        workspace_id=workspace_id,
        operation=operation,
        metadata={"exception_type": type(error).__name__},
    )
