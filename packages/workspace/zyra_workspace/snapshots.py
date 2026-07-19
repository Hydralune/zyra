from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .atomic import atomic_write_bytes, atomic_write_json, file_identity, fsync_directory, sha256_file
from .errors import WorkspaceError, WorkspaceErrorCode
from .models import (
    DirtyPathRecord,
    NestedRepositoryRef,
    SnapshotEntry,
    SnapshotEntryKind,
    SnapshotState,
    WorkspaceSnapshot,
    new_workspace_id,
    snapshot_from_dict,
    stable_digest,
    utc_now,
)
from .paths import WorkspacePathSafetyPolicy


SNAPSHOT_MANIFEST_SCHEMA = "zyra.workspace-snapshot-manifest.v1"
SNAPSHOT_COMMIT_SCHEMA = "zyra.workspace-snapshot-commit.v1"


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    snapshot_id: str
    valid: bool
    manifest_hash: str
    checked_entries: int
    checked_bytes: int
    missing_blobs: tuple[str, ...] = ()
    corrupt_blobs: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()

    def require_valid(self, *, workspace_id: str = "") -> None:
        if self.valid:
            return
        raise WorkspaceError(
            WorkspaceErrorCode.SNAPSHOT_CORRUPT,
            "The workspace snapshot failed integrity verification.",
            workspace_id=workspace_id,
            operation="verify_snapshot",
            metadata=self.to_dict(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "valid": self.valid,
            "manifest_hash": self.manifest_hash,
            "checked_entries": self.checked_entries,
            "checked_bytes": self.checked_bytes,
            "missing_blobs": list(self.missing_blobs),
            "corrupt_blobs": list(self.corrupt_blobs),
            "findings": list(self.findings),
        }


@dataclass(frozen=True, slots=True)
class SnapshotRestoreResult:
    snapshot: WorkspaceSnapshot
    restored_root_identity: str
    restored_entries: int
    restored_bytes: int
    displaced_root_ref: str = ""
    cleanup_pending: bool = False
    completed_at: str = field(default_factory=utc_now)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot.snapshot_id,
            "workspace_id": self.snapshot.workspace_id,
            "restored_entries": self.restored_entries,
            "restored_bytes": self.restored_bytes,
            "cleanup_pending": self.cleanup_pending,
            "completed_at": self.completed_at,
        }


class WorkspaceSnapshotRuntime:
    """Content-addressed, committed snapshots with verify-before-mutate restore."""

    def __init__(
        self,
        root: str | Path,
        *,
        path_policy: WorkspacePathSafetyPolicy,
        reject_symlinks: bool = True,
        maximum_scan_entries: int = 200_000,
    ) -> None:
        self.root = Path(root)
        self.blob_root = self.root / "blobs" / "sha256"
        self.manifest_root = self.root / "manifests"
        self.staging_root = self.root / "staging"
        self.path_policy = path_policy
        self.reject_symlinks = reject_symlinks
        self.maximum_scan_entries = maximum_scan_entries
        self._lock = threading.RLock()

    def create(
        self,
        *,
        workspace_id: str,
        owner_epoch: int,
        lease_id: str,
        source_root: str | Path,
        parent_snapshot_id: str = "",
        baseline_id: str = "",
        dirty_paths: Iterable[DirtyPathRecord] = (),
        repository_refs: Iterable[NestedRepositoryRef] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> WorkspaceSnapshot:
        source = Path(source_root).resolve()
        if not source.is_dir() or source.is_symlink():
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "The workspace snapshot source root is missing or unsafe.",
                workspace_id=workspace_id,
                operation="create_snapshot",
            )
        snapshot_id = new_workspace_id("snap")
        with self._lock:
            entries = self._scan_and_store(source, workspace_id=workspace_id)
            manifest_payload = self._manifest_payload(
                snapshot_id=snapshot_id,
                workspace_id=workspace_id,
                owner_epoch=owner_epoch,
                lease_id=lease_id,
                entries=entries,
                parent_snapshot_id=parent_snapshot_id,
                baseline_id=baseline_id,
                dirty_paths=tuple(dirty_paths),
                repository_refs=tuple(repository_refs),
                metadata=metadata,
            )
            canonical = _canonical_json(manifest_payload)
            manifest_hash = hashlib.sha256(canonical).hexdigest()
            manifest_payload["manifest_hash"] = manifest_hash
            manifest_path = self.manifest_root / snapshot_id / "manifest.json"
            atomic_write_json(manifest_path, manifest_payload)
            commit = {
                "schema": SNAPSHOT_COMMIT_SCHEMA,
                "snapshot_id": snapshot_id,
                "workspace_id": workspace_id,
                "manifest_hash": manifest_hash,
                "manifest_file_hash": sha256_file(manifest_path),
                "committed_at": utc_now(),
            }
            atomic_write_json(manifest_path.with_name("COMMITTED.json"), commit)
            fsync_directory(manifest_path.parent)
            snapshot = self.load(snapshot_id)
            verification = self.verify(snapshot)
            verification.require_valid(workspace_id=workspace_id)
            return snapshot

    def load(self, snapshot_id: str) -> WorkspaceSnapshot:
        directory = self.manifest_root / snapshot_id
        manifest_path = directory / "manifest.json"
        commit_path = directory / "COMMITTED.json"
        if not manifest_path.exists() or not commit_path.exists():
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_NOT_FOUND,
                "A committed workspace snapshot was not found.",
                operation="load_snapshot",
                actual=snapshot_id,
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            commit = json.loads(commit_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_CORRUPT,
                "The workspace snapshot manifest could not be decoded.",
                operation="load_snapshot",
                actual=snapshot_id,
            ) from error
        if manifest.get("schema") != SNAPSHOT_MANIFEST_SCHEMA or commit.get("schema") != SNAPSHOT_COMMIT_SCHEMA:
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_CORRUPT,
                "The workspace snapshot manifest schema is unsupported.",
                operation="load_snapshot",
                actual=snapshot_id,
            )
        if commit.get("snapshot_id") != snapshot_id or manifest.get("snapshot_id") != snapshot_id:
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_CORRUPT,
                "The workspace snapshot identity does not match its directory.",
                operation="load_snapshot",
                actual=snapshot_id,
            )
        expected_file_hash = str(commit.get("manifest_file_hash") or "")
        if expected_file_hash != sha256_file(manifest_path):
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_CORRUPT,
                "The committed workspace snapshot manifest changed after commit.",
                operation="load_snapshot",
                actual=snapshot_id,
            )
        manifest_hash = str(manifest.pop("manifest_hash", ""))
        actual_hash = hashlib.sha256(_canonical_json(manifest)).hexdigest()
        manifest["manifest_hash"] = manifest_hash
        if manifest_hash != actual_hash or commit.get("manifest_hash") != manifest_hash:
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_CORRUPT,
                "The workspace snapshot manifest digest is invalid.",
                operation="load_snapshot",
                actual=snapshot_id,
            )
        manifest["state"] = SnapshotState.COMMITTED.value
        manifest["committed_at"] = str(commit.get("committed_at") or "")
        return snapshot_from_dict(manifest)

    def list(self, *, workspace_id: str = "") -> tuple[WorkspaceSnapshot, ...]:
        if not self.manifest_root.exists():
            return ()
        snapshots: list[WorkspaceSnapshot] = []
        for directory in sorted(self.manifest_root.iterdir()):
            if not directory.is_dir() or not (directory / "COMMITTED.json").exists():
                continue
            try:
                snapshot = self.load(directory.name)
            except WorkspaceError:
                continue
            if not workspace_id or snapshot.workspace_id == workspace_id:
                snapshots.append(snapshot)
        return tuple(sorted(snapshots, key=lambda item: (item.created_at, item.snapshot_id)))

    def verify(self, snapshot: WorkspaceSnapshot, *, verify_blob_content: bool = True) -> SnapshotVerification:
        missing: list[str] = []
        corrupt: list[str] = []
        findings: list[str] = []
        checked_bytes = 0
        manifest_directory = self.manifest_root / snapshot.snapshot_id
        if not (manifest_directory / "COMMITTED.json").exists():
            findings.append("commit_marker_missing")
        collision_paths: list[str] = []
        for entry in snapshot.entries:
            collision_paths.append(entry.relative_path)
            if entry.kind is not SnapshotEntryKind.FILE:
                continue
            checked_bytes += entry.size_bytes
            blob = self._blob_path(entry.content_hash)
            if not blob.is_file() or blob.is_symlink():
                missing.append(entry.content_hash)
                continue
            try:
                info = blob.stat()
                if int(info.st_size) != entry.size_bytes:
                    corrupt.append(entry.content_hash)
                elif verify_blob_content and sha256_file(blob) != entry.content_hash:
                    corrupt.append(entry.content_hash)
            except OSError:
                corrupt.append(entry.content_hash)
        try:
            self.path_policy.assert_no_collisions(collision_paths)
        except WorkspaceError:
            findings.append("path_collision")
        if snapshot.total_bytes != checked_bytes:
            findings.append("total_bytes_mismatch")
        if snapshot.file_count != sum(1 for item in snapshot.entries if item.kind is SnapshotEntryKind.FILE):
            findings.append("file_count_mismatch")
        return SnapshotVerification(
            snapshot_id=snapshot.snapshot_id,
            valid=not missing and not corrupt and not findings,
            manifest_hash=snapshot.manifest_hash,
            checked_entries=len(snapshot.entries),
            checked_bytes=checked_bytes,
            missing_blobs=tuple(sorted(set(missing))),
            corrupt_blobs=tuple(sorted(set(corrupt))),
            findings=tuple(findings),
        )

    def restore(
        self,
        *,
        snapshot: WorkspaceSnapshot,
        workspace_id: str,
        owner_epoch: int,
        target_root: str | Path,
        preserve_displaced: bool = True,
    ) -> SnapshotRestoreResult:
        if snapshot.workspace_id != workspace_id:
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_OWNER_MISMATCH,
                "The snapshot belongs to a different workspace.",
                workspace_id=workspace_id,
                operation="restore_snapshot",
                expected=workspace_id,
                actual=snapshot.workspace_id,
            )
        if snapshot.owner_epoch > owner_epoch:
            raise WorkspaceError(
                WorkspaceErrorCode.OWNER_EPOCH_STALE,
                "A snapshot from a future owner epoch cannot be restored.",
                workspace_id=workspace_id,
                operation="restore_snapshot",
                expected=owner_epoch,
                actual=snapshot.owner_epoch,
            )
        verification = self.verify(snapshot)
        verification.require_valid(workspace_id=workspace_id)
        target = Path(target_root).resolve()
        parent = target.parent
        if not parent.is_dir():
            raise WorkspaceError(
                WorkspaceErrorCode.RESTORE_FAILED,
                "The workspace restore parent does not exist.",
                workspace_id=workspace_id,
                operation="restore_snapshot",
            )
        staging = parent / f".{target.name}.zyra-restore-{snapshot.snapshot_id}-{secrets.token_hex(4)}"
        displaced = parent / f".{target.name}.zyra-displaced-{snapshot.snapshot_id}-{secrets.token_hex(4)}"
        with self._lock:
            try:
                staging.mkdir(mode=0o700)
                self._materialize(snapshot, staging)
                staged = self._verify_materialized(snapshot, staging)
                staged.require_valid(workspace_id=workspace_id)
                target_identity = file_identity(target)
                if target.exists():
                    os.replace(target, displaced)
                try:
                    os.replace(staging, target)
                    fsync_directory(parent)
                except Exception:
                    if displaced.exists() and not target.exists():
                        os.replace(displaced, target)
                    raise
                cleanup_pending = False
                displaced_ref = ""
                if displaced.exists():
                    displaced_ref = displaced.name
                    if preserve_displaced:
                        cleanup_pending = True
                    else:
                        shutil.rmtree(displaced)
                        displaced_ref = ""
                return SnapshotRestoreResult(
                    snapshot=snapshot,
                    restored_root_identity=file_identity(target),
                    restored_entries=len(snapshot.entries),
                    restored_bytes=snapshot.total_bytes,
                    displaced_root_ref=displaced_ref,
                    cleanup_pending=cleanup_pending,
                )
            except WorkspaceError:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                raise
            except Exception as error:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                raise WorkspaceError(
                    WorkspaceErrorCode.RESTORE_FAILED,
                    "The workspace snapshot restore failed before commit.",
                    workspace_id=workspace_id,
                    operation="restore_snapshot",
                    metadata={"exception_type": type(error).__name__},
                ) from error

    def restore_write_set(
        self,
        *,
        snapshot: WorkspaceSnapshot,
        workspace_id: str,
        owner_epoch: int,
        target_root: str | Path,
        candidate_paths: Sequence[str],
        expected_live_hashes: Mapping[str, str | None],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Restore only transaction-owned paths without erasing user changes.

        ``expected_live_hashes`` binds each completed mutation to the bytes it
        wrote; ``None`` means the transaction deleted the path.  Unknown or
        externally changed live values are reported as conflicts and kept in
        place for recovery instead of being overwritten by a whole-tree
        snapshot restore.
        """

        if snapshot.workspace_id != workspace_id or snapshot.owner_epoch > owner_epoch:
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_OWNER_MISMATCH,
                "The write-set rollback snapshot does not belong to the live workspace epoch.",
                workspace_id=workspace_id,
                operation="restore_workspace_write_set",
            )
        self.verify(snapshot).require_valid(workspace_id=workspace_id)
        root = Path(target_root).resolve()
        entries = {item.relative_path: item for item in snapshot.entries}
        restored: list[str] = []
        conflicts: list[str] = []
        with self._lock:
            for raw_path in dict.fromkeys(str(item) for item in candidate_paths):
                logical_path, parts = self.path_policy.validate_logical_path(raw_path)
                if not parts:
                    conflicts.append(logical_path)
                    continue
                target = root.joinpath(*parts).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    conflicts.append(logical_path)
                    continue
                snapshot_entry = entries.get(logical_path)
                snapshot_hash = (
                    snapshot_entry.content_hash
                    if snapshot_entry is not None and snapshot_entry.kind is SnapshotEntryKind.FILE
                    else None
                )
                if target.exists():
                    if target.is_symlink() or not target.is_file():
                        live_hash: str | None = "<non-regular>"
                    else:
                        live_hash = sha256_file(target)
                else:
                    live_hash = None
                if logical_path not in expected_live_hashes:
                    # The failed mutation may or may not have reached disk.
                    # Only a value still equal to the undo snapshot is known
                    # safe; everything else is retained as a conflict.
                    if live_hash != snapshot_hash:
                        conflicts.append(logical_path)
                    continue
                if live_hash != expected_live_hashes[logical_path]:
                    conflicts.append(logical_path)
                    continue
                if snapshot_entry is None:
                    if target.exists():
                        target.unlink()
                        fsync_directory(target.parent)
                elif snapshot_entry.kind is SnapshotEntryKind.FILE:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(
                        target,
                        self._blob_path(snapshot_entry.content_hash).read_bytes(),
                        mode=snapshot_entry.mode,
                    )
                else:
                    conflicts.append(logical_path)
                    continue
                restored.append(logical_path)
        return tuple(restored), tuple(conflicts)

    def prune_uncommitted(self) -> tuple[str, ...]:
        removed: list[str] = []
        with self._lock:
            for root in (self.manifest_root, self.staging_root):
                if not root.exists():
                    continue
                for entry in root.iterdir():
                    if not entry.is_dir():
                        continue
                    if root == self.manifest_root and (entry / "COMMITTED.json").exists():
                        continue
                    shutil.rmtree(entry, ignore_errors=False)
                    removed.append(entry.name)
        return tuple(removed)

    def delete(self, snapshot_id: str, *, require_superseded: bool = False) -> bool:
        directory = self.manifest_root / snapshot_id
        if not directory.exists():
            return False
        snapshot = self.load(snapshot_id)
        if require_superseded and snapshot.state is not SnapshotState.SUPERSEDED:
            raise WorkspaceError(
                WorkspaceErrorCode.CLEANUP_CONFLICT,
                "Only superseded snapshots may be deleted by this cleanup policy.",
                workspace_id=snapshot.workspace_id,
                operation="delete_snapshot",
            )
        shutil.rmtree(directory)
        return True

    def _scan_and_store(self, source: Path, *, workspace_id: str) -> tuple[SnapshotEntry, ...]:
        entries: list[SnapshotEntry] = []
        observed = 0
        for current, directories, files in os.walk(source, topdown=True, followlinks=False):
            current_path = Path(current)
            directories.sort()
            files.sort()
            observed += len(directories) + len(files)
            if observed > self.maximum_scan_entries:
                raise WorkspaceError(
                    WorkspaceErrorCode.QUOTA_EXCEEDED,
                    "Workspace snapshot scanning exceeded its bounded entry count.",
                    workspace_id=workspace_id,
                    operation="create_snapshot",
                    expected=self.maximum_scan_entries,
                    actual=observed,
                )
            relative_current = current_path.relative_to(source)
            for directory_name in tuple(directories):
                path = current_path / directory_name
                info = path.lstat()
                relative = (relative_current / directory_name).as_posix()
                if stat.S_ISLNK(info.st_mode):
                    directories.remove(directory_name)
                    if self.reject_symlinks:
                        raise WorkspaceError(
                            WorkspaceErrorCode.SYMLINK_ESCAPE,
                            "Workspace snapshots reject directory symlinks.",
                            workspace_id=workspace_id,
                            operation="create_snapshot",
                            path=relative,
                        )
                elif not stat.S_ISDIR(info.st_mode):
                    raise WorkspaceError(
                        WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                        "Workspace snapshots reject non-directory traversal entries.",
                        workspace_id=workspace_id,
                        operation="create_snapshot",
                        path=relative,
                    )
                entries.append(
                    SnapshotEntry(
                        relative_path=relative,
                        kind=SnapshotEntryKind.DIRECTORY,
                        content_hash="",
                        size_bytes=0,
                        mtime_ns=int(info.st_mtime_ns),
                        mode=stat.S_IMODE(info.st_mode),
                        file_identity=file_identity(path),
                    )
                )
            for file_name in files:
                path = current_path / file_name
                relative = (relative_current / file_name).as_posix()
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    if self.reject_symlinks:
                        raise WorkspaceError(
                            WorkspaceErrorCode.SYMLINK_ESCAPE,
                            "Workspace snapshots reject file symlinks.",
                            workspace_id=workspace_id,
                            operation="create_snapshot",
                            path=relative,
                        )
                    target = os.readlink(path)
                    entries.append(
                        SnapshotEntry(
                            relative_path=relative,
                            kind=SnapshotEntryKind.SYMLINK,
                            content_hash=hashlib.sha256(target.encode("utf-8")).hexdigest(),
                            size_bytes=len(target.encode("utf-8")),
                            mtime_ns=int(info.st_mtime_ns),
                            mode=stat.S_IMODE(info.st_mode),
                            link_target=target,
                            file_identity=file_identity(path),
                        )
                    )
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise WorkspaceError(
                        WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                        "Workspace snapshots reject devices, pipes, and sockets.",
                        workspace_id=workspace_id,
                        operation="create_snapshot",
                        path=relative,
                    )
                before_identity = file_identity(path)
                digest = sha256_file(path)
                self._store_blob(path, digest)
                after = path.lstat()
                if file_identity(path) != before_identity or int(after.st_size) != int(info.st_size):
                    raise WorkspaceError(
                        WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                        "A workspace file changed while its snapshot was being created.",
                        workspace_id=workspace_id,
                        operation="create_snapshot",
                        path=relative,
                    )
                entries.append(
                    SnapshotEntry(
                        relative_path=relative,
                        kind=SnapshotEntryKind.FILE,
                        content_hash=digest,
                        size_bytes=int(info.st_size),
                        mtime_ns=int(info.st_mtime_ns),
                        mode=stat.S_IMODE(info.st_mode),
                        blob_ref=f"sha256/{digest[:2]}/{digest}",
                        file_identity=file_identity(path),
                    )
                )
        entries.sort(key=lambda item: (item.relative_path.count("/"), item.relative_path, item.kind.value))
        self.path_policy.assert_no_collisions(item.relative_path for item in entries)
        return tuple(entries)

    def _store_blob(self, source: Path, digest: str) -> Path:
        destination = self._blob_path(digest)
        if destination.exists():
            if destination.is_symlink() or sha256_file(destination) != digest:
                raise WorkspaceError(
                    WorkspaceErrorCode.SNAPSHOT_CORRUPT,
                    "An existing workspace snapshot blob does not match its digest.",
                    operation="store_snapshot_blob",
                    actual=digest,
                )
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{digest}.{secrets.token_hex(6)}.tmp")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            if sha256_file(temporary) != digest:
                raise WorkspaceError(
                    WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                    "A workspace file changed while its blob was being staged.",
                    operation="store_snapshot_blob",
                )
            try:
                os.replace(temporary, destination)
            except FileExistsError:
                temporary.unlink(missing_ok=True)
            fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _materialize(self, snapshot: WorkspaceSnapshot, staging: Path) -> None:
        for entry in snapshot.entries:
            target = staging.joinpath(*Path(entry.relative_path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.kind is SnapshotEntryKind.DIRECTORY:
                target.mkdir(exist_ok=True)
            elif entry.kind is SnapshotEntryKind.FILE:
                content = self._blob_path(entry.content_hash).read_bytes()
                atomic_write_bytes(target, content, mode=entry.mode)
            else:
                if self.reject_symlinks:
                    raise WorkspaceError(
                        WorkspaceErrorCode.SNAPSHOT_CORRUPT,
                        "A symlink entry cannot be restored under the active workspace policy.",
                        workspace_id=snapshot.workspace_id,
                        operation="materialize_snapshot",
                        path=entry.relative_path,
                    )
                os.symlink(entry.link_target, target)
        for entry in reversed(snapshot.entries):
            target = staging.joinpath(*Path(entry.relative_path).parts)
            if entry.kind is not SnapshotEntryKind.SYMLINK:
                try:
                    os.chmod(target, entry.mode)
                    os.utime(target, ns=(entry.mtime_ns, entry.mtime_ns), follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass

    def _verify_materialized(self, snapshot: WorkspaceSnapshot, staging: Path) -> SnapshotVerification:
        missing: list[str] = []
        corrupt: list[str] = []
        checked_bytes = 0
        for entry in snapshot.entries:
            path = staging.joinpath(*Path(entry.relative_path).parts)
            if entry.kind is SnapshotEntryKind.DIRECTORY:
                if not path.is_dir() or path.is_symlink():
                    missing.append(entry.relative_path)
                continue
            if entry.kind is SnapshotEntryKind.FILE:
                checked_bytes += entry.size_bytes
                if not path.is_file() or path.is_symlink():
                    missing.append(entry.relative_path)
                elif sha256_file(path) != entry.content_hash:
                    corrupt.append(entry.relative_path)
        return SnapshotVerification(
            snapshot_id=snapshot.snapshot_id,
            valid=not missing and not corrupt and checked_bytes == snapshot.total_bytes,
            manifest_hash=snapshot.manifest_hash,
            checked_entries=len(snapshot.entries),
            checked_bytes=checked_bytes,
            missing_blobs=tuple(missing),
            corrupt_blobs=tuple(corrupt),
            findings=() if checked_bytes == snapshot.total_bytes else ("total_bytes_mismatch",),
        )

    def _manifest_payload(
        self,
        *,
        snapshot_id: str,
        workspace_id: str,
        owner_epoch: int,
        lease_id: str,
        entries: tuple[SnapshotEntry, ...],
        parent_snapshot_id: str,
        baseline_id: str,
        dirty_paths: tuple[DirtyPathRecord, ...],
        repository_refs: tuple[NestedRepositoryRef, ...],
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        total_bytes = sum(item.size_bytes for item in entries if item.kind is SnapshotEntryKind.FILE)
        file_count = sum(1 for item in entries if item.kind is SnapshotEntryKind.FILE)
        directory_count = sum(1 for item in entries if item.kind is SnapshotEntryKind.DIRECTORY)
        root_tree_hash = stable_digest([item.to_dict() for item in entries])
        return {
            "schema": SNAPSHOT_MANIFEST_SCHEMA,
            "snapshot_id": snapshot_id,
            "workspace_id": workspace_id,
            "owner_epoch": owner_epoch,
            "lease_id": lease_id,
            "state": SnapshotState.PREPARING.value,
            "root_tree_hash": root_tree_hash,
            "entries": [item.to_dict() for item in entries],
            "parent_snapshot_id": parent_snapshot_id,
            "baseline_id": baseline_id,
            "created_at": utc_now(),
            "committed_at": "",
            "total_bytes": total_bytes,
            "file_count": file_count,
            "directory_count": directory_count,
            "dirty_paths": [item.to_dict() for item in dirty_paths],
            "repository_refs": [item.to_dict() for item in repository_refs],
            "metadata": {**dict(metadata or {}), "content_addressed": True, "commit_marker_required": True},
        }

    def _blob_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_CORRUPT,
                "Workspace snapshot blob references must be SHA-256 digests.",
                operation="resolve_snapshot_blob",
                actual=digest,
            )
        return self.blob_root / digest[:2] / digest


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
