from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field, fields, is_dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Iterable, get_args, get_origin

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
    TargetBinding,
    TestEntry,
    stable_ledger_id,
    to_jsonable,
)
from .ledger_store import InternalizationLedger


class SchemaSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class SchemaCode(StrEnum):
    FIELD_PRESENT = "FIELD_PRESENT"
    FIELD_MISSING = "FIELD_MISSING"
    FIELD_TYPE_MISMATCH = "FIELD_TYPE_MISMATCH"
    ENUM_VALUE_INVALID = "ENUM_VALUE_INVALID"
    ENUM_COVERAGE_RECORDED = "ENUM_COVERAGE_RECORDED"
    PATH_NOT_PROJECT_RELATIVE = "PATH_NOT_PROJECT_RELATIVE"
    TARGET_PATH_MISSING = "TARGET_PATH_MISSING"
    TEST_PATH_MISSING = "TEST_PATH_MISSING"
    SOURCE_EVIDENCE_INCOMPLETE = "SOURCE_EVIDENCE_INCOMPLETE"
    RUNTIME_ENTRY_INCOMPLETE = "RUNTIME_ENTRY_INCOMPLETE"
    MAIN_PATH_BINDING_INCOMPLETE = "MAIN_PATH_BINDING_INCOMPLETE"
    LICENSE_NOTICE_INCOMPLETE = "LICENSE_NOTICE_INCOMPLETE"
    LEDGER_ID_UNSTABLE = "LEDGER_ID_UNSTABLE"
    ROUNDTRIP_FAILED = "ROUNDTRIP_FAILED"
    ROUNDTRIP_PASSED = "ROUNDTRIP_PASSED"
    JSON_SERIALIZATION_FAILED = "JSON_SERIALIZATION_FAILED"
    DUPLICATE_LEDGER_ID = "DUPLICATE_LEDGER_ID"
    DUPLICATE_SOURCE_CAPABILITY = "DUPLICATE_SOURCE_CAPABILITY"
    CONTRACT_FIELD_UNCOVERED = "CONTRACT_FIELD_UNCOVERED"
    CONTRACT_VERSION_RECORDED = "CONTRACT_VERSION_RECORDED"
    STATUS_LIFECYCLE_MISMATCH = "STATUS_LIFECYCLE_MISMATCH"
    STRATEGY_STATUS_MISMATCH = "STRATEGY_STATUS_MISMATCH"


class FieldRequirement(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    REQUIRED_WHEN_CONNECTED = "required_when_connected"
    REQUIRED_WHEN_MATERIALIZED = "required_when_materialized"
    REQUIRED_WHEN_PRODUCTIZED = "required_when_productized"


@dataclass(slots=True)
class SchemaFieldContract:
    owner: str
    name: str
    kind: str
    requirement: FieldRequirement
    description: str = ""
    enum_values: list[str] = dc_field(default_factory=list)
    nested_contract: str = ""
    list_item_kind: str = ""

    @property
    def contract_id(self) -> str:
        return f"{self.owner}.{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class SchemaFinding:
    code: SchemaCode
    severity: SchemaSeverity
    message: str
    ledger_id: str = ""
    source_repo: str = ""
    owner_unit: str = ""
    field: str = ""
    path: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {SchemaSeverity.ERROR, SchemaSeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class EntrySchemaProfile:
    ledger_id: str
    source_repo: str
    owner_unit: str
    lifecycle: str
    main_path_status: str
    migration_strategy: str
    required_field_count: int
    present_required_fields: int
    optional_field_count: int
    nested_object_count: int
    target_count: int
    test_count: int
    source_evidence_count: int
    roundtrip_ok: bool
    json_serializable: bool
    stable_id_matches: bool
    connected_status: bool
    materialized_lifecycle: bool
    productized_lifecycle: bool
    finding_count: int = 0

    @property
    def required_field_ratio(self) -> float:
        if not self.required_field_count:
            return 1.0
        return round(self.present_required_fields / self.required_field_count, 4)

    @property
    def ok(self) -> bool:
        return self.roundtrip_ok and self.json_serializable and self.stable_id_matches and self.required_field_ratio == 1.0

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["required_field_ratio"] = self.required_field_ratio
        payload["ok"] = self.ok
        return payload


@dataclass(slots=True)
class SchemaEnumCoverage:
    enum_name: str
    allowed_values: list[str]
    observed_values: dict[str, int]
    missing_values: list[str]
    unknown_values: list[str]

    @property
    def complete(self) -> bool:
        return not self.unknown_values

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerSchemaContractReport:
    ok: bool
    total_entries: int
    checked_entries: int
    contract_version: str
    field_contracts: list[SchemaFieldContract]
    enum_coverage: list[SchemaEnumCoverage]
    findings: list[SchemaFinding]
    profiles: list[EntrySchemaProfile]
    summary: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == SchemaSeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == SchemaSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == SchemaSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


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

PRODUCTIZED_LIFECYCLES = {
    LedgerLifecycle.INTERNALIZED,
    LedgerLifecycle.PRODUCTIZED,
}


def build_schema_contract_report(
    ledger: InternalizationLedger,
    *,
    owner_unit: str = "",
    include_entries: bool = False,
) -> LedgerSchemaContractReport:
    entries = ledger.by_owner_unit(owner_unit) if owner_unit else ledger.entries()
    field_contracts = ledger_field_contracts()
    findings: list[SchemaFinding] = []
    profiles: list[EntrySchemaProfile] = []
    seen_ids: dict[str, InternalizationLedgerEntry] = {}
    seen_source_capabilities: dict[tuple[str, str, str], InternalizationLedgerEntry] = {}
    for entry in entries:
        entry_findings = validate_entry_schema(entry, field_contracts)
        duplicate_id = seen_ids.get(entry.ledger_id)
        if duplicate_id is not None:
            entry_findings.append(
                SchemaFinding(
                    code=SchemaCode.DUPLICATE_LEDGER_ID,
                    severity=SchemaSeverity.BLOCKER,
                    message=f"Duplicate ledger id: {entry.ledger_id}",
                    ledger_id=entry.ledger_id,
                    source_repo=entry.source_repo,
                    owner_unit=entry.owner_unit,
                    remediation="Use stable_ledger_id or split records with distinct capability names.",
                )
            )
        seen_ids[entry.ledger_id] = entry
        source_key = (entry.source_repo, entry.source_path, entry.capability_name)
        duplicate_source = seen_source_capabilities.get(source_key)
        if duplicate_source is not None:
            entry_findings.append(
                SchemaFinding(
                    code=SchemaCode.DUPLICATE_SOURCE_CAPABILITY,
                    severity=SchemaSeverity.WARNING,
                    message="Duplicate source repository/path/capability tuple.",
                    ledger_id=entry.ledger_id,
                    source_repo=entry.source_repo,
                    owner_unit=entry.owner_unit,
                    remediation="Keep duplicates only when distinct target boundaries are explicit.",
                    metadata={"first_ledger_id": duplicate_source.ledger_id},
                )
            )
        seen_source_capabilities[source_key] = entry
        findings.extend(entry_findings)
        profiles.append(entry_schema_profile(entry, entry_findings, field_contracts))
    enum_coverage = build_enum_coverage(entries)
    findings.extend(enum_findings(enum_coverage))
    findings.extend(contract_meta_findings(field_contracts))
    ok = not any(finding.blocking for finding in findings)
    return LedgerSchemaContractReport(
        ok=ok,
        total_entries=len(ledger.entries()),
        checked_entries=len(entries),
        contract_version="m1-01a.schema-contract.v2",
        field_contracts=field_contracts,
        enum_coverage=enum_coverage,
        findings=findings,
        profiles=profiles if include_entries else [],
        summary=schema_summary(entries, findings, profiles, enum_coverage, owner_unit),
    )


def ledger_field_contracts() -> list[SchemaFieldContract]:
    contracts: list[SchemaFieldContract] = []
    contracts.extend(entry_contracts())
    contracts.extend(target_binding_contracts())
    contracts.extend(runtime_entry_contracts())
    contracts.extend(test_entry_contracts())
    contracts.extend(source_evidence_contracts())
    contracts.extend(main_path_binding_contracts())
    contracts.extend(license_notice_contracts())
    return contracts


def entry_contracts() -> list[SchemaFieldContract]:
    return [
        SchemaFieldContract("InternalizationLedgerEntry", "ledger_id", "str", FieldRequirement.REQUIRED, "Stable ledger id."),
        SchemaFieldContract("InternalizationLedgerEntry", "source_repo", "str", FieldRequirement.REQUIRED, "Source repository key."),
        SchemaFieldContract("InternalizationLedgerEntry", "source_path", "str", FieldRequirement.REQUIRED, "Repo-relative source path."),
        SchemaFieldContract("InternalizationLedgerEntry", "capability_name", "str", FieldRequirement.REQUIRED, "Human capability name."),
        SchemaFieldContract("InternalizationLedgerEntry", "capability_summary", "str", FieldRequirement.REQUIRED, "Capability summary."),
        SchemaFieldContract("InternalizationLedgerEntry", "target_bindings", "list", FieldRequirement.REQUIRED, "Target bindings.", nested_contract="TargetBinding", list_item_kind="TargetBinding"),
        SchemaFieldContract("InternalizationLedgerEntry", "migration_strategy", "enum", FieldRequirement.REQUIRED, "Migration strategy.", enum_values=enum_values(MigrationStrategy)),
        SchemaFieldContract("InternalizationLedgerEntry", "main_path_status", "enum", FieldRequirement.REQUIRED, "Main path status.", enum_values=enum_values(MainPathStatus)),
        SchemaFieldContract("InternalizationLedgerEntry", "lifecycle", "enum", FieldRequirement.REQUIRED, "Lifecycle state.", enum_values=enum_values(LedgerLifecycle)),
        SchemaFieldContract("InternalizationLedgerEntry", "runtime_entry", "object", FieldRequirement.OPTIONAL, "Runtime invocation contract.", nested_contract="RuntimeEntry"),
        SchemaFieldContract("InternalizationLedgerEntry", "test_entries", "list", FieldRequirement.REQUIRED_WHEN_CONNECTED, "Behavior tests.", nested_contract="TestEntry", list_item_kind="TestEntry"),
        SchemaFieldContract("InternalizationLedgerEntry", "main_path", "object", FieldRequirement.REQUIRED_WHEN_CONNECTED, "Main path binding.", nested_contract="MainPathBinding"),
        SchemaFieldContract("InternalizationLedgerEntry", "line_count_policy", "enum", FieldRequirement.REQUIRED, "Line count policy.", enum_values=enum_values(LineCountPolicy)),
        SchemaFieldContract("InternalizationLedgerEntry", "license_notice", "object", FieldRequirement.REQUIRED_WHEN_PRODUCTIZED, "License notice.", nested_contract="LicenseNotice"),
        SchemaFieldContract("InternalizationLedgerEntry", "owner_unit", "str", FieldRequirement.REQUIRED, "Execution unit owner."),
        SchemaFieldContract("InternalizationLedgerEntry", "milestone", "str", FieldRequirement.REQUIRED, "Milestone owner."),
        SchemaFieldContract("InternalizationLedgerEntry", "downstream_units", "list", FieldRequirement.OPTIONAL, "Downstream units."),
        SchemaFieldContract("InternalizationLedgerEntry", "dependencies", "list", FieldRequirement.OPTIONAL, "Upstream dependencies."),
        SchemaFieldContract("InternalizationLedgerEntry", "source_evidence", "list", FieldRequirement.OPTIONAL, "Additional source evidence.", nested_contract="SourceEvidence", list_item_kind="SourceEvidence"),
        SchemaFieldContract("InternalizationLedgerEntry", "tags", "list", FieldRequirement.OPTIONAL, "Tags."),
        SchemaFieldContract("InternalizationLedgerEntry", "blockers", "list", FieldRequirement.OPTIONAL, "Known blockers."),
        SchemaFieldContract("InternalizationLedgerEntry", "risk_notes", "list", FieldRequirement.OPTIONAL, "Risk notes."),
        SchemaFieldContract("InternalizationLedgerEntry", "replacement_plan", "str", FieldRequirement.OPTIONAL, "Replacement/deferred plan."),
        SchemaFieldContract("InternalizationLedgerEntry", "created_at", "str", FieldRequirement.REQUIRED, "Creation timestamp."),
        SchemaFieldContract("InternalizationLedgerEntry", "updated_at", "str", FieldRequirement.REQUIRED, "Update timestamp."),
        SchemaFieldContract("InternalizationLedgerEntry", "metadata", "dict", FieldRequirement.OPTIONAL, "Metadata."),
    ]


def target_binding_contracts() -> list[SchemaFieldContract]:
    return [
        SchemaFieldContract("TargetBinding", "target_path", "str", FieldRequirement.REQUIRED, "Project-relative target path."),
        SchemaFieldContract("TargetBinding", "role", "str", FieldRequirement.REQUIRED, "Target role."),
        SchemaFieldContract("TargetBinding", "required_for_main_path", "bool", FieldRequirement.REQUIRED, "Whether target is needed for main path."),
        SchemaFieldContract("TargetBinding", "must_exist_for_statuses", "list", FieldRequirement.REQUIRED, "Statuses requiring materialized target.", enum_values=enum_values(MainPathStatus)),
    ]


def runtime_entry_contracts() -> list[SchemaFieldContract]:
    return [
        SchemaFieldContract("RuntimeEntry", "command", "str", FieldRequirement.OPTIONAL, "Runtime command."),
        SchemaFieldContract("RuntimeEntry", "module", "str", FieldRequirement.OPTIONAL, "Importable module."),
        SchemaFieldContract("RuntimeEntry", "function", "str", FieldRequirement.OPTIONAL, "Callable function/class."),
        SchemaFieldContract("RuntimeEntry", "protocol", "str", FieldRequirement.OPTIONAL, "Invocation protocol."),
        SchemaFieldContract("RuntimeEntry", "health_check", "str", FieldRequirement.OPTIONAL, "Health check command or route."),
        SchemaFieldContract("RuntimeEntry", "config_refs", "list", FieldRequirement.OPTIONAL, "Config references."),
        SchemaFieldContract("RuntimeEntry", "environment_refs", "list", FieldRequirement.OPTIONAL, "Environment references."),
    ]


def test_entry_contracts() -> list[SchemaFieldContract]:
    return [
        SchemaFieldContract("TestEntry", "path", "str", FieldRequirement.REQUIRED, "Test path."),
        SchemaFieldContract("TestEntry", "command", "str", FieldRequirement.OPTIONAL, "Test command."),
        SchemaFieldContract("TestEntry", "kind", "str", FieldRequirement.REQUIRED, "Test kind."),
        SchemaFieldContract("TestEntry", "expected_signal", "str", FieldRequirement.OPTIONAL, "Expected behavior signal."),
        SchemaFieldContract("TestEntry", "required", "bool", FieldRequirement.REQUIRED, "Whether test is required."),
    ]


def source_evidence_contracts() -> list[SchemaFieldContract]:
    return [
        SchemaFieldContract("SourceEvidence", "source_repo", "str", FieldRequirement.REQUIRED, "Source repo."),
        SchemaFieldContract("SourceEvidence", "source_path", "str", FieldRequirement.REQUIRED, "Source path."),
        SchemaFieldContract("SourceEvidence", "exists_in_workspace", "bool", FieldRequirement.REQUIRED, "Source existence flag."),
        SchemaFieldContract("SourceEvidence", "source_kind", "str", FieldRequirement.REQUIRED, "Source kind."),
        SchemaFieldContract("SourceEvidence", "reason", "str", FieldRequirement.OPTIONAL, "Evidence reason."),
        SchemaFieldContract("SourceEvidence", "symbols", "list", FieldRequirement.OPTIONAL, "Symbols."),
        SchemaFieldContract("SourceEvidence", "tags", "list", FieldRequirement.OPTIONAL, "Tags."),
    ]


def main_path_binding_contracts() -> list[SchemaFieldContract]:
    return [
        SchemaFieldContract("MainPathBinding", "surfaces", "list", FieldRequirement.OPTIONAL, "Surface names."),
        SchemaFieldContract("MainPathBinding", "event_types", "list", FieldRequirement.OPTIONAL, "Event types."),
        SchemaFieldContract("MainPathBinding", "api_routes", "list", FieldRequirement.OPTIONAL, "API routes."),
        SchemaFieldContract("MainPathBinding", "control_commands", "list", FieldRequirement.OPTIONAL, "Control commands."),
        SchemaFieldContract("MainPathBinding", "artifact_kinds", "list", FieldRequirement.OPTIONAL, "Artifact kinds."),
        SchemaFieldContract("MainPathBinding", "worker_runtime", "str", FieldRequirement.OPTIONAL, "Worker runtime id."),
        SchemaFieldContract("MainPathBinding", "ui_panels", "list", FieldRequirement.OPTIONAL, "UI panels."),
    ]


def license_notice_contracts() -> list[SchemaFieldContract]:
    return [
        SchemaFieldContract("LicenseNotice", "source_repo", "str", FieldRequirement.REQUIRED, "Source repo."),
        SchemaFieldContract("LicenseNotice", "status", "enum", FieldRequirement.REQUIRED, "Notice status.", enum_values=enum_values(NoticeStatus)),
        SchemaFieldContract("LicenseNotice", "license_hint", "str", FieldRequirement.OPTIONAL, "License hint."),
        SchemaFieldContract("LicenseNotice", "notice_path", "str", FieldRequirement.REQUIRED, "Notice path."),
        SchemaFieldContract("LicenseNotice", "source_url", "str", FieldRequirement.OPTIONAL, "Source URL."),
        SchemaFieldContract("LicenseNotice", "notes", "str", FieldRequirement.OPTIONAL, "Notes."),
    ]


def validate_entry_schema(entry: InternalizationLedgerEntry, contracts: list[SchemaFieldContract] | None = None) -> list[SchemaFinding]:
    contracts = contracts or ledger_field_contracts()
    findings: list[SchemaFinding] = []
    entry_contract_map = {contract.name: contract for contract in contracts if contract.owner == "InternalizationLedgerEntry"}
    for name, contract in entry_contract_map.items():
        value = getattr(entry, name, None)
        if value_is_missing(value) and requirement_applies(contract.requirement, entry):
            findings.append(
                SchemaFinding(
                    code=SchemaCode.FIELD_MISSING,
                    severity=severity_for_requirement(contract.requirement),
                    message=f"Required ledger field is missing: {name}",
                    ledger_id=entry.ledger_id,
                    source_repo=entry.source_repo,
                    owner_unit=entry.owner_unit,
                    field=name,
                    remediation="Populate this field or downgrade lifecycle/status until evidence exists.",
                )
            )
            continue
        findings.extend(type_findings(entry, name, value, contract))
    findings.extend(path_findings(entry))
    findings.extend(stable_id_findings(entry))
    findings.extend(roundtrip_findings(entry))
    findings.extend(relationship_findings(entry))
    findings.extend(nested_object_findings(entry, contracts))
    return findings


def type_findings(
    entry: InternalizationLedgerEntry,
    field_name: str,
    value: Any,
    contract: SchemaFieldContract,
) -> list[SchemaFinding]:
    if value_is_missing(value):
        return []
    if contract.kind == "enum":
        raw = str(value)
        if raw not in contract.enum_values:
            return [
                SchemaFinding(
                    code=SchemaCode.ENUM_VALUE_INVALID,
                    severity=SchemaSeverity.ERROR,
                    message=f"Invalid enum value for {field_name}: {raw}",
                    ledger_id=entry.ledger_id,
                    source_repo=entry.source_repo,
                    owner_unit=entry.owner_unit,
                    field=field_name,
                    remediation="Use one of the declared enum values.",
                    metadata={"allowed": contract.enum_values},
                )
            ]
        return []
    if contract.kind == "str" and not isinstance(value, str):
        return [type_mismatch(entry, field_name, contract.kind, type(value).__name__)]
    if contract.kind == "list" and not isinstance(value, list):
        return [type_mismatch(entry, field_name, contract.kind, type(value).__name__)]
    if contract.kind == "dict" and not isinstance(value, dict):
        return [type_mismatch(entry, field_name, contract.kind, type(value).__name__)]
    if contract.kind == "bool" and not isinstance(value, bool):
        return [type_mismatch(entry, field_name, contract.kind, type(value).__name__)]
    if contract.kind == "object" and not (is_dataclass(value) or value is None):
        return [type_mismatch(entry, field_name, contract.kind, type(value).__name__)]
    return []


def nested_object_findings(entry: InternalizationLedgerEntry, contracts: list[SchemaFieldContract]) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = []
    findings.extend(validate_target_bindings(entry))
    findings.extend(validate_test_entries(entry))
    findings.extend(validate_source_evidence(entry))
    findings.extend(validate_runtime_entry(entry))
    findings.extend(validate_main_path_binding(entry))
    findings.extend(validate_license_notice(entry))
    return findings


def validate_target_bindings(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = []
    for index, binding in enumerate(entry.target_bindings):
        prefix = f"target_bindings[{index}]"
        if not binding.target_path.strip():
            findings.append(field_finding(entry, SchemaCode.TARGET_PATH_MISSING, SchemaSeverity.ERROR, prefix, "Target path is empty."))
        if not is_project_relative_path(binding.target_path):
            findings.append(field_finding(entry, SchemaCode.PATH_NOT_PROJECT_RELATIVE, SchemaSeverity.ERROR, prefix, "Target path must be project-relative.", path=binding.target_path))
        if not binding.role.strip():
            findings.append(field_finding(entry, SchemaCode.FIELD_MISSING, SchemaSeverity.WARNING, f"{prefix}.role", "Target role is empty."))
        for status in binding.must_exist_for_statuses:
            if str(status) not in enum_values(MainPathStatus):
                findings.append(field_finding(entry, SchemaCode.ENUM_VALUE_INVALID, SchemaSeverity.ERROR, f"{prefix}.must_exist_for_statuses", "Invalid required status.", metadata={"status": str(status)}))
    return findings


def validate_test_entries(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = []
    for index, test in enumerate(entry.test_entries):
        prefix = f"test_entries[{index}]"
        if test.required and not test.path.strip():
            findings.append(field_finding(entry, SchemaCode.TEST_PATH_MISSING, SchemaSeverity.ERROR, prefix, "Required test entry has no path."))
        if test.path and not is_project_relative_path(test.path):
            findings.append(field_finding(entry, SchemaCode.PATH_NOT_PROJECT_RELATIVE, SchemaSeverity.ERROR, prefix, "Test path must be project-relative.", path=test.path))
        if not test.kind.strip():
            findings.append(field_finding(entry, SchemaCode.FIELD_MISSING, SchemaSeverity.WARNING, f"{prefix}.kind", "Test kind is empty."))
    return findings


def validate_source_evidence(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = []
    for index, evidence in enumerate(entry.source_evidence):
        prefix = f"source_evidence[{index}]"
        if not evidence.source_repo.strip() or not evidence.source_path.strip():
            findings.append(field_finding(entry, SchemaCode.SOURCE_EVIDENCE_INCOMPLETE, SchemaSeverity.WARNING, prefix, "Source evidence lacks source_repo or source_path."))
        if evidence.source_path and not is_repo_relative_source_path(evidence.source_path):
            findings.append(field_finding(entry, SchemaCode.PATH_NOT_PROJECT_RELATIVE, SchemaSeverity.ERROR, prefix, "Source evidence path must be repo-relative.", path=evidence.source_path))
    return findings


def validate_runtime_entry(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    if entry.main_path_status in CONNECTED_STATUSES and entry.runtime_entry.is_empty():
        return [
            SchemaFinding(
                code=SchemaCode.RUNTIME_ENTRY_INCOMPLETE,
                severity=SchemaSeverity.ERROR,
                message="Connected entry has no runtime entry.",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                field="runtime_entry",
                remediation="Bind connected entries to a Zyra runtime module, command, protocol, or health check.",
            )
        ]
    return []


def validate_main_path_binding(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    if entry.main_path_status in CONNECTED_STATUSES and entry.main_path.is_empty():
        return [
            SchemaFinding(
                code=SchemaCode.MAIN_PATH_BINDING_INCOMPLETE,
                severity=SchemaSeverity.ERROR,
                message="Connected entry has no main_path binding.",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                field="main_path",
                remediation="Record API route, event type, control command, worker runtime, artifact, or UI panel.",
            )
        ]
    return []


def validate_license_notice(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    if entry.lifecycle in PRODUCTIZED_LIFECYCLES and entry.license_notice is None:
        return [
            SchemaFinding(
                code=SchemaCode.LICENSE_NOTICE_INCOMPLETE,
                severity=SchemaSeverity.WARNING,
                message="Productized/internalized entry has no license notice record.",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                field="license_notice",
                remediation="Record notice status or explain why no notice is required.",
            )
        ]
    if entry.license_notice is not None and not entry.license_notice.source_repo.strip():
        return [field_finding(entry, SchemaCode.LICENSE_NOTICE_INCOMPLETE, SchemaSeverity.WARNING, "license_notice.source_repo", "License notice source_repo is empty.")]
    return []


def path_findings(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = []
    if entry.source_path and not is_repo_relative_source_path(entry.source_path):
        findings.append(field_finding(entry, SchemaCode.PATH_NOT_PROJECT_RELATIVE, SchemaSeverity.ERROR, "source_path", "Source path must be repo-relative.", path=entry.source_path))
    for path in entry.runtime_entry.config_refs + entry.runtime_entry.environment_refs:
        if path and path.startswith("/") or looks_windows_absolute(path):
            findings.append(field_finding(entry, SchemaCode.PATH_NOT_PROJECT_RELATIVE, SchemaSeverity.WARNING, "runtime_entry", "Runtime reference should not be absolute.", path=path))
    return findings


def stable_id_findings(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    expected = stable_ledger_id(entry.source_repo, entry.source_path, entry.capability_name)
    if entry.ledger_id != expected:
        return [
            SchemaFinding(
                code=SchemaCode.LEDGER_ID_UNSTABLE,
                severity=SchemaSeverity.WARNING,
                message="Ledger id does not match current stable_ledger_id convention.",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                field="ledger_id",
                remediation="Regenerate id only if no persisted references depend on the old id.",
                metadata={"expected": expected},
            )
        ]
    return []


def roundtrip_findings(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = []
    try:
        raw = entry.to_dict()
        json.dumps(raw, ensure_ascii=False, sort_keys=True)
    except Exception as error:
        findings.append(
            SchemaFinding(
                code=SchemaCode.JSON_SERIALIZATION_FAILED,
                severity=SchemaSeverity.BLOCKER,
                message=f"Ledger entry is not JSON-serializable: {error}",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                remediation="Remove non-JSON metadata or convert it to structured values.",
            )
        )
        return findings
    try:
        restored = InternalizationLedgerEntry.from_dict(raw)
        restored_raw = restored.to_dict()
    except Exception as error:
        findings.append(
            SchemaFinding(
                code=SchemaCode.ROUNDTRIP_FAILED,
                severity=SchemaSeverity.BLOCKER,
                message=f"Ledger entry does not roundtrip through from_dict: {error}",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                remediation="Fix schema conversion before persisting or advancing this entry.",
            )
        )
        return findings
    if normalize_for_compare(raw) != normalize_for_compare(restored_raw):
        findings.append(
            SchemaFinding(
                code=SchemaCode.ROUNDTRIP_FAILED,
                severity=SchemaSeverity.ERROR,
                message="Ledger entry JSON payload changes after from_dict/to_dict roundtrip.",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                remediation="Keep aliases and enum serialization stable.",
            )
        )
    return findings


def relationship_findings(entry: InternalizationLedgerEntry) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = []
    if entry.main_path_status in CONNECTED_STATUSES and entry.lifecycle not in MATERIALIZED_LIFECYCLES:
        findings.append(
            SchemaFinding(
                code=SchemaCode.STATUS_LIFECYCLE_MISMATCH,
                severity=SchemaSeverity.ERROR,
                message="Connected main_path_status requires materialized lifecycle.",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                remediation="Advance lifecycle or downgrade main_path_status.",
                metadata={"status": str(entry.main_path_status), "lifecycle": str(entry.lifecycle)},
            )
        )
    if entry.migration_strategy == MigrationStrategy.VENDORED_RUNTIME and entry.main_path_status in CONNECTED_STATUSES:
        findings.append(
            SchemaFinding(
                code=SchemaCode.STRATEGY_STATUS_MISMATCH,
                severity=SchemaSeverity.WARNING,
                message="Vendored runtime entry is marked connected; verify Zyra-owned adapter boundary.",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                remediation="Use adapter/direct_port when Zyra owns behavior, or keep vendored status as source pool.",
            )
        )
    if entry.lifecycle in PRODUCTIZED_LIFECYCLES and not entry.test_entries:
        findings.append(
            SchemaFinding(
                code=SchemaCode.TEST_PATH_MISSING,
                severity=SchemaSeverity.ERROR,
                message="Productized/internalized entry has no test binding.",
                ledger_id=entry.ledger_id,
                source_repo=entry.source_repo,
                owner_unit=entry.owner_unit,
                field="test_entries",
                remediation="Bind a behavior test or lower lifecycle.",
            )
        )
    return findings


def entry_schema_profile(
    entry: InternalizationLedgerEntry,
    findings: list[SchemaFinding],
    contracts: list[SchemaFieldContract],
) -> EntrySchemaProfile:
    required = [contract for contract in contracts if contract.owner == "InternalizationLedgerEntry" and requirement_applies(contract.requirement, entry)]
    present = sum(1 for contract in required if not value_is_missing(getattr(entry, contract.name, None)))
    optional = [contract for contract in contracts if contract.owner == "InternalizationLedgerEntry" and contract.requirement == FieldRequirement.OPTIONAL]
    return EntrySchemaProfile(
        ledger_id=entry.ledger_id,
        source_repo=entry.source_repo,
        owner_unit=entry.owner_unit,
        lifecycle=str(entry.lifecycle),
        main_path_status=str(entry.main_path_status),
        migration_strategy=str(entry.migration_strategy),
        required_field_count=len(required),
        present_required_fields=present,
        optional_field_count=len(optional),
        nested_object_count=sum(1 for item in [entry.runtime_entry, entry.main_path, entry.license_notice] if item is not None),
        target_count=len(entry.target_bindings),
        test_count=len(entry.test_entries),
        source_evidence_count=len(entry.source_evidence),
        roundtrip_ok=not any(finding.code == SchemaCode.ROUNDTRIP_FAILED for finding in findings),
        json_serializable=not any(finding.code == SchemaCode.JSON_SERIALIZATION_FAILED for finding in findings),
        stable_id_matches=not any(finding.code == SchemaCode.LEDGER_ID_UNSTABLE for finding in findings),
        connected_status=entry.main_path_status in CONNECTED_STATUSES,
        materialized_lifecycle=entry.lifecycle in MATERIALIZED_LIFECYCLES,
        productized_lifecycle=entry.lifecycle in PRODUCTIZED_LIFECYCLES,
        finding_count=len(findings),
    )


def build_enum_coverage(entries: Iterable[InternalizationLedgerEntry]) -> list[SchemaEnumCoverage]:
    items = list(entries)
    return [
        enum_coverage("MigrationStrategy", enum_values(MigrationStrategy), [str(entry.migration_strategy) for entry in items]),
        enum_coverage("MainPathStatus", enum_values(MainPathStatus), [str(entry.main_path_status) for entry in items]),
        enum_coverage("LedgerLifecycle", enum_values(LedgerLifecycle), [str(entry.lifecycle) for entry in items]),
        enum_coverage("LineCountPolicy", enum_values(LineCountPolicy), [str(entry.line_count_policy) for entry in items]),
        enum_coverage(
            "NoticeStatus",
            enum_values(NoticeStatus),
            [str(entry.license_notice.status) for entry in items if entry.license_notice is not None],
        ),
    ]


def enum_coverage(enum_name: str, allowed_values: list[str], observed_values: Iterable[str]) -> SchemaEnumCoverage:
    counts: dict[str, int] = {}
    unknown: dict[str, int] = {}
    for value in observed_values:
        raw = str(value)
        if raw in allowed_values:
            counts[raw] = counts.get(raw, 0) + 1
        else:
            unknown[raw] = unknown.get(raw, 0) + 1
    missing = [value for value in allowed_values if value not in counts]
    return SchemaEnumCoverage(
        enum_name=enum_name,
        allowed_values=allowed_values,
        observed_values=dict(sorted(counts.items())),
        missing_values=missing,
        unknown_values=sorted(unknown),
    )


def enum_findings(coverage: Iterable[SchemaEnumCoverage]) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = []
    for item in coverage:
        if item.unknown_values:
            findings.append(
                SchemaFinding(
                    code=SchemaCode.ENUM_VALUE_INVALID,
                    severity=SchemaSeverity.ERROR,
                    message=f"{item.enum_name} has unknown observed values.",
                    field=item.enum_name,
                    remediation="Normalize values to enum definitions before persistence.",
                    metadata=item.to_dict(),
                )
            )
        else:
            findings.append(
                SchemaFinding(
                    code=SchemaCode.ENUM_COVERAGE_RECORDED,
                    severity=SchemaSeverity.INFO,
                    message=f"{item.enum_name} coverage recorded.",
                    field=item.enum_name,
                    metadata=item.to_dict(),
                )
            )
    return findings


def contract_meta_findings(contracts: Iterable[SchemaFieldContract]) -> list[SchemaFinding]:
    findings: list[SchemaFinding] = [
        SchemaFinding(
            code=SchemaCode.CONTRACT_VERSION_RECORDED,
            severity=SchemaSeverity.INFO,
            message="Ledger schema contract version m1-01a.schema-contract.v2 is active.",
            metadata={"field_contract_count": len(list(contracts))},
        )
    ]
    declared_owners = {contract.owner for contract in contracts}
    dataclass_owners = {"InternalizationLedgerEntry", "TargetBinding", "RuntimeEntry", "TestEntry", "SourceEvidence", "MainPathBinding", "LicenseNotice"}
    for owner in sorted(dataclass_owners - declared_owners):
        findings.append(
            SchemaFinding(
                code=SchemaCode.CONTRACT_FIELD_UNCOVERED,
                severity=SchemaSeverity.ERROR,
                message=f"No schema contract declared for {owner}.",
                field=owner,
                remediation="Add contract fields for this nested ledger object.",
            )
        )
    return findings


def schema_summary(
    entries: list[InternalizationLedgerEntry],
    findings: list[SchemaFinding],
    profiles: list[EntrySchemaProfile],
    enum_coverage: list[SchemaEnumCoverage],
    owner_unit: str,
) -> dict[str, Any]:
    by_code: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    for finding in findings:
        by_code[str(finding.code)] = by_code.get(str(finding.code), 0) + 1
        by_severity[str(finding.severity)] = by_severity.get(str(finding.severity), 0) + 1
        if finding.source_repo:
            by_source[finding.source_repo] = by_source.get(finding.source_repo, 0) + 1
        if finding.owner_unit:
            by_owner[finding.owner_unit] = by_owner.get(finding.owner_unit, 0) + 1
    return {
        "owner_unit": owner_unit or "all",
        "entries": len(entries),
        "connected_entries": sum(1 for entry in entries if entry.main_path_status in CONNECTED_STATUSES),
        "materialized_entries": sum(1 for entry in entries if entry.lifecycle in MATERIALIZED_LIFECYCLES),
        "productized_entries": sum(1 for entry in entries if entry.lifecycle in PRODUCTIZED_LIFECYCLES),
        "profiles_ok": sum(1 for profile in profiles if profile.ok),
        "profile_count": len(profiles),
        "findings_by_code": dict(sorted(by_code.items())),
        "findings_by_severity": dict(sorted(by_severity.items())),
        "findings_by_source": dict(sorted(by_source.items())),
        "findings_by_owner": dict(sorted(by_owner.items())),
        "enum_names": [item.enum_name for item in enum_coverage],
        "unknown_enum_values": sum(len(item.unknown_values) for item in enum_coverage),
    }


def schema_contract_payload(report: LedgerSchemaContractReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {SchemaSeverity.ERROR, SchemaSeverity.BLOCKER}
    ]
    payload["warning_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity == SchemaSeverity.WARNING
    ]
    return payload


def assert_schema_contract(report: LedgerSchemaContractReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.ledger_id or finding.field}: {finding.message}"
        for finding in report.findings
        if finding.blocking
    )
    raise AssertionError(f"Ledger schema contract failed:\n{formatted}")


def type_mismatch(entry: InternalizationLedgerEntry, field_name: str, expected: str, actual: str) -> SchemaFinding:
    return SchemaFinding(
        code=SchemaCode.FIELD_TYPE_MISMATCH,
        severity=SchemaSeverity.ERROR,
        message=f"Field {field_name} expected {expected}, got {actual}.",
        ledger_id=entry.ledger_id,
        source_repo=entry.source_repo,
        owner_unit=entry.owner_unit,
        field=field_name,
        remediation="Normalize input payload before persistence.",
        metadata={"expected": expected, "actual": actual},
    )


def field_finding(
    entry: InternalizationLedgerEntry,
    code: SchemaCode,
    severity: SchemaSeverity,
    field_name: str,
    message: str,
    *,
    path: str = "",
    metadata: dict[str, Any] | None = None,
) -> SchemaFinding:
    return SchemaFinding(
        code=code,
        severity=severity,
        message=message,
        ledger_id=entry.ledger_id,
        source_repo=entry.source_repo,
        owner_unit=entry.owner_unit,
        field=field_name,
        path=path,
        remediation="Fix the ledger entry before marking this source item connected or productized.",
        metadata=metadata or {},
    )


def value_is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def requirement_applies(requirement: FieldRequirement, entry: InternalizationLedgerEntry) -> bool:
    if requirement == FieldRequirement.REQUIRED:
        return True
    if requirement == FieldRequirement.OPTIONAL:
        return False
    if requirement == FieldRequirement.REQUIRED_WHEN_CONNECTED:
        return entry.main_path_status in CONNECTED_STATUSES
    if requirement == FieldRequirement.REQUIRED_WHEN_MATERIALIZED:
        return entry.lifecycle in MATERIALIZED_LIFECYCLES
    if requirement == FieldRequirement.REQUIRED_WHEN_PRODUCTIZED:
        return entry.lifecycle in PRODUCTIZED_LIFECYCLES
    return False


def severity_for_requirement(requirement: FieldRequirement) -> SchemaSeverity:
    if requirement == FieldRequirement.REQUIRED:
        return SchemaSeverity.ERROR
    if requirement == FieldRequirement.REQUIRED_WHEN_CONNECTED:
        return SchemaSeverity.ERROR
    if requirement == FieldRequirement.REQUIRED_WHEN_MATERIALIZED:
        return SchemaSeverity.WARNING
    if requirement == FieldRequirement.REQUIRED_WHEN_PRODUCTIZED:
        return SchemaSeverity.WARNING
    return SchemaSeverity.INFO


def is_project_relative_path(path: str) -> bool:
    if not path:
        return True
    normalized = path.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if normalized.startswith("/"):
        return False
    if parts and ":" in parts[0]:
        return False
    if any(part == ".." for part in parts):
        return False
    return True


def is_repo_relative_source_path(path: str) -> bool:
    return is_project_relative_path(path)


def looks_windows_absolute(path: str) -> bool:
    return len(path) >= 2 and path[1] == ":"


def enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [str(item) for item in enum_type]


def normalize_for_compare(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def dataclass_field_summary(cls: type[Any]) -> dict[str, dict[str, str]]:
    if not is_dataclass(cls):
        return {}
    summary: dict[str, dict[str, str]] = {}
    for item in fields(cls):
        annotation = item.type
        summary[item.name] = {
            "annotation": annotation_name(annotation),
            "default": default_name(item.default),
            "default_factory": getattr(item.default_factory, "__name__", ""),
        }
    return summary


def annotation_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is not None:
        args = ", ".join(annotation_name(arg) for arg in get_args(annotation))
        return f"{getattr(origin, '__name__', str(origin))}[{args}]"
    return getattr(annotation, "__name__", str(annotation))


def default_name(value: Any) -> str:
    text = repr(value)
    if "dataclasses._MISSING_TYPE" in text:
        return ""
    return text
