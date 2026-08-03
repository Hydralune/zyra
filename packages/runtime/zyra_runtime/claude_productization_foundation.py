from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from zyra_core import EventRecord, EventType, to_jsonable


OWNER_UNIT = "M1-02A"
MILESTONE = "M1"
PRIMARY_SOURCE_REPO = "claude-code-best"
REFERENCE_REPOS = ("claude-reviews-claude", "Dive-into-Claude-Code")
SOURCE_POOL_MARKERS = (
    "vendor/",
    "vendor-runtimes/",
    "source-pool/",
    "runtime-sources/",
    "productized/",
    "third_party/",
)


class FoundationDecision(StrEnum):
    ACTIVE = "active"
    ADAPTER = "adapter"
    CONTRACT_ONLY = "contract_only"
    REFERENCE_ONLY = "reference_only"
    DEFERRED = "deferred"
    LEGACY_SOURCE_POOL = "legacy_source_pool"
    REJECTED = "rejected"


class BoundaryKind(StrEnum):
    QUERY_ENGINE = "query_engine"
    QUERY_SESSION = "query_session"
    TOOL_LOOP = "tool_loop"
    PERMISSION = "permission"
    MCP = "mcp"
    COMMANDS = "commands"
    SKILLS = "skills"
    SUBAGENT = "subagent"
    CONTEXT_COMPACT = "context_compact"
    CODE_WORKER_ENTRY = "code_worker_entry"


class SurfaceKind(StrEnum):
    RUNTIME_PACKAGE = "runtime_package"
    WORKER_PACKAGE = "worker_package"
    INTEGRATION_PACKAGE = "integration_package"
    CODE_WORKER_APP = "code_worker_app"
    PRODUCTIZED_SCRIPT = "productized_script"
    BEHAVIOR_TEST = "behavior_test"


class ProbeSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class SourceSignal:
    name: str
    pattern: str
    required: bool = True
    description: str = ""

    def match(self, text: str) -> bool:
        return re.search(self.pattern, text, flags=re.MULTILINE) is not None


@dataclass(frozen=True, slots=True)
class SourcePathSpec:
    repo: str
    path: str
    signals: tuple[SourceSignal, ...] = ()
    reference_only: bool = False
    description: str = ""

    @property
    def path_key(self) -> str:
        return f"{self.repo}:{self.path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "path": self.path,
            "signals": [to_jsonable(signal) for signal in self.signals],
            "reference_only": self.reference_only,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class TargetSurfaceSpec:
    path: str
    kind: SurfaceKind
    role: str
    required_for_main_path: bool = True
    import_symbol: str = ""
    expected_signal: str = ""

    @property
    def normalized_path(self) -> str:
        return self.path.replace("\\", "/")

    @property
    def is_source_pool_like(self) -> bool:
        lowered = self.normalized_path.lower()
        return lowered.startswith(SOURCE_POOL_MARKERS) or "/productized/" in lowered

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class BoundarySpec:
    boundary_id: str
    kind: BoundaryKind
    title: str
    source_paths: tuple[SourcePathSpec, ...]
    target_surfaces: tuple[TargetSurfaceSpec, ...]
    downstream_units: tuple[str, ...]
    main_path_event_types: tuple[str, ...]
    control_commands: tuple[str, ...] = ()
    artifact_kinds: tuple[str, ...] = ()
    worker_runtime: str = "CodeWorkerRuntime:claude-productization-foundation"
    rationale: str = ""

    @property
    def primary_sources(self) -> tuple[SourcePathSpec, ...]:
        return tuple(source for source in self.source_paths if source.repo == PRIMARY_SOURCE_REPO)

    @property
    def reference_sources(self) -> tuple[SourcePathSpec, ...]:
        return tuple(source for source in self.source_paths if source.reference_only)

    @property
    def zyra_surfaces(self) -> tuple[TargetSurfaceSpec, ...]:
        return tuple(surface for surface in self.target_surfaces if not surface.is_source_pool_like)

    @property
    def source_pool_surfaces(self) -> tuple[TargetSurfaceSpec, ...]:
        return tuple(surface for surface in self.target_surfaces if surface.is_source_pool_like)

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary_id": self.boundary_id,
            "kind": str(self.kind),
            "title": self.title,
            "source_paths": [item.to_dict() for item in self.source_paths],
            "target_surfaces": [item.to_dict() for item in self.target_surfaces],
            "downstream_units": list(self.downstream_units),
            "main_path_event_types": list(self.main_path_event_types),
            "control_commands": list(self.control_commands),
            "artifact_kinds": list(self.artifact_kinds),
            "worker_runtime": self.worker_runtime,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class SourceFileInspection:
    spec: SourcePathSpec
    resolved_path: str
    exists: bool
    line_count: int = 0
    sha256: str = ""
    matched_signals: tuple[str, ...] = ()
    missing_required_signals: tuple[str, ...] = ()
    source_pool_only: bool = False
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.exists and not self.missing_required_signals and not self.errors

    @property
    def reference_only(self) -> bool:
        return self.spec.reference_only

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.spec.repo,
            "source_path": self.spec.path,
            "resolved_path": self.resolved_path,
            "exists": self.exists,
            "line_count": self.line_count,
            "sha256": self.sha256,
            "matched_signals": list(self.matched_signals),
            "missing_required_signals": list(self.missing_required_signals),
            "source_pool_only": self.source_pool_only,
            "reference_only": self.reference_only,
            "errors": list(self.errors),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class TargetSurfaceInspection:
    spec: TargetSurfaceSpec
    resolved_path: str
    exists: bool
    line_count: int = 0
    imports_expected_symbol: bool = False
    source_pool_like: bool = False
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        if self.spec.required_for_main_path and self.source_pool_like:
            return False
        if self.spec.required_for_main_path and not self.exists:
            return False
        if self.spec.import_symbol and self.exists and not self.imports_expected_symbol:
            return False
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.spec.path,
            "kind": str(self.spec.kind),
            "role": self.spec.role,
            "required_for_main_path": self.spec.required_for_main_path,
            "resolved_path": self.resolved_path,
            "exists": self.exists,
            "line_count": self.line_count,
            "imports_expected_symbol": self.imports_expected_symbol,
            "source_pool_like": self.source_pool_like,
            "errors": list(self.errors),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class FoundationFinding:
    code: str
    severity: ProbeSeverity
    message: str
    boundary_id: str = ""
    path: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {ProbeSeverity.ERROR, ProbeSeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class BoundaryInspection:
    spec: BoundarySpec
    source_inspections: tuple[SourceFileInspection, ...]
    target_inspections: tuple[TargetSurfaceInspection, ...]
    decision: FoundationDecision
    findings: tuple[FoundationFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def primary_source_count(self) -> int:
        return sum(1 for item in self.source_inspections if item.spec.repo == PRIMARY_SOURCE_REPO and item.exists)

    @property
    def zyra_target_count(self) -> int:
        return sum(1 for item in self.target_inspections if not item.source_pool_like and item.exists)

    @property
    def source_pool_target_count(self) -> int:
        return sum(1 for item in self.target_inspections if item.source_pool_like)

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary": self.spec.to_dict(),
            "decision": str(self.decision),
            "ok": self.ok,
            "source_inspections": [item.to_dict() for item in self.source_inspections],
            "target_inspections": [item.to_dict() for item in self.target_inspections],
            "findings": [finding.to_dict() for finding in self.findings],
            "primary_source_count": self.primary_source_count,
            "zyra_target_count": self.zyra_target_count,
            "source_pool_target_count": self.source_pool_target_count,
        }


@dataclass(frozen=True, slots=True)
class FoundationProbeResult:
    project_root: str
    owner_unit: str
    boundaries: tuple[BoundaryInspection, ...]
    source_root: str
    source_pool_root: str
    findings: tuple[FoundationFinding, ...]
    event_records: tuple[EventRecord, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == ProbeSeverity.WARNING)

    @property
    def decision_counts(self) -> dict[str, int]:
        return dict(Counter(str(item.decision) for item in self.boundaries))

    @property
    def coverage(self) -> dict[str, Any]:
        source_count = sum(item.primary_source_count for item in self.boundaries)
        zyra_target_count = sum(item.zyra_target_count for item in self.boundaries)
        pool_target_count = sum(item.source_pool_target_count for item in self.boundaries)
        pool_only_primary_sources = sum(
            1
            for boundary in self.boundaries
            for source in boundary.source_inspections
            if source.spec.repo == PRIMARY_SOURCE_REPO and source.source_pool_only
        )
        return {
            "boundary_count": len(self.boundaries),
            "primary_source_count": source_count,
            "zyra_target_count": zyra_target_count,
            "source_pool_target_count": pool_target_count,
            "source_pool_only_primary_sources": pool_only_primary_sources,
            "ok_boundary_count": sum(1 for item in self.boundaries if item.ok),
            "blocked_boundary_count": sum(1 for item in self.boundaries if not item.ok),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_root": self.project_root,
            "owner_unit": self.owner_unit,
            "source_root": self.source_root,
            "source_pool_root": self.source_pool_root,
            "decision_counts": self.decision_counts,
            "coverage": self.coverage,
            "blocking_count": self.blocking_count,
            "warning_count": self.warning_count,
            "boundaries": [item.to_dict() for item in self.boundaries],
            "findings": [finding.to_dict() for finding in self.findings],
            "event_records": [to_jsonable(event) for event in self.event_records],
        }


@dataclass(frozen=True, slots=True)
class FoundationExecutionRequest:
    run_id: str = "m1-02a-foundation"
    task_id: str = "claude-source-productization-foundation"
    node_id: str = "code-worker"
    include_reference_sources: bool = True
    require_clean_source_pool: bool = True
    require_primary_source_files: bool = True
    allow_source_pool_fallback: bool = False
    require_worker_runtime: bool = True
    extra_constraints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class ClaudeProductizationFoundation:
    def __init__(self, project_root: str | Path, *, source_workspace_root: str | Path | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.source_workspace_root = (
            Path(source_workspace_root).resolve()
            if source_workspace_root
            else self.project_root / "provenance"
        )
        self.source_root = self.source_workspace_root / PRIMARY_SOURCE_REPO
        self.source_pool_root = self.project_root / "vendor-runtimes" / "claude-code-runtime" / "productized" / PRIMARY_SOURCE_REPO

    def inspect(self, request: FoundationExecutionRequest | None = None) -> FoundationProbeResult:
        active_request = request or FoundationExecutionRequest()
        boundaries = tuple(self._inspect_boundary(boundary, active_request) for boundary in default_foundation_boundaries())
        findings = list(self._global_findings(boundaries, active_request))
        for boundary in boundaries:
            findings.extend(boundary.findings)
        event_records = tuple(self._event_records(active_request, boundaries, findings))
        return FoundationProbeResult(
            project_root=str(self.project_root),
            owner_unit=OWNER_UNIT,
            boundaries=boundaries,
            source_root=str(self.source_root),
            source_pool_root=str(self.source_pool_root),
            findings=tuple(findings),
            event_records=event_records,
        )

    def health(self) -> dict[str, Any]:
        result = self.inspect()
        return {
            "ok": result.ok,
            "ownerUnit": OWNER_UNIT,
            "source": PRIMARY_SOURCE_REPO,
            "decisionCounts": result.decision_counts,
            "coverage": result.coverage,
            "blockingCount": result.blocking_count,
            "warningCount": result.warning_count,
            "sourcePoolRuntimeMainPath": False,
        }

    def source_to_target_matrix(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        result = self.inspect()
        for boundary in result.boundaries:
            primary_sources = [item.spec.path for item in boundary.source_inspections if item.spec.repo == PRIMARY_SOURCE_REPO]
            reference_sources = [
                f"{item.spec.repo}:{item.spec.path}"
                for item in boundary.source_inspections
                if item.spec.repo != PRIMARY_SOURCE_REPO
            ]
            zyra_targets = [item.spec.path for item in boundary.target_inspections if not item.source_pool_like]
            source_pool_targets = [item.spec.path for item in boundary.target_inspections if item.source_pool_like]
            rows.append(
                {
                    "boundary_id": boundary.spec.boundary_id,
                    "kind": str(boundary.spec.kind),
                    "decision": str(boundary.decision),
                    "primary_sources": primary_sources,
                    "reference_sources": reference_sources,
                    "zyra_targets": zyra_targets,
                    "source_pool_targets": source_pool_targets,
                    "worker_runtime": boundary.spec.worker_runtime,
                    "events": list(boundary.spec.main_path_event_types),
                    "tests": [
                        target.path
                        for target in boundary.spec.target_surfaces
                        if target.kind == SurfaceKind.BEHAVIOR_TEST
                    ],
                }
            )
        return rows

    def _inspect_boundary(
        self,
        boundary: BoundarySpec,
        request: FoundationExecutionRequest,
    ) -> BoundaryInspection:
        source_inspections = tuple(self._inspect_source(source, request) for source in boundary.source_paths)
        target_inspections = tuple(self._inspect_target(target) for target in boundary.target_surfaces)
        findings: list[FoundationFinding] = []
        primary_sources = [item for item in source_inspections if item.spec.repo == PRIMARY_SOURCE_REPO]
        reference_sources = [item for item in source_inspections if item.reference_only]
        required_targets = [item for item in target_inspections if item.spec.required_for_main_path]
        if request.require_primary_source_files and (not primary_sources or not any(item.exists for item in primary_sources)):
            findings.append(
                FoundationFinding(
                    code="PRIMARY_SOURCE_MISSING",
                    severity=ProbeSeverity.ERROR,
                    boundary_id=boundary.boundary_id,
                    message=f"{boundary.boundary_id} has no primary claude-code-best source evidence.",
                    remediation="Keep claude-code-best as the only runtime source and mark auxiliary repositories reference-only.",
                )
            )
        for item in primary_sources:
            if item.exists and item.missing_required_signals:
                findings.append(
                    FoundationFinding(
                        code="PRIMARY_SOURCE_SIGNAL_MISSING",
                        severity=ProbeSeverity.WARNING,
                        boundary_id=boundary.boundary_id,
                        path=item.spec.path,
                        message=f"{item.spec.path} is present but missing expected signals: {', '.join(item.missing_required_signals)}.",
                        remediation="Re-check the source path or update the boundary contract with a specific replacement source.",
                    )
                )
        if request.include_reference_sources and not reference_sources:
            findings.append(
                FoundationFinding(
                    code="REFERENCE_CROSSWALK_MISSING",
                    severity=ProbeSeverity.WARNING,
                    boundary_id=boundary.boundary_id,
                    message=f"{boundary.boundary_id} has no reference-only crosswalk source.",
                    remediation="Map claude-reviews-claude or Dive-into-Claude-Code docs into the validation checklist.",
                )
            )
        for item in required_targets:
            if item.source_pool_like:
                findings.append(
                    FoundationFinding(
                        code="SOURCE_POOL_CANNOT_BE_MAIN_PATH",
                        severity=ProbeSeverity.BLOCKER,
                        boundary_id=boundary.boundary_id,
                        path=item.spec.path,
                        message=f"{item.spec.path} is source-pool-like but marked required for main path.",
                        remediation="Move the behavior into packages/apps/scripts and mark source-pool target required_for_main_path=false.",
                    )
                )
            elif not item.exists:
                findings.append(
                    FoundationFinding(
                        code="ZYRA_TARGET_MISSING",
                        severity=ProbeSeverity.ERROR,
                        boundary_id=boundary.boundary_id,
                        path=item.spec.path,
                        message=f"{item.spec.path} is required for main path but does not exist.",
                        remediation="Implement or export the Zyra-owned module before claiming foundation completion.",
                    )
                )
            elif item.spec.import_symbol and not item.imports_expected_symbol:
                findings.append(
                    FoundationFinding(
                        code="ZYRA_TARGET_SYMBOL_MISSING",
                        severity=ProbeSeverity.ERROR,
                        boundary_id=boundary.boundary_id,
                        path=item.spec.path,
                        message=f"{item.spec.path} does not expose or reference {item.spec.import_symbol}.",
                        remediation="Connect the target surface to the named runtime symbol.",
                    )
                )
        decision = self._decision_for_boundary(source_inspections, target_inspections, findings, request)
        return BoundaryInspection(
            spec=boundary,
            source_inspections=source_inspections,
            target_inspections=target_inspections,
            decision=decision,
            findings=tuple(findings),
        )

    def _decision_for_boundary(
        self,
        source_inspections: Iterable[SourceFileInspection],
        target_inspections: Iterable[TargetSurfaceInspection],
        findings: Iterable[FoundationFinding],
        request: FoundationExecutionRequest,
    ) -> FoundationDecision:
        if any(finding.severity == ProbeSeverity.BLOCKER for finding in findings):
            return FoundationDecision.REJECTED
        target_items = list(target_inspections)
        source_items = list(source_inspections)
        if any(finding.severity == ProbeSeverity.ERROR for finding in findings):
            return FoundationDecision.DEFERRED
        has_primary_source = any(item.spec.repo == PRIMARY_SOURCE_REPO and item.exists for item in source_items)
        has_zyra_target = any(item.exists and not item.source_pool_like for item in target_items)
        if has_zyra_target and (has_primary_source or not request.require_primary_source_files):
            return FoundationDecision.ACTIVE
        if any(item.source_pool_like for item in target_items):
            return FoundationDecision.LEGACY_SOURCE_POOL
        return FoundationDecision.CONTRACT_ONLY

    def _inspect_source(self, spec: SourcePathSpec, request: FoundationExecutionRequest) -> SourceFileInspection:
        source_root = self.source_root if spec.repo == PRIMARY_SOURCE_REPO else self.source_workspace_root / "claudecode-related" / spec.repo.split("/", 1)[-1]
        target = source_root / spec.path
        source_pool_target = self.source_pool_root / spec.path if spec.repo == PRIMARY_SOURCE_REPO else target
        resolved = target if target.exists() else source_pool_target if request.allow_source_pool_fallback else target
        errors: list[str] = []
        if not resolved.exists():
            return SourceFileInspection(
                spec=spec,
                resolved_path=str(resolved),
                exists=False,
                errors=("source file missing",),
                source_pool_only=False,
            )
        try:
            text, digest = _read_source_tree_text(resolved)
        except OSError as error:
            return SourceFileInspection(
                spec=spec,
                resolved_path=str(resolved),
                exists=False,
                errors=(str(error),),
                source_pool_only=_is_relative_to(resolved, self.source_pool_root),
            )
        matched: list[str] = []
        missing: list[str] = []
        for signal in spec.signals:
            if signal.match(text):
                matched.append(signal.name)
            elif signal.required:
                missing.append(signal.name)
        return SourceFileInspection(
            spec=spec,
            resolved_path=str(resolved),
            exists=True,
            line_count=_line_count(text),
            sha256=digest,
            matched_signals=tuple(matched),
            missing_required_signals=tuple(missing),
            source_pool_only=_is_relative_to(resolved, self.source_pool_root),
        )

    def _inspect_target(self, spec: TargetSurfaceSpec) -> TargetSurfaceInspection:
        target = self.project_root / spec.normalized_path
        source_pool_like = spec.is_source_pool_like
        if not target.exists():
            return TargetSurfaceInspection(
                spec=spec,
                resolved_path=str(target),
                exists=False,
                source_pool_like=source_pool_like,
            )
        try:
            text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        except OSError as error:
            return TargetSurfaceInspection(
                spec=spec,
                resolved_path=str(target),
                exists=False,
                source_pool_like=source_pool_like,
                errors=(str(error),),
            )
        imports_symbol = True
        if spec.import_symbol:
            imports_symbol = spec.import_symbol in text
        return TargetSurfaceInspection(
            spec=spec,
            resolved_path=str(target),
            exists=True,
            line_count=_line_count(text),
            imports_expected_symbol=imports_symbol,
            source_pool_like=source_pool_like,
        )

    def _global_findings(
        self,
        boundaries: Iterable[BoundaryInspection],
        request: FoundationExecutionRequest,
    ) -> Iterable[FoundationFinding]:
        boundary_list = list(boundaries)
        if request.require_worker_runtime and not any(
            any(
                target.spec.kind == SurfaceKind.WORKER_PACKAGE and target.exists and not target.source_pool_like
                for target in boundary.target_inspections
            )
            for boundary in boundary_list
        ):
            yield FoundationFinding(
                code="WORKER_RUNTIME_SURFACE_MISSING",
                severity=ProbeSeverity.BLOCKER,
                message="No Zyra-owned worker package surface is reachable for M1-02A.",
                remediation="Connect the foundation to zyra_workers instead of relying on vendor-runtimes.",
            )
        if request.require_clean_source_pool:
            bad_targets = [
                target
                for boundary in boundary_list
                for target in boundary.target_inspections
                if target.source_pool_like and target.spec.required_for_main_path
            ]
            if bad_targets:
                yield FoundationFinding(
                    code="SOURCE_POOL_REQUIRED_FOR_MAIN_PATH",
                    severity=ProbeSeverity.BLOCKER,
                    message=f"{len(bad_targets)} source-pool targets are still marked required for main path.",
                    remediation="Downgrade source-pool targets and register Zyra-owned targets for connected runtime claims.",
                    metadata={"paths": [item.spec.path for item in bad_targets[:20]]},
                )

    def _event_records(
        self,
        request: FoundationExecutionRequest,
        boundaries: Iterable[BoundaryInspection],
        findings: Iterable[FoundationFinding],
    ) -> Iterable[EventRecord]:
        finding_list = list(findings)
        boundary_list = list(boundaries)
        yield EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "claude_productization_foundation": {
                    "owner_unit": OWNER_UNIT,
                    "boundary_count": len(boundary_list),
                    "ok_boundary_count": sum(1 for item in boundary_list if item.ok),
                    "blocking_count": sum(1 for item in finding_list if item.blocking),
                    "decision_counts": dict(Counter(str(item.decision) for item in boundary_list)),
                }
            },
        )


def default_foundation_boundaries() -> tuple[BoundarySpec, ...]:
    return (
        BoundarySpec(
            boundary_id="query-engine-session-loop",
            kind=BoundaryKind.QUERY_ENGINE,
            title="QueryEngine and query loop foundation",
            source_paths=(
                SourcePathSpec(
                    PRIMARY_SOURCE_REPO,
                    "src/QueryEngine.ts",
                    signals=(
                        SourceSignal("query-engine-config", r"QueryEngineConfig"),
                        SourceSignal("tool-use-loop", r"tool|Tool", required=False),
                    ),
                    description="Primary Claude Code QueryEngine control loop source.",
                ),
                SourcePathSpec(
                    PRIMARY_SOURCE_REPO,
                    "src/query.ts",
                    signals=(SourceSignal("query-state", r"type\s+State|interface\s+State", required=False),),
                    description="Claude Code query loop state and lifecycle source.",
                ),
                SourcePathSpec(
                    "claudecode-related/claude-reviews-claude",
                    "architecture/zh-CN/01-query-engine.md",
                    reference_only=True,
                    description="Reference-only query engine decomposition checklist.",
                ),
            ),
            target_surfaces=(
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/query-engine.ts", SurfaceKind.RUNTIME_PACKAGE, "query-engine-runtime", import_symbol="ClaudeRuntimeCore"),
        TargetSurfaceSpec("packages/runtime/claude-runtime/src/session.ts", SurfaceKind.RUNTIME_PACKAGE, "query-session-owner", import_symbol="RuntimeSession"),
        TargetSurfaceSpec("packages/runtime/claude-runtime/src/protocol.ts", SurfaceKind.RUNTIME_PACKAGE, "runtime-protocol", import_symbol="RUNTIME_PROTOCOL_VERSION"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/claude_query_plan.py", SurfaceKind.RUNTIME_PACKAGE, "query-plan-runtime", import_symbol="ClaudeQueryPlanner"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/claude_runtime_state.py", SurfaceKind.RUNTIME_PACKAGE, "runtime-state-ledger", import_symbol="ClaudeRuntimeStateLedger"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/claude_session_lifecycle.py", SurfaceKind.RUNTIME_PACKAGE, "session-lifecycle-runtime", import_symbol="ClaudeSessionLifecycleRuntime"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/claude_runtime_contracts.py", SurfaceKind.RUNTIME_PACKAGE, "source-custody-contracts", import_symbol="build_productized_claude_runtime_contracts"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/query_session.py", SurfaceKind.RUNTIME_PACKAGE, "state-port", import_symbol="QuerySession"),
                TargetSurfaceSpec("packages/workers/zyra_workers/code_worker_runtime.py", SurfaceKind.WORKER_PACKAGE, "default-worker-entry", import_symbol="TypeScriptClaudeQueryEngine"),
                TargetSurfaceSpec("packages/workers/zyra_workers/typescript_claude_runtime.py", SurfaceKind.WORKER_PACKAGE, "typescript-process-host", import_symbol="TypeScriptClaudeQueryEngine"),
        TargetSurfaceSpec("apps/code-worker/src/main.ts", SurfaceKind.CODE_WORKER_APP, "runtime-contract", import_symbol="runtimeContract"),
                TargetSurfaceSpec("tests/unit/test_query_session_lifecycle.py", SurfaceKind.BEHAVIOR_TEST, "behavior-test", required_for_main_path=False),
                TargetSurfaceSpec("tests/integration/test_code_worker_clean_productized_runtime.py", SurfaceKind.BEHAVIOR_TEST, "clean-behavior-test", required_for_main_path=False),
            ),
            downstream_units=("M1-02B", "M1-02D", "M1-03A"),
            main_path_event_types=("query_session", "agent_message"),
            artifact_kinds=("trace", "structured_data"),
            rationale="M1-02A foundation seeds the query/session state port that M1-02B deepens.",
        ),
        BoundarySpec(
            boundary_id="tool-loop-budget-foundation",
            kind=BoundaryKind.TOOL_LOOP,
            title="Tool loop, tool registry, and result budget foundation",
            source_paths=(
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/Tool.ts", signals=(SourceSignal("tool-interface", r"interface\s+Tool|type\s+Tool"),)),
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/tools.ts", signals=(SourceSignal("base-tools", r"getAllBaseTools|getTools", required=False),)),
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/services/tools/toolOrchestration.ts", signals=(SourceSignal("orchestration", r"runToolUse|orchestrat", required=False),)),
                SourcePathSpec("claudecode-related/claude-reviews-claude", "architecture/zh-CN/02-tool-system.md", reference_only=True),
            ),
            target_surfaces=(
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/query-engine.ts", SurfaceKind.RUNTIME_PACKAGE, "query-tool-loop-runtime", import_symbol="ClaudeRuntimeCore"),
        TargetSurfaceSpec("packages/runtime/claude-runtime/src/tools.ts", SurfaceKind.RUNTIME_PACKAGE, "query-tool-scheduler", import_symbol="scheduleToolBatches"),
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/tools/execution-runtime.ts", SurfaceKind.RUNTIME_PACKAGE, "tool-use-semantic-runtime", import_symbol="ToolExecutionRuntime"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/tools.py", SurfaceKind.RUNTIME_PACKAGE, "tool-registry", import_symbol="ToolRegistry"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/tool_loop.py", SurfaceKind.RUNTIME_PACKAGE, "tool-scheduler", import_symbol="ToolLoopScheduler"),
                TargetSurfaceSpec("packages/workers/zyra_workers/code_worker_runtime.py", SurfaceKind.WORKER_PACKAGE, "default-worker-entry", import_symbol="TypeScriptClaudeQueryEngine"),
                TargetSurfaceSpec("tests/unit/test_tool_loop_budget_runtime.py", SurfaceKind.BEHAVIOR_TEST, "behavior-test", required_for_main_path=False),
                TargetSurfaceSpec("tests/integration/test_code_worker_clean_productized_runtime.py", SurfaceKind.BEHAVIOR_TEST, "clean-behavior-test", required_for_main_path=False),
            ),
            downstream_units=("M1-02C", "M1-03A", "M1-08"),
            main_path_event_types=("tool_batch_started", "tool_call_completed", "tool_result_budget_exceeded"),
            artifact_kinds=("structured_data",),
            rationale="M1-02A must choose the source boundary; M1-02C attaches execution and budget semantics.",
        ),
        BoundarySpec(
            boundary_id="permission-runtime-foundation",
            kind=BoundaryKind.PERMISSION,
            title="Permission runtime foundation",
            source_paths=(
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/hooks/toolPermission", signals=(SourceSignal("permission", r"permission|canUseTool|allow|deny|ask", required=False),)),
                SourcePathSpec("claudecode-related/claude-reviews-claude", "architecture/zh-CN/07-permission-pipeline.md", reference_only=True),
            ),
            target_surfaces=(
                TargetSurfaceSpec("packages/runtime/zyra_runtime/permissions.py", SurfaceKind.RUNTIME_PACKAGE, "permission-policy", import_symbol="ToolPermissionPolicy"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/executor.py", SurfaceKind.RUNTIME_PACKAGE, "permission-gate", import_symbol="permission"),
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/permission/evaluator.ts", SurfaceKind.RUNTIME_PACKAGE, "permission-event-runtime", import_symbol="PermissionEvaluator"),
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/permission/coordinator.ts", SurfaceKind.RUNTIME_PACKAGE, "permission-event", import_symbol="PermissionCoordinator"),
                TargetSurfaceSpec("tests/integration/test_code_worker_clean_productized_runtime.py", SurfaceKind.BEHAVIOR_TEST, "permission-semantic-test", required_for_main_path=False),
            ),
            downstream_units=("M1-03A", "M1-04C"),
            main_path_event_types=("tool_permission_requested", "tool_permission_decision", "tool_call_denied"),
            control_commands=("permission.allow", "permission.deny"),
            rationale="Foundation must keep permission as a Zyra-owned policy gate rather than a UI-only prompt.",
        ),
        BoundarySpec(
            boundary_id="mcp-services-foundation",
            kind=BoundaryKind.MCP,
            title="MCP service boundary foundation",
            source_paths=(
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/services/mcp", signals=(SourceSignal("mcp", r"MCP|mcp|resources|prompts|elicitation", required=False),)),
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/commands/mcp", signals=(SourceSignal("mcp-command", r"mcp|server|auth", required=False),)),
                SourcePathSpec("claudecode-related/claude-reviews-claude", "architecture/zh-CN/15-services-api-layer.md", reference_only=True),
            ),
            target_surfaces=(
                TargetSurfaceSpec("packages/integrations/zyra_integrations/source_extraction.py", SurfaceKind.INTEGRATION_PACKAGE, "source-crosswalk", import_symbol="claude_code_m1_02a_plan"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/scaffold.py", SurfaceKind.RUNTIME_PACKAGE, "mcp-contract", import_symbol="McpServerContract"),
                TargetSurfaceSpec("scripts/zyra_source_extract.py", SurfaceKind.PRODUCTIZED_SCRIPT, "cli-entry", required_for_main_path=False),
            ),
            downstream_units=("M1-03B", "M1-08"),
            main_path_event_types=("mcp_server_loaded", "mcp_tool_discovered"),
            control_commands=("mcp.list", "mcp.disable"),
            rationale="MCP is not executed in 02A but the source and target boundary must be durable.",
        ),
        BoundarySpec(
            boundary_id="commands-control-foundation",
            kind=BoundaryKind.COMMANDS,
            title="Slash/session command control foundation",
            source_paths=(
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/commands.ts", signals=(SourceSignal("commands", r"commands|Command", required=False),)),
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/commands/compact", signals=(SourceSignal("compact-command", r"compact", required=False),)),
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/commands/context", signals=(SourceSignal("context-command", r"context", required=False),)),
                SourcePathSpec("claudecode-related/claude-reviews-claude", "architecture/zh-CN/13-bridge-system.md", reference_only=True),
                SourcePathSpec("claudecode-related/claude-reviews-claude", "architecture/zh-CN/14-ui-state-management.md", reference_only=True),
            ),
            target_surfaces=(
                TargetSurfaceSpec("packages/runtime/zyra_runtime/control.py", SurfaceKind.RUNTIME_PACKAGE, "control-event", import_symbol="control_event_from_command"),
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/control/runtime.ts", SurfaceKind.RUNTIME_PACKAGE, "control-command-runtime", import_symbol="TypeScriptControlRuntime"),
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/query-engine.ts", SurfaceKind.RUNTIME_PACKAGE, "query-control-entry", import_symbol="runtimeConstraints"),
                TargetSurfaceSpec("apps/code-worker/src/main.ts", SurfaceKind.CODE_WORKER_APP, "contract-cli", import_symbol="runtimeContract"),
                TargetSurfaceSpec("tests/integration/test_claude_code_productized_runtime.py", SurfaceKind.BEHAVIOR_TEST, "legacy-regression", required_for_main_path=False),
            ),
            downstream_units=("M1-02D", "M1-03D", "M2-04A"),
            main_path_event_types=("control_command", "agent_message"),
            control_commands=("context.show", "compact.run", "session.resume"),
            rationale="Commands must become ControlCommand/event-log surfaces, not remain TUI-only behaviors.",
        ),
        BoundarySpec(
            boundary_id="context-compact-foundation",
            kind=BoundaryKind.CONTEXT_COMPACT,
            title="Context assembly and compact restore foundation",
            source_paths=(
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/services/compact", signals=(SourceSignal("compact", r"compact|autoCompact|reactive", required=False),)),
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/utils/sessionRestore.ts", signals=(SourceSignal("session-restore", r"restore|resume", required=False),)),
                SourcePathSpec("claudecode-related/claude-reviews-claude", "architecture/zh-CN/10-context-assembly.md", reference_only=True),
                SourcePathSpec("claudecode-related/claude-reviews-claude", "architecture/zh-CN/11-compact-system.md", reference_only=True),
            ),
            target_surfaces=(
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/session.ts", SurfaceKind.RUNTIME_PACKAGE, "context-compact-runtime", import_symbol="compact"),
        TargetSurfaceSpec("packages/runtime/claude-runtime/src/budget.ts", SurfaceKind.RUNTIME_PACKAGE, "tool-result-budget-runtime", import_symbol="applyToolResultBudget"),
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/context/assembly-runtime.ts", SurfaceKind.RUNTIME_PACKAGE, "context-window-runtime", import_symbol="ContextAssemblyRuntime"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/claude_session_lifecycle.py", SurfaceKind.RUNTIME_PACKAGE, "resume-plan-runtime", import_symbol="ClaudeSessionResumePlan"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/session.py", SurfaceKind.RUNTIME_PACKAGE, "context-session", import_symbol="ContextSessionRuntime"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/query_session.py", SurfaceKind.RUNTIME_PACKAGE, "snapshot-port", import_symbol="QuerySessionSnapshot"),
                TargetSurfaceSpec("packages/runtime/claude-runtime/src/compact/context-runtime.ts", SurfaceKind.RUNTIME_PACKAGE, "compact-event", import_symbol="ContextCompactionRuntime"),
                TargetSurfaceSpec("tests/integration/test_code_worker_clean_productized_runtime.py", SurfaceKind.BEHAVIOR_TEST, "context-semantic-test", required_for_main_path=False),
            ),
            downstream_units=("M1-02D", "M1-06C", "M2-04B"),
            main_path_event_types=("context_compacted", "query_session_snapshot"),
            artifact_kinds=("structured_data", "trace"),
            rationale="02A only establishes the source/target custody; restore semantics continue in 02D/06C.",
        ),
        BoundarySpec(
            boundary_id="skill-subagent-foundation",
            kind=BoundaryKind.SKILLS,
            title="SkillTool and AgentTool foundation",
            source_paths=(
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/tools/SkillTool", signals=(SourceSignal("skill-tool", r"Skill|allowed|tool", required=False),)),
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/tools/AgentTool", signals=(SourceSignal("agent-tool", r"Agent|subagent|fork", required=False),)),
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/tools/AgentTool/forkSubagent.ts", signals=(SourceSignal("forked-agent", r"fork|agent", required=False),)),
                SourcePathSpec("claudecode-related/claude-reviews-claude", "architecture/zh-CN/04-plugin-system.md", reference_only=True),
                SourcePathSpec("claudecode-related/claude-reviews-claude", "architecture/zh-CN/08-agent-swarms.md", reference_only=True),
            ),
            target_surfaces=(
                TargetSurfaceSpec("packages/runtime/zyra_runtime/scaffold.py", SurfaceKind.RUNTIME_PACKAGE, "skill-contract", import_symbol="SkillContract"),
                TargetSurfaceSpec("packages/workers/zyra_workers/runtime_scaffold.py", SurfaceKind.WORKER_PACKAGE, "worker-contract", import_symbol="CodeWorkerScaffold"),
                TargetSurfaceSpec("packages/workers/zyra_workers/code_worker_runtime.py", SurfaceKind.WORKER_PACKAGE, "worker-runtime", import_symbol="CodeWorkerRuntime"),
            ),
            downstream_units=("M1-03C", "M1-03D", "M1-07A"),
            main_path_event_types=("skill_loaded", "subagent_invoked"),
            artifact_kinds=("structured_data",),
            rationale="02A must reserve first-class Zyra module space for later SkillTool/AgentTool deep execution.",
        ),
        BoundarySpec(
            boundary_id="code-worker-ingress-foundation",
            kind=BoundaryKind.CODE_WORKER_ENTRY,
            title="CodeWorker app ingress and protocol foundation",
            source_paths=(
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/entrypoints/cli.tsx", signals=(SourceSignal("cli", r"query|command|session", required=False),)),
                SourcePathSpec(PRIMARY_SOURCE_REPO, "src/commands/doctor", signals=(SourceSignal("doctor", r"doctor|diagnostic", required=False),)),
                SourcePathSpec("claudecode-related/Dive-into-Claude-Code", "docs/build-your-own-agent_zh.md", reference_only=True),
            ),
            target_surfaces=(
                TargetSurfaceSpec("apps/code-worker/src/main.ts", SurfaceKind.CODE_WORKER_APP, "line-protocol", import_symbol="runStdioRuntime"),
                TargetSurfaceSpec("packages/workers/zyra_workers/code_worker_bridge.py", SurfaceKind.WORKER_PACKAGE, "sidecar-client", import_symbol="CodeWorkerSidecarClient"),
                TargetSurfaceSpec("packages/workers/zyra_workers/code_worker_runtime.py", SurfaceKind.WORKER_PACKAGE, "default-runtime-entry", import_symbol="CodeWorkerRuntime"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/claude_runtime_contracts.py", SurfaceKind.RUNTIME_PACKAGE, "sidecar-free-contracts", import_symbol="sidecar_free_contracts"),
                TargetSurfaceSpec("packages/runtime/zyra_runtime/claude_clean_runtime.py", SurfaceKind.RUNTIME_PACKAGE, "clean-runtime-audit", import_symbol="ClaudeCleanRuntimeAuditor"),
                TargetSurfaceSpec("scripts/verify_code_worker_sidecar.py", SurfaceKind.PRODUCTIZED_SCRIPT, "health-check", required_for_main_path=False),
                TargetSurfaceSpec("scripts/verify_claude_productization_foundation.py", SurfaceKind.PRODUCTIZED_SCRIPT, "clean-health-check", required_for_main_path=False),
            ),
            downstream_units=("M1-02B", "M1-02C", "M1-08"),
            main_path_event_types=("code_worker_health", "agent_message"),
            control_commands=("code_worker.health",),
            rationale="The app ingress provides a submission-contained protocol while Python packages own state and policy.",
        ),
    )


def foundation_event_payload(result: FoundationProbeResult) -> dict[str, Any]:
    return {
        "event_type": "claude_productization_foundation",
        "owner_unit": result.owner_unit,
        "ok": result.ok,
        "coverage": result.coverage,
        "decision_counts": result.decision_counts,
        "blocking_count": result.blocking_count,
        "warning_count": result.warning_count,
    }


def assert_foundation_ready(result: FoundationProbeResult) -> None:
    if result.ok:
        return
    details = "\n".join(
        f"- {finding.code} {finding.path}: {finding.message}"
        for finding in result.findings
        if finding.blocking
    )
    raise AssertionError(f"Claude productization foundation is not ready:\n{details}")


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _read_source_tree_text(path: Path) -> tuple[str, str]:
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        return text, hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    chunks: list[str] = []
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if not child.is_file() or child.suffix.lower() not in {".ts", ".tsx", ".js", ".mjs", ".json", ".md", ".py"}:
            continue
        relative = child.relative_to(path).as_posix()
        text = child.read_text(encoding="utf-8", errors="replace")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        chunks.append(f"\n# file: {relative}\n{text}")
    return "\n".join(chunks), digest.hexdigest()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _slug(value: str) -> str:
    lowered = value.lower().replace("\\", "/")
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def stable_foundation_id(boundary_id: str, path: str = "") -> str:
    digest = hashlib.sha1(f"{OWNER_UNIT}:{boundary_id}:{path}".encode("utf-8")).hexdigest()[:16]
    return f"m1_02a_{_slug(boundary_id)}_{digest}"


def foundation_payload_for_cli(project_root: str | Path, *, source_workspace_root: str | Path | None = None) -> dict[str, Any]:
    runtime = ClaudeProductizationFoundation(project_root, source_workspace_root=source_workspace_root)
    result = runtime.inspect()
    return result.to_dict()
