from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


class SessionLifecycleStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    EMPTY = "empty"


class SessionLifecycleSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class SessionLifecycleStage(StrEnum):
    SEED = "seed"
    INPUT = "input"
    CONTEXT = "context"
    STORE = "store"
    REPLAY = "replay"
    TURN_BINDING = "turn_binding"
    FOUNDATION_AUDIT = "foundation_audit"
    QUERY_ENGINE = "query_engine"
    TOOL_LOOP = "tool_loop"
    QUERY_COMPLETE = "query_complete"
    TRANSCRIPT = "transcript"
    ACCEPTANCE = "acceptance"
    UNKNOWN = "unknown"


class SessionLifecycleSurface(StrEnum):
    SESSION_FOUNDATION = "session_foundation"
    SESSION_REPLAY = "session_replay"
    TURN_LIFECYCLE = "turn_lifecycle"
    FOUNDATION_AUDIT = "foundation_audit"
    QUERY_ENGINE = "query_engine"
    TOOL_LOOP = "tool_loop"
    TRANSCRIPT_MAPPING = "transcript_mapping"
    ACCEPTANCE = "acceptance"
    EVENT_LOG = "event_log"


@dataclass(frozen=True, slots=True)
class SessionLifecycleEventView:
    index: int
    phase: str
    stage: SessionLifecycleStage
    surface: SessionLifecycleSurface
    run_id: str
    task_id: str
    node_id: str
    session_id: str
    worker_request_id: str
    ok: bool | None
    status: str
    event_type: str
    event_id: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.stage != SessionLifecycleStage.UNKNOWN

    @property
    def failed(self) -> bool:
        return self.ok is False

    @property
    def stage_rank(self) -> int:
        return stage_rank(self.stage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "phase": self.phase,
            "stage": str(self.stage),
            "surface": str(self.surface),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "failed": self.failed,
            "status": self.status,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionLifecycleTransition:
    transition_id: str
    from_stage: SessionLifecycleStage
    to_stage: SessionLifecycleStage
    from_phase: str
    to_phase: str
    from_index: int
    to_index: int
    status: SessionLifecycleStatus
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {SessionLifecycleStatus.READY, SessionLifecycleStatus.DEGRADED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "from_stage": str(self.from_stage),
            "to_stage": str(self.to_stage),
            "from_phase": self.from_phase,
            "to_phase": self.to_phase,
            "from_index": self.from_index,
            "to_index": self.to_index,
            "status": str(self.status),
            "ok": self.ok,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionLifecycleCheckpoint:
    checkpoint_id: str
    stage: SessionLifecycleStage
    phase: str
    first_index: int
    last_index: int
    count: int
    ok_count: int
    failed_count: int
    status_values: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.count > 0 and self.failed_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "stage": str(self.stage),
            "phase": self.phase,
            "first_index": self.first_index,
            "last_index": self.last_index,
            "count": self.count,
            "ok_count": self.ok_count,
            "failed_count": self.failed_count,
            "status_values": list(self.status_values),
            "ok": self.ok,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionLifecycleFinding:
    code: str
    severity: SessionLifecycleSeverity
    surface: SessionLifecycleSurface
    message: str
    phase: str = ""
    stage: SessionLifecycleStage = SessionLifecycleStage.UNKNOWN
    event_index: int = -1
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == SessionLifecycleSeverity.BLOCKER

    @property
    def passed(self) -> bool:
        return self.severity == SessionLifecycleSeverity.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "phase": self.phase,
            "stage": str(self.stage),
            "event_index": self.event_index,
            "blocking": self.blocking,
            "passed": self.passed,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionLifecycleStateReport:
    report_id: str
    status: SessionLifecycleStatus
    session_id: str
    worker_request_id: str
    events: tuple[SessionLifecycleEventView, ...]
    checkpoints: tuple[SessionLifecycleCheckpoint, ...]
    transitions: tuple[SessionLifecycleTransition, ...]
    findings: tuple[SessionLifecycleFinding, ...]
    required_phases: tuple[str, ...]
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {SessionLifecycleStatus.READY, SessionLifecycleStatus.DEGRADED} and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == SessionLifecycleSeverity.WARNING)

    @property
    def pass_count(self) -> int:
        return sum(1 for finding in self.findings if finding.passed)

    @property
    def phase_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            if not event.phase:
                continue
            counts[event.phase] = counts.get(event.phase, 0) + 1
        return counts

    @property
    def stage_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[str(event.stage)] = counts.get(str(event.stage), 0) + 1
        return counts

    @property
    def missing_required_phases(self) -> tuple[str, ...]:
        present = set(self.phase_counts)
        return tuple(phase for phase in self.required_phases if phase not in present)

    def metadata_values(self) -> dict[str, str]:
        return {
            "session_lifecycle_report_id": self.report_id,
            "session_lifecycle_ok": str(self.ok).lower(),
            "session_lifecycle_status": str(self.status),
            "session_lifecycle_session_id": self.session_id,
            "session_lifecycle_worker_request_id": self.worker_request_id,
            "session_lifecycle_event_count": str(len(self.events)),
            "session_lifecycle_checkpoint_count": str(len(self.checkpoints)),
            "session_lifecycle_transition_count": str(len(self.transitions)),
            "session_lifecycle_blockers": str(self.blocker_count),
            "session_lifecycle_warnings": str(self.warning_count),
            "session_lifecycle_missing_required_phases": str(len(self.missing_required_phases)),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "ok": self.ok,
            "status": str(self.status),
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "events": [event.to_dict() for event in self.events],
            "checkpoints": [checkpoint.to_dict() for checkpoint in self.checkpoints],
            "transitions": [transition.to_dict() for transition in self.transitions],
            "findings": [finding.to_dict() for finding in self.findings],
            "required_phases": list(self.required_phases),
            "missing_required_phases": list(self.missing_required_phases),
            "phase_counts": self.phase_counts,
            "stage_counts": self.stage_counts,
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "pass_count": self.pass_count,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class SessionLifecycleRuntime:
    """Projects query session EventRecords into a lifecycle state report."""

    def __init__(self, *, required_phases: Sequence[str] | None = None) -> None:
        self.required_phases = tuple(required_phases or ())

    def build_report(
        self,
        events: Sequence[Any],
        *,
        session_id: str = "",
        worker_request_id: str = "",
        require_query_engine: bool = True,
        require_transcript: bool = True,
    ) -> SessionLifecycleStateReport:
        required = self.required_phases or default_required_lifecycle_phases(
            require_query_engine=require_query_engine,
            require_transcript=require_transcript,
        )
        views = tuple(project_lifecycle_event(event, index=index) for index, event in enumerate(events))
        selected = tuple(view for view in views if view.phase or view.known)
        resolved_session_id = session_id or first_non_empty(view.session_id for view in selected)
        resolved_worker_request_id = worker_request_id or first_non_empty(view.worker_request_id for view in selected)
        checkpoints = tuple(build_lifecycle_checkpoints(selected))
        transitions = tuple(build_lifecycle_transitions(selected))
        findings = list(validate_lifecycle_events(
            selected,
            required_phases=required,
            session_id=resolved_session_id,
            worker_request_id=resolved_worker_request_id,
            require_query_engine=require_query_engine,
            require_transcript=require_transcript,
        ))
        status = lifecycle_status(events=selected, transitions=transitions, findings=findings)
        return SessionLifecycleStateReport(
            report_id=new_id("slife"),
            status=status,
            session_id=resolved_session_id,
            worker_request_id=resolved_worker_request_id,
            events=selected,
            checkpoints=checkpoints,
            transitions=transitions,
            findings=tuple(findings),
            required_phases=tuple(required),
            metadata={
                "require_query_engine": require_query_engine,
                "require_transcript": require_transcript,
                "source": "SessionLifecycleRuntime.build_report",
            },
        )

    def event_for_report(
        self,
        report: SessionLifecycleStateReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "session_lifecycle_state",
                    "report_id": report.report_id,
                    "ok": report.ok,
                    "status": str(report.status),
                    "event_count": len(report.events),
                    "checkpoint_count": len(report.checkpoints),
                    "transition_count": len(report.transitions),
                    "blocker_count": report.blocker_count,
                    "warning_count": report.warning_count,
                    "missing_required_phases": list(report.missing_required_phases),
                }
            },
        )


def project_lifecycle_event(event: Any, *, index: int) -> SessionLifecycleEventView:
    payload = event_payload(event)
    query_session = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
    worker_request = payload.get("worker_request") if isinstance(payload.get("worker_request"), Mapping) else {}
    worker_result = payload.get("worker_result") if isinstance(payload.get("worker_result"), Mapping) else {}
    phase = str(query_session.get("phase") or "")
    if not phase and worker_result:
        phase = "worker_result"
    session_id = str(query_session.get("session_id") or worker_result.get("metadata", {}).get("query_session_id") or "")
    worker_request_id = str(
        query_session.get("worker_request_id")
        or worker_request.get("request_id")
        or worker_result.get("request_id")
        or ""
    )
    ok_value: bool | None = None
    if "ok" in query_session:
        ok_value = query_session.get("ok") is True
    elif "ok" in worker_result:
        ok_value = worker_result.get("ok") is True
    status = str(query_session.get("status") or worker_result.get("error") or "")
    return SessionLifecycleEventView(
        index=index,
        phase=phase,
        stage=stage_for_phase(phase),
        surface=surface_for_phase(phase),
        run_id=event_attr(event, "run_id"),
        task_id=event_attr(event, "task_id"),
        node_id=event_attr(event, "node_id"),
        session_id=session_id,
        worker_request_id=worker_request_id,
        ok=ok_value,
        status=status,
        event_type=event_attr(event, "event_type"),
        event_id=str(payload.get("event_id") or query_session.get("event_id") or ""),
        created_at=str(payload.get("created_at") or query_session.get("created_at") or ""),
        metadata={
            "payload_keys": sorted(str(key) for key in payload.keys()),
            "query_session_keys": sorted(str(key) for key in query_session.keys()),
        },
    )


def build_lifecycle_checkpoints(events: Sequence[SessionLifecycleEventView]) -> Iterable[SessionLifecycleCheckpoint]:
    grouped: dict[tuple[SessionLifecycleStage, str], list[SessionLifecycleEventView]] = {}
    for event in events:
        key = (event.stage, event.phase)
        grouped.setdefault(key, []).append(event)
    for (stage, phase), items in grouped.items():
        ok_count = sum(1 for item in items if item.ok is True)
        failed_count = sum(1 for item in items if item.failed)
        status_values = tuple(sorted({item.status for item in items if item.status}))
        yield SessionLifecycleCheckpoint(
            checkpoint_id=new_id("slchk"),
            stage=stage,
            phase=phase,
            first_index=min(item.index for item in items),
            last_index=max(item.index for item in items),
            count=len(items),
            ok_count=ok_count,
            failed_count=failed_count,
            status_values=status_values,
            metadata={"surface": str(items[-1].surface)},
        )


def build_lifecycle_transitions(events: Sequence[SessionLifecycleEventView]) -> Iterable[SessionLifecycleTransition]:
    known = [event for event in events if event.known]
    for previous, current in zip(known, known[1:]):
        status = SessionLifecycleStatus.READY
        metadata: dict[str, Any] = {}
        if current.stage_rank < previous.stage_rank:
            status = SessionLifecycleStatus.BLOCKED
            metadata["reason"] = "stage_rank_regressed"
        yield SessionLifecycleTransition(
            transition_id=new_id("sltr"),
            from_stage=previous.stage,
            to_stage=current.stage,
            from_phase=previous.phase,
            to_phase=current.phase,
            from_index=previous.index,
            to_index=current.index,
            status=status,
            metadata=metadata,
        )


def validate_lifecycle_events(
    events: Sequence[SessionLifecycleEventView],
    *,
    required_phases: Sequence[str],
    session_id: str,
    worker_request_id: str,
    require_query_engine: bool,
    require_transcript: bool,
) -> Iterable[SessionLifecycleFinding]:
    if not events:
        yield SessionLifecycleFinding(
            code="empty_lifecycle",
            severity=SessionLifecycleSeverity.BLOCKER,
            surface=SessionLifecycleSurface.EVENT_LOG,
            message="No query session lifecycle events were available.",
        )
        return
    phase_counts: dict[str, int] = {}
    for event in events:
        if event.phase:
            phase_counts[event.phase] = phase_counts.get(event.phase, 0) + 1
        if event.failed:
            recovered_tool_failure = is_recovered_tool_failure(event, events)
            yield SessionLifecycleFinding(
                code="event_failed",
                severity=SessionLifecycleSeverity.WARNING if recovered_tool_failure else SessionLifecycleSeverity.BLOCKER,
                surface=event.surface,
                message=(
                    "A lifecycle event reported ok=false but was followed by a continue recovery."
                    if recovered_tool_failure
                    else "A lifecycle event reported ok=false."
                ),
                phase=event.phase,
                stage=event.stage,
                event_index=event.index,
                metadata={"status": event.status, "recovered": recovered_tool_failure},
            )
        if (
            session_id
            and event.session_id
            and event.session_id != session_id
            and not allows_replay_source_identity(event)
        ):
            yield SessionLifecycleFinding(
                code="session_id_mismatch",
                severity=SessionLifecycleSeverity.BLOCKER,
                surface=event.surface,
                message="Lifecycle event belongs to a different query session.",
                phase=event.phase,
                stage=event.stage,
                event_index=event.index,
                metadata={"expected": session_id, "actual": event.session_id},
            )
        if (
            worker_request_id
            and event.worker_request_id
            and event.worker_request_id != worker_request_id
            and not allows_replay_source_identity(event)
        ):
            yield SessionLifecycleFinding(
                code="worker_request_id_mismatch",
                severity=SessionLifecycleSeverity.BLOCKER,
                surface=event.surface,
                message="Lifecycle event belongs to a different worker request.",
                phase=event.phase,
                stage=event.stage,
                event_index=event.index,
                metadata={"expected": worker_request_id, "actual": event.worker_request_id},
            )
    for phase in required_phases:
        if phase not in phase_counts:
            yield SessionLifecycleFinding(
                code="required_phase_missing",
                severity=SessionLifecycleSeverity.BLOCKER,
                surface=surface_for_phase(phase),
                message="Required lifecycle phase is missing.",
                phase=phase,
                stage=stage_for_phase(phase),
                metadata={
                    "require_query_engine": require_query_engine,
                    "require_transcript": require_transcript,
                },
            )
        else:
            yield SessionLifecycleFinding(
                code="required_phase_present",
                severity=SessionLifecycleSeverity.PASS,
                surface=surface_for_phase(phase),
                message="Required lifecycle phase is present.",
                phase=phase,
                stage=stage_for_phase(phase),
                metadata={"count": phase_counts[phase]},
            )
    for phase, count in phase_counts.items():
        if phase in singleton_lifecycle_phases() and count > 1:
            yield SessionLifecycleFinding(
                code="duplicate_singleton_phase",
                severity=SessionLifecycleSeverity.WARNING,
                surface=surface_for_phase(phase),
                message="A singleton lifecycle phase appeared more than once.",
                phase=phase,
                stage=stage_for_phase(phase),
                metadata={"count": count},
            )
    yield from validate_lifecycle_order(events)


def allows_replay_source_identity(event: SessionLifecycleEventView) -> bool:
    return event.phase == "session_replay_plan" and event.stage == SessionLifecycleStage.REPLAY


def validate_lifecycle_order(events: Sequence[SessionLifecycleEventView]) -> Iterable[SessionLifecycleFinding]:
    known = [event for event in events if event.known]
    previous: SessionLifecycleEventView | None = None
    for event in known:
        if previous is not None and event.stage_rank < previous.stage_rank:
            yield SessionLifecycleFinding(
                code="lifecycle_stage_regressed",
                severity=SessionLifecycleSeverity.BLOCKER,
                surface=event.surface,
                message="Lifecycle stage order regressed.",
                phase=event.phase,
                stage=event.stage,
                event_index=event.index,
                metadata={
                    "previous_phase": previous.phase,
                    "previous_stage": str(previous.stage),
                    "previous_index": previous.index,
                },
            )
        previous = event


def is_recovered_tool_failure(event: SessionLifecycleEventView, events: Sequence[SessionLifecycleEventView]) -> bool:
    if event.phase not in {"tool_call_completed", "tool_batch_completed", "tool_failure_signal", "watchdog_signal", "error"}:
        return False
    phases = [item.phase for item in events]
    return "continue" in phases and "session_completed" in phases


def lifecycle_status(
    *,
    events: Sequence[SessionLifecycleEventView],
    transitions: Sequence[SessionLifecycleTransition],
    findings: Sequence[SessionLifecycleFinding],
) -> SessionLifecycleStatus:
    if not events:
        return SessionLifecycleStatus.EMPTY
    if any(finding.blocking for finding in findings) or any(not transition.ok for transition in transitions):
        return SessionLifecycleStatus.BLOCKED
    if any(finding.severity == SessionLifecycleSeverity.WARNING for finding in findings) or any(
        transition.status == SessionLifecycleStatus.DEGRADED for transition in transitions
    ):
        return SessionLifecycleStatus.DEGRADED
    return SessionLifecycleStatus.READY


def stage_for_phase(phase: str) -> SessionLifecycleStage:
    normalized = phase.lower()
    if normalized == "query_session_seed_created":
        return SessionLifecycleStage.SEED
    if normalized == "query_input_processed":
        return SessionLifecycleStage.INPUT
    if normalized == "context_snapshot_ready":
        return SessionLifecycleStage.CONTEXT
    if normalized == "session_store_append":
        return SessionLifecycleStage.STORE
    if normalized == "session_replay_plan":
        return SessionLifecycleStage.REPLAY
    if normalized == "turn_lifecycle_projection":
        return SessionLifecycleStage.TURN_BINDING
    if normalized == "session_foundation_audit":
        return SessionLifecycleStage.FOUNDATION_AUDIT
    if normalized in {"tool_loop_plan", "tool_batch_started", "tool_batch_completed", "tool_call_started", "tool_call_completed"}:
        return SessionLifecycleStage.TOOL_LOOP
    if normalized in {"session_started", "turn_started", "stream_request_start", "message_delta", "context_compacted"}:
        return SessionLifecycleStage.QUERY_ENGINE
    if normalized in {"session_completed", "query_session_snapshot"}:
        return SessionLifecycleStage.QUERY_COMPLETE
    if normalized == "transcript_event_mapping":
        return SessionLifecycleStage.TRANSCRIPT
    if normalized == "session_acceptance":
        return SessionLifecycleStage.ACCEPTANCE
    return SessionLifecycleStage.UNKNOWN


def surface_for_phase(phase: str) -> SessionLifecycleSurface:
    stage = stage_for_phase(phase)
    if stage in {SessionLifecycleStage.SEED, SessionLifecycleStage.INPUT, SessionLifecycleStage.CONTEXT, SessionLifecycleStage.STORE}:
        return SessionLifecycleSurface.SESSION_FOUNDATION
    if stage == SessionLifecycleStage.REPLAY:
        return SessionLifecycleSurface.SESSION_REPLAY
    if stage == SessionLifecycleStage.TURN_BINDING:
        return SessionLifecycleSurface.TURN_LIFECYCLE
    if stage == SessionLifecycleStage.FOUNDATION_AUDIT:
        return SessionLifecycleSurface.FOUNDATION_AUDIT
    if stage == SessionLifecycleStage.TOOL_LOOP:
        return SessionLifecycleSurface.TOOL_LOOP
    if stage in {SessionLifecycleStage.QUERY_ENGINE, SessionLifecycleStage.QUERY_COMPLETE}:
        return SessionLifecycleSurface.QUERY_ENGINE
    if stage == SessionLifecycleStage.TRANSCRIPT:
        return SessionLifecycleSurface.TRANSCRIPT_MAPPING
    if stage == SessionLifecycleStage.ACCEPTANCE:
        return SessionLifecycleSurface.ACCEPTANCE
    return SessionLifecycleSurface.EVENT_LOG


def stage_rank(stage: SessionLifecycleStage) -> int:
    order = {
        SessionLifecycleStage.SEED: 10,
        SessionLifecycleStage.INPUT: 20,
        SessionLifecycleStage.CONTEXT: 30,
        SessionLifecycleStage.STORE: 40,
        SessionLifecycleStage.REPLAY: 50,
        SessionLifecycleStage.TURN_BINDING: 60,
        SessionLifecycleStage.FOUNDATION_AUDIT: 70,
        SessionLifecycleStage.QUERY_ENGINE: 80,
        SessionLifecycleStage.TOOL_LOOP: 80,
        SessionLifecycleStage.QUERY_COMPLETE: 100,
        SessionLifecycleStage.TRANSCRIPT: 110,
        SessionLifecycleStage.ACCEPTANCE: 120,
        SessionLifecycleStage.UNKNOWN: 999,
    }
    return order[stage]


def default_required_lifecycle_phases(*, require_query_engine: bool, require_transcript: bool) -> tuple[str, ...]:
    phases = [
        "query_session_seed_created",
        "query_input_processed",
        "context_snapshot_ready",
        "session_store_append",
        "turn_lifecycle_projection",
        "session_foundation_audit",
    ]
    if require_query_engine:
        phases.extend(["session_started", "stream_request_start", "session_completed"])
    if require_transcript:
        phases.extend(["transcript_event_mapping", "session_acceptance"])
    return tuple(phases)


def singleton_lifecycle_phases() -> tuple[str, ...]:
    return (
        "query_session_seed_created",
        "query_input_processed",
        "context_snapshot_ready",
        "turn_lifecycle_projection",
        "session_foundation_audit",
        "transcript_event_mapping",
    )


def event_payload(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        payload = event.get("payload", {})
        return dict(payload) if isinstance(payload, Mapping) else {}
    payload = getattr(event, "payload", {})
    return dict(payload) if isinstance(payload, Mapping) else {}


def event_attr(event: Any, name: str) -> str:
    if isinstance(event, Mapping):
        value = event.get(name, "")
    else:
        value = getattr(event, name, "")
    return str(value or "")


def first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


def session_lifecycle_metadata(report: SessionLifecycleStateReport | None) -> dict[str, str]:
    if report is None:
        return {
            "session_lifecycle_report_id": "",
            "session_lifecycle_ok": "",
            "session_lifecycle_status": "",
        }
    return report.metadata_values()


def render_session_lifecycle_markdown(report: SessionLifecycleStateReport) -> str:
    lines = [
        "# Session Lifecycle State",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- report_id: `{report.report_id}`",
        f"- session_id: `{report.session_id}`",
        f"- worker_request_id: `{report.worker_request_id}`",
        f"- events: `{len(report.events)}`",
        f"- checkpoints: `{len(report.checkpoints)}`",
        f"- transitions: `{len(report.transitions)}`",
        f"- blockers: `{report.blocker_count}`",
        f"- warnings: `{report.warning_count}`",
        "",
        "## Checkpoints",
        "",
    ]
    for checkpoint in report.checkpoints:
        lines.append(
            f"- `{checkpoint.stage}` `{checkpoint.phase}` count=`{checkpoint.count}` "
            f"failed=`{checkpoint.failed_count}`"
        )
    lines.extend(["", "## Findings", ""])
    for finding in report.findings:
        lines.append(f"- `{finding.severity}` `{finding.phase}` `{finding.code}` {finding.message}")
    return "\n".join(lines) + "\n"


def lifecycle_report_from_payload(payload: Mapping[str, Any]) -> SessionLifecycleStateReport:
    events = tuple(
        SessionLifecycleEventView(
            index=_safe_int(item.get("index"), default=0),
            phase=str(item.get("phase") or ""),
            stage=_enum_or_default(SessionLifecycleStage, item.get("stage"), SessionLifecycleStage.UNKNOWN),
            surface=_enum_or_default(SessionLifecycleSurface, item.get("surface"), SessionLifecycleSurface.EVENT_LOG),
            run_id=str(item.get("run_id") or ""),
            task_id=str(item.get("task_id") or ""),
            node_id=str(item.get("node_id") or ""),
            session_id=str(item.get("session_id") or ""),
            worker_request_id=str(item.get("worker_request_id") or ""),
            ok=item.get("ok") if isinstance(item.get("ok"), bool) else None,
            status=str(item.get("status") or ""),
            event_type=str(item.get("event_type") or ""),
            event_id=str(item.get("event_id") or ""),
            created_at=str(item.get("created_at") or ""),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("events", [])
        if isinstance(item, Mapping)
    )
    findings = tuple(
        SessionLifecycleFinding(
            code=str(item.get("code") or ""),
            severity=_enum_or_default(SessionLifecycleSeverity, item.get("severity"), SessionLifecycleSeverity.INFO),
            surface=_enum_or_default(SessionLifecycleSurface, item.get("surface"), SessionLifecycleSurface.EVENT_LOG),
            message=str(item.get("message") or ""),
            phase=str(item.get("phase") or ""),
            stage=_enum_or_default(SessionLifecycleStage, item.get("stage"), SessionLifecycleStage.UNKNOWN),
            event_index=_safe_int(item.get("event_index"), default=-1),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("findings", [])
        if isinstance(item, Mapping)
    )
    return SessionLifecycleStateReport(
        report_id=str(payload.get("report_id") or new_id("slife")),
        status=_enum_or_default(SessionLifecycleStatus, payload.get("status"), SessionLifecycleStatus.BLOCKED),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        events=events,
        checkpoints=tuple(build_lifecycle_checkpoints(events)),
        transitions=tuple(build_lifecycle_transitions(events)),
        findings=findings,
        required_phases=tuple(str(item) for item in payload.get("required_phases", []) if item),
        created_at=str(payload.get("created_at") or now_iso()),
        metadata=dict(payload.get("metadata") or {}),
    )


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default
