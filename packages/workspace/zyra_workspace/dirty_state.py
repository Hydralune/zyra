from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .atomic import atomic_write_json, read_json_object, sha256_file
from .errors import WorkspaceError, WorkspaceErrorCode
from .git_boundary import WorkspaceGitBoundary
from .models import (
    DirtyPathRecord,
    NestedRepositoryRef,
    WorkspaceBaseline,
    WorkspaceDirtyKind,
    WorkspaceDirtyOwner,
    new_workspace_id,
    stable_digest,
    utc_now,
)
from .paths import WorkspacePathSafetyPolicy


OWNERSHIP_SCHEMA = "zyra.workspace-dirty-ownership.v1"


@dataclass(frozen=True, slots=True)
class DirtyOwnershipClaim:
    workspace_id: str
    path: str
    owner: WorkspaceDirtyOwner
    worker_id: str = ""
    operation_id: str = ""
    base_hash: str = ""
    claimed_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "path": self.path,
            "owner": self.owner.value,
            "worker_id": self.worker_id,
            "operation_id": self.operation_id,
            "base_hash": self.base_hash,
            "claimed_at": self.claimed_at,
            "metadata": dict(self.metadata),
        }


class DirtyOwnershipStore:
    """Durable ownership ledger that keeps user dirt separate from agent patches."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def claim(self, claim: DirtyOwnershipClaim, *, replace_existing: bool = False) -> DirtyOwnershipClaim:
        key = self._key(claim.workspace_id, claim.path)
        with self._lock:
            state = self._load()
            existing = state["claims"].get(key)
            if existing and not replace_existing:
                current = _claim_from_dict(existing)
                if current == claim:
                    return current
                raise WorkspaceError(
                    WorkspaceErrorCode.DIRTY_STATE_CONFLICT,
                    "The dirty path already has a different ownership claim.",
                    workspace_id=claim.workspace_id,
                    operation="claim_dirty_path",
                    path=claim.path,
                    metadata={"current_owner": current.owner.value, "requested_owner": claim.owner.value},
                )
            state["claims"][key] = claim.to_dict()
            self._commit(state)
            return claim

    def release(
        self,
        workspace_id: str,
        path: str,
        *,
        operation_id: str = "",
        worker_id: str = "",
    ) -> DirtyOwnershipClaim | None:
        key = self._key(workspace_id, path)
        with self._lock:
            state = self._load()
            raw = state["claims"].get(key)
            if not raw:
                return None
            current = _claim_from_dict(raw)
            if operation_id and current.operation_id != operation_id:
                raise WorkspaceError(
                    WorkspaceErrorCode.DIRTY_STATE_CONFLICT,
                    "The dirty ownership operation does not match the release request.",
                    workspace_id=workspace_id,
                    operation="release_dirty_path",
                    path=path,
                )
            if worker_id and current.worker_id != worker_id:
                raise WorkspaceError(
                    WorkspaceErrorCode.DIRTY_STATE_CONFLICT,
                    "The dirty ownership worker does not match the release request.",
                    workspace_id=workspace_id,
                    operation="release_dirty_path",
                    path=path,
                )
            state["claims"].pop(key, None)
            self._commit(state)
            return current

    def get(self, workspace_id: str, path: str) -> DirtyOwnershipClaim | None:
        with self._lock:
            raw = self._load()["claims"].get(self._key(workspace_id, path))
        return _claim_from_dict(raw) if raw else None

    def list(self, workspace_id: str) -> tuple[DirtyOwnershipClaim, ...]:
        with self._lock:
            claims = tuple(
                _claim_from_dict(value)
                for value in self._load()["claims"].values()
                if str(value.get("workspace_id") or "") == workspace_id
            )
        return tuple(sorted(claims, key=lambda item: item.path))

    def replace_workspace(self, workspace_id: str, claims: Iterable[DirtyOwnershipClaim]) -> None:
        replacements = tuple(claims)
        if any(item.workspace_id != workspace_id for item in replacements):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Dirty ownership replacements must belong to one workspace.",
                workspace_id=workspace_id,
                operation="replace_dirty_ownership",
            )
        with self._lock:
            state = self._load()
            state["claims"] = {
                key: value
                for key, value in state["claims"].items()
                if str(value.get("workspace_id") or "") != workspace_id
            }
            for claim in replacements:
                state["claims"][self._key(workspace_id, claim.path)] = claim.to_dict()
            self._commit(state)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema": OWNERSHIP_SCHEMA, "revision": 0, "claims": {}, "updated_at": utc_now()}
        state = read_json_object(self.path)
        if state.get("schema") != OWNERSHIP_SCHEMA or not isinstance(state.get("claims"), dict):
            raise WorkspaceError(
                WorkspaceErrorCode.STORE_CORRUPT,
                "The dirty ownership store is corrupt or has an unsupported schema.",
                operation="load_dirty_ownership",
            )
        return state

    def _commit(self, state: dict[str, Any]) -> None:
        state["revision"] = int(state.get("revision") or 0) + 1
        state["updated_at"] = utc_now()
        atomic_write_json(self.path, state)

    @staticmethod
    def _key(workspace_id: str, path: str) -> str:
        canonical = str(path or "").replace("\\", "/").strip("/")
        return f"{workspace_id}:{canonical}"


@dataclass(frozen=True, slots=True)
class WorkspaceDirtyState:
    workspace_id: str
    repository_refs: tuple[NestedRepositoryRef, ...]
    paths: tuple[DirtyPathRecord, ...]
    root_tree_hash: str
    scanned_at: str = field(default_factory=utc_now)

    @property
    def dirty(self) -> bool:
        return bool(self.paths)

    def user_paths(self) -> tuple[DirtyPathRecord, ...]:
        return tuple(item for item in self.paths if item.owner is WorkspaceDirtyOwner.USER_BASELINE)

    def agent_paths(self) -> tuple[DirtyPathRecord, ...]:
        return tuple(item for item in self.paths if item.owner is WorkspaceDirtyOwner.AGENT_PATCH)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "repository_refs": [item.to_dict() for item in self.repository_refs],
            "paths": [item.to_dict() for item in self.paths],
            "root_tree_hash": self.root_tree_hash,
            "dirty": self.dirty,
            "scanned_at": self.scanned_at,
        }


class WorkspaceDirtyStateRuntime:
    def __init__(
        self,
        *,
        workspace_id: str,
        workspace_root: str | Path,
        ownership_store: DirtyOwnershipStore,
        path_policy: WorkspacePathSafetyPolicy,
        maximum_scan_entries: int = 200_000,
    ) -> None:
        self.workspace_id = workspace_id
        self.workspace_root = Path(workspace_root).resolve()
        self.ownership_store = ownership_store
        self.path_policy = path_policy
        self.maximum_scan_entries = maximum_scan_entries
        self.git = WorkspaceGitBoundary(self.workspace_root, path_policy=path_policy)

    def capture_baseline(self, *, owner_epoch: int, snapshot_id: str = "") -> WorkspaceBaseline:
        state = self.scan(default_owner=WorkspaceDirtyOwner.USER_BASELINE)
        claims = tuple(
            DirtyOwnershipClaim(
                workspace_id=self.workspace_id,
                path=item.path,
                owner=WorkspaceDirtyOwner.USER_BASELINE,
                base_hash=item.current_hash,
                metadata={"captured_as_baseline": True, "dirty_kind": item.kind.value},
            )
            for item in state.paths
        )
        self.ownership_store.replace_workspace(self.workspace_id, claims)
        return WorkspaceBaseline(
            baseline_id=new_workspace_id("baseline"),
            workspace_id=self.workspace_id,
            owner_epoch=owner_epoch,
            root_tree_hash=state.root_tree_hash,
            repository_refs=state.repository_refs,
            dirty_paths=tuple(replace(item, owner=WorkspaceDirtyOwner.USER_BASELINE) for item in state.paths),
            synthetic_tree_ref=f"sha256:{state.root_tree_hash}",
            snapshot_id=snapshot_id,
            metadata={
                "captures_untracked": True,
                "nested_repository_count": len(state.repository_refs),
                "dirty_path_count": len(state.paths),
            },
        )

    def scan(self, *, default_owner: WorkspaceDirtyOwner = WorkspaceDirtyOwner.UNKNOWN) -> WorkspaceDirtyState:
        repositories = self.discover_repositories()
        observed: dict[str, DirtyPathRecord] = {}
        for repository in repositories:
            if not self.git.is_repository(repository.relative_root):
                continue
            for status_code, path, original_path in parse_porcelain_v1_z(
                self.git.status_porcelain(repository.relative_root)
            ):
                full_path = self._join_repository_path(repository.relative_root, path)
                ownership = self.ownership_store.get(self.workspace_id, full_path)
                record = DirtyPathRecord(
                    workspace_id=self.workspace_id,
                    path=full_path,
                    kind=_dirty_kind(status_code),
                    owner=ownership.owner if ownership else default_owner,
                    baseline_hash=ownership.base_hash if ownership else "",
                    current_hash=self._current_hash(full_path),
                    worker_id=ownership.worker_id if ownership else "",
                    operation_id=ownership.operation_id if ownership else "",
                    nested_repository_id=repository.repository_id,
                    metadata={
                        "status": status_code,
                        "original_path": self._join_repository_path(repository.relative_root, original_path)
                        if original_path
                        else "",
                    },
                )
                observed[full_path] = record
        root_tree_hash = self.synthetic_tree_hash(observed.values(), repositories)
        return WorkspaceDirtyState(
            workspace_id=self.workspace_id,
            repository_refs=repositories,
            paths=tuple(sorted(observed.values(), key=lambda item: item.path)),
            root_tree_hash=root_tree_hash,
        )

    def discover_repositories(self) -> tuple[NestedRepositoryRef, ...]:
        candidates: list[Path] = []
        if (self.workspace_root / ".git").exists():
            candidates.append(self.workspace_root)
        observed_entries = 0
        for current, directories, files in os.walk(self.workspace_root, topdown=True, followlinks=False):
            observed_entries += len(directories) + len(files)
            if observed_entries > self.maximum_scan_entries:
                raise WorkspaceError(
                    WorkspaceErrorCode.QUOTA_EXCEEDED,
                    "Nested repository discovery exceeded its bounded entry scan.",
                    workspace_id=self.workspace_id,
                    operation="discover_nested_repositories",
                    expected=self.maximum_scan_entries,
                    actual=observed_entries,
                )
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if name not in {".git", ".zyra", "node_modules", "__pycache__"}
                and not (current_path / name).is_symlink()
            ]
            if current_path == self.workspace_root:
                continue
            if ".git" in files or (current_path / ".git").is_dir():
                candidates.append(current_path)
        unique = sorted(set(path.resolve() for path in candidates), key=lambda item: (len(item.parts), str(item)))
        repositories: list[NestedRepositoryRef] = []
        for candidate in unique:
            relative = candidate.relative_to(self.workspace_root).as_posix() or "."
            if not self.git.is_repository(relative):
                continue
            head_commit = self.git.head_commit(relative)
            head_ref = self.git.head_ref(relative)
            tree_hash = self.git.tree_hash(relative)
            repository_id = f"repo_{stable_digest({'workspace': self.workspace_id, 'path': relative})[:24]}"
            parent = ""
            for prior in reversed(repositories):
                prior_path = Path(prior.relative_root)
                if prior.relative_root == "." or prior_path in Path(relative).parents:
                    parent = prior.repository_id
                    break
            dirty = bool(self.git.status_porcelain(relative))
            repositories.append(
                NestedRepositoryRef(
                    repository_id=repository_id,
                    relative_root=relative,
                    git_dir_ref=".git",
                    head_ref=head_ref,
                    head_commit=head_commit,
                    baseline_tree=tree_hash,
                    dirty=dirty,
                    parent_repository_id=parent,
                    metadata={"git_queries_read_only": True},
                )
            )
        return tuple(repositories)

    def claim_agent_write(
        self,
        path: str,
        *,
        worker_id: str,
        operation_id: str,
        base_hash: str = "",
    ) -> DirtyOwnershipClaim:
        canonical, _ = self.path_policy.validate_logical_path(path)
        existing = self.ownership_store.get(self.workspace_id, canonical)
        if existing and existing.owner is WorkspaceDirtyOwner.USER_BASELINE:
            raise WorkspaceError(
                WorkspaceErrorCode.DIRTY_STATE_CONFLICT,
                "An agent write cannot silently take ownership of user baseline dirt.",
                workspace_id=self.workspace_id,
                operation="claim_agent_write",
                path=canonical,
                metadata={"baseline_owner": existing.owner.value},
            )
        return self.ownership_store.claim(
            DirtyOwnershipClaim(
                workspace_id=self.workspace_id,
                path=canonical,
                owner=WorkspaceDirtyOwner.AGENT_PATCH,
                worker_id=worker_id,
                operation_id=operation_id,
                base_hash=base_hash,
            ),
            replace_existing=bool(existing),
        )

    def assert_user_baseline_preserved(self, baseline: WorkspaceBaseline) -> None:
        current = self.scan()
        current_by_path = {item.path: item for item in current.paths}
        conflicts: list[str] = []
        for original in baseline.dirty_paths:
            latest = current_by_path.get(original.path)
            if latest is None:
                conflicts.append(f"removed:{original.path}")
                continue
            if original.current_hash and latest.current_hash != original.current_hash:
                conflicts.append(f"changed:{original.path}")
        if conflicts:
            raise WorkspaceError(
                WorkspaceErrorCode.DIRTY_STATE_CONFLICT,
                "Workspace cleanup or merge would alter the user's dirty baseline.",
                workspace_id=self.workspace_id,
                operation="assert_user_baseline_preserved",
                metadata={"conflicts": conflicts[:256], "conflict_count": len(conflicts)},
            )

    def synthetic_tree_hash(
        self,
        paths: Iterable[DirtyPathRecord],
        repositories: Iterable[NestedRepositoryRef],
    ) -> str:
        return stable_digest(
            {
                "workspace_id": self.workspace_id,
                "repositories": [
                    {
                        "path": item.relative_root,
                        "head": item.head_commit,
                        "tree": item.baseline_tree,
                        "dirty": item.dirty,
                    }
                    for item in repositories
                ],
                "dirty_paths": [
                    {
                        "path": item.path,
                        "kind": item.kind.value,
                        "hash": item.current_hash,
                        "owner": item.owner.value,
                    }
                    for item in sorted(paths, key=lambda value: value.path)
                ],
            }
        )

    def _current_hash(self, relative_path: str) -> str:
        path = self.workspace_root.joinpath(*Path(relative_path).parts)
        if not path.exists() or not path.is_file() or path.is_symlink():
            return ""
        try:
            return sha256_file(path)
        except OSError:
            return ""

    @staticmethod
    def _join_repository_path(repository: str, path: str) -> str:
        if not path:
            return ""
        if repository == ".":
            return path.replace("\\", "/")
        return f"{repository.rstrip('/')}/{path.replace('\\', '/')}"


def parse_porcelain_v1_z(value: bytes) -> tuple[tuple[str, str, str], ...]:
    fields = value.split(b"\x00")
    result: list[tuple[str, str, str]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        decoded = field.decode("utf-8", errors="surrogateescape")
        if len(decoded) < 3 or decoded[2] != " ":
            raise WorkspaceError(
                WorkspaceErrorCode.GIT_QUERY_FAILED,
                "Git porcelain output did not match the bounded parser contract.",
                operation="parse_git_status",
                actual=decoded[:32],
            )
        status_code = decoded[:2]
        path = decoded[3:].replace("\\", "/")
        original_path = ""
        if "R" in status_code or "C" in status_code:
            if index >= len(fields) or not fields[index]:
                raise WorkspaceError(
                    WorkspaceErrorCode.GIT_QUERY_FAILED,
                    "Git rename status omitted its original path.",
                    operation="parse_git_status",
                )
            original_path = fields[index].decode("utf-8", errors="surrogateescape").replace("\\", "/")
            index += 1
        result.append((status_code, path, original_path))
    return tuple(result)


def _dirty_kind(status_code: str) -> WorkspaceDirtyKind:
    if status_code == "??":
        return WorkspaceDirtyKind.UNTRACKED
    if status_code == "!!":
        return WorkspaceDirtyKind.IGNORED
    if "U" in status_code or status_code in {"AA", "DD"}:
        return WorkspaceDirtyKind.CONFLICTED
    if "R" in status_code or "C" in status_code:
        return WorkspaceDirtyKind.RENAMED
    if "D" in status_code:
        return WorkspaceDirtyKind.DELETED
    if "A" in status_code:
        return WorkspaceDirtyKind.ADDED
    if "M" in status_code or "T" in status_code:
        return WorkspaceDirtyKind.MODIFIED
    return WorkspaceDirtyKind.MODIFIED


def _claim_from_dict(value: Mapping[str, Any]) -> DirtyOwnershipClaim:
    return DirtyOwnershipClaim(
        workspace_id=str(value.get("workspace_id") or ""),
        path=str(value.get("path") or ""),
        owner=WorkspaceDirtyOwner(str(value.get("owner") or WorkspaceDirtyOwner.UNKNOWN.value)),
        worker_id=str(value.get("worker_id") or ""),
        operation_id=str(value.get("operation_id") or ""),
        base_hash=str(value.get("base_hash") or ""),
        claimed_at=str(value.get("claimed_at") or utc_now()),
        metadata=dict(value.get("metadata") or {}),
    )
