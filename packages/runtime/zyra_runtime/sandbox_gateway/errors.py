from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class GatewayErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    INVALID_STATE = "invalid_state"
    STATE_CONFLICT = "state_conflict"
    STATE_CORRUPT = "state_corrupt"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_NOT_READY = "session_not_ready"
    SESSION_CLOSED = "session_closed"
    SESSION_FENCED = "session_fenced"
    LEASE_EXPIRED = "lease_expired"
    QUEUE_FULL = "queue_full"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_MISMATCH = "approval_mismatch"
    APPROVAL_REPLAY = "approval_replay"
    APPROVAL_EXPIRED = "approval_expired"
    PERMISSION_UNAVAILABLE = "permission_unavailable"
    COMMAND_MUTATED = "command_mutated"
    PATH_INVALID = "path_invalid"
    PATH_ESCAPE = "path_escape"
    PATH_REPLACED = "path_replaced"
    LINK_REJECTED = "link_rejected"
    WORKSPACE_STALE = "workspace_stale"
    WORKSPACE_GATEWAY_UNAVAILABLE = "workspace_gateway_unavailable"
    DIRECT_WORKSPACE_WRITE = "direct_workspace_write"
    NETWORK_DENIED = "network_denied"
    CREDENTIAL_DENIED = "credential_denied"
    CREDENTIAL_REPLAY = "credential_replay"
    UNTRUSTED_CONTROL_WRITE = "untrusted_control_write"
    ARCHIVE_INVALID = "archive_invalid"
    ARCHIVE_TRAVERSAL = "archive_traversal"
    ARCHIVE_BOMB = "archive_bomb"
    FILE_TOO_LARGE = "file_too_large"
    CONTENT_MISMATCH = "content_mismatch"
    HASHLINE_MISMATCH = "hashline_mismatch"
    QUARANTINED = "quarantined"
    PROCESS_START_FAILED = "process_start_failed"
    PROCESS_TIMEOUT = "process_timeout"
    PROCESS_CANCELLED = "process_cancelled"
    PROCESS_OUTPUT_LIMIT = "process_output_limit"
    PROCESS_TREE_LEAK = "process_tree_leak"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BACKEND_PROTOCOL = "backend_protocol"
    ARTIFACT_WRITE_FAILED = "artifact_write_failed"
    PATCH_REJECTED = "patch_rejected"
    PATCH_CONFLICT = "patch_conflict"
    EVENT_SINK_FAILED = "event_sink_failed"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class GatewayErrorDetail:
    code: GatewayErrorCode
    message: str
    operation: str = ""
    retryable: bool = False
    recovery: tuple[str, ...] = ()
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "operation": self.operation,
            "retryable": self.retryable,
            "recovery": list(self.recovery),
            "metadata": dict(self.metadata or {}),
        }


class SandboxGatewayError(RuntimeError):
    def __init__(
        self,
        code: GatewayErrorCode,
        message: str,
        *,
        operation: str = "",
        retryable: bool = False,
        recovery: tuple[str, ...] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = GatewayErrorDetail(
            code=code,
            message=message,
            operation=operation,
            retryable=retryable,
            recovery=recovery,
            metadata=metadata,
        )

    @property
    def code(self) -> GatewayErrorCode:
        return self.detail.code

    @property
    def operation(self) -> str:
        return self.detail.operation

    @property
    def retryable(self) -> bool:
        return self.detail.retryable

    def to_dict(self) -> dict[str, Any]:
        return self.detail.to_dict()


def fail(
    code: GatewayErrorCode,
    message: str,
    *,
    operation: str = "",
    retryable: bool = False,
    recovery: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
) -> "NoReturn":
    raise SandboxGatewayError(
        code,
        message,
        operation=operation,
        retryable=retryable,
        recovery=recovery,
        metadata=metadata,
    )


try:
    from typing import NoReturn
except ImportError:  # pragma: no cover
    NoReturn = Any  # type: ignore[misc,assignment]
