from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from zyra_core import EventRecord, EventType, now_iso

from .claude_runtime_context_ports import RuntimeContextAssemblyReport
from .claude_runtime_contracts import ClaudeRuntimeContractBundle
from .claude_downstream_handoff_runtime import DownstreamHandoffReport, build_downstream_handoff_report
from .claude_disconnect_semantics_runtime import DisconnectSemanticsReport, build_disconnect_semantics_report
from .claude_event_contract_runtime import EventContractRuntimeReport, build_event_contract_runtime_report
from .claude_productization_review_runtime import ProductizationReviewReport, build_productization_review_report
from .claude_api_inventory_contract_runtime import ApiInventoryContractReport, build_api_inventory_contract_report
from .claude_runtime_policy_matrix import (
    RuntimePolicyInputs,
    RuntimePolicyMatrixReport,
    build_runtime_policy_matrix_report,
)
from .claude_state_custody_runtime import StateCustodyRuntimeReport, build_state_custody_runtime_report
from .claude_source_graph_crosswalk import (
    ClaudeProductizationIntegrationReport,
    ClaudeSourceGraphBatch,
    CrosswalkDecision,
    EventContractPhase,
    RuntimePortKind,
)
from .workers import WorkerRequest


class SourceGraphAuditStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    BLOCKED = "blocked"


class SourceGraphAuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class SourceGraphEvidenceKind(StrEnum):
    SOURCE_BATCH = "source_batch"
    ZYRA_TARGET = "zyra_target"
    RUNTIME_PORT = "runtime_port"
    DOWNSTREAM_CONTRACT = "downstream_contract"
    EVENT_CONTRACT = "event_contract"
    CLEAN_BOUNDARY = "clean_boundary"
    DISCONNECT_SCENARIO = "disconnect_scenario"
    RUNTIME_POLICY = "runtime_policy"
    STATE_CUSTODY = "state_custody"
    API_INVENTORY = "api_inventory"
    LINE_BUCKET_RULE = "line_bucket_rule"


class ProductizationLineBucket(StrEnum):
    PRODUCTION_INTERNALIZED = "production_internalized"
    TEST_BEHAVIOR = "test_behavior"
    SCRIPT_VERIFICATION = "script_verification"
    DOCS_SELF_REVIEW = "docs_self_review"
    DATA_LEDGER = "data_ledger"
    VENDOR_LIKE = "vendor_like"
    MOCK_FIXTURE = "mock_fixture"
    ADAPTER_ONLY = "adapter_only"


@dataclass(frozen=True, slots=True)
class SourceGraphAuditFinding:
    severity: SourceGraphAuditSeverity
    code: str
    message: str
    evidence_kind: SourceGraphEvidenceKind
    subject: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == SourceGraphAuditSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "evidence_kind": str(self.evidence_kind),
            "subject": self.subject,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class BatchCoverageRow:
    batch: ClaudeSourceGraphBatch
    owner_slice: str
    downstream_slices: tuple[str, ...]
    source_ref_count: int
    zyra_target_count: int
    current_slice_target_count: int
    runtime_port_count: int
    event_phase_count: int
    decision: CrosswalkDecision
    status: SourceGraphAuditStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch": str(self.batch),
            "owner_slice": self.owner_slice,
            "downstream_slices": list(self.downstream_slices),
            "source_ref_count": self.source_ref_count,
            "zyra_target_count": self.zyra_target_count,
            "current_slice_target_count": self.current_slice_target_count,
            "runtime_port_count": self.runtime_port_count,
            "event_phase_count": self.event_phase_count,
            "decision": str(self.decision),
            "status": str(self.status),
        }


@dataclass(frozen=True, slots=True)
class RuntimePortAuditRow:
    port_id: str
    port_kind: RuntimePortKind | str
    owner_slice: str
    required_for_shell: bool
    binding_status: str
    active_binding: bool
    producer: str
    consumer: str
    state_owner: str
    event_phases: tuple[str, ...]
    status: SourceGraphAuditStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "port_id": self.port_id,
            "port_kind": str(self.port_kind),
            "owner_slice": self.owner_slice,
            "required_for_shell": self.required_for_shell,
            "binding_status": self.binding_status,
            "active_binding": self.active_binding,
            "producer": self.producer,
            "consumer": self.consumer,
            "state_owner": self.state_owner,
            "event_phases": list(self.event_phases),
            "status": str(self.status),
        }


@dataclass(frozen=True, slots=True)
class DownstreamHandoffRow:
    contract_id: str
    owner_slice: str
    target_unit: str
    capability: str
    source_batches: tuple[str, ...]
    required_ports: tuple[str, ...]
    required_events: tuple[str, ...]
    required_targets: tuple[str, ...]
    acceptance_test_entrypoints: tuple[str, ...]
    status: SourceGraphAuditStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "owner_slice": self.owner_slice,
            "target_unit": self.target_unit,
            "capability": self.capability,
            "source_batches": list(self.source_batches),
            "required_ports": list(self.required_ports),
            "required_events": list(self.required_events),
            "required_targets": list(self.required_targets),
            "acceptance_test_entrypoints": list(self.acceptance_test_entrypoints),
            "status": str(self.status),
        }


@dataclass(frozen=True, slots=True)
class EventContractAuditRow:
    phase: EventContractPhase
    owner_slice: str
    payload_key: str
    producer: str
    consumer: str
    currently_emitted: bool
    observed_in_runtime_context: bool
    status: SourceGraphAuditStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "owner_slice": self.owner_slice,
            "payload_key": self.payload_key,
            "producer": self.producer,
            "consumer": self.consumer,
            "currently_emitted": self.currently_emitted,
            "observed_in_runtime_context": self.observed_in_runtime_context,
            "status": str(self.status),
        }


@dataclass(frozen=True, slots=True)
class DisconnectScenario:
    scenario_id: str
    disabled_constraint: dict[str, Any]
    expected_error: str
    expected_effect: str
    proves_module: str
    required_test: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "disabled_constraint": dict(self.disabled_constraint),
            "expected_error": self.expected_error,
            "expected_effect": self.expected_effect,
            "proves_module": self.proves_module,
            "required_test": self.required_test,
        }


@dataclass(frozen=True, slots=True)
class EffectiveLineBucketRule:
    bucket: ProductizationLineBucket
    counts_as_effective_production: bool
    allowed_roots: tuple[str, ...]
    excluded_patterns: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": str(self.bucket),
            "counts_as_effective_production": self.counts_as_effective_production,
            "allowed_roots": list(self.allowed_roots),
            "excluded_patterns": list(self.excluded_patterns),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class SourceGraphAuditReport:
    ok: bool
    checked_at: str
    contract_id: str
    owner_slice: str
    batch_coverage: tuple[BatchCoverageRow, ...]
    runtime_ports: tuple[RuntimePortAuditRow, ...]
    downstream_handoffs: tuple[DownstreamHandoffRow, ...]
    downstream_handoff_report: DownstreamHandoffReport
    event_contracts: tuple[EventContractAuditRow, ...]
    event_contract_runtime_report: EventContractRuntimeReport
    productization_review_report: ProductizationReviewReport
    disconnect_semantics_report: DisconnectSemanticsReport
    state_custody_runtime_report: StateCustodyRuntimeReport
    api_inventory_contract_report: ApiInventoryContractReport
    runtime_policy_matrix_report: RuntimePolicyMatrixReport
    disconnect_scenarios: tuple[DisconnectScenario, ...]
    line_bucket_rules: tuple[EffectiveLineBucketRule, ...]
    findings: tuple[SourceGraphAuditFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[SourceGraphAuditFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[SourceGraphAuditFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == SourceGraphAuditSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    def metadata(self) -> dict[str, str]:
        return {
            "source_graph_audit_ok": str(self.ok).lower(),
            "source_graph_audit_blockers": str(len(self.blockers)),
            "source_graph_audit_warnings": str(len(self.warnings)),
            "source_graph_audit_first_blocker": self.first_blocker_code,
            "source_graph_audit_batch_rows": str(len(self.batch_coverage)),
            "source_graph_audit_runtime_ports": str(len(self.runtime_ports)),
            "source_graph_audit_downstream_handoffs": str(len(self.downstream_handoffs)),
            "source_graph_audit_handoff_owners": str(self.downstream_handoff_report.owner_count),
            "source_graph_audit_handoff_edges": str(len(self.downstream_handoff_report.dependency_edges)),
            "source_graph_audit_event_contracts": str(len(self.event_contracts)),
            "source_graph_audit_event_runtime_ok": str(self.event_contract_runtime_report.ok).lower(),
            "source_graph_audit_event_runtime_observed": self.event_contract_runtime_report.metadata()["event_contract_runtime_observed"],
            "source_graph_audit_productization_review_ok": str(self.productization_review_report.ok).lower(),
            "source_graph_audit_productization_review_blockers": str(len(self.productization_review_report.blockers)),
            "source_graph_audit_disconnect_scenarios": str(len(self.disconnect_scenarios)),
            "source_graph_audit_disconnect_semantics_ok": str(self.disconnect_semantics_report.ok).lower(),
            "source_graph_audit_disconnect_semantics_surfaces": str(len(self.disconnect_semantics_report.coverage)),
            "source_graph_audit_state_custody_ok": str(self.state_custody_runtime_report.ok).lower(),
            "source_graph_audit_state_custody_observations": str(len(self.state_custody_runtime_report.observations)),
            "source_graph_audit_state_custody_blockers": str(len(self.state_custody_runtime_report.blockers)),
            "source_graph_audit_api_inventory_ok": str(self.api_inventory_contract_report.ok).lower(),
            "source_graph_audit_api_inventory_fields": str(len(self.api_inventory_contract_report.fields)),
            "source_graph_audit_api_inventory_blockers": str(len(self.api_inventory_contract_report.blockers)),
            "source_graph_audit_runtime_policy_ok": str(self.runtime_policy_matrix_report.ok).lower(),
            "source_graph_audit_runtime_policy_rules": str(len(self.runtime_policy_matrix_report.decisions)),
            "source_graph_audit_runtime_policy_blockers": str(len(self.runtime_policy_matrix_report.blockers)),
            "source_graph_audit_line_bucket_rules": str(len(self.line_bucket_rules)),
            "source_graph_audit_contract_id": self.contract_id,
            "source_graph_audit_owner_slice": self.owner_slice,
        }

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": "source_graph_audit",
            "ok": self.ok,
            "contract_id": self.contract_id,
            "owner_slice": self.owner_slice,
            "batch_rows": len(self.batch_coverage),
            "runtime_ports": len(self.runtime_ports),
            "downstream_handoffs": len(self.downstream_handoffs),
            "event_contracts": len(self.event_contracts),
            "disconnect_scenarios": len(self.disconnect_scenarios),
            "state_custody_observations": len(self.state_custody_runtime_report.observations),
            "api_inventory_fields": len(self.api_inventory_contract_report.fields),
            "runtime_policy_rules": len(self.runtime_policy_matrix_report.decisions),
            "runtime_policy_blockers": [finding.to_dict() for finding in self.runtime_policy_matrix_report.blockers],
            "blocking_error": self.first_blocker_code,
            "blockers": [finding.to_dict() for finding in self.blockers],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "contract_id": self.contract_id,
            "owner_slice": self.owner_slice,
            "batch_coverage": [row.to_dict() for row in self.batch_coverage],
            "runtime_ports": [row.to_dict() for row in self.runtime_ports],
            "downstream_handoffs": [row.to_dict() for row in self.downstream_handoffs],
            "downstream_handoff_report": self.downstream_handoff_report.to_dict(),
            "event_contracts": [row.to_dict() for row in self.event_contracts],
            "event_contract_runtime_report": self.event_contract_runtime_report.to_dict(),
            "productization_review_report": self.productization_review_report.to_dict(),
            "disconnect_semantics_report": self.disconnect_semantics_report.to_dict(),
            "state_custody_runtime_report": self.state_custody_runtime_report.to_dict(),
            "api_inventory_contract_report": self.api_inventory_contract_report.to_dict(),
            "runtime_policy_matrix_report": self.runtime_policy_matrix_report.to_dict(),
            "disconnect_scenarios": [scenario.to_dict() for scenario in self.disconnect_scenarios],
            "line_bucket_rules": [rule.to_dict() for rule in self.line_bucket_rules],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


def build_claude_source_graph_audit(
    *,
    project_root: str | Path,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_contracts: ClaudeRuntimeContractBundle,
    runtime_context_report: RuntimeContextAssemblyReport | None = None,
) -> SourceGraphAuditReport:
    project_path = Path(project_root).resolve()
    batch_rows = _batch_rows(integration_report)
    runtime_rows = _runtime_port_rows(integration_report, runtime_context_report)
    downstream_rows = _downstream_rows(integration_report)
    downstream_report = build_downstream_handoff_report(
        integration_report=integration_report,
        runtime_context_report=runtime_context_report,
    )
    event_rows = _event_rows(integration_report, runtime_context_report)
    event_runtime_report = build_event_contract_runtime_report(
        integration_report=integration_report,
        runtime_context_report=runtime_context_report,
    )
    productization_review = build_productization_review_report(
        project_root=project_root,
        integration_report=integration_report,
        runtime_contracts=runtime_contracts,
        runtime_context_report=runtime_context_report,
    )
    disconnect_semantics = build_disconnect_semantics_report(
        integration_report=integration_report,
        runtime_context_report=runtime_context_report,
    )
    state_custody_runtime = build_state_custody_runtime_report(
        project_root=project_root,
        integration_report=integration_report,
        runtime_contracts=runtime_contracts,
        runtime_context_report=runtime_context_report,
    )
    api_inventory_contract = build_api_inventory_contract_report(
        project_root=project_root,
        contracts=runtime_contracts,
        integration_report=integration_report,
        runtime_context_report=runtime_context_report,
    )
    runtime_policy_matrix = build_runtime_policy_matrix_report(
        RuntimePolicyInputs(
            integration_report=integration_report,
            runtime_context_report=runtime_context_report,
            runtime_contracts=runtime_contracts,
            downstream_handoff_report=downstream_report,
            event_contract_report=event_runtime_report,
            productization_review_report=productization_review,
            disconnect_semantics_report=disconnect_semantics,
        )
    )
    scenarios = default_disconnect_scenarios()
    bucket_rules = default_line_bucket_rules()
    findings = [
        *_integration_findings(integration_report),
        *_batch_findings(batch_rows),
        *_target_findings(project_path, integration_report),
        *_runtime_port_findings(runtime_rows),
        *_downstream_findings(downstream_rows),
        *_handoff_report_findings(downstream_report),
        *_event_runtime_findings(event_runtime_report),
        *_productization_review_findings(productization_review),
        *_disconnect_semantics_findings(disconnect_semantics),
        *_state_custody_runtime_findings(state_custody_runtime),
        *_api_inventory_contract_findings(api_inventory_contract),
        *_runtime_policy_findings(runtime_policy_matrix),
        *_bucket_rule_findings(bucket_rules),
    ]
    ok = integration_report.ok and (runtime_context_report.ok if runtime_context_report else True) and not any(
        finding.blocking for finding in findings
    )
    return SourceGraphAuditReport(
        ok=ok,
        checked_at=now_iso(),
        contract_id=integration_report.crosswalk.contract_id,
        owner_slice=integration_report.crosswalk.owner_slice,
        batch_coverage=batch_rows,
        runtime_ports=runtime_rows,
        downstream_handoffs=downstream_rows,
        downstream_handoff_report=downstream_report,
        event_contracts=event_rows,
        event_contract_runtime_report=event_runtime_report,
        productization_review_report=productization_review,
        disconnect_semantics_report=disconnect_semantics,
        state_custody_runtime_report=state_custody_runtime,
        api_inventory_contract_report=api_inventory_contract,
        runtime_policy_matrix_report=runtime_policy_matrix,
        disconnect_scenarios=scenarios,
        line_bucket_rules=bucket_rules,
        findings=tuple(findings),
    )


def claude_source_graph_audit_event(request: WorkerRequest, report: SourceGraphAuditReport) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.CONSTRAINT_CHECK,
        payload={"claude_source_graph_audit": report.event_payload()},
    )


def source_graph_audit_markdown(report: SourceGraphAuditReport) -> str:
    lines = [
        "## Source Graph Audit",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- contract_id: `{report.contract_id}`",
        f"- batch_rows: `{len(report.batch_coverage)}`",
        f"- runtime_ports: `{len(report.runtime_ports)}`",
        f"- downstream_handoffs: `{len(report.downstream_handoffs)}`",
        f"- event_contracts: `{len(report.event_contracts)}`",
        f"- disconnect_scenarios: `{len(report.disconnect_scenarios)}`",
        f"- state_custody_observations: `{len(report.state_custody_runtime_report.observations)}`",
        f"- api_inventory_fields: `{len(report.api_inventory_contract_report.fields)}`",
        f"- runtime_policy_rules: `{len(report.runtime_policy_matrix_report.decisions)}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Batch Coverage",
        "",
    ]
    for row in report.batch_coverage:
        lines.append(f"- `{row.batch}` owner=`{row.owner_slice}` targets=`{row.zyra_target_count}` status=`{row.status}`")
    lines.extend(["", "### Disconnect Scenarios", ""])
    for scenario in report.disconnect_scenarios:
        lines.append(f"- `{scenario.scenario_id}` expects `{scenario.expected_error}` proves `{scenario.proves_module}`")
    lines.extend(["", "### State Custody", ""])
    for row in report.state_custody_runtime_report.observations[:24]:
        lines.append(f"- `{row.state_key}` owner=`{row.observed_owner}` status=`{row.status}`")
    lines.extend(["", "### API Inventory Contract", ""])
    for row in report.api_inventory_contract_report.fields[:24]:
        lines.append(f"- `{row.payload_key}` status=`{row.status}` risk=`{row.risk}`")
    lines.extend(["", "### Runtime Policy Matrix", ""])
    for decision in report.runtime_policy_matrix_report.decisions:
        lines.append(
            f"- `{decision.rule.rule_id}` dimension=`{decision.rule.dimension}` "
            f"status=`{decision.status}` subject=`{decision.subject}`"
        )
    if report.blockers:
        lines.extend(["", "### Audit Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    if report.warnings:
        lines.extend(["", "### Audit Warnings", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.warnings[:12])
    return "\n".join(lines) + "\n"


def assert_claude_source_graph_audit_ready(report: SourceGraphAuditReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"Claude source graph audit failed: {blockers}")


def default_disconnect_scenarios() -> tuple[DisconnectScenario, ...]:
    return (
        DisconnectScenario(
            scenario_id="disconnect.source_graph_crosswalk",
            disabled_constraint={"disable_source_graph_crosswalk": True},
            expected_error="source_graph_crosswalk_disabled",
            expected_effect="CodeWorker stops before tool execution and emits integration_gate_blocked.",
            proves_module="ClaudeSourceGraphCrosswalk",
            required_test="tests/integration/test_claude_productization_integration.py::test_disabling_source_graph_blocks_before_tool_execution",
        ),
        DisconnectScenario(
            scenario_id="disconnect.runtime_context_source_graph_port",
            disabled_constraint={"disabled_runtime_context_ports": ["source_graph"]},
            expected_error="runtime_context_source_graph_port_disabled",
            expected_effect="CodeWorker stops before tool execution and does not write workspace files.",
            proves_module="RuntimeContextPortContract.source_graph",
            required_test="tests/integration/test_claude_productization_integration.py::test_disabling_required_runtime_context_port_blocks_before_tool_execution",
        ),
        DisconnectScenario(
            scenario_id="disconnect.tool_use_context",
            disabled_constraint={"disable_tool_use_context_port": True},
            expected_error="runtime_context_tool_registry_port_disabled",
            expected_effect="ToolUseContext cannot bind registry/executor/budget and execution is blocked.",
            proves_module="ToolUseContextPortContract.default",
            required_test="tests/integration/test_claude_productization_integration.py::test_disabling_tool_use_context_port_blocks_before_tool_execution",
        ),
        DisconnectScenario(
            scenario_id="disconnect.downstream_contracts",
            disabled_constraint={"disable_downstream_contracts": True},
            expected_error="downstream_contracts_disabled",
            expected_effect="02B/02C/02D/03A handoff cannot be proven and CodeWorker does not execute tools.",
            proves_module="DownstreamContract registry",
            required_test="tests/integration/test_claude_productization_integration.py::test_crosswalk_reports_disabled_batch_and_downstream_contracts_as_blockers",
        ),
        DisconnectScenario(
            scenario_id="disconnect.productized_query_engine",
            disabled_constraint={"disable_productized_runtime": True},
            expected_error="productized_query_engine_runtime_disabled",
            expected_effect="CodeWorker runtime shell is unavailable and no QueryEngine loop runs.",
            proves_module="ZyraClaudeQueryEngine/CodeWorkerRuntime",
            required_test="tests/integration/test_code_worker_clean_productized_runtime.py::test_disconnecting_productized_query_engine_fails_default_task",
        ),
    )


def default_line_bucket_rules() -> tuple[EffectiveLineBucketRule, ...]:
    return (
        EffectiveLineBucketRule(
            bucket=ProductizationLineBucket.PRODUCTION_INTERNALIZED,
            counts_as_effective_production=True,
            allowed_roots=("apps/", "packages/", "skills/", "scripts/"),
            excluded_patterns=("vendor/", "vendor-runtimes/", "source-pool/", "runtime-sources/", "fixtures/", "tests/"),
            rationale="Zyra-owned runtime modules, APIs, scripts and skill/runtime code reachable from the default path.",
        ),
        EffectiveLineBucketRule(
            bucket=ProductizationLineBucket.TEST_BEHAVIOR,
            counts_as_effective_production=False,
            allowed_roots=("tests/",),
            excluded_patterns=("snapshot-only", "fixture-only", "import-only"),
            rationale="Tests verify behavior and failure semantics but do not satisfy production line minimum.",
        ),
        EffectiveLineBucketRule(
            bucket=ProductizationLineBucket.SCRIPT_VERIFICATION,
            counts_as_effective_production=False,
            allowed_roots=("scripts/",),
            excluded_patterns=("inventory-only", "manifest-only"),
            rationale="Verification scripts are supporting evidence; only runtime-supporting code can count as production.",
        ),
        EffectiveLineBucketRule(
            bucket=ProductizationLineBucket.DOCS_SELF_REVIEW,
            counts_as_effective_production=False,
            allowed_roots=("docs/",),
            excluded_patterns=("*.md",),
            rationale="Docs and self-review records are required evidence but never effective production code.",
        ),
        EffectiveLineBucketRule(
            bucket=ProductizationLineBucket.DATA_LEDGER,
            counts_as_effective_production=False,
            allowed_roots=("docs/", "packages/"),
            excluded_patterns=("*.json", "*.yaml", "*.csv", "ledger", "manifest", "inventory"),
            rationale="Ledger/source map data cannot replace runtime behavior.",
        ),
        EffectiveLineBucketRule(
            bucket=ProductizationLineBucket.VENDOR_LIKE,
            counts_as_effective_production=False,
            allowed_roots=("vendor/", "vendor-runtimes/", "source-pool/", "runtime-sources/"),
            excluded_patterns=("*",),
            rationale="Vendored or source-pool code is excluded by definition for M1-02A-02.",
        ),
        EffectiveLineBucketRule(
            bucket=ProductizationLineBucket.MOCK_FIXTURE,
            counts_as_effective_production=False,
            allowed_roots=("tests/", "fixtures/"),
            excluded_patterns=("mock", "fixture", "golden", "snapshot"),
            rationale="Mock/fixture-only assets cannot prove source graph internalization.",
        ),
        EffectiveLineBucketRule(
            bucket=ProductizationLineBucket.ADAPTER_ONLY,
            counts_as_effective_production=False,
            allowed_roots=("packages/", "apps/"),
            excluded_patterns=("bridge-only", "launcher-only", "sidecar-only"),
            rationale="Thin adapters count only when backed by Zyra-owned runtime semantics, state and tests.",
        ),
    )


def _batch_rows(report: ClaudeProductizationIntegrationReport) -> tuple[BatchCoverageRow, ...]:
    rows: list[BatchCoverageRow] = []
    for batch in report.crosswalk.batches:
        status = SourceGraphAuditStatus.PASSING
        if not batch.source_refs or not batch.zyra_targets:
            status = SourceGraphAuditStatus.BLOCKED
        rows.append(
            BatchCoverageRow(
                batch=batch.batch,
                owner_slice=batch.owner_slice,
                downstream_slices=batch.downstream_slices,
                source_ref_count=len(batch.source_refs),
                zyra_target_count=len(batch.zyra_targets),
                current_slice_target_count=len(batch.current_slice_targets),
                runtime_port_count=len(batch.runtime_ports),
                event_phase_count=len(batch.event_phases),
                decision=batch.decision,
                status=status,
            )
        )
    return tuple(rows)


def _runtime_port_rows(
    report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport | None,
) -> tuple[RuntimePortAuditRow, ...]:
    bindings = {}
    if runtime_context_report is not None:
        bindings = {binding.port_id: binding for binding in runtime_context_report.runtime_bindings}
    rows: list[RuntimePortAuditRow] = []
    for port in report.crosswalk.runtime_context_ports:
        binding = bindings.get(port.port_id)
        if runtime_context_report is not None and port.required_for_runtime_shell and binding is None:
            status = SourceGraphAuditStatus.BLOCKED
        elif binding is not None and binding.blocking:
            status = SourceGraphAuditStatus.BLOCKED
        elif binding is None:
            status = SourceGraphAuditStatus.WARNING
        else:
            status = SourceGraphAuditStatus.PASSING
        rows.append(
            RuntimePortAuditRow(
                port_id=port.port_id,
                port_kind=port.kind,
                owner_slice=port.owner_slice,
                required_for_shell=port.required_for_runtime_shell,
                binding_status=str(binding.status) if binding else "not_observed",
                active_binding=bool(binding and binding.active_in_default_path),
                producer=port.producer,
                consumer=port.consumer,
                state_owner=port.state_owner,
                event_phases=tuple(str(phase) for phase in port.event_phases),
                status=status,
            )
        )
    return tuple(rows)


def _downstream_rows(report: ClaudeProductizationIntegrationReport) -> tuple[DownstreamHandoffRow, ...]:
    rows: list[DownstreamHandoffRow] = []
    for contract in report.crosswalk.downstream_contracts:
        status = SourceGraphAuditStatus.PASSING
        if not contract.required_ports or not contract.required_events or not contract.acceptance_test_entrypoints:
            status = SourceGraphAuditStatus.BLOCKED
        rows.append(
            DownstreamHandoffRow(
                contract_id=contract.contract_id,
                owner_slice=contract.owner_slice,
                target_unit=contract.target_unit,
                capability=contract.capability,
                source_batches=tuple(str(batch) for batch in contract.source_batches),
                required_ports=tuple(str(port) for port in contract.required_ports),
                required_events=tuple(str(phase) for phase in contract.required_events),
                required_targets=contract.required_targets,
                acceptance_test_entrypoints=contract.acceptance_test_entrypoints,
                status=status,
            )
        )
    return tuple(rows)


def _event_rows(
    report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport | None,
) -> tuple[EventContractAuditRow, ...]:
    observed = _observed_event_phases(runtime_context_report)
    rows: list[EventContractAuditRow] = []
    for contract in report.crosswalk.event_contracts:
        observed_in_runtime = str(contract.phase) in observed
        status = SourceGraphAuditStatus.PASSING
        if contract.currently_emitted and runtime_context_report is not None and not observed_in_runtime:
            status = SourceGraphAuditStatus.WARNING
        rows.append(
            EventContractAuditRow(
                phase=contract.phase,
                owner_slice=contract.owner_slice,
                payload_key=contract.payload_key,
                producer=contract.producer,
                consumer=contract.consumer,
                currently_emitted=contract.currently_emitted,
                observed_in_runtime_context=observed_in_runtime,
                status=status,
            )
        )
    return tuple(rows)


def _integration_findings(report: ClaudeProductizationIntegrationReport) -> list[SourceGraphAuditFinding]:
    if report.ok:
        return []
    return [
        SourceGraphAuditFinding(
            severity=SourceGraphAuditSeverity.BLOCKER,
            code=report.blocking_error or "integration_report_blocked",
            message="Source graph integration report is blocked.",
            evidence_kind=SourceGraphEvidenceKind.SOURCE_BATCH,
        )
    ]


def _batch_findings(rows: Iterable[BatchCoverageRow]) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    actual = {row.batch for row in rows}
    for missing in sorted(set(ClaudeSourceGraphBatch) - actual, key=str):
        findings.append(
            SourceGraphAuditFinding(
                severity=SourceGraphAuditSeverity.BLOCKER,
                code="audit_batch_missing",
                message=f"Audit coverage is missing source graph batch {missing}.",
                evidence_kind=SourceGraphEvidenceKind.SOURCE_BATCH,
                subject=str(missing),
            )
        )
    for row in rows:
        if row.status == SourceGraphAuditStatus.BLOCKED:
            findings.append(
                SourceGraphAuditFinding(
                    severity=SourceGraphAuditSeverity.BLOCKER,
                    code="audit_batch_blocked",
                    message=f"Source graph batch {row.batch} lacks sources or Zyra targets.",
                    evidence_kind=SourceGraphEvidenceKind.SOURCE_BATCH,
                    subject=str(row.batch),
                )
            )
    return findings


def _target_findings(project_root: Path, report: ClaudeProductizationIntegrationReport) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    for target in report.crosswalk.current_slice_targets:
        if target.is_source_pool:
            findings.append(
                SourceGraphAuditFinding(
                    severity=SourceGraphAuditSeverity.BLOCKER,
                    code="audit_source_pool_target",
                    message=f"Current slice target {target.path} is vendor/source-pool-like.",
                    evidence_kind=SourceGraphEvidenceKind.ZYRA_TARGET,
                    subject=target.path,
                )
            )
        if not (project_root / target.path).exists():
            findings.append(
                SourceGraphAuditFinding(
                    severity=SourceGraphAuditSeverity.BLOCKER,
                    code="audit_current_target_missing",
                    message=f"Current slice target {target.path} does not exist.",
                    evidence_kind=SourceGraphEvidenceKind.ZYRA_TARGET,
                    subject=target.path,
                )
            )
    return findings


def _runtime_port_findings(rows: Iterable[RuntimePortAuditRow]) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    for row in rows:
        if row.status == SourceGraphAuditStatus.BLOCKED:
            findings.append(
                SourceGraphAuditFinding(
                    severity=SourceGraphAuditSeverity.BLOCKER,
                    code="audit_required_runtime_port_not_active",
                    message=f"Required runtime port {row.port_id} is not actively bound.",
                    evidence_kind=SourceGraphEvidenceKind.RUNTIME_PORT,
                    subject=row.port_id,
                )
            )
    return findings


def _downstream_findings(rows: Iterable[DownstreamHandoffRow]) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    required_owners = {"M1-02B", "M1-02C", "M1-02D", "M1-03A", "M1-03B", "M1-03C", "M1-03D", "M2"}
    actual_owners = {row.owner_slice for row in rows}
    for owner in sorted(required_owners - actual_owners):
        findings.append(
            SourceGraphAuditFinding(
                severity=SourceGraphAuditSeverity.BLOCKER,
                code="audit_downstream_owner_missing",
                message=f"Downstream handoff for {owner} is missing.",
                evidence_kind=SourceGraphEvidenceKind.DOWNSTREAM_CONTRACT,
                subject=owner,
            )
        )
    for row in rows:
        if row.status == SourceGraphAuditStatus.BLOCKED:
            findings.append(
                SourceGraphAuditFinding(
                    severity=SourceGraphAuditSeverity.BLOCKER,
                    code="audit_downstream_contract_blocked",
                    message=f"Downstream contract {row.contract_id} lacks required ports, events or tests.",
                    evidence_kind=SourceGraphEvidenceKind.DOWNSTREAM_CONTRACT,
                    subject=row.contract_id,
                )
            )
    return findings


def _handoff_report_findings(report: DownstreamHandoffReport) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    if report.ok:
        return findings
    for blocker in report.blockers:
        findings.append(
            SourceGraphAuditFinding(
                severity=SourceGraphAuditSeverity.BLOCKER,
                code=f"handoff_{blocker.code}",
                message=blocker.message,
                evidence_kind=SourceGraphEvidenceKind.DOWNSTREAM_CONTRACT,
                subject=blocker.owner_slice,
            )
        )
    return findings


def _event_runtime_findings(report: EventContractRuntimeReport) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    for blocker in report.blockers:
        findings.append(
            SourceGraphAuditFinding(
                severity=SourceGraphAuditSeverity.BLOCKER,
                code=f"event_runtime_{blocker.code}",
                message=blocker.message,
                evidence_kind=SourceGraphEvidenceKind.EVENT_CONTRACT,
                subject=blocker.phase,
            )
        )
    return findings


def _productization_review_findings(report: ProductizationReviewReport) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    for blocker in report.blockers:
        findings.append(
            SourceGraphAuditFinding(
                severity=SourceGraphAuditSeverity.BLOCKER,
                code=f"productization_{blocker.code}",
                message=blocker.message,
                evidence_kind=SourceGraphEvidenceKind.CLEAN_BOUNDARY,
                subject=blocker.subject,
            )
        )
    return findings


def _disconnect_semantics_findings(report: DisconnectSemanticsReport) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    for blocker in report.blockers:
        findings.append(
            SourceGraphAuditFinding(
                severity=SourceGraphAuditSeverity.BLOCKER,
                code=f"disconnect_{blocker.code}",
                message=blocker.message,
                evidence_kind=SourceGraphEvidenceKind.DISCONNECT_SCENARIO,
                subject=blocker.scenario_id,
            )
        )
    return findings


def _state_custody_runtime_findings(report: StateCustodyRuntimeReport) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    for blocker in report.blockers:
        findings.append(
            SourceGraphAuditFinding(
                severity=SourceGraphAuditSeverity.BLOCKER,
                code=f"state_custody_{blocker.code}",
                message=blocker.message,
                evidence_kind=SourceGraphEvidenceKind.STATE_CUSTODY,
                subject=blocker.state_key,
            )
        )
    return findings


def _api_inventory_contract_findings(report: ApiInventoryContractReport) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    for blocker in report.blockers:
        findings.append(
            SourceGraphAuditFinding(
                severity=SourceGraphAuditSeverity.BLOCKER,
                code=f"api_inventory_{blocker.code}",
                message=blocker.message,
                evidence_kind=SourceGraphEvidenceKind.API_INVENTORY,
                subject=blocker.subject,
            )
        )
    return findings


def _runtime_policy_findings(report: RuntimePolicyMatrixReport) -> list[SourceGraphAuditFinding]:
    findings: list[SourceGraphAuditFinding] = []
    for blocker in report.blockers:
        findings.append(
            SourceGraphAuditFinding(
                severity=SourceGraphAuditSeverity.BLOCKER,
                code=f"runtime_policy_{blocker.code}",
                message=blocker.message,
                evidence_kind=SourceGraphEvidenceKind.RUNTIME_POLICY,
                subject=blocker.rule_id,
            )
        )
    return findings


def _bucket_rule_findings(rows: Iterable[EffectiveLineBucketRule]) -> list[SourceGraphAuditFinding]:
    effective = [row for row in rows if row.counts_as_effective_production]
    if len(effective) == 1 and effective[0].bucket == ProductizationLineBucket.PRODUCTION_INTERNALIZED:
        return []
    return [
        SourceGraphAuditFinding(
            severity=SourceGraphAuditSeverity.BLOCKER,
            code="audit_line_bucket_effective_rule_invalid",
            message="Only production_internalized should count as effective production for this slice.",
            evidence_kind=SourceGraphEvidenceKind.LINE_BUCKET_RULE,
        )
    ]


def _observed_event_phases(report: RuntimeContextAssemblyReport | None) -> set[str]:
    if report is None:
        return set()
    observed: set[str] = {str(EventContractPhase.RUNTIME_CONTEXT_READY)}
    for binding in report.runtime_bindings:
        observed.update(str(phase) for phase in binding.event_phases)
    for binding in report.tool_use_bindings:
        observed.update(str(phase) for phase in binding.event_phases)
    return observed
