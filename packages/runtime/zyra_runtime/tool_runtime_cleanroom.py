from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .tool_runtime_foundation import (
    TOOL_LOOP_FOUNDATION_OWNER_UNIT,
    TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ToolRuntimeSourceLedgerRow,
    tool_runtime_source_to_target_rows,
)


class ToolCleanroomStatus(StrEnum):
    PASS = "pass"
    BLOCKED = "blocked"


class ToolCleanroomSeverity(StrEnum):
    INFO = "info"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ToolCleanroomFinding:
    code: str
    severity: ToolCleanroomSeverity
    message: str
    path: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolCleanroomSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "blocking": self.blocking,
            "message": self.message,
            "path": self.path,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolCleanroomPathCheck:
    check_id: str
    path: str
    category: str
    clean: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "path": self.path,
            "category": self.category,
            "clean": self.clean,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ToolCleanroomReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    checks: tuple[ToolCleanroomPathCheck, ...]
    findings: tuple[ToolCleanroomFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolCleanroomStatus:
        return ToolCleanroomStatus.PASS if self.ok else ToolCleanroomStatus.BLOCKED

    @property
    def check_count(self) -> int:
        return len(self.checks)

    @property
    def clean_count(self) -> int:
        return sum(1 for check in self.checks if check.clean)

    @property
    def blocked_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def blocked_paths(self) -> tuple[str, ...]:
        return tuple(finding.path for finding in self.findings if finding.blocking and finding.path)

    def require_clean(self) -> None:
        if self.ok:
            return
        paths = ", ".join(self.blocked_paths) or "unknown"
        raise AssertionError(f"tool cleanroom blocked paths: {paths}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "check_count": self.check_count,
            "clean_count": self.clean_count,
            "blocked_count": self.blocked_count,
            "blocked_paths": list(self.blocked_paths),
            "checks": [check.to_dict() for check in self.checks],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_cleanroom_report_id": self.report_id,
            "tool_cleanroom_owner_unit": self.owner_unit,
            "tool_cleanroom_ok": str(self.ok).lower(),
            "tool_cleanroom_status": str(self.status),
            "tool_cleanroom_checks": str(self.check_count),
            "tool_cleanroom_clean": str(self.clean_count),
            "tool_cleanroom_blocked": str(self.blocked_count),
        }


class ToolCleanroomRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
        forbidden_segments: Sequence[str] | None = None,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.forbidden_segments = tuple(forbidden_segments or ("vendor", "vendor-runtimes", "source-pool", "runtime-sources"))

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        target_paths: Sequence[str] | None = None,
        source_rows: Sequence[ToolRuntimeSourceLedgerRow] | None = None,
    ) -> ToolCleanroomReport:
        rows = tool_runtime_source_to_target_rows() if source_rows is None else tuple(source_rows)
        paths = list(target_paths or [])
        paths.extend(row.target_path for row in rows)
        checks = tuple(self._check(path) for path in sorted({path for path in paths if path}))
        findings = tuple(self._findings(checks))
        return ToolCleanroomReport(
            report_id=new_id("toolcleanroom"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            checks=checks,
            findings=findings,
        )

    def event_for_report(
        self,
        report: ToolCleanroomReport,
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
                    "phase": "tool_cleanroom_report",
                    "tool_cleanroom_report": report.to_dict(),
                }
            },
        )

    def _check(self, path: str) -> ToolCleanroomPathCheck:
        normalized = path.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if normalized.startswith("../") or "/../" in normalized:
            return ToolCleanroomPathCheck(new_id("toolcleanpath"), path, "relative_escape", False, "path escapes zyra root")
        for segment in self.forbidden_segments:
            if segment in parts:
                return ToolCleanroomPathCheck(new_id("toolcleanpath"), path, "forbidden_segment", False, f"contains {segment}")
        if normalized.startswith("packages/") or normalized.startswith("apps/") or normalized.startswith("skills/") or normalized.startswith("scripts/"):
            return ToolCleanroomPathCheck(new_id("toolcleanpath"), path, "zyra_owned", True)
        return ToolCleanroomPathCheck(new_id("toolcleanpath"), path, "other", True)

    def _findings(self, checks: Sequence[ToolCleanroomPathCheck]) -> list[ToolCleanroomFinding]:
        findings: list[ToolCleanroomFinding] = []
        for check in checks:
            if not check.clean:
                findings.append(
                    ToolCleanroomFinding(
                        code="TOOL_CLEANROOM_FORBIDDEN_PATH",
                        severity=ToolCleanroomSeverity.BLOCKER,
                        message="Tool foundation target path violates cleanroom constraints.",
                        path=check.path,
                        metadata={"category": check.category, "reason": check.reason},
                    )
                )
        return findings


def tool_cleanroom_metadata(report: ToolCleanroomReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_cleanroom_ok": "true",
            "tool_cleanroom_checks": "0",
        }
    return report.metadata()


def assert_tool_cleanroom_pass(report: ToolCleanroomReport) -> None:
    report.require_clean()


def render_tool_cleanroom_markdown(report: ToolCleanroomReport) -> str:
    lines = [
        "## Tool Cleanroom Report",
        "",
        f"- owner_unit: `{report.owner_unit}`",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- checks: `{report.check_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.path}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)
