from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .tool_runtime_budget_chain import ToolBudgetChainReport
from .tool_runtime_execution_timeline import ToolExecutionTimelineReport
from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_permission_checkpoint import ToolPermissionCheckpointReport
from .tool_runtime_result_replay_index import ToolResultReplayIndexReport
from .tool_runtime_semantic_effects import ToolSemanticEffectReport
from .tool_runtime_session_bridge import ToolSessionBridgeReport


class ToolSourceEffectKind(StrEnum):
    TOOL_REGISTRY = "tool_registry"
    TOOL_ORCHESTRATION = "tool_orchestration"
    TOOL_EXECUTION = "tool_execution"
    TOOL_RESULT_BUDGET = "tool_result_budget"
    TOOL_RESULT_CONTEXT = "tool_result_context"
    PERMISSION_RUNTIME = "permission_runtime"
    SESSION_BRIDGE = "session_bridge"
    REPLAY_AND_RESTORE = "replay_and_restore"
    SUPPORTING_REFERENCE = "supporting_reference"


class ToolSourceEffectStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolSourceEffectSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolSourceEffectSurface(StrEnum):
    SOURCE_LEDGER = "source_ledger"
    RUNTIME_REPORT = "runtime_report"
    EVENT_LOG = "event_log"
    RECEIPT = "receipt"


@dataclass(frozen=True, slots=True)
class ToolSourceEffectEvidence:
    evidence_id: str
    kind: ToolSourceEffectKind
    source_repo: str
    source_path: str
    target_path: str
    capability: str
    decision: str
    required_for_default_path: bool
    effective_code: bool
    live_effect: bool
    report_id: str = ""
    report_status: str = ""
    event_phase: str = ""
    tool_call_count: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def required_live_effect(self) -> bool:
        return self.required_for_default_path or self.effective_code

    @property
    def satisfied(self) -> bool:
        return self.live_effect or not self.required_live_effect

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": str(self.kind),
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "capability": self.capability,
            "decision": self.decision,
            "required_for_default_path": self.required_for_default_path,
            "effective_code": self.effective_code,
            "required_live_effect": self.required_live_effect,
            "live_effect": self.live_effect,
            "satisfied": self.satisfied,
            "report_id": self.report_id,
            "report_status": self.report_status,
            "event_phase": self.event_phase,
            "tool_call_count": self.tool_call_count,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolSourceEffectFinding:
    code: str
    severity: ToolSourceEffectSeverity
    surface: ToolSourceEffectSurface
    message: str
    source_path: str = ""
    target_path: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolSourceEffectSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolSourceEffectReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    evidence: tuple[ToolSourceEffectEvidence, ...]
    findings: tuple[ToolSourceEffectFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolSourceEffectStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolSourceEffectStatus.BLOCKED
        if not self.evidence:
            return ToolSourceEffectStatus.EMPTY
        if self.findings:
            return ToolSourceEffectStatus.PARTIAL
        return ToolSourceEffectStatus.READY

    @property
    def live_effect_count(self) -> int:
        return sum(1 for item in self.evidence if item.live_effect)

    @property
    def required_count(self) -> int:
        return sum(1 for item in self.evidence if item.required_live_effect)

    @property
    def satisfied_required_count(self) -> int:
        return sum(1 for item in self.evidence if item.required_live_effect and item.satisfied)

    @property
    def source_repos(self) -> tuple[str, ...]:
        return tuple(sorted({item.source_repo for item in self.evidence if item.source_repo}))

    @property
    def active_kinds(self) -> tuple[str, ...]:
        return tuple(sorted({str(item.kind) for item in self.evidence if item.live_effect}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_source_effects.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "evidence_count": len(self.evidence),
            "required_count": self.required_count,
            "satisfied_required_count": self.satisfied_required_count,
            "live_effect_count": self.live_effect_count,
            "source_repos": list(self.source_repos),
            "active_kinds": list(self.active_kinds),
            "findings": [finding.to_dict() for finding in self.findings],
            "evidence": [item.to_dict() for item in self.evidence],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_source_effects_report_id": self.report_id,
            "tool_source_effects_owner_unit": self.owner_unit,
            "tool_source_effects_runtime_id": self.runtime_id,
            "tool_source_effects_ok": str(self.ok).lower(),
            "tool_source_effects_status": str(self.status),
            "tool_source_effects_evidence": str(len(self.evidence)),
            "tool_source_effects_required": str(self.required_count),
            "tool_source_effects_required_satisfied": str(self.satisfied_required_count),
            "tool_source_effects_live": str(self.live_effect_count),
            "tool_source_effects_repos": ",".join(self.source_repos),
            "tool_source_effects_kinds": ",".join(self.active_kinds),
            "tool_source_effects_findings": str(len(self.findings)),
        }


class ToolSourceEffectRuntime:
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
        materialization: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
        session_bridge_report: ToolSessionBridgeReport | None,
        timeline_report: ToolExecutionTimelineReport | None,
        budget_chain_report: ToolBudgetChainReport | None,
        permission_checkpoint_report: ToolPermissionCheckpointReport | None,
        result_replay_index_report: ToolResultReplayIndexReport | None,
        semantic_effect_report: ToolSemanticEffectReport | None,
    ) -> ToolSourceEffectReport:
        rows = materialization.get("source_ledger") if isinstance(materialization.get("source_ledger"), Sequence) else ()
        runtime_state = _RuntimeEffectState(
            receipt_count=sum(1 for receipt in receipts if isinstance(receipt, Mapping)),
            session_bridge_report=session_bridge_report,
            timeline_report=timeline_report,
            budget_chain_report=budget_chain_report,
            permission_checkpoint_report=permission_checkpoint_report,
            result_replay_index_report=result_replay_index_report,
            semantic_effect_report=semantic_effect_report,
        )
        evidence = tuple(self._evidence_for_row(row, runtime_state) for row in rows if isinstance(row, Mapping))
        findings = tuple(self._findings(evidence, runtime_state))
        return ToolSourceEffectReport(
            report_id=new_id("toolsrceffect"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            evidence=evidence,
            findings=findings,
        )

    def event_for_report(
        self,
        report: ToolSourceEffectReport,
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
                    "phase": "tool_source_effects",
                    "tool_source_effects": report.to_dict(),
                }
            },
        )

    def _evidence_for_row(self, row: Mapping[str, Any], state: "_RuntimeEffectState") -> ToolSourceEffectEvidence:
        source_path = str(row.get("source_path") or "")
        target_path = str(row.get("target_path") or "")
        capability = str(row.get("capability") or "")
        kind = _kind_for_row(source_path=source_path, target_path=target_path, capability=capability)
        report_id, report_status, live_effect, phase, count = state.evidence_for_kind(kind)
        required = _as_bool(row.get("required_for_default_path"))
        effective = _as_bool(row.get("effective_code"))
        return ToolSourceEffectEvidence(
            evidence_id=new_id("toolsrcevidence"),
            kind=kind,
            source_repo=str(row.get("source_repo") or ""),
            source_path=source_path,
            target_path=target_path,
            capability=capability,
            decision=str(row.get("decision") or ""),
            required_for_default_path=required,
            effective_code=effective,
            live_effect=live_effect,
            report_id=report_id,
            report_status=report_status,
            event_phase=phase,
            tool_call_count=count,
            metadata={"upstream_signal": str(row.get("upstream_signal") or "")},
        )

    def _findings(
        self,
        evidence: Sequence[ToolSourceEffectEvidence],
        state: "_RuntimeEffectState",
    ) -> list[ToolSourceEffectFinding]:
        findings: list[ToolSourceEffectFinding] = []
        if state.receipt_count and not evidence:
            findings.append(
                ToolSourceEffectFinding(
                    code="TOOL_SOURCE_EFFECTS_EMPTY_WITH_RECEIPTS",
                    severity=ToolSourceEffectSeverity.BLOCKER,
                    surface=ToolSourceEffectSurface.SOURCE_LEDGER,
                    message="Tool execution receipts exist but no source-to-effect evidence was built.",
                )
            )
        for item in evidence:
            if item.required_live_effect and not item.live_effect:
                findings.append(
                    ToolSourceEffectFinding(
                        code="TOOL_SOURCE_EFFECT_REQUIRED_ROW_HAS_NO_LIVE_EFFECT",
                        severity=ToolSourceEffectSeverity.BLOCKER,
                        surface=ToolSourceEffectSurface.RUNTIME_REPORT,
                        message="A required or effective source ledger row is not backed by a live runtime effect.",
                        source_path=item.source_path,
                        target_path=item.target_path,
                        metadata={"kind": str(item.kind), "decision": item.decision},
                    )
                )
            elif not item.required_live_effect and not item.live_effect and _important_reference(item):
                findings.append(
                    ToolSourceEffectFinding(
                        code="TOOL_SOURCE_EFFECT_REFERENCE_NOT_REACHED",
                        severity=ToolSourceEffectSeverity.WARNING,
                        surface=ToolSourceEffectSurface.SOURCE_LEDGER,
                        message="A non-required source row was present but not reached by this run.",
                        source_path=item.source_path,
                        target_path=item.target_path,
                        metadata={"kind": str(item.kind), "decision": item.decision},
                    )
                )
        if state.receipt_count and state.timeline_report is not None and not state.timeline_report.ok:
            findings.append(
                ToolSourceEffectFinding(
                    code="TOOL_SOURCE_EFFECT_TIMELINE_NOT_READY",
                    severity=ToolSourceEffectSeverity.BLOCKER,
                    surface=ToolSourceEffectSurface.EVENT_LOG,
                    message="Timeline report is not ready, so execution source effects cannot be trusted.",
                    metadata={"timeline_status": str(state.timeline_report.status)},
                )
            )
        return findings


@dataclass(frozen=True, slots=True)
class _RuntimeEffectState:
    receipt_count: int
    session_bridge_report: ToolSessionBridgeReport | None
    timeline_report: ToolExecutionTimelineReport | None
    budget_chain_report: ToolBudgetChainReport | None
    permission_checkpoint_report: ToolPermissionCheckpointReport | None
    result_replay_index_report: ToolResultReplayIndexReport | None
    semantic_effect_report: ToolSemanticEffectReport | None

    def evidence_for_kind(self, kind: ToolSourceEffectKind) -> tuple[str, str, bool, str, int]:
        if kind == ToolSourceEffectKind.SESSION_BRIDGE:
            report = self.session_bridge_report
            return _report_tuple(report, "tool_session_bridge_attached", getattr(report, "valid_tool_use_count", 0) if report else 0)
        if kind in {ToolSourceEffectKind.TOOL_ORCHESTRATION, ToolSourceEffectKind.TOOL_EXECUTION, ToolSourceEffectKind.TOOL_REGISTRY}:
            report = self.timeline_report
            live = bool(report and report.ok and report.tool_call_count >= min(self.receipt_count, max(self.receipt_count, 1)))
            return (
                getattr(report, "report_id", "") if report else "",
                str(getattr(report, "status", "")) if report else "",
                live,
                "tool_execution_timeline",
                getattr(report, "tool_call_count", 0) if report else 0,
            )
        if kind in {ToolSourceEffectKind.TOOL_RESULT_BUDGET, ToolSourceEffectKind.TOOL_RESULT_CONTEXT}:
            report = self.budget_chain_report
            live = bool(report and report.ok and (report.tool_call_count or not self.receipt_count))
            return (
                getattr(report, "report_id", "") if report else "",
                str(getattr(report, "status", "")) if report else "",
                live,
                "tool_budget_chain",
                getattr(report, "tool_call_count", 0) if report else 0,
            )
        if kind == ToolSourceEffectKind.PERMISSION_RUNTIME:
            report = self.permission_checkpoint_report
            live = bool(report and report.ok)
            return (
                getattr(report, "report_id", "") if report else "",
                str(getattr(report, "status", "")) if report else "",
                live,
                "tool_permission_checkpoint",
                getattr(report, "pending_count", 0) if report else 0,
            )
        if kind == ToolSourceEffectKind.REPLAY_AND_RESTORE:
            report = self.result_replay_index_report
            live = bool(report and report.ok and (report.tool_call_count or not self.receipt_count))
            return (
                getattr(report, "report_id", "") if report else "",
                str(getattr(report, "status", "")) if report else "",
                live,
                "tool_result_replay_index",
                getattr(report, "tool_call_count", 0) if report else 0,
            )
        report = self.semantic_effect_report
        live = bool(report and report.ok)
        return (
            getattr(report, "report_id", "") if report else "",
            str(getattr(report, "status", "")) if report else "",
            live,
            "tool_semantic_effects",
            getattr(report, "applicable_count", 0) if report else 0,
        )


def tool_source_effects_metadata(report: ToolSourceEffectReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_source_effects_ok": "false", "tool_source_effects_evidence": "0"}
    return report.metadata()


def assert_tool_source_effects_ready(report: ToolSourceEffectReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.findings if finding.blocking)
    raise AssertionError(f"tool source effects blocked: {blockers or 'unknown'}")


def render_tool_source_effects_markdown(report: ToolSourceEffectReport) -> str:
    lines = [
        "## Tool Source Effects",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- evidence: `{len(report.evidence)}`",
        f"- required: `{report.required_count}`",
        f"- live: `{report.live_effect_count}`",
        f"- repos: `{', '.join(report.source_repos)}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)


def _kind_for_row(*, source_path: str, target_path: str, capability: str) -> ToolSourceEffectKind:
    text = f"{source_path} {target_path} {capability}".lower()
    if "session" in text and "tool" in text:
        return ToolSourceEffectKind.SESSION_BRIDGE
    if "toolorchestration" in text or "scheduler" in text or "batch" in text:
        return ToolSourceEffectKind.TOOL_ORCHESTRATION
    if "toolexecution" in text or "executor" in text or "shellcommand" in text:
        return ToolSourceEffectKind.TOOL_EXECUTION
    if "toolresultstorage" in text or "budget" in text or "output_store" in text:
        return ToolSourceEffectKind.TOOL_RESULT_BUDGET
    if "permission" in text or "approval" in text or "handoff" in text:
        return ToolSourceEffectKind.PERMISSION_RUNTIME
    if "result_context" in text or "continuation" in text:
        return ToolSourceEffectKind.TOOL_RESULT_CONTEXT
    if "replay" in text or "restore" in text or "checkpoint" in text:
        return ToolSourceEffectKind.REPLAY_AND_RESTORE
    if "registry" in text or "tools" in text:
        return ToolSourceEffectKind.TOOL_REGISTRY
    return ToolSourceEffectKind.SUPPORTING_REFERENCE


def _report_tuple(report: Any, phase: str, count: int) -> tuple[str, str, bool, str, int]:
    return (
        getattr(report, "report_id", "") if report else "",
        str(getattr(report, "status", "")) if report else "",
        bool(report and getattr(report, "ok", False)),
        phase,
        count,
    )


def _important_reference(item: ToolSourceEffectEvidence) -> bool:
    return item.decision not in {"deferred", "reference_only", "contract_only"} and item.kind != ToolSourceEffectKind.SUPPORTING_REFERENCE


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}
