from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable

from .ledger_models import to_jsonable


class ContractSurface(StrEnum):
    API = "api"
    CLI = "cli"
    EVENT = "event"
    SCRIPT = "script"


class ContractRequirement(StrEnum):
    REQUIRED = "required"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"


@dataclass(slots=True)
class LedgerApiContract:
    method: str
    path: str
    alias_paths: list[str]
    requirement: ContractRequirement
    purpose: str
    query_params: list[str] = field(default_factory=list)
    body_fields: list[str] = field(default_factory=list)
    response_fields: list[str] = field(default_factory=list)
    event_payload: str = ""
    owner: str = "M1-01A"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerCliContract:
    command: str
    requirement: ContractRequirement
    purpose: str
    required_args: list[str] = field(default_factory=list)
    optional_args: list[str] = field(default_factory=list)
    output_fields: list[str] = field(default_factory=list)
    writes_event: bool = False
    mutates_ledger: bool = False
    owner: str = "M1-01A"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerEventContract:
    event_type: str
    payload_key: str
    requirement: ContractRequirement
    purpose: str
    required_payload_fields: list[str] = field(default_factory=list)
    producer_surfaces: list[str] = field(default_factory=list)
    owner: str = "M1-01A"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerScriptContract:
    script: str
    requirement: ContractRequirement
    purpose: str
    required_args: list[str] = field(default_factory=list)
    optional_args: list[str] = field(default_factory=list)
    output_fields: list[str] = field(default_factory=list)
    failure_conditions: list[str] = field(default_factory=list)
    owner: str = "M1-01A"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ContractFinding:
    surface: ContractSurface
    name: str
    message: str
    requirement: ContractRequirement
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerContractReport:
    ok: bool
    api_contracts: list[LedgerApiContract]
    cli_contracts: list[LedgerCliContract]
    event_contracts: list[LedgerEventContract]
    script_contracts: list[LedgerScriptContract]
    findings: list[ContractFinding] = field(default_factory=list)

    @property
    def required_surface_count(self) -> int:
        return sum(1 for contract in self.all_contracts() if contract.requirement == ContractRequirement.REQUIRED)

    @property
    def finding_count(self) -> int:
        return len(self.findings)

    def all_contracts(self) -> list[Any]:
        return [*self.api_contracts, *self.cli_contracts, *self.event_contracts, *self.script_contracts]

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["required_surface_count"] = self.required_surface_count
        payload["finding_count"] = self.finding_count
        return payload


def ledger_api_contracts() -> list[LedgerApiContract]:
    return [
        LedgerApiContract(
            method="GET",
            path="/ledger",
            alias_paths=["/integrations/ledger"],
            requirement=ContractRequirement.REQUIRED,
            purpose="List and query ledger entries with summary and optional advanced selector facets.",
            query_params=[
                "source_repo",
                "owner_unit",
                "milestone",
                "lifecycle",
                "status",
                "strategy",
                "target",
                "q",
                "tag",
                "limit",
                "target_verdict",
                "runtime_module",
                "api_route",
                "license_status",
            ],
            response_fields=["ledger_path", "summary", "entries"],
        ),
        LedgerApiContract(
            method="GET",
            path="/ledger/{ledger_id}",
            alias_paths=["/integrations/ledger/{ledger_id}"],
            requirement=ContractRequirement.REQUIRED,
            purpose="Show one source-to-target ledger entry.",
            response_fields=["entry"],
        ),
        LedgerApiContract(
            method="GET",
            path="/ledger/audit",
            alias_paths=["/integrations/ledger/audit"],
            requirement=ContractRequirement.REQUIRED,
            purpose="Run strict or non-strict ledger audit and filter findings.",
            query_params=["strict", "severity", "source_repo", "owner_unit", "code"],
            response_fields=["ok", "disposition", "finding_count", "error_count", "blocker_count", "warning_count", "findings"],
        ),
        LedgerApiContract(
            method="POST",
            path="/ledger/audit",
            alias_paths=["/integrations/ledger/audit"],
            requirement=ContractRequirement.REQUIRED,
            purpose="Run audit and optionally write an event-log record.",
            body_fields=["strict", "write_event"],
            response_fields=["audit", "event", "ledger_path"],
            event_payload="integration_ledger_audit",
        ),
        LedgerApiContract(
            method="GET",
            path="/ledger/readiness",
            alias_paths=["/integrations/ledger/readiness"],
            requirement=ContractRequirement.REQUIRED,
            purpose="Return execution-unit readiness with audit and line-count context.",
            query_params=["unit", "owner_unit", "base", "cached", "minimum_effective_lines"],
            response_fields=["owner_unit", "total_entries", "ready_for_advance", "blocked_entries", "line_count_ok", "debt"],
        ),
        LedgerApiContract(
            method="GET",
            path="/ledger/report",
            alias_paths=["/integrations/ledger/report"],
            requirement=ContractRequirement.REQUIRED,
            purpose="Return full coverage, audit, debt, readiness, matrix, health, and optional line-count report.",
            query_params=["unit", "owner_unit", "base", "cached", "minimum_effective_lines"],
            response_fields=["summary", "coverage", "unit_matrix", "audit", "debt", "readiness", "health", "line_count"],
        ),
        LedgerApiContract(
            method="GET",
            path="/ledger/accounting",
            alias_paths=["/integrations/ledger/accounting"],
            requirement=ContractRequirement.REQUIRED,
            purpose="Return source-repo, execution-unit, and target-path accounting for internalization progress.",
            query_params=["unit", "owner_unit", "no_entries"],
            response_fields=["summary", "source_accounts", "unit_accounts", "target_accounts", "findings", "entry_accounts"],
        ),
        LedgerApiContract(
            method="GET",
            path="/ledger/linecount",
            alias_paths=["/integrations/ledger/linecount"],
            requirement=ContractRequirement.REQUIRED,
            purpose="Classify git numstat into raw, excluded seed/inventory, and effective implementation lines.",
            query_params=["base", "head", "cached", "unit", "minimum_effective_lines"],
            response_fields=["raw_added", "excluded_added", "effective_added", "minimum_effective_lines", "ok", "seed_or_inventory_excluded"],
        ),
        LedgerApiContract(
            method="POST",
            path="/ledger/{ledger_id}/advance",
            alias_paths=["/integrations/ledger/{ledger_id}/advance"],
            requirement=ContractRequirement.REQUIRED,
            purpose="Advance one ledger entry through guarded lifecycle/status policy.",
            body_fields=["lifecycle", "main_path_status", "reason", "note", "effective_lines", "actor", "force", "write_event"],
            response_fields=["ok", "request", "policy", "mutation", "before", "after", "audit", "event_path"],
            event_payload="integration_ledger_update",
        ),
        LedgerApiContract(
            method="POST",
            path="/ledger/entries",
            alias_paths=["/integrations/ledger/entries"],
            requirement=ContractRequirement.REQUIRED,
            purpose="Upsert one full ledger entry payload and write mutation event.",
            body_fields=["entry", "write_event"],
            response_fields=["entry", "mutation", "event", "ledger_path"],
            event_payload="integration_ledger_update",
        ),
        LedgerApiContract(
            method="POST",
            path="/ledger/snapshots",
            alias_paths=["/integrations/ledger/snapshots"],
            requirement=ContractRequirement.RECOMMENDED,
            purpose="Create an audit/readiness/line-count snapshot.",
            body_fields=["label", "owner_unit", "base", "no_entries"],
            response_fields=["snapshot", "path"],
        ),
        LedgerApiContract(
            method="GET",
            path="/ledger/snapshots",
            alias_paths=["/integrations/ledger/snapshots"],
            requirement=ContractRequirement.RECOMMENDED,
            purpose="List stored ledger snapshots.",
            response_fields=["snapshots"],
        ),
    ]


def ledger_cli_contracts() -> list[LedgerCliContract]:
    return [
        LedgerCliContract("seed", ContractRequirement.REQUIRED, "Write bundled seed ledger.", optional_args=["--merge", "--json"], output_fields=["ledger_path", "summary"], mutates_ledger=True),
        LedgerCliContract("list", ContractRequirement.REQUIRED, "List ledger entries.", optional_args=["--source-repo", "--owner-unit", "--status", "--limit", "--json"], output_fields=["summary", "entries"]),
        LedgerCliContract("show", ContractRequirement.REQUIRED, "Show one ledger entry.", required_args=["ledger_id"], optional_args=["--json"], output_fields=["ledger_id", "source_repo", "target_bindings"]),
        LedgerCliContract("audit", ContractRequirement.REQUIRED, "Run ledger audit.", optional_args=["--strict", "--write-event", "--event-log", "--fail-on-error", "--severity", "--source-repo", "--owner-unit", "--code", "--json"], output_fields=["ok", "findings"], writes_event=True),
        LedgerCliContract("export", ContractRequirement.RECOMMENDED, "Export filtered ledger.", optional_args=["--output", "--format", "--source-repo", "--owner-unit"], output_fields=["entries"]),
        LedgerCliContract("upsert", ContractRequirement.REQUIRED, "Upsert entry from JSON.", required_args=["entry_json"], optional_args=["--json"], output_fields=["mutation", "ledger_path"], mutates_ledger=True),
        LedgerCliContract("readiness", ContractRequirement.REQUIRED, "Show unit readiness.", optional_args=["--owner-unit", "--base", "--cached", "--json"], output_fields=["owner_unit", "total_entries", "debt"]),
        LedgerCliContract("report", ContractRequirement.REQUIRED, "Build full ledger report.", optional_args=["--owner-unit", "--base", "--cached", "--json"], output_fields=["coverage", "unit_matrix", "audit", "health"]),
        LedgerCliContract("accounting", ContractRequirement.REQUIRED, "Build source/unit/target accounting report.", optional_args=["--owner-unit", "--no-entries", "--fail-on-error", "--markdown", "--json"], output_fields=["summary", "source_accounts", "unit_accounts", "target_accounts", "findings"]),
        LedgerCliContract("matrix", ContractRequirement.REQUIRED, "Show execution-unit dependency matrix.", optional_args=["--json"], output_fields=["unit_count", "covered_units", "rows"]),
        LedgerCliContract("gate", ContractRequirement.REQUIRED, "Run completion gate.", required_args=["--owner-unit"], optional_args=["--base", "--cached", "--minimum-effective-lines", "--include-source-scan", "--fail-on-error", "--json"], output_fields=["ok", "findings", "readiness"]),
        LedgerCliContract("source-scan", ContractRequirement.RECOMMENDED, "Verify source evidence and forbidden parent dependencies.", optional_args=["--source-root", "--include-tests", "--json"], output_fields=["forbidden_hits", "source_verifications", "target_verifications"]),
        LedgerCliContract("linecount", ContractRequirement.REQUIRED, "Classify raw/effective/excluded line count.", required_args=["--base"], optional_args=["--head", "--cached", "--owner-unit", "--minimum-effective-lines", "--fail-on-shortfall", "--json"], output_fields=["raw_added", "excluded_added", "effective_added", "ok"]),
        LedgerCliContract("classify-path", ContractRequirement.RECOMMENDED, "Classify target paths by effective-count policy.", required_args=["paths"], optional_args=["--json"], output_fields=["paths"]),
        LedgerCliContract("snapshot", ContractRequirement.RECOMMENDED, "Create snapshot.", optional_args=["--label", "--owner-unit", "--base", "--output", "--no-entries", "--json"], output_fields=["snapshot", "path"]),
        LedgerCliContract("snapshots", ContractRequirement.RECOMMENDED, "List snapshots.", optional_args=["--json"], output_fields=["snapshots"]),
        LedgerCliContract("diff", ContractRequirement.RECOMMENDED, "Diff two snapshots.", required_args=["base_snapshot", "head_snapshot"], optional_args=["--json"], output_fields=["added_entries", "removed_entries", "changed_entries"]),
        LedgerCliContract("advance", ContractRequirement.REQUIRED, "Advance entry through guarded lifecycle.", required_args=["ledger_id", "--reason"], optional_args=["--lifecycle", "--status", "--effective-lines", "--force", "--no-event", "--json"], output_fields=["ok", "policy", "mutation"], writes_event=True, mutates_ledger=True),
    ]


def ledger_event_contracts() -> list[LedgerEventContract]:
    return [
        LedgerEventContract(
            event_type="system_notice",
            payload_key="integration_ledger_audit",
            requirement=ContractRequirement.REQUIRED,
            purpose="Persist audit result into the global event log.",
            required_payload_fields=["trigger", "ok", "disposition", "total_entries", "finding_count", "error_count", "blocker_count", "warning_count", "strict", "summary", "findings"],
            producer_surfaces=["POST /ledger/audit", "ledger CLI audit --write-event"],
        ),
        LedgerEventContract(
            event_type="system_notice",
            payload_key="integration_ledger_update",
            requirement=ContractRequirement.REQUIRED,
            purpose="Persist ledger mutation and guarded advance result.",
            required_payload_fields=["trigger", "action", "ledger_id", "before", "after"],
            producer_surfaces=["POST /ledger/entries", "POST /ledger/{id}/advance", "ledger CLI advance"],
        ),
    ]


def ledger_script_contracts() -> list[LedgerScriptContract]:
    return [
        LedgerScriptContract(
            script="scripts/verify_internalization_ledger.py",
            requirement=ContractRequirement.REQUIRED,
            purpose="Strict ledger audit and effective line-count gate.",
            required_args=["--base when checking line count"],
            optional_args=["--cached", "--json", "--unit", "--minimum-effective-lines", "--fail-on-shortfall", "--fail-on-warning"],
            output_fields=["audit", "line_count", "missing_source_repos", "ok"],
            failure_conditions=["audit error/blocker", "missing required source repo", "effective line-count shortfall when --fail-on-shortfall is set"],
        ),
        LedgerScriptContract(
            script="scripts/zyra_integration_ledger.py",
            requirement=ContractRequirement.REQUIRED,
            purpose="CLI wrapper for ledger package.",
            optional_args=["all package CLI subcommands"],
            output_fields=["subcommand payload"],
            failure_conditions=["subcommand validation failure"],
        ),
        LedgerScriptContract(
            script="scripts/build_integration_ledger_seed.py",
            requirement=ContractRequirement.RECOMMENDED,
            purpose="Regenerate source-to-target seed ledger from repository maps.",
            optional_args=["script-specific source map flags"],
            output_fields=["internalization_ledger_seed.json"],
            failure_conditions=["invalid source map or unsupported repository"],
        ),
    ]


def build_contract_report(*, implemented_cli_commands: Iterable[str] = (), implemented_api_paths: Iterable[str] = ()) -> LedgerContractReport:
    api_contracts = ledger_api_contracts()
    cli_contracts = ledger_cli_contracts()
    event_contracts = ledger_event_contracts()
    script_contracts = ledger_script_contracts()
    findings: list[ContractFinding] = []
    implemented_cli = set(implemented_cli_commands)
    implemented_api = set(implemented_api_paths)
    if implemented_cli:
        required_cli = {contract.command for contract in cli_contracts if contract.requirement == ContractRequirement.REQUIRED}
        for command in sorted(required_cli - implemented_cli):
            findings.append(
                ContractFinding(
                    surface=ContractSurface.CLI,
                    name=command,
                    message=f"Required CLI command is not implemented: {command}",
                    requirement=ContractRequirement.REQUIRED,
                    remediation="Add the command to ledger_cli.build_parser and tests.",
                )
            )
    if implemented_api:
        required_paths = {f"{contract.method} {contract.path}" for contract in api_contracts if contract.requirement == ContractRequirement.REQUIRED}
        for path in sorted(required_paths - implemented_api):
            findings.append(
                ContractFinding(
                    surface=ContractSurface.API,
                    name=path,
                    message=f"Required API route is not implemented: {path}",
                    requirement=ContractRequirement.REQUIRED,
                    remediation="Add route handling to apps/api/zyra_api/main.py and tests.",
                )
            )
    ok = not any(finding.requirement == ContractRequirement.REQUIRED for finding in findings)
    return LedgerContractReport(
        ok=ok,
        api_contracts=api_contracts,
        cli_contracts=cli_contracts,
        event_contracts=event_contracts,
        script_contracts=script_contracts,
        findings=findings,
    )


def contract_summary(report: LedgerContractReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "required_surface_count": report.required_surface_count,
        "finding_count": report.finding_count,
        "api_contract_count": len(report.api_contracts),
        "cli_contract_count": len(report.cli_contracts),
        "event_contract_count": len(report.event_contracts),
        "script_contract_count": len(report.script_contracts),
    }
