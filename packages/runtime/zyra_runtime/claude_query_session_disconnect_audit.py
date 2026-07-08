from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


class QuerySessionDisconnectStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class QuerySessionDisconnectSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class QuerySessionDisconnectSurface(StrEnum):
    QUERY_ENTRY_PACKET = "query_entry_packet"
    SESSION_STORE = "session_store"
    CONTROL = "control"
    CONTEXT = "context"
    EVENT_FLOW = "event_flow"
    QUERY_ENGINE = "query_engine"
    HANDOFF = "handoff"
    CUSTODY = "custody"


class QuerySessionDisconnectEffect(StrEnum):
    NO_STREAM = "no_stream"
    NO_QUERY_STARTED = "no_query_started"
    BLOCK_REASON = "block_reason"
    STORE_BLOCKED = "store_blocked"
    CONTROL_TERMINAL = "control_terminal"
    NORMAL_STREAM = "normal_stream"


@dataclass(frozen=True, slots=True)
class QuerySessionDisconnectScenario:
    scenario_id: str
    surface: QuerySessionDisconnectSurface
    trigger_key: str
    expected_effect: QuerySessionDisconnectEffect
    active: bool
    expected_value: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "surface": str(self.surface),
            "trigger_key": self.trigger_key,
            "expected_effect": str(self.expected_effect),
            "active": self.active,
            "expected_value": self.expected_value,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class QuerySessionDisconnectObservation:
    scenario_id: str
    observed: bool
    ok: bool
    effect: QuerySessionDisconnectEffect
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "observed": self.observed,
            "ok": self.ok,
            "effect": str(self.effect),
            "details": to_jsonable(self.details),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionDisconnectFinding:
    code: str
    severity: QuerySessionDisconnectSeverity
    surface: QuerySessionDisconnectSurface
    message: str
    scenario_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == QuerySessionDisconnectSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "scenario_id": self.scenario_id,
            "blocking": self.blocking,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionDisconnectAuditReport:
    report_id: str
    status: QuerySessionDisconnectStatus
    session_id: str
    worker_request_id: str
    scenarios: tuple[QuerySessionDisconnectScenario, ...]
    observations: tuple[QuerySessionDisconnectObservation, ...]
    findings: tuple[QuerySessionDisconnectFinding, ...]
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {QuerySessionDisconnectStatus.READY, QuerySessionDisconnectStatus.DEGRADED}

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == QuerySessionDisconnectSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        for finding in self.findings:
            if finding.blocking:
                return finding.code
        return ""

    @property
    def active_scenario_count(self) -> int:
        return sum(1 for scenario in self.scenarios if scenario.active)

    @property
    def active_scenario_ids(self) -> tuple[str, ...]:
        return tuple(scenario.scenario_id for scenario in self.scenarios if scenario.active)

    @property
    def failed_observation_count(self) -> int:
        return sum(1 for observation in self.observations if observation.observed and not observation.ok)

    @property
    def effect_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for scenario in self.scenarios:
            if scenario.active:
                key = str(scenario.expected_effect)
                counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def normal_stream_observed(self) -> bool:
        for observation in self.observations:
            if observation.scenario_id == "normal_query_entry_streams" and observation.observed:
                return observation.ok
        return False

    @property
    def terminal_control_observed(self) -> bool:
        terminal_effect = str(QuerySessionDisconnectEffect.CONTROL_TERMINAL)
        return any(
            observation.observed and observation.ok and str(observation.effect) == terminal_effect
            for observation in self.observations
        )

    def metadata_values(self) -> dict[str, str]:
        return {
            "query_disconnect_report_id": self.report_id,
            "query_disconnect_ok": str(self.ok).lower(),
            "query_disconnect_status": str(self.status),
            "query_disconnect_blockers": str(self.blocker_count),
            "query_disconnect_warnings": str(self.warning_count),
            "query_disconnect_first_blocker": self.first_blocker_code,
            "query_disconnect_scenario_count": str(len(self.scenarios)),
            "query_disconnect_active_scenario_count": str(self.active_scenario_count),
            "query_disconnect_active_scenario_ids": ",".join(self.active_scenario_ids),
            "query_disconnect_observation_count": str(len(self.observations)),
            "query_disconnect_failed_observation_count": str(self.failed_observation_count),
            "query_disconnect_normal_stream_observed": str(self.normal_stream_observed).lower(),
            "query_disconnect_terminal_control_observed": str(self.terminal_control_observed).lower(),
            "query_disconnect_effect_counts": ",".join(
                f"{effect}:{count}" for effect, count in sorted(self.effect_counts.items())
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "observations": [observation.to_dict() for observation in self.observations],
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "first_blocker_code": self.first_blocker_code,
            "active_scenario_count": self.active_scenario_count,
            "active_scenario_ids": list(self.active_scenario_ids),
            "failed_observation_count": self.failed_observation_count,
            "effect_counts": self.effect_counts,
            "normal_stream_observed": self.normal_stream_observed,
            "terminal_control_observed": self.terminal_control_observed,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class QuerySessionDisconnectAuditRuntime:
    """Audits disconnect and mutation effects for the query session gate."""

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        constraints: Mapping[str, Any],
        packet: Mapping[str, Any],
        integration_report: Mapping[str, Any],
        handoff_report: Mapping[str, Any],
        custody_report: Mapping[str, Any],
        event_flow_report: Mapping[str, Any],
        state_graph_report: Mapping[str, Any],
        events: Sequence[EventRecord],
    ) -> QuerySessionDisconnectAuditReport:
        phases = query_disconnect_phases(events)
        scenarios = tuple(default_query_disconnect_scenarios(constraints, packet=packet))
        observations = tuple(
            observe_disconnect_scenario(
                scenario,
                packet=packet,
                integration_report=integration_report,
                handoff_report=handoff_report,
                custody_report=custody_report,
                event_flow_report=event_flow_report,
                state_graph_report=state_graph_report,
                phases=phases,
            )
            for scenario in scenarios
        )
        findings = [
            *validate_disconnect_observations(scenarios, observations),
            *validate_normal_path_observation(scenarios, phases, integration_report, handoff_report),
        ]
        status = disconnect_status_from_findings(findings)
        return QuerySessionDisconnectAuditReport(
            report_id=new_id("qdisc"),
            status=status,
            session_id=session_id,
            worker_request_id=worker_request_id,
            scenarios=scenarios,
            observations=observations,
            findings=tuple(findings),
            metadata={
                "source_path": "src/QueryEngine.ts",
                "target_path": "packages/runtime/zyra_runtime/claude_query_session_disconnect_audit.py",
                "owner_unit": "M1-02B",
            },
        )

    def event_for_report(
        self,
        report: QuerySessionDisconnectAuditReport,
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
                    "phase": "query_disconnect_audit",
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "ok": report.ok,
                    "status": str(report.status),
                    "scenario_count": len(report.scenarios),
                    "active_scenario_count": report.active_scenario_count,
                    "observation_count": len(report.observations),
                    "blocker_count": report.blocker_count,
                    "first_blocker_code": report.first_blocker_code,
                }
            },
        )


def default_query_disconnect_scenarios(
    constraints: Mapping[str, Any],
    *,
    packet: Mapping[str, Any],
) -> Iterable[QuerySessionDisconnectScenario]:
    yield QuerySessionDisconnectScenario(
        scenario_id="disable_query_entry_packet_blocks_query",
        surface=QuerySessionDisconnectSurface.QUERY_ENTRY_PACKET,
        trigger_key="disable_query_entry_packet",
        expected_effect=QuerySessionDisconnectEffect.BLOCK_REASON,
        active=_truthy(constraints.get("disable_query_entry_packet")),
        expected_value="disabled",
        description="Disabling QueryEntryPacketBuilder must prevent QueryEngine stream.",
    )
    yield QuerySessionDisconnectScenario(
        scenario_id="disable_query_entry_store_blocks_query",
        surface=QuerySessionDisconnectSurface.SESSION_STORE,
        trigger_key="disable_query_entry_store",
        expected_effect=QuerySessionDisconnectEffect.STORE_BLOCKED,
        active=_truthy(constraints.get("disable_query_entry_store")),
        expected_value="query_entry_store_append_ready",
        description="Disabling query entry store persistence must block the main path.",
    )
    yield QuerySessionDisconnectScenario(
        scenario_id="cancel_session_blocks_stream",
        surface=QuerySessionDisconnectSurface.CONTROL,
        trigger_key="cancel_session",
        expected_effect=QuerySessionDisconnectEffect.CONTROL_TERMINAL,
        active=_truthy(constraints.get("cancel_session")) or _truthy(constraints.get("cancel")),
        expected_value="query_cancelled",
        description="Cancel state must terminate before QueryEngine stream.",
    )
    yield QuerySessionDisconnectScenario(
        scenario_id="interrupt_session_blocks_stream",
        surface=QuerySessionDisconnectSurface.CONTROL,
        trigger_key="interrupt_session",
        expected_effect=QuerySessionDisconnectEffect.CONTROL_TERMINAL,
        active=_truthy(constraints.get("interrupt_session")) or _truthy(constraints.get("interrupt")),
        expected_value="query_interrupted",
        description="Interrupt state must terminate before QueryEngine stream.",
    )
    yield QuerySessionDisconnectScenario(
        scenario_id="stale_context_blocks_stream",
        surface=QuerySessionDisconnectSurface.CONTEXT,
        trigger_key="expected_context_fingerprint",
        expected_effect=QuerySessionDisconnectEffect.CONTROL_TERMINAL,
        active=bool(constraints.get("expected_context_fingerprint"))
        and str(constraints.get("expected_context_fingerprint")) != str(packet.get("context_fingerprint") or ""),
        expected_value="query_stale_context_blocked",
        description="Stale expected context fingerprint must block QueryEngine stream.",
    )
    yield QuerySessionDisconnectScenario(
        scenario_id="normal_query_entry_streams",
        surface=QuerySessionDisconnectSurface.QUERY_ENGINE,
        trigger_key="normal_path",
        expected_effect=QuerySessionDisconnectEffect.NORMAL_STREAM,
        active=not any(
            _truthy(constraints.get(key))
            for key in (
                "disable_query_entry_packet",
                "disable_query_entry_store",
                "cancel_session",
                "cancel",
                "interrupt_session",
                "interrupt",
            )
        )
        and not (
            bool(constraints.get("expected_context_fingerprint"))
            and str(constraints.get("expected_context_fingerprint")) != str(packet.get("context_fingerprint") or "")
        ),
        description="Default query entry path must reach query_started and stream_request_start.",
    )


def observe_disconnect_scenario(
    scenario: QuerySessionDisconnectScenario,
    *,
    packet: Mapping[str, Any],
    integration_report: Mapping[str, Any],
    handoff_report: Mapping[str, Any],
    custody_report: Mapping[str, Any],
    event_flow_report: Mapping[str, Any],
    state_graph_report: Mapping[str, Any],
    phases: Sequence[str],
) -> QuerySessionDisconnectObservation:
    if not scenario.active:
        return QuerySessionDisconnectObservation(
            scenario_id=scenario.scenario_id,
            observed=False,
            ok=True,
            effect=scenario.expected_effect,
            details={"inactive": True},
        )
    stream_present = "stream_request_start" in phases
    query_started_present = "query_started" in phases
    block_reason = str(packet.get("block_reason") or "")
    if scenario.expected_effect == QuerySessionDisconnectEffect.BLOCK_REASON:
        ok = block_reason == scenario.expected_value and not stream_present
        return QuerySessionDisconnectObservation(
            scenario_id=scenario.scenario_id,
            observed=True,
            ok=ok,
            effect=scenario.expected_effect,
            details={"block_reason": block_reason, "stream_present": stream_present},
        )
    if scenario.expected_effect == QuerySessionDisconnectEffect.STORE_BLOCKED:
        first_blocker = str(integration_report.get("first_blocker_code") or "")
        ok = first_blocker == scenario.expected_value and not stream_present
        return QuerySessionDisconnectObservation(
            scenario_id=scenario.scenario_id,
            observed=True,
            ok=ok,
            effect=scenario.expected_effect,
            details={"first_blocker": first_blocker, "stream_present": stream_present},
        )
    if scenario.expected_effect == QuerySessionDisconnectEffect.CONTROL_TERMINAL:
        ok = scenario.expected_value in phases and not stream_present
        return QuerySessionDisconnectObservation(
            scenario_id=scenario.scenario_id,
            observed=True,
            ok=ok,
            effect=scenario.expected_effect,
            details={"expected_phase": scenario.expected_value, "phases": list(phases), "stream_present": stream_present},
        )
    if scenario.expected_effect == QuerySessionDisconnectEffect.NORMAL_STREAM:
        ok = (
            query_started_present
            and stream_present
            and integration_report.get("ok") is True
            and handoff_report.get("ok") is True
            and custody_report.get("ok") is True
            and event_flow_report.get("ok") is True
            and state_graph_report.get("ok") is True
        )
        return QuerySessionDisconnectObservation(
            scenario_id=scenario.scenario_id,
            observed=True,
            ok=ok,
            effect=scenario.expected_effect,
            details={
                "query_started_present": query_started_present,
                "stream_present": stream_present,
                "integration_ok": integration_report.get("ok"),
                "handoff_ok": handoff_report.get("ok"),
                "custody_ok": custody_report.get("ok"),
                "event_flow_ok": event_flow_report.get("ok"),
                "state_graph_ok": state_graph_report.get("ok"),
            },
        )
    return QuerySessionDisconnectObservation(
        scenario_id=scenario.scenario_id,
        observed=True,
        ok=False,
        effect=scenario.expected_effect,
        details={"unsupported_effect": str(scenario.expected_effect)},
    )


def validate_disconnect_observations(
    scenarios: Sequence[QuerySessionDisconnectScenario],
    observations: Sequence[QuerySessionDisconnectObservation],
) -> Iterable[QuerySessionDisconnectFinding]:
    scenario_by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    for observation in observations:
        scenario = scenario_by_id.get(observation.scenario_id)
        if scenario is None:
            continue
        if not scenario.active:
            continue
        if observation.ok:
            yield QuerySessionDisconnectFinding(
                code=f"{scenario.scenario_id}_ready",
                severity=QuerySessionDisconnectSeverity.PASS,
                surface=scenario.surface,
                scenario_id=scenario.scenario_id,
                message=f"Disconnect scenario produced expected effect: {scenario.scenario_id}.",
                metadata=observation.to_dict(),
            )
        else:
            yield QuerySessionDisconnectFinding(
                code=f"{scenario.scenario_id}_failed",
                severity=QuerySessionDisconnectSeverity.BLOCKER,
                surface=scenario.surface,
                scenario_id=scenario.scenario_id,
                message=f"Disconnect scenario did not produce expected effect: {scenario.scenario_id}.",
                metadata={"scenario": scenario.to_dict(), "observation": observation.to_dict()},
            )


def validate_normal_path_observation(
    scenarios: Sequence[QuerySessionDisconnectScenario],
    phases: Sequence[str],
    integration_report: Mapping[str, Any],
    handoff_report: Mapping[str, Any],
) -> Iterable[QuerySessionDisconnectFinding]:
    normal = next((scenario for scenario in scenarios if scenario.scenario_id == "normal_query_entry_streams"), None)
    if normal is None or not normal.active:
        return
    missing = [phase for phase in ("query_entry_packet_ready", "query_started", "stream_request_start") if phase not in phases]
    if missing:
        yield QuerySessionDisconnectFinding(
            code="normal_path_missing_query_phases",
            severity=QuerySessionDisconnectSeverity.BLOCKER,
            surface=QuerySessionDisconnectSurface.EVENT_FLOW,
            scenario_id=normal.scenario_id,
            message="Normal query path is missing required phases.",
            metadata={"missing": missing, "phases": list(phases)},
        )
    if integration_report.get("ok") is not True or handoff_report.get("ok") is not True:
        yield QuerySessionDisconnectFinding(
            code="normal_path_integration_or_handoff_not_ok",
            severity=QuerySessionDisconnectSeverity.BLOCKER,
            surface=QuerySessionDisconnectSurface.HANDOFF,
            scenario_id=normal.scenario_id,
            message="Normal query path streamed without integration/handoff readiness.",
            metadata={"integration_ok": integration_report.get("ok"), "handoff_ok": handoff_report.get("ok")},
        )


def disconnect_effect_matrix(report: QuerySessionDisconnectAuditReport) -> list[dict[str, Any]]:
    rows = []
    observation_by_id = {observation.scenario_id: observation for observation in report.observations}
    for scenario in report.scenarios:
        observation = observation_by_id.get(scenario.scenario_id)
        rows.append(
            {
                "scenario_id": scenario.scenario_id,
                "surface": str(scenario.surface),
                "trigger_key": scenario.trigger_key,
                "active": scenario.active,
                "expected_effect": str(scenario.expected_effect),
                "expected_value": scenario.expected_value,
                "observed": observation.observed if observation else False,
                "ok": observation.ok if observation else False,
                "details": observation.details if observation else {},
            }
        )
    return rows


def disconnect_failure_summary(report: QuerySessionDisconnectAuditReport) -> dict[str, Any]:
    failed = [
        row
        for row in disconnect_effect_matrix(report)
        if row["active"] is True and row["observed"] is True and row["ok"] is not True
    ]
    return {
        "failed_count": len(failed),
        "failed_scenario_ids": [str(row["scenario_id"]) for row in failed],
        "first_blocker_code": report.first_blocker_code,
        "normal_stream_observed": report.normal_stream_observed,
        "terminal_control_observed": report.terminal_control_observed,
    }


def query_disconnect_phases(events: Sequence[EventRecord]) -> tuple[str, ...]:
    phases: list[str] = []
    for event in events:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        query_payload = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
        phase = str(query_payload.get("phase") or "")
        if phase:
            phases.append(phase)
    return tuple(phases)


def disconnect_status_from_findings(
    findings: Sequence[QuerySessionDisconnectFinding],
) -> QuerySessionDisconnectStatus:
    if any(finding.blocking for finding in findings):
        return QuerySessionDisconnectStatus.BLOCKED
    if any(finding.severity == QuerySessionDisconnectSeverity.WARNING for finding in findings):
        return QuerySessionDisconnectStatus.DEGRADED
    return QuerySessionDisconnectStatus.READY


def query_disconnect_metadata(report: QuerySessionDisconnectAuditReport | None) -> dict[str, str]:
    if report is None:
        return {
            "query_disconnect_ok": "false",
            "query_disconnect_status": "missing",
            "query_disconnect_blockers": "1",
        }
    return report.metadata_values()


def render_query_disconnect_markdown(report: QuerySessionDisconnectAuditReport) -> str:
    lines = [
        "## Query Session Disconnect Audit",
        "",
        f"- report_id: `{report.report_id}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- active_scenarios: `{report.active_scenario_count}`",
        f"- failed_observations: `{report.failed_observation_count}`",
        f"- normal_stream_observed: `{str(report.normal_stream_observed).lower()}`",
        f"- terminal_control_observed: `{str(report.terminal_control_observed).lower()}`",
        f"- blocker_count: `{report.blocker_count}`",
        "",
        "### Scenarios",
        "",
    ]
    lines.extend(
        f"- `{scenario.scenario_id}` active=`{str(scenario.active).lower()}` effect=`{scenario.expected_effect}`"
        for scenario in report.scenarios
    )
    lines.extend(["", "### Observations", ""])
    lines.extend(
        f"- `{observation.scenario_id}` ok=`{str(observation.ok).lower()}` observed=`{str(observation.observed).lower()}`"
        for observation in report.observations
    )
    lines.extend(["", "### Effect Matrix", ""])
    lines.extend(
        f"- `{row['scenario_id']}` active=`{str(row['active']).lower()}` effect=`{row['expected_effect']}` ok=`{str(row['ok']).lower()}`"
        for row in disconnect_effect_matrix(report)
    )
    lines.extend(["", "### Findings", ""])
    lines.extend(
        f"- `{finding.severity}` `{finding.surface}` `{finding.code}`: {finding.message}"
        for finding in report.findings
    )
    return "\n".join(lines)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
