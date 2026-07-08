from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolContractGateStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    BLOCKED = "blocked"


class ToolContractGateSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ToolContractGateComponent:
    name: str
    ok: bool
    status: str
    required: bool
    evidence_id: str = ""
    summary: str = ""
    metrics: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.required and not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "required": self.required,
            "blocking": self.blocking,
            "evidence_id": self.evidence_id,
            "summary": self.summary,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class ToolContractGateFinding:
    code: str
    severity: ToolContractGateSeverity
    message: str
    component: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolContractGateSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "blocking": self.blocking,
            "message": self.message,
            "component": self.component,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolContractGateReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    components: tuple[ToolContractGateComponent, ...]
    findings: tuple[ToolContractGateFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolContractGateStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolContractGateStatus.BLOCKED
        if self.findings:
            return ToolContractGateStatus.WARN
        return ToolContractGateStatus.PASS

    @property
    def component_count(self) -> int:
        return len(self.components)

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def required_count(self) -> int:
        return sum(1 for component in self.components if component.required)

    @property
    def passed_required_count(self) -> int:
        return sum(1 for component in self.components if component.required and component.ok)

    @property
    def failed_components(self) -> tuple[ToolContractGateComponent, ...]:
        return tuple(component for component in self.components if not component.ok)

    @property
    def failed_required_components(self) -> tuple[ToolContractGateComponent, ...]:
        return tuple(component for component in self.components if component.blocking)

    def component(self, name: str) -> ToolContractGateComponent | None:
        for component in self.components:
            if component.name == name:
                return component
        return None

    def require_component(self, name: str) -> ToolContractGateComponent:
        component = self.component(name)
        if component is None:
            raise AssertionError(f"tool contract gate component missing: {name}")
        if component.blocking:
            raise AssertionError(f"tool contract gate component failed: {name} status={component.status}")
        return component

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "component_count": self.component_count,
            "required_count": self.required_count,
            "passed_required_count": self.passed_required_count,
            "blocking_count": self.blocking_count,
            "failed_components": [component.name for component in self.failed_components],
            "failed_required_components": [component.name for component in self.failed_required_components],
            "components": [component.to_dict() for component in self.components],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_contract_gate_report_id": self.report_id,
            "tool_contract_gate_owner_unit": self.owner_unit,
            "tool_contract_gate_ok": str(self.ok).lower(),
            "tool_contract_gate_status": str(self.status),
            "tool_contract_gate_components": str(self.component_count),
            "tool_contract_gate_required": str(self.required_count),
            "tool_contract_gate_required_passed": str(self.passed_required_count),
            "tool_contract_gate_blockers": str(self.blocking_count),
            "tool_contract_gate_findings": str(len(self.findings)),
        }


class ToolContractGateRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
        required_components: Sequence[str] | None = None,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.required_components = tuple(required_components or default_tool_contract_gate_components())

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        reports: Mapping[str, Any],
    ) -> ToolContractGateReport:
        components = tuple(self._component(name, reports.get(name), required=name in self.required_components) for name in sorted(set(reports) | set(self.required_components)))
        findings = tuple(self._findings(components))
        return ToolContractGateReport(
            report_id=new_id("toolcontractgate"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            components=components,
            findings=findings,
        )

    def event_for_report(
        self,
        report: ToolContractGateReport,
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
                    "phase": "tool_contract_gate",
                    "tool_contract_gate": report.to_dict(),
                }
            },
        )

    def _component(self, name: str, report: Any, *, required: bool) -> ToolContractGateComponent:
        payload = _report_payload(report)
        ok = payload.get("ok")
        if ok is None and "status" in payload:
            ok = str(payload.get("status")) not in {"blocked", "fail", "failed"}
        if ok is None:
            ok = report is not None and not (required and not payload)
        return ToolContractGateComponent(
            name=name,
            ok=bool(ok),
            status=str(payload.get("status") or ("pass" if ok else "missing")),
            required=required,
            evidence_id=str(payload.get("report_id") or payload.get("snapshot_id") or payload.get("materialization_id") or ""),
            summary=str(payload.get("summary") or ""),
            metrics={key: str(value) for key, value in payload.items() if _metric_key(key, value)},
        )

    def _findings(self, components: Sequence[ToolContractGateComponent]) -> list[ToolContractGateFinding]:
        findings: list[ToolContractGateFinding] = []
        for component in components:
            if component.blocking:
                findings.append(
                    ToolContractGateFinding(
                        code="TOOL_CONTRACT_GATE_REQUIRED_COMPONENT_FAILED",
                        severity=ToolContractGateSeverity.BLOCKER,
                        message="A required tool foundation component did not pass its runtime contract.",
                        component=component.name,
                        metadata={"status": component.status, "evidence_id": component.evidence_id},
                    )
                )
            elif not component.required and not component.ok:
                findings.append(
                    ToolContractGateFinding(
                        code="TOOL_CONTRACT_GATE_OPTIONAL_COMPONENT_FAILED",
                        severity=ToolContractGateSeverity.WARNING,
                        message="An optional tool foundation component did not pass its runtime contract.",
                        component=component.name,
                        metadata={"status": component.status},
                    )
                )
        return findings


def default_tool_contract_gate_components() -> tuple[str, ...]:
    return (
        "materialization",
        "audit",
        "persistence",
        "permission_handoff",
        "budget_policy",
        "streaming",
        "continuation",
        "concurrency",
        "failure_policy",
        "output_store",
        "source_coverage",
        "cleanroom",
        "settlement",
    )


def tool_contract_gate_metadata(report: ToolContractGateReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_contract_gate_ok": "true",
            "tool_contract_gate_components": "0",
        }
    return report.metadata()


def assert_tool_contract_gate_pass(report: ToolContractGateReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(component.name for component in report.failed_required_components) or "unknown"
    finding_codes = ", ".join(finding.code for finding in report.findings if finding.blocking) or "no_blocking_code"
    raise AssertionError(f"tool contract gate blocked: components={blockers}; findings={finding_codes}")


def render_tool_contract_gate_markdown(report: ToolContractGateReport) -> str:
    lines = [
        "## Tool Contract Gate",
        "",
        f"- owner_unit: `{report.owner_unit}`",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- components: `{report.component_count}`",
        f"- required: `{report.required_count}`",
        f"- required_passed: `{report.passed_required_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    lines.extend(["", "### Components", ""])
    for component in report.components:
        lines.append(
            f"- `{component.name}`: ok `{str(component.ok).lower()}`, status `{component.status}`, required `{str(component.required).lower()}`"
        )
    return "\n".join(lines)


def _report_payload(report: Any) -> dict[str, Any]:
    if report is None:
        return {}
    if isinstance(report, Mapping):
        return dict(to_jsonable(report))
    to_dict = getattr(report, "to_dict", None)
    if callable(to_dict):
        return dict(to_jsonable(to_dict()))
    metadata = getattr(report, "metadata", None)
    if callable(metadata):
        return dict(to_jsonable(metadata()))
    return {}


def _metric_key(key: str, value: Any) -> bool:
    if key in {"report_id", "snapshot_id", "materialization_id", "owner_unit", "runtime_id", "session_id", "worker_request_id"}:
        return False
    return isinstance(value, (str, int, float, bool))
