from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = "zyra.browser-observability.v1"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_observation_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def digest_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_identity(name: str, value: str, *, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip()
    if not normalized and allow_empty:
        return ""
    if not _IDENTITY.fullmatch(normalized):
        raise ValueError(f"{name} is not a valid bounded identity")
    return normalized


def require_non_negative(name: str, value: int | float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be numeric")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def frozen_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(key): item for key, item in dict(value or {}).items()}


def string_tuple(value: Sequence[Any] | None) -> tuple[str, ...]:
    return tuple(str(item) for item in value or ())


class WatchdogName(StrEnum):
    LOCAL_BROWSER = "local_browser"
    SECURITY = "security"
    DOWNLOADS = "downloads"
    STORAGE_STATE = "storage_state"
    PERMISSIONS = "permissions"
    SCREENSHOT = "screenshot"
    POPUPS = "popups"
    ABOUT_BLANK = "about_blank"
    CRASH_DETECTOR = "crash_detector"


ATTACHED_WATCHDOGS: tuple[WatchdogName, ...] = (
    WatchdogName.LOCAL_BROWSER,
    WatchdogName.SECURITY,
    WatchdogName.DOWNLOADS,
    WatchdogName.STORAGE_STATE,
    WatchdogName.PERMISSIONS,
    WatchdogName.SCREENSHOT,
    WatchdogName.POPUPS,
    WatchdogName.ABOUT_BLANK,
    WatchdogName.CRASH_DETECTOR,
)


class WatchdogMaturity(StrEnum):
    ACTIVE = "active"
    REFERENCE_ONLY = "reference_only"
    OPTIONAL = "optional"
    DEFERRED = "deferred"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


class SignalKind(StrEnum):
    PROCESS_STARTED = "process_started"
    PROCESS_EXITED = "process_exited"
    PROCESS_LEAK = "process_leak"
    CDP_CONNECTED = "cdp_connected"
    CDP_DISCONNECTED = "cdp_disconnected"
    HEARTBEAT_LATE = "heartbeat_late"
    REQUEST_TIMEOUT = "request_timeout"
    REQUEST_STALLED = "request_stalled"
    SECURITY_BLOCK = "security_block"
    SECURITY_DEGRADED = "security_degraded"
    DOWNLOAD_STARTED = "download_started"
    DOWNLOAD_COMPLETED = "download_completed"
    DOWNLOAD_QUARANTINED = "download_quarantined"
    DOWNLOAD_FAILED = "download_failed"
    STORAGE_DIRTY = "storage_dirty"
    STORAGE_PERSISTED = "storage_persisted"
    STORAGE_FAILED = "storage_failed"
    PERMISSION_DRIFT = "permission_drift"
    PERMISSION_REVOKED = "permission_revoked"
    SCREENSHOT_CAPTURED = "screenshot_captured"
    SCREENSHOT_MISSING = "screenshot_missing"
    POPUP_OPENED = "popup_opened"
    POPUP_QUARANTINED = "popup_quarantined"
    POPUP_CLOSED = "popup_closed"
    ABOUT_BLANK_EXPECTED = "about_blank_expected"
    ABOUT_BLANK_STALLED = "about_blank_stalled"
    TOOL_FAILED = "tool_failed"
    HISTORY_CORRUPTION = "history_corruption"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_TAMPERED = "artifact_tampered"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class HistoryKind(StrEnum):
    SESSION_STARTED = "session_started"
    ACTION_PLANNED = "action_planned"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    OBSERVATION = "observation"
    STATE_CAPTURE = "state_capture"
    ARTIFACT_PUBLISHED = "artifact_published"
    WATCHDOG_SIGNAL = "watchdog_signal"
    SESSION_STOPPED = "session_stopped"
    RECOVERY_INPUT = "recovery_input"
    TRACE_SPAN = "trace_span"
    JUDGE_ADVISORY = "judge_advisory"


class ArtifactRole(StrEnum):
    SCREENSHOT = "screenshot"
    DOM_SNAPSHOT = "dom_snapshot"
    DOWNLOAD = "download"
    TRACE = "trace"
    HISTORY_SEGMENT = "history_segment"
    STORAGE_STATE = "storage_state"
    DIAGNOSTIC = "diagnostic"


class TraceSpanKind(StrEnum):
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SUBAGENT = "subagent"
    PROVIDER = "provider"
    MCP = "mcp"
    BROWSER_ACTION = "browser_action"
    WATCHDOG = "watchdog"
    ARTIFACT = "artifact"


class TraceStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class RecoveryReason(StrEnum):
    BROWSER_PROCESS_EXIT = "browser_process_exit"
    CDP_DISCONNECT = "cdp_disconnect"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    REQUEST_TIMEOUT = "request_timeout"
    SECURITY_DENIAL = "security_denial"
    DOWNLOAD_FAILURE = "download_failure"
    STORAGE_FAILURE = "storage_failure"
    PERMISSION_DRIFT = "permission_drift"
    ACTION_FAILURE = "action_failure"
    ARTIFACT_FAILURE = "artifact_failure"
    HISTORY_FAILURE = "history_failure"


@dataclass(frozen=True, slots=True)
class ObservationScope:
    run_id: str
    task_id: str
    browser_session_id: str
    worker_request_id: str
    node_id: str = ""
    canonical_session_id: str = ""

    def __post_init__(self) -> None:
        require_identity("run_id", self.run_id)
        require_identity("task_id", self.task_id)
        require_identity("browser_session_id", self.browser_session_id)
        require_identity("worker_request_id", self.worker_request_id)
        require_identity("node_id", self.node_id, allow_empty=True)
        require_identity("canonical_session_id", self.canonical_session_id, allow_empty=True)

    @property
    def key(self) -> str:
        return digest_value(
            {
                "run_id": self.run_id,
                "task_id": self.task_id,
                "browser_session_id": self.browser_session_id,
                "worker_request_id": self.worker_request_id,
            }
        )[7:39]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "browser_session_id": self.browser_session_id,
            "canonical_session_id": self.canonical_session_id,
            "worker_request_id": self.worker_request_id,
        }


@dataclass(frozen=True, slots=True)
class BrowserObservation:
    scope: ObservationScope
    sequence: int
    observed_at: str = field(default_factory=utc_now)
    monotonic_ms: int = 0
    process_id: int | None = None
    process_running: bool | None = None
    process_exit_code: int | None = None
    cdp_connected: bool | None = None
    cdp_disconnect_reason: str = ""
    last_heartbeat_ms: int | None = None
    active_request_id: str = ""
    active_request_started_ms: int | None = None
    current_url: str = ""
    target_ids: tuple[str, ...] = ()
    popup_target_ids: tuple[str, ...] = ()
    permissions_expected: tuple[str, ...] = ()
    permissions_reported: tuple[str, ...] = ()
    download_items: tuple[Mapping[str, Any], ...] = ()
    storage_dirty: bool = False
    storage_persisted_at: str = ""
    screenshot_artifact_id: str = ""
    action_terminal: bool = False
    action_ok: bool | None = None
    action_error: str = ""
    intentional_stop: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_non_negative("sequence", self.sequence)
        require_non_negative("monotonic_ms", self.monotonic_ms)
        if self.process_id is not None:
            require_non_negative("process_id", self.process_id)
        if self.last_heartbeat_ms is not None:
            require_non_negative("last_heartbeat_ms", self.last_heartbeat_ms)
        if self.active_request_started_ms is not None:
            require_non_negative("active_request_started_ms", self.active_request_started_ms)
        object.__setattr__(self, "target_ids", string_tuple(self.target_ids))
        object.__setattr__(self, "popup_target_ids", string_tuple(self.popup_target_ids))
        object.__setattr__(self, "permissions_expected", string_tuple(self.permissions_expected))
        object.__setattr__(self, "permissions_reported", string_tuple(self.permissions_reported))
        object.__setattr__(self, "download_items", tuple(dict(item) for item in self.download_items))
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "scope": self.scope.to_dict(),
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "monotonic_ms": self.monotonic_ms,
            "process_id": self.process_id,
            "process_running": self.process_running,
            "process_exit_code": self.process_exit_code,
            "cdp_connected": self.cdp_connected,
            "cdp_disconnect_reason": self.cdp_disconnect_reason,
            "last_heartbeat_ms": self.last_heartbeat_ms,
            "active_request_id": self.active_request_id,
            "active_request_started_ms": self.active_request_started_ms,
            "current_url": self.current_url,
            "target_ids": list(self.target_ids),
            "popup_target_ids": list(self.popup_target_ids),
            "permissions_expected": list(self.permissions_expected),
            "permissions_reported": list(self.permissions_reported),
            "download_items": [dict(item) for item in self.download_items],
            "storage_dirty": self.storage_dirty,
            "storage_persisted_at": self.storage_persisted_at,
            "screenshot_artifact_id": self.screenshot_artifact_id,
            "action_terminal": self.action_terminal,
            "action_ok": self.action_ok,
            "action_error": self.action_error,
            "intentional_stop": self.intentional_stop,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WatchdogSignal:
    scope: ObservationScope
    watchdog: WatchdogName
    kind: SignalKind
    status: HealthStatus
    severity: Severity
    summary: str
    signal_id: str = field(default_factory=lambda: new_observation_id("browser-signal"))
    detected_at: str = field(default_factory=utc_now)
    sequence: int = 0
    retryable: bool = False
    terminal: bool = False
    evidence_event_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identity("signal_id", self.signal_id)
        require_non_negative("sequence", self.sequence)
        if not str(self.summary).strip():
            raise ValueError("watchdog signal summary must not be empty")
        object.__setattr__(self, "evidence_event_ids", string_tuple(self.evidence_event_ids))
        object.__setattr__(self, "artifact_ids", string_tuple(self.artifact_ids))
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata))

    @property
    def fingerprint(self) -> str:
        return digest_value(
            {
                "scope": self.scope.to_dict(),
                "watchdog": str(self.watchdog),
                "kind": str(self.kind),
                "sequence": self.sequence,
                "summary": self.summary,
                "metadata": dict(self.metadata),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.worker-health-signal.v1",
            "signal_id": self.signal_id,
            "scope": self.scope.to_dict(),
            "watchdog": str(self.watchdog),
            "kind": str(self.kind),
            "status": str(self.status),
            "severity": str(self.severity),
            "summary": self.summary,
            "detected_at": self.detected_at,
            "sequence": self.sequence,
            "retryable": self.retryable,
            "terminal": self.terminal,
            "evidence_event_ids": list(self.evidence_event_ids),
            "artifact_ids": list(self.artifact_ids),
            "metadata": dict(self.metadata),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    scope: ObservationScope
    kind: HistoryKind
    sequence: int
    payload: Mapping[str, Any]
    record_id: str = field(default_factory=lambda: new_observation_id("browser-history"))
    created_at: str = field(default_factory=utc_now)
    previous_digest: str = ""
    causal_event_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    tool_call_id: str = ""
    parent_record_id: str = ""
    branch_id: str = "main"

    def __post_init__(self) -> None:
        require_identity("record_id", self.record_id)
        require_identity("branch_id", self.branch_id)
        require_identity("tool_call_id", self.tool_call_id, allow_empty=True)
        require_identity("parent_record_id", self.parent_record_id, allow_empty=True)
        require_non_negative("sequence", self.sequence)
        object.__setattr__(self, "payload", frozen_mapping(self.payload))
        object.__setattr__(self, "causal_event_ids", string_tuple(self.causal_event_ids))
        object.__setattr__(self, "artifact_ids", string_tuple(self.artifact_ids))

    @property
    def content_digest(self) -> str:
        return digest_value(self.unsigned_dict())

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.history-record.v1",
            "record_id": self.record_id,
            "scope": self.scope.to_dict(),
            "kind": str(self.kind),
            "sequence": self.sequence,
            "created_at": self.created_at,
            "previous_digest": self.previous_digest,
            "causal_event_ids": list(self.causal_event_ids),
            "artifact_ids": list(self.artifact_ids),
            "tool_call_id": self.tool_call_id,
            "parent_record_id": self.parent_record_id,
            "branch_id": self.branch_id,
            "payload": dict(self.payload),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.unsigned_dict(),
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class HistoryHead:
    scope_key: str
    sequence: int
    record_id: str
    content_digest: str
    updated_at: str
    segment: int = 0
    offset: int = 0

    def __post_init__(self) -> None:
        require_identity("scope_key", self.scope_key)
        require_non_negative("sequence", self.sequence)
        require_non_negative("segment", self.segment)
        require_non_negative("offset", self.offset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "sequence": self.sequence,
            "record_id": self.record_id,
            "content_digest": self.content_digest,
            "updated_at": self.updated_at,
            "segment": self.segment,
            "offset": self.offset,
        }


@dataclass(frozen=True, slots=True)
class ArtifactLineage:
    scope: ObservationScope
    artifact_id: str
    role: ArtifactRole
    uri: str
    sha256: str
    size_bytes: int
    receipt_id: str = field(default_factory=lambda: new_observation_id("artifact-receipt"))
    created_at: str = field(default_factory=utc_now)
    source_event_ids: tuple[str, ...] = ()
    source_record_ids: tuple[str, ...] = ()
    parent_artifact_ids: tuple[str, ...] = ()
    media_type: str = "application/octet-stream"
    quarantined: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identity("artifact_id", self.artifact_id)
        require_identity("receipt_id", self.receipt_id)
        require_non_negative("size_bytes", self.size_bytes)
        if not self.sha256.startswith("sha256:"):
            raise ValueError("artifact lineage requires a sha256 digest")
        object.__setattr__(self, "source_event_ids", string_tuple(self.source_event_ids))
        object.__setattr__(self, "source_record_ids", string_tuple(self.source_record_ids))
        object.__setattr__(self, "parent_artifact_ids", string_tuple(self.parent_artifact_ids))
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.artifact-lineage.v1",
            "scope": self.scope.to_dict(),
            "receipt_id": self.receipt_id,
            "artifact_id": self.artifact_id,
            "role": str(self.role),
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "source_event_ids": list(self.source_event_ids),
            "source_record_ids": list(self.source_record_ids),
            "parent_artifact_ids": list(self.parent_artifact_ids),
            "media_type": self.media_type,
            "quarantined": self.quarantined,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TraceSpan:
    scope: ObservationScope
    kind: TraceSpanKind
    name: str
    status: TraceStatus
    span_id: str = field(default_factory=lambda: new_observation_id("browser-span"))
    trace_id: str = ""
    parent_span_id: str = ""
    started_at: str = field(default_factory=utc_now)
    finished_at: str = ""
    duration_ms: int = 0
    tool_call_id: str = ""
    input_digest: str = ""
    output_digest: str = ""
    error_code: str = ""
    event_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identity("span_id", self.span_id)
        require_identity("trace_id", self.trace_id, allow_empty=True)
        require_identity("parent_span_id", self.parent_span_id, allow_empty=True)
        require_identity("tool_call_id", self.tool_call_id, allow_empty=True)
        require_non_negative("duration_ms", self.duration_ms)
        if not self.name.strip():
            raise ValueError("trace span name must not be empty")
        object.__setattr__(self, "event_ids", string_tuple(self.event_ids))
        object.__setattr__(self, "artifact_ids", string_tuple(self.artifact_ids))
        object.__setattr__(self, "attributes", frozen_mapping(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.trace-span.v1",
            "scope": self.scope.to_dict(),
            "span_id": self.span_id,
            "trace_id": self.trace_id or self.scope.key,
            "parent_span_id": self.parent_span_id,
            "kind": str(self.kind),
            "name": self.name,
            "status": str(self.status),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "tool_call_id": self.tool_call_id,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "error_code": self.error_code,
            "event_ids": list(self.event_ids),
            "artifact_ids": list(self.artifact_ids),
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class RecoveryInput:
    scope: ObservationScope
    reason: RecoveryReason
    summary: str
    signal_ids: tuple[str, ...]
    input_id: str = field(default_factory=lambda: new_observation_id("browser-recovery-input"))
    created_at: str = field(default_factory=utc_now)
    failed_tool_call_ids: tuple[str, ...] = ()
    failed_receipt_ids: tuple[str, ...] = ()
    evidence_event_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    retryable: bool = False
    outcome_unknown: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identity("input_id", self.input_id)
        if not self.summary.strip():
            raise ValueError("recovery input summary must not be empty")
        object.__setattr__(self, "signal_ids", string_tuple(self.signal_ids))
        object.__setattr__(self, "failed_tool_call_ids", string_tuple(self.failed_tool_call_ids))
        object.__setattr__(self, "failed_receipt_ids", string_tuple(self.failed_receipt_ids))
        object.__setattr__(self, "evidence_event_ids", string_tuple(self.evidence_event_ids))
        object.__setattr__(self, "artifact_ids", string_tuple(self.artifact_ids))
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.recovery-input.v1",
            "input_id": self.input_id,
            "scope": self.scope.to_dict(),
            "reason": str(self.reason),
            "summary": self.summary,
            "created_at": self.created_at,
            "signal_ids": list(self.signal_ids),
            "failed_tool_call_ids": list(self.failed_tool_call_ids),
            "failed_receipt_ids": list(self.failed_receipt_ids),
            "evidence_event_ids": list(self.evidence_event_ids),
            "artifact_ids": list(self.artifact_ids),
            "retryable": self.retryable,
            "outcome_unknown": self.outcome_unknown,
            "metadata": dict(self.metadata),
            "planner_owner": "M1-07C",
            "is_recovery_plan": False,
        }


@dataclass(frozen=True, slots=True)
class ToolPair:
    tool_call_id: str
    call_record_id: str
    result_record_id: str
    tool_name: str
    ok: bool
    started_at: str
    finished_at: str
    event_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identity("tool_call_id", self.tool_call_id)
        require_identity("call_record_id", self.call_record_id)
        require_identity("result_record_id", self.result_record_id)
        object.__setattr__(self, "event_ids", string_tuple(self.event_ids))
        object.__setattr__(self, "artifact_ids", string_tuple(self.artifact_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "call_record_id": self.call_record_id,
            "result_record_id": self.result_record_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "event_ids": list(self.event_ids),
            "artifact_ids": list(self.artifact_ids),
        }


@dataclass(frozen=True, slots=True)
class ReplayIssue:
    code: str
    summary: str
    sequence: int = 0
    record_id: str = ""
    fatal: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", frozen_mapping(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "summary": self.summary,
            "sequence": self.sequence,
            "record_id": self.record_id,
            "fatal": self.fatal,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class ReplayProjection:
    scope: ObservationScope
    records: tuple[HistoryRecord, ...]
    tool_pairs: tuple[ToolPair, ...]
    trace_spans: tuple[TraceSpan, ...]
    artifacts: tuple[ArtifactLineage, ...]
    signals: tuple[WatchdogSignal, ...]
    issues: tuple[ReplayIssue, ...]
    complete: bool
    head_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.replay.v1",
            "scope": self.scope.to_dict(),
            "record_count": len(self.records),
            "tool_pair_count": len(self.tool_pairs),
            "trace_span_count": len(self.trace_spans),
            "artifact_count": len(self.artifacts),
            "signal_count": len(self.signals),
            "complete": self.complete,
            "head_digest": self.head_digest,
            "records": [item.to_dict() for item in self.records],
            "tool_pairs": [item.to_dict() for item in self.tool_pairs],
            "trace_spans": [item.to_dict() for item in self.trace_spans],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "signals": [item.to_dict() for item in self.signals],
            "issues": [item.to_dict() for item in self.issues],
        }


@dataclass(frozen=True, slots=True)
class ObservabilityResult:
    scope: ObservationScope
    records: tuple[HistoryRecord, ...] = ()
    signals: tuple[WatchdogSignal, ...] = ()
    spans: tuple[TraceSpan, ...] = ()
    artifacts: tuple[Any, ...] = ()
    artifact_lineage: tuple[ArtifactLineage, ...] = ()
    events: tuple[Any, ...] = ()
    recovery_inputs: tuple[RecoveryInput, ...] = ()
    projection: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "signals", tuple(self.signals))
        object.__setattr__(self, "spans", tuple(self.spans))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "artifact_lineage", tuple(self.artifact_lineage))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "recovery_inputs", tuple(self.recovery_inputs))
        object.__setattr__(self, "projection", frozen_mapping(self.projection))


def scope_from_mapping(value: Mapping[str, Any]) -> ObservationScope:
    return ObservationScope(
        run_id=str(value.get("run_id") or ""),
        task_id=str(value.get("task_id") or ""),
        node_id=str(value.get("node_id") or ""),
        browser_session_id=str(value.get("browser_session_id") or ""),
        canonical_session_id=str(value.get("canonical_session_id") or ""),
        worker_request_id=str(value.get("worker_request_id") or ""),
    )


def history_record_from_mapping(value: Mapping[str, Any]) -> HistoryRecord:
    return HistoryRecord(
        scope=scope_from_mapping(dict(value.get("scope") or {})),
        kind=HistoryKind(str(value.get("kind") or HistoryKind.OBSERVATION)),
        sequence=int(value.get("sequence") or 0),
        payload=dict(value.get("payload") or {}),
        record_id=str(value.get("record_id") or new_observation_id("browser-history")),
        created_at=str(value.get("created_at") or utc_now()),
        previous_digest=str(value.get("previous_digest") or ""),
        causal_event_ids=string_tuple(value.get("causal_event_ids")),
        artifact_ids=string_tuple(value.get("artifact_ids")),
        tool_call_id=str(value.get("tool_call_id") or ""),
        parent_record_id=str(value.get("parent_record_id") or ""),
        branch_id=str(value.get("branch_id") or "main"),
    )
