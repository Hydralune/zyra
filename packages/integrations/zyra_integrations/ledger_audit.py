from __future__ import annotations

import ast
import os
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .ledger_models import (
    AuditDisposition,
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LineCountPolicy,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    to_jsonable,
)
from .ledger_policy import (
    UNIT_BUDGETS,
    LedgerPolicySeverity,
    classify_path,
    validate_entry_policy,
)
from .ledger_store import InternalizationLedger


class AuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class AuditFindingCode(StrEnum):
    INITIAL_SEED_INCOMPLETE = "INITIAL_SEED_INCOMPLETE"
    MISSING_TARGET_PATH = "MISSING_TARGET_PATH"
    TARGET_OUTSIDE_ZYRA = "TARGET_OUTSIDE_ZYRA"
    TARGET_PATH_NOT_FOUND = "TARGET_PATH_NOT_FOUND"
    MISSING_TEST_ENTRY = "MISSING_TEST_ENTRY"
    MISSING_RUNTIME_ENTRY = "MISSING_RUNTIME_ENTRY"
    MISSING_MAIN_PATH_STATUS = "MISSING_MAIN_PATH_STATUS"
    FALSE_CONNECTED_STATUS = "FALSE_CONNECTED_STATUS"
    FORBIDDEN_RELATIVE_SOURCE_DEP = "FORBIDDEN_RELATIVE_SOURCE_DEP"
    MISSING_LICENSE_NOTICE = "MISSING_LICENSE_NOTICE"
    INVALID_LINE_COUNT_POLICY = "INVALID_LINE_COUNT_POLICY"
    VENDOR_POOL_WITHOUT_BOUNDARY = "VENDOR_POOL_WITHOUT_BOUNDARY"
    DUPLICATE_RECORD_ID = "DUPLICATE_RECORD_ID"
    CONFLICTING_TARGET_OWNER = "CONFLICTING_TARGET_OWNER"
    SCHEMA_VALIDATION_ERROR = "SCHEMA_VALIDATION_ERROR"
    PLANNED_TARGET_NOT_MATERIALIZED = "PLANNED_TARGET_NOT_MATERIALIZED"
    SOURCE_PATH_NOT_VERIFIED = "SOURCE_PATH_NOT_VERIFIED"
    AUDIT_EVENT_NOT_WRITTEN = "AUDIT_EVENT_NOT_WRITTEN"
    POLICY_VALIDATION_ERROR = "POLICY_VALIDATION_ERROR"
    POLICY_VALIDATION_WARNING = "POLICY_VALIDATION_WARNING"
    EXECUTION_UNIT_COVERAGE_INCOMPLETE = "EXECUTION_UNIT_COVERAGE_INCOMPLETE"
    MATERIALIZED_TARGET_IS_DATA_ONLY = "MATERIALIZED_TARGET_IS_DATA_ONLY"
    COUNTABLE_FILE_UNMAPPED_IN_LEDGER = "COUNTABLE_FILE_UNMAPPED_IN_LEDGER"


REQUIRED_SOURCE_REPOS = {
    "claude-code-best",
    "browser-use",
    "OpenHands",
    "agentscope",
    "agent-framework",
    "hermes-agent",
    "langgraph",
}

ALLOWED_TARGET_PREFIXES = (
    "apps/",
    "packages/",
    "tests/",
    "scripts/",
    "vendor-runtimes/",
    "skills/",
    "third_party/",
    "tmp/",
)

CONNECTED_STATUSES = {
    MainPathStatus.API_CONNECTED,
    MainPathStatus.EVENT_LOG_CONNECTED,
    MainPathStatus.CONTROL_COMMAND_CONNECTED,
    MainPathStatus.WORKER_RUNTIME_CONNECTED,
    MainPathStatus.UI_CONNECTED,
    MainPathStatus.TESTED_MAIN_PATH,
}

MATERIALIZED_LIFECYCLES = {
    LedgerLifecycle.ACTIVE,
    LedgerLifecycle.INTERNALIZED,
    LedgerLifecycle.PRODUCTIZED,
}

RUNTIME_STRATEGIES = {
    MigrationStrategy.ADAPTER,
    MigrationStrategy.SIDECAR_RUNTIME,
    MigrationStrategy.VENDORED_RUNTIME,
    MigrationStrategy.DIRECT_PORT,
}

VENDOR_LIKE_RUNTIME_ROOTS = (
    Path("packages/integrations/loopx_runtime"),
)

NON_RUNTIME_DATA_ROOTS = (
    Path("packages/integrations/zyra_integrations/data"),
)


@dataclass(slots=True)
class LedgerAuditFinding:
    code: AuditFindingCode
    severity: AuditSeverity
    message: str
    ledger_id: str = ""
    source_repo: str = ""
    source_path: str = ""
    target_path: str = ""
    owner_unit: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerAuditReport:
    ok: bool
    disposition: AuditDisposition
    total_entries: int
    finding_count: int
    error_count: int
    blocker_count: int
    warning_count: int
    findings: list[LedgerAuditFinding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    strict: bool = True
    event_written: bool = False

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    def fail_if_errors(self) -> None:
        if not self.ok:
            formatted = "\n".join(f"{finding.severity} {finding.code}: {finding.message}" for finding in self.findings)
            raise AssertionError(f"Internalization ledger audit failed:\n{formatted}")


class InternalizationLedgerAuditor:
    def __init__(self, project_root: str | Path, *, strict: bool = True) -> None:
        self.project_root = Path(project_root)
        self.strict = strict

    def audit(self, ledger: InternalizationLedger) -> LedgerAuditReport:
        findings: list[LedgerAuditFinding] = []
        findings.extend(self._audit_seed_coverage(ledger))
        findings.extend(self._audit_execution_unit_coverage(ledger))
        findings.extend(self._audit_schema(ledger))
        findings.extend(self._audit_target_ownership(ledger))
        for entry in ledger.entries():
            findings.extend(self.audit_entry(entry))
        if self.strict:
            findings.extend(self._audit_forbidden_runtime_dependencies())
        return self._build_report(ledger, findings)

    def audit_entry(self, entry: InternalizationLedgerEntry) -> list[LedgerAuditFinding]:
        findings: list[LedgerAuditFinding] = []
        findings.extend(self._audit_targets(entry))
        findings.extend(self._audit_tests(entry))
        findings.extend(self._audit_main_path(entry))
        findings.extend(self._audit_runtime(entry))
        findings.extend(self._audit_license(entry))
        findings.extend(self._audit_line_count_policy(entry))
        findings.extend(self._audit_source_evidence(entry))
        findings.extend(self._audit_policy(entry))
        return findings

    def _audit_seed_coverage(self, ledger: InternalizationLedger) -> list[LedgerAuditFinding]:
        present = {entry.source_repo for entry in ledger.entries()}
        missing = sorted(REQUIRED_SOURCE_REPOS - present)
        if not missing:
            return []
        return [
            LedgerAuditFinding(
                code=AuditFindingCode.INITIAL_SEED_INCOMPLETE,
                severity=AuditSeverity.BLOCKER,
                message=f"Initial ledger seed is missing required source repos: {', '.join(missing)}",
                remediation="Add at least one source-to-target record for every required repository.",
                metadata={"missing_repositories": missing},
            )
        ]

    def _audit_schema(self, ledger: InternalizationLedger) -> list[LedgerAuditFinding]:
        findings: list[LedgerAuditFinding] = []
        for error in ledger.validate_schema():
            ledger_id = error.split(":", 1)[0] if ":" in error else ""
            findings.append(
                LedgerAuditFinding(
                    code=AuditFindingCode.SCHEMA_VALIDATION_ERROR,
                    severity=AuditSeverity.ERROR,
                    ledger_id=ledger_id,
                    message=error,
                    remediation="Fix the ledger entry so all required schema fields are present and valid.",
                )
            )
        return findings

    def _audit_execution_unit_coverage(self, ledger: InternalizationLedger) -> list[LedgerAuditFinding]:
        present = {entry.owner_unit for entry in ledger.entries() if entry.owner_unit}
        missing = sorted(
            unit
            for unit, budget in UNIT_BUDGETS.items()
            if budget.requires_source_migration and unit not in present
        )
        if not missing:
            return []
        return [
            LedgerAuditFinding(
                code=AuditFindingCode.EXECUTION_UNIT_COVERAGE_INCOMPLETE,
                severity=AuditSeverity.WARNING,
                message=f"Ledger has no source-to-target records for execution units: {', '.join(missing)}",
                remediation="Add planned ledger records or document why the unit has no source-mapped migration objects.",
                metadata={"missing_owner_units": missing, "required_owner_units": sorted(UNIT_BUDGETS)},
            )
        ]

    def _audit_targets(self, entry: InternalizationLedgerEntry) -> list[LedgerAuditFinding]:
        findings: list[LedgerAuditFinding] = []
        if not entry.target_bindings:
            return [
                self._entry_finding(
                    entry,
                    AuditFindingCode.MISSING_TARGET_PATH,
                    AuditSeverity.BLOCKER,
                    "Ledger entry has no target path.",
                    remediation="Assign at least one project-relative target path under packages/apps/tests/scripts/vendor-runtimes/skills.",
                )
            ]
        for binding in entry.target_bindings:
            target_path = binding.target_path
            if not target_path:
                findings.append(
                    self._entry_finding(
                        entry,
                        AuditFindingCode.MISSING_TARGET_PATH,
                        AuditSeverity.BLOCKER,
                        "Ledger entry has an empty target path.",
                        target_path=target_path,
                        remediation="Fill target_path before the entry can be used by later migration units.",
                    )
                )
                continue
            normalized = target_path.replace("\\", "/")
            if not normalized.startswith(ALLOWED_TARGET_PREFIXES):
                findings.append(
                    self._entry_finding(
                        entry,
                        AuditFindingCode.TARGET_OUTSIDE_ZYRA,
                        AuditSeverity.ERROR,
                        f"Target path is outside allowed Zyra surfaces: {target_path}",
                        target_path=target_path,
                        remediation="Move the target to packages/apps/tests/scripts/vendor-runtimes/skills or mark the record rejected.",
                    )
                )
            target_abs = self.project_root / normalized
            status_requires_materialization = (
                entry.lifecycle in MATERIALIZED_LIFECYCLES
                or entry.main_path_status in set(binding.must_exist_for_statuses)
            )
            if status_requires_materialization and not target_abs.exists():
                findings.append(
                    self._entry_finding(
                        entry,
                        AuditFindingCode.TARGET_PATH_NOT_FOUND,
                        AuditSeverity.ERROR,
                        f"Entry is marked {entry.lifecycle}/{entry.main_path_status} but target does not exist: {target_path}",
                        target_path=target_path,
                        remediation="Create the target module/test/runtime or downgrade the lifecycle to planned/candidate.",
                    )
                )
            elif not target_abs.exists() and entry.lifecycle in {LedgerLifecycle.PLANNED, LedgerLifecycle.CANDIDATE, LedgerLifecycle.IN_PROGRESS}:
                findings.append(
                    self._entry_finding(
                        entry,
                        AuditFindingCode.PLANNED_TARGET_NOT_MATERIALIZED,
                        AuditSeverity.WARNING,
                        f"Planned target is not materialized yet: {target_path}",
                        target_path=target_path,
                        remediation="Later execution units must create or productize this path before marking the entry connected.",
                    )
                )
        return findings

    def _audit_tests(self, entry: InternalizationLedgerEntry) -> list[LedgerAuditFinding]:
        if entry.lifecycle in {LedgerLifecycle.PLANNED, LedgerLifecycle.CANDIDATE, LedgerLifecycle.DEFERRED, LedgerLifecycle.REJECTED}:
            if entry.test_entries:
                return []
            return [
                self._entry_finding(
                    entry,
                    AuditFindingCode.MISSING_TEST_ENTRY,
                    AuditSeverity.WARNING,
                    "Planned ledger entry has no planned test entry.",
                    remediation="Add a planned test path so later units have a verification target.",
                )
            ]
        if entry.test_entries:
            return []
        return [
            self._entry_finding(
                entry,
                AuditFindingCode.MISSING_TEST_ENTRY,
                AuditSeverity.ERROR,
                "Active/internalized ledger entry has no test entry.",
                remediation="Add unit, integration, smoke, or verification command coverage.",
            )
        ]

    def _audit_main_path(self, entry: InternalizationLedgerEntry) -> list[LedgerAuditFinding]:
        findings: list[LedgerAuditFinding] = []
        if not str(entry.main_path_status):
            findings.append(
                self._entry_finding(
                    entry,
                    AuditFindingCode.MISSING_MAIN_PATH_STATUS,
                    AuditSeverity.ERROR,
                    "main_path_status is missing.",
                    remediation="Set a status such as planned, event_log_connected, worker_runtime_connected, or tested_main_path.",
                )
            )
        if entry.main_path_status in CONNECTED_STATUSES and entry.main_path.is_empty():
            findings.append(
                self._entry_finding(
                    entry,
                    AuditFindingCode.FALSE_CONNECTED_STATUS,
                    AuditSeverity.ERROR,
                    f"Entry is marked {entry.main_path_status} but has no API/event/control/worker/UI binding.",
                    remediation="Add concrete main_path references or downgrade the status.",
                )
            )
        return findings

    def _audit_runtime(self, entry: InternalizationLedgerEntry) -> list[LedgerAuditFinding]:
        findings: list[LedgerAuditFinding] = []
        runtime_required = entry.migration_strategy in RUNTIME_STRATEGIES or entry.main_path_status in CONNECTED_STATUSES
        if runtime_required and entry.lifecycle not in {LedgerLifecycle.PLANNED, LedgerLifecycle.CANDIDATE, LedgerLifecycle.DEFERRED, LedgerLifecycle.REJECTED} and entry.runtime_entry.is_empty():
            findings.append(
                self._entry_finding(
                    entry,
                    AuditFindingCode.MISSING_RUNTIME_ENTRY,
                    AuditSeverity.ERROR,
                    "Entry claims runtime migration or connected status but runtime_entry is empty.",
                    remediation="Add command/module/function/protocol/health_check runtime metadata.",
                )
            )
        if entry.migration_strategy == MigrationStrategy.VENDORED_RUNTIME and entry.main_path.is_empty():
            findings.append(
                self._entry_finding(
                    entry,
                    AuditFindingCode.VENDOR_POOL_WITHOUT_BOUNDARY,
                    AuditSeverity.WARNING if entry.lifecycle in {LedgerLifecycle.PLANNED, LedgerLifecycle.CANDIDATE} else AuditSeverity.ERROR,
                    "Vendored runtime entry has no main-path boundary recorded.",
                    remediation="Record sidecar/API/worker/event boundary, or mark it as inventory-only.",
                )
            )
        return findings

    def _audit_license(self, entry: InternalizationLedgerEntry) -> list[LedgerAuditFinding]:
        if entry.migration_strategy not in {MigrationStrategy.DIRECT_PORT, MigrationStrategy.VENDORED_RUNTIME, MigrationStrategy.SIDECAR_RUNTIME, MigrationStrategy.ADAPTER}:
            return []
        if entry.license_notice is None or entry.license_notice.status in {NoticeStatus.PENDING, NoticeStatus.NEEDS_REVIEW}:
            severity = AuditSeverity.WARNING if entry.lifecycle in {LedgerLifecycle.PLANNED, LedgerLifecycle.CANDIDATE} else AuditSeverity.ERROR
            return [
                self._entry_finding(
                    entry,
                    AuditFindingCode.MISSING_LICENSE_NOTICE,
                    severity,
                    "Migration/runtime entry has no completed license/NOTICE status.",
                    remediation="Record license and NOTICE handling before productization/freeze.",
                )
            ]
        return []

    def _audit_line_count_policy(self, entry: InternalizationLedgerEntry) -> list[LedgerAuditFinding]:
        if entry.lifecycle in MATERIALIZED_LIFECYCLES and entry.line_count_policy in {
            LineCountPolicy.EXCLUDED_DOCUMENTATION,
            LineCountPolicy.EXCLUDED_INVENTORY_ONLY,
        }:
            return [
                self._entry_finding(
                    entry,
                    AuditFindingCode.INVALID_LINE_COUNT_POLICY,
                    AuditSeverity.ERROR,
                    f"Materialized entry cannot use line_count_policy={entry.line_count_policy}.",
                    remediation="Use counts_as_runtime/test/script or downgrade lifecycle.",
                )
            ]
        if entry.lifecycle in MATERIALIZED_LIFECYCLES:
            data_only_targets = [
                target
                for target in entry.target_paths
                if classify_path(target).is_generated_data and not classify_path(target).is_source_like
            ]
            if data_only_targets and len(data_only_targets) == len(entry.target_paths):
                return [
                    self._entry_finding(
                        entry,
                        AuditFindingCode.MATERIALIZED_TARGET_IS_DATA_ONLY,
                        AuditSeverity.ERROR,
                        "Materialized entry targets only seed/inventory/data files.",
                        target_path=data_only_targets[0],
                        remediation="Add real source/runtime/test target paths before marking the entry materialized.",
                        metadata={"data_only_targets": data_only_targets},
                    )
                ]
        return []

    def _audit_policy(self, entry: InternalizationLedgerEntry) -> list[LedgerAuditFinding]:
        findings: list[LedgerAuditFinding] = []
        for policy_finding in validate_entry_policy(entry):
            severity = (
                AuditSeverity.BLOCKER
                if policy_finding.severity == LedgerPolicySeverity.BLOCKER
                else AuditSeverity.ERROR
                if policy_finding.severity == LedgerPolicySeverity.ERROR
                else AuditSeverity.WARNING
                if policy_finding.severity == LedgerPolicySeverity.WARNING
                else AuditSeverity.INFO
            )
            code = AuditFindingCode.POLICY_VALIDATION_ERROR if severity in {AuditSeverity.ERROR, AuditSeverity.BLOCKER} else AuditFindingCode.POLICY_VALIDATION_WARNING
            findings.append(
                self._entry_finding(
                    entry,
                    code,
                    severity,
                    policy_finding.message,
                    target_path=policy_finding.path,
                    remediation=policy_finding.remediation,
                    metadata={
                        "policy_code": str(policy_finding.code),
                        "field": policy_finding.field,
                        **policy_finding.metadata,
                    },
                )
            )
        return findings

    def _audit_source_evidence(self, entry: InternalizationLedgerEntry) -> list[LedgerAuditFinding]:
        if entry.source_evidence and any(evidence.exists_in_workspace for evidence in entry.source_evidence):
            return []
        return [
            self._entry_finding(
                entry,
                AuditFindingCode.SOURCE_PATH_NOT_VERIFIED,
                AuditSeverity.WARNING,
                "Source path has not been verified against the current workspace snapshot.",
                remediation="Run the seed builder or source mapper against the source repositories.",
            )
        ]

    def _audit_target_ownership(self, ledger: InternalizationLedger) -> list[LedgerAuditFinding]:
        findings: list[LedgerAuditFinding] = []
        owners: dict[str, list[InternalizationLedgerEntry]] = {}
        for entry in ledger.entries():
            for target in entry.target_paths:
                owners.setdefault(target, []).append(entry)
        for target, entries in sorted(owners.items()):
            materialized_entries = [
                entry
                for entry in entries
                if entry.lifecycle in MATERIALIZED_LIFECYCLES or entry.main_path_status in CONNECTED_STATUSES
            ]
            if len(materialized_entries) <= 1:
                continue
            entries = materialized_entries
            source_repos = {entry.source_repo for entry in entries}
            owner_units = {entry.owner_unit for entry in entries}
            if len(entries) > 1 and len(source_repos) > 1 and len(owner_units) > 1:
                findings.append(
                    LedgerAuditFinding(
                        code=AuditFindingCode.CONFLICTING_TARGET_OWNER,
                        severity=AuditSeverity.WARNING,
                        target_path=target,
                        message=f"Target path has multiple source owners: {', '.join(sorted(source_repos))}",
                        remediation="Keep this only if the target is a shared adapter; otherwise split target paths.",
                        metadata={
                            "ledger_ids": [entry.ledger_id for entry in entries],
                            "owner_units": sorted(owner_units),
                        },
                    )
                )
        return findings

    def _audit_forbidden_runtime_dependencies(self) -> list[LedgerAuditFinding]:
        findings: list[LedgerAuditFinding] = []
        forbidden = _forbidden_fragments()
        for path in self._iter_scanned_project_files():
            relative = path.relative_to(self.project_root).as_posix()
            if relative.startswith("scripts/remediation/"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            fragments = (
                _python_runtime_dependency_fragments(text, forbidden)
                if path.suffix.lower() == ".py"
                else [fragment for fragment in forbidden if fragment in text]
            )
            for fragment in fragments:
                findings.append(
                    LedgerAuditFinding(
                        code=AuditFindingCode.FORBIDDEN_RELATIVE_SOURCE_DEP,
                        severity=AuditSeverity.BLOCKER,
                        message=f"{relative} contains forbidden runtime dependency {fragment!r}",
                        target_path=relative,
                        remediation="Move the source code into zyra or use a productized vendor runtime inside zyra.",
                    )
                )
        return findings

    def _iter_scanned_project_files(self) -> list[Path]:
        suffixes = {".bat", ".cmd", ".css", ".html", ".js", ".json", ".jsx", ".mjs", ".ps1", ".py", ".sh", ".toml", ".ts", ".tsx", ".yaml", ".yml"}
        ignored_dirs = {
            ".cache",
            ".git",
            ".mypy_cache",
            ".next",
            ".nox",
            ".nuxt",
            ".pytest_cache",
            ".ruff_cache",
            ".tmp",
            ".tox",
            ".venv",
            "__pycache__",
            "build",
            "coverage",
            "dist",
            "docs",
            "node_modules",
            "provenance",
            "tests",
            "tmp",
        }
        git_files = self._git_scanned_project_files(suffixes, ignored_dirs)
        if git_files is not None:
            return git_files
        return self._walk_scanned_project_files(suffixes, ignored_dirs)

    def _git_scanned_project_files(self, suffixes: set[str], ignored_dirs: set[str]) -> list[Path] | None:
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={self.project_root.as_posix()}",
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                cwd=self.project_root,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None

        files: list[Path] = []
        for raw_path in completed.stdout.splitlines():
            if not raw_path:
                continue
            relative = Path(raw_path)
            if (
                relative.suffix.lower() not in suffixes
                or set(relative.parts) & ignored_dirs
                or _is_non_runtime_scan_path(relative)
            ):
                continue
            candidate = self.project_root / relative
            if candidate.is_file():
                files.append(candidate)
        return files

    def _walk_scanned_project_files(self, suffixes: set[str], ignored_dirs: set[str]) -> list[Path]:
        files: list[Path] = []
        for root, dirnames, filenames in os.walk(self.project_root):
            root_path = Path(root)
            try:
                relative = root_path.relative_to(self.project_root)
                relative_parts = set(relative.parts)
            except ValueError:
                continue
            if relative_parts & ignored_dirs or _is_non_runtime_scan_path(relative):
                dirnames[:] = []
                continue
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if dirname not in ignored_dirs
                and not _is_non_runtime_scan_path(relative / dirname)
            ]
            for filename in filenames:
                path = root_path / filename
                if path.suffix.lower() in suffixes:
                    files.append(path)
        return files

    def _entry_finding(
        self,
        entry: InternalizationLedgerEntry,
        code: AuditFindingCode,
        severity: AuditSeverity,
        message: str,
        *,
        target_path: str = "",
        remediation: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> LedgerAuditFinding:
        return LedgerAuditFinding(
            code=code,
            severity=severity,
            message=message,
            ledger_id=entry.ledger_id,
            source_repo=entry.source_repo,
            source_path=entry.source_path,
            target_path=target_path,
            owner_unit=entry.owner_unit,
            remediation=remediation,
            metadata=metadata or {},
        )

    def _build_report(self, ledger: InternalizationLedger, findings: list[LedgerAuditFinding]) -> LedgerAuditReport:
        blocker_count = sum(1 for finding in findings if finding.severity == AuditSeverity.BLOCKER)
        error_count = sum(1 for finding in findings if finding.severity == AuditSeverity.ERROR)
        warning_count = sum(1 for finding in findings if finding.severity == AuditSeverity.WARNING)
        ok = blocker_count == 0 and error_count == 0
        disposition = AuditDisposition.PASSING
        if blocker_count:
            disposition = AuditDisposition.BLOCKED
        elif error_count:
            disposition = AuditDisposition.FAILING
        elif warning_count:
            disposition = AuditDisposition.WARNING
        return LedgerAuditReport(
            ok=ok,
            disposition=disposition,
            total_entries=len(ledger),
            finding_count=len(findings),
            error_count=error_count,
            blocker_count=blocker_count,
            warning_count=warning_count,
            findings=findings,
            summary=ledger.summary().to_dict(),
            strict=self.strict,
        )


def filter_findings(
    findings: list[LedgerAuditFinding],
    *,
    severity: str = "",
    source_repo: str = "",
    owner_unit: str = "",
    code: str = "",
) -> list[LedgerAuditFinding]:
    result: list[LedgerAuditFinding] = []
    for finding in findings:
        if severity and str(finding.severity) != severity:
            continue
        if source_repo and finding.source_repo != source_repo:
            continue
        if owner_unit and finding.owner_unit != owner_unit:
            continue
        if code and str(finding.code) != code:
            continue
        result.append(finding)
    return result


def _forbidden_fragments() -> list[str]:
    repos = [
        "agent-framework",
        "agentscope",
        "browser-use",
        "claude-code-best",
        "hermes-agent",
        "langgraph",
        "openclaw",
        "OpenHands",
    ]
    fragments: list[str] = []
    for repo in repos:
        fragments.extend(
            [
                f"../{repo}",
                f"..\\{repo}",
                f"G:\\agent-zoo\\{repo}",
                f"g:\\agent-zoo\\{repo}",
            ]
        )
    return fragments


def _is_non_runtime_scan_path(path: Path) -> bool:
    return any(
        path == root or root in path.parents
        for root in VENDOR_LIKE_RUNTIME_ROOTS + NON_RUNTIME_DATA_ROOTS
    )


def _python_runtime_dependency_fragments(text: str, forbidden: list[str]) -> list[str]:
    """Find executable path literals without flagging deny-list definitions."""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [fragment for fragment in forbidden if fragment in text]

    found: set[str] = set()

    def is_static_denylist(name: str) -> bool:
        return (
            name.startswith("FORBIDDEN_")
            or name.endswith("_DENYLIST")
        )

    class RuntimeLiteralVisitor(ast.NodeVisitor):
        def visit_Assign(self, node: ast.Assign) -> None:
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if any(is_static_denylist(name) for name in names):
                return
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if isinstance(node.target, ast.Name) and is_static_denylist(node.target.id):
                return
            self.generic_visit(node)

        def visit_For(self, node: ast.For) -> None:
            literals = [
                item.value
                for item in getattr(node.iter, "elts", [])
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            if literals and any(fragment in value for value in literals for fragment in forbidden):
                for statement in [*node.body, *node.orelse]:
                    self.visit(statement)
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            # Audit and metric code commonly counts forbidden literals in
            # inspected source text.  A comparison needle is evidence of a
            # deny check, not an executable path dependency.
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"count", "endswith", "find", "startswith"}
            ):
                self.visit(node.func.value)
                for keyword in node.keywords:
                    self.visit(keyword.value)
                return
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if not isinstance(node.value, str):
                return
            for fragment in forbidden:
                if fragment in node.value:
                    found.add(fragment)

    RuntimeLiteralVisitor().visit(tree)
    return sorted(found)
