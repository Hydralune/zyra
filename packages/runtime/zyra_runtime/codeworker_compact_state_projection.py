from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .runtime_budget_state import CODEWORKER_API_FOUNDATION_RUNTIME_ID, M1_02D_OWNER_UNIT


class CompactStateProjectionStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class CompactStateProjectionSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class CompactStateProjectionSectionKind(StrEnum):
    FOUNDATION = "foundation"
    COMPACT_RESTORE = "compact_restore"
    COMPACT_RESTORE_POLICY = "compact_restore_policy"
    CONTEXT_EPOCH = "context_epoch"
    RUNTIME_BUDGET = "runtime_budget"
    RUNTIME_BUDGET_REPLAY = "runtime_budget_replay"
    MODEL_PROVIDER = "model_provider"
    MODEL_STREAM = "model_stream"
    STREAM_WATCHDOG = "stream_watchdog"
    API_RETRY = "api_retry"
    API_RETRY_PLAYBOOK = "api_retry_playbook"
    AUDIT = "audit"
    EVENT_FLOW = "event_flow"


@dataclass(frozen=True, slots=True)
class CompactStateMetric:
    metric_id: str
    name: str
    value: str
    unit: str = ""
    status: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status.lower() not in {"blocked", "disabled", "fail", "failed", "false"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
            "ok": self.ok,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactStateLink:
    link_id: str
    label: str
    target: str
    kind: str = "metadata"
    present: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "label": self.label,
            "target": self.target,
            "kind": self.kind,
            "present": self.present,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactStateSection:
    section_id: str
    kind: CompactStateProjectionSectionKind
    title: str
    status: str
    ok: bool
    metrics: tuple[CompactStateMetric, ...]
    links: tuple[CompactStateLink, ...] = ()
    summary: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def metric_count(self) -> int:
        return len(self.metrics)

    @property
    def missing_link_count(self) -> int:
        return sum(1 for link in self.links if not link.present)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "kind": str(self.kind),
            "title": self.title,
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "metrics": [metric.to_dict() for metric in self.metrics],
            "links": [link.to_dict() for link in self.links],
            "metric_count": self.metric_count,
            "missing_link_count": self.missing_link_count,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactStateProjectionFinding:
    code: str
    severity: CompactStateProjectionSeverity
    section: CompactStateProjectionSectionKind
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == CompactStateProjectionSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "section": str(self.section),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactStateProjectionReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    sections: tuple[CompactStateSection, ...]
    findings: tuple[CompactStateProjectionFinding, ...]
    event_phase_counts: dict[str, int]
    source_decisions: tuple[dict[str, str], ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return all(section.ok for section in self.sections) and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> CompactStateProjectionStatus:
        if not self.ok:
            return CompactStateProjectionStatus.BLOCKED
        if self.findings or any(section.status not in {"ready", "pass", "not_needed", "recovered"} for section in self.sections):
            return CompactStateProjectionStatus.DEGRADED
        return CompactStateProjectionStatus.READY

    @property
    def blocking_count(self) -> int:
        return sum(1 for section in self.sections if not section.ok) + sum(1 for finding in self.findings if finding.blocking)

    @property
    def section_count(self) -> int:
        return len(self.sections)

    def section(self, kind: CompactStateProjectionSectionKind) -> CompactStateSection | None:
        for section in self.sections:
            if section.kind == kind:
                return section
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_compact_state_projection.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "blocking_count": self.blocking_count,
            "section_count": self.section_count,
            "sections": [section.to_dict() for section in self.sections],
            "findings": [finding.to_dict() for finding in self.findings],
            "event_phase_counts": dict(self.event_phase_counts),
            "source_decisions": [dict(item) for item in self.source_decisions],
            "created_at": self.created_at,
        }

    def compact_state_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "status": str(self.status),
            "report_id": self.report_id,
            "blocking_count": self.blocking_count,
        }
        for section in self.sections:
            key = str(section.kind)
            payload[key] = {
                "status": section.status,
                "ok": section.ok,
                "summary": section.summary,
                "metrics": {metric.name: metric.value for metric in section.metrics},
                "links": [link.to_dict() for link in section.links],
            }
        return payload

    def metadata(self) -> dict[str, str]:
        return {
            "compact_state_projection_report_id": self.report_id,
            "compact_state_projection_owner_unit": self.owner_unit,
            "compact_state_projection_runtime_id": self.runtime_id,
            "compact_state_projection_ok": str(self.ok).lower(),
            "compact_state_projection_status": str(self.status),
            "compact_state_projection_sections": str(self.section_count),
            "compact_state_projection_blocking_count": str(self.blocking_count),
            "compact_state_projection_findings": str(len(self.findings)),
        }


class CodeWorkerCompactStateProjectionRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        metadata: Mapping[str, str],
        event_records: Sequence[EventRecord],
    ) -> CompactStateProjectionReport:
        phase_counts = _phase_counts(event_records)
        sections = (
            self._foundation_section(metadata),
            self._compact_restore_section(metadata),
            self._compact_restore_policy_section(metadata),
            self._context_epoch_section(metadata),
            self._runtime_budget_section(metadata),
            self._runtime_budget_replay_section(metadata),
            self._model_provider_section(metadata),
            self._model_stream_section(metadata),
            self._watchdog_section(metadata),
            self._api_retry_section(metadata),
            self._api_retry_playbook_section(metadata),
            self._audit_section(metadata),
            self._event_flow_section(phase_counts),
        )
        findings = self._findings(sections, phase_counts=phase_counts)
        return CompactStateProjectionReport(
            report_id=new_id("compact_state"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            sections=sections,
            findings=tuple(findings),
            event_phase_counts=phase_counts,
            source_decisions=default_compact_state_projection_source_decisions(),
        )

    def event_for_report(
        self,
        report: CompactStateProjectionReport,
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
                    "phase": "compact_state_projection",
                    "compact_state_projection": report.to_dict(),
                }
            },
        )

    def _foundation_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.FOUNDATION,
            "CodeWorker API Foundation",
            metadata.get("codeworker_api_foundation_status", "missing"),
            metadata.get("codeworker_api_foundation_ok") == "true",
            [
                _metric("report_id", metadata.get("codeworker_api_foundation_report_id", "")),
                _metric("blockers", metadata.get("codeworker_api_foundation_blockers", "0")),
                _metric("chain", metadata.get("codeworker_api_foundation_chain", "")),
            ],
            links=[_link("foundation-report", metadata.get("codeworker_api_foundation_report_id", ""))],
            summary="Default path foundation gate for compact/API state.",
        )

    def _compact_restore_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.COMPACT_RESTORE,
            "Compact Restore",
            metadata.get("compact_restore_status", "missing"),
            metadata.get("compact_restore_ok") == "true",
            [
                _metric("boundary_id", metadata.get("compact_restore_boundary_id", "")),
                _metric("boundary_applied", metadata.get("compact_restore_boundary_applied", "")),
                _metric("restore_contract_id", metadata.get("compact_restore_contract_id", "")),
                _metric("restore_segments", metadata.get("compact_restore_segments", "0")),
                _metric("missing_segments", metadata.get("compact_restore_missing_segments", "0")),
            ],
            links=[
                _link("compact-boundary", metadata.get("compact_restore_boundary_id", "")),
                _link("restore-contract", metadata.get("compact_restore_contract_id", "")),
                _link("compact-artifact", metadata.get("compact_restore_artifact_id", ""), kind="artifact"),
            ],
            summary="Compact boundary and next-turn restore contract.",
        )

    def _compact_restore_policy_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.COMPACT_RESTORE_POLICY,
            "Compact Restore Policy",
            metadata.get("compact_restore_policy_status", "missing"),
            metadata.get("compact_restore_policy_ok") == "true",
            [
                _metric("report_id", metadata.get("compact_restore_policy_report_id", "")),
                _metric("rules", metadata.get("compact_restore_policy_rules", "0")),
                _metric("satisfied_rules", metadata.get("compact_restore_policy_satisfied_rules", "0")),
                _metric("blocked_rules", metadata.get("compact_restore_policy_blocked_rules", "0")),
                _metric("required_segments", metadata.get("compact_restore_policy_required_segments", "0")),
                _metric("restore_contract_present", metadata.get("compact_restore_policy_restore_contract_present", "")),
            ],
            links=[_link("compact-restore-policy", metadata.get("compact_restore_policy_report_id", ""))],
            summary="Policy validation that required restore segments and budget state survive compaction.",
        )

    def _context_epoch_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.CONTEXT_EPOCH,
            "Context Epoch",
            metadata.get("context_epoch_status", "missing"),
            metadata.get("context_epoch_ok") == "true",
            [
                _metric("report_id", metadata.get("context_epoch_report_id", "")),
                _metric("nodes", metadata.get("context_epoch_nodes", "0")),
                _metric("edges", metadata.get("context_epoch_edges", "0")),
                _metric("restore_epochs", metadata.get("context_epoch_restore_epochs", "0")),
                _metric("blocking_edges", metadata.get("context_epoch_blocking_edges", "0")),
            ],
            links=[_link("context-epoch-report", metadata.get("context_epoch_report_id", ""))],
            summary="Epoch graph joining context, compact, restore, stream and retry state.",
        )

    def _runtime_budget_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.RUNTIME_BUDGET,
            "Runtime Budget",
            metadata.get("runtime_budget_state_status", "missing"),
            metadata.get("runtime_budget_state_ok") == "true",
            [
                _metric("snapshot_id", metadata.get("runtime_budget_state_snapshot_id", "")),
                _metric("context_used_chars", metadata.get("runtime_budget_state_context_used_chars", "0"), unit="chars"),
                _metric("context_limit_chars", metadata.get("runtime_budget_state_context_limit_chars", "0"), unit="chars"),
                _metric("pressure", metadata.get("runtime_budget_state_highest_pressure", "")),
                _metric("retry_count", metadata.get("runtime_budget_state_retry_count", "0")),
                _metric("compact_count", metadata.get("runtime_budget_state_compact_count", "0")),
            ],
            links=[_link("budget-snapshot", metadata.get("runtime_budget_state_snapshot_id", ""))],
            summary="Budget custody shared by compact restore, stream usage and retry state.",
        )

    def _runtime_budget_replay_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.RUNTIME_BUDGET_REPLAY,
            "Runtime Budget Replay",
            metadata.get("runtime_budget_replay_status", "missing"),
            metadata.get("runtime_budget_replay_ok") == "true",
            [
                _metric("report_id", metadata.get("runtime_budget_replay_report_id", "")),
                _metric("snapshot_status", metadata.get("runtime_budget_replay_snapshot_status", "")),
                _metric("mutations", metadata.get("runtime_budget_replay_mutations", "0")),
                _metric("retry_mutations", metadata.get("runtime_budget_replay_retry_mutations", "0")),
                _metric("compact_mutations", metadata.get("runtime_budget_replay_compact_mutations", "0")),
                _metric("restore_mutations", metadata.get("runtime_budget_replay_restore_mutations", "0")),
                _metric("blocking_count", metadata.get("runtime_budget_replay_blocking_count", "0")),
            ],
            links=[_link("budget-replay-report", metadata.get("runtime_budget_replay_report_id", ""))],
            summary="Replay of RuntimeBudgetState mutations against compact/restore/retry event causality.",
        )

    def _model_provider_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.MODEL_PROVIDER,
            "Model Provider",
            metadata.get("model_provider_status", "missing"),
            metadata.get("model_provider_ok") == "true",
            [
                _metric("report_id", metadata.get("model_provider_report_id", "")),
                _metric("selected_model", metadata.get("model_provider_selected_model", "")),
                _metric("selected_provider", metadata.get("model_provider_selected_provider", "")),
                _metric("fallback_models", metadata.get("model_provider_fallback_models", "")),
                _metric("credential_status", metadata.get("model_provider_credential_status", "")),
            ],
            links=[_link("provider-report", metadata.get("model_provider_report_id", ""))],
            summary="Provider catalog route and fallback model set.",
        )

    def _model_stream_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        recovered_ok = (
            metadata.get("api_retry_ok") == "true"
            and metadata.get("api_retry_recovered") == "true"
            and metadata.get("api_retry_status") in {"retried", "fallback_selected"}
            and metadata.get("model_stream_error_kind") not in {"", "none", "disabled"}
        )
        return _section(
            CompactStateProjectionSectionKind.MODEL_STREAM,
            "Model Stream",
            metadata.get("model_stream_status", "missing"),
            metadata.get("model_stream_ok") == "true" or recovered_ok,
            [
                _metric("report_id", metadata.get("model_stream_report_id", "")),
                _metric("model", metadata.get("model_stream_model", "")),
                _metric("frames", metadata.get("model_stream_frames", "0")),
                _metric("error_kind", metadata.get("model_stream_error_kind", "")),
                _metric("input_tokens", metadata.get("model_stream_input_tokens", "0")),
                _metric("output_tokens", metadata.get("model_stream_output_tokens", "0")),
            ],
            links=[_link("model-stream-report", metadata.get("model_stream_report_id", ""))],
            summary="Model stream frames, usage patch and semantic error state.",
        )

    def _watchdog_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.STREAM_WATCHDOG,
            "Stream Watchdog",
            metadata.get("model_stream_watchdog_status", "missing"),
            metadata.get("model_stream_watchdog_ok") == "true",
            [
                _metric("report_id", metadata.get("model_stream_watchdog_report_id", "")),
                _metric("signals", metadata.get("model_stream_watchdog_signals", "0")),
                _metric("error_signals", metadata.get("model_stream_watchdog_error_signals", "0")),
                _metric("recovered_signals", metadata.get("model_stream_watchdog_recovered_signals", "0")),
                _metric("usage_patches", metadata.get("model_stream_watchdog_usage_patches", "0")),
            ],
            links=[_link("stream-watchdog-report", metadata.get("model_stream_watchdog_report_id", ""))],
            summary="Frame watchdog and retry recovery validation.",
        )

    def _api_retry_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.API_RETRY,
            "API Retry",
            metadata.get("api_retry_status", "missing"),
            metadata.get("api_retry_ok") == "true",
            [
                _metric("report_id", metadata.get("api_retry_report_id", "")),
                _metric("retry_count", metadata.get("api_retry_retry_count", "0")),
                _metric("fallback_used", metadata.get("api_retry_fallback_used", "")),
                _metric("recovered", metadata.get("api_retry_recovered", "")),
                _metric("final_model", metadata.get("api_retry_final_model", "")),
                _metric("error_kinds", metadata.get("api_retry_error_kinds", "")),
            ],
            links=[_link("api-retry-report", metadata.get("api_retry_report_id", ""))],
            summary="Retry/fallback decision state for model API errors.",
        )

    def _api_retry_playbook_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.API_RETRY_PLAYBOOK,
            "API Retry Playbook",
            metadata.get("api_retry_playbook_status", "missing"),
            metadata.get("api_retry_playbook_ok") == "true",
            [
                _metric("report_id", metadata.get("api_retry_playbook_report_id", "")),
                _metric("decisions", metadata.get("api_retry_playbook_decisions", "0")),
                _metric("recovered", metadata.get("api_retry_playbook_recovered", "0")),
                _metric("fallbacks", metadata.get("api_retry_playbook_fallbacks", "0")),
                _metric("retry_budget_required", metadata.get("api_retry_playbook_retry_budget_required", "0")),
                _metric("findings", metadata.get("api_retry_playbook_findings", "0")),
            ],
            links=[_link("api-retry-playbook", metadata.get("api_retry_playbook_report_id", ""))],
            summary="Semantic error to retry/fallback decision validation for model API calls.",
        )

    def _audit_section(self, metadata: Mapping[str, str]) -> CompactStateSection:
        return _section(
            CompactStateProjectionSectionKind.AUDIT,
            "Foundation Audit",
            metadata.get("codeworker_api_audit_status", "missing"),
            metadata.get("codeworker_api_audit_ok") == "true",
            [
                _metric("report_id", metadata.get("codeworker_api_audit_report_id", "")),
                _metric("blocking_count", metadata.get("codeworker_api_audit_blocking_count", "0")),
                _metric("event_coverage", metadata.get("codeworker_api_audit_event_coverage", "0")),
                _metric("reachability", metadata.get("codeworker_api_audit_reachability", "0")),
                _metric("restore_quality", metadata.get("codeworker_api_audit_restore_quality", "")),
            ],
            links=[_link("api-audit-report", metadata.get("codeworker_api_audit_report_id", ""))],
            summary="Self-audit for reachability, source decisions and restore quality.",
        )

    def _event_flow_section(self, phase_counts: Mapping[str, int]) -> CompactStateSection:
        required = (
            "compact_restore_report",
            "compact_restore_policy",
            "next_turn_restore_contract",
            "context_epoch_report",
            "model_stream_report",
            "model_stream_watchdog",
            "api_retry_report",
            "api_retry_playbook",
            "runtime_budget_replay",
            "codeworker_api_foundation",
            "codeworker_api_foundation_audit",
        )
        metrics = [_metric(phase, str(phase_counts.get(phase, 0))) for phase in required]
        ok = all(phase_counts.get(phase, 0) > 0 for phase in required)
        return _section(
            CompactStateProjectionSectionKind.EVENT_FLOW,
            "Event Flow",
            "ready" if ok else "blocked",
            ok,
            metrics,
            summary="Required compact/API event phases observed in QueryEngine stream.",
        )

    def _findings(
        self,
        sections: Sequence[CompactStateSection],
        *,
        phase_counts: Mapping[str, int],
    ) -> list[CompactStateProjectionFinding]:
        findings: list[CompactStateProjectionFinding] = []
        for section in sections:
            if not section.ok:
                findings.append(
                    CompactStateProjectionFinding(
                        code="COMPACT_STATE_SECTION_NOT_OK",
                        severity=CompactStateProjectionSeverity.BLOCKER,
                        section=section.kind,
                        message=f"Compact-state projection section {section.kind} is not ok.",
                        metadata={"status": section.status},
                    )
                )
        if phase_counts.get("compact_state_projection", 0) > 1:
            findings.append(
                CompactStateProjectionFinding(
                    code="COMPACT_STATE_PROJECTION_REENTERED",
                    severity=CompactStateProjectionSeverity.WARNING,
                    section=CompactStateProjectionSectionKind.EVENT_FLOW,
                    message="Compact-state projection was emitted more than once in this QueryEngine run.",
                    metadata={"count": str(phase_counts.get("compact_state_projection", 0))},
                )
            )
        return findings


def compact_state_projection_metadata(report: CompactStateProjectionReport | None) -> dict[str, str]:
    if report is None:
        return {
            "compact_state_projection_ok": "false",
            "compact_state_projection_status": "missing",
            "compact_state_projection_report_id": "",
        }
    return report.metadata()


def compact_state_projection_from_metadata(
    metadata: Mapping[str, Any],
    events: Sequence[Any] = (),
) -> dict[str, Any]:
    runtime = CodeWorkerCompactStateProjectionRuntime()
    event_records = [_coerce_event(event) for event in events]
    report = runtime.build_report(
        session_id=str(metadata.get("query_session_id") or ""),
        worker_request_id=str(metadata.get("worker_request_id") or ""),
        metadata={str(k): str(v) for k, v in dict(metadata).items()},
        event_records=[event for event in event_records if event is not None],
    )
    return report.compact_state_payload()


def render_compact_state_projection_markdown(report: CompactStateProjectionReport) -> str:
    lines = [
        "# CodeWorker Compact State Projection",
        "",
        f"- report_id: {report.report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- sections: {report.section_count}",
        f"- blocking_count: {report.blocking_count}",
        "",
        "## Sections",
    ]
    for section in report.sections:
        lines.append(f"- {section.kind}: status={section.status} ok={str(section.ok).lower()} metrics={section.metric_count}")
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def default_compact_state_projection_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/query.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_compact_state_projection.py",
            "decision": "zyra_module_migrated",
            "capability": "compact/API state projection from QueryEngine runtime reports",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/*",
            "target_path": "packages/runtime/zyra_runtime/codeworker_compact_state_projection.py",
            "decision": "adapter_encapsulated",
            "capability": "session event status projection adapted to Zyra API payload",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/*",
            "target_path": "packages/runtime/zyra_runtime/codeworker_compact_state_projection.py",
            "decision": "zyra_module_migrated",
            "capability": "compact restore policy status projection",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/provider/*",
            "target_path": "packages/runtime/zyra_runtime/codeworker_compact_state_projection.py",
            "decision": "adapter_encapsulated",
            "capability": "API retry playbook and runtime budget replay projection",
        },
    )


def _section(
    kind: CompactStateProjectionSectionKind,
    title: str,
    status: str,
    ok: bool,
    metrics: Sequence[CompactStateMetric],
    *,
    links: Sequence[CompactStateLink] = (),
    summary: str = "",
    metadata: Mapping[str, str] | None = None,
) -> CompactStateSection:
    return CompactStateSection(
        section_id=new_id("compact_section"),
        kind=kind,
        title=title,
        status=status or "missing",
        ok=bool(ok),
        metrics=tuple(metrics),
        links=tuple(links),
        summary=summary,
        metadata={str(k): str(v) for k, v in dict(metadata or {}).items()},
    )


def _metric(name: str, value: Any, *, unit: str = "", status: str = "") -> CompactStateMetric:
    return CompactStateMetric(
        metric_id=new_id("compact_metric"),
        name=name,
        value=str(value),
        unit=unit,
        status=status,
    )


def _link(label: str, target: str, *, kind: str = "report") -> CompactStateLink:
    return CompactStateLink(
        link_id=new_id("compact_link"),
        label=label,
        target=str(target or ""),
        kind=kind,
        present=bool(target),
    )


def _phase_counts(event_records: Sequence[EventRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in event_records:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        query_session = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
        phase = str(query_session.get("phase") or "")
        if phase:
            counts[phase] = counts.get(phase, 0) + 1
    return counts


def _coerce_event(value: Any) -> EventRecord | None:
    if isinstance(value, EventRecord):
        return value
    if not isinstance(value, Mapping):
        return None
    payload = value.get("payload")
    return EventRecord(
        run_id=str(value.get("run_id") or ""),
        task_id=str(value.get("task_id") or ""),
        node_id=value.get("node_id"),
        event_type=value.get("event_type") or EventType.AGENT_MESSAGE,
        payload=payload if isinstance(payload, dict) else {},
        created_at=str(value.get("created_at") or now_iso()),
    )
