from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolFailureRoute(StrEnum):
    NONE = "none"
    REPAIR_TOOL_ARGUMENTS = "repair_tool_arguments"
    PERMISSION_RUNTIME = "permission_runtime"
    ARTIFACT_EXTERNALIZED = "artifact_externalized"
    RETRY_OR_BACKGROUND = "retry_or_background"
    RECOVERY_PLANNER = "recovery_planner"
    STOP_SESSION = "stop_session"


class ToolFailurePolicyStatus(StrEnum):
    CLEAN = "clean"
    ROUTED = "routed"
    BLOCKED = "blocked"


class ToolFailurePolicySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ToolFailurePolicyFinding:
    code: str
    severity: ToolFailurePolicySeverity
    message: str
    tool_call_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolFailurePolicySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "blocking": self.blocking,
            "message": self.message,
            "tool_call_id": self.tool_call_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolFailurePolicyDecision:
    decision_id: str
    tool_call_id: str
    tool_name: str
    error: str
    signal_kind: str
    route: ToolFailureRoute
    retryable: bool
    fatal: bool
    budget_related: bool
    permission_related: bool
    schema_related: bool
    summary: str
    source_path: str = "packages/runtime/zyra_runtime/tool_runtime_failure_policy.py"
    upstream_signal: str = "tool failure signal watchdog recovery route"
    created_at: str = field(default_factory=now_iso)

    @property
    def routed(self) -> bool:
        return self.route != ToolFailureRoute.NONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "error": self.error,
            "signal_kind": self.signal_kind,
            "route": str(self.route),
            "retryable": self.retryable,
            "fatal": self.fatal,
            "budget_related": self.budget_related,
            "permission_related": self.permission_related,
            "schema_related": self.schema_related,
            "summary": self.summary,
            "routed": self.routed,
            "source_path": self.source_path,
            "upstream_signal": self.upstream_signal,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolFailurePolicyReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    decisions: tuple[ToolFailurePolicyDecision, ...]
    findings: tuple[ToolFailurePolicyFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolFailurePolicyStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolFailurePolicyStatus.BLOCKED
        if any(decision.routed for decision in self.decisions):
            return ToolFailurePolicyStatus.ROUTED
        return ToolFailurePolicyStatus.CLEAN

    @property
    def decision_count(self) -> int:
        return len(self.decisions)

    @property
    def routed_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.routed)

    @property
    def retryable_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.retryable)

    @property
    def fatal_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.fatal)

    @property
    def permission_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.permission_related)

    @property
    def budget_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.budget_related)

    @property
    def schema_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.schema_related)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "decision_count": self.decision_count,
            "routed_count": self.routed_count,
            "retryable_count": self.retryable_count,
            "fatal_count": self.fatal_count,
            "permission_count": self.permission_count,
            "budget_count": self.budget_count,
            "schema_count": self.schema_count,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_failure_policy_report_id": self.report_id,
            "tool_failure_policy_owner_unit": self.owner_unit,
            "tool_failure_policy_ok": str(self.ok).lower(),
            "tool_failure_policy_status": str(self.status),
            "tool_failure_policy_decisions": str(self.decision_count),
            "tool_failure_policy_routed": str(self.routed_count),
            "tool_failure_policy_retryable": str(self.retryable_count),
            "tool_failure_policy_fatal": str(self.fatal_count),
            "tool_failure_policy_permission": str(self.permission_count),
            "tool_failure_policy_budget": str(self.budget_count),
            "tool_failure_policy_schema": str(self.schema_count),
            "tool_failure_policy_findings": str(len(self.findings)),
        }


class ToolFailurePolicyRuntime:
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
        receipts: Sequence[Mapping[str, Any]],
        failure_signals: Sequence[Mapping[str, Any]],
    ) -> ToolFailurePolicyReport:
        signal_index = _signals_by_tool(failure_signals)
        decisions = tuple(self._decisions(receipts, signal_index))
        findings = tuple(self._findings(decisions, receipts))
        return ToolFailurePolicyReport(
            report_id=new_id("toolfailurepolicy"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            decisions=decisions,
            findings=findings,
        )

    def event_for_report(
        self,
        report: ToolFailurePolicyReport,
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
                    "phase": "tool_failure_policy",
                    "tool_failure_policy": report.to_dict(),
                }
            },
        )

    def _decisions(
        self,
        receipts: Sequence[Mapping[str, Any]],
        signal_index: Mapping[str, Mapping[str, Any]],
    ) -> list[ToolFailurePolicyDecision]:
        decisions: list[ToolFailurePolicyDecision] = []
        for receipt in receipts:
            request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
            result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
            decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
            failure_signal = receipt.get("failure_signal") if isinstance(receipt.get("failure_signal"), Mapping) else None
            tool_call_id = str(request.get("tool_call_id") or result.get("tool_call_id") or "")
            signal = failure_signal or signal_index.get(tool_call_id) or {}
            error = str(result.get("error") or signal.get("kind") or "")
            signal_kind = str(signal.get("kind") or "")
            route = _route_for(error=error, signal=signal, budget_applied=decision.get("applied") is True)
            decisions.append(
                ToolFailurePolicyDecision(
                    decision_id=new_id("toolfailuredecision"),
                    tool_call_id=tool_call_id,
                    tool_name=str(request.get("tool_name") or ""),
                    error=error,
                    signal_kind=signal_kind,
                    route=route,
                    retryable=_retryable(route=route, signal=signal),
                    fatal=route == ToolFailureRoute.STOP_SESSION,
                    budget_related=route == ToolFailureRoute.ARTIFACT_EXTERNALIZED or decision.get("applied") is True,
                    permission_related=route == ToolFailureRoute.PERMISSION_RUNTIME,
                    schema_related=route == ToolFailureRoute.REPAIR_TOOL_ARGUMENTS,
                    summary=str(result.get("summary") or signal.get("message") or ""),
                )
            )
        return decisions

    def _findings(
        self,
        decisions: Sequence[ToolFailurePolicyDecision],
        receipts: Sequence[Mapping[str, Any]],
    ) -> list[ToolFailurePolicyFinding]:
        findings: list[ToolFailurePolicyFinding] = []
        if receipts and not decisions:
            findings.append(
                ToolFailurePolicyFinding(
                    code="TOOL_FAILURE_POLICY_RECEIPTS_WITHOUT_DECISIONS",
                    severity=ToolFailurePolicySeverity.BLOCKER,
                    message="Receipts were present but no failure policy decisions were produced.",
                )
            )
        for decision in decisions:
            if decision.error and decision.route == ToolFailureRoute.NONE:
                findings.append(
                    ToolFailurePolicyFinding(
                        code="TOOL_FAILURE_POLICY_UNROUTED_ERROR",
                        severity=ToolFailurePolicySeverity.BLOCKER,
                        message="A tool error did not map to a recovery or watchdog route.",
                        tool_call_id=decision.tool_call_id,
                        metadata={"error": decision.error},
                    )
                )
            if decision.budget_related and decision.route != ToolFailureRoute.ARTIFACT_EXTERNALIZED:
                findings.append(
                    ToolFailurePolicyFinding(
                        code="TOOL_FAILURE_POLICY_BUDGET_ROUTE_MISMATCH",
                        severity=ToolFailurePolicySeverity.BLOCKER,
                        message="A budget-shaped result did not route to artifact_externalized.",
                        tool_call_id=decision.tool_call_id,
                        metadata={"route": str(decision.route)},
                    )
                )
            if decision.permission_related and not decision.summary:
                findings.append(
                    ToolFailurePolicyFinding(
                        code="TOOL_FAILURE_POLICY_PERMISSION_WITHOUT_SUMMARY",
                        severity=ToolFailurePolicySeverity.WARNING,
                        message="A permission-related decision has no user-facing summary.",
                        tool_call_id=decision.tool_call_id,
                    )
                )
        return findings


def tool_failure_policy_metadata(report: ToolFailurePolicyReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_failure_policy_ok": "true",
            "tool_failure_policy_decisions": "0",
        }
    return report.metadata()


def render_tool_failure_policy_markdown(report: ToolFailurePolicyReport) -> str:
    lines = [
        "## Tool Failure Policy",
        "",
        f"- owner_unit: `{report.owner_unit}`",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- decisions: `{report.decision_count}`",
        f"- routed: `{report.routed_count}`",
        f"- retryable: `{report.retryable_count}`",
        f"- fatal: `{report.fatal_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    lines.extend(["", "### Decisions", ""])
    for decision in report.decisions:
        lines.append(
            f"- `{decision.tool_call_id}` `{decision.tool_name}`: error `{decision.error}`, route `{decision.route}`"
        )
    return "\n".join(lines)


def _signals_by_tool(signals: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for signal in signals:
        tool_call_id = str(signal.get("tool_call_id") or "")
        if tool_call_id:
            indexed[tool_call_id] = signal
    return indexed


def _route_for(
    *,
    error: str,
    signal: Mapping[str, Any],
    budget_applied: bool,
) -> ToolFailureRoute:
    if budget_applied or error == "budget_exceeded" or str(signal.get("kind") or "") == "budget_exceeded":
        return ToolFailureRoute.ARTIFACT_EXTERNALIZED
    if error == "schema_error":
        return ToolFailureRoute.REPAIR_TOOL_ARGUMENTS
    if error in {"permission_required", "permission_denied"}:
        return ToolFailureRoute.PERMISSION_RUNTIME
    if error in {"timeout", "non_zero_exit"}:
        return ToolFailureRoute.RETRY_OR_BACKGROUND
    if error in {"runtime_error", "unknown_tool"}:
        return ToolFailureRoute.RECOVERY_PLANNER
    if error:
        return ToolFailureRoute.STOP_SESSION
    return ToolFailureRoute.NONE


def _retryable(*, route: ToolFailureRoute, signal: Mapping[str, Any]) -> bool:
    if "retryable" in signal:
        return signal.get("retryable") is True
    return route in {
        ToolFailureRoute.REPAIR_TOOL_ARGUMENTS,
        ToolFailureRoute.RETRY_OR_BACKGROUND,
        ToolFailureRoute.RECOVERY_PLANNER,
    }
