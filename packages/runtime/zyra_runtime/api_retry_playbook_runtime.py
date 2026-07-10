from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .model_api_runtime import ApiErrorKind, ApiRetryDecisionKind, ApiRetryReport, ModelStreamReport
from .runtime_budget_state import CODEWORKER_API_FOUNDATION_RUNTIME_ID, M1_02D_OWNER_UNIT, RuntimeBudgetSnapshot


class ApiRetryPlaybookStatus(StrEnum):
    READY = "ready"
    RECOVERED = "recovered"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ApiRetryPlaybookSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ApiRetryPlaybookSurface(StrEnum):
    RULE = "rule"
    DECISION = "decision"
    FALLBACK = "fallback"
    BUDGET = "budget"
    PROVIDER = "provider"
    STREAM_ERROR = "stream_error"


@dataclass(frozen=True, slots=True)
class ApiRetryPlaybookRule:
    rule_id: str
    error_kind: ApiErrorKind
    expected_decision: ApiRetryDecisionKind
    retryable: bool
    fallback_required: bool = False
    budget_mutation_required: bool = False
    max_attempts: int = 3
    route: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "error_kind": str(self.error_kind),
            "expected_decision": str(self.expected_decision),
            "retryable": self.retryable,
            "fallback_required": self.fallback_required,
            "budget_mutation_required": self.budget_mutation_required,
            "max_attempts": self.max_attempts,
            "route": self.route,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ApiRetryPlaybookDecision:
    decision_id: str
    stream_report_id: str
    retry_report_id: str
    error_kind: ApiErrorKind
    expected_decision: ApiRetryDecisionKind
    actual_decision: str
    expected_retryable: bool
    actual_retryable: bool
    fallback_required: bool
    fallback_used: bool
    budget_retry_count: int
    recovered: bool
    stream_succeeded: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        if self.expected_decision == ApiRetryDecisionKind.NO_RETRY:
            return self.actual_decision == str(ApiRetryDecisionKind.NO_RETRY) and self.stream_succeeded
        decision_ok = self.actual_decision == str(self.expected_decision)
        retryable_ok = self.expected_retryable == self.actual_retryable
        fallback_ok = (not self.fallback_required) or self.fallback_used
        budget_ok = self.budget_retry_count > 0 if self.expected_retryable else True
        recovered_ok = self.recovered if self.expected_retryable else True
        return decision_ok and retryable_ok and fallback_ok and budget_ok and recovered_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "stream_report_id": self.stream_report_id,
            "retry_report_id": self.retry_report_id,
            "error_kind": str(self.error_kind),
            "expected_decision": str(self.expected_decision),
            "actual_decision": self.actual_decision,
            "expected_retryable": self.expected_retryable,
            "actual_retryable": self.actual_retryable,
            "fallback_required": self.fallback_required,
            "fallback_used": self.fallback_used,
            "budget_retry_count": self.budget_retry_count,
            "recovered": self.recovered,
            "stream_succeeded": self.stream_succeeded,
            "ok": self.ok,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ApiRetryPlaybookFinding:
    code: str
    severity: ApiRetryPlaybookSeverity
    surface: ApiRetryPlaybookSurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ApiRetryPlaybookSeverity.BLOCKER

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
class ApiRetryPlaybookReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    rules: tuple[ApiRetryPlaybookRule, ...]
    decisions: tuple[ApiRetryPlaybookDecision, ...]
    findings: tuple[ApiRetryPlaybookFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return all(decision.ok for decision in self.decisions) and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ApiRetryPlaybookStatus:
        if not self.ok:
            return ApiRetryPlaybookStatus.BLOCKED
        if any(decision.recovered and decision.error_kind != ApiErrorKind.NONE for decision in self.decisions):
            return ApiRetryPlaybookStatus.RECOVERED
        if self.findings:
            return ApiRetryPlaybookStatus.DEGRADED
        return ApiRetryPlaybookStatus.READY

    @property
    def decision_count(self) -> int:
        return len(self.decisions)

    @property
    def recovered_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.recovered and decision.error_kind != ApiErrorKind.NONE)

    @property
    def fallback_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.fallback_used)

    @property
    def retry_budget_required_count(self) -> int:
        return sum(
            1
            for decision in self.decisions
            if decision.expected_retryable and decision.expected_decision != ApiRetryDecisionKind.NO_RETRY
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.api_retry_playbook.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "rule_count": len(self.rules),
            "decision_count": self.decision_count,
            "recovered_count": self.recovered_count,
            "fallback_count": self.fallback_count,
            "retry_budget_required_count": self.retry_budget_required_count,
            "rules": [rule.to_dict() for rule in self.rules],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "api_retry_playbook_report_id": self.report_id,
            "api_retry_playbook_owner_unit": self.owner_unit,
            "api_retry_playbook_runtime_id": self.runtime_id,
            "api_retry_playbook_ok": str(self.ok).lower(),
            "api_retry_playbook_status": str(self.status),
            "api_retry_playbook_rules": str(len(self.rules)),
            "api_retry_playbook_decisions": str(self.decision_count),
            "api_retry_playbook_recovered": str(self.recovered_count),
            "api_retry_playbook_fallbacks": str(self.fallback_count),
            "api_retry_playbook_retry_budget_required": str(self.retry_budget_required_count),
            "api_retry_playbook_findings": str(len(self.findings)),
        }


class ApiRetryPlaybookRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.rules = default_api_retry_playbook_rules()

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        stream_reports: Sequence[ModelStreamReport],
        retry_reports: Sequence[ApiRetryReport],
        budget_snapshot: RuntimeBudgetSnapshot,
        provider_report: Any = None,
    ) -> ApiRetryPlaybookReport:
        retry_by_stream = _retry_by_stream_report(retry_reports)
        decisions: list[ApiRetryPlaybookDecision] = []
        findings: list[ApiRetryPlaybookFinding] = []
        for stream_report in stream_reports:
            rule = self._rule_for(stream_report.error_kind)
            retry_report = retry_by_stream.get(stream_report.report_id)
            if retry_report is None:
                findings.append(
                    ApiRetryPlaybookFinding(
                        code="API_RETRY_REPORT_MISSING_FOR_STREAM",
                        severity=ApiRetryPlaybookSeverity.BLOCKER,
                        surface=ApiRetryPlaybookSurface.DECISION,
                        message="Every model stream report must have an ApiRetryReport, even when no retry is needed.",
                        metadata={"stream_report_id": stream_report.report_id},
                    )
                )
                continue
            actual_attempt = next(
                (
                    attempt
                    for attempt in retry_report.attempts
                    if attempt.metadata.get("stream_report_id") == stream_report.report_id
                ),
                None,
            )
            actual_decision = str(actual_attempt.decision) if actual_attempt else ""
            actual_retryable = bool(actual_attempt.retryable) if actual_attempt else False
            no_retry_success = bool(
                stream_report.ok
                and stream_report.error_kind == ApiErrorKind.NONE
                and actual_decision == str(ApiRetryDecisionKind.NO_RETRY)
                and not actual_retryable
            )
            decision = ApiRetryPlaybookDecision(
                decision_id=new_id("retry_playbook_decision"),
                stream_report_id=stream_report.report_id,
                retry_report_id=retry_report.report_id,
                error_kind=stream_report.error_kind,
                expected_decision=rule.expected_decision,
                actual_decision=actual_decision,
                expected_retryable=rule.retryable,
                actual_retryable=actual_retryable,
                fallback_required=rule.fallback_required,
                fallback_used=bool(actual_attempt.fallback_selected) if actual_attempt else False,
                budget_retry_count=budget_snapshot.retry_count,
                recovered=retry_report.recovered,
                stream_succeeded=no_retry_success,
                metadata={
                    "route": rule.route,
                    "provider_selected_model": str(getattr(getattr(provider_report, "route", None), "selected_model", "")),
                },
            )
            decisions.append(decision)
            if not decision.ok:
                findings.append(
                    ApiRetryPlaybookFinding(
                        code="API_RETRY_PLAYBOOK_DECISION_MISMATCH",
                        severity=ApiRetryPlaybookSeverity.BLOCKER,
                        surface=ApiRetryPlaybookSurface.DECISION,
                        message="ApiRetryRuntime decision does not match the semantic retry playbook.",
                        metadata=decision.to_dict(),
                    )
                )
        if not stream_reports:
            findings.append(
                ApiRetryPlaybookFinding(
                    code="NO_MODEL_STREAM_REPORTS_FOR_PLAYBOOK",
                    severity=ApiRetryPlaybookSeverity.BLOCKER,
                    surface=ApiRetryPlaybookSurface.STREAM_ERROR,
                    message="ApiRetryPlaybookRuntime requires model stream reports.",
                )
            )
        if budget_snapshot.retry_count < sum(1 for decision in decisions if decision.expected_retryable):
            findings.append(
                ApiRetryPlaybookFinding(
                    code="RETRY_BUDGET_MUTATION_MISSING",
                    severity=ApiRetryPlaybookSeverity.BLOCKER,
                    surface=ApiRetryPlaybookSurface.BUDGET,
                    message="RuntimeBudgetState retry count is lower than expected retryable decisions.",
                    metadata={"budget_retry_count": str(budget_snapshot.retry_count)},
                )
            )
        return ApiRetryPlaybookReport(
            report_id=new_id("api_retry_playbook"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            rules=self.rules,
            decisions=tuple(decisions),
            findings=tuple(findings),
            source_decisions=default_api_retry_playbook_source_decisions(),
        )

    def event_for_report(
        self,
        report: ApiRetryPlaybookReport,
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
                    "phase": "api_retry_playbook",
                    "api_retry_playbook": report.to_dict(),
                }
            },
        )

    def _rule_for(self, error_kind: ApiErrorKind) -> ApiRetryPlaybookRule:
        for rule in self.rules:
            if rule.error_kind == error_kind:
                return rule
        return ApiRetryPlaybookRule(
            rule_id=new_id("retry_rule"),
            error_kind=error_kind,
            expected_decision=ApiRetryDecisionKind.FAIL_FAST,
            retryable=False,
            route="unknown_error_fail_fast",
        )


def default_api_retry_playbook_rules() -> tuple[ApiRetryPlaybookRule, ...]:
    return (
        ApiRetryPlaybookRule(
            rule_id="retry-rule-none",
            error_kind=ApiErrorKind.NONE,
            expected_decision=ApiRetryDecisionKind.NO_RETRY,
            retryable=False,
            route="complete_without_retry",
        ),
        ApiRetryPlaybookRule(
            rule_id="retry-rule-rate-limit",
            error_kind=ApiErrorKind.RATE_LIMIT,
            expected_decision=ApiRetryDecisionKind.RETRY_SAME_MODEL,
            retryable=True,
            budget_mutation_required=True,
            route="retry_after_backoff",
        ),
        ApiRetryPlaybookRule(
            rule_id="retry-rule-model-unavailable",
            error_kind=ApiErrorKind.MODEL_UNAVAILABLE,
            expected_decision=ApiRetryDecisionKind.RETRY_FALLBACK_MODEL,
            retryable=True,
            fallback_required=True,
            budget_mutation_required=True,
            route="fallback_model",
        ),
        ApiRetryPlaybookRule(
            rule_id="retry-rule-stream-stall",
            error_kind=ApiErrorKind.STREAM_STALL,
            expected_decision=ApiRetryDecisionKind.RETRY_SAME_MODEL,
            retryable=True,
            budget_mutation_required=True,
            route="stream_watchdog_retry",
        ),
        ApiRetryPlaybookRule(
            rule_id="retry-rule-timeout",
            error_kind=ApiErrorKind.TIMEOUT,
            expected_decision=ApiRetryDecisionKind.RETRY_SAME_MODEL,
            retryable=True,
            budget_mutation_required=True,
            route="timeout_retry",
        ),
        ApiRetryPlaybookRule(
            rule_id="retry-rule-prompt-too-long",
            error_kind=ApiErrorKind.PROMPT_TOO_LONG,
            expected_decision=ApiRetryDecisionKind.REDUCE_PROMPT_AND_RETRY,
            retryable=True,
            budget_mutation_required=True,
            route="compact_then_retry",
        ),
        ApiRetryPlaybookRule(
            rule_id="retry-rule-auth",
            error_kind=ApiErrorKind.AUTH,
            expected_decision=ApiRetryDecisionKind.FAIL_FAST,
            retryable=False,
            route="credential_block",
        ),
        ApiRetryPlaybookRule(
            rule_id="retry-rule-tool-mismatch",
            error_kind=ApiErrorKind.TOOL_USE_RESULT_MISMATCH,
            expected_decision=ApiRetryDecisionKind.FAIL_FAST,
            retryable=False,
            route="tool_pairing_block",
        ),
    )


def api_retry_playbook_metadata(report: ApiRetryPlaybookReport | None) -> dict[str, str]:
    if report is None:
        return {"api_retry_playbook_ok": "false", "api_retry_playbook_status": "missing", "api_retry_playbook_report_id": ""}
    return report.metadata()


def render_api_retry_playbook_markdown(report: ApiRetryPlaybookReport) -> str:
    lines = [
        "# API Retry Playbook",
        "",
        f"- report_id: {report.report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- decisions: {report.decision_count}",
        f"- recovered: {report.recovered_count}",
        f"- fallbacks: {report.fallback_count}",
        "",
        "## Decisions",
    ]
    for decision in report.decisions:
        lines.append(
            f"- error={decision.error_kind} expected={decision.expected_decision} actual={decision.actual_decision} ok={str(decision.ok).lower()}"
        )
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def default_api_retry_playbook_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/retry-and-errors",
            "target_path": "packages/runtime/zyra_runtime/api_retry_playbook_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "semantic error to retry/fallback playbook validation",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/provider/*",
            "target_path": "packages/runtime/zyra_runtime/api_retry_playbook_runtime.py",
            "decision": "adapter_encapsulated",
            "capability": "provider fallback route validation for retry decisions",
        },
    )


def _retry_by_stream_report(retry_reports: Sequence[ApiRetryReport]) -> dict[str, ApiRetryReport]:
    mapping: dict[str, ApiRetryReport] = {}
    for report in retry_reports:
        for attempt in report.attempts:
            stream_report_id = attempt.metadata.get("stream_report_id", "")
            if stream_report_id:
                mapping[stream_report_id] = report
    return mapping
