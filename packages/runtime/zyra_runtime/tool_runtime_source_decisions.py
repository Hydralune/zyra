from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .tool_runtime_foundation import (
    TOOL_LOOP_FOUNDATION_OWNER_UNIT,
    TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ToolRuntimeSourceDecision,
    ToolRuntimeSourceLedgerRow,
    tool_runtime_source_to_target_rows,
)


class ToolSourceCoverageStatus(StrEnum):
    PASS = "pass"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ToolSourceCoverageSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ToolSourceRequirement:
    requirement_id: str
    source_repo: str
    source_path: str
    minimum_decision: ToolRuntimeSourceDecision
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "minimum_decision": str(self.minimum_decision),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ToolSourceCoverageRow:
    coverage_id: str
    requirement: ToolSourceRequirement
    matched: bool
    decision: str
    target_paths: tuple[str, ...]
    owner_unit: str
    test_entrypoints: tuple[str, ...]
    notes: str = ""
    created_at: str = field(default_factory=now_iso)

    @property
    def active(self) -> bool:
        return self.decision in {
            str(ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED),
            str(ToolRuntimeSourceDecision.ACTIVE_PORT),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage_id": self.coverage_id,
            "requirement": self.requirement.to_dict(),
            "matched": self.matched,
            "decision": self.decision,
            "active": self.active,
            "target_paths": list(self.target_paths),
            "owner_unit": self.owner_unit,
            "test_entrypoints": list(self.test_entrypoints),
            "notes": self.notes,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolSourceCoverageFinding:
    code: str
    severity: ToolSourceCoverageSeverity
    message: str
    source_repo: str = ""
    source_path: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolSourceCoverageSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "blocking": self.blocking,
            "message": self.message,
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolSourceCoverageReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    rows: tuple[ToolSourceCoverageRow, ...]
    findings: tuple[ToolSourceCoverageFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolSourceCoverageStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolSourceCoverageStatus.BLOCKED
        if self.findings:
            return ToolSourceCoverageStatus.DEGRADED
        return ToolSourceCoverageStatus.PASS

    @property
    def requirement_count(self) -> int:
        return len(self.rows)

    @property
    def matched_count(self) -> int:
        return sum(1 for row in self.rows if row.matched)

    @property
    def active_count(self) -> int:
        return sum(1 for row in self.rows if row.active)

    @property
    def repo_count(self) -> int:
        return len({row.requirement.source_repo for row in self.rows if row.matched})

    @property
    def missing_requirements(self) -> tuple[ToolSourceCoverageRow, ...]:
        return tuple(row for row in self.rows if not row.matched)

    def require_pass(self) -> None:
        if self.ok:
            return
        missing = ", ".join(
            f"{row.requirement.source_repo}:{row.requirement.source_path}"
            for row in self.missing_requirements
        )
        blockers = ", ".join(finding.code for finding in self.findings if finding.blocking)
        raise AssertionError(f"tool source coverage blocked: missing={missing or 'none'}; blockers={blockers or 'unknown'}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "requirement_count": self.requirement_count,
            "matched_count": self.matched_count,
            "active_count": self.active_count,
            "repo_count": self.repo_count,
            "rows": [row.to_dict() for row in self.rows],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        repos = sorted({row.requirement.source_repo for row in self.rows if row.matched})
        return {
            "tool_source_coverage_report_id": self.report_id,
            "tool_source_coverage_owner_unit": self.owner_unit,
            "tool_source_coverage_ok": str(self.ok).lower(),
            "tool_source_coverage_status": str(self.status),
            "tool_source_coverage_requirements": str(self.requirement_count),
            "tool_source_coverage_matched": str(self.matched_count),
            "tool_source_coverage_active": str(self.active_count),
            "tool_source_coverage_repos": ",".join(repos),
            "tool_source_coverage_findings": str(len(self.findings)),
        }


class ToolSourceCoverageRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
        requirements: Sequence[ToolSourceRequirement] | None = None,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.requirements = tuple(requirements or default_tool_source_requirements())

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        source_rows: Sequence[ToolRuntimeSourceLedgerRow] | None = None,
    ) -> ToolSourceCoverageReport:
        effective_source_rows = tool_runtime_source_to_target_rows() if source_rows is None else tuple(source_rows)
        rows = tuple(self._coverage_rows(tuple(effective_source_rows)))
        findings = tuple(self._findings(rows))
        return ToolSourceCoverageReport(
            report_id=new_id("toolsourcecoverage"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            rows=rows,
            findings=findings,
        )

    def event_for_report(
        self,
        report: ToolSourceCoverageReport,
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
                    "phase": "tool_source_coverage",
                    "tool_source_coverage": report.to_dict(),
                }
            },
        )

    def _coverage_rows(self, source_rows: Sequence[ToolRuntimeSourceLedgerRow]) -> list[ToolSourceCoverageRow]:
        rows: list[ToolSourceCoverageRow] = []
        for requirement in self.requirements:
            match = _match_requirement(requirement, source_rows)
            rows.append(
                ToolSourceCoverageRow(
                    coverage_id=new_id("toolsourcecoverage"),
                    requirement=requirement,
                    matched=match is not None,
                    decision=str(match.decision) if match is not None else "",
                    target_paths=(match.target_path,) if match is not None and match.target_path else (),
                    owner_unit=match.owner_unit if match is not None else "",
                    test_entrypoints=(),
                    notes=match.capability if match is not None else "missing source decision row",
                )
            )
        return rows

    def _findings(self, rows: Sequence[ToolSourceCoverageRow]) -> list[ToolSourceCoverageFinding]:
        findings: list[ToolSourceCoverageFinding] = []
        for row in rows:
            requirement = row.requirement
            if not row.matched:
                findings.append(
                    ToolSourceCoverageFinding(
                        code="TOOL_SOURCE_REQUIREMENT_MISSING",
                        severity=ToolSourceCoverageSeverity.BLOCKER,
                        message="A required tool-loop source path has no Zyra source decision row.",
                        source_repo=requirement.source_repo,
                        source_path=requirement.source_path,
                    )
                )
                continue
            if not row.active and requirement.minimum_decision == ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED:
                findings.append(
                    ToolSourceCoverageFinding(
                        code="TOOL_SOURCE_REQUIREMENT_NOT_ACTIVE",
                        severity=ToolSourceCoverageSeverity.BLOCKER,
                        message="A required active migration source path was not mapped to an active Zyra target.",
                        source_repo=requirement.source_repo,
                        source_path=requirement.source_path,
                        metadata={"decision": row.decision},
                    )
                )
            if not row.target_paths:
                findings.append(
                    ToolSourceCoverageFinding(
                        code="TOOL_SOURCE_REQUIREMENT_WITHOUT_TARGET",
                        severity=ToolSourceCoverageSeverity.BLOCKER,
                        message="A source decision row has no target path.",
                        source_repo=requirement.source_repo,
                        source_path=requirement.source_path,
                    )
                )
        matched_repos = {row.requirement.source_repo for row in rows if row.matched}
        for repo in {"claude-code-best", "opencode", "hermes-agent"} - matched_repos:
            findings.append(
                ToolSourceCoverageFinding(
                    code="TOOL_SOURCE_REQUIRED_REPO_MISSING",
                    severity=ToolSourceCoverageSeverity.BLOCKER,
                    message="A required source repository is absent from tool-loop source coverage.",
                    source_repo=repo,
                )
            )
        return findings


def default_tool_source_requirements() -> tuple[ToolSourceRequirement, ...]:
    return (
        ToolSourceRequirement(
            requirement_id="claude-query-loop",
            source_repo="claude-code-best",
            source_path="src/query.ts",
            minimum_decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            rationale="assistant tool_use -> tool_result -> continuation loop must be owned by Zyra QueryEngine.",
        ),
        ToolSourceRequirement(
            requirement_id="claude-tool-registry",
            source_repo="claude-code-best",
            source_path="src/tools.ts",
            minimum_decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            rationale="built-in tool pool and deny filtering must be materialized into Zyra registry runtime.",
        ),
        ToolSourceRequirement(
            requirement_id="claude-tool-execution",
            source_repo="claude-code-best",
            source_path="src/services/tools/toolExecution.ts",
            minimum_decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            rationale="permission -> tool.call -> result shaping order must be productized.",
        ),
        ToolSourceRequirement(
            requirement_id="claude-streaming-tool-executor",
            source_repo="claude-code-best",
            source_path="src/services/tools/StreamingToolExecutor.ts",
            minimum_decision=ToolRuntimeSourceDecision.ZYRA_MODULE_MIGRATED,
            rationale="streaming progress frames are required by this foundation slice.",
        ),
        ToolSourceRequirement(
            requirement_id="opencode-tool-output-store",
            source_repo="opencode",
            source_path="packages/opencode/src/tool/output.ts",
            minimum_decision=ToolRuntimeSourceDecision.ACTIVE_PORT,
            rationale="opencode output-store semantics inform Zyra ToolOutputStoreRuntime.",
        ),
        ToolSourceRequirement(
            requirement_id="hermes-tool-approval",
            source_repo="hermes-agent",
            source_path="hermes/agents/approval/**",
            minimum_decision=ToolRuntimeSourceDecision.ACTIVE_PORT,
            rationale="Hermes approval/search boundaries inform permission handoff and failure policy.",
        ),
    )


def tool_source_coverage_metadata(report: ToolSourceCoverageReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_source_coverage_ok": "true",
            "tool_source_coverage_requirements": "0",
        }
    return report.metadata()


def assert_tool_source_coverage_pass(report: ToolSourceCoverageReport) -> None:
    report.require_pass()


def render_tool_source_coverage_markdown(report: ToolSourceCoverageReport) -> str:
    lines = [
        "## Tool Source Coverage",
        "",
        f"- owner_unit: `{report.owner_unit}`",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- requirements: `{report.requirement_count}`",
        f"- matched: `{report.matched_count}`",
        f"- active: `{report.active_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    lines.extend(["", "### Rows", ""])
    for row in report.rows:
        lines.append(
            f"- `{row.requirement.source_repo}` `{row.requirement.source_path}`: "
            f"matched `{str(row.matched).lower()}`, decision `{row.decision}`"
        )
    return "\n".join(lines)


def _match_requirement(
    requirement: ToolSourceRequirement,
    rows: Sequence[ToolRuntimeSourceLedgerRow],
) -> ToolRuntimeSourceLedgerRow | None:
    for row in rows:
        if row.source_repo != requirement.source_repo:
            continue
        if row.source_path == requirement.source_path or requirement.source_path in row.source_path:
            return row
    return None
