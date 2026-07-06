from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .ledger_models import (
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LicenseNotice,
    LineCountPolicy,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    SourceEvidence,
    TestEntry,
    stable_ledger_id,
    to_jsonable,
)
from .ledger_store import InternalizationLedger, load_project_ledger, load_seed_ledger, package_seed_path, save_project_ledger


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

DEFAULT_EXCLUDE_PATTERNS = [
    ".git/**",
    ".github/**",
    ".githooks/**",
    ".husky/**",
    ".vscode/**",
    ".idea/**",
    "**/.DS_Store",
    "**/Thumbs.db",
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.cache/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/coverage/**",
    "**/.next/**",
    "**/.nuxt/**",
    "**/*.map",
    "**/*.log",
    "**/*.tmp",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.sqlite",
    "**/*.sqlite3",
    "**/bun.lock",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/yarn.lock",
    "**/poetry.lock",
    "**/uv.lock",
    "**/README.md",
    "**/CHANGELOG.md",
    "**/SECURITY.md",
    "**/CONTRIBUTING.md",
    "**/LICENSE",
    "**/CLAUDE.md",
    "**/TODO.md",
    "**/RECORD.md",
    "**/docs/**",
    "**/examples/**",
    "**/example/**",
    "**/demo/**",
    "**/playground/**",
    "**/fixtures/**",
    "**/__fixtures__/**",
    "**/testdata/**",
    "**/*.test.ts",
    "**/*.spec.ts",
    "**/*.test.tsx",
    "**/*.spec.tsx",
]


CLAUDE_CODE_PILOT_SOURCES = [
    "src/Tool.ts",
    "src/tools.ts",
    "src/context.ts",
    "src/cost-tracker.ts",
    "src/services/tools/toolOrchestration.ts",
    "src/services/tools/StreamingToolExecutor.ts",
    "src/utils/sessionState.ts",
    "src/utils/abortController.ts",
    "src/utils/generators.ts",
    "src/utils/systemPromptType.ts",
    "src/types/ids.ts",
    "src/types/message.ts",
    "src/types/permissions.ts",
    "src/types/tools.ts",
    "src/query/config.ts",
    "src/query/deps.ts",
    "src/query/stopHooks.ts",
    "src/query/tokenBudget.ts",
    "src/tools/BashTool/bashCommandHelpers.ts",
    "src/tools/BashTool/bashPermissions.ts",
    "src/tools/BashTool/bashSecurity.ts",
    "src/tools/BashTool/pathValidation.ts",
    "src/tools/BashTool/readOnlyValidation.ts",
    "src/tools/BashTool/commandSemantics.ts",
    "src/tools/BashTool/destructiveCommandWarning.ts",
    "src/tools/BashTool/modeValidation.ts",
    "src/tools/BashTool/shouldUseSandbox.ts",
    "src/tools/AgentTool/forkSubagent.ts",
    "src/tools/AgentTool/prompt.ts",
    "src/tools/AgentTool/agentMemory.ts",
    "src/tools/AgentTool/agentMemorySnapshot.ts",
    "src/tools/AgentTool/agentToolUtils.ts",
    "src/tools/SkillTool/prompt.ts",
]


CLAUDE_CODE_PRODUCTIZED_RUNTIME_SOURCES = [
    "src/QueryEngine.ts",
    "src/query.ts",
    "src/Tool.ts",
    "src/tools.ts",
    "src/commands.ts",
    "src/context.ts",
    "src/cost-tracker.ts",
    "src/services/tools",
    "src/services/compact",
    "src/hooks/toolPermission",
    "src/tools/AgentTool",
    "src/tools/SkillTool",
    "src/tools/BashTool",
    "src/tools/FileReadTool",
    "src/tools/FileEditTool",
    "src/tools/FileWriteTool",
    "src/tools/GlobTool",
    "src/tools/GrepTool",
    "src/tools/TodoWriteTool",
    "src/tools/ToolSearchTool",
    "src/tools/WebFetchTool",
    "src/tools/WebSearchTool",
    "src/tools/ListMcpResourcesTool",
    "src/tools/ReadMcpResourceTool",
    "src/tools/MCPTool",
    "src/tools/McpAuthTool",
    "src/services/mcp",
    "src/utils/toolResultStorage.ts",
    "src/utils/queryContext.ts",
    "src/utils/sessionStorage.ts",
    "src/utils/sessionState.ts",
    "src/utils/messagePredicates.ts",
    "src/utils/messageQueueManager.ts",
    "src/utils/messages.ts",
]


class OverwritePolicy(StrEnum):
    NEVER = "never"
    IF_CHANGED = "if_changed"
    ALWAYS = "always"


class ExtractionDisposition(StrEnum):
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
        target = self.target_root / normalize_repo_path(self.relative_path)
        return target.relative_to(self.project_root).as_posix()

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
    disposition: ExtractionDisposition
    reason: str = ""
    previous_sha256: str = ""

    @property
    def effective_line_count(self) -> int:
        if self.disposition in {ExtractionDisposition.COPIED, ExtractionDisposition.SKIPPED_IDENTICAL, ExtractionDisposition.DRY_RUN}:
            return self.line_count if self.source_like else 0
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repo": self.source.source_repo,
            "source_path": normalize_repo_path(self.source.source_path),
            "target_path": self.target.project_relative_path,
            "line_count": self.line_count,
            "effective_line_count": self.effective_line_count,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "previous_sha256": self.previous_sha256,
            "source_like": self.source_like,
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
        return to_jsonable(self)


@dataclass(slots=True)
class ExtractionPlan:
    source_repo: str
    source_root: Path
    project_root: Path
    target_root: Path
    include_paths: list[str]
    exclude_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS))
    owner_unit: str = "M1-01B"
    milestone: str = "M1"
    downstream_units: list[str] = field(default_factory=lambda: ["M1-02A", "M1-02B", "M1-02C", "M1-03A"])
    overwrite_policy: OverwritePolicy = OverwritePolicy.IF_CHANGED
    dry_run: bool = False
    report_path: Path | None = None
    manifest_module_path: Path | None = None
    runtime_command: str = "python scripts/verify_extraction_runtime_scaffold.py"
    runtime_module: str = "zyra_runtime.scaffold"
    runtime_health_check: str = "python scripts/verify_extraction_runtime_scaffold.py"
    test_path: str = "tests/unit/test_source_extraction_runtime_scaffold.py"
    test_command: str = "python -m unittest tests.unit.test_source_extraction_runtime_scaffold"
    capability_prefix: str = "claude_code_runtime_pilot"
    capability_summary: str = "Pilot extraction for Claude Code runtime contracts used by M1-02A."
    target_mount: str = "pilot/claude-code-best"
    manifest_export_name: str = "zyraClaudeCodePilotManifest"
    manifest_health_export_name: str = "zyraClaudeCodePilotHealth"
    manifest_kind: str = "pilot"
    runtime_function: str = "default_m1_01b_runtime_scaffold"
    lifecycle: LedgerLifecycle = LedgerLifecycle.ACTIVE
    main_path_status: MainPathStatus = MainPathStatus.WORKER_RUNTIME_CONNECTED
    main_path_worker_runtime: str = "CodeWorkerRuntime:claude-code-runtime-pilot"
    main_path_surfaces: list[str] = field(
        default_factory=lambda: ["vendor-runtimes", "packages/runtime", "packages/workers", "packages/integrations"]
    )
    main_path_event_types: list[str] = field(default_factory=lambda: ["runtime_scaffold_health", "source_extraction_completed"])
    main_path_control_commands: list[str] = field(default_factory=lambda: ["ledger:accounting", "ledger:gate"])
    main_path_artifact_kinds: list[str] = field(default_factory=lambda: ["source_inventory", "runtime_manifest"])
    tags: list[str] = field(default_factory=lambda: ["m1-01b", "source-extraction", "claude-code-runtime-pilot"])
    source_evidence_tags: list[str] = field(default_factory=lambda: ["m1-01b", "pilot-extraction"])
    replacement_plan: str = (
        "M1-02A will promote the selected Claude Code runtime files from pilot scope into the productized runtime boundary."
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
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def copied_count(self) -> int:
        return len(self.copied)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded)

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def effective_line_count(self) -> int:
        return sum(item.effective_line_count for item in [*self.copied, *self.skipped])

    @property
    def raw_line_count(self) -> int:
        return sum(item.line_count for item in [*self.copied, *self.skipped, *self.excluded, *self.missing])

    @property
    def target_paths(self) -> list[str]:
        return sorted({item.target.project_relative_path for item in [*self.copied, *self.skipped]})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": {
                "source_repo": self.plan.source_repo,
                "owner_unit": self.plan.owner_unit,
                "target_root": self.plan.normalized_target_root().relative_to(self.plan.project_root).as_posix(),
                "dry_run": self.plan.dry_run,
                "overwrite_policy": str(self.plan.overwrite_policy),
                "copied_count": self.copied_count,
                "skipped_count": self.skipped_count,
                "excluded_count": self.excluded_count,
                "missing_count": self.missing_count,
                "effective_line_count": self.effective_line_count,
                "raw_line_count": self.raw_line_count,
                "ledger_upsert_count": len(self.ledger_upserts),
            },
            "copied": [item.to_dict() for item in self.copied],
            "skipped": [item.to_dict() for item in self.skipped],
            "excluded": [item.to_dict() for item in self.excluded],
            "missing": [item.to_dict() for item in self.missing],
            "ledger_upserts": [item.to_dict() for item in self.ledger_upserts],
            "errors": list(self.errors),
        }


class SourceExtractionError(RuntimeError):
    pass


class SourceExtractor:
    def __init__(self, plan: ExtractionPlan) -> None:
        self.plan = plan
        self.project_root = plan.project_root.resolve()
        self.source_root = plan.source_root.resolve()
        self.target_root = plan.normalized_target_root()
        self._assert_inside_project(self.target_root)

    def run(self) -> ExtractionReport:
        report = ExtractionReport(plan=self.plan)
        candidates = self._collect_source_files()
        for source_path in candidates:
            source = ExtractionSource(
                source_repo=self.plan.source_repo,
                repo_root=self.source_root,
                source_path=source_path,
            )
            target = ExtractionTarget(
                project_root=self.project_root,
                target_root=self.target_root,
                relative_path=f"{self.plan.target_mount}/{normalize_repo_path(source_path)}",
            )
            item = self._build_item(source, target)
            if item.disposition == ExtractionDisposition.EXCLUDED:
                report.excluded.append(item)
                continue
            if item.disposition == ExtractionDisposition.MISSING_SOURCE:
                report.missing.append(item)
                report.errors.append(f"missing source: {source.source_path}")
                continue
            if not self.plan.dry_run and item.disposition == ExtractionDisposition.COPIED:
                self._copy_item(source.absolute_path, target.absolute_path)
            if item.disposition in {ExtractionDisposition.COPIED, ExtractionDisposition.DRY_RUN}:
                report.copied.append(item)
            else:
                report.skipped.append(item)
        if not report.copied and not report.skipped:
            report.errors.append("extraction produced no copied or reusable files")
        if report.missing:
            report.errors.append(f"{len(report.missing)} source files were missing")
        report.ledger_upserts = [
            self._ledger_upsert_plan_for_item(item)
            for item in [*report.copied, *report.skipped]
            if item.source_like
        ]
        if not self.plan.dry_run:
            self._write_runtime_manifest(report)
            self._write_report(report)
        return report

    def build_ledger_entries(self, report: ExtractionReport) -> list[InternalizationLedgerEntry]:
        return [
            self._entry_for_item(item)
            for item in [*report.copied, *report.skipped]
            if item.source_like
        ]

    def upsert_ledger_entries(
        self,
        report: ExtractionReport,
        *,
        project_ledger: bool = True,
        seed_ledger: bool = False,
    ) -> dict[str, Any]:
        entries = self.build_ledger_entries(report)
        payload: dict[str, Any] = {"entry_count": len(entries), "project_ledger": None, "seed_ledger": None}
        if project_ledger:
            ledger = load_project_ledger(self.project_root, bootstrap=True)
            actions = []
            for entry in entries:
                mutation = ledger.upsert(entry)
                actions.append(to_jsonable(mutation))
            save_project_ledger(self.project_root, ledger)
            payload["project_ledger"] = {"path": str(self.project_root / "tmp" / "internalization_ledger.json"), "actions": actions}
        if seed_ledger:
            seed = load_seed_ledger()
            actions = []
            for entry in entries:
                mutation = seed.upsert(entry)
                actions.append(to_jsonable(mutation))
            seed.save(package_seed_path())
            payload["seed_ledger"] = {"path": str(package_seed_path()), "actions": actions}
        return payload

    def _collect_source_files(self) -> list[str]:
        files: set[str] = set()
        for include in self.plan.include_paths:
            normalized = normalize_repo_path(include)
            source_abs = self._safe_source_path(normalized)
            if source_abs.is_file():
                files.add(normalized)
                continue
            if source_abs.is_dir():
                for child in source_abs.rglob("*"):
                    if child.is_file():
                        files.add(child.relative_to(self.source_root).as_posix())
                continue
            files.add(normalized)
        return sorted(files)

    def _build_item(self, source: ExtractionSource, target: ExtractionTarget) -> ExtractionItem:
        source_path = normalize_repo_path(source.source_path)
        if self._is_excluded(source_path):
            return self._empty_item(source, target, ExtractionDisposition.EXCLUDED, reason="matched exclude pattern")
        source_abs = self._safe_source_path(source_path)
        if not source_abs.exists() or not source_abs.is_file():
            return self._empty_item(source, target, ExtractionDisposition.MISSING_SOURCE, reason="source file not found")
        target_abs = target.absolute_path.resolve()
        self._assert_inside_project(target_abs)
        text = source_abs.read_text(encoding="utf-8", errors="replace")
        raw = source_abs.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        previous_sha = _sha256_file(target_abs) if target_abs.exists() and target_abs.is_file() else ""
        source_like = PurePosixPath(source_path).suffix.lower() in SOURCE_SUFFIXES
        line_count = len(text.splitlines())
        disposition = ExtractionDisposition.COPIED
        reason = "copy scheduled"
        if self.plan.dry_run:
            disposition = ExtractionDisposition.DRY_RUN
            reason = "dry run"
        elif target_abs.exists():
            if self.plan.overwrite_policy == OverwritePolicy.ALWAYS:
                disposition = ExtractionDisposition.COPIED
                reason = "overwrite policy is always"
            elif previous_sha == sha:
                disposition = ExtractionDisposition.SKIPPED_IDENTICAL
                reason = "target already matches source"
            elif self.plan.overwrite_policy == OverwritePolicy.NEVER:
                disposition = ExtractionDisposition.SKIPPED_EXISTING
                reason = "target exists and overwrite policy is never"
            elif self.plan.overwrite_policy == OverwritePolicy.IF_CHANGED:
                disposition = ExtractionDisposition.COPIED
                reason = "target changed and overwrite allowed"
        return ExtractionItem(
            source=source,
            target=target,
            line_count=line_count,
            byte_count=len(raw),
            sha256=sha,
            source_like=source_like,
            disposition=disposition,
            reason=reason,
            previous_sha256=previous_sha,
        )

    def _empty_item(
        self,
        source: ExtractionSource,
        target: ExtractionTarget,
        disposition: ExtractionDisposition,
        *,
        reason: str,
    ) -> ExtractionItem:
        return ExtractionItem(
            source=source,
            target=target,
            line_count=0,
            byte_count=0,
            sha256="",
            source_like=False,
            disposition=disposition,
            reason=reason,
        )

    def _copy_item(self, source: Path, target: Path) -> None:
        self._assert_inside_project(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def _write_report(self, report: ExtractionReport) -> None:
        report_path = self.plan.report_path or self.target_root / "metadata" / "source_inventory.json"
        if not report_path.is_absolute():
            report_path = self.project_root / report_path
        self._assert_inside_project(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _write_runtime_manifest(self, report: ExtractionReport) -> None:
        manifest = self.plan.manifest_module_path or self.target_root / "src" / "zyra-pilot-manifest.mjs"
        if not manifest.is_absolute():
            manifest = self.project_root / manifest
        self._assert_inside_project(manifest)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        files = [
            {
                "sourcePath": item.source.source_path,
                "targetPath": item.target.project_relative_path,
                "sha256": item.sha256,
                "lineCount": item.line_count,
                "effectiveLineCount": item.effective_line_count,
            }
            for item in [*report.copied, *report.skipped]
            if item.source_like
        ]
        text = [
            f"export const {self.plan.manifest_export_name} = Object.freeze({{",
            f"  sourceRepo: {json.dumps(self.plan.source_repo, ensure_ascii=False)},",
            f"  ownerUnit: {json.dumps(self.plan.owner_unit, ensure_ascii=False)},",
            f"  manifestKind: {json.dumps(self.plan.manifest_kind, ensure_ascii=False)},",
            '  targetRuntime: "vendor-runtimes/claude-code-runtime",',
            f"  targetMount: {json.dumps(self.plan.target_mount, ensure_ascii=False)},",
            f"  purpose: {json.dumps(self.plan.capability_summary, ensure_ascii=False)},",
            f"  copiedFileCount: {len(files)},",
            f"  effectiveLineCount: {sum(item['effectiveLineCount'] for item in files)},",
            "  files: Object.freeze([",
        ]
        for item in files:
            text.append(
                "    Object.freeze("
                + json.dumps(item, ensure_ascii=False, sort_keys=True)
                + "),"
            )
        text.extend(
            [
                "  ]),",
                "});",
                "",
                f"export function {self.plan.manifest_health_export_name}() {{",
                "  return {",
                f"    ok: {self.plan.manifest_export_name}.copiedFileCount > 0,",
                f"    sourceRepo: {self.plan.manifest_export_name}.sourceRepo,",
                f"    ownerUnit: {self.plan.manifest_export_name}.ownerUnit,",
                f"    manifestKind: {self.plan.manifest_export_name}.manifestKind,",
                f"    effectiveLineCount: {self.plan.manifest_export_name}.effectiveLineCount,",
                f"    copiedFileCount: {self.plan.manifest_export_name}.copiedFileCount,",
                "  };",
                "}",
                "",
            ]
        )
        manifest.write_text("\n".join(text), encoding="utf-8")

    def _entry_for_item(self, item: ExtractionItem) -> InternalizationLedgerEntry:
        capability = self._capability_name(item)
        entry = InternalizationLedgerEntry.new(
            source_repo=item.source.source_repo,
            source_path=item.source.source_path,
            capability_name=capability,
            capability_summary=f"{self.plan.capability_summary} Source file line_count={item.line_count}.",
            target_paths=[item.target.project_relative_path],
            migration_strategy=MigrationStrategy.VENDORED_RUNTIME,
            main_path_status=self.plan.main_path_status,
            lifecycle=self.plan.lifecycle,
            owner_unit=self.plan.owner_unit,
            milestone=self.plan.milestone,
            runtime_entry=RuntimeEntry(
                command=self.plan.runtime_command,
                module=self.plan.runtime_module,
                function=self.plan.runtime_function,
                protocol="zyra-runtime-scaffold-v1",
                health_check=self.plan.runtime_health_check,
                config_refs=[self._project_relative_manifest_path()],
            ),
            test_entries=[
                TestEntry(
                    path=self.plan.test_path,
                    command=self.plan.test_command,
                    kind="unit",
                    expected_signal="source extraction and runtime scaffold smoke passes",
                )
            ],
            main_path=MainPathBinding(
                surfaces=list(self.plan.main_path_surfaces),
                event_types=list(self.plan.main_path_event_types),
                control_commands=list(self.plan.main_path_control_commands),
                artifact_kinds=list(self.plan.main_path_artifact_kinds),
                worker_runtime=self.plan.main_path_worker_runtime,
            ),
            line_count_policy=LineCountPolicy.COUNTS_AS_RUNTIME,
            license_notice=LicenseNotice(
                source_repo=item.source.source_repo,
                status=NoticeStatus.RECORDED,
                license_hint="Upstream license/NOTICE is tracked for productized runtime cleanup.",
                notice_path="third_party/NOTICE.md",
                notes="M1-01B pilot extraction; final NOTICE consolidation is handled by M3.",
            ),
            extracted_sha256=item.sha256,
            extracted_lines=item.line_count,
        )
        entry.source_evidence = [
            SourceEvidence(
                source_repo=item.source.source_repo,
                source_path=item.source.source_path,
                exists_in_workspace=True,
                source_kind="file",
                reason=f"{self.plan.manifest_kind} extraction source file",
                symbols=[PurePosixPath(item.source.source_path).stem],
                tags=list(self.plan.source_evidence_tags),
            )
        ]
        entry.downstream_units = list(self.plan.downstream_units)
        entry.tags = list(self.plan.tags)
        entry.replacement_plan = self.plan.replacement_plan
        return entry

    def _ledger_upsert_plan_for_item(self, item: ExtractionItem) -> LedgerUpsertPlan:
        capability = self._capability_name(item)
        return LedgerUpsertPlan(
            ledger_id=stable_ledger_id(item.source.source_repo, item.source.source_path, capability),
            source_repo=item.source.source_repo,
            source_path=item.source.source_path,
            target_path=item.target.project_relative_path,
            owner_unit=self.plan.owner_unit,
            capability_name=capability,
            action="upsert",
            lifecycle=str(self.plan.lifecycle),
            main_path_status=str(self.plan.main_path_status),
        )

    def _capability_name(self, item: ExtractionItem) -> str:
        stem = normalize_repo_path(item.source.source_path).replace("/", "_").replace(".", "_").replace("-", "_")
        return f"{self.plan.capability_prefix}_{stem}"

    def _is_excluded(self, relative_path: str) -> bool:
        normalized = normalize_repo_path(relative_path)
        return any(_match_pattern(normalized, pattern) for pattern in self.plan.exclude_patterns)

    def _safe_source_path(self, relative_path: str) -> Path:
        normalized = normalize_repo_path(relative_path)
        if _has_parent_segment(normalized):
            raise SourceExtractionError(f"source path may not contain parent segments: {relative_path}")
        target = (self.source_root / normalized).resolve()
        try:
            target.relative_to(self.source_root)
        except ValueError as error:
            raise SourceExtractionError(f"source path escapes source root: {relative_path}") from error
        return target

    def _assert_inside_project(self, path: Path) -> None:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError as error:
            raise SourceExtractionError(f"path escapes project root: {path}") from error

    def _project_relative_manifest_path(self) -> str:
        manifest = self.plan.manifest_module_path or self.target_root / "src" / "zyra-pilot-manifest.mjs"
        if not manifest.is_absolute():
            manifest = self.project_root / manifest
        return manifest.resolve().relative_to(self.project_root).as_posix()


def claude_code_m1_01b_plan(
    *,
    project_root: Path,
    source_workspace_root: Path,
    dry_run: bool = False,
    overwrite_policy: OverwritePolicy = OverwritePolicy.IF_CHANGED,
) -> ExtractionPlan:
    target_root = project_root / "vendor-runtimes" / "claude-code-runtime"
    return ExtractionPlan(
        source_repo="claude-code-best",
        source_root=source_workspace_root / "claude-code-best",
        project_root=project_root,
        target_root=target_root,
        include_paths=list(CLAUDE_CODE_PILOT_SOURCES),
        dry_run=dry_run,
        overwrite_policy=overwrite_policy,
        report_path=target_root / "metadata" / "source_inventory.json",
        manifest_module_path=target_root / "src" / "zyra-pilot-manifest.mjs",
    )


def claude_code_m1_02a_plan(
    *,
    project_root: Path,
    source_workspace_root: Path,
    dry_run: bool = False,
    overwrite_policy: OverwritePolicy = OverwritePolicy.IF_CHANGED,
) -> ExtractionPlan:
    target_root = project_root / "vendor-runtimes" / "claude-code-runtime"
    return ExtractionPlan(
        source_repo="claude-code-best",
        source_root=source_workspace_root / "claude-code-best",
        project_root=project_root,
        target_root=target_root,
        include_paths=list(CLAUDE_CODE_PRODUCTIZED_RUNTIME_SOURCES),
        owner_unit="M1-02A",
        milestone="M1",
        downstream_units=["M1-02B", "M1-02C", "M1-02D", "M1-03A", "M1-03B", "M1-03C", "M1-03D", "M1-08"],
        dry_run=dry_run,
        overwrite_policy=overwrite_policy,
        report_path=target_root / "metadata" / "productized_source_inventory.json",
        manifest_module_path=target_root / "src" / "zyra-productized-manifest.mjs",
        runtime_command="node vendor-runtimes/claude-code-runtime/src/zyra-productized-smoke.mjs",
        runtime_module="@zyra/claude-code-runtime/productized",
        runtime_function="zyraClaudeCodeProductizedHealth",
        runtime_health_check="node vendor-runtimes/claude-code-runtime/src/zyra-productized-smoke.mjs",
        test_path="tests/integration/test_claude_code_productized_runtime.py",
        test_command="python -m unittest tests.integration.test_claude_code_productized_runtime",
        capability_prefix="claude_code_runtime_productized",
        capability_summary=(
            "Productized Claude Code runtime source boundary for Zyra CodeWorkerRuntime, QueryEngine, "
            "tool orchestration, compact, permission, MCP, SkillTool, and AgentTool handoff."
        ),
        target_mount="productized/claude-code-best",
        manifest_export_name="zyraClaudeCodeProductizedManifest",
        manifest_health_export_name="zyraClaudeCodeProductizedHealth",
        manifest_kind="productized",
        lifecycle=LedgerLifecycle.PRODUCTIZED,
        main_path_status=MainPathStatus.WORKER_RUNTIME_CONNECTED,
        main_path_worker_runtime="CodeWorkerRuntime:claude-code-runtime-productized",
        main_path_event_types=[
            "query_session",
            "tool_result",
            "runtime_scaffold_health",
            "source_extraction_completed",
            "reference_crosswalk_verified",
        ],
        main_path_control_commands=[
            "code-worker:health",
            "code-worker:inventory",
            "code-worker:query-contract",
            "ledger:accounting",
            "ledger:gate",
        ],
        main_path_artifact_kinds=[
            "trace",
            "source_inventory",
            "runtime_manifest",
            "reference_crosswalk",
            "structured_data",
        ],
        tags=["m1-02a", "source-extraction", "claude-code-runtime-productized", "reference-only-assisted"],
        source_evidence_tags=["m1-02a", "productized-extraction", "reference-only-assisted"],
        replacement_plan=(
            "M1-02B/M1-02C/M1-02D will bind these productized Claude Code runtime sources to Zyra's "
            "query loop, tool loop, session lifecycle, compact/restore, and CodeWorker API without relying on parent paths."
        ),
    )


def write_runtime_scaffold_files(project_root: Path) -> list[Path]:
    runtime_root = project_root / "vendor-runtimes" / "claude-code-runtime"
    files: dict[Path, str] = {
        runtime_root / "package.json": json.dumps(
            {
                "name": "@zyra/claude-code-runtime",
                "private": True,
                "type": "module",
                "version": "0.1.0-m1-01b",
                "description": "Productized Claude Code runtime pilot boundary for Zyra.",
                "scripts": {
                    "health": "node src/zyra-pilot-smoke.mjs",
                    "pilot:health": "node src/zyra-pilot-smoke.mjs",
                    "productized:health": "node src/zyra-productized-smoke.mjs",
                    "inventory": "node src/zyra-pilot-smoke.mjs --inventory",
                    "productized:inventory": "node src/zyra-productized-smoke.mjs --inventory",
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        runtime_root / "src" / "zyra-pilot-smoke.mjs": _pilot_smoke_module(),
    }
    written: list[Path] = []
    for path, text in files.items():
        resolved = path.resolve()
        try:
            resolved.relative_to(project_root.resolve())
        except ValueError as error:
            raise SourceExtractionError(f"scaffold path escapes project root: {path}") from error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def write_productized_runtime_files(project_root: Path) -> list[Path]:
    runtime_root = project_root / "vendor-runtimes" / "claude-code-runtime"
    package_path = runtime_root / "package.json"
    package_payload = {
        "name": "@zyra/claude-code-runtime",
        "private": True,
        "type": "module",
        "version": "0.1.0-m1-02a",
        "description": "Productized Claude Code runtime boundary for Zyra.",
        "scripts": {
            "health": "node src/zyra-productized-smoke.mjs",
            "pilot:health": "node src/zyra-pilot-smoke.mjs",
            "productized:health": "node src/zyra-productized-smoke.mjs",
            "inventory": "node src/zyra-productized-smoke.mjs --inventory",
            "pilot:inventory": "node src/zyra-pilot-smoke.mjs --inventory",
            "productized:inventory": "node src/zyra-productized-smoke.mjs --inventory",
        },
    }
    files: dict[Path, str] = {
        package_path: json.dumps(package_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        runtime_root / "src" / "zyra-productized-smoke.mjs": _productized_smoke_module(),
    }
    written: list[Path] = []
    for path, text in files.items():
        resolved = path.resolve()
        try:
            resolved.relative_to(project_root.resolve())
        except ValueError as error:
            raise SourceExtractionError(f"productized scaffold path escapes project root: {path}") from error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return str(PurePosixPath(normalized))


def _sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _has_parent_segment(path: str) -> bool:
    return ".." in PurePosixPath(path).parts


def _match_pattern(path: str, pattern: str) -> bool:
    normalized = normalize_repo_path(pattern)
    if fnmatch.fnmatch(path, normalized):
        return True
    if normalized.startswith("**/") and fnmatch.fnmatch(path, normalized[3:]):
        return True
    return False


def _pilot_smoke_module() -> str:
    return """import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { zyraClaudeCodePilotHealth, zyraClaudeCodePilotManifest } from "./zyra-pilot-manifest.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const runtimeRoot = path.resolve(__dirname, "..");

function fileExists(projectRelativePath) {
  const projectRoot = path.resolve(runtimeRoot, "..", "..");
  return fs.existsSync(path.resolve(projectRoot, projectRelativePath));
}

const missing = zyraClaudeCodePilotManifest.files.filter((item) => !fileExists(item.targetPath));
const health = {
  ...zyraClaudeCodePilotHealth(),
  runtimeRoot,
  missingTargetCount: missing.length,
  missingTargets: missing.map((item) => item.targetPath),
};

if (process.argv.includes("--inventory")) {
  process.stdout.write(`${JSON.stringify({ ok: health.ok && missing.length === 0, manifest: zyraClaudeCodePilotManifest, health })}\\n`);
} else {
  process.stdout.write(`${JSON.stringify({ ok: health.ok && missing.length === 0, health })}\\n`);
}

if (!health.ok || missing.length > 0) {
  process.exitCode = 1;
}
"""


def _productized_smoke_module() -> str:
    return """import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { zyraClaudeCodeProductizedHealth, zyraClaudeCodeProductizedManifest } from "./zyra-productized-manifest.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const runtimeRoot = path.resolve(__dirname, "..");
const projectRoot = path.resolve(runtimeRoot, "..", "..");
const crosswalkPath = path.resolve(runtimeRoot, "metadata", "reference_crosswalk.json");

function fileExists(projectRelativePath) {
  return fs.existsSync(path.resolve(projectRoot, projectRelativePath));
}

function readCrosswalk() {
  if (!fs.existsSync(crosswalkPath)) {
    return { ok: false, entryCount: 0, missingTargetCount: 0, referenceOnlyRepos: [] };
  }
  const payload = JSON.parse(fs.readFileSync(crosswalkPath, "utf8"));
  return {
    ok: payload.ok === true,
    entryCount: payload.summary?.entry_count ?? 0,
    missingTargetCount: payload.summary?.missing_target_count ?? 0,
    referenceOnlyRepos: payload.summary?.reference_only_repos ?? [],
  };
}

const missing = zyraClaudeCodeProductizedManifest.files.filter((item) => !fileExists(item.targetPath));
const crosswalk = readCrosswalk();
const health = {
  ...zyraClaudeCodeProductizedHealth(),
  runtimeRoot,
  productizedRoot: path.resolve(runtimeRoot, "productized", "claude-code-best"),
  missingTargetCount: missing.length,
  missingTargets: missing.map((item) => item.targetPath),
  referenceCrosswalk: crosswalk,
};

const ok = health.ok && missing.length === 0 && crosswalk.ok;
if (process.argv.includes("--inventory")) {
  process.stdout.write(`${JSON.stringify({ ok, manifest: zyraClaudeCodeProductizedManifest, health })}\\n`);
} else {
  process.stdout.write(`${JSON.stringify({ ok, health })}\\n`);
}

if (!ok) {
  process.exitCode = 1;
}
"""
