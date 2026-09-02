from __future__ import annotations

import hashlib
import os
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    BackendDefinition,
    BackendDispatchError,
    BackendDispatchEnvelope,
    BackendFailureKind,
    BackendLease,
    BackendRecoveryIntent,
    canonical_json,
    checksum,
)
from .store import BackendRegistryStore


@dataclass(frozen=True, slots=True)
class WorkspaceEntryAttestation:
    relative_path: str
    kind: str
    size: int
    modified_ns: int
    mode: int
    identity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "kind": self.kind,
            "size": self.size,
            "modified_ns": self.modified_ns,
            "mode": self.mode,
            "identity": self.identity,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceAttestation:
    attestation_id: str
    envelope_id: str
    lease_id: str
    backend_id: str
    runtime_worker: str
    workspace_root: str
    artifact_root: str
    root_identity: str
    artifact_root_identity: str
    created_at: float
    entry_count: int
    total_bytes: int
    truncated: bool
    entries_digest: str
    writable: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            "envelope_id": self.envelope_id,
            "lease_id": self.lease_id,
            "backend_id": self.backend_id,
            "runtime_worker": self.runtime_worker,
            "workspace_root": self.workspace_root,
            "artifact_root": self.artifact_root,
            "root_identity": self.root_identity,
            "artifact_root_identity": self.artifact_root_identity,
            "created_at": self.created_at,
            "entry_count": self.entry_count,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "entries_digest": self.entries_digest,
            "writable": self.writable,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceAttestationPolicy:
    maximum_entries: int = 50_000
    maximum_stat_bytes: int = 16 * 1024 * 1024 * 1024
    maximum_depth: int = 64
    reject_symlinks: bool = True
    require_workspace_write: bool = True
    require_artifact_write: bool = True
    ignored_names: tuple[str, ...] = (
        ".git",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        ".cache",
    )
    ignored_suffixes: tuple[str, ...] = (
        ".pyc",
        ".pyo",
        ".tmp",
        ".swp",
    )

    def __post_init__(self) -> None:
        if self.maximum_entries < 1:
            raise ValueError("workspace attestation entry budget must be positive")
        if self.maximum_stat_bytes < 1:
            raise ValueError("workspace attestation byte budget must be positive")
        if self.maximum_depth < 1:
            raise ValueError("workspace attestation depth must be positive")


@dataclass(frozen=True, slots=True)
class WorkspaceMutationAssessment:
    before: WorkspaceAttestation
    after: WorkspaceAttestation
    root_replaced: bool
    artifact_root_replaced: bool
    entry_digest_changed: bool
    writable_changed: bool
    safe: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "root_replaced": self.root_replaced,
            "artifact_root_replaced": self.artifact_root_replaced,
            "entry_digest_changed": self.entry_digest_changed,
            "writable_changed": self.writable_changed,
            "safe": self.safe,
            "reasons": list(self.reasons),
        }


class WorkspaceAttestationRuntime:
    def __init__(
        self,
        store: BackendRegistryStore,
        *,
        policy: WorkspaceAttestationPolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or WorkspaceAttestationPolicy()

    def attest(
        self,
        lease: BackendLease,
        definition: BackendDefinition,
        envelope: BackendDispatchEnvelope,
    ) -> WorkspaceAttestation:
        workspace = Path(envelope.workspace_root).expanduser().resolve()
        artifact = Path(envelope.artifact_root).expanduser().resolve()
        self._validate_roots(workspace, artifact, lease, definition, envelope)
        # A registered CLI terminal executes against the user's live working
        # tree.  Walking that tree before and after every typed action is both
        # semantically wrong (the user may edit it concurrently) and makes a
        # single file read/write scale with the size of the whole repository.
        # The terminal transport already fences every target beneath the
        # attested startup root and returns an action receipt.  At this layer we
        # therefore attest the stable root identities only.  Managed local
        # process and container workspaces retain the full entry scan.
        root_only = self._uses_live_terminal_root(definition)
        if root_only:
            entries, total_bytes, truncated = (), 0, False
        else:
            entries, total_bytes, truncated = self._scan(workspace)
        writable = os.access(workspace, os.W_OK) and os.access(artifact, os.W_OK)
        if self.policy.require_workspace_write and not os.access(workspace, os.W_OK):
            raise self._corrupt(
                envelope,
                "workspace root is not writable",
                detail={"workspace_root": str(workspace)},
            )
        if self.policy.require_artifact_write and not os.access(artifact, os.W_OK):
            raise self._corrupt(
                envelope,
                "artifact root is not writable",
                detail={"artifact_root": str(artifact)},
            )
        entry_values = [entry.to_dict() for entry in entries]
        return WorkspaceAttestation(
            attestation_id=f"workspace_attestation_{uuid.uuid4().hex}",
            envelope_id=envelope.envelope_id,
            lease_id=lease.lease_id,
            backend_id=definition.backend_id,
            runtime_worker=definition.runtime_worker,
            workspace_root=str(workspace),
            artifact_root=str(artifact),
            root_identity=path_identity(workspace),
            artifact_root_identity=path_identity(artifact),
            created_at=time.time(),
            entry_count=len(entries),
            total_bytes=total_bytes,
            truncated=truncated,
            entries_digest=checksum(
                {
                    "mode": "root_identity_only" if root_only else "entry_manifest",
                    "entries": entry_values,
                }
            ),
            writable=writable,
            metadata={
                "maximum_entries": self.policy.maximum_entries,
                "maximum_stat_bytes": self.policy.maximum_stat_bytes,
                "maximum_depth": self.policy.maximum_depth,
                "reject_symlinks": self.policy.reject_symlinks,
                "entry_attestation_mode": (
                    "root_identity_only" if root_only else "entry_manifest"
                ),
                "workspace_policy": {
                    "scope": definition.workspace_policy.scope,
                    "isolation": definition.workspace_policy.isolation,
                    "require_existing": definition.workspace_policy.require_existing,
                    "read_only": definition.workspace_policy.read_only,
                    "artifact_only": definition.workspace_policy.artifact_only,
                    "require_writable": definition.workspace_policy.require_writable,
                },
            },
        )

    @staticmethod
    def _uses_live_terminal_root(definition: BackendDefinition) -> bool:
        metadata = dict(definition.metadata)
        return bool(
            metadata.get("terminal_registration") is True
            and str(getattr(definition.kind, "value", definition.kind)) == "edge_http"
            and str(getattr(definition.location, "value", definition.location)) == "local"
            and str(metadata.get("execution_mode") or "") == "terminal_http"
        )

    def compare(
        self,
        before: WorkspaceAttestation,
        after: WorkspaceAttestation,
        *,
        allow_entry_mutation: bool = True,
    ) -> WorkspaceMutationAssessment:
        if before.envelope_id != after.envelope_id:
            raise ValueError("workspace attestations belong to different envelopes")
        root_replaced = before.root_identity != after.root_identity
        artifact_root_replaced = before.artifact_root_identity != after.artifact_root_identity
        entry_digest_changed = before.entries_digest != after.entries_digest
        writable_changed = before.writable != after.writable
        reasons: list[str] = []
        if root_replaced:
            reasons.append("workspace root identity changed during dispatch")
        if artifact_root_replaced:
            reasons.append("artifact root identity changed during dispatch")
        if writable_changed and not after.writable:
            reasons.append("workspace or artifact root lost write access")
        if entry_digest_changed and not allow_entry_mutation:
            reasons.append("workspace mutated while operation declared read-only")
        if before.truncated or after.truncated:
            reasons.append("workspace attestation scan reached configured budget")
        safe = not root_replaced and not artifact_root_replaced and after.writable
        if not allow_entry_mutation:
            safe = safe and not entry_digest_changed
        return WorkspaceMutationAssessment(
            before=before,
            after=after,
            root_replaced=root_replaced,
            artifact_root_replaced=artifact_root_replaced,
            entry_digest_changed=entry_digest_changed,
            writable_changed=writable_changed,
            safe=safe,
            reasons=tuple(reasons),
        )

    def require_safe_completion(
        self,
        before: WorkspaceAttestation,
        after: WorkspaceAttestation,
        *,
        allow_entry_mutation: bool,
    ) -> WorkspaceMutationAssessment:
        assessment = self.compare(
            before,
            after,
            allow_entry_mutation=allow_entry_mutation,
        )
        if not assessment.safe:
            self.store.quarantine_workspace(
                quarantine_id=f"backend_quarantine_{uuid.uuid4().hex}",
                workspace_root=before.workspace_root,
                backend_id=before.backend_id,
                worker_id=before.runtime_worker,
                reason="; ".join(assessment.reasons) or "workspace attestation failed",
                created_at=time.time(),
                expires_at=None,
                metadata={
                    "before_attestation_id": before.attestation_id,
                    "after_attestation_id": after.attestation_id,
                    "assessment": assessment.to_dict(),
                },
            )
            raise BackendDispatchError(
                BackendFailureKind.WORKSPACE_CORRUPT,
                "; ".join(assessment.reasons) or "workspace attestation failed",
                retryable=True,
                recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
                backend_id=before.backend_id,
                lease_id=before.lease_id,
                output_observed=True,
                detail={
                    "before_attestation_id": before.attestation_id,
                    "after_attestation_id": after.attestation_id,
                },
            )
        return assessment

    def _validate_roots(
        self,
        workspace: Path,
        artifact: Path,
        lease: BackendLease,
        definition: BackendDefinition,
        envelope: BackendDispatchEnvelope,
    ) -> None:
        if str(workspace) != str(Path(lease.workspace_root).expanduser().resolve()):
            raise self._corrupt(envelope, "workspace path differs from backend lease")
        if str(artifact) != str(Path(lease.artifact_root).expanduser().resolve()):
            raise self._corrupt(envelope, "artifact path differs from backend lease")
        if not workspace.exists() or not workspace.is_dir():
            raise self._corrupt(
                envelope,
                "workspace root is missing or not a directory",
                detail={"workspace_root": str(workspace)},
            )
        if not artifact.exists() or not artifact.is_dir():
            raise self._corrupt(
                envelope,
                "artifact root is missing or not a directory",
                detail={"artifact_root": str(artifact)},
            )
        if self.policy.reject_symlinks:
            for label, path in (("workspace", workspace), ("artifact", artifact)):
                if path.is_symlink():
                    raise self._corrupt(envelope, f"{label} root may not be a symbolic link")
        if envelope.backend_id != definition.backend_id or lease.backend_id != definition.backend_id:
            raise self._corrupt(envelope, "backend definition identity drift")
        if envelope.backend_lease_id != lease.lease_id:
            raise self._corrupt(envelope, "backend lease identity drift")
        if envelope.runtime_worker != definition.runtime_worker:
            raise self._corrupt(envelope, "runtime worker identity drift")

    def _scan(
        self,
        root: Path,
    ) -> tuple[tuple[WorkspaceEntryAttestation, ...], int, bool]:
        entries: list[WorkspaceEntryAttestation] = []
        total_bytes = 0
        truncated = False
        stack: list[tuple[Path, int]] = [(root, 0)]
        visited_directories: set[str] = set()
        while stack:
            directory, depth = stack.pop()
            identity = path_identity(directory)
            if identity in visited_directories:
                raise BackendDispatchError(
                    BackendFailureKind.WORKSPACE_CORRUPT,
                    "workspace contains a directory identity cycle",
                    retryable=True,
                    recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
                    detail={"directory": str(directory)},
                )
            visited_directories.add(identity)
            if depth > self.policy.maximum_depth:
                truncated = True
                continue
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            except OSError as error:
                raise BackendDispatchError(
                    BackendFailureKind.WORKSPACE_CORRUPT,
                    f"workspace directory cannot be enumerated: {error}",
                    retryable=True,
                    recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
                    detail={"directory": str(directory)},
                ) from error
            for child in children:
                if self._ignored(child):
                    continue
                try:
                    details = child.lstat()
                except OSError as error:
                    raise BackendDispatchError(
                        BackendFailureKind.WORKSPACE_CORRUPT,
                        f"workspace entry cannot be inspected: {error}",
                        retryable=True,
                        recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
                        detail={"path": str(child)},
                    ) from error
                relative = child.relative_to(root).as_posix()
                kind = mode_kind(details.st_mode)
                if kind == "symlink" and self.policy.reject_symlinks:
                    raise BackendDispatchError(
                        BackendFailureKind.WORKSPACE_CORRUPT,
                        "workspace contains a symbolic link",
                        retryable=True,
                        recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
                        detail={"path": relative},
                    )
                entry = WorkspaceEntryAttestation(
                    relative_path=relative,
                    kind=kind,
                    size=int(details.st_size),
                    modified_ns=int(details.st_mtime_ns),
                    mode=stat.S_IMODE(details.st_mode),
                    identity=stat_identity(details),
                )
                entries.append(entry)
                total_bytes += max(0, entry.size if kind == "file" else 0)
                if len(entries) >= self.policy.maximum_entries:
                    truncated = True
                    return tuple(entries), total_bytes, truncated
                if total_bytes >= self.policy.maximum_stat_bytes:
                    truncated = True
                    return tuple(entries), total_bytes, truncated
                if kind == "directory":
                    stack.append((child, depth + 1))
        return tuple(entries), total_bytes, truncated

    def _ignored(self, path: Path) -> bool:
        if path.name in self.policy.ignored_names:
            return True
        return any(path.name.endswith(suffix) for suffix in self.policy.ignored_suffixes)

    @staticmethod
    def _corrupt(
        envelope: BackendDispatchEnvelope,
        message: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> BackendDispatchError:
        return BackendDispatchError(
            BackendFailureKind.WORKSPACE_CORRUPT,
            message,
            retryable=True,
            recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
            backend_id=envelope.backend_id,
            lease_id=envelope.backend_lease_id,
            provider_route_id=envelope.provider_route_id or "",
            detail=dict(detail or {}),
        )


def mode_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISCHR(mode):
        return "character_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "unknown"


def stat_identity(value: os.stat_result) -> str:
    parts = {
        "device": int(getattr(value, "st_dev", 0)),
        "inode": int(getattr(value, "st_ino", 0)),
        "mode": int(value.st_mode),
        "created_ns": int(getattr(value, "st_ctime_ns", 0)),
    }
    return hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()


def path_identity(path: Path) -> str:
    return stat_identity(path.stat(follow_symlinks=False))


__all__ = [
    "WorkspaceAttestation",
    "WorkspaceAttestationPolicy",
    "WorkspaceAttestationRuntime",
    "WorkspaceEntryAttestation",
    "WorkspaceMutationAssessment",
    "mode_kind",
    "path_identity",
    "stat_identity",
]
