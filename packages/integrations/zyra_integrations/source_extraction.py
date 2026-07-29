from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from zyra_core import EventRecord, EventType

from .ledger_models import LedgerLifecycle, MainPathStatus, MigrationStrategy


SOURCE_SUFFIXES = frozenset(
    {".cjs", ".css", ".html", ".js", ".json", ".jsx", ".mjs", ".py", ".ts", ".tsx"}
)
DEFAULT_EXCLUDE_PATTERNS = (
    "**/.git/**",
    "**/__pycache__/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
)


class OverwritePolicy(StrEnum):
    NEVER = "never"
    IF_CHANGED = "if_changed"
    ALWAYS = "always"


class ExtractionDisposition(StrEnum):
    RETIRED = "retired"
    COPIED = "copied"
    SKIPPED_IDENTICAL = "skipped_identical"
    SKIPPED_EXISTING = "skipped_existing"
    EXCLUDED = "excluded"
    MISSING_SOURCE = "missing_source"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class ExtractionSource:
    source_repo: str
    repo_root: Path
    source_path: str

    @property
    def absolute_path(self) -> Path:
        return self.repo_root / normalize_repo_path(self.source_path)


@dataclass(frozen=True, slots=True)
class ExtractionTarget:
    project_root: Path
    target_root: Path
    relative_path: str

    @property
    def project_relative_path(self) -> str:
        return (
            self.target_root / normalize_repo_path(self.relative_path)
        ).relative_to(self.project_root).as_posix()

    @property
    def absolute_path(self) -> Path:
        return self.project_root / self.project_relative_path


@dataclass(frozen=True, slots=True)
class ExtractionItem:
    source: ExtractionSource
    target: ExtractionTarget
    line_count: int
    byte_count: int
    sha256: str
    source_like: bool
    disposition: ExtractionDisposition = ExtractionDisposition.RETIRED
    reason: str = "legacy_source_pool_retired"
    previous_sha256: str = ""
    is_upstream_type_stub: bool = False

    @property
    def effective_line_count(self) -> int:
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repo": self.source.source_repo,
            "source_path": normalize_repo_path(self.source.source_path),
            "target_path": self.target.project_relative_path,
            "line_count": self.line_count,
            "effective_line_count": 0,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "previous_sha256": self.previous_sha256,
            "source_like": self.source_like,
            "is_upstream_type_stub": self.is_upstream_type_stub,
            "disposition": str(self.disposition),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class LedgerUpsertPlan:
    ledger_id: str
    source_repo: str
    source_path: str
    target_path: str
    owner_unit: str
    capability_name: str
    action: str
    lifecycle: str
    main_path_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in self.__dataclass_fields__
        }


@dataclass(slots=True)
class ExtractionPlan:
    source_repo: str
    source_root: Path
    project_root: Path
    target_root: Path
    include_paths: list[str]
    exclude_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS)
    )
    owner_unit: str = "retired"
    milestone: str = "M1"
    downstream_units: list[str] = field(default_factory=list)
    overwrite_policy: OverwritePolicy = OverwritePolicy.NEVER
    dry_run: bool = True
    report_path: Path | None = None
    manifest_module_path: Path | None = None
    runtime_command: str = ""
    runtime_module: str = ""
    runtime_health_check: str = ""
    test_path: str = ""
    test_command: str = ""
    capability_prefix: str = "legacy_source_pool_retired"
    capability_summary: str = "Historical extraction is retired."
    target_mount: str = ""
    manifest_export_name: str = ""
    manifest_health_export_name: str = ""
    manifest_kind: str = "retired"
    runtime_function: str = ""
    lifecycle: LedgerLifecycle = LedgerLifecycle.REJECTED
    main_path_status: MainPathStatus = MainPathStatus.REJECTED
    migration_strategy: MigrationStrategy = MigrationStrategy.REIMPLEMENTED_PATTERN
    main_path_worker_runtime: str = ""
    main_path_surfaces: list[str] = field(default_factory=list)
    main_path_event_types: list[str] = field(default_factory=list)
    main_path_control_commands: list[str] = field(default_factory=list)
    main_path_artifact_kinds: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=lambda: ["historical", "retired"])
    source_evidence_tags: list[str] = field(
        default_factory=lambda: ["frozen-git-object-provenance"]
    )
    replacement_plan: str = (
        "Use the Zyra-owned formal runtime boundaries already present in packages/runtime."
    )

    def normalized_target_root(self) -> Path:
        if self.target_root.is_absolute():
            return self.target_root.resolve()
        return (self.project_root / self.target_root).resolve()


@dataclass(slots=True)
class ExtractionReport:
    plan: ExtractionPlan
    copied: list[ExtractionItem] = field(default_factory=list)
    skipped: list[ExtractionItem] = field(default_factory=list)
    excluded: list[ExtractionItem] = field(default_factory=list)
    missing: list[ExtractionItem] = field(default_factory=list)
    ledger_upserts: list[LedgerUpsertPlan] = field(default_factory=list)
    errors: list[str] = field(
        default_factory=lambda: ["legacy_source_pool_retired"]
    )

    @property
    def ok(self) -> bool:
        return False

    @property
    def target_paths(self) -> list[str]:
        return []

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "retired",
            "reason": "legacy_source_pool_retired",
            "source_repo": self.plan.source_repo,
            "target_paths": [],
            "errors": list(self.errors),
        }

    def completion_event(
        self,
        *,
        run_id: str = "legacy-retired",
        task_id: str = "source-extraction",
    ) -> EventRecord:
        return source_extraction_completed_event(
            self,
            run_id=run_id,
            task_id=task_id,
        )


class SourceExtractionError(RuntimeError):
    pass


class LegacySourcePoolRetiredError(SourceExtractionError):
    pass


class SourceExtractor:
    def __init__(self, plan: ExtractionPlan) -> None:
        del plan
        raise LegacySourcePoolRetiredError(
            "Legacy source extraction is retired. Use packages/runtime formal owners; "
            "no source-pool writer or fallback is available."
        )


def _retired(*_: Any, **__: Any) -> ExtractionPlan:
    raise LegacySourcePoolRetiredError(
        "The first-stage Claude source extraction plans are immutable historical "
        "evidence and cannot be executed or recreated."
    )


claude_code_m1_01b_plan = _retired
claude_code_m1_02a_plan = _retired
claude_code_m1_02b_plan = _retired
claude_code_m1_02c_plan = _retired


def write_runtime_scaffold_files(*_: Any, **__: Any) -> list[Path]:
    _retired()


def write_productized_runtime_files(*_: Any, **__: Any) -> list[Path]:
    _retired()


def source_extraction_completed_event(
    report: ExtractionReport,
    *,
    run_id: str = "legacy-retired",
    task_id: str = "source-extraction",
) -> EventRecord:
    return EventRecord(
        run_id=run_id,
        task_id=task_id,
        node_id="source-extraction-retired",
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "source_extraction_retired": {
                "status": "retired",
                "reason": "legacy_source_pool_retired",
                "source_repo": report.plan.source_repo,
                "current_runtime_owner": "packages/runtime",
                "fallback_available": False,
            }
        },
    )


def normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return str(PurePosixPath(normalized))


__all__ = [
    "DEFAULT_EXCLUDE_PATTERNS",
    "SOURCE_SUFFIXES",
    "ExtractionDisposition",
    "ExtractionItem",
    "ExtractionPlan",
    "ExtractionReport",
    "ExtractionSource",
    "ExtractionTarget",
    "LedgerUpsertPlan",
    "LegacySourcePoolRetiredError",
    "OverwritePolicy",
    "SourceExtractionError",
    "SourceExtractor",
    "claude_code_m1_01b_plan",
    "claude_code_m1_02a_plan",
    "claude_code_m1_02b_plan",
    "claude_code_m1_02c_plan",
    "normalize_repo_path",
    "source_extraction_completed_event",
    "write_productized_runtime_files",
    "write_runtime_scaffold_files",
]
