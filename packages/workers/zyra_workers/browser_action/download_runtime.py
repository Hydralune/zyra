from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, now_iso

from .download_guard import (
    BrowserDownloadGuard,
    DownloadEvent,
    DownloadGuardError,
    DownloadLease,
    DownloadState,
)
from .event_port import ArtifactPort, causal_metadata
from .executor import BrowserExecutionError, ExecutionContext, require_point
from .file_policy import CompletedFile
from .integration_models import BrowserActionIntegrationError, DispatchBoundary, PlanPhase
from .models import digest_value
from .session_adapter import SessionBoundCdpTransport


@dataclass(frozen=True, slots=True)
class CompletedBrowserDownload:
    action_id: str
    browser_session_id: str
    guid: str
    file: CompletedFile
    artifact: ArtifactRef
    source_url_digest: str
    completed_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.action_id or not self.browser_session_id or not self.guid:
            raise ValueError("completed browser download identity is incomplete")
        if self.artifact.artifact_id == "":
            raise ValueError("completed browser download requires canonical artifact")

    def public_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "browser_session_id": self.browser_session_id,
            "guid": self.guid,
            "file": self.file.to_dict(),
            "artifact_id": self.artifact.artifact_id,
            "source_url_digest": self.source_url_digest,
            "completed_at": self.completed_at,
        }


class BrowserDownloadLedger:
    """Session-scoped references to canonical artifacts, never a file scanner."""

    def __init__(self, browser_session_id: str, *, maximum_records: int = 4096) -> None:
        if not browser_session_id or maximum_records < 1:
            raise ValueError("browser download ledger identity/limit is invalid")
        self.browser_session_id = browser_session_id
        self.maximum_records = maximum_records
        self._records: list[CompletedBrowserDownload] = []
        self._guids: set[str] = set()
        self._lock = threading.RLock()

    def append(self, record: CompletedBrowserDownload) -> None:
        if record.browser_session_id != self.browser_session_id:
            raise BrowserExecutionError("download_session_mismatch", "download belongs to another browser session")
        with self._lock:
            if record.guid in self._guids:
                raise BrowserExecutionError("download_guid_replayed", "download guid is already committed")
            self._records.append(record)
            self._guids.add(record.guid)
            if len(self._records) > self.maximum_records:
                removed = self._records.pop(0)
                self._guids.discard(removed.guid)

    def list(self, *, maximum: int, maximum_total_bytes: int) -> tuple[CompletedBrowserDownload, ...]:
        if maximum < 1 or maximum_total_bytes < 1:
            raise ValueError("download collection limits must be positive")
        output: list[CompletedBrowserDownload] = []
        total = 0
        with self._lock:
            candidates = tuple(self._records)
        for record in reversed(candidates):
            if len(output) >= maximum:
                break
            if total + record.file.size > maximum_total_bytes:
                continue
            output.append(record)
            total += record.file.size
        return tuple(reversed(output))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            records = tuple(self._records)
        return {
            "runtime_id": "zyra-browser-download-ledger",
            "owner_unit": "M1-S04C-02",
            "canonical_file_owner": "LocalArtifactStore",
            "filesystem_scan": False,
            "browser_session_id": self.browser_session_id,
            "records": len(records),
            "bytes": sum(record.file.size for record in records),
            "artifact_ids": [record.artifact.artifact_id for record in records],
        }


@dataclass(slots=True)
class _DownloadAttempt:
    context: ExecutionContext
    lease: DownloadLease
    completed: CompletedFile | None = None
    guid: str = ""
    source_url: str = ""
    error: BaseException | None = None
    done: threading.Event = field(default_factory=threading.Event)
    events: list[DownloadEvent] = field(default_factory=list)


class BrowserNativeDownloadRuntime:
    """Consume one exact grant into one native download quarantine lease."""

    def __init__(
        self,
        *,
        guard: BrowserDownloadGuard,
        cdp_runtime: Any,
        transport: SessionBoundCdpTransport,
        artifact_port: ArtifactPort,
        ledger: BrowserDownloadLedger,
        browser_context_id: str,
        default_timeout_seconds: float = 30.0,
        disabled: bool = False,
    ) -> None:
        self.guard = guard
        self.cdp_runtime = cdp_runtime
        self.transport = transport
        self.artifact_port = artifact_port
        self.ledger = ledger
        self.browser_context_id = browser_context_id
        self.default_timeout_seconds = default_timeout_seconds
        self.disabled = disabled
        self._attempt: _DownloadAttempt | None = None
        self._queue: queue.Queue[tuple[str, Mapping[str, Any]] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.RLock()
        self._downloads = 0
        self._failures = 0
        self._last_cleanup: dict[str, Any] = {}

    def download(
        self,
        context: ExecutionContext,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        self._ensure_available()
        file_receipt = context.bindings.file_receipt
        if file_receipt is None:
            raise BrowserExecutionError("download_file_receipt_missing", "download requires an exact file receipt")
        tool_use_id = context.permission.bridge_decision.decision.tool_use_id
        if not tool_use_id or not context.permission.accepted:
            raise BrowserExecutionError("download_permission_grant_missing", "download requires a consumed exact grant")
        lease = self.guard.arm(
            action_id=context.request.identity.action_id,
            receipt=file_receipt,
            browser_context_id=self.browser_context_id,
            consumed_permission_tool_use_id=tool_use_id,
        )
        attempt = _DownloadAttempt(context=context, lease=lease)
        with self._lock:
            if self._attempt is not None:
                self.guard.disarm(lease)
                raise BrowserExecutionError("download_runtime_busy", "another browser download is already active")
            self._attempt = attempt
        self._start_events()
        try:
            self._trigger(context, arguments)
            timeout = self._timeout(context, arguments)
            if not attempt.done.wait(timeout):
                raise BrowserExecutionError(
                    "download_settle_timeout",
                    "browser download did not complete within its action deadline",
                    details={"timeout_seconds": timeout},
                )
            if attempt.error is not None:
                raise attempt.error
            if attempt.completed is None or not attempt.guid:
                raise BrowserExecutionError("download_completion_missing", "browser download completed without an owned file")
            completed = attempt.completed
            path = Path(completed.path)
            if not path.is_file() or path.is_symlink():
                raise BrowserExecutionError("download_owned_file_missing", "completed download artifact file is missing")
            content = path.read_bytes()
            if len(content) != completed.size:
                raise BrowserExecutionError("download_size_changed", "completed download size changed before projection")
            artifact = self.artifact_port.write(
                kind=ArtifactKind.FILE,
                title=completed.name,
                content=content,
                metadata={
                    **causal_metadata(context.request.identity, context.receipt),
                    "download_guid": attempt.guid,
                    "download_file_receipt_id": completed.receipt_id,
                    "download_sha256": completed.sha256,
                },
            )
            record = CompletedBrowserDownload(
                action_id=context.request.identity.action_id,
                browser_session_id=context.request.identity.browser_session_id,
                guid=attempt.guid,
                file=completed,
                artifact=artifact,
                source_url_digest=digest_value(attempt.source_url) if attempt.source_url else "",
            )
            self.ledger.append(record)
            self._downloads += 1
            return {
                "download": record.public_dict(),
                "events": [event.public_dict() for event in attempt.events],
                "quarantine_imported": True,
                "filesystem_scan": False,
            }, (artifact.artifact_id,)
        except Exception:
            self._failures += 1
            raise
        finally:
            self._stop_events()
            try:
                cleanup = self.guard.abort(
                    lease,
                    reason=(
                        "download_action_completed"
                        if attempt.completed is not None and attempt.error is None
                        else "download_action_interrupted"
                    ),
                    cancel_active=attempt.completed is None,
                )
                self._last_cleanup = cleanup.public_dict()
            finally:
                with self._lock:
                    self._attempt = None

    def collect(
        self,
        context: ExecutionContext,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        self._ensure_available()
        if not context.permission.accepted:
            raise BrowserExecutionError("download_permission_grant_missing", "collect_downloads requires a consumed exact grant")
        if bool(arguments.get("include_incomplete", False)):
            raise BrowserExecutionError(
                "incomplete_download_collection_denied",
                "incomplete browser downloads cannot become canonical artifacts",
            )
        records = self.ledger.list(
            maximum=int(arguments.get("max_files", 64)),
            maximum_total_bytes=int(arguments.get("max_total_bytes", 1_000_000_000)),
        )
        artifact_ids = tuple(record.artifact.artifact_id for record in records)
        return {
            "downloads": [record.public_dict() for record in records],
            "count": len(records),
            "total_bytes": sum(record.file.size for record in records),
            "filesystem_scan": False,
            "session_owned_only": True,
        }, artifact_ids

    def _trigger(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> None:
        url = str(arguments.get("url") or "")
        if url:
            response = self.transport.send("Page.navigate", {"url": url})
            if response.get("errorText"):
                raise BrowserExecutionError("download_navigation_failed", str(response["errorText"]))
            return
        point = require_point(context)
        for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
            params: dict[str, Any] = {"type": event_type, "x": point.x, "y": point.y}
            if event_type != "mouseMoved":
                params.update({"button": "left", "clickCount": 1})
            self.transport.send(
                "Input.dispatchMouseEvent",
                params,
                cdp_session_id=context.bindings.selector.binding.cdp_session_id if context.bindings.selector else "",
            )

    def _timeout(self, context: ExecutionContext, arguments: Mapping[str, Any]) -> float:
        requested = max(0.05, float(arguments.get("settle_seconds", self.default_timeout_seconds)))
        deadline = self.transport._deadline
        if deadline is None or deadline.remaining_seconds is None:
            return min(self.default_timeout_seconds, requested)
        return max(0.01, min(requested, deadline.remaining_seconds))

    def _start_events(self) -> None:
        self.cdp_runtime.register("Browser.downloadWillBegin", self._will_begin)
        self.cdp_runtime.register("Browser.downloadProgress", self._progress)
        self._worker = threading.Thread(target=self._run_events, name="zyra-browser-download", daemon=True)
        self._worker.start()

    def _stop_events(self) -> None:
        self.cdp_runtime.unregister("Browser.downloadWillBegin", self._will_begin)
        self.cdp_runtime.unregister("Browser.downloadProgress", self._progress)
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=self.default_timeout_seconds)
            self._worker = None

    def _will_begin(self, payload: Mapping[str, Any]) -> None:
        self._queue.put(("will_begin", dict(payload)))

    def _progress(self, payload: Mapping[str, Any]) -> None:
        self._queue.put(("progress", dict(payload)))

    def _run_events(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                kind, payload = item
                with self._lock:
                    attempt = self._attempt
                if attempt is None:
                    continue
                if kind == "will_begin":
                    event = download_event_from_cdp(payload, will_begin=True)
                    attempt.guid = event.guid
                    attempt.source_url = event.url
                    attempt.events.append(event)
                    self.guard.on_will_begin(attempt.lease, event)
                else:
                    event = download_event_from_cdp(payload, will_begin=False)
                    attempt.events.append(event)
                    completed = self.guard.on_progress(attempt.lease, event)
                    if completed is not None:
                        attempt.completed = completed
                        attempt.done.set()
                    elif event.state in {DownloadState.CANCELLED, DownloadState.REJECTED}:
                        attempt.error = BrowserExecutionError(
                            "download_cancelled",
                            "browser download was cancelled or rejected",
                        )
                        attempt.done.set()
            except Exception as exc:
                with self._lock:
                    attempt = self._attempt
                if attempt is not None:
                    attempt.error = exc
                    attempt.done.set()
            finally:
                self._queue.task_done()

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserExecutionError("download_runtime_disabled", "browser native download runtime is disabled")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = self._attempt
        return {
            "runtime_id": "zyra-browser-native-download-runtime",
            "owner_unit": "M1-S04C-02",
            "disabled": self.disabled,
            "active_action_id": active.context.request.identity.action_id if active else "",
            "downloads": self._downloads,
            "failures": self._failures,
            "last_cleanup": dict(self._last_cleanup),
            "ledger": self.ledger.snapshot(),
            "guard": self.guard.snapshot(),
        }


def download_event_from_cdp(payload: Mapping[str, Any], *, will_begin: bool) -> DownloadEvent:
    guid = str(payload.get("guid") or "")
    raw_state = str(payload.get("state") or ("started" if will_begin else "inProgress"))
    states = {
        "started": DownloadState.STARTED,
        "inprogress": DownloadState.IN_PROGRESS,
        "in_progress": DownloadState.IN_PROGRESS,
        "completed": DownloadState.COMPLETED,
        "canceled": DownloadState.CANCELLED,
        "cancelled": DownloadState.CANCELLED,
        "rejected": DownloadState.REJECTED,
    }
    state = states.get(raw_state.replace("-", "_").casefold())
    if state is None:
        raise DownloadGuardError(
            "download_state_unknown",
            f"browser download event has unknown state {raw_state!r}",
        )
    return DownloadEvent(
        guid=guid,
        state=state,
        suggested_filename=str(payload.get("suggestedFilename") or ""),
        received_bytes=max(0, int(payload.get("receivedBytes") or 0)),
        total_bytes=max(0, int(payload.get("totalBytes") or 0)),
        url=str(payload.get("url") or ""),
    )
