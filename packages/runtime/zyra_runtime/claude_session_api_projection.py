from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import now_iso, to_jsonable


class SessionApiProjectionStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class SessionApiSectionKind(StrEnum):
    CONTRACT = "contract"
    INPUT = "input"
    CONTEXT = "context"
    REPLAY = "replay"
    TURN_LIFECYCLE = "turn_lifecycle"
    ACCEPTANCE = "acceptance"
    LIFECYCLE = "lifecycle"
    LINEAGE = "lineage"
    HEALTH = "health"


class SessionApiProjectionSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class SessionApiMetric:
    key: str
    value: Any
    label: str = ""
    unit: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": to_jsonable(self.value),
            "label": self.label or self.key,
            "unit": self.unit,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionApiLink:
    rel: str
    target: str
    label: str = ""
    surface: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel": self.rel,
            "target": self.target,
            "label": self.label or self.target,
            "surface": self.surface,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionApiHealthCheck:
    check_id: str
    severity: SessionApiProjectionSeverity
    section: SessionApiSectionKind
    ok: bool
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == SessionApiProjectionSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "severity": str(self.severity),
            "section": str(self.section),
            "ok": self.ok,
            "blocking": self.blocking,
            "message": self.message,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionApiSection:
    section_id: str
    kind: SessionApiSectionKind
    title: str
    ok: bool
    status: str
    summary: str
    metrics: tuple[SessionApiMetric, ...] = ()
    links: tuple[SessionApiLink, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "kind": str(self.kind),
            "title": self.title,
            "ok": self.ok,
            "status": self.status,
            "summary": self.summary,
            "metrics": [metric.to_dict() for metric in self.metrics],
            "links": [link.to_dict() for link in self.links],
            "payload": to_jsonable(self.payload),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionApiProjection:
    owner_unit: str
    status: SessionApiProjectionStatus
    generated_at: str
    session_id: str
    worker_request_id: str
    session_foundation: dict[str, Any]
    input_report: dict[str, Any]
    context_snapshot: dict[str, Any]
    runtime_contract: dict[str, Any]
    session_replay: dict[str, Any] | None
    turn_lifecycle: dict[str, Any] | None
    session_acceptance: dict[str, Any] | None
    session_lifecycle: dict[str, Any] | None
    session_lineage: dict[str, Any] | None
    sections: tuple[SessionApiSection, ...]
    health_checks: tuple[SessionApiHealthCheck, ...]
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {SessionApiProjectionStatus.READY, SessionApiProjectionStatus.DEGRADED}

    @property
    def blocker_count(self) -> int:
        return sum(1 for check in self.health_checks if check.blocking and not check.ok)

    @property
    def warning_count(self) -> int:
        return sum(1 for check in self.health_checks if check.severity == SessionApiProjectionSeverity.WARNING)

    @property
    def section_statuses(self) -> dict[str, str]:
        return {str(section.kind): section.status for section in self.sections}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ownerUnit": self.owner_unit,
            "ok": self.ok,
            "status": str(self.status),
            "generatedAt": self.generated_at,
            "sessionId": self.session_id,
            "workerRequestId": self.worker_request_id,
            "sessionFoundation": to_jsonable(self.session_foundation),
            "inputReport": to_jsonable(self.input_report),
            "contextSnapshot": to_jsonable(self.context_snapshot),
            "runtimeContract": to_jsonable(self.runtime_contract),
            "sessionReplay": to_jsonable(self.session_replay),
            "turnLifecycle": to_jsonable(self.turn_lifecycle),
            "sessionAcceptance": to_jsonable(self.session_acceptance),
            "sessionLifecycle": to_jsonable(self.session_lifecycle),
            "sessionLineage": to_jsonable(self.session_lineage),
            "sections": [section.to_dict() for section in self.sections],
            "healthChecks": [check.to_dict() for check in self.health_checks],
            "sectionStatuses": self.section_statuses,
            "blockerCount": self.blocker_count,
            "warningCount": self.warning_count,
            "metadata": dict(self.metadata),
        }


class SessionApiProjectionBuilder:
    """Builds the /workers/code/session-foundation response from live runtime reports."""

    def build(
        self,
        *,
        contracts: Any,
        request: Any,
        input_report: Any,
        context_snapshot: Any,
        replay_plan: Any | None = None,
        turn_lifecycle: Any | None = None,
        acceptance_report: Any | None = None,
        lifecycle_report: Any | None = None,
        lineage_report: Any | None = None,
    ) -> SessionApiProjection:
        session_foundation = dict(getattr(contracts, "session_contract", {}) or {}).get("preQueryFoundation", {})
        runtime_contract = contract_projection(contracts)
        input_payload = object_to_payload(input_report)
        context_payload = object_to_payload(context_snapshot, include_text=False)
        replay_payload = optional_payload(replay_plan)
        turn_payload = optional_payload(turn_lifecycle)
        acceptance_payload = optional_payload(acceptance_report)
        lifecycle_payload = optional_payload(lifecycle_report)
        lineage_payload = optional_payload(lineage_report)
        sections = tuple(
            section
            for section in (
                self._contract_section(contracts, runtime_contract, session_foundation),
                self._input_section(input_report, input_payload),
                self._context_section(context_snapshot, context_payload),
                self._replay_section(replay_plan, replay_payload),
                self._turn_section(turn_lifecycle, turn_payload),
                self._acceptance_section(acceptance_report, acceptance_payload),
                self._lifecycle_section(lifecycle_report, lifecycle_payload),
                self._lineage_section(lineage_report, lineage_payload),
            )
            if section is not None
        )
        health_checks = tuple(build_projection_health_checks(
            contracts=contracts,
            input_report=input_report,
            context_snapshot=context_snapshot,
            replay_plan=replay_plan,
            turn_lifecycle=turn_lifecycle,
            acceptance_report=acceptance_report,
            lifecycle_report=lifecycle_report,
            lineage_report=lineage_report,
        ))
        status = projection_status(health_checks)
        session_id = first_non_empty(
            [
                str(getattr(context_snapshot, "session_id", "") or ""),
                str(getattr(turn_lifecycle, "session_id", "") or ""),
                str(getattr(acceptance_report, "session_id", "") or ""),
                str(getattr(lifecycle_report, "session_id", "") or ""),
                str(getattr(request, "constraints", {}).get("session_id") if isinstance(getattr(request, "constraints", {}), Mapping) else ""),
            ]
        )
        worker_request_id = first_non_empty(
            [
                str(getattr(request, "request_id", "") or ""),
                str(getattr(context_snapshot, "worker_request_id", "") or ""),
                str(getattr(turn_lifecycle, "worker_request_id", "") or ""),
                str(getattr(acceptance_report, "worker_request_id", "") or ""),
                str(getattr(lifecycle_report, "worker_request_id", "") or ""),
            ]
        )
        metadata = projection_metadata(
            contracts=contracts,
            input_report=input_report,
            context_snapshot=context_snapshot,
            replay_plan=replay_plan,
            turn_lifecycle=turn_lifecycle,
            acceptance_report=acceptance_report,
            lifecycle_report=lifecycle_report,
            lineage_report=lineage_report,
            status=status,
            health_checks=health_checks,
            sections=sections,
        )
        return SessionApiProjection(
            owner_unit="M1-02B",
            status=status,
            generated_at=now_iso(),
            session_id=session_id,
            worker_request_id=worker_request_id,
            session_foundation=session_foundation,
            input_report=input_payload,
            context_snapshot=context_payload,
            runtime_contract=runtime_contract,
            session_replay=replay_payload,
            turn_lifecycle=turn_payload,
            session_acceptance=acceptance_payload,
            session_lifecycle=lifecycle_payload,
            session_lineage=lineage_payload,
            sections=sections,
            health_checks=health_checks,
            metadata=metadata,
        )

    def _contract_section(
        self,
        contracts: Any,
        runtime_contract: Mapping[str, Any],
        session_foundation: Mapping[str, Any],
    ) -> SessionApiSection:
        clean_runtime_safe = getattr(contracts, "clean_runtime_safe", False) is True
        target_paths = list(getattr(contracts, "primary_runtime_paths", ()) or ())
        metrics = (
            SessionApiMetric("source_to_target_count", len(getattr(contracts, "source_to_target", ()) or ())),
            SessionApiMetric("primary_runtime_path_count", len(target_paths)),
            SessionApiMetric("clean_runtime_safe", clean_runtime_safe),
        )
        links = tuple(SessionApiLink("runtime_target", path, surface="contract") for path in target_paths)
        return SessionApiSection(
            section_id="session_contract",
            kind=SessionApiSectionKind.CONTRACT,
            title="Session Contract",
            ok=clean_runtime_safe and bool(session_foundation),
            status="ready" if clean_runtime_safe and session_foundation else "blocked",
            summary="Productized Claude session contract and clean runtime boundary.",
            metrics=metrics,
            links=links,
            payload={
                "runtime": runtime_contract,
                "sessionFoundation": to_jsonable(session_foundation),
            },
        )

    def _input_section(self, input_report: Any, payload: Mapping[str, Any]) -> SessionApiSection:
        ok = object_ok(input_report)
        accepted = len(getattr(input_report, "accepted_records", ()) or ())
        records = len(getattr(input_report, "records", ()) or ())
        return SessionApiSection(
            section_id="query_input",
            kind=SessionApiSectionKind.INPUT,
            title="Query Input",
            ok=ok,
            status="ready" if ok else "blocked",
            summary="Pre-query user, slash, bash and structured-turn input classification.",
            metrics=(
                SessionApiMetric("record_count", records),
                SessionApiMetric("accepted_count", accepted),
                SessionApiMetric("rejected_count", max(0, records - accepted)),
            ),
            links=(SessionApiLink("runtime_target", "packages/runtime/zyra_runtime/claude_input_processor.py"),),
            payload=dict(payload),
        )

    def _context_section(self, context_snapshot: Any, payload: Mapping[str, Any]) -> SessionApiSection:
        ok = object_ok(context_snapshot)
        return SessionApiSection(
            section_id="context_snapshot",
            kind=SessionApiSectionKind.CONTEXT,
            title="Context Snapshot",
            ok=ok,
            status=str(getattr(context_snapshot, "status", "ready" if ok else "blocked")),
            summary="Context assembly result bound before QueryEngine dispatch.",
            metrics=(
                SessionApiMetric("selected_block_count", len(getattr(context_snapshot, "selected_blocks", ()) or ())),
                SessionApiMetric("dropped_block_count", len(getattr(context_snapshot, "dropped_blocks", ()) or ())),
                SessionApiMetric("active_chars", getattr(context_snapshot, "active_chars", 0)),
            ),
            links=(SessionApiLink("runtime_target", "packages/runtime/zyra_runtime/claude_context_assembly_foundation.py"),),
            payload=dict(payload),
        )

    def _replay_section(self, replay_plan: Any | None, payload: Mapping[str, Any] | None) -> SessionApiSection | None:
        if replay_plan is None and payload is None:
            return SessionApiSection(
                section_id="session_replay",
                kind=SessionApiSectionKind.REPLAY,
                title="Session Replay",
                ok=True,
                status="not_requested",
                summary="No resume selector was supplied for this projection.",
                metrics=(SessionApiMetric("requested", False),),
                links=(SessionApiLink("runtime_target", "packages/runtime/zyra_runtime/claude_session_replay_runtime.py"),),
                payload={},
            )
        ok = object_ok(replay_plan)
        return SessionApiSection(
            section_id="session_replay",
            kind=SessionApiSectionKind.REPLAY,
            title="Session Replay",
            ok=ok,
            status=str(getattr(replay_plan, "status", "ready" if ok else "blocked")),
            summary="Session store replay and QueryEngine reattach plan.",
            metrics=(
                SessionApiMetric("requested", True),
                SessionApiMetric("action_count", len(getattr(replay_plan, "actions", ()) or ())),
                SessionApiMetric("message_count", len(getattr(replay_plan, "replay_messages", ()) or ())),
                SessionApiMetric("blocker_count", getattr(replay_plan, "blocker_count", 0)),
            ),
            links=(SessionApiLink("runtime_target", "packages/runtime/zyra_runtime/claude_session_replay_runtime.py"),),
            payload=dict(payload or {}),
        )

    def _turn_section(self, turn_lifecycle: Any | None, payload: Mapping[str, Any] | None) -> SessionApiSection | None:
        if turn_lifecycle is None:
            return None
        ok = object_ok(turn_lifecycle)
        return SessionApiSection(
            section_id="turn_lifecycle",
            kind=SessionApiSectionKind.TURN_LIFECYCLE,
            title="Turn Lifecycle",
            ok=ok,
            status=str(getattr(turn_lifecycle, "status", "ready" if ok else "blocked")),
            summary="Input, context, replay and structured tool turns bound to QueryEngine handoff.",
            metrics=(
                SessionApiMetric("seed_count", len(getattr(turn_lifecycle, "seeds", ()) or ())),
                SessionApiMetric("blocker_count", getattr(turn_lifecycle, "blocker_count", 0)),
                SessionApiMetric("warning_count", getattr(turn_lifecycle, "warning_count", 0)),
            ),
            links=(SessionApiLink("runtime_target", "packages/runtime/zyra_runtime/claude_turn_lifecycle_runtime.py"),),
            payload=dict(payload or {}),
        )

    def _acceptance_section(self, acceptance_report: Any | None, payload: Mapping[str, Any] | None) -> SessionApiSection | None:
        if acceptance_report is None:
            return None
        ok = object_ok(acceptance_report)
        return SessionApiSection(
            section_id="session_acceptance",
            kind=SessionApiSectionKind.ACCEPTANCE,
            title="Session Acceptance",
            ok=ok,
            status=str(getattr(acceptance_report, "status", "ready" if ok else "blocked")),
            summary="Aggregated behavioral gate for the session foundation.",
            metrics=(
                SessionApiMetric("component_count", len(getattr(acceptance_report, "components", ()) or ())),
                SessionApiMetric("blocker_count", getattr(acceptance_report, "blocker_count", 0)),
                SessionApiMetric("warning_count", getattr(acceptance_report, "warning_count", 0)),
            ),
            links=(SessionApiLink("runtime_target", "packages/runtime/zyra_runtime/claude_session_acceptance_runtime.py"),),
            payload=dict(payload or {}),
        )

    def _lifecycle_section(self, lifecycle_report: Any | None, payload: Mapping[str, Any] | None) -> SessionApiSection | None:
        if lifecycle_report is None:
            return None
        ok = object_ok(lifecycle_report)
        return SessionApiSection(
            section_id="session_lifecycle",
            kind=SessionApiSectionKind.LIFECYCLE,
            title="Session Lifecycle",
            ok=ok,
            status=str(getattr(lifecycle_report, "status", "ready" if ok else "blocked")),
            summary="EventRecord lifecycle state machine for session foundation reachability.",
            metrics=(
                SessionApiMetric("event_count", len(getattr(lifecycle_report, "events", ()) or ())),
                SessionApiMetric("checkpoint_count", len(getattr(lifecycle_report, "checkpoints", ()) or ())),
                SessionApiMetric("transition_count", len(getattr(lifecycle_report, "transitions", ()) or ())),
                SessionApiMetric("blocker_count", getattr(lifecycle_report, "blocker_count", 0)),
            ),
            links=(SessionApiLink("runtime_target", "packages/runtime/zyra_runtime/claude_session_lifecycle_state.py"),),
            payload=dict(payload or {}),
        )

    def _lineage_section(self, lineage_report: Any | None, payload: Mapping[str, Any] | None) -> SessionApiSection | None:
        if lineage_report is None:
            return None
        ok = object_ok(lineage_report)
        return SessionApiSection(
            section_id="session_lineage",
            kind=SessionApiSectionKind.LINEAGE,
            title="Session Lineage",
            ok=ok,
            status=str(getattr(lineage_report, "status", "ready" if ok else "blocked")),
            summary="Source-to-target lineage for Zyra-owned session lifecycle modules.",
            metrics=(
                SessionApiMetric("target_count", len(getattr(lineage_report, "targets", ()) or ())),
                SessionApiMetric("blocker_count", getattr(lineage_report, "blocker_count", 0)),
                SessionApiMetric("warning_count", getattr(lineage_report, "warning_count", 0)),
            ),
            links=tuple(
                SessionApiLink("runtime_target", str(getattr(target, "path", "")), surface=str(getattr(target, "surface", "")))
                for target in getattr(lineage_report, "targets", ()) or ()
                if getattr(target, "path", "")
            ),
            payload=dict(payload or {}),
        )


def build_projection_health_checks(
    *,
    contracts: Any,
    input_report: Any,
    context_snapshot: Any,
    replay_plan: Any | None,
    turn_lifecycle: Any | None,
    acceptance_report: Any | None,
    lifecycle_report: Any | None,
    lineage_report: Any | None,
) -> Iterable[SessionApiHealthCheck]:
    clean_safe = getattr(contracts, "clean_runtime_safe", False) is True
    yield SessionApiHealthCheck(
        check_id="contract_clean_runtime",
        severity=SessionApiProjectionSeverity.BLOCKER if not clean_safe else SessionApiProjectionSeverity.PASS,
        section=SessionApiSectionKind.CONTRACT,
        ok=clean_safe,
        message="Productized runtime contract must not require source repo, sidecar or vendor runtime.",
    )
    yield component_health_check(
        "input_processor",
        SessionApiSectionKind.INPUT,
        input_report,
        "QueryInputProcessor must accept at least one request input.",
    )
    yield component_health_check(
        "context_snapshot",
        SessionApiSectionKind.CONTEXT,
        context_snapshot,
        "ContextAssemblyRuntime must produce a usable pre-query context snapshot.",
    )
    if replay_plan is not None:
        yield component_health_check(
            "session_replay",
            SessionApiSectionKind.REPLAY,
            replay_plan,
            "Replay plan must be ready or degraded, not blocked.",
        )
    if turn_lifecycle is None:
        yield missing_component_check("turn_lifecycle", SessionApiSectionKind.TURN_LIFECYCLE)
    else:
        yield component_health_check(
            "turn_lifecycle",
            SessionApiSectionKind.TURN_LIFECYCLE,
            turn_lifecycle,
            "Turn lifecycle projection must bind input/context/tool-plan state.",
        )
    if acceptance_report is None:
        yield missing_component_check("session_acceptance", SessionApiSectionKind.ACCEPTANCE)
    else:
        yield component_health_check(
            "session_acceptance",
            SessionApiSectionKind.ACCEPTANCE,
            acceptance_report,
            "Session acceptance gate must pass before API projection is marked ready.",
        )
    if lifecycle_report is None:
        yield missing_component_check("session_lifecycle", SessionApiSectionKind.LIFECYCLE)
    else:
        yield component_health_check(
            "session_lifecycle",
            SessionApiSectionKind.LIFECYCLE,
            lifecycle_report,
            "Lifecycle state machine must observe required session phases.",
        )
    if lineage_report is None:
        yield missing_component_check("session_lineage", SessionApiSectionKind.LINEAGE)
    else:
        yield component_health_check(
            "session_lineage",
            SessionApiSectionKind.LINEAGE,
            lineage_report,
            "Lineage report must find required Zyra-owned runtime targets.",
        )


def component_health_check(
    check_id: str,
    section: SessionApiSectionKind,
    component: Any,
    message: str,
) -> SessionApiHealthCheck:
    ok = object_ok(component)
    return SessionApiHealthCheck(
        check_id=check_id,
        severity=SessionApiProjectionSeverity.PASS if ok else SessionApiProjectionSeverity.BLOCKER,
        section=section,
        ok=ok,
        message=message,
        metadata=component_metadata(component),
    )


def missing_component_check(check_id: str, section: SessionApiSectionKind) -> SessionApiHealthCheck:
    return SessionApiHealthCheck(
        check_id=check_id,
        severity=SessionApiProjectionSeverity.BLOCKER,
        section=section,
        ok=False,
        message="Required API projection component was not built.",
    )


def projection_status(health_checks: Sequence[SessionApiHealthCheck]) -> SessionApiProjectionStatus:
    if any(check.blocking and not check.ok for check in health_checks):
        return SessionApiProjectionStatus.BLOCKED
    if any(check.severity == SessionApiProjectionSeverity.WARNING for check in health_checks):
        return SessionApiProjectionStatus.DEGRADED
    return SessionApiProjectionStatus.READY


def projection_metadata(
    *,
    contracts: Any,
    input_report: Any,
    context_snapshot: Any,
    replay_plan: Any | None,
    turn_lifecycle: Any | None,
    acceptance_report: Any | None,
    lifecycle_report: Any | None,
    lineage_report: Any | None,
    status: SessionApiProjectionStatus,
    health_checks: Sequence[SessionApiHealthCheck],
    sections: Sequence[SessionApiSection],
) -> dict[str, str]:
    metadata: dict[str, str] = {
        "session_api_projection_status": str(status),
        "session_api_projection_ok": str(status in {SessionApiProjectionStatus.READY, SessionApiProjectionStatus.DEGRADED}).lower(),
        "session_api_projection_section_count": str(len(sections)),
        "session_api_projection_health_check_count": str(len(health_checks)),
        "session_api_projection_blockers": str(sum(1 for check in health_checks if check.blocking and not check.ok)),
    }
    metadata.update(call_metadata(contracts))
    for component in (
        input_report,
        context_snapshot,
        replay_plan,
        turn_lifecycle,
        acceptance_report,
        lifecycle_report,
        lineage_report,
    ):
        metadata.update(call_metadata(component))
    return {str(key): str(value) for key, value in metadata.items()}


def contract_projection(contracts: Any) -> dict[str, Any]:
    if contracts is None:
        return {}
    return {
        "runtime_id": str(getattr(contracts, "runtime_id", "")),
        "owner_unit": str(getattr(contracts, "owner_unit", "")),
        "source_repo": str(getattr(contracts, "source_repo", "")),
        "contract_source": str(getattr(contracts, "contract_source", "")),
        "clean_runtime_safe": getattr(contracts, "clean_runtime_safe", False) is True,
        "primary_runtime_paths": list(getattr(contracts, "primary_runtime_paths", ()) or ()),
        "health": to_jsonable(getattr(contracts, "health", {}) or {}),
        "default_path": to_jsonable(getattr(contracts, "default_path", {}) or {}),
    }


def object_to_payload(component: Any, *, include_text: bool | None = None) -> dict[str, Any]:
    if component is None:
        return {}
    to_dict = getattr(component, "to_dict", None)
    if callable(to_dict):
        if include_text is None:
            return to_jsonable(to_dict())
        try:
            return to_jsonable(to_dict(include_text=include_text))
        except TypeError:
            return to_jsonable(to_dict())
    if isinstance(component, Mapping):
        return to_jsonable(dict(component))
    return {"repr": repr(component)}


def optional_payload(component: Any | None) -> dict[str, Any] | None:
    if component is None:
        return None
    return object_to_payload(component)


def object_ok(component: Any) -> bool:
    if component is None:
        return False
    if hasattr(component, "ok"):
        return getattr(component, "ok") is True
    if isinstance(component, Mapping):
        return component.get("ok") is True
    return True


def component_metadata(component: Any) -> dict[str, Any]:
    metadata = call_metadata(component)
    for key in ("status", "blocker_count", "warning_count", "report_id", "projection_id", "mapping_id"):
        if hasattr(component, key):
            metadata[key] = str(getattr(component, key))
    return metadata


def call_metadata(component: Any) -> dict[str, str]:
    if component is None:
        return {}
    if hasattr(component, "metadata") and isinstance(getattr(component, "metadata"), Mapping):
        values = {str(key): str(value) for key, value in getattr(component, "metadata").items()}
    else:
        values = {}
    metadata_values = getattr(component, "metadata_values", None)
    if callable(metadata_values):
        values.update({str(key): str(value) for key, value in metadata_values().items()})
    metadata_method = getattr(component, "metadata", None)
    if callable(metadata_method):
        try:
            values.update({str(key): str(value) for key, value in metadata_method().items()})
        except TypeError:
            pass
    return values


def first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


def session_api_projection_metadata(projection: SessionApiProjection | None) -> dict[str, str]:
    if projection is None:
        return {
            "session_api_projection_ok": "",
            "session_api_projection_status": "",
        }
    return dict(projection.metadata)


def render_session_api_projection_markdown(projection: SessionApiProjection) -> str:
    lines = [
        "# Session Foundation API Projection",
        "",
        f"- ok: `{str(projection.ok).lower()}`",
        f"- status: `{projection.status}`",
        f"- owner_unit: `{projection.owner_unit}`",
        f"- session_id: `{projection.session_id}`",
        f"- worker_request_id: `{projection.worker_request_id}`",
        f"- sections: `{len(projection.sections)}`",
        f"- blockers: `{projection.blocker_count}`",
        f"- warnings: `{projection.warning_count}`",
        "",
        "## Sections",
        "",
    ]
    for section in projection.sections:
        lines.append(f"- `{section.kind}` ok=`{str(section.ok).lower()}` status=`{section.status}` {section.summary}")
    lines.extend(["", "## Health", ""])
    for check in projection.health_checks:
        lines.append(f"- `{check.severity}` `{check.check_id}` ok=`{str(check.ok).lower()}` {check.message}")
    return "\n".join(lines) + "\n"


def api_projection_from_payload(payload: Mapping[str, Any]) -> SessionApiProjection:
    sections = tuple(
        SessionApiSection(
            section_id=str(item.get("section_id") or ""),
            kind=_enum_or_default(SessionApiSectionKind, item.get("kind"), SessionApiSectionKind.HEALTH),
            title=str(item.get("title") or ""),
            ok=item.get("ok") is True,
            status=str(item.get("status") or ""),
            summary=str(item.get("summary") or ""),
            metrics=tuple(
                SessionApiMetric(
                    key=str(metric.get("key") or ""),
                    value=metric.get("value"),
                    label=str(metric.get("label") or ""),
                    unit=str(metric.get("unit") or ""),
                    metadata=dict(metric.get("metadata") or {}),
                )
                for metric in item.get("metrics", [])
                if isinstance(metric, Mapping)
            ),
            links=tuple(
                SessionApiLink(
                    rel=str(link.get("rel") or ""),
                    target=str(link.get("target") or ""),
                    label=str(link.get("label") or ""),
                    surface=str(link.get("surface") or ""),
                    metadata=dict(link.get("metadata") or {}),
                )
                for link in item.get("links", [])
                if isinstance(link, Mapping)
            ),
            payload=dict(item.get("payload") or {}),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("sections", [])
        if isinstance(item, Mapping)
    )
    health_checks = tuple(
        SessionApiHealthCheck(
            check_id=str(item.get("check_id") or ""),
            severity=_enum_or_default(SessionApiProjectionSeverity, item.get("severity"), SessionApiProjectionSeverity.INFO),
            section=_enum_or_default(SessionApiSectionKind, item.get("section"), SessionApiSectionKind.HEALTH),
            ok=item.get("ok") is True,
            message=str(item.get("message") or ""),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("healthChecks", [])
        if isinstance(item, Mapping)
    )
    return SessionApiProjection(
        owner_unit=str(payload.get("ownerUnit") or "M1-02B"),
        status=_enum_or_default(SessionApiProjectionStatus, payload.get("status"), SessionApiProjectionStatus.BLOCKED),
        generated_at=str(payload.get("generatedAt") or now_iso()),
        session_id=str(payload.get("sessionId") or ""),
        worker_request_id=str(payload.get("workerRequestId") or ""),
        session_foundation=dict(payload.get("sessionFoundation") or {}),
        input_report=dict(payload.get("inputReport") or {}),
        context_snapshot=dict(payload.get("contextSnapshot") or {}),
        runtime_contract=dict(payload.get("runtimeContract") or {}),
        session_replay=payload.get("sessionReplay") if isinstance(payload.get("sessionReplay"), Mapping) else None,
        turn_lifecycle=payload.get("turnLifecycle") if isinstance(payload.get("turnLifecycle"), Mapping) else None,
        session_acceptance=payload.get("sessionAcceptance") if isinstance(payload.get("sessionAcceptance"), Mapping) else None,
        session_lifecycle=payload.get("sessionLifecycle") if isinstance(payload.get("sessionLifecycle"), Mapping) else None,
        session_lineage=payload.get("sessionLineage") if isinstance(payload.get("sessionLineage"), Mapping) else None,
        sections=sections,
        health_checks=health_checks,
        metadata={str(key): str(value) for key, value in dict(payload.get("metadata") or {}).items()},
    )


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default
