from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_permission_session import ToolPermissionSessionReport
from .tool_runtime_result_context import ToolResultContextReport
from .tool_runtime_session_bridge import ToolSessionBridgeReport


class ToolIntegrationRequirement(StrEnum):
    SESSION_TOOL_USE_TO_RUNTIME = "session_tool_use_to_runtime"
    TOOL_RESULT_SESSION_APPEND = "tool_result_session_append"
    PERMISSION_PENDING_HANDOFF = "permission_pending_handoff"
    BUDGETED_RESULT_CONTEXT = "budgeted_result_context"
    CONCURRENCY_ORDER_AUDITABLE = "concurrency_order_auditable"
    VALIDATION_FAILURE_VISIBLE = "validation_failure_visible"
    DISCONNECT_CHANGES_BEHAVIOR = "disconnect_changes_behavior"
    OPENCODE_OR_HERMES_EFFECT = "opencode_or_hermes_effect"


class ToolIntegrationStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class ToolIntegrationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolIntegrationSurface(StrEnum):
    SESSION_BRIDGE = "session_bridge"
    TOOL_EXECUTION = "tool_execution"
    RESULT_CONTEXT = "result_context"
    PERMISSION = "permission"
    BUDGET = "budget"
    CONCURRENCY = "concurrency"
    FAILURE = "failure"
    DISCONNECT = "disconnect"
    SOURCE_PORT = "source_port"
    EVENT_LOG = "event_log"


@dataclass(frozen=True, slots=True)
class ToolIntegrationEvidence:
    evidence_id: str
    requirement: ToolIntegrationRequirement
    surface: ToolIntegrationSurface
    status: ToolIntegrationStatus
    summary: str
    event_phases: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status in {ToolIntegrationStatus.PASS, ToolIntegrationStatus.NOT_APPLICABLE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "requirement": str(self.requirement),
            "surface": str(self.surface),
            "status": str(self.status),
            "ok": self.ok,
            "summary": self.summary,
            "event_phases": list(self.event_phases),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolIntegrationFinding:
    code: str
    severity: ToolIntegrationSeverity
    surface: ToolIntegrationSurface
    message: str
    requirement: ToolIntegrationRequirement | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolIntegrationSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "requirement": str(self.requirement) if self.requirement else "",
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolIntegrationReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    evidence: tuple[ToolIntegrationEvidence, ...]
    findings: tuple[ToolIntegrationFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolIntegrationStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolIntegrationStatus.FAIL
        if self.findings:
            return ToolIntegrationStatus.WARN
        return ToolIntegrationStatus.PASS

    @property
    def passed_count(self) -> int:
        return sum(1 for item in self.evidence if item.status == ToolIntegrationStatus.PASS)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == ToolIntegrationSeverity.WARNING)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def non_happy_path_count(self) -> int:
        non_happy = {
            ToolIntegrationRequirement.PERMISSION_PENDING_HANDOFF,
            ToolIntegrationRequirement.BUDGETED_RESULT_CONTEXT,
            ToolIntegrationRequirement.VALIDATION_FAILURE_VISIBLE,
        }
        return sum(1 for item in self.evidence if item.requirement in non_happy and item.status == ToolIntegrationStatus.PASS)

    @property
    def requirements(self) -> dict[str, str]:
        return {str(item.requirement): str(item.status) for item in self.evidence}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_runtime_integration_audit.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "evidence_count": len(self.evidence),
            "passed_count": self.passed_count,
            "warning_count": self.warning_count,
            "blocker_count": self.blocker_count,
            "non_happy_path_count": self.non_happy_path_count,
            "requirements": self.requirements,
            "evidence": [item.to_dict() for item in self.evidence],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_integration_report_id": self.report_id,
            "tool_integration_owner_unit": self.owner_unit,
            "tool_integration_runtime_id": self.runtime_id,
            "tool_integration_ok": str(self.ok).lower(),
            "tool_integration_status": str(self.status),
            "tool_integration_evidence": str(len(self.evidence)),
            "tool_integration_passed": str(self.passed_count),
            "tool_integration_warnings": str(self.warning_count),
            "tool_integration_blockers": str(self.blocker_count),
            "tool_integration_non_happy_paths": str(self.non_happy_path_count),
            **{f"tool_integration_requirement_{key.split('.')[-1]}": value for key, value in self.requirements.items()},
        }


class ToolIntegrationAuditRuntime:
    """Builds the slice-02C-02 runtime acceptance matrix from live reports."""

    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        session_bridge_report: ToolSessionBridgeReport | None,
        result_context_report: ToolResultContextReport | None,
        permission_session_report: ToolPermissionSessionReport | None,
        receipts: Sequence[Mapping[str, Any]],
        streaming_report: Mapping[str, Any] | Any,
        concurrency_report: Mapping[str, Any] | Any,
        event_records: Sequence[EventRecord],
        disabled_components: Sequence[str] = (),
    ) -> ToolIntegrationReport:
        phases = _event_phases(event_records)
        receipt_stats = _receipt_stats(receipts)
        streaming_payload = _payload(streaming_report)
        concurrency_payload = _payload(concurrency_report)
        evidence = [
            self._session_tool_use_evidence(session_bridge_report, receipt_stats, phases),
            self._tool_result_append_evidence(result_context_report, phases),
            self._permission_evidence(permission_session_report, receipt_stats, phases),
            self._budget_evidence(result_context_report, receipt_stats, phases),
            self._concurrency_evidence(concurrency_payload, streaming_payload, phases),
            self._validation_evidence(receipt_stats, phases),
            self._disconnect_evidence(disabled_components),
            self._source_port_evidence(session_bridge_report, result_context_report, permission_session_report),
        ]
        findings = self._findings(evidence)
        return ToolIntegrationReport(
            report_id=new_id("toolintegration"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            evidence=tuple(evidence),
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: ToolIntegrationReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
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
                    "phase": "tool_integration_audit",
                    "tool_integration": report.to_dict(),
                }
            },
        )

    def _session_tool_use_evidence(
        self,
        session_bridge_report: ToolSessionBridgeReport | None,
        receipt_stats: Mapping[str, int],
        phases: Mapping[str, int],
    ) -> ToolIntegrationEvidence:
        bridge_count = session_bridge_report.valid_tool_use_count if session_bridge_report is not None else 0
        accepted = phases.get("assistant_tool_use_accepted", 0)
        completed = receipt_stats.get("receipt_count", 0)
        status = ToolIntegrationStatus.PASS if bridge_count and accepted >= bridge_count and completed >= bridge_count else ToolIntegrationStatus.FAIL
        if not bridge_count:
            status = ToolIntegrationStatus.NOT_APPLICABLE if completed else ToolIntegrationStatus.FAIL
        return ToolIntegrationEvidence(
            evidence_id=new_id("toolintevidence"),
            requirement=ToolIntegrationRequirement.SESSION_TOOL_USE_TO_RUNTIME,
            surface=ToolIntegrationSurface.SESSION_BRIDGE,
            status=status,
            summary="Assistant/session tool_use blocks are accepted by ToolExecutionRuntime before tool execution.",
            event_phases=("tool_session_bridge", "assistant_tool_use_requested", "assistant_tool_use_accepted", "tool_call_started"),
            metadata={
                "bridge_tool_uses": str(bridge_count),
                "accepted_events": str(accepted),
                "receipt_count": str(completed),
                "bridge_origin": str(session_bridge_report.origin) if session_bridge_report is not None else "",
            },
        )

    def _tool_result_append_evidence(
        self,
        result_context_report: ToolResultContextReport | None,
        phases: Mapping[str, int],
    ) -> ToolIntegrationEvidence:
        projected = result_context_report.projection_count if result_context_report is not None else 0
        appended = result_context_report.appended_message_count if result_context_report is not None else 0
        event_count = phases.get("tool_result_session_appended", 0)
        status = ToolIntegrationStatus.PASS if projected and appended >= projected and event_count >= projected else ToolIntegrationStatus.FAIL
        if projected == 0:
            status = ToolIntegrationStatus.NOT_APPLICABLE
        return ToolIntegrationEvidence(
            evidence_id=new_id("toolintevidence"),
            requirement=ToolIntegrationRequirement.TOOL_RESULT_SESSION_APPEND,
            surface=ToolIntegrationSurface.RESULT_CONTEXT,
            status=status,
            summary="Bounded tool results are projected into session-visible tool_result messages.",
            event_phases=("tool_result", "tool_result_session_appended", "tool_result_context_projected"),
            metadata={
                "projections": str(projected),
                "appended_messages": str(appended),
                "append_events": str(event_count),
            },
        )

    def _permission_evidence(
        self,
        permission_session_report: ToolPermissionSessionReport | None,
        receipt_stats: Mapping[str, int],
        phases: Mapping[str, int],
    ) -> ToolIntegrationEvidence:
        questions = permission_session_report.question_count if permission_session_report is not None else 0
        pending = permission_session_report.pending_count if permission_session_report is not None else 0
        permission_results = receipt_stats.get("permission_required_count", 0) + receipt_stats.get("permission_denied_count", 0)
        status = ToolIntegrationStatus.PASS if questions and pending and permission_results else ToolIntegrationStatus.NOT_APPLICABLE
        return ToolIntegrationEvidence(
            evidence_id=new_id("toolintevidence"),
            requirement=ToolIntegrationRequirement.PERMISSION_PENDING_HANDOFF,
            surface=ToolIntegrationSurface.PERMISSION,
            status=status,
            summary="Permission-required tool results create session-visible questions before side effects continue.",
            event_phases=("tool_permission_handoff", "tool_permission_question_appended", "tool_permission_pending_blocker"),
            metadata={
                "questions": str(questions),
                "pending": str(pending),
                "permission_results": str(permission_results),
                "question_events": str(phases.get("tool_permission_question_appended", 0)),
            },
        )

    def _budget_evidence(
        self,
        result_context_report: ToolResultContextReport | None,
        receipt_stats: Mapping[str, int],
        phases: Mapping[str, int],
    ) -> ToolIntegrationEvidence:
        budgeted = result_context_report.budgeted_count if result_context_report is not None else 0
        blocked_raw = result_context_report.raw_output_blocked_count if result_context_report is not None else 0
        artifact_refs = result_context_report.artifact_ref_count if result_context_report is not None else 0
        receipt_budgeted = receipt_stats.get("budgeted_count", 0)
        status = ToolIntegrationStatus.PASS if budgeted and blocked_raw and artifact_refs and receipt_budgeted else ToolIntegrationStatus.NOT_APPLICABLE
        return ToolIntegrationEvidence(
            evidence_id=new_id("toolintevidence"),
            requirement=ToolIntegrationRequirement.BUDGETED_RESULT_CONTEXT,
            surface=ToolIntegrationSurface.BUDGET,
            status=status,
            summary="Large tool results are truncated in context and linked to persisted artifacts.",
            event_phases=("tool_result_budget_exceeded", "watchdog_signal", "tool_result_context_projected"),
            metadata={
                "budgeted_projections": str(budgeted),
                "raw_output_blocked": str(blocked_raw),
                "artifact_refs": str(artifact_refs),
                "receipt_budgeted": str(receipt_budgeted),
                "budget_events": str(phases.get("tool_result_budget_exceeded", 0)),
            },
        )

    def _concurrency_evidence(
        self,
        concurrency_payload: Mapping[str, Any],
        streaming_payload: Mapping[str, Any],
        phases: Mapping[str, int],
    ) -> ToolIntegrationEvidence:
        concurrent_batches = _int(concurrency_payload.get("concurrent_batch_count"))
        serial_write_batches = _int(concurrency_payload.get("serial_write_batch_count"))
        max_parallel_width = _int(concurrency_payload.get("max_parallel_width"))
        frame_count = _int(streaming_payload.get("frame_count"))
        if frame_count and (concurrent_batches or serial_write_batches) and phases.get("tool_batch_started", 0):
            status = ToolIntegrationStatus.PASS
        elif frame_count and phases.get("tool_batch_started", 0):
            status = ToolIntegrationStatus.NOT_APPLICABLE
        else:
            status = ToolIntegrationStatus.FAIL
        return ToolIntegrationEvidence(
            evidence_id=new_id("toolintevidence"),
            requirement=ToolIntegrationRequirement.CONCURRENCY_ORDER_AUDITABLE,
            surface=ToolIntegrationSurface.CONCURRENCY,
            status=status,
            summary="Streaming frames and concurrency report make concurrent/serial execution order auditable.",
            event_phases=("tool_batch_started", "tool_stream_frame", "tool_concurrency_report", "tool_batch_completed"),
            metadata={
                "concurrent_batches": str(concurrent_batches),
                "serial_write_batches": str(serial_write_batches),
                "max_parallel_width": str(max_parallel_width),
                "streaming_frames": str(frame_count),
            },
        )

    def _validation_evidence(
        self,
        receipt_stats: Mapping[str, int],
        phases: Mapping[str, int],
    ) -> ToolIntegrationEvidence:
        schema_errors = receipt_stats.get("schema_error_count", 0)
        status = ToolIntegrationStatus.PASS if schema_errors and phases.get("tool_failure_signal", 0) else ToolIntegrationStatus.NOT_APPLICABLE
        return ToolIntegrationEvidence(
            evidence_id=new_id("toolintevidence"),
            requirement=ToolIntegrationRequirement.VALIDATION_FAILURE_VISIBLE,
            surface=ToolIntegrationSurface.FAILURE,
            status=status,
            summary="Schema validation failures are represented as tool results and watchdog/failure signals.",
            event_phases=("tool_failure_signal", "watchdog_signal", "tool_call_completed"),
            metadata={
                "schema_errors": str(schema_errors),
                "failure_signal_events": str(phases.get("tool_failure_signal", 0)),
                "watchdog_events": str(phases.get("watchdog_signal", 0)),
            },
        )

    def _disconnect_evidence(self, disabled_components: Sequence[str]) -> ToolIntegrationEvidence:
        disabled = tuple(str(component) for component in disabled_components if component)
        status = ToolIntegrationStatus.PASS if disabled else ToolIntegrationStatus.NOT_APPLICABLE
        return ToolIntegrationEvidence(
            evidence_id=new_id("toolintevidence"),
            requirement=ToolIntegrationRequirement.DISCONNECT_CHANGES_BEHAVIOR,
            surface=ToolIntegrationSurface.DISCONNECT,
            status=status,
            summary="Configured disconnect switches stop or alter the tool loop before side effects.",
            event_phases=("tool_loop_foundation_disabled",),
            metadata={"disabled_components": ",".join(disabled), "disabled_count": str(len(disabled))},
        )

    def _source_port_evidence(
        self,
        session_bridge_report: ToolSessionBridgeReport | None,
        result_context_report: ToolResultContextReport | None,
        permission_session_report: ToolPermissionSessionReport | None,
    ) -> ToolIntegrationEvidence:
        opencode_uses = session_bridge_report.opencode_tool_use_count if session_bridge_report is not None else 0
        budgeted = result_context_report.budgeted_count if result_context_report is not None else 0
        permission_questions = permission_session_report.question_count if permission_session_report is not None else 0
        status = ToolIntegrationStatus.PASS if opencode_uses or budgeted or permission_questions else ToolIntegrationStatus.WARN
        return ToolIntegrationEvidence(
            evidence_id=new_id("toolintevidence"),
            requirement=ToolIntegrationRequirement.OPENCODE_OR_HERMES_EFFECT,
            surface=ToolIntegrationSurface.SOURCE_PORT,
            status=status,
            summary="opencode/Hermes source mechanisms affect session bridge, budgeted context or permission question behavior.",
            event_phases=("tool_session_bridge", "tool_result_context_projected", "tool_permission_session_bridge"),
            metadata={
                "opencode_tool_uses": str(opencode_uses),
                "budgeted_results": str(budgeted),
                "permission_questions": str(permission_questions),
            },
        )

    def _findings(self, evidence: Sequence[ToolIntegrationEvidence]) -> list[ToolIntegrationFinding]:
        findings: list[ToolIntegrationFinding] = []
        for item in evidence:
            if item.status == ToolIntegrationStatus.FAIL:
                findings.append(
                    ToolIntegrationFinding(
                        code="TOOL_INTEGRATION_REQUIREMENT_FAILED",
                        severity=ToolIntegrationSeverity.BLOCKER,
                        surface=item.surface,
                        requirement=item.requirement,
                        message=f"Tool loop integration requirement failed: {item.requirement}.",
                        metadata={"evidence_id": item.evidence_id, **item.metadata},
                    )
                )
            elif item.status == ToolIntegrationStatus.WARN:
                findings.append(
                    ToolIntegrationFinding(
                        code="TOOL_INTEGRATION_REQUIREMENT_WARN",
                        severity=ToolIntegrationSeverity.WARNING,
                        surface=item.surface,
                        requirement=item.requirement,
                        message=f"Tool loop integration requirement is only partially evidenced: {item.requirement}.",
                        metadata={"evidence_id": item.evidence_id, **item.metadata},
                    )
                )
        return findings


def tool_integration_metadata(report: ToolIntegrationReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_integration_ok": "false",
            "tool_integration_evidence": "0",
            "tool_integration_passed": "0",
        }
    return report.metadata()


def _event_phases(event_records: Sequence[EventRecord]) -> dict[str, int]:
    phases: dict[str, int] = {}
    for event in event_records:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        query = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
        phase = str(query.get("phase") or "")
        if phase:
            phases[phase] = phases.get(phase, 0) + 1
        if payload.get("tool_result") is not None:
            phases["tool_result"] = phases.get("tool_result", 0) + 1
    return phases


def _receipt_stats(receipts: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    stats = {
        "receipt_count": 0,
        "ok_count": 0,
        "error_count": 0,
        "schema_error_count": 0,
        "permission_required_count": 0,
        "permission_denied_count": 0,
        "budgeted_count": 0,
    }
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        stats["receipt_count"] += 1
        result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
        decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
        if result.get("ok") is True:
            stats["ok_count"] += 1
        else:
            stats["error_count"] += 1
        error = str(result.get("error") or "")
        if error == "schema_error":
            stats["schema_error_count"] += 1
        if error == "permission_required":
            stats["permission_required_count"] += 1
        if error == "permission_denied":
            stats["permission_denied_count"] += 1
        if decision.get("applied") is True:
            stats["budgeted_count"] += 1
    return stats


def _payload(report: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if isinstance(report, Mapping):
        return report
    if hasattr(report, "to_dict"):
        payload = report.to_dict()
        return payload if isinstance(payload, Mapping) else {}
    return {}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
