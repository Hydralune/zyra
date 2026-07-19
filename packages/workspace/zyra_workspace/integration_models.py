from __future__ import annotations

"""Typed contracts for the M1-05A workspace integration boundary.

The foundation slice owns workspace bindings, leases, safe paths, snapshots,
dirty ownership and the local backend.  This module deliberately does not
introduce another owner for any of those states.  It describes the durable
operations which *coordinate* the existing owners: tool transactions,
isolated child workspaces, conflict-aware merge, local endpoint rebind and
opaque worker handoff.

All public projections are path-free.  Physical roots are process-private and
must never be serialized through these records.
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from .errors import WorkspaceError, WorkspaceErrorCode
from .models import stable_digest, utc_now


class IntegrationOperation(StrEnum):
    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    DELETE = "delete"
    MKDIR = "mkdir"
    PATCH = "patch"
    SHELL = "shell"
    ARTIFACT_PUBLISH = "artifact_publish"
    ISOLATION_PREPARE = "isolation_prepare"
    ISOLATION_MERGE = "isolation_merge"
    ISOLATION_DISCARD = "isolation_discard"
    REBIND = "rebind"
    RESTORE = "restore"
    RECOVERY = "recovery"
    HANDOFF = "handoff"


class IntegrationPhase(StrEnum):
    REQUESTED = "requested"
    VALIDATING = "validating"
    FROZEN = "frozen"
    STAGING = "staging"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CONFLICTED = "conflicted"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class MutationKind(StrEnum):
    WRITE_TEXT = "write_text"
    WRITE_BYTES = "write_bytes"
    REPLACE_TEXT = "replace_text"
    DELETE_FILE = "delete_file"
    MAKE_DIRECTORY = "make_directory"


class IsolationState(StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    CAPTURED = "captured"
    MERGING = "merging"
    MERGED = "merged"
    CONFLICTED = "conflicted"
    DISCARDING = "discarding"
    DISCARDED = "discarded"
    INTERRUPTED = "interrupted"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class MergeDisposition(StrEnum):
    UNCHANGED = "unchanged"
    CHILD_ONLY = "child_only"
    PARENT_ONLY = "parent_only"
    IDENTICAL_CHANGE = "identical_change"
    CONFLICT = "conflict"
    DELETE_CHILD_ONLY = "delete_child_only"
    DELETE_PARENT_ONLY = "delete_parent_only"
    DELETE_MODIFY_CONFLICT = "delete_modify_conflict"
    TYPE_CONFLICT = "type_conflict"


class RebindState(StrEnum):
    REQUESTED = "requested"
    FROZEN = "frozen"
    SNAPSHOTTED = "snapshotted"
    DIRTY_VALIDATED = "dirty_validated"
    MATERIALIZED = "materialized"
    VERIFIED = "verified"
    COMMITTING = "committing"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class RecoverySeverity(StrEnum):
    RETRYABLE = "retryable"
    BACKEND_UNAVAILABLE_CANDIDATE = "backend_unavailable_candidate"
    RECOVERY_INPUT = "recovery_input"
    QUARANTINE_REQUIRED = "quarantine_required"
    TERMINAL = "terminal"


class TreeEntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


def _require_text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_ARGUMENT,
            f"{name} is required.",
            operation="validate_workspace_integration_contract",
            metadata={"field": name},
        )
    return text


def _require_non_negative(name: str, value: Any) -> int:
    number = int(value)
    if number < 0:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_ARGUMENT,
            f"{name} must be non-negative.",
            operation="validate_workspace_integration_contract",
            actual=number,
            metadata={"field": name},
        )
    return number


def _normalized_logical_path(value: Any, *, allow_root: bool = False) -> str:
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    if allow_root and text in {"", "."}:
        return "."
    if not text:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_ARGUMENT,
            "A logical workspace path is required.",
            operation="validate_workspace_integration_path",
        )
    parts = tuple(part for part in text.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise WorkspaceError(
            WorkspaceErrorCode.PATH_TRAVERSAL,
            "Logical workspace paths must be traversal-free.",
            operation="validate_workspace_integration_path",
            path=text,
        )
    if ":" in parts[0] or text.startswith("//"):
        raise WorkspaceError(
            WorkspaceErrorCode.ABSOLUTE_PATH_REJECTED,
            "Logical workspace paths must be relative.",
            operation="validate_workspace_integration_path",
            path=text,
        )
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class WorkspaceReadEvidence:
    workspace_id: str
    logical_path: str
    owner_epoch: int
    binding_revision: int
    lease_id: str
    content_hash: str
    size: int
    mtime_ns: int
    file_identity: str
    complete: bool
    read_at: str = field(default_factory=utc_now)
    encoding: str = ""
    evidence_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _require_text("workspace_id", self.workspace_id))
        object.__setattr__(self, "logical_path", _normalized_logical_path(self.logical_path))
        object.__setattr__(self, "lease_id", _require_text("lease_id", self.lease_id))
        object.__setattr__(self, "owner_epoch", max(1, int(self.owner_epoch)))
        object.__setattr__(self, "binding_revision", max(1, int(self.binding_revision)))
        object.__setattr__(self, "size", _require_non_negative("size", self.size))
        object.__setattr__(self, "mtime_ns", _require_non_negative("mtime_ns", self.mtime_ns))
        digest = str(self.content_hash or "")
        if len(digest) != 64:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Read evidence requires a full SHA-256 content hash.",
                workspace_id=self.workspace_id,
                operation="validate_read_evidence",
                path=self.logical_path,
            )
        object.__setattr__(self, "content_hash", digest)
        if not self.evidence_id:
            object.__setattr__(self, "evidence_id", stable_digest(self.signing_payload()))

    def signing_payload(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "logical_path": self.logical_path,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "lease_id": self.lease_id,
            "content_hash": self.content_hash,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "file_identity": self.file_identity,
            "complete": self.complete,
            "read_at": self.read_at,
            "encoding": self.encoding,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.signing_payload(), "evidence_id": self.evidence_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkspaceReadEvidence":
        return cls(
            workspace_id=value.get("workspace_id", ""),
            logical_path=value.get("logical_path", ""),
            owner_epoch=value.get("owner_epoch", 0),
            binding_revision=value.get("binding_revision", 0),
            lease_id=value.get("lease_id", ""),
            content_hash=value.get("content_hash", ""),
            size=value.get("size", 0),
            mtime_ns=value.get("mtime_ns", 0),
            file_identity=value.get("file_identity", ""),
            complete=bool(value.get("complete", False)),
            read_at=str(value.get("read_at") or utc_now()),
            encoding=str(value.get("encoding") or ""),
            evidence_id=str(value.get("evidence_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceMutation:
    mutation_id: str
    kind: MutationKind
    logical_path: str
    content: bytes = b""
    encoding: str = "utf-8"
    old_text: str = ""
    new_text: str = ""
    replace_all: bool = False
    read_evidence_id: str = ""
    expected_absent: bool = False
    mode: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mutation_id", _require_text("mutation_id", self.mutation_id))
        object.__setattr__(self, "logical_path", _normalized_logical_path(self.logical_path))
        if self.kind is MutationKind.REPLACE_TEXT and not self.read_evidence_id:
            raise WorkspaceError(
                WorkspaceErrorCode.READ_REQUIRED,
                "Text replacement requires immutable full-read evidence.",
                operation="validate_workspace_mutation",
                path=self.logical_path,
            )
        if self.kind is MutationKind.REPLACE_TEXT and self.old_text == "":
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Text replacement requires a non-empty exact old value.",
                operation="validate_workspace_mutation",
                path=self.logical_path,
            )
        if self.mode is not None and not 0 <= int(self.mode) <= 0o777:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace mutation mode is outside the supported range.",
                operation="validate_workspace_mutation",
                path=self.logical_path,
            )

    @property
    def mutates_bytes(self) -> bool:
        return self.kind in {
            MutationKind.WRITE_TEXT,
            MutationKind.WRITE_BYTES,
            MutationKind.REPLACE_TEXT,
        }

    def content_digest(self) -> str:
        return stable_digest({
            "kind": self.kind.value,
            "content": self.content.hex(),
            "old_text": self.old_text,
            "new_text": self.new_text,
            "replace_all": self.replace_all,
        })

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        result = {
            "mutation_id": self.mutation_id,
            "kind": self.kind.value,
            "logical_path": self.logical_path,
            "encoding": self.encoding,
            "old_text": self.old_text if include_content else "",
            "new_text": self.new_text if include_content else "",
            "replace_all": self.replace_all,
            "read_evidence_id": self.read_evidence_id,
            "expected_absent": self.expected_absent,
            "mode": self.mode,
            "metadata": dict(self.metadata),
            "content_digest": self.content_digest(),
            "content_size": len(self.content),
            "content_persisted": include_content,
        }
        if include_content:
            result["content_hex"] = self.content.hex()
        return result


@dataclass(frozen=True, slots=True)
class WorkspaceMutationPlan:
    transaction_id: str
    workspace_id: str
    owner_epoch: int
    binding_revision: int
    lease_id: str
    worker_id: str
    mutations: tuple[WorkspaceMutation, ...]
    evidence: tuple[WorkspaceReadEvidence, ...] = ()
    idempotency_key: str = ""
    causation_id: str = ""
    preserve_user_dirty: bool = True
    publish_artifact: bool = False
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("transaction_id", "workspace_id", "lease_id", "worker_id"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        object.__setattr__(self, "owner_epoch", max(1, int(self.owner_epoch)))
        object.__setattr__(self, "binding_revision", max(1, int(self.binding_revision)))
        object.__setattr__(self, "mutations", tuple(self.mutations))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.mutations:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "A workspace mutation transaction cannot be empty.",
                workspace_id=self.workspace_id,
                operation="validate_mutation_plan",
            )
        mutation_ids = [item.mutation_id for item in self.mutations]
        if len(mutation_ids) != len(set(mutation_ids)):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace mutation identifiers must be unique inside a transaction.",
                workspace_id=self.workspace_id,
                operation="validate_mutation_plan",
            )
        evidence_ids = {item.evidence_id for item in self.evidence}
        missing = sorted(
            item.read_evidence_id
            for item in self.mutations
            if item.read_evidence_id and item.read_evidence_id not in evidence_ids
        )
        if missing:
            raise WorkspaceError(
                WorkspaceErrorCode.READ_REQUIRED,
                "Workspace mutation references read evidence outside the transaction.",
                workspace_id=self.workspace_id,
                operation="validate_mutation_plan",
                actual=missing,
            )

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def evidence_by_id(self) -> dict[str, WorkspaceReadEvidence]:
        return {item.evidence_id: item for item in self.evidence}

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "workspace_id": self.workspace_id,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "mutations": [item.to_dict() for item in self.mutations],
            "evidence": [item.to_dict() for item in self.evidence],
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "preserve_user_dirty": self.preserve_user_dirty,
            "publish_artifact": self.publish_artifact,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MutationPathResult:
    logical_path: str
    kind: MutationKind
    before_hash: str
    after_hash: str
    bytes_before: int
    bytes_after: int
    disposition: str
    ownership_claimed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_path", _normalized_logical_path(self.logical_path))
        object.__setattr__(self, "bytes_before", _require_non_negative("bytes_before", self.bytes_before))
        object.__setattr__(self, "bytes_after", _require_non_negative("bytes_after", self.bytes_after))

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "kind": self.kind.value,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "disposition": self.disposition,
            "ownership_claimed": self.ownership_claimed,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceTransactionRecord:
    transaction_id: str
    workspace_id: str
    operation: IntegrationOperation
    phase: IntegrationPhase
    owner_epoch_before: int
    owner_epoch_after: int
    binding_revision_before: int
    binding_revision_after: int
    lease_id_before: str
    lease_id_after: str
    plan_digest: str
    path_results: tuple[MutationPathResult, ...] = ()
    snapshot_id: str = ""
    artifact_refs: tuple[str, ...] = ()
    idempotency_key: str = ""
    started_at: str = field(default_factory=utc_now)
    completed_at: str = ""
    error_code: str = ""
    error_type: str = ""
    message: str = ""
    recovery_input_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_id", _require_text("transaction_id", self.transaction_id))
        object.__setattr__(self, "workspace_id", _require_text("workspace_id", self.workspace_id))
        object.__setattr__(self, "owner_epoch_before", max(1, int(self.owner_epoch_before)))
        object.__setattr__(self, "owner_epoch_after", max(0, int(self.owner_epoch_after)))
        object.__setattr__(self, "binding_revision_before", max(1, int(self.binding_revision_before)))
        object.__setattr__(self, "binding_revision_after", max(0, int(self.binding_revision_after)))
        object.__setattr__(self, "path_results", tuple(self.path_results))
        object.__setattr__(self, "artifact_refs", tuple(str(item) for item in self.artifact_refs))
        object.__setattr__(self, "revision", max(1, int(self.revision)))

    @property
    def terminal(self) -> bool:
        return self.phase in {
            IntegrationPhase.COMMITTED,
            IntegrationPhase.ROLLED_BACK,
            IntegrationPhase.CONFLICTED,
            IntegrationPhase.QUARANTINED,
            IntegrationPhase.FAILED,
        }

    @property
    def ok(self) -> bool:
        return self.phase is IntegrationPhase.COMMITTED and not self.error_code

    def advance(self, phase: IntegrationPhase, **updates: Any) -> "WorkspaceTransactionRecord":
        return replace(self, phase=phase, revision=self.revision + 1, **updates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "workspace_id": self.workspace_id,
            "operation": self.operation.value,
            "phase": self.phase.value,
            "owner_epoch_before": self.owner_epoch_before,
            "owner_epoch_after": self.owner_epoch_after,
            "binding_revision_before": self.binding_revision_before,
            "binding_revision_after": self.binding_revision_after,
            "lease_id_before": self.lease_id_before,
            "lease_id_after": self.lease_id_after,
            "plan_digest": self.plan_digest,
            "path_results": [item.to_dict() for item in self.path_results],
            "snapshot_id": self.snapshot_id,
            "artifact_refs": list(self.artifact_refs),
            "idempotency_key": self.idempotency_key,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error_code": self.error_code,
            "error_type": self.error_type,
            "message": self.message,
            "recovery_input_id": self.recovery_input_id,
            "metadata": dict(self.metadata),
            "revision": self.revision,
            "physical_location_redacted": True,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkspaceTransactionRecord":
        return cls(
            transaction_id=value.get("transaction_id", ""),
            workspace_id=value.get("workspace_id", ""),
            operation=IntegrationOperation(str(value.get("operation") or IntegrationOperation.PATCH.value)),
            phase=IntegrationPhase(str(value.get("phase") or IntegrationPhase.REQUESTED.value)),
            owner_epoch_before=value.get("owner_epoch_before", 0),
            owner_epoch_after=value.get("owner_epoch_after", 0),
            binding_revision_before=value.get("binding_revision_before", 0),
            binding_revision_after=value.get("binding_revision_after", 0),
            lease_id_before=str(value.get("lease_id_before") or ""),
            lease_id_after=str(value.get("lease_id_after") or ""),
            plan_digest=str(value.get("plan_digest") or ""),
            path_results=tuple(
                MutationPathResult(
                    logical_path=item.get("logical_path", ""),
                    kind=MutationKind(str(item.get("kind") or MutationKind.WRITE_BYTES.value)),
                    before_hash=str(item.get("before_hash") or ""),
                    after_hash=str(item.get("after_hash") or ""),
                    bytes_before=item.get("bytes_before", 0),
                    bytes_after=item.get("bytes_after", 0),
                    disposition=str(item.get("disposition") or ""),
                    ownership_claimed=bool(item.get("ownership_claimed", False)),
                )
                for item in value.get("path_results") or ()
                if isinstance(item, Mapping)
            ),
            snapshot_id=str(value.get("snapshot_id") or ""),
            artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
            idempotency_key=str(value.get("idempotency_key") or ""),
            started_at=str(value.get("started_at") or utc_now()),
            completed_at=str(value.get("completed_at") or ""),
            error_code=str(value.get("error_code") or ""),
            error_type=str(value.get("error_type") or ""),
            message=str(value.get("message") or ""),
            recovery_input_id=str(value.get("recovery_input_id") or ""),
            metadata=dict(value.get("metadata") or {}),
            revision=value.get("revision", 1),
        )


@dataclass(frozen=True, slots=True)
class TreeEntry:
    logical_path: str
    kind: TreeEntryKind
    content_hash: str = ""
    size: int = 0
    mode: int = 0
    mtime_ns: int = 0
    repository_root: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_path", _normalized_logical_path(self.logical_path, allow_root=True))
        object.__setattr__(self, "size", _require_non_negative("size", self.size))
        object.__setattr__(self, "mtime_ns", _require_non_negative("mtime_ns", self.mtime_ns))
        if self.repository_root:
            object.__setattr__(self, "repository_root", _normalized_logical_path(self.repository_root, allow_root=True))

    @property
    def identity(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "kind": self.kind.value,
            "content_hash": self.content_hash,
            "size": self.size,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "repository_root": self.repository_root,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TreeEntry":
        return cls(
            logical_path=value.get("logical_path", "."),
            kind=TreeEntryKind(str(value.get("kind") or TreeEntryKind.FILE.value)),
            content_hash=str(value.get("content_hash") or ""),
            size=value.get("size", 0),
            mode=value.get("mode", 0),
            mtime_ns=value.get("mtime_ns", 0),
            repository_root=str(value.get("repository_root") or ""),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceTreeManifest:
    manifest_id: str
    workspace_id: str
    owner_epoch: int
    entries: tuple[TreeEntry, ...]
    created_at: str = field(default_factory=utc_now)
    source: str = "workspace"

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _require_text("workspace_id", self.workspace_id))
        object.__setattr__(self, "owner_epoch", max(1, int(self.owner_epoch)))
        ordered = tuple(sorted(tuple(self.entries), key=lambda item: item.logical_path))
        paths = [item.logical_path for item in ordered]
        if len(paths) != len(set(paths)):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace manifests cannot contain duplicate logical paths.",
                workspace_id=self.workspace_id,
                operation="validate_tree_manifest",
            )
        object.__setattr__(self, "entries", ordered)
        if not self.manifest_id:
            object.__setattr__(self, "manifest_id", stable_digest(self.signing_payload()))

    def signing_payload(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "owner_epoch": self.owner_epoch,
            "entries": [item.to_dict() for item in self.entries],
            "created_at": self.created_at,
            "source": self.source,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.signing_payload(), "manifest_id": self.manifest_id}

    def by_path(self) -> dict[str, TreeEntry]:
        return {item.logical_path: item for item in self.entries}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkspaceTreeManifest":
        return cls(
            manifest_id=str(value.get("manifest_id") or ""),
            workspace_id=value.get("workspace_id", ""),
            owner_epoch=value.get("owner_epoch", 0),
            entries=tuple(
                TreeEntry.from_dict(item)
                for item in value.get("entries") or ()
                if isinstance(item, Mapping)
            ),
            created_at=str(value.get("created_at") or utc_now()),
            source=str(value.get("source") or "workspace"),
        )


@dataclass(frozen=True, slots=True)
class MergeConflict:
    conflict_id: str
    isolation_id: str
    workspace_id: str
    logical_path: str
    disposition: MergeDisposition
    baseline_hash: str
    parent_hash: str
    child_hash: str
    repository_root: str = ""
    created_at: str = field(default_factory=utc_now)
    resolved: bool = False
    resolution: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("conflict_id", "isolation_id", "workspace_id"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        object.__setattr__(self, "logical_path", _normalized_logical_path(self.logical_path))
        if self.repository_root:
            object.__setattr__(self, "repository_root", _normalized_logical_path(self.repository_root, allow_root=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "isolation_id": self.isolation_id,
            "workspace_id": self.workspace_id,
            "logical_path": self.logical_path,
            "disposition": self.disposition.value,
            "baseline_hash": self.baseline_hash,
            "parent_hash": self.parent_hash,
            "child_hash": self.child_hash,
            "repository_root": self.repository_root,
            "created_at": self.created_at,
            "resolved": self.resolved,
            "resolution": self.resolution,
            "metadata": dict(self.metadata),
            "physical_location_redacted": True,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MergeConflict":
        return cls(
            conflict_id=value.get("conflict_id", ""),
            isolation_id=value.get("isolation_id", ""),
            workspace_id=value.get("workspace_id", ""),
            logical_path=value.get("logical_path", ""),
            disposition=MergeDisposition(str(value.get("disposition") or MergeDisposition.CONFLICT.value)),
            baseline_hash=str(value.get("baseline_hash") or ""),
            parent_hash=str(value.get("parent_hash") or ""),
            child_hash=str(value.get("child_hash") or ""),
            repository_root=str(value.get("repository_root") or ""),
            created_at=str(value.get("created_at") or utc_now()),
            resolved=bool(value.get("resolved", False)),
            resolution=str(value.get("resolution") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class IsolationRecord:
    isolation_id: str
    workspace_id: str
    task_id: str
    parent_worker_id: str
    child_worker_id: str
    state: IsolationState
    owner_epoch: int
    binding_revision: int
    lease_id: str
    baseline_manifest_id: str
    baseline_snapshot_id: str
    opaque_location_ref: str
    allowed_operations: tuple[IntegrationOperation, ...]
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    child_manifest_id: str = ""
    parent_manifest_id: str = ""
    merge_transaction_id: str = ""
    conflict_ids: tuple[str, ...] = ()
    nested_repository_roots: tuple[str, ...] = ()
    idempotency_key: str = ""
    expires_at: str = ""
    error_code: str = ""
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 1

    def __post_init__(self) -> None:
        for name in (
            "isolation_id",
            "workspace_id",
            "task_id",
            "parent_worker_id",
            "child_worker_id",
            "lease_id",
            "baseline_manifest_id",
            "opaque_location_ref",
        ):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        object.__setattr__(self, "owner_epoch", max(1, int(self.owner_epoch)))
        object.__setattr__(self, "binding_revision", max(1, int(self.binding_revision)))
        object.__setattr__(self, "allowed_operations", tuple(self.allowed_operations))
        object.__setattr__(self, "conflict_ids", tuple(str(item) for item in self.conflict_ids))
        object.__setattr__(
            self,
            "nested_repository_roots",
            tuple(_normalized_logical_path(item, allow_root=True) for item in self.nested_repository_roots),
        )
        object.__setattr__(self, "revision", max(1, int(self.revision)))

    @property
    def terminal(self) -> bool:
        return self.state in {
            IsolationState.MERGED,
            IsolationState.CONFLICTED,
            IsolationState.DISCARDED,
            IsolationState.QUARANTINED,
            IsolationState.FAILED,
        }

    def advance(self, state: IsolationState, **updates: Any) -> "IsolationRecord":
        return replace(self, state=state, updated_at=utc_now(), revision=self.revision + 1, **updates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "isolation_id": self.isolation_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "parent_worker_id": self.parent_worker_id,
            "child_worker_id": self.child_worker_id,
            "state": self.state.value,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "lease_id": self.lease_id,
            "baseline_manifest_id": self.baseline_manifest_id,
            "baseline_snapshot_id": self.baseline_snapshot_id,
            "opaque_location_ref": self.opaque_location_ref,
            "allowed_operations": [item.value for item in self.allowed_operations],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "child_manifest_id": self.child_manifest_id,
            "parent_manifest_id": self.parent_manifest_id,
            "merge_transaction_id": self.merge_transaction_id,
            "conflict_ids": list(self.conflict_ids),
            "nested_repository_roots": list(self.nested_repository_roots),
            "idempotency_key": self.idempotency_key,
            "expires_at": self.expires_at,
            "error_code": self.error_code,
            "message": self.message,
            "metadata": dict(self.metadata),
            "revision": self.revision,
            "physical_location_redacted": True,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IsolationRecord":
        return cls(
            isolation_id=value.get("isolation_id", ""),
            workspace_id=value.get("workspace_id", ""),
            task_id=value.get("task_id", ""),
            parent_worker_id=value.get("parent_worker_id", ""),
            child_worker_id=value.get("child_worker_id", ""),
            state=IsolationState(str(value.get("state") or IsolationState.PREPARING.value)),
            owner_epoch=value.get("owner_epoch", 0),
            binding_revision=value.get("binding_revision", 0),
            lease_id=value.get("lease_id", ""),
            baseline_manifest_id=value.get("baseline_manifest_id", ""),
            baseline_snapshot_id=str(value.get("baseline_snapshot_id") or ""),
            opaque_location_ref=value.get("opaque_location_ref", ""),
            allowed_operations=tuple(
                IntegrationOperation(str(item)) for item in value.get("allowed_operations") or ()
            ),
            created_at=str(value.get("created_at") or utc_now()),
            updated_at=str(value.get("updated_at") or utc_now()),
            child_manifest_id=str(value.get("child_manifest_id") or ""),
            parent_manifest_id=str(value.get("parent_manifest_id") or ""),
            merge_transaction_id=str(value.get("merge_transaction_id") or ""),
            conflict_ids=tuple(str(item) for item in value.get("conflict_ids") or ()),
            nested_repository_roots=tuple(str(item) for item in value.get("nested_repository_roots") or ()),
            idempotency_key=str(value.get("idempotency_key") or ""),
            expires_at=str(value.get("expires_at") or ""),
            error_code=str(value.get("error_code") or ""),
            message=str(value.get("message") or ""),
            metadata=dict(value.get("metadata") or {}),
            revision=value.get("revision", 1),
        )


@dataclass(frozen=True, slots=True)
class IsolationMergeResult:
    isolation_id: str
    workspace_id: str
    state: IsolationState
    transaction_id: str
    applied_paths: tuple[str, ...]
    preserved_parent_paths: tuple[str, ...]
    conflicts: tuple[MergeConflict, ...]
    owner_epoch_before: int
    owner_epoch_after: int
    receipt_ref: str
    recovery_input_id: str = ""
    idempotent_replay: bool = False
    completed_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return self.state is IsolationState.MERGED and not self.conflicts

    def to_dict(self) -> dict[str, Any]:
        return {
            "isolation_id": self.isolation_id,
            "workspace_id": self.workspace_id,
            "state": self.state.value,
            "transaction_id": self.transaction_id,
            "applied_paths": list(self.applied_paths),
            "preserved_parent_paths": list(self.preserved_parent_paths),
            "conflicts": [item.to_dict() for item in self.conflicts],
            "owner_epoch_before": self.owner_epoch_before,
            "owner_epoch_after": self.owner_epoch_after,
            "receipt_ref": self.receipt_ref,
            "recovery_input_id": self.recovery_input_id,
            "idempotent_replay": self.idempotent_replay,
            "completed_at": self.completed_at,
            "physical_location_redacted": True,
        }


@dataclass(frozen=True, slots=True)
class LocalWorkspaceEndpoint:
    endpoint_id: str
    root_token: str
    relative_root: str
    capability_revision: int = 1
    enabled: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint_id", _require_text("endpoint_id", self.endpoint_id))
        object.__setattr__(self, "root_token", _require_text("root_token", self.root_token))
        object.__setattr__(self, "relative_root", _normalized_logical_path(self.relative_root))
        object.__setattr__(self, "capability_revision", max(1, int(self.capability_revision)))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "root_token": self.root_token,
            "capability_revision": self.capability_revision,
            "enabled": self.enabled,
            "physical_location_redacted": True,
            "metadata": {key: value for key, value in self.metadata.items() if "path" not in key.casefold()},
        }

    def to_private_dict(self) -> dict[str, Any]:
        return {
            **self.to_public_dict(),
            "relative_root": self.relative_root,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LocalWorkspaceEndpoint":
        return cls(
            endpoint_id=value.get("endpoint_id", ""),
            root_token=value.get("root_token", ""),
            relative_root=value.get("relative_root", ""),
            capability_revision=value.get("capability_revision", 1),
            enabled=bool(value.get("enabled", True)),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceReferenceMigration:
    migration_id: str
    workspace_id: str
    source_endpoint_id: str
    target_endpoint_id: str
    source_owner_epoch: int
    target_owner_epoch: int
    snapshot_id: str
    artifact_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    download_refs: tuple[str, ...] = ()
    mount_snapshot_refs: Mapping[str, str] = field(default_factory=dict)
    completed_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("migration_id", "workspace_id", "source_endpoint_id", "target_endpoint_id", "snapshot_id"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "workspace_id": self.workspace_id,
            "source_endpoint_id": self.source_endpoint_id,
            "target_endpoint_id": self.target_endpoint_id,
            "source_owner_epoch": self.source_owner_epoch,
            "target_owner_epoch": self.target_owner_epoch,
            "snapshot_id": self.snapshot_id,
            "artifact_refs": list(self.artifact_refs),
            "event_refs": list(self.event_refs),
            "download_refs": list(self.download_refs),
            "mount_snapshot_refs": dict(self.mount_snapshot_refs),
            "completed_at": self.completed_at,
            "physical_location_redacted": True,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceRebindRecord:
    rebind_id: str
    workspace_id: str
    source_endpoint_id: str
    target_endpoint_id: str
    state: RebindState
    owner_epoch_before: int
    owner_epoch_after: int
    binding_revision_before: int
    binding_revision_after: int
    lease_id_before: str
    lease_id_after: str
    snapshot_id: str = ""
    baseline_id: str = ""
    migration_id: str = ""
    idempotency_key: str = ""
    started_at: str = field(default_factory=utc_now)
    completed_at: str = ""
    rollback_snapshot_id: str = ""
    error_code: str = ""
    error_type: str = ""
    message: str = ""
    recovery_input_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 1

    def __post_init__(self) -> None:
        for name in ("rebind_id", "workspace_id", "source_endpoint_id", "target_endpoint_id", "lease_id_before"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        object.__setattr__(self, "owner_epoch_before", max(1, int(self.owner_epoch_before)))
        object.__setattr__(self, "owner_epoch_after", max(0, int(self.owner_epoch_after)))
        object.__setattr__(self, "binding_revision_before", max(1, int(self.binding_revision_before)))
        object.__setattr__(self, "binding_revision_after", max(0, int(self.binding_revision_after)))
        object.__setattr__(self, "revision", max(1, int(self.revision)))

    @property
    def terminal(self) -> bool:
        return self.state in {RebindState.COMMITTED, RebindState.ROLLED_BACK, RebindState.FAILED}

    def advance(self, state: RebindState, **updates: Any) -> "WorkspaceRebindRecord":
        return replace(self, state=state, revision=self.revision + 1, **updates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rebind_id": self.rebind_id,
            "workspace_id": self.workspace_id,
            "source_endpoint_id": self.source_endpoint_id,
            "target_endpoint_id": self.target_endpoint_id,
            "state": self.state.value,
            "owner_epoch_before": self.owner_epoch_before,
            "owner_epoch_after": self.owner_epoch_after,
            "binding_revision_before": self.binding_revision_before,
            "binding_revision_after": self.binding_revision_after,
            "lease_id_before": self.lease_id_before,
            "lease_id_after": self.lease_id_after,
            "snapshot_id": self.snapshot_id,
            "baseline_id": self.baseline_id,
            "migration_id": self.migration_id,
            "idempotency_key": self.idempotency_key,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "rollback_snapshot_id": self.rollback_snapshot_id,
            "error_code": self.error_code,
            "error_type": self.error_type,
            "message": self.message,
            "recovery_input_id": self.recovery_input_id,
            "metadata": dict(self.metadata),
            "revision": self.revision,
            "physical_location_redacted": True,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkspaceRebindRecord":
        return cls(
            rebind_id=value.get("rebind_id", ""),
            workspace_id=value.get("workspace_id", ""),
            source_endpoint_id=value.get("source_endpoint_id", ""),
            target_endpoint_id=value.get("target_endpoint_id", ""),
            state=RebindState(str(value.get("state") or RebindState.REQUESTED.value)),
            owner_epoch_before=value.get("owner_epoch_before", 0),
            owner_epoch_after=value.get("owner_epoch_after", 0),
            binding_revision_before=value.get("binding_revision_before", 0),
            binding_revision_after=value.get("binding_revision_after", 0),
            lease_id_before=value.get("lease_id_before", ""),
            lease_id_after=str(value.get("lease_id_after") or ""),
            snapshot_id=str(value.get("snapshot_id") or ""),
            baseline_id=str(value.get("baseline_id") or ""),
            migration_id=str(value.get("migration_id") or ""),
            idempotency_key=str(value.get("idempotency_key") or ""),
            started_at=str(value.get("started_at") or utc_now()),
            completed_at=str(value.get("completed_at") or ""),
            rollback_snapshot_id=str(value.get("rollback_snapshot_id") or ""),
            error_code=str(value.get("error_code") or ""),
            error_type=str(value.get("error_type") or ""),
            message=str(value.get("message") or ""),
            recovery_input_id=str(value.get("recovery_input_id") or ""),
            metadata=dict(value.get("metadata") or {}),
            revision=value.get("revision", 1),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceAccessEnvelope:
    envelope_id: str
    workspace_id: str
    task_id: str
    audience: str
    owner_epoch: int
    binding_revision: int
    capability_revision: int
    lease_id: str
    operations: tuple[IntegrationOperation, ...]
    mount_kinds: tuple[str, ...]
    issued_at: str
    expires_at: str
    nonce: str
    signature: str
    key_id: str
    isolation_id: str = ""
    endpoint_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("envelope_id", "workspace_id", "task_id", "audience", "lease_id", "nonce", "key_id"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        object.__setattr__(self, "owner_epoch", max(1, int(self.owner_epoch)))
        object.__setattr__(self, "binding_revision", max(1, int(self.binding_revision)))
        object.__setattr__(self, "capability_revision", max(1, int(self.capability_revision)))
        object.__setattr__(self, "operations", tuple(self.operations))
        object.__setattr__(self, "mount_kinds", tuple(sorted(set(str(item) for item in self.mount_kinds))))

    def signing_payload(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "audience": self.audience,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "capability_revision": self.capability_revision,
            "lease_id": self.lease_id,
            "operations": [item.value for item in self.operations],
            "mount_kinds": list(self.mount_kinds),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "key_id": self.key_id,
            "isolation_id": self.isolation_id,
            "endpoint_id": self.endpoint_id,
            "metadata": dict(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.signing_payload(),
            "signature": self.signature,
            "physical_location_redacted": True,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkspaceAccessEnvelope":
        return cls(
            envelope_id=value.get("envelope_id", ""),
            workspace_id=value.get("workspace_id", ""),
            task_id=value.get("task_id", ""),
            audience=value.get("audience", ""),
            owner_epoch=value.get("owner_epoch", 0),
            binding_revision=value.get("binding_revision", 0),
            capability_revision=value.get("capability_revision", 0),
            lease_id=value.get("lease_id", ""),
            operations=tuple(IntegrationOperation(str(item)) for item in value.get("operations") or ()),
            mount_kinds=tuple(str(item) for item in value.get("mount_kinds") or ()),
            issued_at=value.get("issued_at", ""),
            expires_at=value.get("expires_at", ""),
            nonce=value.get("nonce", ""),
            signature=str(value.get("signature") or ""),
            key_id=value.get("key_id", ""),
            isolation_id=str(value.get("isolation_id") or ""),
            endpoint_id=str(value.get("endpoint_id") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceRecoveryInput:
    recovery_input_id: str
    workspace_id: str
    operation: IntegrationOperation
    severity: RecoverySeverity
    reason_code: str
    owner_epoch: int
    binding_revision: int
    transaction_id: str = ""
    isolation_id: str = ""
    rebind_id: str = ""
    snapshot_id: str = ""
    retryable: bool = False
    created_at: str = field(default_factory=utc_now)
    evidence_refs: tuple[str, ...] = ()
    recommended_actions: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("recovery_input_id", "workspace_id", "reason_code"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        object.__setattr__(self, "owner_epoch", max(1, int(self.owner_epoch)))
        object.__setattr__(self, "binding_revision", max(1, int(self.binding_revision)))
        object.__setattr__(self, "evidence_refs", tuple(str(item) for item in self.evidence_refs))
        object.__setattr__(self, "recommended_actions", tuple(str(item) for item in self.recommended_actions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovery_input_id": self.recovery_input_id,
            "workspace_id": self.workspace_id,
            "operation": self.operation.value,
            "severity": self.severity.value,
            "reason_code": self.reason_code,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "transaction_id": self.transaction_id,
            "isolation_id": self.isolation_id,
            "rebind_id": self.rebind_id,
            "snapshot_id": self.snapshot_id,
            "retryable": self.retryable,
            "created_at": self.created_at,
            "evidence_refs": list(self.evidence_refs),
            "recommended_actions": list(self.recommended_actions),
            "metadata": dict(self.metadata),
            "physical_location_redacted": True,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkspaceRecoveryInput":
        return cls(
            recovery_input_id=value.get("recovery_input_id", ""),
            workspace_id=value.get("workspace_id", ""),
            operation=IntegrationOperation(str(value.get("operation") or IntegrationOperation.RECOVERY.value)),
            severity=RecoverySeverity(str(value.get("severity") or RecoverySeverity.RECOVERY_INPUT.value)),
            reason_code=value.get("reason_code", ""),
            owner_epoch=value.get("owner_epoch", 0),
            binding_revision=value.get("binding_revision", 0),
            transaction_id=str(value.get("transaction_id") or ""),
            isolation_id=str(value.get("isolation_id") or ""),
            rebind_id=str(value.get("rebind_id") or ""),
            snapshot_id=str(value.get("snapshot_id") or ""),
            retryable=bool(value.get("retryable", False)),
            created_at=str(value.get("created_at") or utc_now()),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or ()),
            recommended_actions=tuple(str(item) for item in value.get("recommended_actions") or ()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ArtifactPublication:
    publication_id: str
    workspace_id: str
    transaction_id: str
    logical_path: str
    artifact_id: str
    artifact_uri: str
    content_hash: str
    bytes_published: int
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("publication_id", "workspace_id", "transaction_id", "artifact_id", "artifact_uri"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        object.__setattr__(self, "logical_path", _normalized_logical_path(self.logical_path))
        object.__setattr__(self, "bytes_published", _require_non_negative("bytes_published", self.bytes_published))

    def to_dict(self) -> dict[str, Any]:
        return {
            "publication_id": self.publication_id,
            "workspace_id": self.workspace_id,
            "transaction_id": self.transaction_id,
            "logical_path": self.logical_path,
            "artifact_id": self.artifact_id,
            "artifact_uri": self.artifact_uri,
            "content_hash": self.content_hash,
            "bytes_published": self.bytes_published,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "physical_location_redacted": True,
        }


@dataclass(frozen=True, slots=True)
class WorkerWorkspaceReceipt:
    receipt_id: str
    workspace_id: str
    worker_id: str
    operation: IntegrationOperation
    ok: bool
    owner_epoch: int
    binding_revision: int
    lease_id: str
    transaction_id: str = ""
    isolation_id: str = ""
    rebind_id: str = ""
    logical_paths: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    error_code: str = ""
    summary: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("receipt_id", "workspace_id", "worker_id", "lease_id"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        object.__setattr__(self, "owner_epoch", max(1, int(self.owner_epoch)))
        object.__setattr__(self, "binding_revision", max(1, int(self.binding_revision)))
        object.__setattr__(
            self,
            "logical_paths",
            tuple(_normalized_logical_path(item) for item in self.logical_paths),
        )
        object.__setattr__(self, "artifact_refs", tuple(str(item) for item in self.artifact_refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "workspace_id": self.workspace_id,
            "worker_id": self.worker_id,
            "operation": self.operation.value,
            "ok": self.ok,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "lease_id": self.lease_id,
            "transaction_id": self.transaction_id,
            "isolation_id": self.isolation_id,
            "rebind_id": self.rebind_id,
            "logical_paths": list(self.logical_paths),
            "artifact_refs": list(self.artifact_refs),
            "error_code": self.error_code,
            "summary": self.summary,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "physical_location_redacted": True,
        }


def classify_merge_disposition(
    baseline: TreeEntry | None,
    parent: TreeEntry | None,
    child: TreeEntry | None,
) -> MergeDisposition:
    """Classify one path without reading physical location information."""

    baseline_identity = baseline.identity if baseline is not None else "absent"
    parent_identity = parent.identity if parent is not None else "absent"
    child_identity = child.identity if child is not None else "absent"
    parent_changed = parent_identity != baseline_identity
    child_changed = child_identity != baseline_identity
    if not parent_changed and not child_changed:
        return MergeDisposition.UNCHANGED
    if parent_changed and not child_changed:
        return MergeDisposition.PARENT_ONLY
    if child_changed and not parent_changed:
        if child is None:
            return MergeDisposition.DELETE_CHILD_ONLY
        return MergeDisposition.CHILD_ONLY
    if parent_identity == child_identity:
        return MergeDisposition.IDENTICAL_CHANGE
    if parent is None or child is None:
        return MergeDisposition.DELETE_MODIFY_CONFLICT
    if parent.kind is not child.kind:
        return MergeDisposition.TYPE_CONFLICT
    return MergeDisposition.CONFLICT


def manifest_delta(
    baseline: WorkspaceTreeManifest,
    current: WorkspaceTreeManifest,
) -> tuple[tuple[str, MergeDisposition], ...]:
    baseline_by_path = baseline.by_path()
    current_by_path = current.by_path()
    paths = sorted(set(baseline_by_path) | set(current_by_path))
    result: list[tuple[str, MergeDisposition]] = []
    for path in paths:
        before = baseline_by_path.get(path)
        after = current_by_path.get(path)
        disposition = classify_merge_disposition(before, before, after)
        if disposition is not MergeDisposition.UNCHANGED:
            result.append((path, disposition))
    return tuple(result)


def ensure_path_free_projection(value: Any) -> None:
    """Fail closed when a public integration projection leaks host paths."""

    suspicious_keys = {
        "path",
        "physical_path",
        "physical_root",
        "workspace_root",
        "source_download_path",
        "downloads_dir",
    }

    def walk(item: Any, key: str = "") -> None:
        if isinstance(item, Mapping):
            for child_key, child_value in item.items():
                lowered = str(child_key).casefold()
                if lowered in suspicious_keys or lowered.endswith("_physical_path"):
                    if child_value not in {None, "", False}:
                        raise WorkspaceError(
                            WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                            "A public workspace projection contains a physical path field.",
                            operation="validate_public_workspace_projection",
                            metadata={"field": str(child_key)},
                        )
                walk(child_value, lowered)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child, key)

    walk(value)


def public_receipt_digest(receipts: Iterable[Mapping[str, Any]]) -> str:
    normalized = [dict(item) for item in receipts]
    for item in normalized:
        ensure_path_free_projection(item)
    return stable_digest(normalized)


__all__ = [
    "ArtifactPublication",
    "IntegrationOperation",
    "IntegrationPhase",
    "IsolationMergeResult",
    "IsolationRecord",
    "IsolationState",
    "LocalWorkspaceEndpoint",
    "MergeConflict",
    "MergeDisposition",
    "MutationKind",
    "MutationPathResult",
    "RebindState",
    "RecoverySeverity",
    "TreeEntry",
    "TreeEntryKind",
    "WorkerWorkspaceReceipt",
    "WorkspaceAccessEnvelope",
    "WorkspaceMutation",
    "WorkspaceMutationPlan",
    "WorkspaceReadEvidence",
    "WorkspaceRebindRecord",
    "WorkspaceRecoveryInput",
    "WorkspaceReferenceMigration",
    "WorkspaceTransactionRecord",
    "WorkspaceTreeManifest",
    "classify_merge_disposition",
    "ensure_path_free_projection",
    "manifest_delta",
    "public_receipt_digest",
]
