from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .file_policy import (
    BrowserFilePolicy,
    BrowserFilePolicyError,
    CompletedFile,
    FileIntent,
    FileReceipt,
    assert_contained,
    validate_filename,
)
from .models import digest_value, stable_id


class DownloadGuardError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class DownloadState(StrEnum):
    ARMED = "armed"
    STARTED = "started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class DownloadEvent:
    guid: str
    state: DownloadState
    suggested_filename: str = ""
    received_bytes: int = 0
    total_bytes: int = 0
    url: str = ""

    def __post_init__(self) -> None:
        if not self.guid:
            raise ValueError("browser download event requires guid")
        if min(self.received_bytes, self.total_bytes) < 0:
            raise ValueError("browser download byte counters cannot be negative")

    def public_dict(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "state": str(self.state),
            "suggested_filename": self.suggested_filename,
            "received_bytes": self.received_bytes,
            "total_bytes": self.total_bytes,
            "url_digest": digest_value(self.url) if self.url else "",
        }


@dataclass(frozen=True, slots=True)
class DownloadLease:
    lease_id: str
    action_id: str
    receipt: FileReceipt
    quarantine_root: str
    browser_context_id: str
    grant_tool_use_id: str
    maximum_bytes: int

    def __post_init__(self) -> None:
        if self.receipt.intent != FileIntent.DOWNLOAD:
            raise ValueError("browser download lease requires a download file receipt")
        if not self.grant_tool_use_id or self.maximum_bytes < 1:
            raise ValueError("browser download lease requires consumed grant identity and quota")

    def public_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "action_id": self.action_id,
            "file_receipt_id": self.receipt.receipt_id,
            "quarantine_root": self.quarantine_root,
            "browser_context_id": self.browser_context_id,
            "grant_tool_use_id": self.grant_tool_use_id,
            "maximum_bytes": self.maximum_bytes,
        }


@dataclass(slots=True)
class DownloadProgress:
    guid: str
    filename: str
    path: Path
    state: DownloadState = DownloadState.STARTED
    received_bytes: int = 0
    total_bytes: int = 0
    events: list[DownloadEvent] = field(default_factory=list)


class DownloadControlPort(Protocol):
    def arm(self, *, browser_context_id: str, download_path: str, events_enabled: bool) -> None: ...

    def cancel(self, guid: str) -> None: ...

    def disarm(self, *, browser_context_id: str) -> None: ...


@dataclass(slots=True)
class RecordingDownloadControlPort:
    operations: list[dict[str, Any]] = field(default_factory=list)

    def arm(self, *, browser_context_id: str, download_path: str, events_enabled: bool) -> None:
        self.operations.append(
            {
                "operation": "arm",
                "browser_context_id": browser_context_id,
                "download_path": download_path,
                "events_enabled": events_enabled,
            }
        )

    def cancel(self, guid: str) -> None:
        self.operations.append({"operation": "cancel", "guid": guid})

    def disarm(self, *, browser_context_id: str) -> None:
        self.operations.append({"operation": "disarm", "browser_context_id": browser_context_id})


class BrowserDownloadGuard:
    """Grant-scoped native download behavior and quarantine importer.

    Browser download permission is never enabled at session startup.  ``arm``
    requires a consumed permission tool-use id and an exact owned destination
    receipt.  Chrome writes only to a per-lease quarantine directory; completed
    bytes are copied through ``BrowserFilePolicy`` into the final artifact root.
    """

    def __init__(
        self,
        *,
        file_policy: BrowserFilePolicy,
        control_port: DownloadControlPort,
        quarantine_parent: str | Path,
        disabled: bool = False,
    ) -> None:
        self.file_policy = file_policy
        self.control_port = control_port
        self.quarantine_parent = Path(quarantine_parent).resolve(strict=False)
        self.disabled = disabled
        self._leases: dict[str, DownloadLease] = {}
        self._progress: dict[str, DownloadProgress] = {}
        self._lock = threading.RLock()

    def arm(
        self,
        *,
        action_id: str,
        receipt: FileReceipt,
        browser_context_id: str,
        consumed_permission_tool_use_id: str,
    ) -> DownloadLease:
        if self.disabled:
            raise DownloadGuardError("download_guard_disabled", "browser download guard is disabled")
        if not consumed_permission_tool_use_id:
            raise DownloadGuardError("download_grant_missing", "native download cannot be armed before grant consumption")
        self.file_policy.revalidate(receipt)
        lease_id = stable_id(
            "brdownload",
            action_id,
            receipt.receipt_id,
            browser_context_id,
            consumed_permission_tool_use_id,
        )
        quarantine = self.quarantine_parent / lease_id
        assert_contained(quarantine, self.quarantine_parent, resolve_leaf=False)
        with self._lock:
            if lease_id in self._leases:
                raise DownloadGuardError("download_lease_replayed", "browser download lease is already armed")
            quarantine.mkdir(parents=True, exist_ok=False)
            lease = DownloadLease(
                lease_id=lease_id,
                action_id=action_id,
                receipt=receipt,
                quarantine_root=str(quarantine),
                browser_context_id=browser_context_id,
                grant_tool_use_id=consumed_permission_tool_use_id,
                maximum_bytes=receipt.quota_bytes,
            )
            self._leases[lease_id] = lease
        try:
            self.control_port.arm(
                browser_context_id=browser_context_id,
                download_path=str(quarantine),
                events_enabled=True,
            )
        except Exception:
            with self._lock:
                self._leases.pop(lease_id, None)
            raise
        return lease

    def on_will_begin(self, lease: DownloadLease, event: DownloadEvent) -> None:
        self._require_lease(lease)
        filename = validate_filename(event.suggested_filename or lease.receipt.expected_name)
        if filename != lease.receipt.expected_name:
            self.control_port.cancel(event.guid)
            raise DownloadGuardError(
                "download_filename_mismatch",
                "browser suggested filename differs from the approved destination",
                details={"approved": lease.receipt.expected_name, "suggested": filename},
            )
        path = Path(lease.quarantine_root) / event.guid
        assert_contained(path, Path(lease.quarantine_root), resolve_leaf=False)
        with self._lock:
            if event.guid in self._progress:
                raise DownloadGuardError("download_guid_replayed", "browser download guid is already tracked")
            self._progress[event.guid] = DownloadProgress(
                guid=event.guid,
                filename=filename,
                path=path,
                state=DownloadState.STARTED,
                received_bytes=event.received_bytes,
                total_bytes=event.total_bytes,
                events=[event],
            )

    def on_progress(self, lease: DownloadLease, event: DownloadEvent) -> CompletedFile | None:
        self._require_lease(lease)
        with self._lock:
            progress = self._progress.get(event.guid)
            if progress is None:
                self.control_port.cancel(event.guid)
                raise DownloadGuardError("download_untracked", "browser download progress has no approved start event")
            if event.received_bytes < progress.received_bytes:
                self.control_port.cancel(event.guid)
                raise DownloadGuardError("download_progress_regressed", "browser download byte counter regressed")
            progress.received_bytes = event.received_bytes
            progress.total_bytes = event.total_bytes
            progress.state = event.state
            progress.events.append(event)
            if event.received_bytes > lease.maximum_bytes or event.total_bytes > lease.maximum_bytes:
                self.control_port.cancel(event.guid)
                progress.state = DownloadState.REJECTED
                raise DownloadGuardError("download_quota_exceeded", "native browser download exceeds approved quota")
        if event.state == DownloadState.COMPLETED:
            return self._import_completed(lease, progress)
        if event.state in {DownloadState.CANCELLED, DownloadState.REJECTED}:
            return None
        return None

    def disarm(self, lease: DownloadLease) -> None:
        self._require_lease(lease)
        try:
            self.control_port.disarm(browser_context_id=lease.browser_context_id)
        finally:
            with self._lock:
                self._leases.pop(lease.lease_id, None)

    def _import_completed(self, lease: DownloadLease, progress: DownloadProgress) -> CompletedFile:
        source = progress.path
        assert_contained(source, Path(lease.quarantine_root), resolve_leaf=True)
        if not source.is_file() or source.is_symlink():
            raise DownloadGuardError("download_file_invalid", "completed browser download is not a regular quarantine file")
        size = source.stat().st_size
        if size != progress.received_bytes or size > lease.maximum_bytes:
            raise DownloadGuardError("download_size_mismatch", "completed browser download size does not match progress")
        hasher = hashlib.sha256()
        try:
            with self.file_policy.open_download(lease.receipt) as writer, source.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    writer.write(chunk)
                completed = writer.complete()
        except BrowserFilePolicyError as exc:
            raise DownloadGuardError(exc.code, str(exc), details=exc.details) from exc
        if completed.sha256 != hasher.hexdigest():
            raise DownloadGuardError("download_digest_mismatch", "download digest changed during quarantine import")
        with self._lock:
            self._progress.pop(progress.guid, None)
        try:
            source.unlink()
        except OSError:
            pass
        return completed

    def _require_lease(self, lease: DownloadLease) -> None:
        if self.disabled:
            raise DownloadGuardError("download_guard_disabled", "browser download guard is disabled")
        with self._lock:
            stored = self._leases.get(lease.lease_id)
        if stored != lease:
            raise DownloadGuardError("download_lease_invalid", "browser download lease is missing or stale")
