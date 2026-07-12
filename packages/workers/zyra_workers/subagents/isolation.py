from __future__ import annotations

import copy
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from zyra_core import new_id, now_iso

from .digests import digest_object
from .errors import IsolationCleanupFailed, IsolationRequestRejected, SubagentDisabled
from .models import (
    IsolationCleanupReceipt,
    IsolationCleanupStatus,
    SubagentIsolationKind,
    SubagentIsolationManifest,
    SubagentIsolationRequest,
)


class SubagentIsolationRequestPort(Protocol):
    def prepare(self, request: SubagentIsolationRequest) -> SubagentIsolationManifest: ...

    def cleanup(self, manifest: SubagentIsolationManifest) -> IsolationCleanupReceipt: ...


@dataclass(frozen=True, slots=True)
class LogicalIsolationPolicy:
    allow_shared_workspace: bool = True
    allow_network: bool = False
    require_existing_workspace: bool = True
    allowed_workspace_roots: tuple[str, ...] = ()


class LogicalWorkspaceIsolationPort:
    """Honest adapter over the existing M0 workspace boundary.

    It validates containment and emits a real manifest/cleanup receipt but
    deliberately rejects worktree, sandbox and remote requests. Those physical
    lifecycle owners belong to M1-05A/07A. A caller can later replace this port
    without changing the logical SubagentTask aggregate.
    """

    def __init__(self, policy: LogicalIsolationPolicy | None = None, *, disabled: bool = False) -> None:
        self.policy = policy or LogicalIsolationPolicy()
        self.disabled = disabled
        self._lock = RLock()
        self._active: dict[str, SubagentIsolationManifest] = {}

    def prepare(self, request: SubagentIsolationRequest) -> SubagentIsolationManifest:
        if self.disabled:
            raise SubagentDisabled("SubagentIsolationRequestPort")
        if request.kind not in {SubagentIsolationKind.NONE, SubagentIsolationKind.WORKSPACE}:
            raise IsolationRequestRejected(
                "physical worktree/sandbox/remote isolation is owned by M1-05A/07A and is unavailable",
                requested_kind=request.kind.value,
                owner="M1-05A WorkspaceIsolationManager",
            )
        if request.kind == SubagentIsolationKind.WORKSPACE and not self.policy.allow_shared_workspace:
            raise IsolationRequestRejected("shared workspace logical isolation is disabled")
        if request.network_allowed and not self.policy.allow_network:
            raise IsolationRequestRejected("logical isolation policy denies network access")
        root = Path(request.workspace_root).resolve()
        if self.policy.require_existing_workspace and not root.is_dir():
            raise IsolationRequestRejected("workspace root does not exist", workspace_root=str(root))
        allowed_roots = tuple(Path(item).resolve() for item in self.policy.allowed_workspace_roots)
        if allowed_roots and not any(_contains(candidate, root) for candidate in allowed_roots):
            raise IsolationRequestRejected(
                "workspace root is outside configured logical isolation roots",
                workspace_root=str(root),
                allowed_roots=[str(item) for item in allowed_roots],
            )
        cwd = Path(request.requested_cwd).resolve() if request.requested_cwd else root
        if not _contains(root, cwd):
            raise IsolationRequestRejected(
                "requested child cwd escapes the workspace root",
                workspace_root=str(root),
                requested_cwd=str(cwd),
            )
        for path in (*request.writable_paths, *request.read_only_paths):
            candidate = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
            if not _contains(root, candidate):
                raise IsolationRequestRejected("isolation path escapes workspace root", path=str(candidate))
        isolation_id = new_id("subisolation")
        cleanup_token = secrets.token_urlsafe(32)
        manifest = SubagentIsolationManifest(
            isolation_id=isolation_id,
            request_id=request.request_id,
            kind=request.kind,
            workspace_root=str(root),
            effective_cwd=str(cwd),
            logical_only=True,
            state_owner="M1-03D logical contract; physical lifecycle owner M1-05A",
            cleanup_token=cleanup_token,
            metadata={
                "backend": "logical_shared_workspace" if request.kind == SubagentIsolationKind.WORKSPACE else "no_isolation",
                "network_allowed": request.network_allowed,
                "cleanup_required": request.cleanup_required,
                "request_digest": digest_object(request.to_dict()),
                "physical_isolation_claimed": False,
            },
        )
        with self._lock:
            self._active[isolation_id] = manifest
        return copy.deepcopy(manifest)

    def cleanup(self, manifest: SubagentIsolationManifest) -> IsolationCleanupReceipt:
        if self.disabled:
            raise SubagentDisabled("SubagentIsolationRequestPort")
        with self._lock:
            current = self._active.get(manifest.isolation_id)
            if current is None:
                raise IsolationCleanupFailed(
                    manifest.isolation_id,
                    "isolation manifest is missing or was already released",
                )
            if not secrets.compare_digest(current.cleanup_token, manifest.cleanup_token):
                raise IsolationCleanupFailed(manifest.isolation_id, "isolation cleanup token mismatch")
            if current.request_id != manifest.request_id or current.workspace_root != manifest.workspace_root:
                raise IsolationCleanupFailed(manifest.isolation_id, "isolation manifest identity mismatch")
            self._active.pop(manifest.isolation_id, None)
        return IsolationCleanupReceipt(
            isolation_id=manifest.isolation_id,
            status=(
                IsolationCleanupStatus.NOT_REQUIRED
                if manifest.kind == SubagentIsolationKind.NONE
                else IsolationCleanupStatus.RELEASED
            ),
            safe_to_forget=True,
            reason="logical isolation lease released; no physical workspace was created",
            metadata={
                "logical_only": True,
                "physical_cleanup_performed": False,
                "workspace_root": manifest.workspace_root,
            },
        )

    def restore(self, manifest: SubagentIsolationManifest) -> None:
        if self.disabled:
            raise SubagentDisabled("SubagentIsolationRequestPort")
        root = Path(manifest.workspace_root).resolve()
        cwd = Path(manifest.effective_cwd).resolve()
        if not root.is_dir() or not cwd.is_dir() or not _contains(root, cwd):
            raise IsolationRequestRejected(
                "logical isolation cannot be restored because the exact workspace/cwd is unavailable",
                workspace_root=str(root),
                effective_cwd=str(cwd),
            )
        with self._lock:
            self._active[manifest.isolation_id] = copy.deepcopy(manifest)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            manifests = [item.safe_dict() for item in self._active.values()]
        return {
            "schema": "zyra.subagent-isolation/v1",
            "backend": "logical_shared_workspace",
            "physical_owner": "M1-05A WorkspaceIsolationManager",
            "active": manifests,
            "digest": digest_object(manifests),
        }


class RejectingIsolationPort:
    def prepare(self, request: SubagentIsolationRequest) -> SubagentIsolationManifest:
        raise SubagentDisabled("SubagentIsolationRequestPort")

    def cleanup(self, manifest: SubagentIsolationManifest) -> IsolationCleanupReceipt:
        raise SubagentDisabled("SubagentIsolationRequestPort")


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return candidate == root

