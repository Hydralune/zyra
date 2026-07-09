from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from zyra_core import EventRecord, EventType, new_id, now_iso

from .compact_restore_runtime import CompactRestoreReport, compact_restore_metadata
from .model_api_runtime import ApiRetryReport, ModelStreamReport, api_retry_metadata, model_stream_metadata
from .runtime_budget_state import (
    CODEWORKER_API_FOUNDATION_RUNTIME_ID,
    M1_02D_OWNER_UNIT,
    RuntimeBudgetSnapshot,
    RuntimeBudgetState,
    runtime_budget_metadata,
)


class CodeWorkerApiFoundationStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class CodeWorkerApiFoundationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class CodeWorkerApiFoundationSurface(StrEnum):
    RUNTIME_BUDGET_STATE = "runtime_budget_state"
    COMPACT_RESTORE = "compact_restore"
    MODEL_STREAM = "model_stream"
    API_RETRY = "api_retry"
    TOOL_RESULT_CONTEXT = "tool_result_context"
    DEFAULT_PATH = "default_path"


@dataclass(frozen=True, slots=True)
class CodeWorkerApiFoundationFinding:
    code: str
    severity: CodeWorkerApiFoundationSeverity
    surface: CodeWorkerApiFoundationSurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == CodeWorkerApiFoundationSeverity.BLOCKER

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
class CodeWorkerApiFoundationPathEdge:
    edge_id: str
    from_node: str
    to_node: str
    required: bool
    satisfied: bool
    evidence: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.required and not self.satisfied

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "required": self.required,
            "satisfied": self.satisfied,
            "blocking": self.blocking,
            "evidence": self.evidence,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeWorkerApiFoundationReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    budget_snapshot: RuntimeBudgetSnapshot
    compact_restore: CompactRestoreReport
    model_stream: ModelStreamReport
    api_retry: ApiRetryReport
    path_edges: tuple[CodeWorkerApiFoundationPathEdge, ...]
    findings: tuple[CodeWorkerApiFoundationFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return (
            not self.disabled
            and self.budget_snapshot.ok
            and self.compact_restore.ok
            and (self.model_stream.ok or self.api_retry.recovered)
            and self.api_retry.ok
            and not any(edge.blocking for edge in self.path_edges)
            and not any(finding.blocking for finding in self.findings)
        )

    @property
    def status(self) -> CodeWorkerApiFoundationStatus:
        if self.disabled:
            return CodeWorkerApiFoundationStatus.DISABLED
        if not self.ok:
            return CodeWorkerApiFoundationStatus.BLOCKED
        if self.findings:
            return CodeWorkerApiFoundationStatus.DEGRADED
        return CodeWorkerApiFoundationStatus.READY

    @property
    def blocking_edges(self) -> int:
        return sum(1 for edge in self.path_edges if edge.blocking)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking) + self.blocking_edges

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_api_foundation.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "budget_snapshot": self.budget_snapshot.to_dict(),
            "compact_restore": self.compact_restore.to_dict(),
            "model_stream": self.model_stream.to_dict(),
            "api_retry": self.api_retry.to_dict(),
            "path_edges": [edge.to_dict() for edge in self.path_edges],
            "blocking_edges": self.blocking_edges,
            "blocker_count": self.blocker_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "codeworker_api_foundation_report_id": self.report_id,
            "codeworker_api_foundation_owner_unit": self.owner_unit,
            "codeworker_api_foundation_runtime_id": self.runtime_id,
            "codeworker_api_foundation_ok": str(self.ok).lower(),
            "codeworker_api_foundation_status": str(self.status),
            "codeworker_api_foundation_disabled": str(self.disabled).lower(),
            "codeworker_api_foundation_edges": str(len(self.path_edges)),
            "codeworker_api_foundation_blocking_edges": str(self.blocking_edges),
            "codeworker_api_foundation_findings": str(len(self.findings)),
            "codeworker_api_foundation_blockers": str(self.blocker_count),
            "codeworker_api_foundation_default_path": "true",
            "codeworker_api_foundation_chain": (
                "session/tool_state->context_usage->CompactRestoreRuntime->"
                "ModelStreamRuntime/ApiRetryRuntime->RuntimeBudgetState->next_turn_restore"
            ),
            **runtime_budget_metadata(self.budget_snapshot),
            **compact_restore_metadata(self.compact_restore),
            **model_stream_metadata(self.model_stream),
            **api_retry_metadata(self.api_retry),
        }


class CodeWorkerApiFoundationRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled

    def build_report(
        self,
        *,
        budget_state: RuntimeBudgetState,
        compact_restore: CompactRestoreReport,
        model_stream: ModelStreamReport,
        api_retry: ApiRetryReport,
        tool_result_context_report: Any = None,
        session_snapshot: Mapping[str, Any] | None = None,
    ) -> CodeWorkerApiFoundationReport:
        budget_snapshot = budget_state.require_ready()
        findings: list[CodeWorkerApiFoundationFinding] = []
        if self.disabled:
            findings.append(
                CodeWorkerApiFoundationFinding(
                    code="CODEWORKER_API_FOUNDATION_DISABLED",
                    severity=CodeWorkerApiFoundationSeverity.BLOCKER,
                    surface=CodeWorkerApiFoundationSurface.DEFAULT_PATH,
                    message="CodeWorkerApiFoundationRuntime is disabled.",
                )
            )
        if not budget_snapshot.ok:
            findings.append(
                CodeWorkerApiFoundationFinding(
                    code="RUNTIME_BUDGET_STATE_BLOCKED",
                    severity=CodeWorkerApiFoundationSeverity.BLOCKER,
                    surface=CodeWorkerApiFoundationSurface.RUNTIME_BUDGET_STATE,
                    message="RuntimeBudgetState failed readiness validation.",
                )
            )
        if not compact_restore.ok:
            findings.append(
                CodeWorkerApiFoundationFinding(
                    code="COMPACT_RESTORE_BLOCKED",
                    severity=CodeWorkerApiFoundationSeverity.BLOCKER,
                    surface=CodeWorkerApiFoundationSurface.COMPACT_RESTORE,
                    message="CompactRestoreRuntime failed to build a usable compact/restore report.",
                )
            )
        if not model_stream.ok and not api_retry.recovered:
            findings.append(
                CodeWorkerApiFoundationFinding(
                    code="MODEL_STREAM_BLOCKED",
                    severity=CodeWorkerApiFoundationSeverity.BLOCKER,
                    surface=CodeWorkerApiFoundationSurface.MODEL_STREAM,
                    message="ModelStreamRuntime failed to produce a usable stream report.",
                )
            )
        if not api_retry.ok:
            findings.append(
                CodeWorkerApiFoundationFinding(
                    code="API_RETRY_BLOCKED",
                    severity=CodeWorkerApiFoundationSeverity.BLOCKER,
                    surface=CodeWorkerApiFoundationSurface.API_RETRY,
                    message="ApiRetryRuntime failed to produce a usable retry/fallback report.",
                )
            )
        projection_count = len(getattr(tool_result_context_report, "projections", ()) or ())
        edges = self._path_edges(
            budget_snapshot=budget_snapshot,
            compact_restore=compact_restore,
            model_stream=model_stream,
            api_retry=api_retry,
            projection_count=projection_count,
            session_snapshot=session_snapshot or {},
        )
        for edge in edges:
            if edge.blocking:
                findings.append(
                    CodeWorkerApiFoundationFinding(
                        code=f"PATH_EDGE_BLOCKED_{edge.from_node.upper()}_TO_{edge.to_node.upper()}",
                        severity=CodeWorkerApiFoundationSeverity.BLOCKER,
                        surface=CodeWorkerApiFoundationSurface.DEFAULT_PATH,
                        message=f"Required CodeWorker API foundation path edge {edge.from_node}->{edge.to_node} is not satisfied.",
                        metadata=edge.metadata,
                    )
                )
        return CodeWorkerApiFoundationReport(
            report_id=new_id("codeworker_api"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=budget_snapshot.session_id,
            worker_request_id=budget_snapshot.worker_request_id,
            budget_snapshot=budget_snapshot,
            compact_restore=compact_restore,
            model_stream=model_stream,
            api_retry=api_retry,
            path_edges=edges,
            findings=tuple(findings),
            source_decisions=default_codeworker_api_foundation_source_decisions(),
            disabled=self.disabled,
        )

    def event_for_report(
        self,
        report: CodeWorkerApiFoundationReport,
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
                    "phase": "codeworker_api_foundation",
                    "codeworker_api_foundation": report.to_dict(),
                }
            },
        )

    def _path_edges(
        self,
        *,
        budget_snapshot: RuntimeBudgetSnapshot,
        compact_restore: CompactRestoreReport,
        model_stream: ModelStreamReport,
        api_retry: ApiRetryReport,
        projection_count: int,
        session_snapshot: Mapping[str, Any],
    ) -> tuple[CodeWorkerApiFoundationPathEdge, ...]:
        restore_contract = compact_restore.restore_contract
        metadata = session_snapshot.get("metadata") if isinstance(session_snapshot.get("metadata"), Mapping) else {}
        return (
            CodeWorkerApiFoundationPathEdge(
                edge_id=new_id("api_edge"),
                from_node="session_tool_state",
                to_node="context_usage",
                required=True,
                satisfied=projection_count > 0 or _limit_for_scope_name(budget_snapshot, "context_window") is not None,
                evidence=f"tool_result_projections={projection_count}",
                metadata={"projection_count": str(projection_count)},
            ),
            CodeWorkerApiFoundationPathEdge(
                edge_id=new_id("api_edge"),
                from_node="context_usage",
                to_node="CompactRestoreRuntime",
                required=True,
                satisfied=compact_restore.context_budget.ok,
                evidence=compact_restore.context_budget.report_id,
            ),
            CodeWorkerApiFoundationPathEdge(
                edge_id=new_id("api_edge"),
                from_node="CompactRestoreRuntime",
                to_node="ModelStreamRuntime",
                required=True,
                satisfied=bool(model_stream.frames),
                evidence=model_stream.report_id,
            ),
            CodeWorkerApiFoundationPathEdge(
                edge_id=new_id("api_edge"),
                from_node="ModelStreamRuntime",
                to_node="ApiRetryRuntime",
                required=True,
                satisfied=bool(api_retry.attempts),
                evidence=api_retry.report_id,
            ),
            CodeWorkerApiFoundationPathEdge(
                edge_id=new_id("api_edge"),
                from_node="ApiRetryRuntime",
                to_node="RuntimeBudgetState",
                required=True,
                satisfied=budget_snapshot.ok and budget_snapshot.retry_count >= api_retry.retry_count,
                evidence=budget_snapshot.snapshot_id,
                metadata={"retry_count": str(api_retry.retry_count), "budget_retry_count": str(budget_snapshot.retry_count)},
            ),
            CodeWorkerApiFoundationPathEdge(
                edge_id=new_id("api_edge"),
                from_node="RuntimeBudgetState",
                to_node="next_turn_restore",
                required=True,
                satisfied=restore_contract is not None and restore_contract.ok,
                evidence=restore_contract.contract_id if restore_contract else "",
                metadata={"metadata_keys": ",".join(sorted(str(key) for key in metadata.keys())[:20])},
            ),
        )


def codeworker_api_foundation_metadata(report: CodeWorkerApiFoundationReport | None) -> dict[str, str]:
    if report is None:
        return {
            "codeworker_api_foundation_ok": "false",
            "codeworker_api_foundation_status": "missing",
            "codeworker_api_foundation_report_id": "",
        }
    return report.metadata()


def render_codeworker_api_foundation_markdown(report: CodeWorkerApiFoundationReport) -> str:
    lines = [
        "# CodeWorker API Foundation",
        "",
        f"- report_id: {report.report_id}",
        f"- owner_unit: {report.owner_unit}",
        f"- runtime_id: {report.runtime_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        "",
        "## Path Edges",
    ]
    for edge in report.path_edges:
        lines.append(
            f"- {edge.from_node} -> {edge.to_node}: satisfied={str(edge.satisfied).lower()} required={str(edge.required).lower()} evidence={edge.evidence}"
        )
    lines.extend(["", "## Components"])
    lines.append(f"- RuntimeBudgetState: {report.budget_snapshot.status} ({report.budget_snapshot.snapshot_id})")
    lines.append(f"- CompactRestoreRuntime: {report.compact_restore.status} ({report.compact_restore.report_id})")
    lines.append(f"- ModelStreamRuntime: {report.model_stream.status} ({report.model_stream.report_id})")
    lines.append(f"- ApiRetryRuntime: {report.api_retry.status} ({report.api_retry.report_id})")
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def default_codeworker_api_foundation_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/QueryEngine.ts",
            "target_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "session/tool state to context budget and model stream dispatch",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/claude.ts",
            "target_path": "packages/runtime/zyra_runtime/model_api_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "stream frame, usage patch, semantic error and retry/fallback contract",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "compact boundary and next-turn restore contract",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/*",
            "target_path": "packages/runtime/zyra_runtime/runtime_budget_state.py",
            "decision": "adapter_encapsulated",
            "capability": "durable session epoch and budget state contract",
        },
    )


def _limit_for_scope_name(snapshot: RuntimeBudgetSnapshot, scope_name: str) -> Any:
    for limit in snapshot.limits:
        if str(limit.scope) == scope_name or str(limit.scope).endswith(scope_name):
            return limit
    return None
