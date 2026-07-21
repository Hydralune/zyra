from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class WorkerPoolErrorCode(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"
    WORKER_NOT_FOUND = "worker_not_found"
    WORKER_ALREADY_EXISTS = "worker_already_exists"
    INVALID_WORKER_TRANSITION = "invalid_worker_transition"
    CAPABILITY_MISMATCH = "capability_mismatch"
    ATTESTATION_REJECTED = "attestation_rejected"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    LOGICAL_TASK_CONFLICT = "logical_task_conflict"
    ATTEMPT_NOT_FOUND = "attempt_not_found"
    ATTEMPT_CONFLICT = "attempt_conflict"
    LEASE_NOT_FOUND = "lease_not_found"
    LEASE_CONFLICT = "lease_conflict"
    LEASE_EXPIRED = "lease_expired"
    LEASE_FENCED = "lease_fenced"
    OWNER_MISMATCH = "owner_mismatch"
    DRAINING = "draining"
    CANCELLED = "cancelled"
    INBOX_NOT_FOUND = "inbox_not_found"
    INBOX_CLAIM_CONFLICT = "inbox_claim_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    STORE_CONFLICT = "store_conflict"
    STORE_CORRUPTION = "store_corruption"
    EDGE_CONNECTOR_DISABLED = "edge_connector_disabled"
    EDGE_PROTOCOL_ERROR = "edge_protocol_error"
    EDGE_AUTHENTICATION_FAILED = "edge_authentication_failed"
    EDGE_UNAVAILABLE = "edge_unavailable"
    EDGE_TIMEOUT = "edge_timeout"
    EXECUTION_REJECTED = "execution_rejected"


@dataclass(frozen=True, slots=True)
class WorkerPoolErrorDetail:
    code: WorkerPoolErrorCode
    message: str
    operation: str
    retryable: bool = False
    worker_id: str = ""
    task_id: str = ""
    attempt_id: str = ""
    lease_id: str = ""
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "operation": self.operation,
            "retryable": self.retryable,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "lease_id": self.lease_id,
            "metadata": dict(self.metadata or {}),
        }


class WorkerPoolError(RuntimeError):
    def __init__(
        self,
        code: WorkerPoolErrorCode,
        message: str,
        *,
        operation: str,
        retryable: bool = False,
        worker_id: str = "",
        task_id: str = "",
        attempt_id: str = "",
        lease_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.detail = WorkerPoolErrorDetail(
            code=code,
            message=message,
            operation=operation,
            retryable=retryable,
            worker_id=worker_id,
            task_id=task_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            metadata=metadata,
        )
        super().__init__(f"{code.value}: {message}")

    @property
    def code(self) -> WorkerPoolErrorCode:
        return self.detail.code

    def to_dict(self) -> dict[str, Any]:
        return self.detail.to_dict()


class WorkerNotFound(WorkerPoolError):
    def __init__(self, worker_id: str, *, operation: str) -> None:
        super().__init__(
            WorkerPoolErrorCode.WORKER_NOT_FOUND,
            "worker is not registered",
            operation=operation,
            worker_id=worker_id,
        )


class LeaseFenced(WorkerPoolError):
    def __init__(
        self,
        lease_id: str,
        *,
        operation: str,
        worker_id: str = "",
        attempt_id: str = "",
        reason: str = "lease fence rejected the caller",
    ) -> None:
        super().__init__(
            WorkerPoolErrorCode.LEASE_FENCED,
            reason,
            operation=operation,
            worker_id=worker_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
        )


class StoreConflict(WorkerPoolError):
    def __init__(
        self,
        message: str,
        *,
        operation: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            WorkerPoolErrorCode.STORE_CONFLICT,
            message,
            operation=operation,
            retryable=True,
            metadata=metadata,
        )
