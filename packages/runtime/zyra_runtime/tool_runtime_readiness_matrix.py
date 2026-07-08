from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolReadinessComponent(StrEnum):
    REGISTRY = "registry"
    EXECUTION = "execution"
    RESULT_BUDGET = "result_budget"
    PERMISSION_HANDOFF = "permission_handoff"
    SESSION_BRIDGE = "session_bridge"
    RESULT_CONTEXT = "result_context"
    EXECUTION_TIMELINE = "execution_timeline"
    BUDGET_CHAIN = "budget_chain"
    PERMISSION_CHECKPOINT = "permission_checkpoint"
    CONTINUATION_PACKET = "continuation_packet"
    RESULT_REPLAY_INDEX = "result_replay_index"
    SOURCE_EFFECTS = "source_effects"
    SEMANTIC_EFFECTS = "semantic_effects"


class ToolReadinessStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    EMPTY = "empty"


class ToolReadinessSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolReadinessSurface(StrEnum):
    DISABLE_FLAG = "disable_flag"
    REPORT = "report"
    CONTRACT = "contract"
    MAIN_PATH = "main_path"


@dataclass(frozen=True, slots=True)
class ToolReadinessRow:
    row_id: str
    component: ToolReadinessComponent
    required: bool
    disabled: bool
    report_present: bool
    report_ok: bool
    report_status: str = ""
    report_id: str = ""
    event_phase: str = ""
    expected_disconnect_effect: str = ""
    observed_effect: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        if self.disabled:
            return False
        if self.required:
            return self.report_present and self.report_ok
        return not self.report_present or self.report_ok

    @property
    def blocking(self) -> bool:
        return self.required and not self.ready

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "component": str(self.component),
            "required": self.required,
            "disabled": self.disabled,
            "report_present": self.report_present,
            "report_ok": self.report_ok,
            "report_status": self.report_status,
            "report_id": self.report_id,
            "event_phase": self.event_phase,
            "expected_disconnect_effect": self.expected_disconnect_effect,
            "observed_effect": self.observed_effect,
            "ready": self.ready,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolReadinessFinding:
    code: str
    severity: ToolReadinessSeverity
    surface: ToolReadinessSurface
    message: str
    component: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolReadinessSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "component": self.component,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolReadinessMatrixReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    rows: tuple[ToolReadinessRow, ...]
    findings: tuple[ToolReadinessFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolReadinessStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolReadinessStatus.BLOCKED
        if not self.rows:
            return ToolReadinessStatus.EMPTY
        if self.findings:
            return ToolReadinessStatus.DEGRADED
        return ToolReadinessStatus.READY

    @property
    def required_count(self) -> int:
        return sum(1 for row in self.rows if row.required)

    @property
    def ready_count(self) -> int:
        return sum(1 for row in self.rows if row.ready)

    @property
    def disabled_count(self) -> int:
        return sum(1 for row in self.rows if row.disabled)

    @property
    def optional_count(self) -> int:
        return sum(1 for row in self.rows if not row.required)

    def row(self, component: ToolReadinessComponent | str) -> ToolReadinessRow | None:
        value = str(component)
        for row in self.rows:
            if str(row.component) == value:
                return row
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_readiness_matrix.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "row_count": len(self.rows),
            "required_count": self.required_count,
            "optional_count": self.optional_count,
            "ready_count": self.ready_count,
            "disabled_count": self.disabled_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "rows": [row.to_dict() for row in self.rows],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_readiness_matrix_report_id": self.report_id,
            "tool_readiness_matrix_owner_unit": self.owner_unit,
            "tool_readiness_matrix_runtime_id": self.runtime_id,
            "tool_readiness_matrix_ok": str(self.ok).lower(),
            "tool_readiness_matrix_status": str(self.status),
            "tool_readiness_matrix_rows": str(len(self.rows)),
            "tool_readiness_matrix_required": str(self.required_count),
            "tool_readiness_matrix_optional": str(self.optional_count),
            "tool_readiness_matrix_ready": str(self.ready_count),
            "tool_readiness_matrix_disabled": str(self.disabled_count),
            "tool_readiness_matrix_findings": str(len(self.findings)),
        }


class ToolReadinessMatrixRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
        required_components: Sequence[ToolReadinessComponent] | None = None,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.required_components = tuple(
            required_components
            or (
                ToolReadinessComponent.REGISTRY,
                ToolReadinessComponent.EXECUTION,
                ToolReadinessComponent.RESULT_BUDGET,
                ToolReadinessComponent.RESULT_CONTEXT,
                ToolReadinessComponent.BUDGET_CHAIN,
                ToolReadinessComponent.RESULT_REPLAY_INDEX,
                ToolReadinessComponent.SOURCE_EFFECTS,
            )
        )

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        disabled_components: Sequence[str],
        reports: Mapping[str, Any],
    ) -> ToolReadinessMatrixReport:
        disabled = {str(item) for item in disabled_components if str(item)}
        rows = tuple(self._row(component, disabled=disabled, report=reports.get(str(component))) for component in ToolReadinessComponent)
        findings = tuple(self._findings(rows))
        return ToolReadinessMatrixReport(
            report_id=new_id("toolready"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            rows=rows,
            findings=findings,
        )

    def event_for_report(
        self,
        report: ToolReadinessMatrixReport,
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
                    "phase": "tool_readiness_matrix",
                    "tool_readiness_matrix": report.to_dict(),
                }
            },
        )

    def _row(self, component: ToolReadinessComponent, *, disabled: set[str], report: Any) -> ToolReadinessRow:
        payload = _payload(report)
        report_present = bool(payload)
        report_ok = _ok(payload, report)
        disabled_names = _disabled_names(component)
        is_disabled = any(name in disabled for name in disabled_names)
        expected, observed = _disconnect_effect(component, is_disabled, report_present, report_ok, payload)
        return ToolReadinessRow(
            row_id=new_id("toolreadyrow"),
            component=component,
            required=component in self.required_components,
            disabled=is_disabled,
            report_present=report_present,
            report_ok=report_ok,
            report_status=str(payload.get("status") or ("ready" if report_ok else "missing")),
            report_id=str(payload.get("report_id") or payload.get("materialization_id") or payload.get("artifact_id") or ""),
            event_phase=_phase_for_component(component),
            expected_disconnect_effect=expected,
            observed_effect=observed,
            metadata={key: str(value) for key, value in payload.items() if _metric_key(key, value)},
        )

    def _findings(self, rows: Sequence[ToolReadinessRow]) -> list[ToolReadinessFinding]:
        findings: list[ToolReadinessFinding] = []
        for row in rows:
            if row.disabled and row.required:
                findings.append(
                    ToolReadinessFinding(
                        code="TOOL_READINESS_REQUIRED_COMPONENT_DISABLED",
                        severity=ToolReadinessSeverity.BLOCKER,
                        surface=ToolReadinessSurface.DISABLE_FLAG,
                        message="A required tool loop component is disabled.",
                        component=str(row.component),
                        metadata={"expected_disconnect_effect": row.expected_disconnect_effect},
                    )
                )
            elif row.blocking:
                findings.append(
                    ToolReadinessFinding(
                        code="TOOL_READINESS_REQUIRED_COMPONENT_NOT_READY",
                        severity=ToolReadinessSeverity.BLOCKER,
                        surface=ToolReadinessSurface.REPORT,
                        message="A required tool loop component did not produce a ready report.",
                        component=str(row.component),
                        metadata={"status": row.report_status, "report_id": row.report_id},
                    )
                )
            elif not row.required and row.report_present and not row.report_ok:
                findings.append(
                    ToolReadinessFinding(
                        code="TOOL_READINESS_OPTIONAL_COMPONENT_DEGRADED",
                        severity=ToolReadinessSeverity.WARNING,
                        surface=ToolReadinessSurface.REPORT,
                        message="An optional tool loop component produced a degraded report.",
                        component=str(row.component),
                        metadata={"status": row.report_status},
                    )
                )
        if not any(row.component == ToolReadinessComponent.RESULT_REPLAY_INDEX and row.ready for row in rows):
            findings.append(
                ToolReadinessFinding(
                    code="TOOL_READINESS_REPLAY_INDEX_NOT_READY",
                    severity=ToolReadinessSeverity.BLOCKER,
                    surface=ToolReadinessSurface.MAIN_PATH,
                    message="Result replay index is required so the next slice can restore tool outputs without vendor state.",
                    component=str(ToolReadinessComponent.RESULT_REPLAY_INDEX),
                )
            )
        return findings


def tool_readiness_matrix_metadata(report: ToolReadinessMatrixReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_readiness_matrix_ok": "false", "tool_readiness_matrix_rows": "0"}
    return report.metadata()


def assert_tool_readiness_matrix_ready(report: ToolReadinessMatrixReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.findings if finding.blocking)
    raise AssertionError(f"tool readiness matrix blocked: {blockers or 'unknown'}")


def render_tool_readiness_matrix_markdown(report: ToolReadinessMatrixReport) -> str:
    lines = [
        "## Tool Readiness Matrix",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- rows: `{len(report.rows)}`",
        f"- required: `{report.required_count}`",
        f"- ready: `{report.ready_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)


def _payload(report: Any) -> dict[str, Any]:
    if report is None:
        return {}
    if isinstance(report, Mapping):
        return dict(report)
    if hasattr(report, "to_dict"):
        payload = report.to_dict()
        return dict(payload) if isinstance(payload, Mapping) else {}
    return {}


def _ok(payload: Mapping[str, Any], report: Any) -> bool:
    if payload.get("ok") is not None:
        return bool(payload.get("ok"))
    if report is not None and hasattr(report, "ok"):
        return bool(getattr(report, "ok"))
    status = str(payload.get("status") or "")
    if status:
        return status not in {"blocked", "failed", "fail"}
    return bool(payload)


def _metric_key(key: str, value: Any) -> bool:
    if isinstance(value, (dict, list, tuple)):
        return False
    text = str(key)
    return text.endswith("_count") or text in {"status", "ok", "tool_call_count", "entry_count", "event_count", "node_count", "evidence_count"}


def _disabled_names(component: ToolReadinessComponent) -> tuple[str, ...]:
    mapping = {
        ToolReadinessComponent.REGISTRY: ("ToolRegistryRuntime", "tool_registry", "registry"),
        ToolReadinessComponent.EXECUTION: ("ToolExecutionRuntime", "tool_execution", "execution"),
        ToolReadinessComponent.RESULT_BUDGET: ("ToolResultBudgetRuntime", "tool_result_budget", "result_budget"),
        ToolReadinessComponent.PERMISSION_HANDOFF: ("ToolPermissionHandoffRuntime", "permission_handoff"),
        ToolReadinessComponent.SESSION_BRIDGE: ("ToolSessionBridgeRuntime", "session_bridge"),
        ToolReadinessComponent.RESULT_CONTEXT: ("ToolResultContextRuntime", "result_context"),
        ToolReadinessComponent.EXECUTION_TIMELINE: ("ToolExecutionTimelineRuntime", "execution_timeline"),
        ToolReadinessComponent.BUDGET_CHAIN: ("ToolBudgetChainRuntime", "budget_chain"),
        ToolReadinessComponent.PERMISSION_CHECKPOINT: ("ToolPermissionCheckpointRuntime", "permission_checkpoint"),
        ToolReadinessComponent.CONTINUATION_PACKET: ("ToolContinuationPacketRuntime", "continuation_packet"),
        ToolReadinessComponent.RESULT_REPLAY_INDEX: ("ToolResultReplayIndexRuntime", "result_replay_index"),
        ToolReadinessComponent.SOURCE_EFFECTS: ("ToolSourceEffectRuntime", "source_effects"),
        ToolReadinessComponent.SEMANTIC_EFFECTS: ("ToolSemanticEffectRuntime", "semantic_effects"),
    }
    return mapping.get(component, (str(component),))


def _phase_for_component(component: ToolReadinessComponent) -> str:
    return {
        ToolReadinessComponent.REGISTRY: "tool_registry_materialized",
        ToolReadinessComponent.EXECUTION: "tool_call_completed",
        ToolReadinessComponent.RESULT_BUDGET: "tool_budget_policy",
        ToolReadinessComponent.PERMISSION_HANDOFF: "tool_permission_handoff",
        ToolReadinessComponent.SESSION_BRIDGE: "tool_session_bridge_attached",
        ToolReadinessComponent.RESULT_CONTEXT: "tool_result_context_projected",
        ToolReadinessComponent.EXECUTION_TIMELINE: "tool_execution_timeline",
        ToolReadinessComponent.BUDGET_CHAIN: "tool_budget_chain",
        ToolReadinessComponent.PERMISSION_CHECKPOINT: "tool_permission_checkpoint",
        ToolReadinessComponent.CONTINUATION_PACKET: "tool_continuation_packet",
        ToolReadinessComponent.RESULT_REPLAY_INDEX: "tool_result_replay_index",
        ToolReadinessComponent.SOURCE_EFFECTS: "tool_source_effects",
        ToolReadinessComponent.SEMANTIC_EFFECTS: "tool_semantic_effects",
    }[component]


def _disconnect_effect(
    component: ToolReadinessComponent,
    disabled: bool,
    report_present: bool,
    report_ok: bool,
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    expected = {
        ToolReadinessComponent.REGISTRY: "tool registry materialization fails before any tool execution",
        ToolReadinessComponent.EXECUTION: "tool calls are not executed and receipts are absent",
        ToolReadinessComponent.RESULT_BUDGET: "large outputs are not bounded or externalized",
        ToolReadinessComponent.PERMISSION_HANDOFF: "ask-required tools cannot append permission questions",
        ToolReadinessComponent.SESSION_BRIDGE: "assistant tool_use blocks do not enter the tool loop",
        ToolReadinessComponent.RESULT_CONTEXT: "tool results are not appended for the next turn",
        ToolReadinessComponent.EXECUTION_TIMELINE: "event causality cannot be reconstructed",
        ToolReadinessComponent.BUDGET_CHAIN: "raw-to-bounded result lineage is absent",
        ToolReadinessComponent.PERMISSION_CHECKPOINT: "pending permission state cannot be replayed",
        ToolReadinessComponent.CONTINUATION_PACKET: "next-turn packet lacks tool output summaries",
        ToolReadinessComponent.RESULT_REPLAY_INDEX: "tool outputs cannot be restored from Zyra-owned state",
        ToolReadinessComponent.SOURCE_EFFECTS: "source-to-target rows cannot be tied to runtime effects",
        ToolReadinessComponent.SEMANTIC_EFFECTS: "semantic tool side effects are not verified",
    }[component]
    if disabled:
        observed = "disabled by QueryEngine config"
    elif report_present and report_ok:
        observed = f"ready report {payload.get('report_id') or payload.get('materialization_id') or ''}".strip()
    elif report_present:
        observed = f"report status={payload.get('status') or 'not-ready'}"
    else:
        observed = "report not present in this path"
    return expected, observed
