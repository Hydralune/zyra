from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger_audit import InternalizationLedgerAuditor
from .ledger_linecount import EffectiveLineCountReport
from .ledger_matrix import downstream_closure, entries_for_unit_and_dependencies, upstream_closure
from .ledger_models import InternalizationLedgerEntry, to_jsonable
from .ledger_remediation import RemediationPlan, build_remediation_plan
from .ledger_reports import UnitReadinessReport, build_unit_readiness_report
from .ledger_store import InternalizationLedger


@dataclass(slots=True)
class HandoffEntry:
    ledger_id: str
    source_repo: str
    source_path: str
    capability_name: str
    owner_unit: str
    lifecycle: str
    main_path_status: str
    primary_target: str
    target_paths: list[str]
    runtime_module: str = ""
    runtime_command: str = ""
    test_commands: list[str] = field(default_factory=list)
    api_routes: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    control_commands: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class UnitHandoffPackage:
    owner_unit: str
    upstream_units: list[str]
    downstream_units: list[str]
    entry_count: int
    entries: list[HandoffEntry]
    readiness: dict[str, Any]
    remediation: dict[str, Any]
    line_count: dict[str, Any] | None = None
    recommended_commands: list[str] = field(default_factory=list)
    next_agent_instructions: list[str] = field(default_factory=list)

    @property
    def ok_to_start(self) -> bool:
        readiness_ok = bool(self.readiness.get("ok")) if self.readiness else False
        blockers = int(self.remediation.get("blocker_actions", 0)) if self.remediation else 0
        return blockers == 0 and self.entry_count > 0 and readiness_ok

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["ok_to_start"] = self.ok_to_start
        return payload


def build_unit_handoff_package(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str,
    line_count_report: EffectiveLineCountReport | None = None,
    include_upstream_entries: bool = True,
) -> UnitHandoffPackage:
    audit = InternalizationLedgerAuditor(project_root, strict=True).audit(ledger)
    readiness = build_unit_readiness_report(
        project_root,
        ledger,
        owner_unit=owner_unit,
        audit_report=audit,
        line_count_report=line_count_report,
    )
    remediation = build_remediation_plan(audit, owner_unit=owner_unit, include_warnings=True)
    entries = entries_for_unit_and_dependencies(ledger, owner_unit) if include_upstream_entries else ledger.by_owner_unit(owner_unit)
    handoff_entries = [handoff_entry_from_ledger(entry) for entry in entries]
    return UnitHandoffPackage(
        owner_unit=owner_unit,
        upstream_units=upstream_closure(owner_unit),
        downstream_units=downstream_closure(owner_unit),
        entry_count=len(handoff_entries),
        entries=handoff_entries,
        readiness=readiness.to_dict(),
        remediation=remediation.to_dict(),
        line_count=line_count_report.to_dict() if line_count_report else None,
        recommended_commands=recommended_commands(owner_unit, line_count_report=line_count_report),
        next_agent_instructions=next_agent_instructions(owner_unit),
    )


def handoff_entry_from_ledger(entry: InternalizationLedgerEntry) -> HandoffEntry:
    return HandoffEntry(
        ledger_id=entry.ledger_id,
        source_repo=entry.source_repo,
        source_path=entry.source_path,
        capability_name=entry.capability_name,
        owner_unit=entry.owner_unit,
        lifecycle=str(entry.lifecycle),
        main_path_status=str(entry.main_path_status),
        primary_target=entry.primary_target_path,
        target_paths=entry.target_paths,
        runtime_module=entry.runtime_entry.module,
        runtime_command=entry.runtime_entry.command,
        test_commands=[test.command for test in entry.test_entries if test.command],
        api_routes=list(entry.main_path.api_routes),
        event_types=list(entry.main_path.event_types),
        control_commands=list(entry.main_path.control_commands),
        blockers=list(entry.blockers),
        risk_notes=list(entry.risk_notes),
    )


def recommended_commands(owner_unit: str, *, line_count_report: EffectiveLineCountReport | None = None) -> list[str]:
    commands = [
        ".\\.venv\\Scripts\\python.exe -m unittest discover -s tests",
        ".\\.venv\\Scripts\\python.exe scripts\\verify_submission_boundary.py",
        f".\\.venv\\Scripts\\python.exe scripts\\zyra_integration_ledger.py readiness --owner-unit {owner_unit} --json",
        f".\\.venv\\Scripts\\python.exe scripts\\zyra_integration_ledger.py gate --owner-unit {owner_unit} --json",
    ]
    if line_count_report is not None:
        commands.append(
            ".\\.venv\\Scripts\\python.exe scripts\\verify_internalization_ledger.py "
            f"--base {line_count_report.base} --unit {owner_unit} --minimum-effective-lines {line_count_report.minimum_effective_lines} --fail-on-shortfall"
        )
    return commands


def next_agent_instructions(owner_unit: str) -> list[str]:
    return [
        f"Read the full execution-unit document for {owner_unit} before editing files.",
        "Use the internalization ledger to select concrete source-to-target records; do not rely on raw seed line counts.",
        "Update ledger lifecycle/status only through guarded CLI/API advance or equivalent workflow code.",
        "Report raw added lines, excluded seed/inventory lines, and effective added lines separately.",
        "Do not mark planned records as connected until target path, runtime entry, tests, and main-path binding exist.",
    ]


def handoff_markdown(package: UnitHandoffPackage) -> str:
    lines = [
        f"# {package.owner_unit} Handoff Package",
        "",
        f"- ok_to_start: {package.ok_to_start}",
        f"- entry_count: {package.entry_count}",
        f"- upstream_units: {', '.join(package.upstream_units) or 'none'}",
        f"- downstream_units: {', '.join(package.downstream_units) or 'none'}",
        "",
        "## Commands",
        "",
    ]
    for command in package.recommended_commands:
        lines.append(f"- `{command}`")
    lines.extend(["", "## Instructions", ""])
    for instruction in package.next_agent_instructions:
        lines.append(f"- {instruction}")
    lines.extend(["", "## Entries", ""])
    for entry in package.entries[:100]:
        lines.extend(
            [
                f"### {entry.ledger_id}",
                "",
                f"- source: `{entry.source_repo}:{entry.source_path}`",
                f"- target: `{entry.primary_target}`",
                f"- owner_unit: `{entry.owner_unit}`",
                f"- status: `{entry.lifecycle}/{entry.main_path_status}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
