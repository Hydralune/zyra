from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


class QuerySessionEventFlowStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class QuerySessionEventFlowSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class QuerySessionEventFlowSurface(StrEnum):
    SESSION_STORE = "session_store"
    INPUT = "input"
    CONTEXT = "context"
    QUERY_ENTRY = "query_entry"
    QUERY_ENGINE = "query_engine"
    CONTROL = "control"
    TRANSCRIPT = "transcript"
    LIFECYCLE = "lifecycle"


@dataclass(frozen=True, slots=True)
class QuerySessionPhaseObservation:
    phase: str
    sequence: int
    event_type: str
    run_id: str
    task_id: str
    node_id: str
    session_id: str = ""
    worker_request_id: str = ""
    ok: bool | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def canonical_phase(self) -> str:
        return canonical_query_session_phase(self.phase)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "canonical_phase": self.canonical_phase,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "payload": to_jsonable(self.payload),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionEventFlowRequirement:
    requirement_id: str
    phase: str
    surface: QuerySessionEventFlowSurface
    required: bool = True
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    allowed_when_blocked: bool = True
    description: str = ""

    @property
    def canonical_phase(self) -> str:
        return canonical_query_session_phase(self.phase)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "phase": self.phase,
            "canonical_phase": self.canonical_phase,
            "surface": str(self.surface),
            "required": self.required,
            "before": list(self.before),
            "after": list(self.after),
            "allowed_when_blocked": self.allowed_when_blocked,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class QuerySessionEventFlowFinding:
    code: str
    severity: QuerySessionEventFlowSeverity
    surface: QuerySessionEventFlowSurface
    message: str
    phase: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == QuerySessionEventFlowSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "phase": self.phase,
            "blocking": self.blocking,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionEventFlowReport:
    report_id: str
    status: QuerySessionEventFlowStatus
    session_id: str
    worker_request_id: str
    observations: tuple[QuerySessionPhaseObservation, ...]
    requirements: tuple[QuerySessionEventFlowRequirement, ...]
    findings: tuple[QuerySessionEventFlowFinding, ...]
    blocked_before_engine: bool
    expected_engine_stream: bool
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {QuerySessionEventFlowStatus.READY, QuerySessionEventFlowStatus.DEGRADED}

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == QuerySessionEventFlowSeverity.WARNING)

    @property
    def phase_order(self) -> tuple[str, ...]:
        return tuple(observation.canonical_phase for observation in self.observations)

    @property
    def first_blocker_code(self) -> str:
        for finding in self.findings:
            if finding.blocking:
                return finding.code
        return ""

    def phase_index(self, phase: str) -> int | None:
        target = canonical_query_session_phase(phase)
        for observation in self.observations:
            if observation.canonical_phase == target:
                return observation.sequence
        return None

    def metadata_values(self) -> dict[str, str]:
        return {
            "query_event_flow_report_id": self.report_id,
            "query_event_flow_ok": str(self.ok).lower(),
            "query_event_flow_status": str(self.status),
            "query_event_flow_blockers": str(self.blocker_count),
            "query_event_flow_warnings": str(self.warning_count),
            "query_event_flow_first_blocker": self.first_blocker_code,
            "query_event_flow_observation_count": str(len(self.observations)),
            "query_event_flow_expected_stream": str(self.expected_engine_stream).lower(),
            "query_event_flow_blocked_before_engine": str(self.blocked_before_engine).lower(),
            "query_event_flow_has_query_started": str(self.phase_index("query_started") is not None).lower(),
            "query_event_flow_has_stream_request_start": str(
                self.phase_index("stream_request_start") is not None
            ).lower(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "observations": [observation.to_dict() for observation in self.observations],
            "requirements": [requirement.to_dict() for requirement in self.requirements],
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "first_blocker_code": self.first_blocker_code,
            "phase_order": list(self.phase_order),
            "blocked_before_engine": self.blocked_before_engine,
            "expected_engine_stream": self.expected_engine_stream,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class QuerySessionEventFlowRuntime:
    """Audits the observable pre-query and QueryEngine event sequence."""

    def build_report(
        self,
        events: Sequence[EventRecord],
        *,
        session_id: str,
        worker_request_id: str,
        expected_engine_stream: bool,
        blocked_before_engine: bool = False,
    ) -> QuerySessionEventFlowReport:
        observations = tuple(observe_query_session_phases(events))
        requirements = tuple(
            default_query_session_event_flow_requirements(
                expected_engine_stream=expected_engine_stream,
                blocked_before_engine=blocked_before_engine,
            )
        )
        findings = [
            *validate_required_phases(observations, requirements),
            *validate_phase_order(observations, requirements),
            *validate_engine_boundary(
                observations,
                expected_engine_stream=expected_engine_stream,
                blocked_before_engine=blocked_before_engine,
            ),
            *validate_session_identity(observations, session_id=session_id, worker_request_id=worker_request_id),
        ]
        status = event_flow_status_from_findings(findings)
        return QuerySessionEventFlowReport(
            report_id=new_id("qflow"),
            status=status,
            session_id=session_id,
            worker_request_id=worker_request_id,
            observations=observations,
            requirements=requirements,
            findings=tuple(findings),
            blocked_before_engine=blocked_before_engine,
            expected_engine_stream=expected_engine_stream,
            metadata={
                "source_path": "src/QueryEngine.ts",
                "target_path": "packages/runtime/zyra_runtime/claude_query_session_event_flow.py",
                "owner_unit": "M1-02B",
            },
        )

    def event_for_report(
        self,
        report: QuerySessionEventFlowReport,
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
                    "phase": "query_event_flow_audit",
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "ok": report.ok,
                    "status": str(report.status),
                    "blocker_count": report.blocker_count,
                    "warning_count": report.warning_count,
                    "first_blocker_code": report.first_blocker_code,
                    "phase_order": list(report.phase_order),
                    "expected_engine_stream": report.expected_engine_stream,
                    "blocked_before_engine": report.blocked_before_engine,
                }
            },
        )


def observe_query_session_phases(events: Sequence[EventRecord]) -> Iterable[QuerySessionPhaseObservation]:
    sequence = 0
    for event in events:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        query_payload = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else None
        if query_payload is None:
            continue
        phase = str(query_payload.get("phase") or "")
        if not phase:
            continue
        sequence += 1
        yield QuerySessionPhaseObservation(
            phase=phase,
            sequence=sequence,
            event_type=str(event.event_type),
            run_id=event.run_id,
            task_id=event.task_id,
            node_id=event.node_id or "",
            session_id=str(query_payload.get("session_id") or ""),
            worker_request_id=str(query_payload.get("worker_request_id") or ""),
            ok=query_payload.get("ok") if isinstance(query_payload.get("ok"), bool) else None,
            payload=dict(query_payload),
        )


def default_query_session_event_flow_requirements(
    *,
    expected_engine_stream: bool,
    blocked_before_engine: bool,
) -> Iterable[QuerySessionEventFlowRequirement]:
    yield QuerySessionEventFlowRequirement(
        requirement_id="session_created_visible",
        phase="session_created",
        surface=QuerySessionEventFlowSurface.SESSION_STORE,
        required=True,
        before=("input_processed", "context_assembled", "query_started"),
        description="A CodeWorker session seed must be visible before input/context/query entry.",
    )
    yield QuerySessionEventFlowRequirement(
        requirement_id="input_processed_visible",
        phase="input_processed",
        surface=QuerySessionEventFlowSurface.INPUT,
        required=True,
        after=("session_created",),
        before=("context_assembled", "query_started"),
        description="QueryInputProcessor must be observable before query entry.",
    )
    yield QuerySessionEventFlowRequirement(
        requirement_id="context_assembled_visible",
        phase="context_assembled",
        surface=QuerySessionEventFlowSurface.CONTEXT,
        required=True,
        after=("session_created", "input_processed"),
        before=("query_started",),
        description="ContextAssemblyRuntime snapshot must be observable before query entry.",
    )
    yield QuerySessionEventFlowRequirement(
        requirement_id="query_entry_packet_visible",
        phase="query_entry_packet_ready",
        surface=QuerySessionEventFlowSurface.QUERY_ENTRY,
        required=True,
        after=("context_assembled",),
        before=("query_started", "stream_request_start"),
        description="The query entry packet must be emitted before QueryEngine stream start.",
    )
    yield QuerySessionEventFlowRequirement(
        requirement_id="query_started_visible",
        phase="query_started",
        surface=QuerySessionEventFlowSurface.QUERY_ENTRY,
        required=not blocked_before_engine,
        after=("query_entry_packet_ready",),
        before=("stream_request_start",),
        allowed_when_blocked=False,
        description="A non-blocked session must emit query_started before the engine stream.",
    )
    yield QuerySessionEventFlowRequirement(
        requirement_id="stream_request_start_visible",
        phase="stream_request_start",
        surface=QuerySessionEventFlowSurface.QUERY_ENGINE,
        required=expected_engine_stream,
        after=("query_started",),
        allowed_when_blocked=False,
        description="QueryEngine can stream only after the query entry packet permits it.",
    )
    yield QuerySessionEventFlowRequirement(
        requirement_id="transcript_mapping_visible",
        phase="transcript_event_mapping",
        surface=QuerySessionEventFlowSurface.TRANSCRIPT,
        required=expected_engine_stream,
        after=("stream_request_start",),
        allowed_when_blocked=False,
        description="Transcript mapping is expected after a real engine stream.",
    )
    yield QuerySessionEventFlowRequirement(
        requirement_id="session_lifecycle_visible",
        phase="session_lifecycle_state",
        surface=QuerySessionEventFlowSurface.LIFECYCLE,
        required=True,
        after=("context_assembled",),
        description="Session lifecycle state must remain auditable for pre-query and post-query paths.",
    )


def validate_required_phases(
    observations: Sequence[QuerySessionPhaseObservation],
    requirements: Sequence[QuerySessionEventFlowRequirement],
) -> Iterable[QuerySessionEventFlowFinding]:
    observed = {observation.canonical_phase for observation in observations}
    for requirement in requirements:
        if requirement.required and requirement.canonical_phase not in observed:
            yield QuerySessionEventFlowFinding(
                code=f"missing_{requirement.canonical_phase}",
                severity=QuerySessionEventFlowSeverity.BLOCKER,
                surface=requirement.surface,
                phase=requirement.phase,
                message=f"Required query session event phase is missing: {requirement.phase}.",
                metadata=requirement.to_dict(),
            )
        elif requirement.canonical_phase in observed:
            yield QuerySessionEventFlowFinding(
                code=f"{requirement.canonical_phase}_observed",
                severity=QuerySessionEventFlowSeverity.PASS,
                surface=requirement.surface,
                phase=requirement.phase,
                message=f"Query session event phase observed: {requirement.phase}.",
            )


def validate_phase_order(
    observations: Sequence[QuerySessionPhaseObservation],
    requirements: Sequence[QuerySessionEventFlowRequirement],
) -> Iterable[QuerySessionEventFlowFinding]:
    first_index = first_phase_indices(observations)
    for requirement in requirements:
        phase_index = first_index.get(requirement.canonical_phase)
        if phase_index is None:
            continue
        for after_phase in requirement.after:
            after_index = first_index.get(canonical_query_session_phase(after_phase))
            if after_index is not None and after_index > phase_index:
                yield QuerySessionEventFlowFinding(
                    code=f"{requirement.canonical_phase}_before_{canonical_query_session_phase(after_phase)}",
                    severity=QuerySessionEventFlowSeverity.BLOCKER,
                    surface=requirement.surface,
                    phase=requirement.phase,
                    message=f"{requirement.phase} appeared before prerequisite {after_phase}.",
                    metadata={"phase_index": phase_index, "after_index": after_index},
                )
        for before_phase in requirement.before:
            before_index = first_index.get(canonical_query_session_phase(before_phase))
            if before_index is not None and before_index < phase_index:
                yield QuerySessionEventFlowFinding(
                    code=f"{requirement.canonical_phase}_after_{canonical_query_session_phase(before_phase)}",
                    severity=QuerySessionEventFlowSeverity.BLOCKER,
                    surface=requirement.surface,
                    phase=requirement.phase,
                    message=f"{requirement.phase} appeared after dependent phase {before_phase}.",
                    metadata={"phase_index": phase_index, "before_index": before_index},
                )


def validate_engine_boundary(
    observations: Sequence[QuerySessionPhaseObservation],
    *,
    expected_engine_stream: bool,
    blocked_before_engine: bool,
) -> Iterable[QuerySessionEventFlowFinding]:
    first_index = first_phase_indices(observations)
    stream_index = first_index.get("stream_request_start")
    query_started_index = first_index.get("query_started")
    if blocked_before_engine and stream_index is not None:
        yield QuerySessionEventFlowFinding(
            code="blocked_session_reached_stream_request_start",
            severity=QuerySessionEventFlowSeverity.BLOCKER,
            surface=QuerySessionEventFlowSurface.QUERY_ENGINE,
            phase="stream_request_start",
            message="A blocked query session emitted stream_request_start.",
            metadata={"stream_index": stream_index},
        )
    if expected_engine_stream and stream_index is None:
        yield QuerySessionEventFlowFinding(
            code="expected_stream_request_start_missing",
            severity=QuerySessionEventFlowSeverity.BLOCKER,
            surface=QuerySessionEventFlowSurface.QUERY_ENGINE,
            phase="stream_request_start",
            message="A non-blocked query session did not reach QueryEngine stream start.",
        )
    if stream_index is not None and query_started_index is None:
        yield QuerySessionEventFlowFinding(
            code="stream_started_without_query_started",
            severity=QuerySessionEventFlowSeverity.BLOCKER,
            surface=QuerySessionEventFlowSurface.QUERY_ENGINE,
            phase="stream_request_start",
            message="QueryEngine stream started without query_started event.",
        )
    if stream_index is not None and query_started_index is not None and stream_index < query_started_index:
        yield QuerySessionEventFlowFinding(
            code="stream_started_before_query_started",
            severity=QuerySessionEventFlowSeverity.BLOCKER,
            surface=QuerySessionEventFlowSurface.QUERY_ENGINE,
            phase="stream_request_start",
            message="QueryEngine stream started before query_started.",
            metadata={"stream_index": stream_index, "query_started_index": query_started_index},
        )
    if blocked_before_engine and any(
        observation.canonical_phase in {"query_cancelled", "query_interrupted", "query_stale_context_blocked"}
        for observation in observations
    ):
        yield QuerySessionEventFlowFinding(
            code="blocked_control_phase_observed",
            severity=QuerySessionEventFlowSeverity.PASS,
            surface=QuerySessionEventFlowSurface.CONTROL,
            message="Blocked query session emitted a control-state phase before engine entry.",
        )


def validate_session_identity(
    observations: Sequence[QuerySessionPhaseObservation],
    *,
    session_id: str,
    worker_request_id: str,
) -> Iterable[QuerySessionEventFlowFinding]:
    mismatches = []
    for observation in observations:
        if observation.canonical_phase == "session_replay_plan":
            continue
        if observation.session_id and session_id and observation.session_id != session_id:
            mismatches.append(observation.to_dict())
        if observation.worker_request_id and worker_request_id and observation.worker_request_id != worker_request_id:
            mismatches.append(observation.to_dict())
    if mismatches:
        yield QuerySessionEventFlowFinding(
            code="query_event_flow_identity_mismatch",
            severity=QuerySessionEventFlowSeverity.BLOCKER,
            surface=QuerySessionEventFlowSurface.LIFECYCLE,
            message="Observed query session events do not share the expected session/request identity.",
            metadata={"mismatches": mismatches[:8], "session_id": session_id, "worker_request_id": worker_request_id},
        )
    else:
        yield QuerySessionEventFlowFinding(
            code="query_event_flow_identity_consistent",
            severity=QuerySessionEventFlowSeverity.PASS,
            surface=QuerySessionEventFlowSurface.LIFECYCLE,
            message="Observed query session events share the expected session/request identity when present.",
        )


def first_phase_indices(observations: Sequence[QuerySessionPhaseObservation]) -> dict[str, int]:
    indices: dict[str, int] = {}
    for observation in observations:
        indices.setdefault(observation.canonical_phase, observation.sequence)
    return indices


def canonical_query_session_phase(phase: str) -> str:
    normalized = str(phase or "").strip().lower().replace("-", "_")
    aliases = {
        "query_session_seed_created": "session_created",
        "session_seed_created": "session_created",
        "query_input_processed": "input_processed",
        "input_accepted": "input_processed",
        "context_snapshot_ready": "context_assembled",
        "context_snapshot_attached": "context_assembled",
        "query_session_seed_attached": "session_attached",
        "query_entry_ready": "query_entry_packet_ready",
        "query_entry_packet": "query_entry_packet_ready",
        "stream_start": "stream_request_start",
        "stream_request_started": "stream_request_start",
    }
    return aliases.get(normalized, normalized)


def event_flow_status_from_findings(
    findings: Sequence[QuerySessionEventFlowFinding],
) -> QuerySessionEventFlowStatus:
    if any(finding.blocking for finding in findings):
        return QuerySessionEventFlowStatus.BLOCKED
    if any(finding.severity == QuerySessionEventFlowSeverity.WARNING for finding in findings):
        return QuerySessionEventFlowStatus.DEGRADED
    return QuerySessionEventFlowStatus.READY


def query_event_flow_metadata(report: QuerySessionEventFlowReport | None) -> dict[str, str]:
    if report is None:
        return {
            "query_event_flow_ok": "false",
            "query_event_flow_status": "missing",
            "query_event_flow_blockers": "1",
        }
    return report.metadata_values()


def render_query_event_flow_markdown(report: QuerySessionEventFlowReport) -> str:
    lines = [
        "## Query Session Event Flow",
        "",
        f"- report_id: `{report.report_id}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- session_id: `{report.session_id}`",
        f"- worker_request_id: `{report.worker_request_id}`",
        f"- expected_engine_stream: `{str(report.expected_engine_stream).lower()}`",
        f"- blocked_before_engine: `{str(report.blocked_before_engine).lower()}`",
        f"- observation_count: `{len(report.observations)}`",
        f"- blocker_count: `{report.blocker_count}`",
        "",
        "### Phase Order",
        "",
    ]
    lines.extend(f"- `{observation.sequence}` `{observation.canonical_phase}`" for observation in report.observations)
    lines.extend(["", "### Findings", ""])
    lines.extend(
        f"- `{finding.severity}` `{finding.surface}` `{finding.code}`: {finding.message}"
        for finding in report.findings
    )
    return "\n".join(lines)
