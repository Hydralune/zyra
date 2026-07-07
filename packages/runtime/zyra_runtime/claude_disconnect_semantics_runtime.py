from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping

from zyra_core import EventRecord, EventType, now_iso

from .claude_runtime_context_ports import RuntimeContextAssemblyReport
from .claude_source_graph_crosswalk import ClaudeProductizationIntegrationReport
from .workers import WorkerRequest


class DisconnectSemanticsStatus(StrEnum):
    READY = "ready"
    WARNING = "warning"
    BLOCKED = "blocked"


class DisconnectSemanticsSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class DisconnectEffectKind(StrEnum):
    PRE_TOOL_BLOCK = "pre_tool_block"
    TOOL_LOOP_BLOCK = "tool_loop_block"
    CONTEXT_GATE_BLOCK = "context_gate_block"
    DOWNSTREAM_HANDOFF_BLOCK = "downstream_handoff_block"
    QUERY_ENGINE_BLOCK = "query_engine_block"
    AUDIT_BLOCK = "audit_block"


class DisconnectProofSurface(StrEnum):
    SOURCE_GRAPH_CROSSWALK = "source_graph_crosswalk"
    RUNTIME_CONTEXT_PORT = "runtime_context_port"
    TOOL_USE_CONTEXT = "tool_use_context"
    DOWNSTREAM_CONTRACT = "downstream_contract"
    QUERY_ENGINE = "query_engine"
    SOURCE_GRAPH_AUDIT = "source_graph_audit"


@dataclass(frozen=True, slots=True)
class DisconnectConstraint:
    key: str
    value: Any
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class DisconnectExpectedEffect:
    effect_kind: DisconnectEffectKind
    expected_error: str
    tool_execution_allowed: bool
    expected_event_phase: str
    expected_metadata_key: str
    expected_metadata_value: str
    semantic_claim: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_kind": str(self.effect_kind),
            "expected_error": self.expected_error,
            "tool_execution_allowed": self.tool_execution_allowed,
            "expected_event_phase": self.expected_event_phase,
            "expected_metadata_key": self.expected_metadata_key,
            "expected_metadata_value": self.expected_metadata_value,
            "semantic_claim": self.semantic_claim,
        }


@dataclass(frozen=True, slots=True)
class DisconnectScenarioPlan:
    scenario_id: str
    proof_surface: DisconnectProofSurface
    disabled_constraints: tuple[DisconnectConstraint, ...]
    expected_effect: DisconnectExpectedEffect
    proves_module: str
    source_graph_batch: str
    required_test: str
    owner_slice: str
    status: DisconnectSemanticsStatus

    @property
    def expected_error(self) -> str:
        return self.expected_effect.expected_error

    def constraints_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for constraint in self.disabled_constraints:
            payload[constraint.key] = constraint.value
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "proof_surface": str(self.proof_surface),
            "disabled_constraints": [constraint.to_dict() for constraint in self.disabled_constraints],
            "constraints_payload": self.constraints_payload(),
            "expected_effect": self.expected_effect.to_dict(),
            "expected_error": self.expected_error,
            "proves_module": self.proves_module,
            "source_graph_batch": self.source_graph_batch,
            "required_test": self.required_test,
            "owner_slice": self.owner_slice,
            "status": str(self.status),
        }


@dataclass(frozen=True, slots=True)
class DisconnectCoverageRow:
    proof_surface: DisconnectProofSurface
    scenario_count: int
    expected_errors: tuple[str, ...]
    owner_slices: tuple[str, ...]
    tests: tuple[str, ...]
    status: DisconnectSemanticsStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "proof_surface": str(self.proof_surface),
            "scenario_count": self.scenario_count,
            "expected_errors": list(self.expected_errors),
            "owner_slices": list(self.owner_slices),
            "tests": list(self.tests),
            "status": str(self.status),
        }


@dataclass(frozen=True, slots=True)
class DisconnectSemanticsFinding:
    severity: DisconnectSemanticsSeverity
    code: str
    message: str
    scenario_id: str = ""
    proof_surface: DisconnectProofSurface | str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == DisconnectSemanticsSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "scenario_id": self.scenario_id,
            "proof_surface": str(self.proof_surface),
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class DisconnectSemanticsReport:
    ok: bool
    checked_at: str
    contract_id: str
    owner_slice: str
    scenarios: tuple[DisconnectScenarioPlan, ...]
    coverage: tuple[DisconnectCoverageRow, ...]
    findings: tuple[DisconnectSemanticsFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[DisconnectSemanticsFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[DisconnectSemanticsFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == DisconnectSemanticsSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    def metadata(self) -> dict[str, str]:
        return {
            "disconnect_semantics_ok": str(self.ok).lower(),
            "disconnect_semantics_scenarios": str(len(self.scenarios)),
            "disconnect_semantics_coverage_rows": str(len(self.coverage)),
            "disconnect_semantics_blockers": str(len(self.blockers)),
            "disconnect_semantics_warnings": str(len(self.warnings)),
            "disconnect_semantics_first_blocker": self.first_blocker_code,
            "disconnect_semantics_contract_id": self.contract_id,
            "disconnect_semantics_owner_slice": self.owner_slice,
        }

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": "disconnect_semantics",
            "ok": self.ok,
            "contract_id": self.contract_id,
            "owner_slice": self.owner_slice,
            "scenario_count": len(self.scenarios),
            "coverage_rows": len(self.coverage),
            "blocking_error": self.first_blocker_code,
            "blockers": [finding.to_dict() for finding in self.blockers],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "contract_id": self.contract_id,
            "owner_slice": self.owner_slice,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "coverage": [row.to_dict() for row in self.coverage],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


def build_disconnect_semantics_report(
    *,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport | None = None,
) -> DisconnectSemanticsReport:
    scenarios = default_disconnect_scenario_plans()
    coverage = tuple(_coverage_rows(scenarios))
    findings = [
        *_scenario_findings(scenarios),
        *_coverage_findings(coverage),
        *_integration_findings(integration_report),
        *_runtime_context_findings(runtime_context_report),
    ]
    ok = integration_report.ok and (runtime_context_report.ok if runtime_context_report else True) and not any(
        finding.blocking for finding in findings
    )
    return DisconnectSemanticsReport(
        ok=ok,
        checked_at=now_iso(),
        contract_id=integration_report.crosswalk.contract_id,
        owner_slice=integration_report.crosswalk.owner_slice,
        scenarios=scenarios,
        coverage=coverage,
        findings=tuple(findings),
    )


def disconnect_semantics_event(request: WorkerRequest, report: DisconnectSemanticsReport) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.CONSTRAINT_CHECK,
        payload={"claude_disconnect_semantics": report.event_payload()},
    )


def disconnect_semantics_markdown(report: DisconnectSemanticsReport) -> str:
    lines = [
        "## Disconnect Semantics",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- scenarios: `{len(report.scenarios)}`",
        f"- coverage_rows: `{len(report.coverage)}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Scenarios",
        "",
    ]
    for scenario in report.scenarios:
        lines.append(
            f"- `{scenario.scenario_id}` surface=`{scenario.proof_surface}` expected=`{scenario.expected_error}` tool_allowed=`{str(scenario.expected_effect.tool_execution_allowed).lower()}`"
        )
    if report.blockers:
        lines.extend(["", "### Disconnect Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    return "\n".join(lines) + "\n"


def assert_disconnect_semantics_ready(report: DisconnectSemanticsReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"Disconnect semantics report is not ready: {blockers}")


def default_disconnect_scenario_plans() -> tuple[DisconnectScenarioPlan, ...]:
    return (
        DisconnectScenarioPlan(
            scenario_id="disconnect.source_graph_crosswalk",
            proof_surface=DisconnectProofSurface.SOURCE_GRAPH_CROSSWALK,
            disabled_constraints=(
                DisconnectConstraint(
                    key="disable_source_graph_crosswalk",
                    value=True,
                    description="Disable the source graph crosswalk gate before worker startup.",
                ),
            ),
            expected_effect=DisconnectExpectedEffect(
                effect_kind=DisconnectEffectKind.PRE_TOOL_BLOCK,
                expected_error="source_graph_crosswalk_disabled",
                tool_execution_allowed=False,
                expected_event_phase="integration_gate_blocked",
                expected_metadata_key="claude_productization_integration_ok",
                expected_metadata_value="false",
                semantic_claim="Without the Zyra-owned crosswalk, CodeWorker cannot prove source-to-target ownership and must not execute tools.",
            ),
            proves_module="ClaudeSourceGraphCrosswalk",
            source_graph_batch="batch-01-query-session-context",
            required_test="tests/integration/test_claude_productization_integration.py::test_disabling_source_graph_blocks_before_tool_execution",
            owner_slice="M1-02A-02",
            status=DisconnectSemanticsStatus.READY,
        ),
        DisconnectScenarioPlan(
            scenario_id="disconnect.runtime_context_source_graph_port",
            proof_surface=DisconnectProofSurface.RUNTIME_CONTEXT_PORT,
            disabled_constraints=(
                DisconnectConstraint(
                    key="disabled_runtime_context_ports",
                    value=("source_graph",),
                    description="Disable the RuntimeContext source_graph port.",
                ),
            ),
            expected_effect=DisconnectExpectedEffect(
                effect_kind=DisconnectEffectKind.CONTEXT_GATE_BLOCK,
                expected_error="runtime_context_source_graph_port_disabled",
                tool_execution_allowed=False,
                expected_event_phase="integration_gate_blocked",
                expected_metadata_key="source_graph_first_blocker",
                expected_metadata_value="runtime_context_source_graph_port_disabled",
                semantic_claim="Source graph must be a required RuntimeContext port; disabling it changes execution behavior before tool writes.",
            ),
            proves_module="RuntimeContextPortContract.source_graph",
            source_graph_batch="batch-01-query-session-context",
            required_test="tests/integration/test_claude_productization_integration.py::test_disabling_required_runtime_context_port_blocks_before_tool_execution",
            owner_slice="M1-02A-02",
            status=DisconnectSemanticsStatus.READY,
        ),
        DisconnectScenarioPlan(
            scenario_id="disconnect.tool_use_context",
            proof_surface=DisconnectProofSurface.TOOL_USE_CONTEXT,
            disabled_constraints=(
                DisconnectConstraint(
                    key="disable_tool_use_context_port",
                    value=True,
                    description="Disable ToolUseContext registry/executor/budget ports as a group.",
                ),
            ),
            expected_effect=DisconnectExpectedEffect(
                effect_kind=DisconnectEffectKind.TOOL_LOOP_BLOCK,
                expected_error="runtime_context_tool_registry_port_disabled",
                tool_execution_allowed=False,
                expected_event_phase="integration_gate_blocked",
                expected_metadata_key="source_graph_first_blocker",
                expected_metadata_value="runtime_context_tool_registry_port_disabled",
                semantic_claim="ToolUseContext is not a static interface; disabling it blocks the tool loop before tool execution.",
            ),
            proves_module="ToolUseContextPortContract.default",
            source_graph_batch="batch-02-query-tool-loop",
            required_test="tests/integration/test_claude_productization_integration.py::test_disabling_tool_use_context_port_blocks_before_tool_execution",
            owner_slice="M1-02A-02",
            status=DisconnectSemanticsStatus.READY,
        ),
        DisconnectScenarioPlan(
            scenario_id="disconnect.downstream_contracts",
            proof_surface=DisconnectProofSurface.DOWNSTREAM_CONTRACT,
            disabled_constraints=(
                DisconnectConstraint(
                    key="disable_downstream_contracts",
                    value=True,
                    description="Disable all downstream handoff contracts.",
                ),
            ),
            expected_effect=DisconnectExpectedEffect(
                effect_kind=DisconnectEffectKind.DOWNSTREAM_HANDOFF_BLOCK,
                expected_error="downstream_contracts_disabled",
                tool_execution_allowed=False,
                expected_event_phase="integration_gate_blocked",
                expected_metadata_key="source_graph_first_blocker",
                expected_metadata_value="downstream_contracts_disabled",
                semantic_claim="02A-02 must expose handoff contracts for later slices; disabling them invalidates current startup.",
            ),
            proves_module="DownstreamContract registry",
            source_graph_batch="batch-01-query-session-context",
            required_test="tests/integration/test_claude_productization_integration.py::test_crosswalk_reports_disabled_batch_and_downstream_contracts_as_blockers",
            owner_slice="M1-02A-02",
            status=DisconnectSemanticsStatus.READY,
        ),
        DisconnectScenarioPlan(
            scenario_id="disconnect.productized_query_engine",
            proof_surface=DisconnectProofSurface.QUERY_ENGINE,
            disabled_constraints=(
                DisconnectConstraint(
                    key="disable_productized_runtime",
                    value=True,
                    description="Disable the Zyra-owned QueryEngine runtime shell.",
                ),
            ),
            expected_effect=DisconnectExpectedEffect(
                effect_kind=DisconnectEffectKind.QUERY_ENGINE_BLOCK,
                expected_error="productized_query_engine_runtime_disabled",
                tool_execution_allowed=False,
                expected_event_phase="agent_message",
                expected_metadata_key="sidecar_contracts_used",
                expected_metadata_value="false",
                semantic_claim="The default CodeWorker path depends on ZyraClaudeQueryEngine, not sidecar contracts or source-pool runtime.",
            ),
            proves_module="ZyraClaudeQueryEngine/CodeWorkerRuntime",
            source_graph_batch="batch-01-query-session-context",
            required_test="tests/integration/test_code_worker_clean_productized_runtime.py::test_disconnecting_productized_query_engine_fails_default_task",
            owner_slice="M1-02A",
            status=DisconnectSemanticsStatus.READY,
        ),
        DisconnectScenarioPlan(
            scenario_id="disconnect.source_graph_audit",
            proof_surface=DisconnectProofSurface.SOURCE_GRAPH_AUDIT,
            disabled_constraints=(
                DisconnectConstraint(
                    key="disable_source_graph_audit",
                    value=True,
                    description="Reserved control for future mutation tests that bypass the audit gate.",
                ),
            ),
            expected_effect=DisconnectExpectedEffect(
                effect_kind=DisconnectEffectKind.AUDIT_BLOCK,
                expected_error="claude_source_graph_audit_failed",
                tool_execution_allowed=False,
                expected_event_phase="source_graph_audit",
                expected_metadata_key="source_graph_audit_ok",
                expected_metadata_value="false",
                semantic_claim="Source graph audit must be a runtime gate; bypassing it should be detectable by M3 mutation audit.",
            ),
            proves_module="ClaudeSourceGraphAuditRuntime",
            source_graph_batch="batch-01-query-session-context",
            required_test="tests/integration/test_claude_productization_integration.py",
            owner_slice="M1-02A-02",
            status=DisconnectSemanticsStatus.WARNING,
        ),
    )


def scenario_payloads_by_error(report: DisconnectSemanticsReport) -> dict[str, dict[str, Any]]:
    return {scenario.expected_error: scenario.constraints_payload() for scenario in report.scenarios}


def scenario_ids_by_surface(report: DisconnectSemanticsReport) -> dict[str, list[str]]:
    by_surface: dict[str, list[str]] = {}
    for scenario in report.scenarios:
        by_surface.setdefault(str(scenario.proof_surface), []).append(scenario.scenario_id)
    return by_surface


def _coverage_rows(scenarios: Iterable[DisconnectScenarioPlan]) -> list[DisconnectCoverageRow]:
    by_surface: dict[DisconnectProofSurface, list[DisconnectScenarioPlan]] = {}
    for scenario in scenarios:
        by_surface.setdefault(scenario.proof_surface, []).append(scenario)
    rows: list[DisconnectCoverageRow] = []
    for surface, surface_scenarios in sorted(by_surface.items(), key=lambda item: str(item[0])):
        if any(scenario.status == DisconnectSemanticsStatus.BLOCKED for scenario in surface_scenarios):
            status = DisconnectSemanticsStatus.BLOCKED
        elif any(scenario.status == DisconnectSemanticsStatus.WARNING for scenario in surface_scenarios):
            status = DisconnectSemanticsStatus.WARNING
        else:
            status = DisconnectSemanticsStatus.READY
        rows.append(
            DisconnectCoverageRow(
                proof_surface=surface,
                scenario_count=len(surface_scenarios),
                expected_errors=tuple(dict.fromkeys(scenario.expected_error for scenario in surface_scenarios)),
                owner_slices=tuple(dict.fromkeys(scenario.owner_slice for scenario in surface_scenarios)),
                tests=tuple(dict.fromkeys(scenario.required_test for scenario in surface_scenarios)),
                status=status,
            )
        )
    return rows


def _scenario_findings(scenarios: Iterable[DisconnectScenarioPlan]) -> list[DisconnectSemanticsFinding]:
    findings: list[DisconnectSemanticsFinding] = []
    seen_errors: dict[str, str] = {}
    for scenario in scenarios:
        if not scenario.disabled_constraints:
            findings.append(
                DisconnectSemanticsFinding(
                    severity=DisconnectSemanticsSeverity.BLOCKER,
                    code="disconnect_scenario_has_no_disabled_constraints",
                    message=f"Disconnect scenario {scenario.scenario_id} has no disabled constraints.",
                    scenario_id=scenario.scenario_id,
                    proof_surface=scenario.proof_surface,
                )
            )
        if not scenario.expected_error:
            findings.append(
                DisconnectSemanticsFinding(
                    severity=DisconnectSemanticsSeverity.BLOCKER,
                    code="disconnect_scenario_expected_error_missing",
                    message=f"Disconnect scenario {scenario.scenario_id} has no expected error.",
                    scenario_id=scenario.scenario_id,
                    proof_surface=scenario.proof_surface,
                )
            )
        previous = seen_errors.get(scenario.expected_error)
        if previous and previous != scenario.scenario_id:
            findings.append(
                DisconnectSemanticsFinding(
                    severity=DisconnectSemanticsSeverity.WARNING,
                    code="disconnect_expected_error_reused",
                    message=f"Expected error {scenario.expected_error} is shared by {previous} and {scenario.scenario_id}.",
                    scenario_id=scenario.scenario_id,
                    proof_surface=scenario.proof_surface,
                )
            )
        seen_errors[scenario.expected_error] = scenario.scenario_id
        if not scenario.required_test:
            findings.append(
                DisconnectSemanticsFinding(
                    severity=DisconnectSemanticsSeverity.BLOCKER,
                    code="disconnect_scenario_test_missing",
                    message=f"Disconnect scenario {scenario.scenario_id} has no required test.",
                    scenario_id=scenario.scenario_id,
                    proof_surface=scenario.proof_surface,
                )
            )
    return findings


def _coverage_findings(rows: Iterable[DisconnectCoverageRow]) -> list[DisconnectSemanticsFinding]:
    required = {
        DisconnectProofSurface.SOURCE_GRAPH_CROSSWALK,
        DisconnectProofSurface.RUNTIME_CONTEXT_PORT,
        DisconnectProofSurface.TOOL_USE_CONTEXT,
        DisconnectProofSurface.DOWNSTREAM_CONTRACT,
        DisconnectProofSurface.QUERY_ENGINE,
    }
    actual = {row.proof_surface for row in rows}
    findings = [
        DisconnectSemanticsFinding(
            severity=DisconnectSemanticsSeverity.BLOCKER,
            code="disconnect_surface_coverage_missing",
            message=f"Disconnect semantics coverage is missing {surface}.",
            proof_surface=surface,
        )
        for surface in sorted(required - actual, key=str)
    ]
    for row in rows:
        if row.status == DisconnectSemanticsStatus.BLOCKED:
            findings.append(
                DisconnectSemanticsFinding(
                    severity=DisconnectSemanticsSeverity.BLOCKER,
                    code="disconnect_surface_blocked",
                    message=f"Disconnect surface {row.proof_surface} has a blocked scenario.",
                    proof_surface=row.proof_surface,
                )
            )
    return findings


def _integration_findings(report: ClaudeProductizationIntegrationReport) -> list[DisconnectSemanticsFinding]:
    if report.ok:
        return []
    return [
        DisconnectSemanticsFinding(
            severity=DisconnectSemanticsSeverity.BLOCKER,
            code=report.blocking_error or "disconnect_integration_blocked",
            message="Disconnect semantics cannot be trusted while integration report is blocked.",
            proof_surface=DisconnectProofSurface.SOURCE_GRAPH_CROSSWALK,
        )
    ]


def _runtime_context_findings(report: RuntimeContextAssemblyReport | None) -> list[DisconnectSemanticsFinding]:
    if report is None or report.ok:
        return []
    return [
        DisconnectSemanticsFinding(
            severity=DisconnectSemanticsSeverity.BLOCKER,
            code=report.first_blocker_code or "disconnect_runtime_context_blocked",
            message="Disconnect semantics cannot be trusted while RuntimeContext assembly is blocked.",
            proof_surface=DisconnectProofSurface.RUNTIME_CONTEXT_PORT,
        )
    ]
