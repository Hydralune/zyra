from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .atomic import file_identity, open_regular_file_no_follow, read_descriptor
from .errors import WorkspaceError, WorkspaceErrorCode
from .models import (
    FileReadRecord,
    FileWritePrecondition,
    WorkspaceReadMode,
    combine_ranges,
    utc_now,
)
from .store import WorkspaceBindingStore


@dataclass(frozen=True, slots=True)
class WorkspaceReadResult:
    content: bytes
    record: FileReadRecord
    truncated: bool = False
    encoding: str = "binary"

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "content_bytes": len(self.content),
            "truncated": self.truncated,
            "encoding": self.encoding,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceWriteValidation:
    valid: bool
    record: FileReadRecord | None
    current_hash: str
    current_mtime_ns: int
    current_size: int
    current_identity: str
    path_exists: bool
    findings: tuple[str, ...] = ()

    def require_valid(self, *, workspace_id: str, path: str) -> None:
        if self.valid:
            return
        finding = self.findings[0] if self.findings else WorkspaceErrorCode.READ_REQUIRED.value
        try:
            code = WorkspaceErrorCode(finding)
        except ValueError:
            code = WorkspaceErrorCode.READ_REQUIRED
        raise WorkspaceError(
            code,
            "Workspace write precondition no longer matches the live file.",
            workspace_id=workspace_id,
            operation="validate_write_precondition",
            path=path,
            metadata={"findings": list(self.findings)},
        )


class WorkspaceFileStateRuntime:
    def __init__(self, store: WorkspaceBindingStore) -> None:
        self.store = store

    def read(
        self,
        *,
        workspace_id: str,
        path: str,
        physical_path: str | Path,
        owner_epoch: int,
        lease_id: str,
        mode: WorkspaceReadMode = WorkspaceReadMode.FULL,
        start: int = 0,
        length: int | None = None,
        max_bytes: int = 16 * 1024 * 1024,
        metadata: Mapping[str, Any] | None = None,
    ) -> WorkspaceReadResult:
        binding = self.store.require_binding(workspace_id)
        if binding.owner_epoch != owner_epoch:
            raise WorkspaceError(
                WorkspaceErrorCode.OWNER_EPOCH_STALE,
                "Workspace read used a stale owner epoch.",
                workspace_id=workspace_id,
                operation="read_file",
                path=path,
                expected=binding.owner_epoch,
                actual=owner_epoch,
            )
        selected = Path(physical_path)
        previous = self.store.get_read_record(workspace_id, path)
        epoch = (previous.read_epoch + 1) if previous is not None else 1
        if not selected.exists():
            record = FileReadRecord(
                workspace_id=workspace_id,
                path=path,
                read_epoch=epoch,
                mode=WorkspaceReadMode.METADATA,
                base_hash="",
                base_mtime_ns=0,
                base_size=0,
                file_identity="absent",
                fully_read=False,
                owner_epoch=owner_epoch,
                lease_id=lease_id,
                metadata={**dict(metadata or {}), "exists": False},
            )
            self.store.put_read_record(record)
            return WorkspaceReadResult(content=b"", record=record, encoding="binary")
        with open_regular_file_no_follow(selected) as opened:
            if opened.size > max_bytes and mode is WorkspaceReadMode.FULL:
                raise WorkspaceError(
                    WorkspaceErrorCode.SINGLE_FILE_LIMIT_EXCEEDED,
                    "Workspace full read exceeds its bounded read budget.",
                    workspace_id=workspace_id,
                    operation="read_file",
                    path=path,
                    expected=max_bytes,
                    actual=opened.size,
                )
            if mode is WorkspaceReadMode.METADATA:
                content = b""
                ranges: tuple[tuple[int, int], ...] = ()
                fully_read = False
            elif mode is WorkspaceReadMode.RANGE:
                if length is None:
                    raise ValueError("range reads require a length")
                bounded_length = min(length, max_bytes)
                content = read_descriptor(opened.descriptor, start=start, length=bounded_length)
                ranges = combine_ranges((*((previous.ranges if previous else ())), (start, start + len(content))))
                fully_read = bool(start == 0 and len(content) == opened.size)
            else:
                content = read_descriptor(opened.descriptor, start=0, length=None)
                ranges = ((0, len(content)),)
                fully_read = True
            os.lseek(opened.descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(opened.descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(opened.descriptor)
            after_identity = f"{int(after.st_dev):x}:{int(after.st_ino):x}:{int(after.st_mode & 0o170000):x}"
            if after_identity != opened.identity or int(after.st_size) != opened.size or int(after.st_mtime_ns) != opened.mtime_ns:
                raise WorkspaceError(
                    WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                    "Workspace file changed while it was being read.",
                    workspace_id=workspace_id,
                    operation="read_file",
                    path=path,
                )
            record = FileReadRecord(
                workspace_id=workspace_id,
                path=path,
                read_epoch=epoch,
                mode=mode,
                base_hash=digest.hexdigest(),
                base_mtime_ns=opened.mtime_ns,
                base_size=opened.size,
                file_identity=opened.identity,
                ranges=ranges,
                fully_read=fully_read,
                owner_epoch=owner_epoch,
                lease_id=lease_id,
                metadata={**dict(metadata or {}), "exists": True},
            )
        self.store.put_read_record(record)
        return WorkspaceReadResult(
            content=content,
            record=record,
            truncated=(mode is WorkspaceReadMode.RANGE and len(content) < opened.size),
            encoding="binary",
        )

    def precondition_for_latest(
        self,
        *,
        workspace_id: str,
        path: str,
        owner_epoch: int,
        lease_id: str,
        require_full_read: bool = True,
        allow_create: bool = False,
        expected_absent: bool = False,
        requested_ranges: tuple[tuple[int, int], ...] = (),
    ) -> FileWritePrecondition:
        record = self.store.get_read_record(workspace_id, path)
        if record is None:
            raise WorkspaceError(
                WorkspaceErrorCode.READ_REQUIRED,
                "Workspace writes require a prior read receipt.",
                workspace_id=workspace_id,
                operation="create_write_precondition",
                path=path,
            )
        return FileWritePrecondition(
            workspace_id=workspace_id,
            path=path,
            expected_read_epoch=record.read_epoch,
            expected_base_hash=record.base_hash,
            expected_base_mtime_ns=record.base_mtime_ns,
            expected_file_identity=record.file_identity,
            expected_owner_epoch=owner_epoch,
            lease_id=lease_id,
            require_full_read=require_full_read,
            allow_create=allow_create,
            expected_absent=expected_absent,
            requested_ranges=requested_ranges,
        )

    def validate_write(
        self,
        precondition: FileWritePrecondition,
        *,
        physical_path: str | Path,
    ) -> WorkspaceWriteValidation:
        binding = self.store.require_binding(precondition.workspace_id)
        findings: list[str] = []
        if binding.owner_epoch != precondition.expected_owner_epoch:
            findings.append(WorkspaceErrorCode.OWNER_EPOCH_STALE.value)
        if binding.lease_id != precondition.lease_id:
            findings.append(WorkspaceErrorCode.LEASE_OWNER_MISMATCH.value)
        record = self.store.get_read_record(precondition.workspace_id, precondition.path)
        if record is None:
            findings.append(WorkspaceErrorCode.READ_REQUIRED.value)
        else:
            if record.read_epoch != precondition.expected_read_epoch:
                findings.append(WorkspaceErrorCode.READ_EPOCH_STALE.value)
            if record.owner_epoch != precondition.expected_owner_epoch:
                findings.append(WorkspaceErrorCode.OWNER_EPOCH_STALE.value)
            if (
                precondition.require_full_read
                and not record.fully_read
                and not (precondition.expected_absent and record.file_identity == "absent")
            ):
                findings.append(WorkspaceErrorCode.READ_INCOMPLETE.value)
            if not precondition.require_full_read:
                for left, right in precondition.requested_ranges:
                    if not record.covers(left, right):
                        findings.append(WorkspaceErrorCode.READ_INCOMPLETE.value)
                        break

        target = Path(physical_path)
        exists = target.exists()
        if not exists:
            identity = "absent"
            current_hash = ""
            mtime_ns = 0
            size = 0
            if not precondition.allow_create and not precondition.expected_absent:
                findings.append(WorkspaceErrorCode.FILE_IDENTITY_CHANGED.value)
            if precondition.expected_file_identity not in {"", "absent"}:
                findings.append(WorkspaceErrorCode.FILE_IDENTITY_CHANGED.value)
        else:
            identity = file_identity(target)
            info = target.lstat()
            mtime_ns = int(info.st_mtime_ns)
            size = int(info.st_size)
            if precondition.expected_absent:
                findings.append(WorkspaceErrorCode.FILE_IDENTITY_CHANGED.value)
            if identity != precondition.expected_file_identity:
                findings.append(WorkspaceErrorCode.FILE_IDENTITY_CHANGED.value)
            digest = hashlib.sha256()
            with open_regular_file_no_follow(target) as opened:
                while True:
                    chunk = os.read(opened.descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            current_hash = digest.hexdigest()
            if current_hash != precondition.expected_base_hash:
                findings.append(WorkspaceErrorCode.BASE_HASH_STALE.value)
            if mtime_ns != precondition.expected_base_mtime_ns:
                findings.append(WorkspaceErrorCode.BASE_MTIME_STALE.value)
        return WorkspaceWriteValidation(
            valid=not findings,
            record=record,
            current_hash=current_hash,
            current_mtime_ns=mtime_ns,
            current_size=size,
            current_identity=identity,
            path_exists=exists,
            findings=tuple(dict.fromkeys(findings)),
        )

    def commit_write(
        self,
        *,
        workspace_id: str,
        path: str,
        physical_path: str | Path,
        owner_epoch: int,
        lease_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> FileReadRecord:
        result = self.read(
            workspace_id=workspace_id,
            path=path,
            physical_path=physical_path,
            owner_epoch=owner_epoch,
            lease_id=lease_id,
            mode=WorkspaceReadMode.FULL,
            max_bytes=self.store.require_binding(workspace_id).quota.max_single_file_bytes,
            metadata={**dict(metadata or {}), "source": "write_commit"},
        )
        return result.record

    def invalidate(self, workspace_id: str, *, paths: tuple[str, ...] | None = None) -> int:
        return self.store.invalidate_read_records(workspace_id, paths=paths)
