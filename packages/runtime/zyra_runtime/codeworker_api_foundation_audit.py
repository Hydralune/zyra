from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .codeworker_api_foundation import CodeWorkerApiFoundationReport
from .runtime_budget_state import CODEWORKER_API_FOUNDATION_RUNTIME_ID, M1_02D_OWNER_UNIT


class CodeWorkerApiAuditStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class CodeWorkerApiAuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class CodeWorkerApiAuditSurface(StrEnum):
    EVENT_FLOW = "event_flow"
    REACHABILITY = "reachability"
    DISCONNECT = "disconnect"
    SOURCE_DECISION = "source_decision"
    RESTORE_CONTRACT = "restore_contract"
    BUDGET_CUSTODY = "budget_custody"
    API_STREAM = "api_stream"
    API_RETRY = "api_retry"
    API_PROJECTION = "api_projection"


@dataclass(frozen=True, slots=True)
class CodeWorkerApiAuditFinding:
    code: str
    severity: CodeWorkerApiAuditSeverity
    surface: CodeWorkerApiAuditSurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == CodeWorkerApiAuditSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerApiEventCoverage:
    required_phase: str
    observed_count: int
    required: bool = True
    source: str = "query_session"
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def satisfied(self) -> bool:
        return self.observed_count > 0 or not self.required

    @property
    def blocking(self) -> bool:
        return self.required and not self.satisfied

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_phase": self.required_phase,
            "observed_count": self.observed_count,
            "required": self.required,
            "source": self.source,
            "satisfied": self.satisfied,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerApiReachabilityCheck:
    check_id: str
    component: str
    entrypoint: str
    metadata_key: str
    event_phase: str
    reached_by_default_path: bool
    disabled_changes_result: bool
    disable_semantics_required: bool = False
    evidence: str = ""

    @property
    def ok(self) -> bool:
        return self.reached_by_default_path and (
            self.disabled_changes_result or not self.disable_semantics_required
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "component": self.component,
            "entrypoint": self.entrypoint,
            "metadata_key": self.metadata_key,
            "event_phase": self.event_phase,
            "reached_by_default_path": self.reached_by_default_path,
            "disabled_changes_result": self.disabled_changes_result,
            "disable_semantics_required": self.disable_semantics_required,
            "ok": self.ok,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerApiSourceDecisionCheck:
    check_id: str
    source_repo: str
    source_path: str
    target_path: str
    decision: str
    capability: str
    required: bool = True

    @property
    def zyra_owned(self) -> bool:
        normalized = self.target_path.replace("\\", "/")
        return normalized.startswith("packages/") or normalized.startswith("apps/") or normalized.startswith("skills/") or normalized.startswith("scripts/")

    @property
    def vendor_like(self) -> bool:
        normalized = self.target_path.replace("\\", "/").lower()
        return any(part in normalized for part in ("/vendor/", "vendor-runtimes", "source-pool", "runtime-sources"))

    @property
    def ok(self) -> bool:
        if not self.required:
            return True
        return self.zyra_owned and not self.vendor_like and self.decision in {
            "zyra_module_migrated",
            "adapter_encapsulated",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "decision": self.decision,
            "capability": self.capability,
            "required": self.required,
            "zyra_owned": self.zyra_owned,
            "vendor_like": self.vendor_like,
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerApiRestoreQuality:
    contract_id: str
    boundary_id: str
    restore_segments: int
    preserved_segments: int
    missing_required_segments: int
    has_budget_state: bool
    has_tool_result: bool
    has_file_or_plan: bool
    has_mcp_or_skill: bool

    @property
    def ok(self) -> bool:
        return (
            bool(self.contract_id)
            and self.restore_segments > 0
            and self.missing_required_segments == 0
            and self.has_budget_state
            and (self.has_tool_result or self.has_file_or_plan)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "boundary_id": self.boundary_id,
            "restore_segments": self.restore_segments,
            "preserved_segments": self.preserved_segments,
            "missing_required_segments": self.missing_required_segments,
            "has_budget_state": self.has_budget_state,
            "has_tool_result": self.has_tool_result,
            "has_file_or_plan": self.has_file_or_plan,
            "has_mcp_or_skill": self.has_mcp_or_skill,
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerApiAuditReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    foundation_report_id: str
    event_coverage: tuple[CodeWorkerApiEventCoverage, ...]
    reachability: tuple[CodeWorkerApiReachabilityCheck, ...]
    source_decisions: tuple[CodeWorkerApiSourceDecisionCheck, ...]
    restore_quality: CodeWorkerApiRestoreQuality
    findings: tuple[CodeWorkerApiAuditFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return (
            not any(item.blocking for item in self.event_coverage)
            and all(item.ok for item in self.reachability)
            and all(item.ok for item in self.source_decisions)
            and self.restore_quality.ok
            and not any(finding.blocking for finding in self.findings)
        )

    @property
    def status(self) -> CodeWorkerApiAuditStatus:
        if not self.ok:
            return CodeWorkerApiAuditStatus.FAIL
        if self.findings:
            return CodeWorkerApiAuditStatus.WARN
        return CodeWorkerApiAuditStatus.PASS

    @property
    def blocking_count(self) -> int:
        return (
            sum(1 for item in self.event_coverage if item.blocking)
            + sum(1 for item in self.reachability if not item.ok)
            + sum(1 for item in self.source_decisions if not item.ok)
            + (0 if self.restore_quality.ok else 1)
            + sum(1 for finding in self.findings if finding.blocking)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_api_foundation_audit.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "foundation_report_id": self.foundation_report_id,
            "ok": self.ok,
            "status": str(self.status),
            "blocking_count": self.blocking_count,
            "event_coverage": [item.to_dict() for item in self.event_coverage],
            "reachability": [item.to_dict() for item in self.reachability],
            "source_decisions": [item.to_dict() for item in self.source_decisions],
            "restore_quality": self.restore_quality.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "codeworker_api_audit_report_id": self.report_id,
            "codeworker_api_audit_owner_unit": self.owner_unit,
            "codeworker_api_audit_runtime_id": self.runtime_id,
            "codeworker_api_audit_ok": str(self.ok).lower(),
            "codeworker_api_audit_status": str(self.status),
            "codeworker_api_audit_blocking_count": str(self.blocking_count),
            "codeworker_api_audit_event_coverage": str(sum(1 for item in self.event_coverage if item.satisfied)),
            "codeworker_api_audit_reachability": str(sum(1 for item in self.reachability if item.ok)),
            "codeworker_api_audit_source_decisions": str(sum(1 for item in self.source_decisions if item.ok)),
            "codeworker_api_audit_restore_quality": str(self.restore_quality.ok).lower(),
            "codeworker_api_audit_findings": str(len(self.findings)),
        }


class CodeWorkerApiFoundationAuditRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
        required_phases: Sequence[str] | None = None,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.required_phases = tuple(required_phases or default_required_codeworker_api_phases())

    def build_report(
        self,
        *,
        foundation_report: CodeWorkerApiFoundationReport,
        event_records: Sequence[EventRecord],
        metadata: Mapping[str, str],
    ) -> CodeWorkerApiAuditReport:
        phase_counts = _phase_counts(event_records)
        event_coverage = tuple(
            CodeWorkerApiEventCoverage(
                required_phase=phase,
                observed_count=phase_counts.get(phase, 0),
                required=True,
                metadata={"foundation_report_id": foundation_report.report_id},
            )
            for phase in self.required_phases
        )
        reachability = self._reachability(foundation_report, metadata=metadata, phase_counts=phase_counts)
        source_decisions = self._source_decisions(foundation_report)
        restore_quality = self._restore_quality(foundation_report)
        findings = self._findings(
            foundation_report=foundation_report,
            event_coverage=event_coverage,
            reachability=reachability,
            source_decisions=source_decisions,
            restore_quality=restore_quality,
            metadata=metadata,
        )
        return CodeWorkerApiAuditReport(
            report_id=new_id("codeworker_api_audit"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=foundation_report.session_id,
            worker_request_id=foundation_report.worker_request_id,
            foundation_report_id=foundation_report.report_id,
            event_coverage=event_coverage,
            reachability=reachability,
            source_decisions=source_decisions,
            restore_quality=restore_quality,
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: CodeWorkerApiAuditReport,
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
                    "phase": "codeworker_api_foundation_audit",
                    "codeworker_api_foundation_audit": report.to_dict(),
                }
            },
        )

    def _reachability(
        self,
        foundation_report: CodeWorkerApiFoundationReport,
        *,
        metadata: Mapping[str, str],
        phase_counts: Mapping[str, int],
    ) -> tuple[CodeWorkerApiReachabilityCheck, ...]:
        components = (
            (
                "RuntimeBudgetState",
                "zyra_runtime.RuntimeBudgetState",
                "runtime_budget_state_ok",
                "runtime_budget_updated",
                "disable_runtime_budget_state",
            ),
            (
                "CompactRestoreRuntime",
                "zyra_runtime.CompactRestoreRuntime.build_report",
                "compact_restore_ok",
                "compact_restore_report",
                "disable_compact_restore_runtime",
            ),
            (
                "ModelStreamRuntime",
                "zyra_runtime.ModelStreamRuntime.stream",
                "model_stream_ok",
                "model_stream_report",
                "disable_model_stream_runtime",
            ),
            (
                "ApiRetryRuntime",
                "zyra_runtime.ApiRetryRuntime.build_report",
                "api_retry_ok",
                "api_retry_report",
                "disable_api_retry_runtime",
            ),
            (
                "CodeWorkerApiFoundationRuntime",
                "zyra_runtime.CodeWorkerApiFoundationRuntime.build_report",
                "codeworker_api_foundation_ok",
                "codeworker_api_foundation",
                "disable_codeworker_api_foundation_runtime",
            ),
        )
        checks: list[CodeWorkerApiReachabilityCheck] = []
        for component, entrypoint, metadata_key, event_phase, disable_flag in components:
            reached = bool(metadata.get(metadata_key)) and phase_counts.get(event_phase, 0) > 0
            disable_verified = metadata.get(f"{metadata_key}_disable_semantics_verified") == "true"
            checks.append(
                CodeWorkerApiReachabilityCheck(
                    check_id=new_id("api_reach"),
                    component=component,
                    entrypoint=entrypoint,
                    metadata_key=metadata_key,
                    event_phase=event_phase,
                    reached_by_default_path=reached,
                    disabled_changes_result=disable_verified,
                    disable_semantics_required=False,
                    evidence=(
                        f"metadata[{metadata_key}]={metadata.get(metadata_key, '')}; "
                        f"phase_count={phase_counts.get(event_phase, 0)}; "
                        f"disable_flag={disable_flag}; "
                        "disconnect behavior is covered by integration tests, not by this in-run audit"
                    ),
                )
            )
        if not foundation_report.ok:
            checks.append(
                CodeWorkerApiReachabilityCheck(
                    check_id=new_id("api_reach"),
                    component="FoundationGate",
                    entrypoint="tool_runtime_gate_failures",
                    metadata_key="codeworker_api_foundation_ok",
                    event_phase="codeworker_api_foundation",
                    reached_by_default_path=phase_counts.get("codeworker_api_foundation", 0) > 0,
                    disabled_changes_result=metadata.get("codeworker_api_foundation_ok") == "false",
                    disable_semantics_required=True,
                    evidence="foundation report is currently blocking; gate failure is expected to surface in WorkerResult",
                )
            )
        return tuple(checks)

    def _source_decisions(
        self,
        foundation_report: CodeWorkerApiFoundationReport,
    ) -> tuple[CodeWorkerApiSourceDecisionCheck, ...]:
        rows: list[CodeWorkerApiSourceDecisionCheck] = []
        for source in foundation_report.source_decisions:
            rows.append(
                CodeWorkerApiSourceDecisionCheck(
                    check_id=new_id("api_src"),
                    source_repo=str(source.get("source_repo") or ""),
                    source_path=str(source.get("source_path") or ""),
                    target_path=str(source.get("target_path") or ""),
                    decision=str(source.get("decision") or ""),
                    capability=str(source.get("capability") or ""),
                    required=True,
                )
            )
        for source in foundation_report.compact_restore.source_decisions:
            rows.append(
                CodeWorkerApiSourceDecisionCheck(
                    check_id=new_id("api_src"),
                    source_repo=str(source.get("source_repo") or ""),
                    source_path=str(source.get("source_path") or ""),
                    target_path=str(source.get("target_path") or ""),
                    decision=str(source.get("decision") or ""),
                    capability=str(source.get("capability") or ""),
                    required=True,
                )
            )
        return tuple(rows)

    def _restore_quality(self, foundation_report: CodeWorkerApiFoundationReport) -> CodeWorkerApiRestoreQuality:
        contract = foundation_report.compact_restore.restore_contract
        if contract is None:
            return CodeWorkerApiRestoreQuality(
                contract_id="",
                boundary_id=foundation_report.compact_restore.boundary_id,
                restore_segments=0,
                preserved_segments=len(foundation_report.compact_restore.preserved_segments),
                missing_required_segments=1,
                has_budget_state=False,
                has_tool_result=False,
                has_file_or_plan=False,
                has_mcp_or_skill=False,
            )
        kinds = {str(segment.kind).split(".")[-1] for segment in contract.restore_segments}
        return CodeWorkerApiRestoreQuality(
            contract_id=contract.contract_id,
            boundary_id=contract.boundary_id,
            restore_segments=contract.restore_segment_count,
            preserved_segments=len(contract.preserved_segments),
            missing_required_segments=contract.missing_required_segments,
            has_budget_state="budget_state" in kinds,
            has_tool_result="tool_result" in kinds,
            has_file_or_plan=bool({"file_attachment", "active_plan"} & kinds),
            has_mcp_or_skill=bool({"mcp_instruction_delta", "invoked_skill"} & kinds),
        )

    def _findings(
        self,
        *,
        foundation_report: CodeWorkerApiFoundationReport,
        event_coverage: Sequence[CodeWorkerApiEventCoverage],
        reachability: Sequence[CodeWorkerApiReachabilityCheck],
        source_decisions: Sequence[CodeWorkerApiSourceDecisionCheck],
        restore_quality: CodeWorkerApiRestoreQuality,
        metadata: Mapping[str, str],
    ) -> list[CodeWorkerApiAuditFinding]:
        findings: list[CodeWorkerApiAuditFinding] = []
        for coverage in event_coverage:
            if coverage.blocking:
                findings.append(
                    CodeWorkerApiAuditFinding(
                        code="REQUIRED_EVENT_PHASE_MISSING",
                        severity=CodeWorkerApiAuditSeverity.BLOCKER,
                        surface=CodeWorkerApiAuditSurface.EVENT_FLOW,
                        message=f"Required CodeWorker API event phase {coverage.required_phase} was not observed.",
                        metadata={"phase": coverage.required_phase},
                    )
                )
        for check in reachability:
            if not check.ok:
                findings.append(
                    CodeWorkerApiAuditFinding(
                        code="COMPONENT_NOT_REACHABLE_FROM_DEFAULT_PATH",
                        severity=CodeWorkerApiAuditSeverity.BLOCKER,
                        surface=CodeWorkerApiAuditSurface.REACHABILITY,
                        message=f"{check.component} did not prove default-path reachability and disable semantics.",
                        metadata={"component": check.component, "evidence": check.evidence},
                    )
                )
        for source in source_decisions:
            if not source.ok:
                findings.append(
                    CodeWorkerApiAuditFinding(
                        code="SOURCE_DECISION_NOT_INTERNALIZED",
                        severity=CodeWorkerApiAuditSeverity.BLOCKER,
                        surface=CodeWorkerApiAuditSurface.SOURCE_DECISION,
                        message=f"Source decision for {source.source_repo}:{source.source_path} does not land in a Zyra-owned target.",
                        metadata={"target_path": source.target_path, "decision": source.decision},
                    )
                )
        if not restore_quality.ok:
            findings.append(
                CodeWorkerApiAuditFinding(
                    code="RESTORE_CONTRACT_QUALITY_FAILED",
                    severity=CodeWorkerApiAuditSeverity.BLOCKER,
                    surface=CodeWorkerApiAuditSurface.RESTORE_CONTRACT,
                    message="Next-turn restore contract does not preserve enough typed state for continuation.",
                    metadata=restore_quality.to_dict(),
                )
            )
        if metadata.get("runtime_budget_state_status") in {"disabled", "blocked"}:
            findings.append(
                CodeWorkerApiAuditFinding(
                    code="BUDGET_CUSTODY_BLOCKED",
                    severity=CodeWorkerApiAuditSeverity.BLOCKER,
                    surface=CodeWorkerApiAuditSurface.BUDGET_CUSTODY,
                    message="RuntimeBudgetState is not ready for CodeWorker API foundation custody.",
                    metadata={"status": metadata.get("runtime_budget_state_status", "")},
                )
            )
        if not foundation_report.ok and not any(finding.blocking for finding in findings):
            findings.append(
                CodeWorkerApiAuditFinding(
                    code="FOUNDATION_REPORT_BLOCKED_WITHOUT_CLASSIFIED_FINDING",
                    severity=CodeWorkerApiAuditSeverity.ERROR,
                    surface=CodeWorkerApiAuditSurface.API_PROJECTION,
                    message="CodeWorkerApiFoundationReport is blocked, but audit did not classify a blocker.",
                )
            )
        return findings


def default_required_codeworker_api_phases() -> tuple[str, ...]:
    return (
        "runtime_budget_updated",
        "runtime_budget_replay",
        "compact_restore_report",
        "compact_restore_policy",
        "next_turn_restore_contract",
        "context_epoch_report",
        "model_provider_catalog",
        "model_stream_report",
        "model_stream_watchdog",
        "api_retry_report",
        "api_retry_playbook",
        "codeworker_api_foundation",
    )


def codeworker_api_audit_metadata(report: CodeWorkerApiAuditReport | None) -> dict[str, str]:
    if report is None:
        return {
            "codeworker_api_audit_ok": "false",
            "codeworker_api_audit_status": "missing",
            "codeworker_api_audit_report_id": "",
        }
    return report.metadata()


def render_codeworker_api_audit_markdown(report: CodeWorkerApiAuditReport) -> str:
    lines = [
        "# CodeWorker API Foundation Audit",
        "",
        f"- report_id: {report.report_id}",
        f"- foundation_report_id: {report.foundation_report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- blocking_count: {report.blocking_count}",
        "",
        "## Event Coverage",
    ]
    for item in report.event_coverage:
        lines.append(f"- {item.required_phase}: observed={item.observed_count} satisfied={str(item.satisfied).lower()}")
    lines.extend(["", "## Reachability"])
    for item in report.reachability:
        lines.append(f"- {item.component}: ok={str(item.ok).lower()} evidence={item.evidence}")
    lines.extend(["", "## Source Decisions"])
    for item in report.source_decisions:
        lines.append(f"- {item.source_repo}:{item.source_path} -> {item.target_path} decision={item.decision} ok={str(item.ok).lower()}")
    lines.extend(["", "## Restore Quality"])
    quality = report.restore_quality
    lines.append(
        f"- contract={quality.contract_id} segments={quality.restore_segments} preserved={quality.preserved_segments} ok={str(quality.ok).lower()}"
    )
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def _phase_counts(event_records: Sequence[EventRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in event_records:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        query_session = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
        phase = str(query_session.get("phase") or "")
        if phase:
            counts[phase] = counts.get(phase, 0) + 1
    return counts
