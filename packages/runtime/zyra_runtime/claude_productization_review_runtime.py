from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from zyra_core import now_iso

from .claude_runtime_context_ports import RuntimeContextAssemblyReport
from .claude_runtime_contracts import ClaudeRuntimeContractBundle
from .claude_source_graph_crosswalk import ClaudeProductizationIntegrationReport, RuntimePortKind


class ProductizationReviewStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    BLOCKED = "blocked"


class ProductizationReviewSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class ProductizationReviewRuleKind(StrEnum):
    CLEAN_BOUNDARY = "clean_boundary"
    STATE_CUSTODY = "state_custody"
    MAIN_PATH_REACHABILITY = "main_path_reachability"
    LINE_BUCKET = "line_bucket"
    SOURCE_TO_TARGET = "source_to_target"
    VENDOR_EXCLUSION = "vendor_exclusion"
    DISCONNECT_SEMANTICS = "disconnect_semantics"


@dataclass(frozen=True, slots=True)
class CleanBoundaryRule:
    rule_id: str
    path: str
    boundary: str
    allowed: bool
    required: bool
    rationale: str

    @property
    def blocking(self) -> bool:
        return self.required and not self.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "path": self.path,
            "boundary": self.boundary,
            "allowed": self.allowed,
            "required": self.required,
            "blocking": self.blocking,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class StateCustodyRule:
    rule_id: str
    state_key: str
    owner: str
    required: bool
    observed: bool
    source: str
    rationale: str

    @property
    def blocking(self) -> bool:
        return self.required and not self.observed

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "state_key": self.state_key,
            "owner": self.owner,
            "required": self.required,
            "observed": self.observed,
            "blocking": self.blocking,
            "source": self.source,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class MainPathReachabilityRule:
    rule_id: str
    module_path: str
    entrypoint: str
    required: bool
    exists: bool
    imported_by_default_path: bool
    runtime_observed: bool
    rationale: str

    @property
    def blocking(self) -> bool:
        return self.required and not (self.exists and self.imported_by_default_path and self.runtime_observed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "module_path": self.module_path,
            "entrypoint": self.entrypoint,
            "required": self.required,
            "exists": self.exists,
            "imported_by_default_path": self.imported_by_default_path,
            "runtime_observed": self.runtime_observed,
            "blocking": self.blocking,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class LineBucketExpectation:
    bucket_id: str
    label: str
    allowed_roots: tuple[str, ...]
    excluded_markers: tuple[str, ...]
    counts_as_effective: bool
    required_for_slice: bool
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket_id": self.bucket_id,
            "label": self.label,
            "allowed_roots": list(self.allowed_roots),
            "excluded_markers": list(self.excluded_markers),
            "counts_as_effective": self.counts_as_effective,
            "required_for_slice": self.required_for_slice,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class SourceToTargetReviewRow:
    row_id: str
    source_repo: str
    source_path: str
    target_paths: tuple[str, ...]
    owner_slice: str
    decision: str
    required_for_default_path: bool
    clean_safe: bool
    target_exists_count: int
    target_missing_count: int
    status: ProductizationReviewStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "target_paths": list(self.target_paths),
            "owner_slice": self.owner_slice,
            "decision": self.decision,
            "required_for_default_path": self.required_for_default_path,
            "clean_safe": self.clean_safe,
            "target_exists_count": self.target_exists_count,
            "target_missing_count": self.target_missing_count,
            "status": str(self.status),
        }


@dataclass(frozen=True, slots=True)
class ProductizationReviewFinding:
    severity: ProductizationReviewSeverity
    code: str
    message: str
    rule_kind: ProductizationReviewRuleKind
    subject: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == ProductizationReviewSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "rule_kind": str(self.rule_kind),
            "subject": self.subject,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class ProductizationReviewReport:
    ok: bool
    checked_at: str
    owner_slice: str
    clean_boundary_rules: tuple[CleanBoundaryRule, ...]
    state_custody_rules: tuple[StateCustodyRule, ...]
    reachability_rules: tuple[MainPathReachabilityRule, ...]
    line_bucket_expectations: tuple[LineBucketExpectation, ...]
    source_to_target_rows: tuple[SourceToTargetReviewRow, ...]
    findings: tuple[ProductizationReviewFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[ProductizationReviewFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[ProductizationReviewFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == ProductizationReviewSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    def metadata(self) -> dict[str, str]:
        effective_buckets = sum(1 for rule in self.line_bucket_expectations if rule.counts_as_effective)
        clean_blockers = sum(1 for rule in self.clean_boundary_rules if rule.blocking)
        custody_blockers = sum(1 for rule in self.state_custody_rules if rule.blocking)
        reachability_blockers = sum(1 for rule in self.reachability_rules if rule.blocking)
        return {
            "productization_review_ok": str(self.ok).lower(),
            "productization_review_blockers": str(len(self.blockers)),
            "productization_review_warnings": str(len(self.warnings)),
            "productization_review_first_blocker": self.first_blocker_code,
            "productization_review_clean_rules": str(len(self.clean_boundary_rules)),
            "productization_review_clean_blockers": str(clean_blockers),
            "productization_review_state_custody_rules": str(len(self.state_custody_rules)),
            "productization_review_state_custody_blockers": str(custody_blockers),
            "productization_review_reachability_rules": str(len(self.reachability_rules)),
            "productization_review_reachability_blockers": str(reachability_blockers),
            "productization_review_line_buckets": str(len(self.line_bucket_expectations)),
            "productization_review_effective_line_buckets": str(effective_buckets),
            "productization_review_source_to_target_rows": str(len(self.source_to_target_rows)),
            "productization_review_owner_slice": self.owner_slice,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "owner_slice": self.owner_slice,
            "clean_boundary_rules": [rule.to_dict() for rule in self.clean_boundary_rules],
            "state_custody_rules": [rule.to_dict() for rule in self.state_custody_rules],
            "reachability_rules": [rule.to_dict() for rule in self.reachability_rules],
            "line_bucket_expectations": [rule.to_dict() for rule in self.line_bucket_expectations],
            "source_to_target_rows": [row.to_dict() for row in self.source_to_target_rows],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


def build_productization_review_report(
    *,
    project_root: str | Path,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_contracts: ClaudeRuntimeContractBundle,
    runtime_context_report: RuntimeContextAssemblyReport | None = None,
) -> ProductizationReviewReport:
    project_path = Path(project_root).resolve()
    clean_rules = _clean_boundary_rules(project_path, integration_report)
    custody_rules = _state_custody_rules(runtime_contracts, integration_report)
    reachability_rules = _reachability_rules(project_path, integration_report, runtime_context_report)
    line_bucket_expectations = default_line_bucket_expectations()
    source_rows = _source_to_target_rows(project_path, runtime_contracts)
    findings = [
        *_clean_boundary_findings(clean_rules),
        *_state_custody_findings(custody_rules),
        *_reachability_findings(reachability_rules),
        *_line_bucket_findings(line_bucket_expectations),
        *_source_to_target_findings(source_rows),
        *_integration_findings(integration_report),
    ]
    ok = integration_report.ok and (runtime_context_report.ok if runtime_context_report else True) and not any(
        finding.blocking for finding in findings
    )
    return ProductizationReviewReport(
        ok=ok,
        checked_at=now_iso(),
        owner_slice=integration_report.crosswalk.owner_slice,
        clean_boundary_rules=clean_rules,
        state_custody_rules=custody_rules,
        reachability_rules=reachability_rules,
        line_bucket_expectations=line_bucket_expectations,
        source_to_target_rows=source_rows,
        findings=tuple(findings),
    )


def productization_review_markdown(report: ProductizationReviewReport) -> str:
    lines = [
        "## Productization Review",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- owner_slice: `{report.owner_slice}`",
        f"- clean_rules: `{len(report.clean_boundary_rules)}`",
        f"- custody_rules: `{len(report.state_custody_rules)}`",
        f"- reachability_rules: `{len(report.reachability_rules)}`",
        f"- source_to_target_rows: `{len(report.source_to_target_rows)}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Main Path Reachability",
        "",
    ]
    for rule in report.reachability_rules:
        lines.append(
            f"- `{rule.rule_id}` exists=`{str(rule.exists).lower()}` imported=`{str(rule.imported_by_default_path).lower()}` runtime=`{str(rule.runtime_observed).lower()}`"
        )
    if report.blockers:
        lines.extend(["", "### Review Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    return "\n".join(lines) + "\n"


def assert_productization_review_ready(report: ProductizationReviewReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"Productization review failed: {blockers}")


def default_line_bucket_expectations() -> tuple[LineBucketExpectation, ...]:
    return (
        LineBucketExpectation(
            bucket_id="production_internalized",
            label="Production internalized code",
            allowed_roots=("apps/", "packages/", "skills/", "scripts/"),
            excluded_markers=("vendor/", "vendor-runtimes/", "source-pool/", "runtime-sources/", "fixture", "mock"),
            counts_as_effective=True,
            required_for_slice=True,
            rationale="Only Zyra-owned runtime, API, script, skill and app code that is reachable from default paths counts.",
        ),
        LineBucketExpectation(
            bucket_id="test_behavior",
            label="Behavior tests",
            allowed_roots=("tests/",),
            excluded_markers=("snapshot-only", "golden-only", "fixture-only"),
            counts_as_effective=False,
            required_for_slice=True,
            rationale="Tests are mandatory evidence but cannot satisfy production line minimum.",
        ),
        LineBucketExpectation(
            bucket_id="docs_review",
            label="Documentation and self-review",
            allowed_roots=("docs/",),
            excluded_markers=("*.md",),
            counts_as_effective=False,
            required_for_slice=True,
            rationale="Self-review and plan docs record evidence but do not count as production implementation.",
        ),
        LineBucketExpectation(
            bucket_id="vendor_like",
            label="Vendor/source-pool paths",
            allowed_roots=("vendor/", "vendor-runtimes/", "source-pool/", "runtime-sources/"),
            excluded_markers=("*",),
            counts_as_effective=False,
            required_for_slice=False,
            rationale="Vendor-like paths are explicitly excluded from M1-02A-02 completion.",
        ),
        LineBucketExpectation(
            bucket_id="data_ledger",
            label="Ledger/source map data",
            allowed_roots=("docs/", "packages/"),
            excluded_markers=("*.json", "*.yaml", "*.csv", "manifest", "inventory", "source_map"),
            counts_as_effective=False,
            required_for_slice=False,
            rationale="Ledger and manifest data proves traceability only when backed by behavior.",
        ),
        LineBucketExpectation(
            bucket_id="adapter_only",
            label="Adapter-only paths",
            allowed_roots=("apps/", "packages/"),
            excluded_markers=("bridge-only", "launcher-only", "sidecar-only"),
            counts_as_effective=False,
            required_for_slice=False,
            rationale="Thin adapter code is effective only when it transfers state/permissions/events into Zyra-owned runtime modules.",
        ),
    )


def _clean_boundary_rules(
    project_root: Path,
    integration_report: ClaudeProductizationIntegrationReport,
) -> tuple[CleanBoundaryRule, ...]:
    rules: list[CleanBoundaryRule] = [
        CleanBoundaryRule(
            rule_id="clean.project_root",
            path=str(project_root),
            boundary="project_root",
            allowed=not _source_pool_like(project_root),
            required=True,
            rationale="Project root must be a clean Zyra checkout, not a source-pool or vendor runtime.",
        ),
        CleanBoundaryRule(
            rule_id="clean.parent_claude_code_best",
            path=_parent_repo_marker("claude-code-best"),
            boundary="forbidden_parent_source_repo",
            allowed=False,
            required=False,
            rationale="Parent source repo may exist in workspace but must never be a runtime dependency.",
        ),
        CleanBoundaryRule(
            rule_id="clean.vendor_runtime_pilot",
            path="vendor-runtimes/claude-code-runtime/pilot",
            boundary="forbidden_pilot_runtime",
            allowed=False,
            required=False,
            rationale="M1-02A-01 pilot remains source-pool evidence and cannot support current completion.",
        ),
    ]
    for index, target in enumerate(integration_report.crosswalk.current_slice_targets, start=1):
        target_path = project_root / target.path
        rules.append(
            CleanBoundaryRule(
                rule_id=f"clean.current_target.{index}",
                path=target.path,
                boundary="current_slice_target",
                allowed=target_path.exists() and not target.is_source_pool and target.is_product_module,
                required=True,
                rationale="Current slice targets must exist under apps/packages/skills/scripts and must not be source-pool paths.",
            )
        )
    return tuple(rules)


def _state_custody_rules(
    runtime_contracts: ClaudeRuntimeContractBundle,
    integration_report: ClaudeProductizationIntegrationReport,
) -> tuple[StateCustodyRule, ...]:
    custody = dict(runtime_contracts.state_custody)
    required = {
        "worker_entry": "CodeWorkerRuntime",
        "query_engine": "ZyraClaudeQueryEngine",
        "query_loop": "ZyraClaudeQueryEngine",
        "tool_executor": "ToolExecutor",
        "permission_runtime": "ToolPermissionPolicy",
        "query_session_snapshot": "QuerySessionSnapshot",
        "worker_result": "WorkerResult",
        "event_log": "EventRecord",
    }
    rules = [
        StateCustodyRule(
            rule_id=f"custody.{key}",
            state_key=key,
            owner=owner,
            required=True,
            observed=custody.get(key) == owner,
            source="ClaudeRuntimeContractBundle.state_custody",
            rationale=f"{key} state must be owned by {owner} on the Zyra default path.",
        )
        for key, owner in required.items()
    ]
    for port in integration_report.crosswalk.runtime_context_ports:
        rules.append(
            StateCustodyRule(
                rule_id=f"custody.port.{port.port_id}",
                state_key=f"runtime_port:{port.port_id}",
                owner=port.state_owner,
                required=port.required_for_runtime_shell,
                observed=bool(port.state_owner),
                source="ClaudeSourceGraphCrosswalk.runtime_context_ports",
                rationale="RuntimeContext ports must declare the Zyra state owner that will persist or mutate their state.",
            )
        )
    return tuple(rules)


def _reachability_rules(
    project_root: Path,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport | None,
) -> tuple[MainPathReachabilityRule, ...]:
    observed_ports = set()
    if runtime_context_report is not None:
        observed_ports = {str(binding.port_kind) for binding in runtime_context_report.runtime_bindings}
    required_modules = [
        (
            "reach.crosswalk",
            "packages/runtime/zyra_runtime/claude_source_graph_crosswalk.py",
            "build_claude_productization_integration_report",
            RuntimePortKind.SOURCE_GRAPH,
        ),
        (
            "reach.context_ports",
            "packages/runtime/zyra_runtime/claude_runtime_context_ports.py",
            "assemble_claude_runtime_context",
            RuntimePortKind.EVENT_SINK,
        ),
        (
            "reach.audit",
            "packages/runtime/zyra_runtime/claude_source_graph_audit.py",
            "build_claude_source_graph_audit",
            RuntimePortKind.SOURCE_GRAPH,
        ),
        (
            "reach.worker",
            "packages/workers/zyra_workers/code_worker_runtime.py",
            "CodeWorkerRuntime.run",
            RuntimePortKind.TOOL_EXECUTOR,
        ),
        (
            "reach.api_inventory",
            "apps/api/zyra_api/main.py",
            "/workers/code/inventory",
            RuntimePortKind.EVENT_SINK,
        ),
    ]
    rules: list[MainPathReachabilityRule] = []
    current_target_paths = {target.path for target in integration_report.crosswalk.current_slice_targets}
    for rule_id, module_path, entrypoint, port_kind in required_modules:
        rules.append(
            MainPathReachabilityRule(
                rule_id=rule_id,
                module_path=module_path,
                entrypoint=entrypoint,
                required=True,
                exists=(project_root / module_path).exists(),
                imported_by_default_path=module_path in current_target_paths or module_path.endswith("code_worker_runtime.py"),
                runtime_observed=(str(port_kind) in observed_ports) if runtime_context_report else True,
                rationale="Module must be reachable from CodeWorker/API default path and tied to a runtime port.",
            )
        )
    return tuple(rules)


def _source_to_target_rows(
    project_root: Path,
    runtime_contracts: ClaudeRuntimeContractBundle,
) -> tuple[SourceToTargetReviewRow, ...]:
    rows: list[SourceToTargetReviewRow] = []
    for index, item in enumerate(runtime_contracts.source_to_target, start=1):
        exists_count = sum(1 for path in item.target_paths if (project_root / path).exists())
        missing_count = len(item.target_paths) - exists_count
        if item.required_for_default_path and missing_count:
            status = ProductizationReviewStatus.BLOCKED
        elif not item.clean_runtime_safe:
            status = ProductizationReviewStatus.BLOCKED
        else:
            status = ProductizationReviewStatus.PASSING
        rows.append(
            SourceToTargetReviewRow(
                row_id=f"source_to_target.{index}",
                source_repo=item.source_repo,
                source_path=item.source_path,
                target_paths=item.target_paths,
                owner_slice=item.owner_unit,
                decision=str(item.decision),
                required_for_default_path=item.required_for_default_path,
                clean_safe=item.clean_runtime_safe,
                target_exists_count=exists_count,
                target_missing_count=missing_count,
                status=status,
            )
        )
    return tuple(rows)


def _clean_boundary_findings(rules: Iterable[CleanBoundaryRule]) -> list[ProductizationReviewFinding]:
    return [
        ProductizationReviewFinding(
            severity=ProductizationReviewSeverity.BLOCKER,
            code="productization_clean_boundary_blocked",
            message=f"Clean boundary rule {rule.rule_id} failed for {rule.path}.",
            rule_kind=ProductizationReviewRuleKind.CLEAN_BOUNDARY,
            subject=rule.path,
        )
        for rule in rules
        if rule.blocking
    ]


def _state_custody_findings(rules: Iterable[StateCustodyRule]) -> list[ProductizationReviewFinding]:
    return [
        ProductizationReviewFinding(
            severity=ProductizationReviewSeverity.BLOCKER,
            code="productization_state_custody_missing",
            message=f"State custody rule {rule.rule_id} expected {rule.owner} for {rule.state_key}.",
            rule_kind=ProductizationReviewRuleKind.STATE_CUSTODY,
            subject=rule.state_key,
        )
        for rule in rules
        if rule.blocking
    ]


def _reachability_findings(rules: Iterable[MainPathReachabilityRule]) -> list[ProductizationReviewFinding]:
    return [
        ProductizationReviewFinding(
            severity=ProductizationReviewSeverity.BLOCKER,
            code="productization_main_path_reachability_failed",
            message=f"Main path reachability rule {rule.rule_id} failed for {rule.module_path}.",
            rule_kind=ProductizationReviewRuleKind.MAIN_PATH_REACHABILITY,
            subject=rule.module_path,
        )
        for rule in rules
        if rule.blocking
    ]


def _line_bucket_findings(rules: Iterable[LineBucketExpectation]) -> list[ProductizationReviewFinding]:
    effective = [rule for rule in rules if rule.counts_as_effective]
    if len(effective) == 1 and effective[0].bucket_id == "production_internalized":
        return []
    return [
        ProductizationReviewFinding(
            severity=ProductizationReviewSeverity.BLOCKER,
            code="productization_effective_line_bucket_rule_invalid",
            message="Only production_internalized may count as effective production.",
            rule_kind=ProductizationReviewRuleKind.LINE_BUCKET,
        )
    ]


def _source_to_target_findings(rows: Iterable[SourceToTargetReviewRow]) -> list[ProductizationReviewFinding]:
    findings: list[ProductizationReviewFinding] = []
    for row in rows:
        if row.status == ProductizationReviewStatus.BLOCKED:
            findings.append(
                ProductizationReviewFinding(
                    severity=ProductizationReviewSeverity.BLOCKER,
                    code="productization_source_to_target_blocked",
                    message=f"Source-to-target row {row.row_id} is blocked for {row.source_path}.",
                    rule_kind=ProductizationReviewRuleKind.SOURCE_TO_TARGET,
                    subject=row.source_path,
                )
            )
    return findings


def _integration_findings(report: ClaudeProductizationIntegrationReport) -> list[ProductizationReviewFinding]:
    if report.ok:
        return []
    return [
        ProductizationReviewFinding(
            severity=ProductizationReviewSeverity.BLOCKER,
            code=report.blocking_error or "productization_integration_blocked",
            message="Productization integration report is blocked.",
            rule_kind=ProductizationReviewRuleKind.SOURCE_TO_TARGET,
        )
    ]


def _source_pool_like(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    return any(
        marker in normalized
        for marker in (
            "/vendor/",
            "/vendor-runtimes/",
            "/source-pool/",
            "/runtime-sources/",
        )
    )


def _parent_repo_marker(repo: str) -> str:
    return f"..{'/'}{repo}"
