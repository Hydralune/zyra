from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from .models import CodeFile, CodeSymbol, SymbolReference, CallEdge, WorkspaceIdentity


class CodeIndexJobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    BUILDING = "building"
    PUBLISHING = "publishing"
    READY = "ready"
    FAILED = "failed"
    STALE = "stale"

    @property
    def terminal(self) -> bool:
        return self in {
            CodeIndexJobState.READY,
            CodeIndexJobState.FAILED,
            CodeIndexJobState.STALE,
        }


class CodeIndexJobOperation(StrEnum):
    REBUILD = "rebuild"
    PATCH = "patch"
    RECONCILE = "reconcile"


class CodeIndexJobError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CodeIndexLeaseUnavailable(CodeIndexJobError):
    pass


class CodeIndexLeaseLost(CodeIndexJobError):
    pass


class CodeIndexPublicationFenced(CodeIndexJobError):
    pass


@dataclass(frozen=True, slots=True)
class CodeIndexBuildJob:
    job_id: str
    workspace_id: str
    task_id: str
    operation: CodeIndexJobOperation
    state: CodeIndexJobState
    generation: int
    source_revision: str
    transaction_id: str = ""
    changed_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    lease_owner: str = ""
    lease_token: str = ""
    lease_epoch: int = 0
    lease_expires_at: float = 0.0
    attempt: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    published_at: float = 0.0
    error_code: str = ""
    error_message: str = ""
    causation_id: str = ""
    idempotency_key: str = ""

    @property
    def fence(self) -> tuple[str, int, str]:
        return self.job_id, self.lease_epoch, self.lease_token

    def to_dict(self, *, include_lease_token: bool = False) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "operation": self.operation.value,
            "state": self.state.value,
            "generation": self.generation,
            "source_revision": self.source_revision,
            "transaction_id": self.transaction_id,
            "changed_paths": list(self.changed_paths),
            "deleted_paths": list(self.deleted_paths),
            "payload": dict(self.payload),
            "lease_owner": self.lease_owner,
            "lease_token": (
                self.lease_token
                if include_lease_token
                else "redacted"
                if self.lease_token
                else ""
            ),
            "lease_epoch": self.lease_epoch,
            "lease_expires_at": self.lease_expires_at,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "published_at": self.published_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class CodeIndexBuildLease:
    job_id: str
    workspace_id: str
    worker_id: str
    token: str
    epoch: int
    generation: int
    source_revision: str
    expires_at: float

    @property
    def fence(self) -> tuple[str, int, str]:
        return self.job_id, self.epoch, self.token

    def to_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "worker_id": self.worker_id,
            "token": self.token if include_token else "redacted",
            "epoch": self.epoch,
            "generation": self.generation,
            "source_revision": self.source_revision,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class CodeIndexBuildCandidate:
    identity: WorkspaceIdentity
    generation: int
    files: tuple[tuple[CodeFile, str], ...]
    symbols: tuple[CodeSymbol, ...]
    references: tuple[SymbolReference, ...]
    calls: tuple[CallEdge, ...]
    content_digest: str
    ignored_count: int
    warnings: tuple[str, ...]
    changed_paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("candidate generation must be positive")
        if not self.content_digest:
            raise ValueError("candidate content digest is required")
        if any(file.workspace_id != self.identity.workspace_id for file, _ in self.files):
            raise ValueError("candidate contains a file from another workspace")

    def summary(self) -> dict[str, Any]:
        return {
            "workspace_id": self.identity.workspace_id,
            "workspace_revision": self.identity.revision,
            "generation": self.generation,
            "file_count": len(self.files),
            "symbol_count": len(self.symbols),
            "reference_count": len(self.references),
            "call_edge_count": len(self.calls),
            "ignored_count": self.ignored_count,
            "content_digest": self.content_digest,
            "changed_paths": list(self.changed_paths),
            "deleted_paths": list(self.deleted_paths),
            "warnings": list(self.warnings),
            "candidate_rows_embedded": False,
        }


@dataclass(frozen=True, slots=True)
class CodeIndexWorkerOutcome:
    status: str
    worker_id: str
    workspace_id: str = ""
    job_id: str = ""
    generation: int = 0
    source_revision: str = ""
    content_digest: str = ""
    file_count: int = 0
    symbol_count: int = 0
    reference_count: int = 0
    call_edge_count: int = 0
    error_code: str = ""
    error_message: str = ""
    fenced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "worker_id": self.worker_id,
            "workspace_id": self.workspace_id,
            "job_id": self.job_id,
            "generation": self.generation,
            "source_revision": self.source_revision,
            "content_digest": self.content_digest,
            "file_count": self.file_count,
            "symbol_count": self.symbol_count,
            "reference_count": self.reference_count,
            "call_edge_count": self.call_edge_count,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "fenced": self.fenced,
        }


@dataclass(frozen=True, slots=True)
class CodeIndexSweepResult:
    stale_job_ids: tuple[str, ...]
    requeued_job_ids: tuple[str, ...]
    generations: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.stale_job_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stale_job_ids": list(self.stale_job_ids),
            "requeued_job_ids": list(self.requeued_job_ids),
            "generations": list(self.generations),
            "count": self.count,
        }


__all__ = [
    "CodeIndexBuildCandidate",
    "CodeIndexBuildJob",
    "CodeIndexBuildLease",
    "CodeIndexJobError",
    "CodeIndexJobOperation",
    "CodeIndexJobState",
    "CodeIndexLeaseLost",
    "CodeIndexLeaseUnavailable",
    "CodeIndexPublicationFenced",
    "CodeIndexSweepResult",
    "CodeIndexWorkerOutcome",
]
