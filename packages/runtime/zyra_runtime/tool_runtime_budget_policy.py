from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolBudgetScope(StrEnum):
    TOOL_RESULT = "tool_result"
    TURN = "turn"
    SESSION = "session"


class ToolBudgetPressure(StrEnum):
    OK = "ok"
    WATCH = "watch"
    HIGH = "high"
    EXCEEDED = "exceeded"


class ToolBudgetPolicyAction(StrEnum):
    NONE = "none"
    EXTERNALIZE_OUTPUT = "externalize_output"
    COMPACT_CONTEXT = "compact_context"
    STOP_OR_RESUME = "stop_or_resume"


class ToolBudgetPolicySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ToolBudgetLimit:
    scope: ToolBudgetScope
    max_chars: int
    watch_ratio: float = 0.65
    high_ratio: float = 0.85

    def pressure_for(self, used_chars: int) -> ToolBudgetPressure:
        if self.max_chars <= 0:
            return ToolBudgetPressure.OK
        ratio = used_chars / self.max_chars
        if ratio > 1:
            return ToolBudgetPressure.EXCEEDED
        if ratio >= self.high_ratio:
            return ToolBudgetPressure.HIGH
        if ratio >= self.watch_ratio:
            return ToolBudgetPressure.WATCH
        return ToolBudgetPressure.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": str(self.scope),
            "max_chars": self.max_chars,
            "watch_ratio": self.watch_ratio,
            "high_ratio": self.high_ratio,
        }


@dataclass(frozen=True, slots=True)
class ToolBudgetLedgerEntry:
    entry_id: str
    scope: ToolBudgetScope
    tool_call_id: str
    tool_name: str
    turn_index: int
    step_index: int
    payload_chars: int
    aggregate_chars: int
    limit_chars: int
    pressure: ToolBudgetPressure
    action: ToolBudgetPolicyAction
    externalized: bool = False
    artifact_id: str = ""
    reason: str = ""
    created_at: str = field(default_factory=now_iso)

    @property
    def exceeded(self) -> bool:
        return self.pressure == ToolBudgetPressure.EXCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "scope": str(self.scope),
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "turn_index": self.turn_index,
            "step_index": self.step_index,
            "payload_chars": self.payload_chars,
            "aggregate_chars": self.aggregate_chars,
            "limit_chars": self.limit_chars,
            "pressure": str(self.pressure),
            "action": str(self.action),
            "externalized": self.externalized,
            "artifact_id": self.artifact_id,
            "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolBudgetPolicyFinding:
    code: str
    severity: ToolBudgetPolicySeverity
    message: str
    scope: ToolBudgetScope
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolBudgetPolicySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "scope": str(self.scope),
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolBudgetPolicyReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    limits: tuple[ToolBudgetLimit, ...]
    ledger: tuple[ToolBudgetLedgerEntry, ...]
    findings: tuple[ToolBudgetPolicyFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def total_payload_chars(self) -> int:
        return sum(entry.payload_chars for entry in self.ledger if entry.scope == ToolBudgetScope.TOOL_RESULT)

    @property
    def externalized_count(self) -> int:
        return sum(1 for entry in self.ledger if entry.externalized)

    @property
    def highest_pressure(self) -> ToolBudgetPressure:
        ordered = {
            ToolBudgetPressure.OK: 0,
            ToolBudgetPressure.WATCH: 1,
            ToolBudgetPressure.HIGH: 2,
            ToolBudgetPressure.EXCEEDED: 3,
        }
        pressure = ToolBudgetPressure.OK
        for entry in self.ledger:
            if ordered[entry.pressure] > ordered[pressure]:
                pressure = entry.pressure
        return pressure

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "limits": [limit.to_dict() for limit in self.limits],
            "ledger": [entry.to_dict() for entry in self.ledger],
            "findings": [finding.to_dict() for finding in self.findings],
            "total_payload_chars": self.total_payload_chars,
            "externalized_count": self.externalized_count,
            "highest_pressure": str(self.highest_pressure),
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_budget_policy_report_id": self.report_id,
            "tool_budget_policy_owner_unit": self.owner_unit,
            "tool_budget_policy_ok": str(self.ok).lower(),
            "tool_budget_policy_entries": str(len(self.ledger)),
            "tool_budget_policy_findings": str(len(self.findings)),
            "tool_budget_policy_externalized": str(self.externalized_count),
            "tool_budget_policy_total_payload_chars": str(self.total_payload_chars),
            "tool_budget_policy_highest_pressure": str(self.highest_pressure),
        }


class ToolBudgetPolicyRuntime:
    def __init__(
        self,
        *,
        tool_result_limit: int,
        turn_limit: int | None = None,
        session_limit: int | None = None,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.tool_result_limit = max(1, int(tool_result_limit))
        self.turn_limit = int(turn_limit) if turn_limit is not None and int(turn_limit) > 0 else None
        self.session_limit = int(session_limit) if session_limit is not None and int(session_limit) > 0 else None

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        receipts: Sequence[Mapping[str, Any]],
        context_snapshots: Sequence[Mapping[str, Any]],
    ) -> ToolBudgetPolicyReport:
        limits = self._limits()
        ledger = self._ledger(receipts, context_snapshots)
        findings = self._findings(ledger)
        return ToolBudgetPolicyReport(
            report_id=new_id("toolbudgetpolicy"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            limits=tuple(limits),
            ledger=tuple(ledger),
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: ToolBudgetPolicyReport,
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
                    "phase": "tool_budget_policy",
                    "budget_policy": report.to_dict(),
                }
            },
        )

    def _limits(self) -> list[ToolBudgetLimit]:
        limits = [ToolBudgetLimit(ToolBudgetScope.TOOL_RESULT, self.tool_result_limit)]
        if self.turn_limit is not None:
            limits.append(ToolBudgetLimit(ToolBudgetScope.TURN, self.turn_limit))
        if self.session_limit is not None:
            limits.append(ToolBudgetLimit(ToolBudgetScope.SESSION, self.session_limit))
        return limits

    def _ledger(
        self,
        receipts: Sequence[Mapping[str, Any]],
        context_snapshots: Sequence[Mapping[str, Any]],
    ) -> list[ToolBudgetLedgerEntry]:
        ledger: list[ToolBudgetLedgerEntry] = []
        session_chars = 0
        turn_chars: dict[int, int] = {}
        for receipt in receipts:
            request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
            budget = receipt.get("budget_receipt") if isinstance(receipt.get("budget_receipt"), Mapping) else {}
            decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
            if not budget and ("payload_chars" in receipt or "decision" in receipt):
                budget = receipt
            if not decision and isinstance(budget.get("decision"), Mapping):
                decision = budget["decision"]
            payload_chars = _safe_int(budget.get("payload_chars"))
            if payload_chars <= 0:
                payload_chars = _safe_int(decision.get("original_chars"))
            turn_index = _safe_int(request.get("turn_index"))
            step_index = _safe_int(request.get("step_index"))
            session_chars += payload_chars
            turn_chars[turn_index] = turn_chars.get(turn_index, 0) + payload_chars
            tool_call_id = str(request.get("tool_call_id") or "")
            tool_name = str(request.get("tool_name") or "")
            externalized = decision.get("applied") is True or budget.get("externalized") is True
            artifact_id = str(decision.get("artifact_id") or "")
            ledger.append(
                self._entry(
                    scope=ToolBudgetScope.TOOL_RESULT,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    turn_index=turn_index,
                    step_index=step_index,
                    payload_chars=payload_chars,
                    aggregate_chars=payload_chars,
                    limit_chars=self.tool_result_limit,
                    externalized=externalized,
                    artifact_id=artifact_id,
                    reason=str(decision.get("reason") or ""),
                )
            )
            if self.turn_limit is not None:
                ledger.append(
                    self._entry(
                        scope=ToolBudgetScope.TURN,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        turn_index=turn_index,
                        step_index=step_index,
                        payload_chars=payload_chars,
                        aggregate_chars=turn_chars[turn_index],
                        limit_chars=self.turn_limit,
                        externalized=externalized,
                        artifact_id=artifact_id,
                        reason="turn_aggregate_budget",
                    )
                )
            if self.session_limit is not None:
                ledger.append(
                    self._entry(
                        scope=ToolBudgetScope.SESSION,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        turn_index=turn_index,
                        step_index=step_index,
                        payload_chars=payload_chars,
                        aggregate_chars=session_chars,
                        limit_chars=self.session_limit,
                        externalized=externalized,
                        artifact_id=artifact_id,
                        reason="session_aggregate_budget",
                    )
                )
        if context_snapshots and not receipts:
            for snapshot in context_snapshots:
                result_chars = _safe_int(snapshot.get("tool_result_chars"))
                if result_chars <= 0:
                    continue
                ledger.append(
                    self._entry(
                        scope=ToolBudgetScope.SESSION,
                        tool_call_id="",
                        tool_name="context_snapshot",
                        turn_index=_safe_int(snapshot.get("turn_index")),
                        step_index=0,
                        payload_chars=result_chars,
                        aggregate_chars=result_chars,
                        limit_chars=self.session_limit or max(result_chars, 1),
                        externalized=False,
                        artifact_id="",
                        reason="context_snapshot_only",
                    )
                )
        return ledger

    def _entry(
        self,
        *,
        scope: ToolBudgetScope,
        tool_call_id: str,
        tool_name: str,
        turn_index: int,
        step_index: int,
        payload_chars: int,
        aggregate_chars: int,
        limit_chars: int,
        externalized: bool,
        artifact_id: str,
        reason: str,
    ) -> ToolBudgetLedgerEntry:
        limit = ToolBudgetLimit(scope, max(1, limit_chars))
        pressure = limit.pressure_for(aggregate_chars)
        action = _action_for_pressure(scope, pressure, externalized)
        return ToolBudgetLedgerEntry(
            entry_id=new_id("toolbudget"),
            scope=scope,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            turn_index=turn_index,
            step_index=step_index,
            payload_chars=payload_chars,
            aggregate_chars=aggregate_chars,
            limit_chars=limit_chars,
            pressure=pressure,
            action=action,
            externalized=externalized,
            artifact_id=artifact_id,
            reason=reason,
        )

    def _findings(self, ledger: Sequence[ToolBudgetLedgerEntry]) -> list[ToolBudgetPolicyFinding]:
        findings: list[ToolBudgetPolicyFinding] = []
        for entry in ledger:
            if entry.pressure == ToolBudgetPressure.EXCEEDED and entry.scope == ToolBudgetScope.TOOL_RESULT and not entry.externalized:
                findings.append(
                    _finding(
                        "TOOL_RESULT_BUDGET_EXCEEDED_WITHOUT_EXTERNALIZATION",
                        ToolBudgetPolicySeverity.BLOCKER,
                        "A tool result exceeded its inline budget but was not externalized.",
                        entry.scope,
                        tool_call_id=entry.tool_call_id,
                        tool_name=entry.tool_name,
                        aggregate_chars=str(entry.aggregate_chars),
                        limit_chars=str(entry.limit_chars),
                    )
                )
            elif entry.pressure == ToolBudgetPressure.EXCEEDED:
                findings.append(
                    _finding(
                        "TOOL_BUDGET_SCOPE_EXCEEDED",
                        ToolBudgetPolicySeverity.WARNING,
                        "A tool budget scope was exceeded and should trigger compact or resume planning.",
                        entry.scope,
                        tool_call_id=entry.tool_call_id,
                        tool_name=entry.tool_name,
                        aggregate_chars=str(entry.aggregate_chars),
                        limit_chars=str(entry.limit_chars),
                    )
                )
            elif entry.pressure == ToolBudgetPressure.HIGH:
                findings.append(
                    _finding(
                        "TOOL_BUDGET_PRESSURE_HIGH",
                        ToolBudgetPolicySeverity.WARNING,
                        "A tool budget scope is under high pressure.",
                        entry.scope,
                        tool_call_id=entry.tool_call_id,
                        tool_name=entry.tool_name,
                        aggregate_chars=str(entry.aggregate_chars),
                        limit_chars=str(entry.limit_chars),
                    )
                )
        return findings


def tool_budget_policy_metadata(report: ToolBudgetPolicyReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_budget_policy_entries": "0",
            "tool_budget_policy_ok": "true",
        }
    return report.metadata()


def render_tool_budget_policy_markdown(report: ToolBudgetPolicyReport) -> str:
    finding_lines = [
        f"- `{finding.code}` [{finding.severity}/{finding.scope}]: {finding.message}"
        for finding in report.findings
    ]
    return "\n".join(
        [
            "## Tool Budget Policy",
            "",
            f"- owner_unit: `{report.owner_unit}`",
            f"- ok: `{str(report.ok).lower()}`",
            f"- highest_pressure: `{report.highest_pressure}`",
            f"- entries: `{len(report.ledger)}`",
            f"- externalized: `{report.externalized_count}`",
            "",
            "### Findings",
            "",
            *(finding_lines or ["- no findings"]),
        ]
    )


def _action_for_pressure(
    scope: ToolBudgetScope,
    pressure: ToolBudgetPressure,
    externalized: bool,
) -> ToolBudgetPolicyAction:
    if pressure == ToolBudgetPressure.OK:
        return ToolBudgetPolicyAction.NONE
    if scope == ToolBudgetScope.TOOL_RESULT and not externalized:
        return ToolBudgetPolicyAction.EXTERNALIZE_OUTPUT
    if scope == ToolBudgetScope.TURN:
        return ToolBudgetPolicyAction.COMPACT_CONTEXT
    if scope == ToolBudgetScope.SESSION:
        return ToolBudgetPolicyAction.STOP_OR_RESUME if pressure == ToolBudgetPressure.EXCEEDED else ToolBudgetPolicyAction.COMPACT_CONTEXT
    return ToolBudgetPolicyAction.NONE


def _finding(
    code: str,
    severity: ToolBudgetPolicySeverity,
    message: str,
    scope: ToolBudgetScope,
    **metadata: str,
) -> ToolBudgetPolicyFinding:
    return ToolBudgetPolicyFinding(
        code=code,
        severity=severity,
        message=message,
        scope=scope,
        metadata={str(key): str(value) for key, value in metadata.items()},
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
