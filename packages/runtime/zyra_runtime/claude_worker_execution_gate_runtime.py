from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from zyra_core import EventRecord, EventType, now_iso

from .claude_runtime_context_ports import RuntimeContextAssemblyReport
from .claude_runtime_contracts import ClaudeRuntimeContractBundle
from .claude_source_graph_audit import SourceGraphAuditReport
from .claude_source_graph_crosswalk import ClaudeProductizationIntegrationReport
from .tools import ToolSpec
from .workers import WorkerRequest


class WorkerExecutionGateStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    BLOCKED = "blocked"


class WorkerExecutionGateSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class WorkerExecutionGateRisk(StrEnum):
    NONE = "none"
    DISABLED_BY_CONSTRAINT = "disabled_by_constraint"
    PRODUCTIZED_RUNTIME_DISABLED = "productized_runtime_disabled"
    INTEGRATION_BLOCKED = "integration_blocked"
    RUNTIME_CONTEXT_BLOCKED = "runtime_context_blocked"
    SOURCE_GRAPH_AUDIT_BLOCKED = "source_graph_audit_blocked"
    SIDEcar_CONTRACTS_REQUESTED = "sidecar_contracts_requested"
    TOOL_REGISTRY_EMPTY = "tool_registry_empty"
    MUTATING_TOOL_WITHOUT_PERMISSION = "mutating_tool_without_permission"
    WORKSPACE_TARGET_INVALID = "workspace_target_invalid"
    SOURCE_POOL_PATH = "source_pool_path"
    QUERY_ENGINE_UNAVAILABLE = "query_engine_unavailable"
    EVENT_COVERAGE_GAP = "event_coverage_gap"
    POLICY_MATRIX_BLOCKED = "policy_matrix_blocked"


class WorkerExecutionGateKind(StrEnum):
    PRODUCTIZED_RUNTIME = "productized_runtime"
    SOURCE_GRAPH = "source_graph"
    RUNTIME_CONTEXT = "runtime_context"
    TOOL_REGISTRY = "tool_registry"
    PERMISSION = "permission"
    WORKSPACE = "workspace"
    EVENT_CAUSALITY = "event_causality"
    QUERY_ENGINE = "query_engine"
    POLICY = "policy"


@dataclass(frozen=True, slots=True)
class WorkerExecutionGateRule:
    rule_id: str
    kind: WorkerExecutionGateKind
    required: bool
    owner_slice: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": str(self.kind),
            "required": self.required,
            "owner_slice": self.owner_slice,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class WorkerExecutionGateDecision:
    rule: WorkerExecutionGateRule
    status: WorkerExecutionGateStatus
    risk: WorkerExecutionGateRisk
    observed: str
    expected: str
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status == WorkerExecutionGateStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.to_dict(),
            "status": str(self.status),
            "risk": str(self.risk),
            "observed": self.observed,
            "expected": self.expected,
            "message": self.message,
            "metadata": dict(self.metadata),
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class WorkerExecutionGateFinding:
    severity: WorkerExecutionGateSeverity
    code: str
    message: str
    rule_id: str
    kind: WorkerExecutionGateKind

    @property
    def blocking(self) -> bool:
        return self.severity == WorkerExecutionGateSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "rule_id": self.rule_id,
            "kind": str(self.kind),
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class WorkerExecutionGateInputs:
    request: WorkerRequest
    project_root: Path
    workspace_root: Path
    artifact_root: Path
    runtime_contracts: ClaudeRuntimeContractBundle
    integration_report: ClaudeProductizationIntegrationReport
    runtime_context_report: RuntimeContextAssemblyReport
    source_graph_audit: SourceGraphAuditReport
    tool_specs: tuple[ToolSpec, ...]
    query_engine_available: bool
    sidecar_contracts_used: bool


@dataclass(frozen=True, slots=True)
class WorkerExecutionGateReport:
    ok: bool
    checked_at: str
    owner_slice: str
    decisions: tuple[WorkerExecutionGateDecision, ...]
    findings: tuple[WorkerExecutionGateFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[WorkerExecutionGateFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[WorkerExecutionGateFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == WorkerExecutionGateSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    @property
    def passing_decisions(self) -> int:
        return sum(1 for decision in self.decisions if decision.status == WorkerExecutionGateStatus.PASSING)

    def metadata(self) -> dict[str, str]:
        return {
            "worker_execution_gate_ok": str(self.ok).lower(),
            "worker_execution_gate_decisions": str(len(self.decisions)),
            "worker_execution_gate_passing": str(self.passing_decisions),
            "worker_execution_gate_blockers": str(len(self.blockers)),
            "worker_execution_gate_warnings": str(len(self.warnings)),
            "worker_execution_gate_first_blocker": self.first_blocker_code,
            "worker_execution_gate_owner_slice": self.owner_slice,
        }

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": "worker_execution_gate",
            "ok": self.ok,
            "owner_slice": self.owner_slice,
            "decisions": len(self.decisions),
            "passing": self.passing_decisions,
            "blocking_error": self.first_blocker_code,
            "blockers": [finding.to_dict() for finding in self.blockers],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "owner_slice": self.owner_slice,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


def build_worker_execution_gate_report(inputs: WorkerExecutionGateInputs) -> WorkerExecutionGateReport:
    rules = default_worker_execution_gate_rules(inputs.integration_report.crosswalk.owner_slice)
    evaluators = _gate_evaluators()
    decisions = tuple(evaluators[rule.rule_id](rule, inputs) for rule in rules)
    findings = tuple(_gate_findings(decisions))
    return WorkerExecutionGateReport(
        ok=not any(finding.blocking for finding in findings),
        checked_at=now_iso(),
        owner_slice=inputs.integration_report.crosswalk.owner_slice,
        decisions=decisions,
        findings=findings,
    )


def build_worker_execution_gate_inputs(
    *,
    request: WorkerRequest,
    project_root: str | Path,
    workspace_root: str | Path,
    artifact_root: str | Path,
    runtime_contracts: ClaudeRuntimeContractBundle,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport,
    source_graph_audit: SourceGraphAuditReport,
    tool_specs: Iterable[ToolSpec],
    query_engine_available: bool,
    sidecar_contracts_used: bool,
) -> WorkerExecutionGateInputs:
    return WorkerExecutionGateInputs(
        request=request,
        project_root=Path(project_root).resolve(),
        workspace_root=Path(workspace_root).resolve(),
        artifact_root=Path(artifact_root).resolve(),
        runtime_contracts=runtime_contracts,
        integration_report=integration_report,
        runtime_context_report=runtime_context_report,
        source_graph_audit=source_graph_audit,
        tool_specs=tuple(tool_specs),
        query_engine_available=query_engine_available,
        sidecar_contracts_used=sidecar_contracts_used,
    )


def default_worker_execution_gate_rules(owner_slice: str) -> tuple[WorkerExecutionGateRule, ...]:
    return (
        WorkerExecutionGateRule(
            rule_id="gate.productized_runtime.enabled",
            kind=WorkerExecutionGateKind.PRODUCTIZED_RUNTIME,
            required=True,
            owner_slice=owner_slice,
            rationale="CodeWorker must use the Zyra-owned productized QueryEngine path.",
        ),
        WorkerExecutionGateRule(
            rule_id="gate.source_graph.ready",
            kind=WorkerExecutionGateKind.SOURCE_GRAPH,
            required=True,
            owner_slice=owner_slice,
            rationale="Source graph validation must pass before any tool execution.",
        ),
        WorkerExecutionGateRule(
            rule_id="gate.runtime_context.ready",
            kind=WorkerExecutionGateKind.RUNTIME_CONTEXT,
            required=True,
            owner_slice=owner_slice,
            rationale="RuntimeContext carries session, registry, permission and artifact state into the loop.",
        ),
        WorkerExecutionGateRule(
            rule_id="gate.source_graph_audit.ready",
            kind=WorkerExecutionGateKind.SOURCE_GRAPH,
            required=True,
            owner_slice=owner_slice,
            rationale="The dynamic audit must pass before the worker proceeds.",
        ),
        WorkerExecutionGateRule(
            rule_id="gate.policy_matrix.ready",
            kind=WorkerExecutionGateKind.POLICY,
            required=True,
            owner_slice=owner_slice,
            rationale="Runtime policy matrix blockers must not be bypassed by the worker.",
        ),
        WorkerExecutionGateRule(
            rule_id="gate.tool_registry.non_empty",
            kind=WorkerExecutionGateKind.TOOL_REGISTRY,
            required=True,
            owner_slice=owner_slice,
            rationale="The loop cannot execute without a real tool registry.",
        ),
        WorkerExecutionGateRule(
            rule_id="gate.permission.bound_for_mutation",
            kind=WorkerExecutionGateKind.PERMISSION,
            required=True,
            owner_slice=owner_slice,
            rationale="Mutating tools require a bound permission mode before execution.",
        ),
        WorkerExecutionGateRule(
            rule_id="gate.workspace.clean_boundary",
            kind=WorkerExecutionGateKind.WORKSPACE,
            required=True,
            owner_slice=owner_slice,
            rationale="Workspace and artifact roots must not point at source-pool or vendor-like locations.",
        ),
        WorkerExecutionGateRule(
            rule_id="gate.query_engine.available",
            kind=WorkerExecutionGateKind.QUERY_ENGINE,
            required=True,
            owner_slice=owner_slice,
            rationale="A QueryEngine factory must be present for the productized runtime path.",
        ),
        WorkerExecutionGateRule(
            rule_id="gate.events.covered",
            kind=WorkerExecutionGateKind.EVENT_CAUSALITY,
            required=True,
            owner_slice=owner_slice,
            rationale="Source graph audit must expose event contracts and runtime observations.",
        ),
    )


def worker_execution_gate_event(request: WorkerRequest, report: WorkerExecutionGateReport) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.CONSTRAINT_CHECK,
        payload={"claude_worker_execution_gate": report.event_payload()},
    )


def worker_execution_gate_markdown(report: WorkerExecutionGateReport) -> str:
    lines = [
        "## Worker Execution Gate",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- decisions: `{len(report.decisions)}`",
        f"- passing: `{report.passing_decisions}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Decisions",
        "",
    ]
    for decision in report.decisions:
        lines.append(
            f"- `{decision.rule.rule_id}` kind=`{decision.rule.kind}` "
            f"status=`{decision.status}` observed=`{decision.observed}`"
        )
    if report.blockers:
        lines.extend(["", "### Worker Gate Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    return "\n".join(lines) + "\n"


def assert_worker_execution_gate_ready(report: WorkerExecutionGateReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"Worker execution gate failed: {blockers}")


def _gate_evaluators() -> dict[str, Any]:
    return {
        "gate.productized_runtime.enabled": _eval_productized_runtime_enabled,
        "gate.source_graph.ready": _eval_source_graph_ready,
        "gate.runtime_context.ready": _eval_runtime_context_ready,
        "gate.source_graph_audit.ready": _eval_source_graph_audit_ready,
        "gate.policy_matrix.ready": _eval_policy_matrix_ready,
        "gate.tool_registry.non_empty": _eval_tool_registry,
        "gate.permission.bound_for_mutation": _eval_permission_bound,
        "gate.workspace.clean_boundary": _eval_workspace_boundary,
        "gate.query_engine.available": _eval_query_engine_available,
        "gate.events.covered": _eval_events_covered,
    }


def _eval_productized_runtime_enabled(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    disabled = inputs.request.constraints.get("disable_worker_execution_gate") is True
    productized_disabled = inputs.request.constraints.get("disable_productized_runtime") is True
    ok = not disabled and not productized_disabled and inputs.runtime_contracts.clean_runtime_safe
    risk = WorkerExecutionGateRisk.NONE
    if disabled:
        risk = WorkerExecutionGateRisk.DISABLED_BY_CONSTRAINT
    elif productized_disabled:
        risk = WorkerExecutionGateRisk.PRODUCTIZED_RUNTIME_DISABLED
    return _decision(
        rule,
        ok=ok,
        risk=risk,
        observed=str(ok).lower(),
        expected="true",
        message="Productized runtime is enabled and clean-source safe.",
        metadata=inputs.runtime_contracts.metadata(),
    )


def _eval_source_graph_ready(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    ok = inputs.integration_report.ok
    return _decision(
        rule,
        ok=ok,
        risk=WorkerExecutionGateRisk.NONE if ok else WorkerExecutionGateRisk.INTEGRATION_BLOCKED,
        observed=inputs.integration_report.blocking_error or "ready",
        expected="ready",
        message="Claude source graph integration passed.",
        metadata=inputs.integration_report.metadata(),
    )


def _eval_runtime_context_ready(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    ok = inputs.runtime_context_report.ok
    return _decision(
        rule,
        ok=ok,
        risk=WorkerExecutionGateRisk.NONE if ok else WorkerExecutionGateRisk.RUNTIME_CONTEXT_BLOCKED,
        observed=inputs.runtime_context_report.first_blocker_code or "ready",
        expected="ready",
        message="RuntimeContext assembly passed.",
        metadata=inputs.runtime_context_report.metadata(),
    )


def _eval_source_graph_audit_ready(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    ok = inputs.source_graph_audit.ok
    return _decision(
        rule,
        ok=ok,
        risk=WorkerExecutionGateRisk.NONE if ok else WorkerExecutionGateRisk.SOURCE_GRAPH_AUDIT_BLOCKED,
        observed=inputs.source_graph_audit.first_blocker_code or "ready",
        expected="ready",
        message="Source graph audit passed.",
        metadata=inputs.source_graph_audit.metadata(),
    )


def _eval_policy_matrix_ready(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    policy = inputs.source_graph_audit.runtime_policy_matrix_report
    ok = policy.ok
    return _decision(
        rule,
        ok=ok,
        risk=WorkerExecutionGateRisk.NONE if ok else WorkerExecutionGateRisk.POLICY_MATRIX_BLOCKED,
        observed=policy.first_blocker_code or "ready",
        expected="ready",
        message="Runtime policy matrix has no blockers.",
        metadata=policy.metadata(),
    )


def _eval_tool_registry(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    tool_count = len(inputs.tool_specs)
    ok = tool_count > 0
    return _decision(
        rule,
        ok=ok,
        risk=WorkerExecutionGateRisk.NONE if ok else WorkerExecutionGateRisk.TOOL_REGISTRY_EMPTY,
        observed=str(tool_count),
        expected=">0",
        message="Tool registry contains executable tool specs.",
        metadata={"tool_names": ",".join(tool.name for tool in inputs.tool_specs[:16])},
    )


def _eval_permission_bound(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    mutating = tuple(tool.name for tool in inputs.tool_specs if tool.metadata.get("read_only") != "true")
    permission_modes = [
        binding
        for binding in inputs.runtime_context_report.runtime_bindings
        if str(binding.port_kind) == "permission_mode" and binding.active_in_default_path
    ]
    ok = not mutating or bool(permission_modes)
    return _decision(
        rule,
        ok=ok,
        risk=WorkerExecutionGateRisk.NONE if ok else WorkerExecutionGateRisk.MUTATING_TOOL_WITHOUT_PERMISSION,
        observed=f"mutating={len(mutating)} permission_modes={len(permission_modes)}",
        expected="permission_modes>=1",
        message="Mutating tools are covered by a RuntimeContext permission mode.",
        metadata={"mutating_tools": ",".join(mutating[:16])},
    )


def _eval_workspace_boundary(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    paths = (inputs.project_root, inputs.workspace_root, inputs.artifact_root)
    source_pool = tuple(str(path) for path in paths if _path_contains_source_pool(path))
    ok = not source_pool and inputs.project_root.exists()
    risk = WorkerExecutionGateRisk.NONE
    if source_pool:
        risk = WorkerExecutionGateRisk.SOURCE_POOL_PATH
    elif not inputs.project_root.exists():
        risk = WorkerExecutionGateRisk.WORKSPACE_TARGET_INVALID
    return _decision(
        rule,
        ok=ok,
        risk=risk,
        observed=";".join(str(path) for path in paths),
        expected="clean Zyra project/workspace/artifact roots",
        message="Worker path roots stay inside clean Zyra-owned boundaries.",
        metadata={"source_pool_paths": ",".join(source_pool)},
    )


def _eval_query_engine_available(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    return _decision(
        rule,
        ok=inputs.query_engine_available,
        risk=WorkerExecutionGateRisk.NONE if inputs.query_engine_available else WorkerExecutionGateRisk.QUERY_ENGINE_UNAVAILABLE,
        observed=str(inputs.query_engine_available).lower(),
        expected="true",
        message="QueryEngine factory is available for the default worker path.",
    )


def _eval_events_covered(
    rule: WorkerExecutionGateRule,
    inputs: WorkerExecutionGateInputs,
) -> WorkerExecutionGateDecision:
    event_count = len(inputs.source_graph_audit.event_contracts)
    runtime_observed = inputs.source_graph_audit.metadata().get("source_graph_audit_event_runtime_observed", "0")
    ok = event_count >= 20 and runtime_observed != "0"
    return _decision(
        rule,
        ok=ok,
        risk=WorkerExecutionGateRisk.NONE if ok else WorkerExecutionGateRisk.EVENT_COVERAGE_GAP,
        observed=f"events={event_count} observed={runtime_observed}",
        expected="events>=20 and observed>0",
        message="Worker execution has enough event contract coverage for runtime causality.",
    )


def _decision(
    rule: WorkerExecutionGateRule,
    *,
    ok: bool,
    risk: WorkerExecutionGateRisk,
    observed: str,
    expected: str,
    message: str,
    metadata: dict[str, str] | None = None,
) -> WorkerExecutionGateDecision:
    if ok:
        status = WorkerExecutionGateStatus.PASSING
    elif rule.required:
        status = WorkerExecutionGateStatus.BLOCKED
    else:
        status = WorkerExecutionGateStatus.WARNING
    return WorkerExecutionGateDecision(
        rule=rule,
        status=status,
        risk=WorkerExecutionGateRisk.NONE if ok else risk,
        observed=observed,
        expected=expected,
        message=message if ok else f"{message} Observed {observed}, expected {expected}.",
        metadata=dict(metadata or {}),
    )


def _gate_findings(decisions: Iterable[WorkerExecutionGateDecision]) -> list[WorkerExecutionGateFinding]:
    findings: list[WorkerExecutionGateFinding] = []
    for decision in decisions:
        if decision.status == WorkerExecutionGateStatus.BLOCKED:
            findings.append(
                WorkerExecutionGateFinding(
                    severity=WorkerExecutionGateSeverity.BLOCKER,
                    code=f"worker_gate_{decision.risk}",
                    message=decision.message,
                    rule_id=decision.rule.rule_id,
                    kind=decision.rule.kind,
                )
            )
        elif decision.status == WorkerExecutionGateStatus.WARNING:
            findings.append(
                WorkerExecutionGateFinding(
                    severity=WorkerExecutionGateSeverity.WARNING,
                    code=f"worker_gate_{decision.risk}",
                    message=decision.message,
                    rule_id=decision.rule.rule_id,
                    kind=decision.rule.kind,
                )
            )
    return findings


def _path_contains_source_pool(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    return any(
        marker in normalized
        for marker in (
            "/vendor/",
            "/vendor-runtimes/",
            "/source-pool/",
            "/runtime-sources/",
            "/third_party/",
        )
    )
