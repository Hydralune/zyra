from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Iterable

from zyra_core import now_iso

from .claude_disconnect_semantics_runtime import DisconnectSemanticsReport
from .claude_downstream_handoff_runtime import DownstreamHandoffReport
from .claude_event_contract_runtime import EventContractRuntimeReport
from .claude_productization_review_runtime import ProductizationReviewReport
from .claude_runtime_context_ports import RuntimeContextAssemblyReport
from .claude_runtime_contracts import ClaudeRuntimeContractBundle
from .claude_source_graph_crosswalk import ClaudeProductizationIntegrationReport


class RuntimePolicyDimension(StrEnum):
    DEFAULT_PATH = "default_path"
    SOURCE_BOUNDARY = "source_boundary"
    STATE_CUSTODY = "state_custody"
    EVENT_CAUSALITY = "event_causality"
    PERMISSION_SEMANTICS = "permission_semantics"
    TOOL_LOOP_SEMANTICS = "tool_loop_semantics"
    DOWNSTREAM_HANDOFF = "downstream_handoff"
    DISCONNECT_SEMANTICS = "disconnect_semantics"
    LINE_ACCOUNTING = "line_accounting"
    FALLBACK_CONTROL = "fallback_control"


class RuntimePolicyStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    BLOCKED = "blocked"


class RuntimePolicySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class RuntimePolicyEffect(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class RuntimePolicyEvidence:
    evidence_id: str
    source: str
    subject: str
    observed_value: str
    expected_value: str
    ok: bool
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "subject": self.subject,
            "observed_value": self.observed_value,
            "expected_value": self.expected_value,
            "ok": self.ok,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuntimePolicyRule:
    rule_id: str
    dimension: RuntimePolicyDimension
    description: str
    effect_on_failure: RuntimePolicyEffect
    evaluator: str
    owner_slice: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "dimension": str(self.dimension),
            "description": self.description,
            "effect_on_failure": str(self.effect_on_failure),
            "evaluator": self.evaluator,
            "owner_slice": self.owner_slice,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class RuntimePolicyDecision:
    rule: RuntimePolicyRule
    status: RuntimePolicyStatus
    evidence: tuple[RuntimePolicyEvidence, ...]
    message: str

    @property
    def blocking(self) -> bool:
        return self.status == RuntimePolicyStatus.BLOCKED

    @property
    def subject(self) -> str:
        return self.evidence[0].subject if self.evidence else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.to_dict(),
            "status": str(self.status),
            "evidence": [item.to_dict() for item in self.evidence],
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class RuntimePolicyFinding:
    severity: RuntimePolicySeverity
    code: str
    message: str
    rule_id: str
    dimension: RuntimePolicyDimension

    @property
    def blocking(self) -> bool:
        return self.severity == RuntimePolicySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "rule_id": self.rule_id,
            "dimension": str(self.dimension),
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class RuntimePolicyMatrixReport:
    ok: bool
    checked_at: str
    owner_slice: str
    decisions: tuple[RuntimePolicyDecision, ...]
    findings: tuple[RuntimePolicyFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[RuntimePolicyFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[RuntimePolicyFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == RuntimePolicySeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    def metadata(self) -> dict[str, str]:
        return {
            "runtime_policy_matrix_ok": str(self.ok).lower(),
            "runtime_policy_matrix_decisions": str(len(self.decisions)),
            "runtime_policy_matrix_blockers": str(len(self.blockers)),
            "runtime_policy_matrix_warnings": str(len(self.warnings)),
            "runtime_policy_matrix_first_blocker": self.first_blocker_code,
            "runtime_policy_matrix_owner_slice": self.owner_slice,
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


@dataclass(frozen=True, slots=True)
class RuntimePolicyInputs:
    integration_report: ClaudeProductizationIntegrationReport
    runtime_context_report: RuntimeContextAssemblyReport | None
    runtime_contracts: ClaudeRuntimeContractBundle
    downstream_handoff_report: DownstreamHandoffReport
    event_contract_report: EventContractRuntimeReport
    productization_review_report: ProductizationReviewReport
    disconnect_semantics_report: DisconnectSemanticsReport


def build_runtime_policy_matrix_report(inputs: RuntimePolicyInputs) -> RuntimePolicyMatrixReport:
    rules = default_runtime_policy_rules(inputs.integration_report.crosswalk.owner_slice)
    evaluators = _policy_evaluators()
    decisions: list[RuntimePolicyDecision] = []
    for rule in rules:
        evaluator = evaluators[rule.evaluator]
        decisions.append(evaluator(rule, inputs))
    findings = tuple(_policy_findings(decisions))
    ok = not any(finding.blocking for finding in findings)
    return RuntimePolicyMatrixReport(
        ok=ok,
        checked_at=now_iso(),
        owner_slice=inputs.integration_report.crosswalk.owner_slice,
        decisions=tuple(decisions),
        findings=findings,
    )


def default_runtime_policy_rules(owner_slice: str) -> tuple[RuntimePolicyRule, ...]:
    return (
        RuntimePolicyRule(
            rule_id="policy.default_path.sidecar_free",
            dimension=RuntimePolicyDimension.DEFAULT_PATH,
            description="Default CodeWorker path must not require sidecar contracts.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="sidecar_free",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.default_path.clean_runtime",
            dimension=RuntimePolicyDimension.DEFAULT_PATH,
            description="Default runtime contract bundle must be clean-runtime safe.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="clean_runtime",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.source_boundary.no_source_pool_targets",
            dimension=RuntimePolicyDimension.SOURCE_BOUNDARY,
            description="Current source graph targets must not point at vendor/source-pool paths.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="no_source_pool_targets",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.runtime_context.required_ports",
            dimension=RuntimePolicyDimension.DEFAULT_PATH,
            description="Required RuntimeContext ports must be assembled before tool execution.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="runtime_context_required_ports",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.state_custody.owned",
            dimension=RuntimePolicyDimension.STATE_CUSTODY,
            description="Runtime state must have Zyra-owned custody rules.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="state_custody",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.event_contracts.ready",
            dimension=RuntimePolicyDimension.EVENT_CAUSALITY,
            description="Event contracts must have owner, payload and required phase coverage.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="event_contracts",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.downstream_handoff.ready",
            dimension=RuntimePolicyDimension.DOWNSTREAM_HANDOFF,
            description="Downstream handoff packages must include owner, port, event and test gates.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="downstream_handoff",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.disconnect_semantics.ready",
            dimension=RuntimePolicyDimension.DISCONNECT_SEMANTICS,
            description="Disconnect scenarios must prove semantic behavior changes.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="disconnect_semantics",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.line_accounting.effective_bucket",
            dimension=RuntimePolicyDimension.LINE_ACCOUNTING,
            description="Only production_internalized bucket may count as effective production.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="line_accounting",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.fallback.no_masking",
            dimension=RuntimePolicyDimension.FALLBACK_CONTROL,
            description="Fallbacks must not mask source graph/runtime context failures.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="fallback_control",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.permission.semantic",
            dimension=RuntimePolicyDimension.PERMISSION_SEMANTICS,
            description="Permission semantics must be real tool execution gates, not static metadata.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="permission_semantics",
            owner_slice=owner_slice,
        ),
        RuntimePolicyRule(
            rule_id="policy.tool_loop.semantic",
            dimension=RuntimePolicyDimension.TOOL_LOOP_SEMANTICS,
            description="ToolUseContext must bind registry, executor, budget and result surfaces.",
            effect_on_failure=RuntimePolicyEffect.BLOCK,
            evaluator="tool_loop_semantics",
            owner_slice=owner_slice,
        ),
    )


def runtime_policy_matrix_markdown(report: RuntimePolicyMatrixReport) -> str:
    lines = [
        "## Runtime Policy Matrix",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- decisions: `{len(report.decisions)}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Decisions",
        "",
    ]
    for decision in report.decisions:
        lines.append(f"- `{decision.rule.rule_id}` status=`{decision.status}`")
    if report.blockers:
        lines.extend(["", "### Policy Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    return "\n".join(lines) + "\n"


def assert_runtime_policy_matrix_ready(report: RuntimePolicyMatrixReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"Runtime policy matrix failed: {blockers}")


def _policy_evaluators() -> dict[str, Callable[[RuntimePolicyRule, RuntimePolicyInputs], RuntimePolicyDecision]]:
    return {
        "sidecar_free": _evaluate_sidecar_free,
        "clean_runtime": _evaluate_clean_runtime,
        "no_source_pool_targets": _evaluate_no_source_pool_targets,
        "runtime_context_required_ports": _evaluate_runtime_context_required_ports,
        "state_custody": _evaluate_state_custody,
        "event_contracts": _evaluate_event_contracts,
        "downstream_handoff": _evaluate_downstream_handoff,
        "disconnect_semantics": _evaluate_disconnect_semantics,
        "line_accounting": _evaluate_line_accounting,
        "fallback_control": _evaluate_fallback_control,
        "permission_semantics": _evaluate_permission_semantics,
        "tool_loop_semantics": _evaluate_tool_loop_semantics,
    }


def _decision(
    rule: RuntimePolicyRule,
    *,
    ok: bool,
    source: str,
    subject: str,
    observed: str,
    expected: str,
    message: str,
    metadata: dict[str, str] | None = None,
    warning: bool = False,
) -> RuntimePolicyDecision:
    if ok:
        status = RuntimePolicyStatus.PASSING
    elif warning or rule.effect_on_failure == RuntimePolicyEffect.WARN:
        status = RuntimePolicyStatus.WARNING
    else:
        status = RuntimePolicyStatus.BLOCKED
    return RuntimePolicyDecision(
        rule=rule,
        status=status,
        evidence=(
            RuntimePolicyEvidence(
                evidence_id=f"{rule.rule_id}.evidence",
                source=source,
                subject=subject,
                observed_value=observed,
                expected_value=expected,
                ok=ok,
                metadata=dict(metadata or {}),
            ),
        ),
        message=message,
    )


def _evaluate_sidecar_free(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    default_path = inputs.runtime_contracts.default_path
    sidecar_required = default_path.get("requiresNodeSidecar") is True or inputs.integration_report.sidecar_contracts_used
    return _decision(
        rule,
        ok=not sidecar_required,
        source="ClaudeRuntimeContractBundle.default_path",
        subject="requiresNodeSidecar",
        observed=str(sidecar_required).lower(),
        expected="false",
        message="Default path excludes Node sidecar contracts.",
    )


def _evaluate_clean_runtime(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    return _decision(
        rule,
        ok=inputs.runtime_contracts.clean_runtime_safe,
        source="ClaudeRuntimeContractBundle.clean_runtime_safe",
        subject="clean_runtime_safe",
        observed=str(inputs.runtime_contracts.clean_runtime_safe).lower(),
        expected="true",
        message="Runtime contract bundle is clean-source safe.",
    )


def _evaluate_no_source_pool_targets(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    count = inputs.integration_report.crosswalk.source_pool_target_count
    return _decision(
        rule,
        ok=count == 0,
        source="ClaudeSourceGraphCrosswalk.source_pool_target_count",
        subject="source_pool_target_count",
        observed=str(count),
        expected="0",
        message="Crosswalk excludes source-pool and vendor-like targets.",
    )


def _evaluate_runtime_context_required_ports(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    report = inputs.runtime_context_report
    ok = bool(report and report.ok and not report.blockers)
    return _decision(
        rule,
        ok=ok,
        source="RuntimeContextAssemblyReport",
        subject="required_ports",
        observed="ready" if ok else (report.first_blocker_code if report else "not_assembled"),
        expected="ready",
        message="RuntimeContext required ports are assembled before tool execution.",
    )


def _evaluate_state_custody(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    report = inputs.productization_review_report
    ok = report.metadata()["productization_review_state_custody_blockers"] == "0"
    return _decision(
        rule,
        ok=ok,
        source="ProductizationReviewReport.state_custody_rules",
        subject="state_custody",
        observed=report.metadata()["productization_review_state_custody_blockers"],
        expected="0",
        message="Runtime state custody has no blockers.",
    )


def _evaluate_event_contracts(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    report = inputs.event_contract_report
    return _decision(
        rule,
        ok=report.ok,
        source="EventContractRuntimeReport",
        subject="event_contracts",
        observed=report.first_blocker_code or "ready",
        expected="ready",
        message="Event contracts are declared with owner/payload coverage.",
    )


def _evaluate_downstream_handoff(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    report = inputs.downstream_handoff_report
    return _decision(
        rule,
        ok=report.ok,
        source="DownstreamHandoffReport",
        subject="downstream_handoff",
        observed=report.first_blocker_code or "ready",
        expected="ready",
        message="Downstream handoff packages are ready.",
    )


def _evaluate_disconnect_semantics(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    report = inputs.disconnect_semantics_report
    return _decision(
        rule,
        ok=report.ok,
        source="DisconnectSemanticsReport",
        subject="disconnect_semantics",
        observed=report.first_blocker_code or "ready",
        expected="ready",
        message="Disconnect scenarios cover runtime semantic effects.",
    )


def _evaluate_line_accounting(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    metadata = inputs.productization_review_report.metadata()
    ok = metadata["productization_review_effective_line_buckets"] == "1"
    return _decision(
        rule,
        ok=ok,
        source="ProductizationReviewReport.line_bucket_expectations",
        subject="effective_line_buckets",
        observed=metadata["productization_review_effective_line_buckets"],
        expected="1",
        message="Exactly one bucket counts as effective production.",
    )


def _evaluate_fallback_control(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    metadata = inputs.integration_report.metadata()
    ok = (
        metadata.get("source_graph_crosswalk_ok") == "true"
        and metadata.get("claude_productization_integration_ok") == "true"
        and metadata.get("claude_integration_sidecar_excluded_from_completion") == "false"
    )
    return _decision(
        rule,
        ok=ok,
        source="ClaudeProductizationIntegrationReport.metadata",
        subject="fallback_control",
        observed=str(ok).lower(),
        expected="true",
        message="Fallbacks do not mask integration failures.",
    )


def _evaluate_permission_semantics(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    ports = set()
    if inputs.runtime_context_report is not None:
        ports = {str(binding.port_kind) for binding in inputs.runtime_context_report.runtime_bindings}
    return _decision(
        rule,
        ok="permission_mode" in ports,
        source="RuntimeContextAssemblyReport.runtime_bindings",
        subject="permission_mode",
        observed=str("permission_mode" in ports).lower(),
        expected="true",
        message="Permission mode is bound as a RuntimeContext port.",
        metadata={"ports": ",".join(sorted(ports))},
    )


def _evaluate_tool_loop_semantics(rule: RuntimePolicyRule, inputs: RuntimePolicyInputs) -> RuntimePolicyDecision:
    report = inputs.runtime_context_report
    tool_bindings = tuple(report.tool_use_bindings) if report is not None else ()
    ok = bool(tool_bindings) and all(not binding.blocking for binding in tool_bindings)
    return _decision(
        rule,
        ok=ok,
        source="RuntimeContextAssemblyReport.tool_use_bindings",
        subject="tool_use_context",
        observed=str(len(tool_bindings)),
        expected=">=1",
        message="ToolUseContext bindings are present and non-blocking.",
    )


def _policy_findings(decisions: Iterable[RuntimePolicyDecision]) -> list[RuntimePolicyFinding]:
    findings: list[RuntimePolicyFinding] = []
    for decision in decisions:
        if decision.status == RuntimePolicyStatus.BLOCKED:
            findings.append(
                RuntimePolicyFinding(
                    severity=RuntimePolicySeverity.BLOCKER,
                    code=f"runtime_policy_{decision.rule.rule_id.replace('.', '_')}_blocked",
                    message=decision.message,
                    rule_id=decision.rule.rule_id,
                    dimension=decision.rule.dimension,
                )
            )
        elif decision.status == RuntimePolicyStatus.WARNING:
            findings.append(
                RuntimePolicyFinding(
                    severity=RuntimePolicySeverity.WARNING,
                    code=f"runtime_policy_{decision.rule.rule_id.replace('.', '_')}_warning",
                    message=decision.message,
                    rule_id=decision.rule.rule_id,
                    dimension=decision.rule.dimension,
                )
            )
    return findings
