from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from zyra_core import ArtifactRef, EventRecord, EventType, to_jsonable
from zyra_runtime import LocalArtifactStore

from ...browser_action.download_guard import (
    BrowserDownloadGuard,
    DownloadAbortReceipt,
    DownloadLease,
)
from ...browser_session.errors import BrowserArtifactError
from ..models import (
    HealthStatus,
    ObservationScope,
    Severity,
    SignalKind,
    WatchdogName,
    WatchdogSignal,
    digest_value,
    new_observation_id,
    utc_now,
)
from .contracts import (
    BrowserIntegrationOutput,
    EvidenceSource,
    EvidenceTerminalState,
    RuntimeEvidenceEnvelope,
)
from .event_bus import AttachedBrowserEvent, AttachedEventKind


class DownloadEvidenceError(BrowserArtifactError):
    code = "browser_download_evidence_error"


class DownloadTerminalState(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class DownloadEvidencePolicy:
    require_atomic_artifact: bool = True
    require_cleanup_on_failure: bool = True
    fail_on_incomplete_publication: bool = True
    maximum_evidence_records: int = 2_000

    def __post_init__(self) -> None:
        if self.maximum_evidence_records < 1:
            raise ValueError("download evidence retention must be positive")


@dataclass(frozen=True, slots=True)
class DownloadLifecycleEvidence:
    scope: ObservationScope
    action_id: str
    guid: str
    state: DownloadTerminalState
    artifact_id: str = ""
    receipt_id: str = field(
        default_factory=lambda: new_observation_id("browser-download-evidence")
    )
    source_event_ids: tuple[str, ...] = ()
    file_receipt_id: str = ""
    filename: str = ""
    size_bytes: int = 0
    sha256: str = ""
    source_url_digest: str = ""
    cleanup_receipt: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    retryable: bool = False
    outcome_unknown: bool = False
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("download evidence requires action_id")
        if self.state == DownloadTerminalState.COMPLETED:
            if not self.guid or not self.artifact_id or not self.sha256:
                raise ValueError("completed download evidence is incomplete")
        if self.size_bytes < 0:
            raise ValueError("download size must be non-negative")
        object.__setattr__(self, "cleanup_receipt", dict(self.cleanup_receipt))

    @property
    def ok(self) -> bool:
        return self.state == DownloadTerminalState.COMPLETED and not self.error

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.browser-observability.download-evidence.v1",
            "receipt_id": self.receipt_id,
            "scope": self.scope.to_dict(),
            "action_id": self.action_id,
            "guid": self.guid,
            "state": str(self.state),
            "artifact_id": self.artifact_id,
            "source_event_ids": list(self.source_event_ids),
            "file_receipt_id": self.file_receipt_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "source_url_digest": self.source_url_digest,
            "cleanup_receipt": dict(self.cleanup_receipt),
            "error": self.error,
            "retryable": self.retryable,
            "outcome_unknown": self.outcome_unknown,
            "created_at": self.created_at,
            "ok": self.ok,
            "canonical_transaction_owner": "M1-S04C-02",
            "canonical_artifact_owner": "LocalArtifactStore",
            "canonical_history_owner": "BrowserHistoryStore",
        }
        if include_digest:
            value["digest"] = digest_value(value)
        return value

    def runtime_evidence(self) -> RuntimeEvidenceEnvelope:
        terminal_state = {
            DownloadTerminalState.COMPLETED: EvidenceTerminalState.COMPLETED,
            DownloadTerminalState.CANCELLED: EvidenceTerminalState.CANCELLED,
            DownloadTerminalState.REJECTED: EvidenceTerminalState.FAILED,
            DownloadTerminalState.FAILED: EvidenceTerminalState.FAILED,
            DownloadTerminalState.INTERRUPTED: EvidenceTerminalState.INTERRUPTED,
        }[self.state]
        return RuntimeEvidenceEnvelope(
            scope=self.scope,
            source=EvidenceSource.BROWSER_DOWNLOAD,
            event_type=f"download_{self.state}",
            payload=self.to_dict(),
            evidence_id=self.receipt_id,
            source_event_id=self.source_event_ids[-1] if self.source_event_ids else "",
            correlation_event_ids=self.source_event_ids,
            action_id=self.action_id,
            action_receipt_id=self.file_receipt_id,
            terminal_state=terminal_state,
            outcome_unknown=self.outcome_unknown,
            retryable=self.retryable,
            artifact_ids=(self.artifact_id,) if self.artifact_id else (),
            metadata={"guid": self.guid, "filename": self.filename},
        )


class BrowserDownloadEvidenceRuntime:
    """Observe 04C native downloads and enforce their terminal evidence contract.

    This component deliberately does not receive bytes or own a transaction.
    BrowserDownloadGuard/BrowserNativeDownloadRuntime remain the live owner and
    LocalArtifactStore remains the committed-byte owner.
    """

    def __init__(
        self,
        artifact_store: LocalArtifactStore,
        *,
        policy: DownloadEvidencePolicy | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.policy = policy or DownloadEvidencePolicy()
        self._evidence: list[DownloadLifecycleEvidence] = []
        self._by_identity: dict[tuple[str, str, str], DownloadLifecycleEvidence] = {}

    def observe(
        self,
        scope: ObservationScope,
        *,
        action_run: Any,
        artifacts: Sequence[ArtifactRef],
        attached_events: Sequence[AttachedBrowserEvent] = (),
        guard_snapshot: Mapping[str, Any] | None = None,
    ) -> BrowserIntegrationOutput:
        evidence = self._completed_from_action(scope, action_run, artifacts)
        evidence.extend(
            self._terminal_from_events(
                scope,
                attached_events,
                action_run=action_run,
                guard_snapshot=guard_snapshot or {},
            )
        )
        deduped: list[DownloadLifecycleEvidence] = []
        for item in evidence:
            key = (scope.key, item.action_id, item.guid or str(item.state))
            existing = self._by_identity.get(key)
            if existing is not None:
                if existing.digest != item.digest:
                    raise DownloadEvidenceError(
                        "download terminal identity changed across observations",
                        session_id=scope.browser_session_id,
                        operation="download_evidence",
                    )
                continue
            self._validate(item)
            self._by_identity[key] = item
            self._evidence.append(item)
            deduped.append(item)
        self._evidence = self._evidence[-self.policy.maximum_evidence_records :]
        signals = tuple(
            signal
            for item in deduped
            if (signal := self._signal(item)) is not None
        )
        events = tuple(self._event(item) for item in deduped)
        return BrowserIntegrationOutput(
            evidence=tuple(item.runtime_evidence() for item in deduped),
            signals=signals,
            events=events,
            projection=self.projection(scope=scope),
        )

    def interrupt(
        self,
        scope: ObservationScope,
        guard: BrowserDownloadGuard,
        lease: DownloadLease,
        *,
        reason: str,
        source_event_ids: Sequence[str] = (),
    ) -> BrowserIntegrationOutput:
        cleanup = guard.abort(
            lease,
            reason=reason,
            cancel_active=True,
        )
        evidence = DownloadLifecycleEvidence(
            scope=scope,
            action_id=lease.action_id,
            guid=(cleanup.cancelled_guids[0] if cleanup.cancelled_guids else ""),
            state=DownloadTerminalState.INTERRUPTED,
            source_event_ids=tuple(source_event_ids),
            file_receipt_id=lease.receipt.receipt_id,
            filename=lease.receipt.expected_name,
            cleanup_receipt=cleanup.public_dict(),
            error=reason,
            retryable=True,
            outcome_unknown=False,
        )
        self._validate(evidence)
        self._evidence.append(evidence)
        signal = self._signal(evidence)
        return BrowserIntegrationOutput(
            evidence=(evidence.runtime_evidence(),),
            signals=(signal,) if signal else (),
            events=(self._event(evidence),),
            projection=self.projection(scope=scope),
        )

    def projection(
        self,
        *,
        scope: ObservationScope | None = None,
    ) -> dict[str, Any]:
        values = tuple(
            item
            for item in self._evidence
            if scope is None or item.scope == scope
        )
        return {
            "schema": "zyra.browser-observability.download-projection.v1",
            "count": len(values),
            "completed": sum(1 for item in values if item.ok),
            "failed": sum(1 for item in values if not item.ok),
            "interrupted": sum(
                1 for item in values if item.state == DownloadTerminalState.INTERRUPTED
            ),
            "cleanup_verified": all(
                not item.cleanup_receipt
                or bool(item.cleanup_receipt.get("cleanup_complete"))
                for item in values
            ),
            "evidence": [item.to_dict() for item in values[-100:]],
            "transaction_owner": "M1-S04C-02",
            "artifact_owner": "LocalArtifactStore",
        }

    def _completed_from_action(
        self,
        scope: ObservationScope,
        action_run: Any,
        artifacts: Sequence[ArtifactRef],
    ) -> list[DownloadLifecycleEvidence]:
        artifact_by_id = {item.artifact_id: item for item in artifacts}
        output: list[DownloadLifecycleEvidence] = []
        for receipt in _action_receipts(action_run):
            value = _mapping(receipt)
            result = _mapping(value.get("result") or value.get("output"))
            download = _mapping(result.get("download"))
            if not download:
                continue
            file_value = _mapping(download.get("file"))
            artifact_id = str(download.get("artifact_id") or "")
            artifact = artifact_by_id.get(artifact_id)
            if artifact is None:
                raise DownloadEvidenceError(
                    f"completed download artifact {artifact_id!r} is not in the worker result",
                    session_id=scope.browser_session_id,
                    operation="download_evidence",
                )
            sha256, size = self._artifact_digest(artifact)
            expected_sha = str(file_value.get("sha256") or "")
            expected_sha = expected_sha.removeprefix("sha256:")
            if expected_sha and sha256.removeprefix("sha256:") != expected_sha:
                raise DownloadEvidenceError(
                    "completed download artifact digest differs from 04C receipt",
                    session_id=scope.browser_session_id,
                    operation="download_evidence",
                )
            output.append(
                DownloadLifecycleEvidence(
                    scope=scope,
                    action_id=str(download.get("action_id") or value.get("action_id") or ""),
                    guid=str(download.get("guid") or ""),
                    state=DownloadTerminalState.COMPLETED,
                    artifact_id=artifact_id,
                    source_event_ids=tuple(_event_ids(value)),
                    file_receipt_id=str(file_value.get("receipt_id") or ""),
                    filename=str(file_value.get("name") or artifact.title),
                    size_bytes=size,
                    sha256=sha256,
                    source_url_digest=str(download.get("source_url_digest") or ""),
                )
            )
        return output

    def _terminal_from_events(
        self,
        scope: ObservationScope,
        events: Sequence[AttachedBrowserEvent],
        *,
        action_run: Any,
        guard_snapshot: Mapping[str, Any],
    ) -> list[DownloadLifecycleEvidence]:
        output: list[DownloadLifecycleEvidence] = []
        action_id = _last_action_id(action_run) or scope.worker_request_id
        cleanup = _mapping(guard_snapshot.get("last_cleanup"))
        for item in events:
            if item.kind != AttachedEventKind.CDP_EVENT:
                continue
            if str(item.payload.get("method") or "") != "Browser.downloadProgress":
                continue
            params = _mapping(item.payload.get("params"))
            raw_state = str(params.get("state") or "").casefold()
            state = {
                "canceled": DownloadTerminalState.CANCELLED,
                "cancelled": DownloadTerminalState.CANCELLED,
                "rejected": DownloadTerminalState.REJECTED,
            }.get(raw_state)
            if state is None:
                continue
            output.append(
                DownloadLifecycleEvidence(
                    scope=scope,
                    action_id=action_id,
                    guid=str(params.get("guid") or ""),
                    state=state,
                    source_event_ids=(item.bus_event_id,),
                    cleanup_receipt=cleanup,
                    error=f"browser download {raw_state}",
                    retryable=state == DownloadTerminalState.CANCELLED,
                    outcome_unknown=False,
                )
            )
        return output

    def _artifact_digest(self, artifact: ArtifactRef) -> tuple[str, int]:
        path = self.artifact_store.resolve_path(artifact)
        if not path.is_file() or path.is_symlink():
            raise DownloadEvidenceError(
                "completed download artifact is not a regular file",
                operation="download_evidence",
            )
        payload = path.read_bytes()
        return "sha256:" + hashlib.sha256(payload).hexdigest(), len(payload)

    def _validate(self, evidence: DownloadLifecycleEvidence) -> None:
        if evidence.ok:
            if evidence.cleanup_receipt and not bool(
                evidence.cleanup_receipt.get("cleanup_complete")
            ):
                raise DownloadEvidenceError(
                    "completed download left transient files",
                    session_id=evidence.scope.browser_session_id,
                    operation="download_cleanup",
                )
            return
        if evidence.artifact_id and self.policy.fail_on_incomplete_publication:
            raise DownloadEvidenceError(
                "non-completed download published a canonical artifact",
                session_id=evidence.scope.browser_session_id,
                operation="download_evidence",
            )
        if (
            self.policy.require_cleanup_on_failure
            and evidence.cleanup_receipt
            and not bool(evidence.cleanup_receipt.get("cleanup_complete"))
        ):
            raise DownloadEvidenceError(
                "interrupted download cleanup did not complete",
                session_id=evidence.scope.browser_session_id,
                operation="download_cleanup",
            )

    @staticmethod
    def _signal(evidence: DownloadLifecycleEvidence) -> WatchdogSignal | None:
        if evidence.ok:
            return None
        return WatchdogSignal(
            scope=evidence.scope,
            watchdog=WatchdogName.DOWNLOADS,
            kind=SignalKind.DOWNLOAD_FAILED,
            status=HealthStatus.UNHEALTHY,
            severity=Severity.ERROR,
            summary=evidence.error or "Browser download did not complete.",
            sequence=1,
            retryable=evidence.retryable,
            terminal=True,
            evidence_event_ids=evidence.source_event_ids,
            artifact_ids=(evidence.artifact_id,) if evidence.artifact_id else (),
            metadata={
                "download_evidence_id": evidence.receipt_id,
                "guid": evidence.guid,
                "outcome_unknown": evidence.outcome_unknown,
            },
        )

    @staticmethod
    def _event(evidence: DownloadLifecycleEvidence) -> EventRecord:
        return EventRecord(
            run_id=evidence.scope.run_id,
            task_id=evidence.scope.task_id,
            node_id=evidence.scope.node_id or None,
            event_type=(
                EventType.ARTIFACT_WRITTEN
                if evidence.ok
                else EventType.WORKER_HEALTH
            ),
            payload={
                "browser_download_evidence": evidence.to_dict(),
                "recovery_planner_owner": "M1-07C",
                "is_recovery_plan": False,
            },
        )


def _action_receipts(action_run: Any) -> tuple[Any, ...]:
    if action_run is None:
        return ()
    return tuple(
        getattr(action_run, "receipts", getattr(action_run, "action_receipts", ()))
    )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    projected = to_jsonable(value)
    return dict(projected) if isinstance(projected, Mapping) else {}


def _event_ids(value: Mapping[str, Any]) -> tuple[str, ...]:
    raw = value.get("event_ids") or value.get("source_event_ids") or ()
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if isinstance(raw, Sequence):
        return tuple(str(item) for item in raw if str(item))
    return ()


def _last_action_id(action_run: Any) -> str:
    receipts = _action_receipts(action_run)
    if not receipts:
        return ""
    value = _mapping(receipts[-1])
    return str(value.get("action_id") or value.get("tool_call_id") or "")
