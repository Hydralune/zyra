from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .claude_session_foundation_audit import FoundationAuditReport
from .claude_session_replay_runtime import SessionReplayPlan
from .claude_session_store import CodeWorkerSessionSeed
from .claude_transcript_event_mapper import TranscriptEventMappingReport
from .claude_turn_lifecycle_runtime import TurnLifecycleProjection


class SessionAcceptanceStatus(StrEnum):
    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    PENDING_TRANSCRIPT = "pending_transcript"


class SessionAcceptanceSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class SessionAcceptanceSurface(StrEnum):
    SESSION_SEED = "session_seed"
    INPUT_PROCESSING = "input_processing"
    CONTEXT_ASSEMBLY = "context_assembly"
    SESSION_STORE = "session_store"
    FOUNDATION_AUDIT = "foundation_audit"
    SESSION_REPLAY = "session_replay"
    TURN_LIFECYCLE = "turn_lifecycle"
    TRANSCRIPT_MAPPING = "transcript_mapping"
    CLEAN_BOUNDARY = "clean_boundary"
    EVENT_REACHABILITY = "event_reachability"


class SessionAcceptanceRuleKind(StrEnum):
    REQUIRED = "required"
    BEHAVIORAL = "behavioral"
    CONSISTENCY = "consistency"
    REACHABILITY = "reachability"
    DISCONNECT = "disconnect"


@dataclass(frozen=True, slots=True)
class SessionAcceptanceEvidence:
    evidence_id: str
    surface: SessionAcceptanceSurface
    label: str
    value: Any
    source_path: str = ""
    target_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "surface": str(self.surface),
            "label": self.label,
            "value": to_jsonable(self.value),
            "source_path": self.source_path,
            "target_path": self.target_path,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionAcceptanceFinding:
    code: str
    severity: SessionAcceptanceSeverity
    surface: SessionAcceptanceSurface
    rule_kind: SessionAcceptanceRuleKind
    message: str
    evidence: tuple[SessionAcceptanceEvidence, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == SessionAcceptanceSeverity.BLOCKER

    @property
    def passed(self) -> bool:
        return self.severity == SessionAcceptanceSeverity.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "rule_kind": str(self.rule_kind),
            "message": self.message,
            "blocking": self.blocking,
            "passed": self.passed,
            "evidence": [item.to_dict() for item in self.evidence],
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionAcceptanceRule:
    rule_id: str
    surface: SessionAcceptanceSurface
    rule_kind: SessionAcceptanceRuleKind
    description: str
    required: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "surface": str(self.surface),
            "rule_kind": str(self.rule_kind),
            "description": self.description,
            "required": self.required,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionAcceptanceComponent:
    component_id: str
    surface: SessionAcceptanceSurface
    ok: bool
    status: str
    blocker_count: int = 0
    warning_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        return self.ok and self.warning_count > 0

    def to_evidence(self) -> SessionAcceptanceEvidence:
        return SessionAcceptanceEvidence(
            evidence_id=f"acceptance_component_{self.component_id}",
            surface=self.surface,
            label=self.component_id,
            value=self.to_dict(),
            metadata={"component_status": self.status},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "surface": str(self.surface),
            "ok": self.ok,
            "status": self.status,
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "degraded": self.degraded,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionAcceptanceReport:
    report_id: str
    status: SessionAcceptanceStatus
    session_id: str
    worker_request_id: str
    components: tuple[SessionAcceptanceComponent, ...]
    findings: tuple[SessionAcceptanceFinding, ...]
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {SessionAcceptanceStatus.ACCEPTED, SessionAcceptanceStatus.DEGRADED}

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking) + sum(
            component.blocker_count for component in self.components
        )

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == SessionAcceptanceSeverity.WARNING) + sum(
            component.warning_count for component in self.components
        )

    @property
    def pass_count(self) -> int:
        return sum(1 for finding in self.findings if finding.passed)

    @property
    def component_statuses(self) -> dict[str, str]:
        return {str(component.surface): component.status for component in self.components}

    def metadata_values(self) -> dict[str, str]:
        return {
            "session_acceptance_report_id": self.report_id,
            "session_acceptance_ok": str(self.ok).lower(),
            "session_acceptance_status": str(self.status),
            "session_acceptance_components": str(len(self.components)),
            "session_acceptance_findings": str(len(self.findings)),
            "session_acceptance_blockers": str(self.blocker_count),
            "session_acceptance_warnings": str(self.warning_count),
            "session_acceptance_passes": str(self.pass_count),
            "session_acceptance_seed_status": self.component_statuses.get(str(SessionAcceptanceSurface.SESSION_SEED), ""),
            "session_acceptance_turn_status": self.component_statuses.get(str(SessionAcceptanceSurface.TURN_LIFECYCLE), ""),
            "session_acceptance_transcript_status": self.component_statuses.get(str(SessionAcceptanceSurface.TRANSCRIPT_MAPPING), ""),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "components": [component.to_dict() for component in self.components],
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "pass_count": self.pass_count,
            "component_statuses": self.component_statuses,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class SessionAcceptanceRuntime:
    """Combines session foundation reports into a behavioral acceptance gate."""

    def __init__(self, *, rules: Sequence[SessionAcceptanceRule] | None = None) -> None:
        self.rules = tuple(rules or default_session_acceptance_rules())

    def evaluate(
        self,
        *,
        seed: CodeWorkerSessionSeed,
        foundation_audit: FoundationAuditReport | None,
        turn_lifecycle: TurnLifecycleProjection | None,
        replay_plan: SessionReplayPlan | None = None,
        transcript_mapping: TranscriptEventMappingReport | None = None,
        require_transcript: bool = False,
    ) -> SessionAcceptanceReport:
        components = list(
            build_acceptance_components(
                seed=seed,
                foundation_audit=foundation_audit,
                turn_lifecycle=turn_lifecycle,
                replay_plan=replay_plan,
                transcript_mapping=transcript_mapping,
                require_transcript=require_transcript,
            )
        )
        findings = list(self._evaluate_rules(components=components, require_transcript=require_transcript))
        status = acceptance_status(components=components, findings=findings, require_transcript=require_transcript, transcript_mapping=transcript_mapping)
        return SessionAcceptanceReport(
            report_id=new_id("accept"),
            status=status,
            session_id=seed.session_id,
            worker_request_id=seed.worker_request_id,
            components=tuple(components),
            findings=tuple(findings),
            metadata={
                "rule_count": len(self.rules),
                "rule_ids": [rule.rule_id for rule in self.rules],
                "source_path": "src/QueryEngine.ts",
                "target_path": "packages/runtime/zyra_runtime/claude_session_acceptance_runtime.py",
                "owner_unit": "M1-02B",
            },
        )

    def event_for_report(
        self,
        report: SessionAcceptanceReport,
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
                    "phase": "session_acceptance",
                    "report_id": report.report_id,
                    "ok": report.ok,
                    "status": str(report.status),
                    "component_count": len(report.components),
                    "finding_count": len(report.findings),
                    "blocker_count": report.blocker_count,
                    "warning_count": report.warning_count,
                }
            },
        )

    def _evaluate_rules(
        self,
        *,
        components: Sequence[SessionAcceptanceComponent],
        require_transcript: bool,
    ) -> Iterable[SessionAcceptanceFinding]:
        by_surface = {component.surface: component for component in components}
        for rule in self.rules:
            component = by_surface.get(rule.surface)
            if component is None:
                if rule.surface == SessionAcceptanceSurface.TRANSCRIPT_MAPPING and not require_transcript:
                    yield self._finding(
                        code=f"{rule.rule_id}_pending",
                        severity=SessionAcceptanceSeverity.INFO,
                        surface=rule.surface,
                        rule_kind=rule.rule_kind,
                        message=f"{rule.description} Pending until QueryEngine produces a transcript.",
                    )
                    continue
                yield self._finding(
                    code=f"{rule.rule_id}_missing",
                    severity=SessionAcceptanceSeverity.BLOCKER if rule.required else SessionAcceptanceSeverity.WARNING,
                    surface=rule.surface,
                    rule_kind=rule.rule_kind,
                    message=f"{rule.description} Component is missing.",
                )
                continue
            if component.ok:
                yield self._finding(
                    code=f"{rule.rule_id}_pass",
                    severity=SessionAcceptanceSeverity.PASS,
                    surface=rule.surface,
                    rule_kind=rule.rule_kind,
                    message=rule.description,
                    evidence=(component.to_evidence(),),
                )
            else:
                yield self._finding(
                    code=f"{rule.rule_id}_blocked",
                    severity=SessionAcceptanceSeverity.BLOCKER if rule.required else SessionAcceptanceSeverity.WARNING,
                    surface=rule.surface,
                    rule_kind=rule.rule_kind,
                    message=rule.description,
                    evidence=(component.to_evidence(),),
                )

    def _finding(
        self,
        *,
        code: str,
        severity: SessionAcceptanceSeverity,
        surface: SessionAcceptanceSurface,
        rule_kind: SessionAcceptanceRuleKind,
        message: str,
        evidence: Sequence[SessionAcceptanceEvidence] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionAcceptanceFinding:
        return SessionAcceptanceFinding(
            code=code,
            severity=severity,
            surface=surface,
            rule_kind=rule_kind,
            message=message,
            evidence=tuple(evidence),
            metadata=dict(metadata or {}),
        )


def build_acceptance_components(
    *,
    seed: CodeWorkerSessionSeed,
    foundation_audit: FoundationAuditReport | None,
    turn_lifecycle: TurnLifecycleProjection | None,
    replay_plan: SessionReplayPlan | None,
    transcript_mapping: TranscriptEventMappingReport | None,
    require_transcript: bool,
) -> Iterable[SessionAcceptanceComponent]:
    yield SessionAcceptanceComponent(
        component_id="code_worker_session_seed",
        surface=SessionAcceptanceSurface.SESSION_SEED,
        ok=seed.ok,
        status=str(seed.status),
        blocker_count=0 if seed.ok else 1,
        warning_count=0,
        metadata={
            "session_id": seed.session_id,
            "input_ok": seed.input_report.ok,
            "context_ok": seed.context_snapshot.ok if seed.context_snapshot else False,
            "store_ok": seed.store_receipt.ok if seed.store_receipt else False,
        },
    )
    yield SessionAcceptanceComponent(
        component_id="query_input_processor",
        surface=SessionAcceptanceSurface.INPUT_PROCESSING,
        ok=seed.input_report.ok,
        status="ready" if seed.input_report.ok else "blocked",
        blocker_count=0 if seed.input_report.ok else max(1, len(seed.input_report.blockers)),
        warning_count=len(seed.input_report.warnings),
        metadata=seed.input_report.to_dict(),
    )
    context_ok = seed.context_snapshot.ok if seed.context_snapshot else False
    yield SessionAcceptanceComponent(
        component_id="context_assembly",
        surface=SessionAcceptanceSurface.CONTEXT_ASSEMBLY,
        ok=context_ok,
        status=str(seed.context_snapshot.status) if seed.context_snapshot else "missing",
        blocker_count=seed.context_snapshot.blocker_count if seed.context_snapshot else 1,
        warning_count=seed.context_snapshot.warning_count if seed.context_snapshot else 0,
        metadata=seed.context_snapshot.metadata_values() if seed.context_snapshot else {},
    )
    store_ok = seed.store_receipt.ok if seed.store_receipt else False
    yield SessionAcceptanceComponent(
        component_id="code_worker_session_store",
        surface=SessionAcceptanceSurface.SESSION_STORE,
        ok=store_ok,
        status="ready" if store_ok else "blocked",
        blocker_count=0 if store_ok else 1,
        warning_count=0,
        metadata=seed.store_receipt.to_dict() if seed.store_receipt else {},
    )
    if foundation_audit is not None:
        yield SessionAcceptanceComponent(
            component_id="session_foundation_audit",
            surface=SessionAcceptanceSurface.FOUNDATION_AUDIT,
            ok=foundation_audit.ok,
            status=foundation_audit.status,
            blocker_count=foundation_audit.blocker_count,
            warning_count=foundation_audit.warning_count,
            metadata=foundation_audit.to_dict(),
        )
    if replay_plan is not None:
        yield SessionAcceptanceComponent(
            component_id="session_replay",
            surface=SessionAcceptanceSurface.SESSION_REPLAY,
            ok=replay_plan.ok,
            status=str(replay_plan.status),
            blocker_count=replay_plan.blocker_count,
            warning_count=replay_plan.warning_count,
            metadata=replay_plan.to_dict(include_raw_context=False),
        )
    if turn_lifecycle is not None:
        yield SessionAcceptanceComponent(
            component_id="turn_lifecycle",
            surface=SessionAcceptanceSurface.TURN_LIFECYCLE,
            ok=turn_lifecycle.ok,
            status=str(turn_lifecycle.status),
            blocker_count=turn_lifecycle.blocker_count,
            warning_count=turn_lifecycle.warning_count,
            metadata=turn_lifecycle.to_dict(),
        )
    if transcript_mapping is not None:
        yield SessionAcceptanceComponent(
            component_id="transcript_mapping",
            surface=SessionAcceptanceSurface.TRANSCRIPT_MAPPING,
            ok=transcript_mapping.ok,
            status=str(transcript_mapping.status),
            blocker_count=transcript_mapping.blocker_count,
            warning_count=transcript_mapping.warning_count,
            metadata=transcript_mapping.to_dict(include_events=False),
        )
    elif require_transcript:
        yield SessionAcceptanceComponent(
            component_id="transcript_mapping",
            surface=SessionAcceptanceSurface.TRANSCRIPT_MAPPING,
            ok=False,
            status="missing",
            blocker_count=1,
            warning_count=0,
            metadata={"required": True},
        )


def acceptance_status(
    *,
    components: Sequence[SessionAcceptanceComponent],
    findings: Sequence[SessionAcceptanceFinding],
    require_transcript: bool,
    transcript_mapping: TranscriptEventMappingReport | None,
) -> SessionAcceptanceStatus:
    if any(component.blocker_count > 0 or not component.ok for component in components):
        return SessionAcceptanceStatus.BLOCKED
    if any(finding.blocking for finding in findings):
        return SessionAcceptanceStatus.BLOCKED
    if require_transcript and transcript_mapping is None:
        return SessionAcceptanceStatus.PENDING_TRANSCRIPT
    if any(component.warning_count > 0 for component in components) or any(
        finding.severity == SessionAcceptanceSeverity.WARNING for finding in findings
    ):
        return SessionAcceptanceStatus.DEGRADED
    return SessionAcceptanceStatus.ACCEPTED


def default_session_acceptance_rules() -> tuple[SessionAcceptanceRule, ...]:
    return (
        SessionAcceptanceRule(
            rule_id="seed_required",
            surface=SessionAcceptanceSurface.SESSION_SEED,
            rule_kind=SessionAcceptanceRuleKind.REQUIRED,
            description="CodeWorker session seed must be created before QueryEngine dispatch.",
        ),
        SessionAcceptanceRule(
            rule_id="input_required",
            surface=SessionAcceptanceSurface.INPUT_PROCESSING,
            rule_kind=SessionAcceptanceRuleKind.BEHAVIORAL,
            description="QueryInputProcessor must accept at least one input record.",
        ),
        SessionAcceptanceRule(
            rule_id="context_required",
            surface=SessionAcceptanceSurface.CONTEXT_ASSEMBLY,
            rule_kind=SessionAcceptanceRuleKind.BEHAVIORAL,
            description="ContextAssemblyRuntime must produce a usable context snapshot.",
        ),
        SessionAcceptanceRule(
            rule_id="store_required",
            surface=SessionAcceptanceSurface.SESSION_STORE,
            rule_kind=SessionAcceptanceRuleKind.CONSISTENCY,
            description="CodeWorkerSessionStore must append seed/input/context records.",
        ),
        SessionAcceptanceRule(
            rule_id="audit_required",
            surface=SessionAcceptanceSurface.FOUNDATION_AUDIT,
            rule_kind=SessionAcceptanceRuleKind.REACHABILITY,
            description="SessionFoundationAuditor must validate the actual seed and store replay.",
        ),
        SessionAcceptanceRule(
            rule_id="turn_lifecycle_required",
            surface=SessionAcceptanceSurface.TURN_LIFECYCLE,
            rule_kind=SessionAcceptanceRuleKind.REACHABILITY,
            description="TurnLifecycleRuntime must bind input/context/tool plan before QueryEngine dispatch.",
        ),
        SessionAcceptanceRule(
            rule_id="transcript_mapping_required",
            surface=SessionAcceptanceSurface.TRANSCRIPT_MAPPING,
            rule_kind=SessionAcceptanceRuleKind.CONSISTENCY,
            description="TranscriptEventMapper must validate QuerySession transcript after QueryEngine completion.",
            required=False,
        ),
    )


def session_acceptance_metadata(report: SessionAcceptanceReport | None) -> dict[str, str]:
    if report is None:
        return {
            "session_acceptance_report_id": "",
            "session_acceptance_ok": "",
            "session_acceptance_status": "",
        }
    return report.metadata_values()


def render_session_acceptance_markdown(report: SessionAcceptanceReport) -> str:
    lines = [
        "# Session Acceptance Report",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- report_id: `{report.report_id}`",
        f"- session_id: `{report.session_id}`",
        f"- worker_request_id: `{report.worker_request_id}`",
        f"- components: `{len(report.components)}`",
        f"- blockers: `{report.blocker_count}`",
        f"- warnings: `{report.warning_count}`",
        f"- passes: `{report.pass_count}`",
        "",
        "## Components",
        "",
    ]
    for component in report.components:
        lines.append(
            f"- `{component.surface}` ok=`{str(component.ok).lower()}` status=`{component.status}` "
            f"blockers=`{component.blocker_count}` warnings=`{component.warning_count}`"
        )
    lines.extend(["", "## Findings", ""])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- `{finding.severity}` `{finding.surface}` `{finding.code}` {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def acceptance_report_from_payload(payload: Mapping[str, Any]) -> SessionAcceptanceReport:
    components = tuple(
        SessionAcceptanceComponent(
            component_id=str(item.get("component_id") or ""),
            surface=_enum_or_default(SessionAcceptanceSurface, item.get("surface"), SessionAcceptanceSurface.SESSION_SEED),
            ok=item.get("ok") is True,
            status=str(item.get("status") or ""),
            blocker_count=_safe_int(item.get("blocker_count"), default=0),
            warning_count=_safe_int(item.get("warning_count"), default=0),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("components", [])
        if isinstance(item, Mapping)
    )
    findings = tuple(
        SessionAcceptanceFinding(
            code=str(item.get("code") or ""),
            severity=_enum_or_default(SessionAcceptanceSeverity, item.get("severity"), SessionAcceptanceSeverity.INFO),
            surface=_enum_or_default(SessionAcceptanceSurface, item.get("surface"), SessionAcceptanceSurface.SESSION_SEED),
            rule_kind=_enum_or_default(SessionAcceptanceRuleKind, item.get("rule_kind"), SessionAcceptanceRuleKind.REQUIRED),
            message=str(item.get("message") or ""),
            evidence=(),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("findings", [])
        if isinstance(item, Mapping)
    )
    return SessionAcceptanceReport(
        report_id=str(payload.get("report_id") or new_id("accept")),
        status=_enum_or_default(SessionAcceptanceStatus, payload.get("status"), SessionAcceptanceStatus.BLOCKED),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        components=components,
        findings=findings,
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
