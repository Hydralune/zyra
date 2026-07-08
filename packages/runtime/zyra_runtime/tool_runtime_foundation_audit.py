from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolFoundationAuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolFoundationAuditStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ToolFoundationAuditSurface(StrEnum):
    REGISTRY = "registry"
    CONTEXT = "tool_use_context"
    EXECUTION = "execution"
    BUDGET = "budget"
    PERMISSION = "permission"
    SOURCE_LEDGER = "source_ledger"
    EVENT_FLOW = "event_flow"
    DISCONNECT = "disconnect"


@dataclass(frozen=True, slots=True)
class ToolFoundationAuditEvidence:
    key: str
    value: Any
    source: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": to_jsonable(self.value), "source": self.source}


@dataclass(frozen=True, slots=True)
class ToolFoundationAuditFinding:
    code: str
    severity: ToolFoundationAuditSeverity
    surface: ToolFoundationAuditSurface
    message: str
    evidence: tuple[ToolFoundationAuditEvidence, ...] = ()
    blocking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "evidence": [item.to_dict() for item in self.evidence],
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class ToolFoundationSourceCoverage:
    source_repos: tuple[str, ...]
    required_rows: int
    reference_only_rows: int
    deferred_rows: int
    required_vendor_like_rows: int

    @property
    def ok(self) -> bool:
        return self.required_rows > 0 and self.required_vendor_like_rows == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repos": list(self.source_repos),
            "required_rows": self.required_rows,
            "reference_only_rows": self.reference_only_rows,
            "deferred_rows": self.deferred_rows,
            "required_vendor_like_rows": self.required_vendor_like_rows,
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class ToolFoundationReachability:
    registry_materialized_events: int
    context_modifier_events: int
    tool_call_started_events: int
    tool_call_completed_events: int
    tool_result_events: int
    disabled_events: int

    @property
    def default_path_touched(self) -> bool:
        return self.registry_materialized_events > 0 and self.tool_call_started_events == self.tool_call_completed_events

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_materialized_events": self.registry_materialized_events,
            "context_modifier_events": self.context_modifier_events,
            "tool_call_started_events": self.tool_call_started_events,
            "tool_call_completed_events": self.tool_call_completed_events,
            "tool_result_events": self.tool_result_events,
            "disabled_events": self.disabled_events,
            "default_path_touched": self.default_path_touched,
        }


@dataclass(frozen=True, slots=True)
class ToolFoundationReceiptCoverage:
    receipt_count: int
    raw_result_count: int
    bounded_result_count: int
    budget_decision_count: int
    failure_signal_count: int
    modifier_count: int
    executor_names: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return (
            self.receipt_count == self.raw_result_count
            and self.receipt_count == self.bounded_result_count
            and self.receipt_count == self.budget_decision_count
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_count": self.receipt_count,
            "raw_result_count": self.raw_result_count,
            "bounded_result_count": self.bounded_result_count,
            "budget_decision_count": self.budget_decision_count,
            "failure_signal_count": self.failure_signal_count,
            "modifier_count": self.modifier_count,
            "executor_names": list(self.executor_names),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class ToolFoundationContextCoverage:
    turn_count: int
    modifier_count: int
    permission_handoffs: int
    budget_entries: int
    artifact_refs: int
    read_state_entries: int
    content_replacements: int
    result_chars: int

    @property
    def ok(self) -> bool:
        return self.turn_count >= 0 and (self.turn_count == 0 or self.modifier_count > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_count": self.turn_count,
            "modifier_count": self.modifier_count,
            "permission_handoffs": self.permission_handoffs,
            "budget_entries": self.budget_entries,
            "artifact_refs": self.artifact_refs,
            "read_state_entries": self.read_state_entries,
            "content_replacements": self.content_replacements,
            "result_chars": self.result_chars,
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class ToolFoundationAuditReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    status: ToolFoundationAuditStatus
    created_at: str
    source_coverage: ToolFoundationSourceCoverage
    reachability: ToolFoundationReachability
    receipt_coverage: ToolFoundationReceiptCoverage
    context_coverage: ToolFoundationContextCoverage
    findings: tuple[ToolFoundationAuditFinding, ...] = ()
    metadata_values: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status != ToolFoundationAuditStatus.FAIL and not any(item.blocking for item in self.findings)

    @property
    def blocker_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.findings if item.blocking)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "status": str(self.status),
            "created_at": self.created_at,
            "ok": self.ok,
            "source_coverage": self.source_coverage.to_dict(),
            "reachability": self.reachability.to_dict(),
            "receipt_coverage": self.receipt_coverage.to_dict(),
            "context_coverage": self.context_coverage.to_dict(),
            "findings": [item.to_dict() for item in self.findings],
            "metadata": dict(self.metadata_values),
        }

    def metadata(self) -> dict[str, str]:
        return {
            **self.metadata_values,
            "tool_foundation_audit_report_id": self.report_id,
            "tool_foundation_audit_owner_unit": self.owner_unit,
            "tool_foundation_audit_status": str(self.status),
            "tool_foundation_audit_ok": str(self.ok).lower(),
            "tool_foundation_audit_blockers": ",".join(self.blocker_codes),
            "tool_foundation_audit_findings": str(len(self.findings)),
            "tool_foundation_audit_receipts": str(self.receipt_coverage.receipt_count),
            "tool_foundation_audit_context_modifiers": str(self.context_coverage.modifier_count),
            "tool_foundation_audit_source_repos": ",".join(self.source_coverage.source_repos),
        }


class ToolFoundationAuditRuntime:
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
        materialization: Mapping[str, Any] | None,
        context_snapshots: Sequence[Mapping[str, Any]],
        receipt_snapshots: Sequence[Mapping[str, Any]],
        event_records: Sequence[EventRecord],
        expected_tool_calls: int,
        disabled_component: str = "",
    ) -> ToolFoundationAuditReport:
        materialization_data = dict(materialization or {})
        source_coverage = _source_coverage(materialization_data)
        reachability = _reachability(event_records)
        receipt_coverage = _receipt_coverage(receipt_snapshots)
        context_coverage = _context_coverage(context_snapshots)
        findings = [
            *self._registry_findings(materialization_data, expected_tool_calls),
            *self._source_findings(source_coverage),
            *self._reachability_findings(reachability, expected_tool_calls, disabled_component),
            *self._receipt_findings(receipt_coverage, expected_tool_calls),
            *self._context_findings(context_coverage, expected_tool_calls),
        ]
        status = _status_from_findings(findings)
        report_id = _report_id(materialization_data, expected_tool_calls)
        metadata = {
            "tool_foundation_expected_tool_calls": str(expected_tool_calls),
            "tool_foundation_disabled_component": disabled_component,
            "tool_foundation_reachability_default_path": str(reachability.default_path_touched).lower(),
            "tool_foundation_source_required_rows": str(source_coverage.required_rows),
            "tool_foundation_source_vendor_like_required": str(source_coverage.required_vendor_like_rows),
        }
        return ToolFoundationAuditReport(
            report_id=report_id,
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            status=status,
            created_at=now_iso(),
            source_coverage=source_coverage,
            reachability=reachability,
            receipt_coverage=receipt_coverage,
            context_coverage=context_coverage,
            findings=tuple(findings),
            metadata_values=metadata,
        )

    def event_for_report(
        self,
        report: ToolFoundationAuditReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": session_id,
                    "worker_request_id": worker_request_id,
                    "phase": "tool_foundation_audit",
                    "report": report.to_dict(),
                }
            },
        )

    def _registry_findings(self, materialization: Mapping[str, Any], expected_tool_calls: int) -> list[ToolFoundationAuditFinding]:
        findings: list[ToolFoundationAuditFinding] = []
        active = materialization.get("active_tool_names")
        if expected_tool_calls > 0 and not active:
            findings.append(
                _finding(
                    "TOOL_REGISTRY_NOT_MATERIALIZED",
                    ToolFoundationAuditSeverity.BLOCKER,
                    ToolFoundationAuditSurface.REGISTRY,
                    "Tool calls were expected but no active materialized tool registry was present.",
                    blocking=True,
                    evidence=[("expected_tool_calls", expected_tool_calls), ("active_tool_names", active)],
                )
            )
        owner = str(materialization.get("owner_unit") or "")
        if materialization and owner != self.owner_unit:
            findings.append(
                _finding(
                    "TOOL_REGISTRY_OWNER_MISMATCH",
                    ToolFoundationAuditSeverity.BLOCKER,
                    ToolFoundationAuditSurface.REGISTRY,
                    "Tool registry materialization is not owned by the current M1-02C foundation unit.",
                    blocking=True,
                    evidence=[("expected_owner", self.owner_unit), ("actual_owner", owner)],
                )
            )
        return findings

    def _source_findings(self, coverage: ToolFoundationSourceCoverage) -> list[ToolFoundationAuditFinding]:
        findings: list[ToolFoundationAuditFinding] = []
        required_repos = {"claude-code-best", "opencode", "hermes-agent"}
        missing = sorted(required_repos.difference(coverage.source_repos))
        if missing:
            findings.append(
                _finding(
                    "TOOL_SOURCE_LEDGER_MISSING_REPOS",
                    ToolFoundationAuditSeverity.ERROR,
                    ToolFoundationAuditSurface.SOURCE_LEDGER,
                    "Tool foundation source ledger does not include all required source repositories.",
                    blocking=True,
                    evidence=[("missing_repos", missing), ("source_repos", coverage.source_repos)],
                )
            )
        if coverage.required_vendor_like_rows:
            findings.append(
                _finding(
                    "TOOL_SOURCE_LEDGER_VENDOR_LIKE_REQUIRED",
                    ToolFoundationAuditSeverity.BLOCKER,
                    ToolFoundationAuditSurface.SOURCE_LEDGER,
                    "A required source ledger row is still marked as vendor/reference/deferred instead of a Zyra-owned migration or active port.",
                    blocking=True,
                    evidence=[("required_vendor_like_rows", coverage.required_vendor_like_rows)],
                )
            )
        return findings

    def _reachability_findings(
        self,
        reachability: ToolFoundationReachability,
        expected_tool_calls: int,
        disabled_component: str,
    ) -> list[ToolFoundationAuditFinding]:
        findings: list[ToolFoundationAuditFinding] = []
        if disabled_component:
            if reachability.disabled_events == 0:
                findings.append(
                    _finding(
                        "TOOL_FOUNDATION_DISABLE_NOT_OBSERVED",
                        ToolFoundationAuditSeverity.BLOCKER,
                        ToolFoundationAuditSurface.DISCONNECT,
                        "A disabled foundation component did not produce a tool_loop_foundation_disabled event.",
                        blocking=True,
                        evidence=[("disabled_component", disabled_component)],
                    )
                )
            return findings
        if expected_tool_calls > 0 and reachability.registry_materialized_events == 0:
            findings.append(
                _finding(
                    "TOOL_REGISTRY_EVENT_MISSING",
                    ToolFoundationAuditSeverity.BLOCKER,
                    ToolFoundationAuditSurface.EVENT_FLOW,
                    "Default worker path executed tool calls without a tool_registry_materialized event.",
                    blocking=True,
                    evidence=[("expected_tool_calls", expected_tool_calls)],
                )
            )
        if expected_tool_calls > 0 and reachability.tool_call_started_events != expected_tool_calls:
            findings.append(
                _finding(
                    "TOOL_CALL_STARTED_COUNT_MISMATCH",
                    ToolFoundationAuditSeverity.ERROR,
                    ToolFoundationAuditSurface.EVENT_FLOW,
                    "Tool start events do not match expected tool calls.",
                    blocking=True,
                    evidence=[
                        ("expected_tool_calls", expected_tool_calls),
                        ("started_events", reachability.tool_call_started_events),
                    ],
                )
            )
        if reachability.tool_call_started_events != reachability.tool_call_completed_events:
            findings.append(
                _finding(
                    "TOOL_CALL_COMPLETION_COUNT_MISMATCH",
                    ToolFoundationAuditSeverity.ERROR,
                    ToolFoundationAuditSurface.EVENT_FLOW,
                    "Tool completion events do not match tool start events.",
                    blocking=True,
                    evidence=[
                        ("started_events", reachability.tool_call_started_events),
                        ("completed_events", reachability.tool_call_completed_events),
                    ],
                )
            )
        if expected_tool_calls > 0 and reachability.context_modifier_events == 0:
            findings.append(
                _finding(
                    "TOOL_CONTEXT_MODIFIER_EVENTS_MISSING",
                    ToolFoundationAuditSeverity.ERROR,
                    ToolFoundationAuditSurface.CONTEXT,
                    "Tool calls completed without observable ToolUseContext modifier events.",
                    blocking=True,
                    evidence=[("expected_tool_calls", expected_tool_calls)],
                )
            )
        return findings

    def _receipt_findings(
        self,
        coverage: ToolFoundationReceiptCoverage,
        expected_tool_calls: int,
    ) -> list[ToolFoundationAuditFinding]:
        findings: list[ToolFoundationAuditFinding] = []
        if expected_tool_calls > 0 and coverage.receipt_count != expected_tool_calls:
            findings.append(
                _finding(
                    "TOOL_EXECUTION_RECEIPT_COUNT_MISMATCH",
                    ToolFoundationAuditSeverity.BLOCKER,
                    ToolFoundationAuditSurface.EXECUTION,
                    "ToolExecutionRuntime receipts do not cover every expected tool call.",
                    blocking=True,
                    evidence=[
                        ("expected_tool_calls", expected_tool_calls),
                        ("receipt_count", coverage.receipt_count),
                    ],
                )
            )
        if not coverage.ok:
            findings.append(
                _finding(
                    "TOOL_EXECUTION_RECEIPT_INCOMPLETE",
                    ToolFoundationAuditSeverity.BLOCKER,
                    ToolFoundationAuditSurface.EXECUTION,
                    "One or more execution receipts lack raw result, bounded result, or budget decision evidence.",
                    blocking=True,
                    evidence=[("receipt_coverage", coverage.to_dict())],
                )
            )
        if expected_tool_calls > 0 and "ToolExecutor" not in coverage.executor_names:
            findings.append(
                _finding(
                    "TOOL_EXECUTOR_NOT_REACHED",
                    ToolFoundationAuditSeverity.BLOCKER,
                    ToolFoundationAuditSurface.EXECUTION,
                    "Execution receipts do not show the Zyra ToolExecutor as a reached runtime component.",
                    blocking=True,
                    evidence=[("executor_names", coverage.executor_names)],
                )
            )
        return findings

    def _context_findings(
        self,
        coverage: ToolFoundationContextCoverage,
        expected_tool_calls: int,
    ) -> list[ToolFoundationAuditFinding]:
        findings: list[ToolFoundationAuditFinding] = []
        if expected_tool_calls > 0 and coverage.turn_count == 0:
            findings.append(
                _finding(
                    "TOOL_USE_CONTEXT_SNAPSHOT_MISSING",
                    ToolFoundationAuditSeverity.BLOCKER,
                    ToolFoundationAuditSurface.CONTEXT,
                    "Tool calls were expected but no ToolUseContext snapshot was retained.",
                    blocking=True,
                    evidence=[("expected_tool_calls", expected_tool_calls)],
                )
            )
        if expected_tool_calls > 0 and coverage.modifier_count < expected_tool_calls:
            findings.append(
                _finding(
                    "TOOL_USE_CONTEXT_MODIFIER_UNDERFLOW",
                    ToolFoundationAuditSeverity.ERROR,
                    ToolFoundationAuditSurface.CONTEXT,
                    "ToolUseContext modifier count is lower than the executed tool call count.",
                    blocking=True,
                    evidence=[
                        ("expected_tool_calls", expected_tool_calls),
                        ("modifier_count", coverage.modifier_count),
                    ],
                )
            )
        return findings


def tool_foundation_audit_metadata(report: ToolFoundationAuditReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_foundation_audit_ok": "false",
            "tool_foundation_audit_status": "missing",
        }
    return report.metadata()


def render_tool_foundation_audit_markdown(report: ToolFoundationAuditReport) -> str:
    findings = report.findings or (
        ToolFoundationAuditFinding(
            code="TOOL_FOUNDATION_AUDIT_CLEAN",
            severity=ToolFoundationAuditSeverity.INFO,
            surface=ToolFoundationAuditSurface.EVENT_FLOW,
            message="Tool foundation audit found no blocking issues.",
        ),
    )
    lines = [
        "## Tool Foundation Audit",
        "",
        f"- owner_unit: `{report.owner_unit}`",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- source_repos: `{','.join(report.source_coverage.source_repos)}`",
        f"- receipts: `{report.receipt_coverage.receipt_count}`",
        f"- context_modifiers: `{report.context_coverage.modifier_count}`",
        f"- default_path_touched: `{str(report.reachability.default_path_touched).lower()}`",
        "",
        "### Findings",
        "",
        *[
            f"- `{finding.code}` [{finding.severity}/{finding.surface}]: {finding.message}"
            for finding in findings
        ],
    ]
    return "\n".join(lines)


def _source_coverage(materialization: Mapping[str, Any]) -> ToolFoundationSourceCoverage:
    rows = materialization.get("source_ledger")
    if not isinstance(rows, list):
        rows = []
    source_repos = sorted({str(row.get("source_repo") or "") for row in rows if isinstance(row, Mapping) and row.get("source_repo")})
    required_rows = 0
    reference_only_rows = 0
    deferred_rows = 0
    required_vendor_like_rows = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decision = str(row.get("decision") or "")
        required = row.get("required_for_default_path") is not False
        if required:
            required_rows += 1
        if decision in {"reference_only", "legacy_vendor_debt"}:
            reference_only_rows += 1
            if required:
                required_vendor_like_rows += 1
        if decision == "deferred":
            deferred_rows += 1
            if required:
                required_vendor_like_rows += 1
    return ToolFoundationSourceCoverage(
        source_repos=tuple(source_repos),
        required_rows=required_rows,
        reference_only_rows=reference_only_rows,
        deferred_rows=deferred_rows,
        required_vendor_like_rows=required_vendor_like_rows,
    )


def _reachability(event_records: Sequence[EventRecord]) -> ToolFoundationReachability:
    phases: list[str] = []
    tool_results = 0
    for event in event_records:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        query = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
        phase = str(query.get("phase") or "")
        if phase:
            phases.append(phase)
        if "tool_result" in payload:
            tool_results += 1
    return ToolFoundationReachability(
        registry_materialized_events=phases.count("tool_registry_materialized"),
        context_modifier_events=phases.count("tool_context_modifier_applied"),
        tool_call_started_events=phases.count("tool_call_started"),
        tool_call_completed_events=phases.count("tool_call_completed"),
        tool_result_events=tool_results,
        disabled_events=phases.count("tool_loop_foundation_disabled"),
    )


def _receipt_coverage(receipts: Sequence[Mapping[str, Any]]) -> ToolFoundationReceiptCoverage:
    raw = 0
    bounded = 0
    budget = 0
    failure = 0
    modifiers = 0
    executors: set[str] = set()
    for receipt in receipts:
        if receipt.get("raw_result") is not None:
            raw += 1
        if receipt.get("bounded_result") is not None:
            bounded += 1
        if receipt.get("budget_decision") is not None:
            budget += 1
        if receipt.get("failure_signal") is not None:
            failure += 1
        context_modifiers = receipt.get("context_modifiers")
        if isinstance(context_modifiers, list):
            modifiers += len(context_modifiers)
        executor_name = str(receipt.get("executor_name") or "")
        if executor_name:
            executors.add(executor_name)
    return ToolFoundationReceiptCoverage(
        receipt_count=len(receipts),
        raw_result_count=raw,
        bounded_result_count=bounded,
        budget_decision_count=budget,
        failure_signal_count=failure,
        modifier_count=modifiers,
        executor_names=tuple(sorted(executors)),
    )


def _context_coverage(snapshots: Sequence[Mapping[str, Any]]) -> ToolFoundationContextCoverage:
    modifiers = 0
    permission = 0
    budget = 0
    artifacts = 0
    read_state = 0
    replacements = 0
    result_chars = 0
    for snapshot in snapshots:
        modifiers += len(snapshot.get("modifier_log") or [])
        permission += len(snapshot.get("permission_handoffs") or [])
        budget += len(snapshot.get("budget_ledger") or [])
        artifacts += len(snapshot.get("artifact_refs") or [])
        read_state += len(snapshot.get("read_file_state") or {})
        replacements += len(snapshot.get("content_replacements") or {})
        try:
            result_chars += int(snapshot.get("tool_result_chars") or 0)
        except (TypeError, ValueError):
            continue
    return ToolFoundationContextCoverage(
        turn_count=len(snapshots),
        modifier_count=modifiers,
        permission_handoffs=permission,
        budget_entries=budget,
        artifact_refs=artifacts,
        read_state_entries=read_state,
        content_replacements=replacements,
        result_chars=result_chars,
    )


def _status_from_findings(findings: Sequence[ToolFoundationAuditFinding]) -> ToolFoundationAuditStatus:
    if any(item.blocking or item.severity == ToolFoundationAuditSeverity.BLOCKER for item in findings):
        return ToolFoundationAuditStatus.FAIL
    if any(item.severity in {ToolFoundationAuditSeverity.WARNING, ToolFoundationAuditSeverity.ERROR} for item in findings):
        return ToolFoundationAuditStatus.WARN
    return ToolFoundationAuditStatus.PASS


def _finding(
    code: str,
    severity: ToolFoundationAuditSeverity,
    surface: ToolFoundationAuditSurface,
    message: str,
    *,
    blocking: bool = False,
    evidence: Sequence[tuple[str, Any]] = (),
) -> ToolFoundationAuditFinding:
    return ToolFoundationAuditFinding(
        code=code,
        severity=severity,
        surface=surface,
        message=message,
        evidence=tuple(ToolFoundationAuditEvidence(key=key, value=value) for key, value in evidence),
        blocking=blocking,
    )


def _report_id(materialization: Mapping[str, Any], expected_tool_calls: int) -> str:
    materialization_id = str(materialization.get("materialization_id") or "no-materialization")
    return f"toolfoundationaudit-{materialization_id}-{expected_tool_calls}"
