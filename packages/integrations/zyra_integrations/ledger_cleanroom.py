from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_boundary import SOURCE_REPOSITORIES, CleanBoundaryReport, build_clean_boundary_report
from .ledger_models import to_jsonable
from .ledger_source_scan import SourceScanReport, build_source_scan_report
from .ledger_store import InternalizationLedger


class CleanroomSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class CleanroomCode(StrEnum):
    COPY_ROOT_PRESENT = "COPY_ROOT_PRESENT"
    COPY_ROOT_MISSING = "COPY_ROOT_MISSING"
    EXCLUDE_ROOT_PRESENT = "EXCLUDE_ROOT_PRESENT"
    SOURCE_REPO_DEPENDENCY = "SOURCE_REPO_DEPENDENCY"
    SOURCE_SCAN_BLOCKED = "SOURCE_SCAN_BLOCKED"
    BOUNDARY_BLOCKED = "BOUNDARY_BLOCKED"
    COMMAND_READY = "COMMAND_READY"
    COMMAND_MISSING_INPUT = "COMMAND_MISSING_INPUT"
    CACHE_RISK = "CACHE_RISK"
    CLEANROOM_READY = "CLEANROOM_READY"


@dataclass(slots=True)
class CleanroomCopyPlan:
    project_root: str
    copy_roots: list[str]
    exclude_roots: list[str]
    forbidden_source_repositories: list[str]
    required_files: list[str]
    optional_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class CleanroomCommand:
    name: str
    command: list[str]
    purpose: str
    required: bool = True
    expects_network: bool = False
    writes_cache: bool = False
    requires_source_workspace: bool = False
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def cleanroom_safe(self) -> bool:
        return not self.expects_network and not self.requires_source_workspace

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["cleanroom_safe"] = self.cleanroom_safe
        return payload


@dataclass(slots=True)
class CleanroomFinding:
    code: CleanroomCode
    severity: CleanroomSeverity
    message: str
    path: str = ""
    command: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class CleanroomReport:
    ok: bool
    copy_plan: CleanroomCopyPlan
    commands: list[CleanroomCommand]
    findings: list[CleanroomFinding]
    boundary: dict[str, Any]
    source_scan: dict[str, Any]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == CleanroomSeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == CleanroomSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == CleanroomSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


DEFAULT_COPY_ROOTS = [
    "apps",
    "packages",
    "scripts",
    "tests",
    "vendor-runtimes",
    "skills",
    "pyproject.toml",
    "README.md",
    ".env.example",
]

DEFAULT_EXCLUDE_ROOTS = [
    ".git",
    ".venv",
    "tmp",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "node_modules",
    "dist",
    "build",
]


def build_cleanroom_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    source_root: Path | None = None,
    include_source_scan: bool = True,
    scan_roots: Iterable[str] | None = None,
) -> CleanroomReport:
    copy_plan = build_cleanroom_copy_plan(project_root)
    commands = default_cleanroom_commands(project_root)
    boundary = build_clean_boundary_report(
        project_root,
        include_tests=True,
        include_cache=False,
        scan_roots=list(scan_roots or ["apps", "packages", "scripts", "tests"]),
    )
    source_scan = (
        build_source_scan_report(project_root, source_root or project_root, ledger, include_tests=True)
        if include_source_scan
        else _empty_source_scan(project_root)
    )
    findings: list[CleanroomFinding] = []
    findings.extend(copy_plan_findings(project_root, copy_plan))
    findings.extend(command_findings(commands))
    findings.extend(boundary_findings(boundary))
    findings.extend(source_scan_findings(source_scan))
    ok = not any(finding.severity in {CleanroomSeverity.ERROR, CleanroomSeverity.BLOCKER} for finding in findings)
    findings.append(
        CleanroomFinding(
            code=CleanroomCode.CLEANROOM_READY,
            severity=CleanroomSeverity.INFO if ok else CleanroomSeverity.WARNING,
            message="Cleanroom scenario is ready." if ok else "Cleanroom scenario has blockers or errors.",
            remediation="Fix blocking findings before treating cleanroom verification as passed.",
        )
    )
    return CleanroomReport(
        ok=ok,
        copy_plan=copy_plan,
        commands=commands,
        findings=findings,
        boundary=boundary.to_dict(),
        source_scan=source_scan.to_dict(),
        summary={
            "copy_root_count": len(copy_plan.copy_roots),
            "required_command_count": sum(1 for command in commands if command.required),
            "cleanroom_safe_command_count": sum(1 for command in commands if command.cleanroom_safe),
            "source_scan_missing_targets": getattr(source_scan, "missing_target_count", 0),
            "source_scan_forbidden_hits": len(getattr(source_scan, "forbidden_hits", [])),
            "boundary_blockers": boundary.blocker_count,
            "boundary_errors": boundary.error_count,
        },
    )


def build_cleanroom_copy_plan(project_root: Path) -> CleanroomCopyPlan:
    return CleanroomCopyPlan(
        project_root=str(project_root),
        copy_roots=list(DEFAULT_COPY_ROOTS),
        exclude_roots=list(DEFAULT_EXCLUDE_ROOTS),
        forbidden_source_repositories=list(SOURCE_REPOSITORIES),
        required_files=["pyproject.toml", "README.md", "scripts/zyra_integration_ledger.py", "scripts/verify_submission_boundary.py"],
        optional_files=[".env.example"],
    )


def default_cleanroom_commands(project_root: Path) -> list[CleanroomCommand]:
    python = ".\\.venv\\Scripts\\python.exe"
    if not (project_root / ".venv" / "Scripts" / "python.exe").exists():
        python = "python"
    return [
        CleanroomCommand(
            name="unit-tests",
            command=[python, "-m", "unittest", "discover", "-s", "tests"],
            purpose="Run submitted tests without parent source repositories.",
            writes_cache=True,
        ),
        CleanroomCommand(
            name="submission-boundary",
            command=[python, "scripts\\verify_submission_boundary.py"],
            purpose="Verify no runtime path depends on parent source repositories.",
        ),
        CleanroomCommand(
            name="ledger-audit",
            command=[python, "scripts\\zyra_integration_ledger.py", "audit", "--strict", "--json"],
            purpose="Verify ledger schema, policy, source coverage, and forbidden refs.",
        ),
        CleanroomCommand(
            name="ledger-boundary",
            command=[python, "scripts\\zyra_integration_ledger.py", "boundary", "--json"],
            purpose="Verify clean submission boundary from CLI.",
        ),
        CleanroomCommand(
            name="ledger-reachability",
            command=[python, "scripts\\zyra_integration_ledger.py", "reachability", "--owner-unit", "M1-01A", "--json"],
            purpose="Verify main-path reachability control plane.",
        ),
        CleanroomCommand(
            name="ledger-acceptance",
            command=[python, "scripts\\zyra_integration_ledger.py", "acceptance", "--owner-unit", "M1-01A", "--json"],
            purpose="Build M1-01A anti-fake acceptance report.",
        ),
    ]


def copy_plan_findings(project_root: Path, plan: CleanroomCopyPlan) -> list[CleanroomFinding]:
    findings: list[CleanroomFinding] = []
    for root in plan.copy_roots:
        path = project_root / root
        if path.exists():
            findings.append(
                CleanroomFinding(
                    code=CleanroomCode.COPY_ROOT_PRESENT,
                    severity=CleanroomSeverity.INFO,
                    message=f"Cleanroom copy root exists: {root}",
                    path=root,
                )
            )
        elif root in plan.required_files:
            findings.append(
                CleanroomFinding(
                    code=CleanroomCode.COPY_ROOT_MISSING,
                    severity=CleanroomSeverity.ERROR,
                    message=f"Required cleanroom copy root is missing: {root}",
                    path=root,
                    remediation="Add the file/directory or update cleanroom copy plan.",
                )
            )
    for excluded in plan.exclude_roots:
        if (project_root / excluded).exists():
            findings.append(
                CleanroomFinding(
                    code=CleanroomCode.EXCLUDE_ROOT_PRESENT,
                    severity=CleanroomSeverity.INFO,
                    message=f"Excluded root exists locally and must not be copied: {excluded}",
                    path=excluded,
                )
            )
    return findings


def command_findings(commands: Iterable[CleanroomCommand]) -> list[CleanroomFinding]:
    findings: list[CleanroomFinding] = []
    for command in commands:
        if command.requires_source_workspace:
            findings.append(
                CleanroomFinding(
                    code=CleanroomCode.COMMAND_MISSING_INPUT,
                    severity=CleanroomSeverity.BLOCKER if command.required else CleanroomSeverity.WARNING,
                    message=f"Cleanroom command requires parent source workspace: {command.name}",
                    command=" ".join(command.command),
                    remediation="Rewrite command to consume only checked-in zyra files or mark it development-only.",
                )
            )
        else:
            findings.append(
                CleanroomFinding(
                    code=CleanroomCode.COMMAND_READY,
                    severity=CleanroomSeverity.INFO,
                    message=f"Cleanroom command is self-contained: {command.name}",
                    command=" ".join(command.command),
                )
            )
        if command.writes_cache:
            findings.append(
                CleanroomFinding(
                    code=CleanroomCode.CACHE_RISK,
                    severity=CleanroomSeverity.INFO,
                    message=f"Command may write cache artifacts: {command.name}",
                    command=" ".join(command.command),
                    remediation="Clean cache before line-count or packaging evidence.",
                )
            )
    return findings


def boundary_findings(report: CleanBoundaryReport) -> list[CleanroomFinding]:
    if report.ok:
        return [
            CleanroomFinding(
                code=CleanroomCode.BOUNDARY_BLOCKED,
                severity=CleanroomSeverity.INFO,
                message="Boundary report has no blockers or errors.",
            )
        ]
    return [
        CleanroomFinding(
            code=CleanroomCode.BOUNDARY_BLOCKED,
            severity=CleanroomSeverity.BLOCKER if report.blocker_count else CleanroomSeverity.ERROR,
            message=f"Boundary report failed with blockers={report.blocker_count} errors={report.error_count}.",
            remediation="Remove parent source dependencies before cleanroom execution.",
        )
    ]


def source_scan_findings(report: SourceScanReport) -> list[CleanroomFinding]:
    findings: list[CleanroomFinding] = []
    forbidden_hits = getattr(report, "forbidden_hits", [])
    missing_targets = getattr(report, "missing_target_count", 0)
    if forbidden_hits:
        findings.append(
            CleanroomFinding(
                code=CleanroomCode.SOURCE_REPO_DEPENDENCY,
                severity=CleanroomSeverity.BLOCKER,
                message=f"Source scan found {len(forbidden_hits)} forbidden parent source dependencies.",
                remediation="Move source code into zyra or whitelist only dedicated negative test fixtures.",
            )
        )
    if missing_targets:
        findings.append(
            CleanroomFinding(
                code=CleanroomCode.SOURCE_SCAN_BLOCKED,
                severity=CleanroomSeverity.ERROR,
                message=f"Source scan found {missing_targets} missing materialized/connected targets.",
                remediation="Create targets or downgrade ledger status.",
            )
        )
    if not findings:
        findings.append(
            CleanroomFinding(
                code=CleanroomCode.SOURCE_SCAN_BLOCKED,
                severity=CleanroomSeverity.INFO,
                message="Source scan has no cleanroom blockers.",
            )
        )
    return findings


def cleanroom_payload(report: CleanroomReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {CleanroomSeverity.ERROR, CleanroomSeverity.BLOCKER}
    ]
    payload["commands_to_run"] = [command.to_dict() for command in report.commands if command.required]
    return payload


def assert_cleanroom_ready(report: CleanroomReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.path or finding.command}: {finding.message}"
        for finding in report.findings
        if finding.severity in {CleanroomSeverity.ERROR, CleanroomSeverity.BLOCKER}
    )
    raise AssertionError(f"Cleanroom report failed:\n{formatted}")


def write_cleanroom_plan(path: Path, report: CleanroomReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cleanroom_payload(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _empty_source_scan(project_root: Path) -> SourceScanReport:
    from .ledger_source_scan import SourceScanReport

    return SourceScanReport(
        project_root=str(project_root),
        source_root=str(project_root),
        scanned_files=0,
        source_verifications=[],
        target_verifications=[],
        forbidden_hits=[],
        missing_source_count=0,
        missing_target_count=0,
        unverified_evidence_count=0,
    )
