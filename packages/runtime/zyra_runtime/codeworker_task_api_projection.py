from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


M1_02D_TASK_API_OWNER_UNIT = "M1-02D"
CODEWORKER_TASK_API_PROJECTION_RUNTIME_ID = "codeworker_task_api_projection_runtime"


class CodeWorkerTaskApiStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class CodeWorkerTaskApiSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class CodeWorkerTaskApiSurface(StrEnum):
    SESSION = "session"
    TOOL_TRACE = "tool_trace"
    COMPACT_STATE = "compact_state"
    RESTORE = "restore"
    MODEL_API = "model_api"
    EVENT_LOG = "event_log"
    CHECKPOINT = "checkpoint"


class ToolTraceItemKind(StrEnum):
    TURN = "turn"
    BATCH = "batch"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MODEL_STREAM = "model_stream"
    API_RETRY = "api_retry"
    COMPACT = "compact"
    RESTORE = "restore"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CodeWorkerTaskApiFinding:
    code: str
    severity: CodeWorkerTaskApiSeverity
    surface: CodeWorkerTaskApiSurface
    message: str
    event_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == CodeWorkerTaskApiSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "event_id": self.event_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryPhaseObservation:
    event_id: str
    phase: str
    session_id: str = ""
    worker_request_id: str = ""
    turn_index: int = 0
    created_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "phase": self.phase,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "turn_index": self.turn_index,
            "created_at": self.created_at,
            "payload": to_jsonable(self.payload),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerSessionSummary:
    task_id: str
    session_id: str
    worker_request_id: str
    resume_token: str = ""
    turn_count: int = 0
    message_count: int = 0
    transcript_entry_count: int = 0
    snapshot_artifact_id: str = ""
    transcript_artifact_id: str = ""
    trace_artifact_id: str = ""
    consistent: bool = False
    source: str = "CodeWorkerRuntime"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return bool(self.session_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "resume_token": self.resume_token,
            "turn_count": self.turn_count,
            "message_count": self.message_count,
            "transcript_entry_count": self.transcript_entry_count,
            "snapshot_artifact_id": self.snapshot_artifact_id,
            "transcript_artifact_id": self.transcript_artifact_id,
            "trace_artifact_id": self.trace_artifact_id,
            "consistent": self.consistent,
            "present": self.present,
            "source": self.source,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerContextUsageProjection:
    active_chars: int = 0
    active_limit_chars: int = 0
    ratio: float = 0.0
    compact_needed: bool = False
    pressure: str = ""
    total_chars: int = 0
    active_blocks: int = 0
    compacted_blocks: int = 0
    restored_blocks: int = 0
    source_phase: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def over_limit_chars(self) -> int:
        return max(0, self.active_chars - self.active_limit_chars)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_chars": self.active_chars,
            "active_limit_chars": self.active_limit_chars,
            "ratio": round(self.ratio, 6),
            "compact_needed": self.compact_needed,
            "pressure": self.pressure,
            "total_chars": self.total_chars,
            "active_blocks": self.active_blocks,
            "compacted_blocks": self.compacted_blocks,
            "restored_blocks": self.restored_blocks,
            "over_limit_chars": self.over_limit_chars,
            "source_phase": self.source_phase,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerCompactApiState:
    status: str = ""
    ok: bool = False
    compact_needed: bool = False
    boundary_id: str = ""
    compact_artifact_id: str = ""
    restore_contract_id: str = ""
    restore_segment_count: int = 0
    preserved_segment_count: int = 0
    compact_state_projection_status: str = ""
    policy_status: str = ""
    policy_blocked_rules: int = 0
    context_usage: CodeWorkerContextUsageProjection = field(default_factory=CodeWorkerContextUsageProjection)
    latest_report: dict[str, Any] = field(default_factory=dict)
    latest_projection: dict[str, Any] = field(default_factory=dict)

    @property
    def has_boundary(self) -> bool:
        return bool(self.boundary_id or self.compact_artifact_id)

    @property
    def has_restore_contract(self) -> bool:
        return bool(self.restore_contract_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "compact_needed": self.compact_needed,
            "boundary_id": self.boundary_id,
            "compact_artifact_id": self.compact_artifact_id,
            "restore_contract_id": self.restore_contract_id,
            "restore_segment_count": self.restore_segment_count,
            "preserved_segment_count": self.preserved_segment_count,
            "compact_state_projection_status": self.compact_state_projection_status,
            "policy_status": self.policy_status,
            "policy_blocked_rules": self.policy_blocked_rules,
            "has_boundary": self.has_boundary,
            "has_restore_contract": self.has_restore_contract,
            "context_usage": self.context_usage.to_dict(),
            "latest_report": to_jsonable(self.latest_report),
            "latest_projection": to_jsonable(self.latest_projection),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerModelApiState:
    model_stream_status: str = ""
    model_stream_ok: bool = False
    selected_model: str = ""
    error_kind: str = ""
    retry_status: str = ""
    retry_ok: bool = False
    retry_count: int = 0
    fallback_used: bool = False
    final_model: str = ""
    watchdog_status: str = ""
    watchdog_signals: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    latest_stream: dict[str, Any] = field(default_factory=dict)
    latest_retry: dict[str, Any] = field(default_factory=dict)

    @property
    def recovered(self) -> bool:
        return self.retry_ok and self.retry_status in {"retried", "fallback_selected", "not_needed"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_stream_status": self.model_stream_status,
            "model_stream_ok": self.model_stream_ok,
            "selected_model": self.selected_model,
            "error_kind": self.error_kind,
            "retry_status": self.retry_status,
            "retry_ok": self.retry_ok,
            "retry_count": self.retry_count,
            "fallback_used": self.fallback_used,
            "final_model": self.final_model,
            "watchdog_status": self.watchdog_status,
            "watchdog_signals": self.watchdog_signals,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 8),
            "recovered": self.recovered,
            "latest_stream": to_jsonable(self.latest_stream),
            "latest_retry": to_jsonable(self.latest_retry),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerRestoreApiState:
    integration_status: str = ""
    integration_ok: bool = False
    application_count: int = 0
    model_message_count: int = 0
    context_block_count: int = 0
    restored_chars: int = 0
    latest_contract_id: str = ""
    latest_turn_index: int = 0
    pending_contract_count: int = 0
    untrusted_message_count: int = 0
    redacted_message_count: int = 0
    security_status: str = ""
    security_ok: bool = False
    latest_application: dict[str, Any] = field(default_factory=dict)
    latest_integration: dict[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return self.application_count > 0 and self.model_message_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "integration_status": self.integration_status,
            "integration_ok": self.integration_ok,
            "application_count": self.application_count,
            "model_message_count": self.model_message_count,
            "context_block_count": self.context_block_count,
            "restored_chars": self.restored_chars,
            "latest_contract_id": self.latest_contract_id,
            "latest_turn_index": self.latest_turn_index,
            "pending_contract_count": self.pending_contract_count,
            "untrusted_message_count": self.untrusted_message_count,
            "redacted_message_count": self.redacted_message_count,
            "security_status": self.security_status,
            "security_ok": self.security_ok,
            "applied": self.applied,
            "latest_application": to_jsonable(self.latest_application),
            "latest_integration": to_jsonable(self.latest_integration),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerToolTraceItem:
    trace_id: str
    kind: ToolTraceItemKind
    phase: str
    event_id: str
    turn_index: int = 0
    batch_index: int = 0
    step_index: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    ok: bool | None = None
    error: str = ""
    summary: str = ""
    artifact_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def failed(self) -> bool:
        return self.ok is False or bool(self.error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "kind": str(self.kind),
            "phase": self.phase,
            "event_id": self.event_id,
            "turn_index": self.turn_index,
            "batch_index": self.batch_index,
            "step_index": self.step_index,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "failed": self.failed,
            "error": self.error,
            "summary": self.summary,
            "artifact_ids": list(self.artifact_ids),
            "metadata": to_jsonable(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerRepairProjection:
    detected: bool
    failed_tool_call_id: str = ""
    failed_command: str = ""
    repair_tool_call_id: str = ""
    repair_path: str = ""
    verification_tool_call_id: str = ""
    verification_command: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "failed_tool_call_id": self.failed_tool_call_id,
            "failed_command": self.failed_command,
            "repair_tool_call_id": self.repair_tool_call_id,
            "repair_path": self.repair_path,
            "verification_tool_call_id": self.verification_tool_call_id,
            "verification_command": self.verification_command,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerToolTraceProjection:
    projection_id: str
    task_id: str
    session_id: str
    worker_request_id: str
    items: tuple[CodeWorkerToolTraceItem, ...]
    repair: CodeWorkerRepairProjection
    findings: tuple[CodeWorkerTaskApiFinding, ...] = field(default_factory=tuple)
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> CodeWorkerTaskApiStatus:
        if any(finding.blocking for finding in self.findings):
            return CodeWorkerTaskApiStatus.BLOCKED
        if not self.items:
            return CodeWorkerTaskApiStatus.EMPTY
        if self.findings:
            return CodeWorkerTaskApiStatus.DEGRADED
        return CodeWorkerTaskApiStatus.READY

    @property
    def tool_call_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolTraceItemKind.TOOL_CALL and item.phase == "tool_call_started")

    @property
    def tool_result_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolTraceItemKind.TOOL_RESULT)

    @property
    def failed_tool_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolTraceItemKind.TOOL_RESULT and item.failed)

    @property
    def model_stream_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolTraceItemKind.MODEL_STREAM)

    @property
    def api_retry_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolTraceItemKind.API_RETRY)

    @property
    def compact_event_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolTraceItemKind.COMPACT)

    @property
    def restore_event_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolTraceItemKind.RESTORE)

    def metadata(self) -> dict[str, str]:
        return {
            "codeworker_tool_trace_projection_id": self.projection_id,
            "codeworker_tool_trace_ok": str(self.ok).lower(),
            "codeworker_tool_trace_status": str(self.status),
            "codeworker_tool_trace_items": str(len(self.items)),
            "codeworker_tool_trace_tool_calls": str(self.tool_call_count),
            "codeworker_tool_trace_tool_results": str(self.tool_result_count),
            "codeworker_tool_trace_failed_tools": str(self.failed_tool_count),
            "codeworker_tool_trace_model_streams": str(self.model_stream_count),
            "codeworker_tool_trace_api_retries": str(self.api_retry_count),
            "codeworker_tool_trace_compact_events": str(self.compact_event_count),
            "codeworker_tool_trace_restore_events": str(self.restore_event_count),
            "codeworker_tool_trace_repair_detected": str(self.repair.detected).lower(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_tool_trace_projection.v1",
            "projection_id": self.projection_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "items": [item.to_dict() for item in self.items],
            "repair": self.repair.to_dict(),
            "tool_call_count": self.tool_call_count,
            "tool_result_count": self.tool_result_count,
            "failed_tool_count": self.failed_tool_count,
            "model_stream_count": self.model_stream_count,
            "api_retry_count": self.api_retry_count,
            "compact_event_count": self.compact_event_count,
            "restore_event_count": self.restore_event_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerSessionApiProjection:
    projection_id: str
    owner_unit: str
    runtime_id: str
    task_id: str
    run_id: str
    session: CodeWorkerSessionSummary
    compact_state: CodeWorkerCompactApiState
    model_api: CodeWorkerModelApiState
    restore_state: CodeWorkerRestoreApiState
    tool_trace: CodeWorkerToolTraceProjection
    observations: tuple[QueryPhaseObservation, ...]
    findings: tuple[CodeWorkerTaskApiFinding, ...] = field(default_factory=tuple)
    source_decisions: tuple[dict[str, str], ...] = field(default_factory=tuple)
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.session.present and self.tool_trace.ok and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> CodeWorkerTaskApiStatus:
        if any(finding.blocking for finding in self.findings):
            return CodeWorkerTaskApiStatus.BLOCKED
        if not self.session.present and not self.observations:
            return CodeWorkerTaskApiStatus.EMPTY
        if self.findings or not self.session.present:
            return CodeWorkerTaskApiStatus.DEGRADED
        return CodeWorkerTaskApiStatus.READY

    @property
    def phase_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for observation in self.observations:
            counts[observation.phase] = counts.get(observation.phase, 0) + 1
        return counts

    def metadata(self) -> dict[str, str]:
        return {
            "codeworker_task_api_projection_id": self.projection_id,
            "codeworker_task_api_ok": str(self.ok).lower(),
            "codeworker_task_api_status": str(self.status),
            "codeworker_task_api_observations": str(len(self.observations)),
            "codeworker_task_api_phase_count": str(len(self.phase_counts)),
            "codeworker_task_api_has_session": str(self.session.present).lower(),
            "codeworker_task_api_has_compact_boundary": str(self.compact_state.has_boundary).lower(),
            "codeworker_task_api_restore_applied": str(self.restore_state.applied).lower(),
            **self.tool_trace.metadata(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_session_api_projection.v1",
            "projection_id": self.projection_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "ok": self.ok,
            "status": str(self.status),
            "session": self.session.to_dict(),
            "compact_state": self.compact_state.to_dict(),
            "model_api": self.model_api.to_dict(),
            "restore_state": self.restore_state.to_dict(),
            "tool_trace": self.tool_trace.to_dict(),
            "phase_counts": self.phase_counts,
            "observations": [observation.to_dict() for observation in self.observations],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


class CodeWorkerTaskApiProjectionRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_TASK_API_OWNER_UNIT,
        runtime_id: str = CODEWORKER_TASK_API_PROJECTION_RUNTIME_ID,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled

    def build_session_projection(
        self,
        *,
        task_id: str,
        state: Mapping[str, Any] | None,
        events: Iterable[Any],
    ) -> CodeWorkerSessionApiProjection:
        event_list = [_event_view(event) for event in events]
        all_observations = tuple(_phase_observations(event_list))
        state_map = state if isinstance(state, Mapping) else {}
        session = self._session_summary(task_id=task_id, state=state_map, observations=all_observations)
        observations = tuple(
            _observations_for_session(
                all_observations,
                session_id=session.session_id,
                worker_request_id=session.worker_request_id,
            )
        )
        compact_state = self._compact_state(observations)
        model_api = self._model_api_state(observations)
        restore_state = self._restore_state(observations)
        tool_trace = self.build_tool_trace_projection(
            task_id=task_id,
            state=state_map,
            events=event_list,
            session=session,
        )
        findings = list(self._projection_findings(session, observations, compact_state, restore_state, model_api))
        if self.disabled:
            findings.append(
                CodeWorkerTaskApiFinding(
                    code="CODEWORKER_TASK_API_PROJECTION_DISABLED",
                    severity=CodeWorkerTaskApiSeverity.BLOCKER,
                    surface=CodeWorkerTaskApiSurface.SESSION,
                    message="CodeWorker task API projection runtime is disabled.",
                )
            )
        return CodeWorkerSessionApiProjection(
            projection_id=new_id("codeworker_api_projection"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            task_id=task_id,
            run_id=str(state_map.get("run_id") or ""),
            session=session,
            compact_state=compact_state,
            model_api=model_api,
            restore_state=restore_state,
            tool_trace=tool_trace,
            observations=observations,
            findings=tuple(findings),
            source_decisions=default_codeworker_task_api_source_decisions(),
        )

    def build_tool_trace_projection(
        self,
        *,
        task_id: str,
        state: Mapping[str, Any] | None,
        events: Iterable[Any],
        session: CodeWorkerSessionSummary | None = None,
    ) -> CodeWorkerToolTraceProjection:
        event_list = [_event_view(event) for event in events]
        all_observations = tuple(_phase_observations(event_list))
        if session is None:
            session = self._session_summary(task_id=task_id, state=state or {}, observations=all_observations)
        observations = tuple(
            _observations_for_session(
                all_observations,
                session_id=session.session_id,
                worker_request_id=session.worker_request_id,
            )
        )
        items = tuple(_trace_items_from_observations(observations))
        repair = _repair_projection(items)
        findings = list(_tool_trace_findings(items, repair))
        if self.disabled:
            findings.append(
                CodeWorkerTaskApiFinding(
                    code="CODEWORKER_TOOL_TRACE_PROJECTION_DISABLED",
                    severity=CodeWorkerTaskApiSeverity.BLOCKER,
                    surface=CodeWorkerTaskApiSurface.TOOL_TRACE,
                    message="CodeWorker tool trace projection runtime is disabled.",
                )
            )
        return CodeWorkerToolTraceProjection(
            projection_id=new_id("codeworker_tool_trace"),
            task_id=task_id,
            session_id=session.session_id,
            worker_request_id=session.worker_request_id,
            items=items,
            repair=repair,
            findings=tuple(findings),
        )

    def build_compact_state_projection(
        self,
        *,
        task_id: str,
        state: Mapping[str, Any] | None,
        events: Iterable[Any],
    ) -> CodeWorkerCompactApiState:
        all_observations = tuple(_phase_observations([_event_view(event) for event in events]))
        state_map = state if isinstance(state, Mapping) else {}
        session = self._session_summary(task_id=task_id, state=state_map, observations=all_observations)
        observations = tuple(
            _observations_for_session(
                all_observations,
                session_id=session.session_id,
                worker_request_id=session.worker_request_id,
            )
        )
        return self._compact_state(observations)

    def event_for_projection(
        self,
        projection: CodeWorkerSessionApiProjection,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "codeworker_task_api_projection",
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": projection.session.session_id,
                    "worker_request_id": projection.session.worker_request_id,
                    "phase": phase,
                    "codeworker_task_api_projection": projection.to_dict(),
                }
            },
        )

    def metadata(self, projection: CodeWorkerSessionApiProjection | None = None) -> dict[str, str]:
        if projection is None:
            return {
                "codeworker_task_api_ok": str(not self.disabled).lower(),
                "codeworker_task_api_status": str(
                    CodeWorkerTaskApiStatus.DISABLED if self.disabled else CodeWorkerTaskApiStatus.READY
                ),
                "codeworker_task_api_runtime_id": self.runtime_id,
            }
        return projection.metadata()

    def _session_summary(
        self,
        *,
        task_id: str,
        state: Mapping[str, Any],
        observations: Sequence[QueryPhaseObservation],
    ) -> CodeWorkerSessionSummary:
        metadata = _as_mapping(state.get("metadata"))
        latest_session = _as_mapping(metadata.get("last_code_worker_session"))
        if not latest_session:
            sessions = [item for item in _as_list(metadata.get("code_worker_sessions")) if isinstance(item, Mapping)]
            latest_session = _as_mapping(sessions[-1]) if sessions else {}
        session_id = str(latest_session.get("session_id") or _latest_attr(observations, "session_id"))
        worker_request_id = str(
            latest_session.get("worker_request_id") or _latest_attr(observations, "worker_request_id")
        )
        return CodeWorkerSessionSummary(
            task_id=task_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            resume_token=str(latest_session.get("resume_token") or ""),
            turn_count=_safe_int(latest_session.get("turn_count") or _max_turn_index(observations)),
            message_count=_safe_int(latest_session.get("message_count")),
            transcript_entry_count=_safe_int(latest_session.get("transcript_entry_count")),
            snapshot_artifact_id=str(latest_session.get("snapshot_artifact_id") or ""),
            transcript_artifact_id=str(latest_session.get("transcript_artifact_id") or ""),
            trace_artifact_id=str(latest_session.get("trace_artifact_id") or ""),
            consistent=_truthy(latest_session.get("consistent")),
            source=str(latest_session.get("source") or "CodeWorkerRuntime"),
            metadata=dict(latest_session),
        )

    def _compact_state(self, observations: Sequence[QueryPhaseObservation]) -> CodeWorkerCompactApiState:
        report = _latest_payload(observations, "compact_restore_report", "compact_restore")
        pending = _latest_payload(observations, "compact_restore_contract_pending", "compact_restore")
        if pending:
            report = pending
        policy = _latest_payload(observations, "compact_restore_policy", "compact_restore_policy")
        projection = _latest_payload(observations, "compact_state_projection", "compact_state_projection")
        boundary = _as_mapping(report.get("boundary"))
        contract = _as_mapping(report.get("restore_contract"))
        budget = _as_mapping(report.get("context_budget"))
        usage = _as_mapping(budget.get("usage"))
        context_usage = CodeWorkerContextUsageProjection(
            active_chars=_safe_int(usage.get("active_chars")),
            active_limit_chars=_safe_int(usage.get("active_limit_chars")),
            ratio=_safe_float(usage.get("ratio")),
            compact_needed=_truthy(usage.get("compact_needed")) or _truthy(report.get("compact_needed")),
            pressure=str(usage.get("pressure") or ""),
            total_chars=_safe_int(usage.get("total_chars")),
            active_blocks=_safe_int(usage.get("active_blocks")),
            compacted_blocks=_safe_int(usage.get("compacted_blocks")),
            restored_blocks=_safe_int(usage.get("restored_blocks")),
            source_phase="compact_restore_contract_pending" if pending else "compact_restore_report",
            metadata=dict(usage),
        )
        return CodeWorkerCompactApiState(
            status=str(report.get("status") or ""),
            ok=_truthy(report.get("ok")),
            compact_needed=_truthy(report.get("compact_needed")) or context_usage.compact_needed,
            boundary_id=str(boundary.get("boundary_id") or report.get("boundary_id") or ""),
            compact_artifact_id=str(boundary.get("artifact_id") or contract.get("compact_artifact_id") or ""),
            restore_contract_id=str(contract.get("contract_id") or report.get("restore_contract_id") or ""),
            restore_segment_count=_safe_int(contract.get("restore_segment_count")),
            preserved_segment_count=len(_as_list(report.get("preserved_segments"))),
            compact_state_projection_status=str(projection.get("status") or ""),
            policy_status=str(policy.get("status") or ""),
            policy_blocked_rules=_safe_int(policy.get("blocked_rule_count")),
            context_usage=context_usage,
            latest_report=report,
            latest_projection=projection,
        )

    def _model_api_state(self, observations: Sequence[QueryPhaseObservation]) -> CodeWorkerModelApiState:
        stream = _latest_payload(observations, "model_stream_report", "model_stream")
        retry = _latest_payload(observations, "api_retry_report", "api_retry")
        watchdog = _latest_payload(observations, "model_stream_watchdog", "model_stream_watchdog")
        usage = _as_mapping(stream.get("usage"))
        envelope = _as_mapping(stream.get("envelope"))
        return CodeWorkerModelApiState(
            model_stream_status=str(stream.get("status") or ""),
            model_stream_ok=_truthy(stream.get("ok")),
            selected_model=str(envelope.get("model") or ""),
            error_kind=str(stream.get("error_kind") or ""),
            retry_status=str(retry.get("status") or ""),
            retry_ok=_truthy(retry.get("ok")),
            retry_count=_safe_int(retry.get("retry_count")),
            fallback_used=_truthy(retry.get("fallback_used")),
            final_model=str(retry.get("final_model") or ""),
            watchdog_status=str(watchdog.get("status") or ""),
            watchdog_signals=_safe_int(watchdog.get("signal_count") or watchdog.get("signals")),
            input_tokens=_safe_int(usage.get("input_tokens")),
            output_tokens=_safe_int(usage.get("output_tokens")),
            total_cost_usd=_safe_float(usage.get("estimated_cost_usd")),
            latest_stream=stream,
            latest_retry=retry,
        )

    def _restore_state(self, observations: Sequence[QueryPhaseObservation]) -> CodeWorkerRestoreApiState:
        app = _latest_payload(observations, "codeworker_restore_context_applied", "restore_application")
        integration = _latest_payload(observations, "codeworker_restore_integration", "restore_integration")
        metadata = _as_mapping(integration.get("metadata"))
        security = _as_mapping(app.get("security_snapshot"))
        return CodeWorkerRestoreApiState(
            integration_status=str(integration.get("status") or metadata.get("restore_integration_status") or ""),
            integration_ok=_truthy(integration.get("ok") or metadata.get("restore_integration_ok")),
            application_count=_safe_int(integration.get("application_count") or metadata.get("restore_integration_applications")),
            model_message_count=_safe_int(integration.get("model_message_count") or metadata.get("restore_integration_model_messages")),
            context_block_count=_safe_int(integration.get("context_block_count") or metadata.get("restore_integration_context_blocks")),
            restored_chars=_safe_int(integration.get("restored_chars") or metadata.get("restore_integration_restored_chars")),
            latest_contract_id=str(integration.get("latest_contract_id") or metadata.get("restore_integration_latest_contract_id") or app.get("contract_id") or ""),
            latest_turn_index=_safe_int(integration.get("latest_turn_index") or metadata.get("restore_integration_latest_turn_index") or app.get("turn_index")),
            pending_contract_count=_safe_int(integration.get("pending_contract_count") or metadata.get("restore_integration_pending_contracts")),
            untrusted_message_count=_safe_int(integration.get("untrusted_message_count") or metadata.get("restore_integration_untrusted_messages")),
            redacted_message_count=_safe_int(integration.get("redacted_message_count") or metadata.get("restore_integration_redacted_messages")),
            security_status=str(security.get("status") or metadata.get("context_security_status") or ""),
            security_ok=_truthy(security.get("ok") or metadata.get("context_security_ok")),
            latest_application=app,
            latest_integration=integration,
        )

    def _projection_findings(
        self,
        session: CodeWorkerSessionSummary,
        observations: Sequence[QueryPhaseObservation],
        compact_state: CodeWorkerCompactApiState,
        restore_state: CodeWorkerRestoreApiState,
        model_api: CodeWorkerModelApiState,
    ) -> list[CodeWorkerTaskApiFinding]:
        findings: list[CodeWorkerTaskApiFinding] = []
        if not session.present:
            findings.append(
                CodeWorkerTaskApiFinding(
                    code="CODEWORKER_SESSION_METADATA_MISSING",
                    severity=CodeWorkerTaskApiSeverity.WARNING if observations else CodeWorkerTaskApiSeverity.BLOCKER,
                    surface=CodeWorkerTaskApiSurface.CHECKPOINT,
                    message="Task checkpoint does not contain a CodeWorker session record.",
                )
            )
        if compact_state.compact_needed and not compact_state.has_restore_contract:
            findings.append(
                CodeWorkerTaskApiFinding(
                    code="COMPACT_NEEDED_WITHOUT_RESTORE_CONTRACT",
                    severity=CodeWorkerTaskApiSeverity.ERROR,
                    surface=CodeWorkerTaskApiSurface.COMPACT_STATE,
                    message="Compact state indicates pressure but no next-turn restore contract is visible.",
                )
            )
        if restore_state.application_count and not restore_state.applied:
            findings.append(
                CodeWorkerTaskApiFinding(
                    code="RESTORE_APPLICATION_WITHOUT_MODEL_MESSAGES",
                    severity=CodeWorkerTaskApiSeverity.ERROR,
                    surface=CodeWorkerTaskApiSurface.RESTORE,
                    message="Restore application exists but no model messages were projected into the next request.",
                )
            )
        if model_api.error_kind and model_api.error_kind != "none" and not model_api.recovered:
            findings.append(
                CodeWorkerTaskApiFinding(
                    code="MODEL_API_ERROR_NOT_RECOVERED",
                    severity=CodeWorkerTaskApiSeverity.ERROR,
                    surface=CodeWorkerTaskApiSurface.MODEL_API,
                    message="Model stream emitted an error that was not recovered by retry/fallback state.",
                    metadata={"error_kind": model_api.error_kind},
                )
            )
        return findings


def codeworker_task_api_metadata(projection: CodeWorkerSessionApiProjection | None) -> dict[str, str]:
    if projection is None:
        return {
            "codeworker_task_api_ok": "false",
            "codeworker_task_api_status": "missing",
            "codeworker_task_api_projection_id": "",
        }
    return projection.metadata()


def default_codeworker_task_api_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/query.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_task_api_projection.py",
            "decision": "zyra_module_migrated",
            "capability": "query stream phases are normalized into task-scoped API projections",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_task_api_projection.py",
            "decision": "zyra_module_migrated",
            "capability": "context usage, compact boundary, restore contract and next-turn application are exposed per task",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/claude.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_task_api_projection.py",
            "decision": "zyra_module_migrated",
            "capability": "model stream, API retry, fallback and typed error surfaces are exposed per task",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/**",
            "target_path": "packages/runtime/zyra_runtime/codeworker_task_api_projection.py",
            "decision": "adapter_encapsulated",
            "capability": "event-sourced task replay drives API state instead of sample fixtures",
        },
    )


def _phase_observations(events: Sequence[Mapping[str, Any]]) -> list[QueryPhaseObservation]:
    observations: list[QueryPhaseObservation] = []
    for event in events:
        payload = _as_mapping(event.get("payload"))
        query_session = _as_mapping(payload.get("query_session"))
        if not query_session:
            continue
        phase = str(query_session.get("phase") or "")
        if not phase:
            continue
        observations.append(
            QueryPhaseObservation(
                event_id=str(event.get("event_id") or ""),
                phase=phase,
                session_id=str(query_session.get("session_id") or ""),
                worker_request_id=str(query_session.get("worker_request_id") or ""),
                turn_index=_safe_int(query_session.get("turn_index") or _as_mapping(query_session.get("metadata")).get("turn_index")),
                created_at=str(event.get("created_at") or ""),
                payload=dict(query_session),
            )
        )
    return observations


def _observations_for_session(
    observations: Sequence[QueryPhaseObservation],
    *,
    session_id: str,
    worker_request_id: str,
) -> list[QueryPhaseObservation]:
    if worker_request_id:
        return [
            observation
            for observation in observations
            if observation.worker_request_id == worker_request_id
            or (
                not observation.worker_request_id
                and bool(session_id)
                and observation.session_id == session_id
            )
        ]
    if session_id:
        return [observation for observation in observations if observation.session_id == session_id]
    return list(observations)


def _trace_items_from_observations(observations: Sequence[QueryPhaseObservation]) -> list[CodeWorkerToolTraceItem]:
    items: list[CodeWorkerToolTraceItem] = []
    for observation in observations:
        phase = observation.phase
        payload = observation.payload
        kind = _trace_kind_for_phase(phase)
        if kind is None:
            continue
        trace_payload = _payload_for_trace_item(phase, payload)
        items.append(
            CodeWorkerToolTraceItem(
                trace_id=new_id("tooltrace"),
                kind=kind,
                phase=phase,
                event_id=observation.event_id,
                turn_index=_safe_int(trace_payload.get("turn_index") or payload.get("turn_index") or observation.turn_index),
                batch_index=_safe_int(trace_payload.get("batch_index") or payload.get("batch_index")),
                step_index=_safe_int(trace_payload.get("step_index") or payload.get("step_index")),
                tool_call_id=str(trace_payload.get("tool_call_id") or payload.get("tool_call_id") or ""),
                tool_name=str(trace_payload.get("tool_name") or payload.get("tool_name") or ""),
                ok=_optional_bool(trace_payload.get("ok") if "ok" in trace_payload else payload.get("ok")),
                error=str(trace_payload.get("error") or payload.get("error") or ""),
                summary=str(trace_payload.get("summary") or payload.get("summary") or ""),
                artifact_ids=tuple(str(item) for item in _as_list(trace_payload.get("artifact_ids") or payload.get("artifact_ids"))),
                metadata=to_jsonable(trace_payload),
                created_at=observation.created_at,
            )
        )
    return items


def _trace_kind_for_phase(phase: str) -> ToolTraceItemKind | None:
    if phase in {"turn_started", "turn_start", "turn_completed", "turn_end"}:
        return ToolTraceItemKind.TURN
    if phase in {"tool_batch_started", "tool_batch_completed"}:
        return ToolTraceItemKind.BATCH
    if phase in {"assistant_tool_use_accepted", "tool_call_started"}:
        return ToolTraceItemKind.TOOL_CALL
    if phase in {"tool_call_completed", "tool_result_context_report", "tool_result_message"}:
        return ToolTraceItemKind.TOOL_RESULT
    if phase in {"model_stream_report", "model_stream_frame", "model_provider_catalog", "model_stream_watchdog"}:
        return ToolTraceItemKind.MODEL_STREAM
    if phase in {"api_retry_report", "api_retry_playbook"}:
        return ToolTraceItemKind.API_RETRY
    if phase in {"context_compacted", "compact_boundary_created", "compact_needed", "compact_restore_contract_pending"}:
        return ToolTraceItemKind.COMPACT
    if phase in {"next_turn_restore_contract", "codeworker_restore_context_applied", "codeworker_restore_integration"}:
        return ToolTraceItemKind.RESTORE
    if phase in {"error", "tool_failure_signal", "watchdog_signal", "model_stream_watchdog_signal"}:
        return ToolTraceItemKind.ERROR
    return None


def _payload_for_trace_item(phase: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if phase == "model_stream_report":
        return _as_mapping(payload.get("model_stream"))
    if phase == "api_retry_report":
        return _as_mapping(payload.get("api_retry"))
    if phase == "api_retry_playbook":
        return _as_mapping(payload.get("api_retry_playbook"))
    if phase == "codeworker_restore_context_applied":
        return _as_mapping(payload.get("restore_application"))
    if phase == "codeworker_restore_integration":
        return _as_mapping(payload.get("restore_integration"))
    if phase == "compact_restore_contract_pending":
        return _as_mapping(payload.get("compact_restore"))
    if phase == "tool_result_message":
        return _as_mapping(payload.get("tool_result_message"))
    if phase == "tool_call_completed":
        return payload
    return payload


def _tool_trace_findings(
    items: Sequence[CodeWorkerToolTraceItem],
    repair: CodeWorkerRepairProjection,
) -> list[CodeWorkerTaskApiFinding]:
    findings: list[CodeWorkerTaskApiFinding] = []
    if not items:
        findings.append(
            CodeWorkerTaskApiFinding(
                code="CODEWORKER_TOOL_TRACE_EMPTY",
                severity=CodeWorkerTaskApiSeverity.WARNING,
                surface=CodeWorkerTaskApiSurface.TOOL_TRACE,
                message="No CodeWorker tool trace events were available for this task.",
            )
        )
        return findings
    failed_shell = [
        item
        for item in items
        if item.kind == ToolTraceItemKind.TOOL_RESULT
        and item.tool_name in {"shell", "bash", "run_command"}
        and item.failed
    ]
    if failed_shell and not repair.detected:
        findings.append(
            CodeWorkerTaskApiFinding(
                code="FAILED_SHELL_WITHOUT_REPAIR_TRACE",
                severity=CodeWorkerTaskApiSeverity.WARNING,
                surface=CodeWorkerTaskApiSurface.TOOL_TRACE,
                message="A shell failure was observed without a later file edit and successful verification command.",
                event_id=failed_shell[-1].event_id,
            )
        )
    return findings


def _repair_projection(items: Sequence[CodeWorkerToolTraceItem]) -> CodeWorkerRepairProjection:
    failed_shell: CodeWorkerToolTraceItem | None = None
    repair_edit: CodeWorkerToolTraceItem | None = None
    verification: CodeWorkerToolTraceItem | None = None
    for item in items:
        if (
            failed_shell is None
            and item.kind == ToolTraceItemKind.TOOL_RESULT
            and item.tool_name in {"shell", "bash", "run_command"}
            and item.failed
        ):
            failed_shell = item
            continue
        if failed_shell is not None and repair_edit is None and item.kind == ToolTraceItemKind.TOOL_RESULT:
            if item.tool_name in {"file_write", "file_edit", "apply_patch"} and item.ok is not False:
                repair_edit = item
                continue
        if failed_shell is not None and repair_edit is not None and item.kind == ToolTraceItemKind.TOOL_RESULT:
            if item.tool_name in {"shell", "bash", "run_command"} and item.ok is True:
                verification = item
                break
    if failed_shell and repair_edit and verification:
        return CodeWorkerRepairProjection(
            detected=True,
            failed_tool_call_id=failed_shell.tool_call_id,
            failed_command=str(failed_shell.metadata.get("command") or failed_shell.summary),
            repair_tool_call_id=repair_edit.tool_call_id,
            repair_path=str(repair_edit.metadata.get("path") or repair_edit.summary),
            verification_tool_call_id=verification.tool_call_id,
            verification_command=str(verification.metadata.get("command") or verification.summary),
            metadata={
                "failed_event_id": failed_shell.event_id,
                "repair_event_id": repair_edit.event_id,
                "verification_event_id": verification.event_id,
            },
        )
    return CodeWorkerRepairProjection(detected=False)


def _latest_payload(observations: Sequence[QueryPhaseObservation], phase: str, *keys: str) -> dict[str, Any]:
    """Read a phase's payload block, tolerating both block names.

    The retired Python runtimes named the block with a short name
    (``restore_integration``); the canonical TypeScript runtime names it after
    the phase (``codeworker_restore_integration``).  A reader bound to one of
    them silently projected an empty block for every run, which is how the task
    API came to report ``integration_ok: false`` on runs whose restore report
    said ``ok: true``.
    """

    for observation in reversed(observations):
        if observation.phase != phase:
            continue
        for key in (*keys, phase):
            value = observation.payload.get(key)
            if isinstance(value, Mapping):
                return dict(value)
    return {}


def _latest_attr(observations: Sequence[QueryPhaseObservation], attr: str) -> str:
    for observation in reversed(observations):
        value = getattr(observation, attr, "")
        if value:
            return str(value)
    return ""


def _max_turn_index(observations: Sequence[QueryPhaseObservation]) -> int:
    return max((observation.turn_index for observation in observations), default=0)


def _event_view(event: Any) -> dict[str, Any]:
    if isinstance(event, EventRecord):
        return event.to_dict() if hasattr(event, "to_dict") else to_jsonable(event)
    if isinstance(event, Mapping):
        return dict(event)
    return to_jsonable(event) if isinstance(to_jsonable(event), dict) else {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    return _truthy(value)
