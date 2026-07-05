from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .ledger_models import (
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LineCountPolicy,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    to_jsonable,
)


class LedgerPolicySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class LedgerPolicyCode(StrEnum):
    ALLOWED = "ALLOWED"
    INVALID_SOURCE_REPO = "INVALID_SOURCE_REPO"
    INVALID_TARGET_PREFIX = "INVALID_TARGET_PREFIX"
    TARGET_IS_DATA_ONLY = "TARGET_IS_DATA_ONLY"
    TARGET_IS_DOCUMENTATION = "TARGET_IS_DOCUMENTATION"
    TARGET_IS_CACHE = "TARGET_IS_CACHE"
    TARGET_IS_VENDOR_POOL = "TARGET_IS_VENDOR_POOL"
    TARGET_HAS_PARENT_REFERENCE = "TARGET_HAS_PARENT_REFERENCE"
    TARGET_IS_ABSOLUTE = "TARGET_IS_ABSOLUTE"
    MISSING_RUNTIME_FOR_STATUS = "MISSING_RUNTIME_FOR_STATUS"
    MISSING_TEST_FOR_STATUS = "MISSING_TEST_FOR_STATUS"
    MISSING_MAIN_PATH_FOR_STATUS = "MISSING_MAIN_PATH_FOR_STATUS"
    MISSING_NOTICE_FOR_STATUS = "MISSING_NOTICE_FOR_STATUS"
    INVALID_LINE_COUNT_POLICY = "INVALID_LINE_COUNT_POLICY"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    DOWNGRADE_REQUIRES_REASON = "DOWNGRADE_REQUIRES_REASON"
    MATERIALIZATION_REQUIRES_TARGET = "MATERIALIZATION_REQUIRES_TARGET"
    MATERIALIZATION_REQUIRES_RUNTIME = "MATERIALIZATION_REQUIRES_RUNTIME"
    MATERIALIZATION_REQUIRES_TEST = "MATERIALIZATION_REQUIRES_TEST"
    PRODUCTIZATION_REQUIRES_NOTICE = "PRODUCTIZATION_REQUIRES_NOTICE"
    PRODUCTIZATION_REQUIRES_MAIN_PATH = "PRODUCTIZATION_REQUIRES_MAIN_PATH"
    PRODUCTIZATION_REQUIRES_EFFECTIVE_CODE = "PRODUCTIZATION_REQUIRES_EFFECTIVE_CODE"


class LedgerSurface(StrEnum):
    APP = "app"
    PACKAGE = "package"
    SCRIPT = "script"
    TEST = "test"
    VENDOR_RUNTIME = "vendor_runtime"
    SKILL = "skill"
    THIRD_PARTY_NOTICE = "third_party_notice"
    TEMPORARY = "temporary"
    DOCUMENTATION = "documentation"
    DATA = "data"
    CACHE = "cache"
    UNKNOWN = "unknown"


class CountVerdict(StrEnum):
    EFFECTIVE = "effective"
    EXCLUDED = "excluded"
    REVIEW = "review"


REQUIRED_SOURCE_REPOS = {
    "claude-code-best",
    "browser-use",
    "OpenHands",
    "openclaw",
    "agentscope",
    "agent-framework",
    "hermes-agent",
    "langgraph",
}

ALLOWED_TARGET_ROOTS = {
    "apps",
    "packages",
    "tests",
    "scripts",
    "vendor-runtimes",
    "skills",
    "third_party",
    "tmp",
}

COUNTED_ROOTS = {
    "apps",
    "packages",
    "tests",
    "scripts",
    "vendor-runtimes",
    "skills",
}

DATA_FILENAMES = {
    "internalization_ledger_seed.json",
    "source_inventory.json",
    "source_inventory.yaml",
    "source_inventory.yml",
    "source_map.json",
    "source_map.yaml",
    "source_map.yml",
    "inventory.json",
    "inventory.yaml",
    "inventory.yml",
}

DATA_SUFFIXES = {
    ".csv",
    ".json",
    ".jsonl",
    ".ndjson",
    ".parquet",
    ".sqlite",
    ".sqlite3",
    ".tsv",
    ".yaml",
    ".yml",
}

SOURCE_SUFFIXES = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".css",
    ".html",
    ".toml",
    ".ps1",
    ".sh",
    ".bat",
    ".cmd",
}

DOCUMENTATION_SUFFIXES = {
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".pdf",
    ".docx",
}

CACHE_PARTS = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    "node_modules",
    "dist",
    "build",
}

MATERIALIZED_LIFECYCLES = {
    LedgerLifecycle.ACTIVE,
    LedgerLifecycle.INTERNALIZED,
    LedgerLifecycle.PRODUCTIZED,
}

CONNECTED_STATUSES = {
    MainPathStatus.API_CONNECTED,
    MainPathStatus.EVENT_LOG_CONNECTED,
    MainPathStatus.CONTROL_COMMAND_CONNECTED,
    MainPathStatus.WORKER_RUNTIME_CONNECTED,
    MainPathStatus.UI_CONNECTED,
    MainPathStatus.TESTED_MAIN_PATH,
}

STATUS_RANK = {
    MainPathStatus.PLANNED: 0,
    MainPathStatus.INVENTORIED: 1,
    MainPathStatus.VENDORED: 2,
    MainPathStatus.ADAPTER_READY: 3,
    MainPathStatus.API_CONNECTED: 4,
    MainPathStatus.EVENT_LOG_CONNECTED: 5,
    MainPathStatus.CONTROL_COMMAND_CONNECTED: 6,
    MainPathStatus.WORKER_RUNTIME_CONNECTED: 7,
    MainPathStatus.UI_CONNECTED: 8,
    MainPathStatus.TESTED_MAIN_PATH: 9,
    MainPathStatus.BLOCKED: -1,
    MainPathStatus.REJECTED: -2,
}

LIFECYCLE_RANK = {
    LedgerLifecycle.CANDIDATE: 0,
    LedgerLifecycle.PLANNED: 1,
    LedgerLifecycle.IN_PROGRESS: 2,
    LedgerLifecycle.ACTIVE: 3,
    LedgerLifecycle.INTERNALIZED: 4,
    LedgerLifecycle.PRODUCTIZED: 5,
    LedgerLifecycle.DEFERRED: -1,
    LedgerLifecycle.REJECTED: -2,
}

RUNTIME_STRATEGIES = {
    MigrationStrategy.DIRECT_PORT,
    MigrationStrategy.ADAPTER,
    MigrationStrategy.SIDECAR_RUNTIME,
    MigrationStrategy.VENDORED_RUNTIME,
}


@dataclass(slots=True)
class LedgerPolicyFinding:
    code: LedgerPolicyCode
    severity: LedgerPolicySeverity
    message: str
    ledger_id: str = ""
    path: str = ""
    field: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class PathClassification:
    path: str
    normalized_path: str
    surface: LedgerSurface
    verdict: CountVerdict
    reason: str
    root: str = ""
    suffix: str = ""
    is_project_relative: bool = True
    is_generated_data: bool = False
    is_source_like: bool = False
    is_test_like: bool = False
    is_runtime_like: bool = False
    is_vendor_runtime: bool = False
    is_documentation: bool = False
    is_cache: bool = False
    is_notice: bool = False
    flags: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class TransitionPolicyResult:
    allowed: bool
    from_lifecycle: LedgerLifecycle
    to_lifecycle: LedgerLifecycle
    from_status: MainPathStatus
    to_status: MainPathStatus
    findings: list[LedgerPolicyFinding] = dc_field(default_factory=list)
    required_actions: list[str] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class UnitBudget:
    unit: str
    minimum_effective_lines: int
    milestone: str = "M1"
    description: str = ""
    excludes_data_lines: bool = True
    requires_source_migration: bool = False

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


UNIT_BUDGETS: dict[str, UnitBudget] = {
    "M1-01A": UnitBudget(
        unit="M1-01A",
        minimum_effective_lines=10_000,
        description="ledger schema, audit, API/CLI, snapshots, strict line-count gate",
        requires_source_migration=False,
    ),
    "M1-01B": UnitBudget(
        unit="M1-01B",
        minimum_effective_lines=10_000,
        description="source extraction and runtime scaffold",
        requires_source_migration=True,
    ),
    "M1-02A": UnitBudget(unit="M1-02A", minimum_effective_lines=18_000, description="Claude source productization", requires_source_migration=True),
    "M1-02B": UnitBudget(unit="M1-02B", minimum_effective_lines=18_000, description="query and session lifecycle", requires_source_migration=True),
    "M1-02C": UnitBudget(unit="M1-02C", minimum_effective_lines=17_000, description="tool loop and result budget", requires_source_migration=True),
    "M1-02D": UnitBudget(unit="M1-02D", minimum_effective_lines=17_000, description="context compact and CodeWorker API", requires_source_migration=True),
    "M1-03A": UnitBudget(unit="M1-03A", minimum_effective_lines=16_000, description="permission runtime", requires_source_migration=True),
    "M1-03B": UnitBudget(unit="M1-03B", minimum_effective_lines=16_000, description="MCP client runtime", requires_source_migration=True),
    "M1-03C": UnitBudget(unit="M1-03C", minimum_effective_lines=16_000, description="skill runtime loader", requires_source_migration=True),
    "M1-03D": UnitBudget(unit="M1-03D", minimum_effective_lines=17_000, description="subagent and commands integration", requires_source_migration=True),
    "M1-04A": UnitBudget(unit="M1-04A", minimum_effective_lines=15_000, description="browser session productization", requires_source_migration=True),
    "M1-04B": UnitBudget(unit="M1-04B", minimum_effective_lines=15_000, description="message manager and state compression", requires_source_migration=True),
    "M1-04C": UnitBudget(unit="M1-04C", minimum_effective_lines=15_000, description="action registry and permission", requires_source_migration=True),
    "M1-04D": UnitBudget(unit="M1-04D", minimum_effective_lines=15_000, description="watchdogs history and artifacts", requires_source_migration=True),
    "M1-05A": UnitBudget(unit="M1-05A", minimum_effective_lines=14_000, description="workspace manager", requires_source_migration=True),
    "M1-05B": UnitBudget(unit="M1-05B", minimum_effective_lines=14_000, description="sandbox gateway", requires_source_migration=True),
    "M1-05C": UnitBudget(unit="M1-05C", minimum_effective_lines=14_000, description="runtime event and message bus", requires_source_migration=True),
    "M1-05D": UnitBudget(unit="M1-05D", minimum_effective_lines=13_000, description="backend failover and dispatch", requires_source_migration=True),
    "M1-06A": UnitBudget(unit="M1-06A", minimum_effective_lines=15_000, description="retrieval and index adapters", requires_source_migration=True),
    "M1-06B": UnitBudget(unit="M1-06B", minimum_effective_lines=15_000, description="memory curator worker", requires_source_migration=True),
    "M1-06C": UnitBudget(unit="M1-06C", minimum_effective_lines=15_000, description="skill memory and compact restore", requires_source_migration=True),
    "M1-07A": UnitBudget(unit="M1-07A", minimum_effective_lines=15_000, description="worker lifecycle and resource pool", requires_source_migration=True),
    "M1-07B": UnitBudget(unit="M1-07B", minimum_effective_lines=15_000, description="watchdog and fault injection", requires_source_migration=True),
    "M1-07C": UnitBudget(unit="M1-07C", minimum_effective_lines=15_000, description="recovery planner routing memory", requires_source_migration=True),
    "M1-08": UnitBudget(unit="M1-08", minimum_effective_lines=20_000, description="main path hardening", requires_source_migration=True),
    "M2-01A": UnitBudget(unit="M2-01A", milestone="M2", minimum_effective_lines=12_000, description="web app shell and API client", requires_source_migration=True),
    "M2-01B": UnitBudget(unit="M2-01B", milestone="M2", minimum_effective_lines=13_000, description="event stream and state store", requires_source_migration=True),
    "M2-02A": UnitBudget(unit="M2-02A", milestone="M2", minimum_effective_lines=13_000, description="topology graph view", requires_source_migration=True),
    "M2-02B": UnitBudget(unit="M2-02B", milestone="M2", minimum_effective_lines=12_000, description="timeline and worker state", requires_source_migration=True),
    "M2-03A": UnitBudget(unit="M2-03A", milestone="M2", minimum_effective_lines=15_000, description="artifact and diff viewers", requires_source_migration=True),
    "M2-03B": UnitBudget(unit="M2-03B", milestone="M2", minimum_effective_lines=15_000, description="terminal browser trace viewers", requires_source_migration=True),
    "M2-04A": UnitBudget(unit="M2-04A", milestone="M2", minimum_effective_lines=13_000, description="command and permission panels", requires_source_migration=True),
    "M2-04B": UnitBudget(unit="M2-04B", milestone="M2", minimum_effective_lines=12_000, description="session memory MCP skill panels", requires_source_migration=True),
    "M2-05": UnitBudget(unit="M2-05", milestone="M2", minimum_effective_lines=20_000, description="scenario runner and demo loop", requires_source_migration=True),
    "M3-01A": UnitBudget(unit="M3-01A", milestone="M3", minimum_effective_lines=9_000, description="vendor source map", requires_source_migration=False),
    "M3-01B": UnitBudget(unit="M3-01B", milestone="M3", minimum_effective_lines=9_000, description="productized runtime cleanup", requires_source_migration=False),
    "M3-02A": UnitBudget(unit="M3-02A", milestone="M3", minimum_effective_lines=9_000, description="test eval hardening", requires_source_migration=False),
    "M3-02B": UnitBudget(unit="M3-02B", milestone="M3", minimum_effective_lines=9_000, description="packaging healthcheck release", requires_source_migration=False),
    "M3-03": UnitBudget(unit="M3-03", milestone="M3", minimum_effective_lines=9_000, description="freeze report and archive", requires_source_migration=False),
}


def normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def path_parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(normalize_repo_path(path)).parts


def has_parent_reference(path: str) -> bool:
    normalized = normalize_repo_path(path).lower()
    return "../" in normalized or normalized.startswith("g:/agent-zoo/") or normalized.startswith("g://agent-zoo/")


def is_absolute_project_path(path: str) -> bool:
    normalized = normalize_repo_path(path)
    parts = path_parts(normalized)
    return normalized.startswith("/") or bool(parts and ":" in parts[0])


def classify_path(path: str) -> PathClassification:
    normalized = normalize_repo_path(path)
    parts = path_parts(normalized)
    root = parts[0] if parts else ""
    suffix = PurePosixPath(normalized).suffix.lower()
    name = PurePosixPath(normalized).name
    flags: list[str] = []
    if has_parent_reference(normalized):
        flags.append("parent-reference")
    if is_absolute_project_path(normalized):
        flags.append("absolute")
    if root not in ALLOWED_TARGET_ROOTS:
        flags.append("outside-target-roots")
    is_cache = bool(set(parts) & CACHE_PARTS)
    is_documentation = suffix in DOCUMENTATION_SUFFIXES or root == "docs"
    structured_data_file = suffix in DATA_SUFFIXES or name in DATA_FILENAMES
    is_generated_data = structured_data_file and (
        "data" in parts
        or "inventory" in name.lower()
        or "seed" in name.lower()
        or "index" in name.lower()
        or "source-map" in name.lower()
        or "source_map" in name.lower()
        or "ledger" in name.lower()
    )
    is_notice = root == "third_party" or "NOTICE" in name.upper()
    is_test_like = root == "tests" or name.startswith("test_") or name.endswith(".test.ts") or name.endswith(".spec.ts")
    is_vendor_runtime = root == "vendor-runtimes"
    is_source_like = suffix in SOURCE_SUFFIXES and not is_generated_data and not is_documentation
    is_runtime_like = root in {"apps", "packages", "scripts", "vendor-runtimes", "skills"} and is_source_like
    surface = LedgerSurface.UNKNOWN
    if root == "apps":
        surface = LedgerSurface.APP
    elif root == "packages":
        surface = LedgerSurface.PACKAGE
    elif root == "scripts":
        surface = LedgerSurface.SCRIPT
    elif root == "tests":
        surface = LedgerSurface.TEST
    elif root == "vendor-runtimes":
        surface = LedgerSurface.VENDOR_RUNTIME
    elif root == "skills":
        surface = LedgerSurface.SKILL
    elif root == "third_party":
        surface = LedgerSurface.THIRD_PARTY_NOTICE
    elif root == "tmp":
        surface = LedgerSurface.TEMPORARY
    if is_cache:
        surface = LedgerSurface.CACHE
    elif is_documentation:
        surface = LedgerSurface.DOCUMENTATION
    elif is_generated_data and not is_notice:
        surface = LedgerSurface.DATA
    if has_parent_reference(normalized) or is_absolute_project_path(normalized) or root not in ALLOWED_TARGET_ROOTS:
        verdict = CountVerdict.EXCLUDED
        reason = "path is outside the Zyra submission boundary"
    elif is_cache:
        verdict = CountVerdict.EXCLUDED
        reason = "cache/build output is excluded"
    elif is_documentation:
        verdict = CountVerdict.EXCLUDED
        reason = "documentation is excluded from effective code"
    elif is_generated_data and not is_notice:
        verdict = CountVerdict.EXCLUDED
        reason = "seed/inventory/data files are excluded from effective code"
    elif root not in COUNTED_ROOTS:
        verdict = CountVerdict.EXCLUDED
        reason = "target root is not part of counted source surfaces"
    elif is_source_like or is_test_like or is_vendor_runtime:
        verdict = CountVerdict.EFFECTIVE
        reason = "source/test/runtime file may count when connected and verified"
    else:
        verdict = CountVerdict.REVIEW
        reason = "file requires manual review before counting"
    return PathClassification(
        path=path,
        normalized_path=normalized,
        surface=surface,
        verdict=verdict,
        reason=reason,
        root=root,
        suffix=suffix,
        is_project_relative=not is_absolute_project_path(normalized) and not has_parent_reference(normalized),
        is_generated_data=is_generated_data,
        is_source_like=is_source_like,
        is_test_like=is_test_like,
        is_runtime_like=is_runtime_like,
        is_vendor_runtime=is_vendor_runtime,
        is_documentation=is_documentation,
        is_cache=is_cache,
        is_notice=is_notice,
        flags=flags,
    )


def classify_paths(paths: Iterable[str]) -> list[PathClassification]:
    return [classify_path(path) for path in paths]


def countable_path(path: str) -> bool:
    return classify_path(path).verdict == CountVerdict.EFFECTIVE


def source_repo_allowed(source_repo: str) -> bool:
    return source_repo in REQUIRED_SOURCE_REPOS


def entry_requires_runtime(entry: InternalizationLedgerEntry) -> bool:
    return entry.lifecycle in MATERIALIZED_LIFECYCLES or entry.main_path_status in CONNECTED_STATUSES or entry.migration_strategy in RUNTIME_STRATEGIES


def entry_requires_tests(entry: InternalizationLedgerEntry) -> bool:
    return entry.lifecycle in MATERIALIZED_LIFECYCLES or entry.main_path_status in CONNECTED_STATUSES


def entry_requires_notice(entry: InternalizationLedgerEntry) -> bool:
    return entry.lifecycle in {LedgerLifecycle.INTERNALIZED, LedgerLifecycle.PRODUCTIZED} and entry.migration_strategy in RUNTIME_STRATEGIES


def entry_has_effective_target(entry: InternalizationLedgerEntry) -> bool:
    return any(countable_path(path) for path in entry.target_paths)


def effective_target_paths(entry: InternalizationLedgerEntry) -> list[str]:
    return [path for path in entry.target_paths if countable_path(path)]


def excluded_target_paths(entry: InternalizationLedgerEntry) -> list[PathClassification]:
    return [classification for classification in classify_paths(entry.target_paths) if classification.verdict != CountVerdict.EFFECTIVE]


def validate_entry_policy(entry: InternalizationLedgerEntry) -> list[LedgerPolicyFinding]:
    findings: list[LedgerPolicyFinding] = []
    if not source_repo_allowed(entry.source_repo):
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.INVALID_SOURCE_REPO,
                severity=LedgerPolicySeverity.ERROR,
                message=f"{entry.source_repo!r} is not one of the required source repositories.",
                ledger_id=entry.ledger_id,
                field="source_repo",
                remediation="Use the exact source repo names from the M1-01A plan.",
            )
        )
    for target in entry.target_paths:
        classification = classify_path(target)
        findings.extend(_findings_for_target(entry, classification))
    if entry_requires_runtime(entry) and entry.runtime_entry.is_empty():
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.MISSING_RUNTIME_FOR_STATUS,
                severity=LedgerPolicySeverity.ERROR,
                message="Entry status or migration strategy requires a runtime entry.",
                ledger_id=entry.ledger_id,
                field="runtime_entry",
                remediation="Record command/module/function/protocol/health_check before marking it active or connected.",
            )
        )
    if entry_requires_tests(entry) and not entry.test_entries:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.MISSING_TEST_FOR_STATUS,
                severity=LedgerPolicySeverity.ERROR,
                message="Entry status requires at least one test entry.",
                ledger_id=entry.ledger_id,
                field="test_entries",
                remediation="Add unit, integration, smoke, or verification command coverage.",
            )
        )
    if entry.main_path_status in CONNECTED_STATUSES and entry.main_path.is_empty():
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.MISSING_MAIN_PATH_FOR_STATUS,
                severity=LedgerPolicySeverity.ERROR,
                message="Connected status requires API/event/control/worker/UI binding.",
                ledger_id=entry.ledger_id,
                field="main_path",
                remediation="Record the exact main-path surface before using a connected status.",
            )
        )
    if entry_requires_notice(entry):
        status = entry.license_notice.status if entry.license_notice else NoticeStatus.PENDING
        if status in {NoticeStatus.PENDING, NoticeStatus.NEEDS_REVIEW}:
            findings.append(
                LedgerPolicyFinding(
                    code=LedgerPolicyCode.MISSING_NOTICE_FOR_STATUS,
                    severity=LedgerPolicySeverity.ERROR,
                    message="Internalized/productized runtime entry must have NOTICE handling resolved.",
                    ledger_id=entry.ledger_id,
                    field="license_notice",
                    remediation="Update license_notice.status to recorded or not_required with notes.",
                )
            )
    if entry.lifecycle in MATERIALIZED_LIFECYCLES and entry.line_count_policy in {
        LineCountPolicy.EXCLUDED_DOCUMENTATION,
        LineCountPolicy.EXCLUDED_INVENTORY_ONLY,
    }:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.INVALID_LINE_COUNT_POLICY,
                severity=LedgerPolicySeverity.ERROR,
                message=f"Materialized entry cannot use line_count_policy={entry.line_count_policy}.",
                ledger_id=entry.ledger_id,
                field="line_count_policy",
                remediation="Use a runtime/test/script counting policy or downgrade lifecycle.",
            )
        )
    return findings


def evaluate_transition(
    entry: InternalizationLedgerEntry,
    *,
    to_lifecycle: LedgerLifecycle | None = None,
    to_status: MainPathStatus | None = None,
    reason: str = "",
    effective_lines: int = 0,
) -> TransitionPolicyResult:
    target_lifecycle = to_lifecycle or entry.lifecycle
    target_status = to_status or entry.main_path_status
    findings: list[LedgerPolicyFinding] = []
    required_actions: list[str] = []
    warnings: list[str] = []
    lifecycle_progress = LIFECYCLE_RANK[target_lifecycle] - LIFECYCLE_RANK[entry.lifecycle]
    status_progress = STATUS_RANK[target_status] - STATUS_RANK[entry.main_path_status]
    if lifecycle_progress < 0 and not reason:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.DOWNGRADE_REQUIRES_REASON,
                severity=LedgerPolicySeverity.ERROR,
                message="Lifecycle downgrade requires an explicit reason.",
                ledger_id=entry.ledger_id,
                field="lifecycle",
                remediation="Provide a reason explaining blockage, rejection, or rollback.",
            )
        )
    if status_progress < 0 and not reason:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.DOWNGRADE_REQUIRES_REASON,
                severity=LedgerPolicySeverity.ERROR,
                message="Main-path status downgrade requires an explicit reason.",
                ledger_id=entry.ledger_id,
                field="main_path_status",
                remediation="Provide a reason explaining why the previous status was too strong.",
            )
        )
    if target_lifecycle in MATERIALIZED_LIFECYCLES and not entry.target_bindings:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.MATERIALIZATION_REQUIRES_TARGET,
                severity=LedgerPolicySeverity.BLOCKER,
                message="Materialized lifecycle requires at least one target binding.",
                ledger_id=entry.ledger_id,
                field="target_bindings",
                remediation="Add a project-relative target path before materialization.",
            )
        )
    if target_lifecycle in MATERIALIZED_LIFECYCLES and entry.runtime_entry.is_empty():
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.MATERIALIZATION_REQUIRES_RUNTIME,
                severity=LedgerPolicySeverity.ERROR,
                message="Materialized lifecycle requires runtime metadata.",
                ledger_id=entry.ledger_id,
                field="runtime_entry",
                remediation="Add command/module/function/protocol/health check metadata.",
            )
        )
    if target_lifecycle in MATERIALIZED_LIFECYCLES and not entry.test_entries:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.MATERIALIZATION_REQUIRES_TEST,
                severity=LedgerPolicySeverity.ERROR,
                message="Materialized lifecycle requires test metadata.",
                ledger_id=entry.ledger_id,
                field="test_entries",
                remediation="Add a test path and command before materialization.",
            )
        )
    if target_lifecycle == LedgerLifecycle.PRODUCTIZED:
        if entry.main_path.is_empty():
            findings.append(
                LedgerPolicyFinding(
                    code=LedgerPolicyCode.PRODUCTIZATION_REQUIRES_MAIN_PATH,
                    severity=LedgerPolicySeverity.ERROR,
                    message="Productized lifecycle requires main-path binding.",
                    ledger_id=entry.ledger_id,
                    field="main_path",
                    remediation="Bind the module to API/event/control/worker/UI surfaces.",
                )
            )
        if entry.license_notice and entry.license_notice.status in {NoticeStatus.PENDING, NoticeStatus.NEEDS_REVIEW}:
            findings.append(
                LedgerPolicyFinding(
                    code=LedgerPolicyCode.PRODUCTIZATION_REQUIRES_NOTICE,
                    severity=LedgerPolicySeverity.ERROR,
                    message="Productized lifecycle requires resolved NOTICE status.",
                    ledger_id=entry.ledger_id,
                    field="license_notice",
                    remediation="Resolve license_notice.status before productization.",
                )
            )
        if effective_lines <= 0 and not entry_has_effective_target(entry):
            findings.append(
                LedgerPolicyFinding(
                    code=LedgerPolicyCode.PRODUCTIZATION_REQUIRES_EFFECTIVE_CODE,
                    severity=LedgerPolicySeverity.ERROR,
                    message="Productized lifecycle requires effective source/runtime/test code evidence.",
                    ledger_id=entry.ledger_id,
                    field="target_bindings",
                    remediation="Do not productize records backed only by seed/inventory/data files.",
                )
            )
    if target_status in CONNECTED_STATUSES and entry.main_path.is_empty():
        required_actions.append("record main_path surfaces before using connected status")
    if target_status == MainPathStatus.TESTED_MAIN_PATH and not entry.test_entries:
        required_actions.append("add test_entries before marking tested_main_path")
    if lifecycle_progress == 0 and status_progress == 0:
        warnings.append("transition does not change lifecycle or main-path status")
    allowed = not any(finding.severity in {LedgerPolicySeverity.ERROR, LedgerPolicySeverity.BLOCKER} for finding in findings)
    return TransitionPolicyResult(
        allowed=allowed,
        from_lifecycle=entry.lifecycle,
        to_lifecycle=target_lifecycle,
        from_status=entry.main_path_status,
        to_status=target_status,
        findings=findings,
        required_actions=required_actions,
        warnings=warnings,
    )


def minimum_effective_lines_for_unit(unit: str) -> int:
    budget = UNIT_BUDGETS.get(unit.upper())
    return budget.minimum_effective_lines if budget else 0


def unit_budget(unit: str) -> UnitBudget | None:
    return UNIT_BUDGETS.get(unit.upper())


def all_unit_budgets() -> list[UnitBudget]:
    return [UNIT_BUDGETS[key] for key in sorted(UNIT_BUDGETS)]


def policy_findings_to_errors(findings: Iterable[LedgerPolicyFinding]) -> list[str]:
    return [
        f"{finding.severity} {finding.code}: {finding.message}"
        for finding in findings
        if finding.severity in {LedgerPolicySeverity.ERROR, LedgerPolicySeverity.BLOCKER}
    ]


def _findings_for_target(entry: InternalizationLedgerEntry, classification: PathClassification) -> list[LedgerPolicyFinding]:
    findings: list[LedgerPolicyFinding] = []
    if "parent-reference" in classification.flags:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.TARGET_HAS_PARENT_REFERENCE,
                severity=LedgerPolicySeverity.BLOCKER,
                message=f"Target path references a parent/source repository: {classification.path}",
                ledger_id=entry.ledger_id,
                path=classification.path,
                field="target_bindings",
                remediation="Move the target into zyra and record a project-relative path.",
            )
        )
    if "absolute" in classification.flags:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.TARGET_IS_ABSOLUTE,
                severity=LedgerPolicySeverity.ERROR,
                message=f"Target path must be project-relative: {classification.path}",
                ledger_id=entry.ledger_id,
                path=classification.path,
                field="target_bindings",
                remediation="Replace absolute paths with repository-relative paths.",
            )
        )
    if "outside-target-roots" in classification.flags:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.INVALID_TARGET_PREFIX,
                severity=LedgerPolicySeverity.ERROR,
                message=f"Target path is outside allowed roots: {classification.path}",
                ledger_id=entry.ledger_id,
                path=classification.path,
                field="target_bindings",
                remediation="Use apps/, packages/, tests/, scripts/, vendor-runtimes/, skills/, third_party/, or tmp/.",
            )
        )
    if classification.is_generated_data:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.TARGET_IS_DATA_ONLY,
                severity=LedgerPolicySeverity.WARNING,
                message=f"Target is seed/inventory/data and cannot count as effective code: {classification.path}",
                ledger_id=entry.ledger_id,
                path=classification.path,
                field="target_bindings",
                remediation="Report this as data scale only; pair it with real loader/audit/API/test code.",
            )
        )
    if classification.is_documentation:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.TARGET_IS_DOCUMENTATION,
                severity=LedgerPolicySeverity.WARNING,
                message=f"Target is documentation and cannot count as effective code: {classification.path}",
                ledger_id=entry.ledger_id,
                path=classification.path,
                field="target_bindings",
                remediation="Keep documentation separate from effective code accounting.",
            )
        )
    if classification.is_cache:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.TARGET_IS_CACHE,
                severity=LedgerPolicySeverity.BLOCKER,
                message=f"Target is cache/build output: {classification.path}",
                ledger_id=entry.ledger_id,
                path=classification.path,
                field="target_bindings",
                remediation="Remove cache/build output from ledger records.",
            )
        )
    if classification.is_vendor_runtime and entry.main_path.is_empty() and entry.lifecycle in MATERIALIZED_LIFECYCLES:
        findings.append(
            LedgerPolicyFinding(
                code=LedgerPolicyCode.TARGET_IS_VENDOR_POOL,
                severity=LedgerPolicySeverity.ERROR,
                message=f"Vendor runtime target has no productized boundary: {classification.path}",
                ledger_id=entry.ledger_id,
                path=classification.path,
                field="main_path",
                remediation="Record sidecar/API/worker/event boundary before counting vendored runtime as internalized.",
            )
        )
    return findings


def existing_project_targets(project_root: Path, entry: InternalizationLedgerEntry) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for target in entry.target_paths:
        classification = classify_path(target)
        result[target] = classification.is_project_relative and (project_root / classification.normalized_path).exists()
    return result


def target_materialization_ratio(project_root: Path, entries: Iterable[InternalizationLedgerEntry]) -> float:
    total = 0
    materialized = 0
    for entry in entries:
        for exists in existing_project_targets(project_root, entry).values():
            total += 1
            if exists:
                materialized += 1
    return materialized / total if total else 0.0
