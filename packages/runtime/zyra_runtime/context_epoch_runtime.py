from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .compact_restore_runtime import CompactRestoreReport
from .model_api_runtime import ApiRetryReport, ModelStreamReport
from .runtime_budget_state import (
    CODEWORKER_API_FOUNDATION_RUNTIME_ID,
    M1_02D_OWNER_UNIT,
    RuntimeBudgetSnapshot,
)


class ContextEpochNodeKind(StrEnum):
    SESSION_START = "session_start"
    CONTEXT_USAGE = "context_usage"
    TOOL_RESULT_CONTEXT = "tool_result_context"
    COMPACT_CANDIDATE = "compact_candidate"
    COMPACT_BOUNDARY = "compact_boundary"
    NEXT_TURN_RESTORE = "next_turn_restore"
    MODEL_PROVIDER = "model_provider"
    MODEL_STREAM = "model_stream"
    API_RETRY = "api_retry"
    RUNTIME_BUDGET = "runtime_budget"
    FOUNDATION_GATE = "foundation_gate"


class ContextEpochEdgeKind(StrEnum):
    PRODUCES = "produces"
    CONSUMES = "consumes"
    MUTATES = "mutates"
    RESTORES = "restores"
    GATES = "gates"
    RECOVERS = "recovers"
    OBSERVES = "observes"


class ContextEpochStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ContextEpochSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ContextEpochSurface(StrEnum):
    NODE = "node"
    EDGE = "edge"
    EVENT = "event"
    RESTORE = "restore"
    BUDGET = "budget"
    MODEL = "model"
    RETRY = "retry"
    FOUNDATION = "foundation"


@dataclass(frozen=True, slots=True)
class ContextEpochNode:
    node_id: str
    kind: ContextEpochNodeKind
    label: str
    epoch_index: int
    present: bool
    report_id: str = ""
    event_phase: str = ""
    artifact_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def missing(self) -> bool:
        return not self.present

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": str(self.kind),
            "label": self.label,
            "epoch_index": self.epoch_index,
            "present": self.present,
            "missing": self.missing,
            "report_id": self.report_id,
            "event_phase": self.event_phase,
            "artifact_id": self.artifact_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextEpochEdge:
    edge_id: str
    kind: ContextEpochEdgeKind
    from_node_id: str
    to_node_id: str
    required: bool
    satisfied: bool
    reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.required and not self.satisfied

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "kind": str(self.kind),
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
            "required": self.required,
            "satisfied": self.satisfied,
            "blocking": self.blocking,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextEpochFinding:
    code: str
    severity: ContextEpochSeverity
    surface: ContextEpochSurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ContextEpochSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextEpochReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    nodes: tuple[ContextEpochNode, ...]
    edges: tuple[ContextEpochEdge, ...]
    findings: tuple[ContextEpochFinding, ...]
    phase_counts: dict[str, int]
    source_decisions: tuple[dict[str, str], ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(edge.blocking for edge in self.edges) and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ContextEpochStatus:
        if not self.ok:
            return ContextEpochStatus.BLOCKED
        if self.findings:
            return ContextEpochStatus.DEGRADED
        return ContextEpochStatus.READY

    @property
    def missing_nodes(self) -> int:
        return sum(1 for node in self.nodes if node.missing)

    @property
    def blocking_edges(self) -> int:
        return sum(1 for edge in self.edges if edge.blocking)

    @property
    def compact_epoch_count(self) -> int:
        return sum(1 for node in self.nodes if node.kind == ContextEpochNodeKind.COMPACT_BOUNDARY and node.present)

    @property
    def restore_epoch_count(self) -> int:
        return sum(1 for node in self.nodes if node.kind == ContextEpochNodeKind.NEXT_TURN_RESTORE and node.present)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.context_epoch_runtime.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "missing_nodes": self.missing_nodes,
            "blocking_edges": self.blocking_edges,
            "compact_epoch_count": self.compact_epoch_count,
            "restore_epoch_count": self.restore_epoch_count,
            "phase_counts": dict(self.phase_counts),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "context_epoch_report_id": self.report_id,
            "context_epoch_owner_unit": self.owner_unit,
            "context_epoch_runtime_id": self.runtime_id,
            "context_epoch_ok": str(self.ok).lower(),
            "context_epoch_status": str(self.status),
            "context_epoch_nodes": str(len(self.nodes)),
            "context_epoch_edges": str(len(self.edges)),
            "context_epoch_missing_nodes": str(self.missing_nodes),
            "context_epoch_blocking_edges": str(self.blocking_edges),
            "context_epoch_compact_epochs": str(self.compact_epoch_count),
            "context_epoch_restore_epochs": str(self.restore_epoch_count),
            "context_epoch_findings": str(len(self.findings)),
        }


class ContextEpochRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        event_records: Sequence[EventRecord],
        budget_snapshot: RuntimeBudgetSnapshot,
        compact_restore: CompactRestoreReport,
        model_stream_reports: Sequence[ModelStreamReport],
        api_retry_reports: Sequence[ApiRetryReport],
        provider_report: Any = None,
        foundation_report: Any = None,
    ) -> ContextEpochReport:
        phase_counts = _phase_counts(event_records)
        nodes = self._nodes(
            session_id=session_id,
            budget_snapshot=budget_snapshot,
            compact_restore=compact_restore,
            model_stream_reports=model_stream_reports,
            api_retry_reports=api_retry_reports,
            provider_report=provider_report,
            foundation_report=foundation_report,
            phase_counts=phase_counts,
        )
        edges = self._edges(nodes, compact_restore=compact_restore, budget_snapshot=budget_snapshot)
        findings = self._findings(
            nodes=nodes,
            edges=edges,
            compact_restore=compact_restore,
            budget_snapshot=budget_snapshot,
            model_stream_reports=model_stream_reports,
            api_retry_reports=api_retry_reports,
            phase_counts=phase_counts,
        )
        return ContextEpochReport(
            report_id=new_id("ctx_epoch"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            nodes=tuple(nodes),
            edges=tuple(edges),
            findings=tuple(findings),
            phase_counts=phase_counts,
            source_decisions=default_context_epoch_source_decisions(),
        )

    def event_for_report(
        self,
        report: ContextEpochReport,
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
                    "phase": "context_epoch_report",
                    "context_epoch": report.to_dict(),
                }
            },
        )

    def _nodes(
        self,
        *,
        session_id: str,
        budget_snapshot: RuntimeBudgetSnapshot,
        compact_restore: CompactRestoreReport,
        model_stream_reports: Sequence[ModelStreamReport],
        api_retry_reports: Sequence[ApiRetryReport],
        provider_report: Any,
        foundation_report: Any,
        phase_counts: Mapping[str, int],
    ) -> list[ContextEpochNode]:
        nodes: list[ContextEpochNode] = []
        nodes.append(
            ContextEpochNode(
                node_id=new_id("epoch_node"),
                kind=ContextEpochNodeKind.SESSION_START,
                label="query-session",
                epoch_index=0,
                present=bool(session_id),
                event_phase="session_started",
                metadata={"phase_count": str(phase_counts.get("session_started", 0))},
            )
        )
        context_limit = budget_snapshot.context_limit
        nodes.append(
            ContextEpochNode(
                node_id=new_id("epoch_node"),
                kind=ContextEpochNodeKind.CONTEXT_USAGE,
                label="context-usage",
                epoch_index=1,
                present=context_limit is not None,
                report_id=budget_snapshot.snapshot_id,
                event_phase="runtime_budget_updated",
                metadata={
                    "used_chars": str(context_limit.used if context_limit else 0),
                    "limit_chars": str(context_limit.limit if context_limit else 0),
                },
            )
        )
        nodes.append(
            ContextEpochNode(
                node_id=new_id("epoch_node"),
                kind=ContextEpochNodeKind.TOOL_RESULT_CONTEXT,
                label="tool-result-context",
                epoch_index=2,
                present=phase_counts.get("tool_result_context_projected", 0) > 0
                or phase_counts.get("tool_result_context_report", 0) > 0,
                event_phase="tool_result_context_projected",
                metadata={"projection_events": str(phase_counts.get("tool_result_context_projected", 0))},
            )
        )
        candidate = compact_restore.context_budget.candidate
        nodes.append(
            ContextEpochNode(
                node_id=new_id("epoch_node"),
                kind=ContextEpochNodeKind.COMPACT_CANDIDATE,
                label="compact-candidate",
                epoch_index=3,
                present=candidate is not None,
                report_id=candidate.candidate_id if candidate is not None else "",
                event_phase="compact_needed",
                metadata={
                    "compact_needed": str(compact_restore.compact_needed).lower(),
                    "candidate_blocks": str(candidate.candidate_count if candidate else 0),
                },
            )
        )
        boundary = compact_restore.boundary
        nodes.append(
            ContextEpochNode(
                node_id=new_id("epoch_node"),
                kind=ContextEpochNodeKind.COMPACT_BOUNDARY,
                label="compact-boundary",
                epoch_index=4,
                present=boundary is not None,
                report_id=boundary.boundary_id if boundary else "",
                event_phase="compact_boundary_created" if boundary and boundary.applied else "compact_needed",
                artifact_id=boundary.artifact_id if boundary else "",
                metadata={"applied": str(bool(boundary and boundary.applied)).lower()},
            )
        )
        restore = compact_restore.restore_contract
        nodes.append(
            ContextEpochNode(
                node_id=new_id("epoch_node"),
                kind=ContextEpochNodeKind.NEXT_TURN_RESTORE,
                label="next-turn-restore",
                epoch_index=5,
                present=restore is not None,
                report_id=restore.contract_id if restore else "",
                event_phase="next_turn_restore_contract",
                artifact_id=restore.compact_artifact_id if restore else "",
                metadata={
                    "restore_segments": str(restore.restore_segment_count if restore else 0),
                    "missing_segments": str(restore.missing_required_segments if restore else 0),
                },
            )
        )
        nodes.append(
            ContextEpochNode(
                node_id=new_id("epoch_node"),
                kind=ContextEpochNodeKind.MODEL_PROVIDER,
                label="model-provider",
                epoch_index=6,
                present=provider_report is not None,
                report_id=str(getattr(provider_report, "report_id", "") or ""),
                event_phase="model_provider_catalog",
                metadata={"status": str(getattr(provider_report, "status", "") or "")},
            )
        )
        for index, report in enumerate(model_stream_reports, start=7):
            nodes.append(
                ContextEpochNode(
                    node_id=new_id("epoch_node"),
                    kind=ContextEpochNodeKind.MODEL_STREAM,
                    label=f"model-stream-turn-{report.envelope.turn_index}",
                    epoch_index=index,
                    present=True,
                    report_id=report.report_id,
                    event_phase="model_stream_report",
                    metadata={
                        "model": report.envelope.model,
                        "status": str(report.status),
                        "error_kind": str(report.error_kind),
                    },
                )
            )
        retry_epoch_base = 7 + len(model_stream_reports)
        for index, report in enumerate(api_retry_reports, start=retry_epoch_base):
            nodes.append(
                ContextEpochNode(
                    node_id=new_id("epoch_node"),
                    kind=ContextEpochNodeKind.API_RETRY,
                    label=f"api-retry-{index - retry_epoch_base + 1}",
                    epoch_index=index,
                    present=True,
                    report_id=report.report_id,
                    event_phase="api_retry_report",
                    metadata={
                        "status": str(report.status),
                        "retry_count": str(report.retry_count),
                        "fallback_used": str(report.fallback_used).lower(),
                    },
                )
            )
        nodes.append(
            ContextEpochNode(
                node_id=new_id("epoch_node"),
                kind=ContextEpochNodeKind.RUNTIME_BUDGET,
                label="runtime-budget",
                epoch_index=retry_epoch_base + len(api_retry_reports),
                present=budget_snapshot.ok,
                report_id=budget_snapshot.snapshot_id,
                event_phase="runtime_budget_updated",
                metadata={
                    "status": str(budget_snapshot.status),
                    "retry_count": str(budget_snapshot.retry_count),
                    "compact_count": str(budget_snapshot.compact_count),
                },
            )
        )
        nodes.append(
            ContextEpochNode(
                node_id=new_id("epoch_node"),
                kind=ContextEpochNodeKind.FOUNDATION_GATE,
                label="codeworker-api-foundation",
                epoch_index=retry_epoch_base + len(api_retry_reports) + 1,
                present=foundation_report is not None,
                report_id=str(getattr(foundation_report, "report_id", "") or ""),
                event_phase="codeworker_api_foundation",
                metadata={"ok": str(bool(getattr(foundation_report, "ok", False))).lower()},
            )
        )
        return nodes

    def _edges(
        self,
        nodes: Sequence[ContextEpochNode],
        *,
        compact_restore: CompactRestoreReport,
        budget_snapshot: RuntimeBudgetSnapshot,
    ) -> list[ContextEpochEdge]:
        by_kind: dict[ContextEpochNodeKind, list[ContextEpochNode]] = {}
        for node in nodes:
            by_kind.setdefault(node.kind, []).append(node)
        edges: list[ContextEpochEdge] = []

        def add(kind: ContextEpochEdgeKind, source: ContextEpochNodeKind, target: ContextEpochNodeKind, *, required: bool = True, reason: str = "") -> None:
            source_node = by_kind.get(source, [None])[0]
            target_node = by_kind.get(target, [None])[0]
            edges.append(
                ContextEpochEdge(
                    edge_id=new_id("epoch_edge"),
                    kind=kind,
                    from_node_id=source_node.node_id if source_node else "",
                    to_node_id=target_node.node_id if target_node else "",
                    required=required,
                    satisfied=bool(source_node and target_node and source_node.present and target_node.present),
                    reason=reason,
                )
            )

        add(ContextEpochEdgeKind.PRODUCES, ContextEpochNodeKind.SESSION_START, ContextEpochNodeKind.CONTEXT_USAGE, reason="session seeds context budget")
        add(ContextEpochEdgeKind.CONSUMES, ContextEpochNodeKind.TOOL_RESULT_CONTEXT, ContextEpochNodeKind.CONTEXT_USAGE, required=False, reason="tool result context contributes to budget")
        add(ContextEpochEdgeKind.PRODUCES, ContextEpochNodeKind.CONTEXT_USAGE, ContextEpochNodeKind.COMPACT_CANDIDATE, required=compact_restore.compact_needed, reason="budget pressure creates compact candidate")
        add(ContextEpochEdgeKind.PRODUCES, ContextEpochNodeKind.COMPACT_CANDIDATE, ContextEpochNodeKind.COMPACT_BOUNDARY, required=compact_restore.compact_needed, reason="candidate becomes boundary or compact-needed state")
        add(ContextEpochEdgeKind.RESTORES, ContextEpochNodeKind.COMPACT_BOUNDARY, ContextEpochNodeKind.NEXT_TURN_RESTORE, reason="boundary feeds restore contract")
        add(ContextEpochEdgeKind.CONSUMES, ContextEpochNodeKind.NEXT_TURN_RESTORE, ContextEpochNodeKind.MODEL_STREAM, reason="restore contract available before subsequent model stream")
        add(ContextEpochEdgeKind.PRODUCES, ContextEpochNodeKind.MODEL_PROVIDER, ContextEpochNodeKind.MODEL_STREAM, reason="provider route selects model stream")
        add(ContextEpochEdgeKind.RECOVERS, ContextEpochNodeKind.MODEL_STREAM, ContextEpochNodeKind.API_RETRY, reason="api retry observes stream report")
        add(ContextEpochEdgeKind.MUTATES, ContextEpochNodeKind.API_RETRY, ContextEpochNodeKind.RUNTIME_BUDGET, reason="retry mutates runtime budget state")
        add(ContextEpochEdgeKind.GATES, ContextEpochNodeKind.RUNTIME_BUDGET, ContextEpochNodeKind.FOUNDATION_GATE, reason="budget state gates foundation")
        if budget_snapshot.compact_count == 0 and compact_restore.boundary and compact_restore.boundary.applied:
            budget_node = by_kind.get(ContextEpochNodeKind.RUNTIME_BUDGET, [None])[0]
            boundary_node = by_kind.get(ContextEpochNodeKind.COMPACT_BOUNDARY, [None])[0]
            edges.append(
                ContextEpochEdge(
                    edge_id=new_id("epoch_edge"),
                    kind=ContextEpochEdgeKind.MUTATES,
                    from_node_id=boundary_node.node_id if boundary_node else "",
                    to_node_id=budget_node.node_id if budget_node else "",
                    required=True,
                    satisfied=False,
                    reason="applied compact boundary must mutate RuntimeBudgetState.compact_count",
                )
            )
        return edges

    def _findings(
        self,
        *,
        nodes: Sequence[ContextEpochNode],
        edges: Sequence[ContextEpochEdge],
        compact_restore: CompactRestoreReport,
        budget_snapshot: RuntimeBudgetSnapshot,
        model_stream_reports: Sequence[ModelStreamReport],
        api_retry_reports: Sequence[ApiRetryReport],
        phase_counts: Mapping[str, int],
    ) -> list[ContextEpochFinding]:
        findings: list[ContextEpochFinding] = []
        for edge in edges:
            if edge.blocking:
                findings.append(
                    ContextEpochFinding(
                        code="CONTEXT_EPOCH_EDGE_BLOCKED",
                        severity=ContextEpochSeverity.BLOCKER,
                        surface=ContextEpochSurface.EDGE,
                        message=f"Required context epoch edge {edge.kind} is not satisfied.",
                        metadata={"edge_id": edge.edge_id, "reason": edge.reason},
                    )
                )
        for node in nodes:
            if node.missing and node.kind in {
                ContextEpochNodeKind.CONTEXT_USAGE,
                ContextEpochNodeKind.NEXT_TURN_RESTORE,
                ContextEpochNodeKind.MODEL_STREAM,
                ContextEpochNodeKind.RUNTIME_BUDGET,
                ContextEpochNodeKind.FOUNDATION_GATE,
            }:
                findings.append(
                    ContextEpochFinding(
                        code="REQUIRED_CONTEXT_EPOCH_NODE_MISSING",
                        severity=ContextEpochSeverity.BLOCKER,
                        surface=ContextEpochSurface.NODE,
                        message=f"Required context epoch node {node.kind} is missing.",
                        metadata={"node_id": node.node_id, "label": node.label},
                    )
                )
        if compact_restore.restore_contract is None:
            findings.append(
                ContextEpochFinding(
                    code="NEXT_TURN_RESTORE_CONTRACT_MISSING",
                    severity=ContextEpochSeverity.BLOCKER,
                    surface=ContextEpochSurface.RESTORE,
                    message="Context epoch requires a next-turn restore contract.",
                )
            )
        elif compact_restore.restore_contract.missing_required_segments:
            findings.append(
                ContextEpochFinding(
                    code="NEXT_TURN_RESTORE_CONTRACT_INCOMPLETE",
                    severity=ContextEpochSeverity.BLOCKER,
                    surface=ContextEpochSurface.RESTORE,
                    message="Next-turn restore contract has missing required segments.",
                    metadata={"missing_required_segments": str(compact_restore.restore_contract.missing_required_segments)},
                )
            )
        if not budget_snapshot.ok:
            findings.append(
                ContextEpochFinding(
                    code="RUNTIME_BUDGET_SNAPSHOT_NOT_OK",
                    severity=ContextEpochSeverity.BLOCKER,
                    surface=ContextEpochSurface.BUDGET,
                    message="RuntimeBudgetSnapshot is not ok inside context epoch runtime.",
                    metadata={"status": str(budget_snapshot.status)},
                )
            )
        if not model_stream_reports:
            findings.append(
                ContextEpochFinding(
                    code="MODEL_STREAM_EPOCH_MISSING",
                    severity=ContextEpochSeverity.BLOCKER,
                    surface=ContextEpochSurface.MODEL,
                    message="No model stream epoch was created.",
                )
            )
        if not api_retry_reports:
            findings.append(
                ContextEpochFinding(
                    code="API_RETRY_EPOCH_MISSING",
                    severity=ContextEpochSeverity.BLOCKER,
                    surface=ContextEpochSurface.RETRY,
                    message="No API retry epoch was created.",
                )
            )
        required_phases = (
            "runtime_budget_updated",
            "compact_restore_report",
            "next_turn_restore_contract",
            "model_stream_report",
            "api_retry_report",
            "codeworker_api_foundation",
        )
        for phase in required_phases:
            if phase_counts.get(phase, 0) <= 0:
                findings.append(
                    ContextEpochFinding(
                        code="CONTEXT_EPOCH_REQUIRED_EVENT_MISSING",
                        severity=ContextEpochSeverity.BLOCKER,
                        surface=ContextEpochSurface.EVENT,
                        message=f"Required context epoch event phase {phase} was not observed.",
                        metadata={"phase": phase},
                    )
                )
        return findings


def context_epoch_metadata(report: ContextEpochReport | None) -> dict[str, str]:
    if report is None:
        return {"context_epoch_ok": "false", "context_epoch_status": "missing", "context_epoch_report_id": ""}
    return report.metadata()


def render_context_epoch_markdown(report: ContextEpochReport) -> str:
    lines = [
        "# Context Epoch Runtime",
        "",
        f"- report_id: {report.report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- nodes: {len(report.nodes)}",
        f"- edges: {len(report.edges)}",
        f"- compact_epochs: {report.compact_epoch_count}",
        f"- restore_epochs: {report.restore_epoch_count}",
        "",
        "## Nodes",
    ]
    for node in report.nodes:
        lines.append(f"- {node.epoch_index} {node.kind} present={str(node.present).lower()} report={node.report_id}")
    lines.extend(["", "## Edges"])
    for edge in report.edges:
        lines.append(f"- {edge.kind} {edge.from_node_id}->{edge.to_node_id} satisfied={str(edge.satisfied).lower()} reason={edge.reason}")
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def default_context_epoch_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/*",
            "target_path": "packages/runtime/zyra_runtime/context_epoch_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "context epoch graph and session event continuity",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/sessionMemoryCompact.ts",
            "target_path": "packages/runtime/zyra_runtime/context_epoch_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "compact boundary to restore contract epoch transition",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/claude.ts",
            "target_path": "packages/runtime/zyra_runtime/context_epoch_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "model stream and retry epochs joined to runtime budget state",
        },
    )


def _phase_counts(event_records: Sequence[EventRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in event_records:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        query_session = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
        phase = str(query_session.get("phase") or "")
        if phase:
            counts[phase] = counts.get(phase, 0) + 1
    return counts
