from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, TypeVar
from uuid import uuid5, NAMESPACE_URL


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stable_ledger_id(source_repo: str, source_path: str, capability_name: str = "") -> str:
    raw = f"{source_repo}:{source_path}:{capability_name}".strip(":")
    digest = uuid5(NAMESPACE_URL, raw).hex[:16]
    prefix = _slug(source_repo)
    return f"ile_{prefix}_{digest}"


class MigrationStrategy(StrEnum):
    DIRECT_PORT = "direct_port"
    ADAPTER = "adapter"
    SIDECAR_RUNTIME = "sidecar_runtime"
    VENDORED_RUNTIME = "vendored_runtime"
    REIMPLEMENTED_PATTERN = "reimplemented_pattern"
    PLANNED_ADAPTER = "planned_adapter"
    CANDIDATE_REVIEW = "candidate_review"
    NOT_SELECTED = "not_selected"


class MainPathStatus(StrEnum):
    PLANNED = "planned"
    INVENTORIED = "inventoried"
    VENDORED = "vendored"
    ADAPTER_READY = "adapter_ready"
    API_CONNECTED = "api_connected"
    EVENT_LOG_CONNECTED = "event_log_connected"
    CONTROL_COMMAND_CONNECTED = "control_command_connected"
    WORKER_RUNTIME_CONNECTED = "worker_runtime_connected"
    UI_CONNECTED = "ui_connected"
    TESTED_MAIN_PATH = "tested_main_path"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class LedgerLifecycle(StrEnum):
    CANDIDATE = "candidate"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    ACTIVE = "active"
    INTERNALIZED = "internalized"
    PRODUCTIZED = "productized"
    DEFERRED = "deferred"
    REJECTED = "rejected"


class LineCountPolicy(StrEnum):
    EXCLUDED_INVENTORY_ONLY = "excluded_inventory_only"
    COUNTS_WHEN_PRODUCTIZED = "counts_when_productized"
    COUNTS_AS_RUNTIME = "counts_as_runtime"
    COUNTS_AS_TEST = "counts_as_test"
    COUNTS_AS_SCRIPT = "counts_as_script"
    EXCLUDED_DOCUMENTATION = "excluded_documentation"


class NoticeStatus(StrEnum):
    PENDING = "pending"
    RECORDED = "recorded"
    NOT_REQUIRED = "not_required"
    NEEDS_REVIEW = "needs_review"


class AuditDisposition(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    FAILING = "failing"
    BLOCKED = "blocked"


@dataclass(slots=True)
class LicenseNotice:
    source_repo: str
    status: NoticeStatus = NoticeStatus.PENDING
    license_hint: str = ""
    notice_path: str = "third_party/NOTICE.md"
    source_url: str = ""
    notes: str = ""

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.source_repo.strip():
            errors.append("license_notice.source_repo is required")
        if not self.notice_path.strip():
            errors.append("license_notice.notice_path is required")
        return errors


@dataclass(slots=True)
class RuntimeEntry:
    command: str = ""
    module: str = ""
    function: str = ""
    protocol: str = ""
    health_check: str = ""
    config_refs: list[str] = field(default_factory=list)
    environment_refs: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.command.strip(),
                self.module.strip(),
                self.function.strip(),
                self.protocol.strip(),
                self.health_check.strip(),
                self.config_refs,
                self.environment_refs,
            ]
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        for item in [self.command, self.module, self.function, self.health_check, *self.config_refs, *self.environment_refs]:
            if _has_forbidden_parent_reference(item):
                errors.append(f"runtime_entry contains parent-source reference: {item}")
        return errors


@dataclass(slots=True)
class TestEntry:
    path: str
    command: str = ""
    kind: str = "unit"
    expected_signal: str = ""
    required: bool = True

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.path.strip() and self.required:
            errors.append("test_entry.path is required")
        if _has_forbidden_parent_reference(self.path) or _has_forbidden_parent_reference(self.command):
            errors.append(f"test_entry contains parent-source reference: {self.path} {self.command}".strip())
        return errors


@dataclass(slots=True)
class SourceEvidence:
    source_repo: str
    source_path: str
    exists_in_workspace: bool = False
    source_kind: str = "file"
    reason: str = ""
    symbols: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.source_repo.strip():
            errors.append("source_evidence.source_repo is required")
        if not self.source_path.strip():
            errors.append("source_evidence.source_path is required")
        if _has_forbidden_parent_reference(self.source_path):
            errors.append(f"source_evidence.source_path must be repo-relative, got {self.source_path!r}")
        return errors


@dataclass(slots=True)
class TargetBinding:
    target_path: str
    role: str = "primary"
    required_for_main_path: bool = True
    must_exist_for_statuses: list[MainPathStatus] = field(
        default_factory=lambda: [
            MainPathStatus.ADAPTER_READY,
            MainPathStatus.API_CONNECTED,
            MainPathStatus.EVENT_LOG_CONNECTED,
            MainPathStatus.CONTROL_COMMAND_CONNECTED,
            MainPathStatus.WORKER_RUNTIME_CONNECTED,
            MainPathStatus.UI_CONNECTED,
            MainPathStatus.TESTED_MAIN_PATH,
        ]
    )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.target_path.strip():
            errors.append("target_path is required")
        if _has_forbidden_parent_reference(self.target_path):
            errors.append(f"target_path contains parent-source reference: {self.target_path}")
        path_parts = PurePosixPath(self.target_path).parts
        if self.target_path.startswith("/") or (path_parts and ":" in path_parts[0]):
            errors.append(f"target_path must be project-relative: {self.target_path}")
        return errors


@dataclass(slots=True)
class MainPathBinding:
    surfaces: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    api_routes: list[str] = field(default_factory=list)
    control_commands: list[str] = field(default_factory=list)
    artifact_kinds: list[str] = field(default_factory=list)
    worker_runtime: str = ""
    ui_panels: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors: list[str] = []
        for value in [
            *self.surfaces,
            *self.event_types,
            *self.api_routes,
            *self.control_commands,
            *self.artifact_kinds,
            self.worker_runtime,
            *self.ui_panels,
        ]:
            if _has_forbidden_parent_reference(value):
                errors.append(f"main_path binding contains parent-source reference: {value}")
        return errors

    def is_empty(self) -> bool:
        return not any(
            [
                self.surfaces,
                self.event_types,
                self.api_routes,
                self.control_commands,
                self.artifact_kinds,
                self.worker_runtime.strip(),
                self.ui_panels,
            ]
        )


@dataclass(slots=True)
class InternalizationLedgerEntry:
    ledger_id: str
    source_repo: str
    source_path: str
    capability_name: str
    capability_summary: str
    target_bindings: list[TargetBinding]
    migration_strategy: MigrationStrategy
    main_path_status: MainPathStatus
    lifecycle: LedgerLifecycle = LedgerLifecycle.PLANNED
    runtime_entry: RuntimeEntry = field(default_factory=RuntimeEntry)
    test_entries: list[TestEntry] = field(default_factory=list)
    main_path: MainPathBinding = field(default_factory=MainPathBinding)
    line_count_policy: LineCountPolicy = LineCountPolicy.COUNTS_WHEN_PRODUCTIZED
    license_notice: LicenseNotice | None = None
    owner_unit: str = ""
    milestone: str = ""
    downstream_units: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    source_evidence: list[SourceEvidence] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    replacement_plan: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        *,
        source_repo: str,
        source_path: str,
        capability_name: str,
        capability_summary: str,
        target_paths: list[str],
        migration_strategy: MigrationStrategy = MigrationStrategy.PLANNED_ADAPTER,
        main_path_status: MainPathStatus = MainPathStatus.PLANNED,
        lifecycle: LedgerLifecycle = LedgerLifecycle.PLANNED,
        owner_unit: str = "",
        milestone: str = "",
        test_entries: list[TestEntry] | None = None,
        runtime_entry: RuntimeEntry | None = None,
        main_path: MainPathBinding | None = None,
        line_count_policy: LineCountPolicy = LineCountPolicy.COUNTS_WHEN_PRODUCTIZED,
        license_notice: LicenseNotice | None = None,
        **metadata: Any,
    ) -> "InternalizationLedgerEntry":
        return cls(
            ledger_id=stable_ledger_id(source_repo, source_path, capability_name),
            source_repo=source_repo,
            source_path=source_path,
            capability_name=capability_name,
            capability_summary=capability_summary,
            target_bindings=[TargetBinding(path) for path in target_paths],
            migration_strategy=migration_strategy,
            main_path_status=main_path_status,
            lifecycle=lifecycle,
            runtime_entry=runtime_entry or RuntimeEntry(),
            test_entries=list(test_entries or []),
            main_path=main_path or MainPathBinding(),
            line_count_policy=line_count_policy,
            license_notice=license_notice or LicenseNotice(source_repo=source_repo),
            owner_unit=owner_unit,
            milestone=milestone,
            metadata=dict(metadata),
        )

    @property
    def target_paths(self) -> list[str]:
        return [binding.target_path for binding in self.target_bindings]

    @property
    def primary_target_path(self) -> str:
        if not self.target_bindings:
            return ""
        for binding in self.target_bindings:
            if binding.role == "primary":
                return binding.target_path
        return self.target_bindings[0].target_path

    def update_status(
        self,
        *,
        main_path_status: MainPathStatus | str | None = None,
        lifecycle: LedgerLifecycle | str | None = None,
        note: str = "",
    ) -> None:
        if main_path_status is not None:
            self.main_path_status = _coerce_enum(MainPathStatus, main_path_status)
        if lifecycle is not None:
            self.lifecycle = _coerce_enum(LedgerLifecycle, lifecycle)
        if note:
            self.risk_notes.append(note)
        self.updated_at = now_iso()

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.ledger_id.strip():
            errors.append("ledger_id is required")
        if not self.source_repo.strip():
            errors.append("source_repo is required")
        if not self.source_path.strip():
            errors.append("source_path is required")
        if not self.capability_name.strip():
            errors.append("capability_name is required")
        if not self.capability_summary.strip():
            errors.append("capability_summary is required")
        if not self.target_bindings:
            errors.append("at least one target_path is required")
        if not self.test_entries:
            errors.append("at least one test_entry is required")
        if _has_forbidden_parent_reference(self.source_path):
            errors.append(f"source_path must be repo-relative, got {self.source_path!r}")
        for binding in self.target_bindings:
            errors.extend(binding.validate())
        for test_entry in self.test_entries:
            errors.extend(test_entry.validate())
        errors.extend(self.runtime_entry.validate())
        errors.extend(self.main_path.validate())
        if self.license_notice is None:
            errors.append("license_notice is required")
        else:
            errors.extend(self.license_notice.validate())
        for item in [self.owner_unit, self.milestone, *self.downstream_units, *self.dependencies, *self.tags]:
            if _has_forbidden_parent_reference(item):
                errors.append(f"metadata contains parent-source reference: {item}")
        for evidence in self.source_evidence:
            errors.extend(evidence.validate())
        return errors

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InternalizationLedgerEntry":
        source_repo = str(data.get("source_repo") or "")
        license_data = data.get("license_notice")
        return cls(
            ledger_id=str(data.get("ledger_id") or stable_ledger_id(source_repo, str(data.get("source_path") or ""), str(data.get("capability_name") or ""))),
            source_repo=source_repo,
            source_path=str(data.get("source_path") or ""),
            capability_name=str(data.get("capability_name") or ""),
            capability_summary=str(data.get("capability_summary") or ""),
            target_bindings=[
                _target_binding_from_json(item)
                for item in _as_list(data.get("target_bindings") or data.get("targets"))
            ]
            or [TargetBinding(str(path)) for path in _as_list(data.get("target_paths"))],
            migration_strategy=_coerce_enum(MigrationStrategy, data.get("migration_strategy"), MigrationStrategy.CANDIDATE_REVIEW),
            main_path_status=_coerce_enum(MainPathStatus, data.get("main_path_status"), MainPathStatus.PLANNED),
            lifecycle=_coerce_enum(LedgerLifecycle, data.get("lifecycle"), LedgerLifecycle.CANDIDATE),
            runtime_entry=_runtime_entry_from_json(_as_dict(data.get("runtime_entry"))),
            test_entries=[_test_entry_from_json(item) for item in _as_list(data.get("test_entries"))],
            main_path=_main_path_from_json(_as_dict(data.get("main_path"))),
            line_count_policy=_coerce_enum(LineCountPolicy, data.get("line_count_policy"), LineCountPolicy.EXCLUDED_INVENTORY_ONLY),
            license_notice=_license_notice_from_json(_as_dict(license_data), source_repo=source_repo) if license_data is not None else LicenseNotice(source_repo=source_repo),
            owner_unit=str(data.get("owner_unit") or ""),
            milestone=str(data.get("milestone") or ""),
            downstream_units=[str(item) for item in _as_list(data.get("downstream_units"))],
            dependencies=[str(item) for item in _as_list(data.get("dependencies"))],
            source_evidence=[_source_evidence_from_json(item) for item in _as_list(data.get("source_evidence"))],
            tags=[str(item) for item in _as_list(data.get("tags"))],
            blockers=[str(item) for item in _as_list(data.get("blockers"))],
            risk_notes=[str(item) for item in _as_list(data.get("risk_notes"))],
            replacement_plan=str(data.get("replacement_plan") or ""),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            metadata=dict(_as_dict(data.get("metadata"))),
        )


@dataclass(slots=True)
class LedgerQuery:
    source_repo: str = ""
    owner_unit: str = ""
    milestone: str = ""
    lifecycle: LedgerLifecycle | None = None
    main_path_status: MainPathStatus | None = None
    migration_strategy: MigrationStrategy | None = None
    target_contains: str = ""
    capability_contains: str = ""
    tag: str = ""
    limit: int = 200

    def matches(self, entry: InternalizationLedgerEntry) -> bool:
        checks = [
            not self.source_repo or entry.source_repo == self.source_repo,
            not self.owner_unit or entry.owner_unit == self.owner_unit,
            not self.milestone or entry.milestone == self.milestone,
            self.lifecycle is None or entry.lifecycle == self.lifecycle,
            self.main_path_status is None or entry.main_path_status == self.main_path_status,
            self.migration_strategy is None or entry.migration_strategy == self.migration_strategy,
            not self.target_contains or any(self.target_contains in path for path in entry.target_paths),
            not self.capability_contains or self.capability_contains.lower() in f"{entry.capability_name} {entry.capability_summary}".lower(),
            not self.tag or self.tag in entry.tags,
        ]
        return all(checks)


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: to_jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [to_jsonable(item) for item in value]
    return value


EnumT = TypeVar("EnumT", bound=StrEnum)


def _coerce_enum(enum_type: type[EnumT], value: Any, default: EnumT | None = None) -> EnumT:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise


def _target_binding_from_json(value: Any) -> TargetBinding:
    data = _as_dict(value)
    statuses = [
        _coerce_enum(MainPathStatus, item)
        for item in _as_list(data.get("must_exist_for_statuses"))
    ]
    return TargetBinding(
        target_path=str(data.get("target_path") or data.get("path") or ""),
        role=str(data.get("role") or "primary"),
        required_for_main_path=bool(data.get("required_for_main_path", True)),
        must_exist_for_statuses=statuses or TargetBinding("").must_exist_for_statuses,
    )


def _runtime_entry_from_json(data: dict[str, Any]) -> RuntimeEntry:
    return RuntimeEntry(
        command=str(data.get("command") or ""),
        module=str(data.get("module") or ""),
        function=str(data.get("function") or ""),
        protocol=str(data.get("protocol") or ""),
        health_check=str(data.get("health_check") or ""),
        config_refs=[str(item) for item in _as_list(data.get("config_refs"))],
        environment_refs=[str(item) for item in _as_list(data.get("environment_refs"))],
    )


def _test_entry_from_json(value: Any) -> TestEntry:
    data = _as_dict(value)
    return TestEntry(
        path=str(data.get("path") or ""),
        command=str(data.get("command") or ""),
        kind=str(data.get("kind") or "unit"),
        expected_signal=str(data.get("expected_signal") or ""),
        required=bool(data.get("required", True)),
    )


def _main_path_from_json(data: dict[str, Any]) -> MainPathBinding:
    return MainPathBinding(
        surfaces=[str(item) for item in _as_list(data.get("surfaces"))],
        event_types=[str(item) for item in _as_list(data.get("event_types"))],
        api_routes=[str(item) for item in _as_list(data.get("api_routes"))],
        control_commands=[str(item) for item in _as_list(data.get("control_commands"))],
        artifact_kinds=[str(item) for item in _as_list(data.get("artifact_kinds"))],
        worker_runtime=str(data.get("worker_runtime") or ""),
        ui_panels=[str(item) for item in _as_list(data.get("ui_panels"))],
    )


def _license_notice_from_json(data: dict[str, Any], *, source_repo: str) -> LicenseNotice:
    return LicenseNotice(
        source_repo=str(data.get("source_repo") or source_repo),
        status=_coerce_enum(NoticeStatus, data.get("status"), NoticeStatus.PENDING),
        license_hint=str(data.get("license_hint") or ""),
        notice_path=str(data.get("notice_path") or "third_party/NOTICE.md"),
        source_url=str(data.get("source_url") or ""),
        notes=str(data.get("notes") or ""),
    )


def _source_evidence_from_json(value: Any) -> SourceEvidence:
    data = _as_dict(value)
    return SourceEvidence(
        source_repo=str(data.get("source_repo") or ""),
        source_path=str(data.get("source_path") or ""),
        exists_in_workspace=bool(data.get("exists_in_workspace", False)),
        source_kind=str(data.get("source_kind") or "file"),
        reason=str(data.get("reason") or ""),
        symbols=[str(item) for item in _as_list(data.get("symbols"))],
        tags=[str(item) for item in _as_list(data.get("tags"))],
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _slug(value: str) -> str:
    chars = []
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
        elif chars and chars[-1] != "_":
            chars.append("_")
    return "".join(chars).strip("_") or "source"


def _has_forbidden_parent_reference(value: str) -> bool:
    normalized = value.replace("\\", "/").lower()
    if "../" in normalized:
        return True
    if normalized.startswith("g:/agent-zoo/") or normalized.startswith("g://agent-zoo/"):
        return True
    return False


def entry_to_json(entry: InternalizationLedgerEntry) -> str:
    return json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True)
