from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable

from .ledger_audit import AuditFindingCode, AuditSeverity, LedgerAuditFinding, LedgerAuditReport
from .ledger_models import to_jsonable


class RemediationActionType(StrEnum):
    CREATE_TARGET = "create_target"
    ADD_TEST = "add_test"
    ADD_RUNTIME = "add_runtime"
    ADD_MAIN_PATH = "add_main_path"
    RESOLVE_NOTICE = "resolve_notice"
    REMOVE_PARENT_DEP = "remove_parent_dep"
    FIX_SCHEMA = "fix_schema"
    UPDATE_LINE_COUNT_POLICY = "update_line_count_policy"
    REFRESH_SOURCE_EVIDENCE = "refresh_source_evidence"
    REVIEW_PLANNED_TARGET = "review_planned_target"
    SPLIT_TARGET_OWNER = "split_target_owner"
    ADD_UNIT_LEDGER_RECORDS = "add_unit_ledger_records"
    MANUAL_REVIEW = "manual_review"


class RemediationPriority(StrEnum):
    BLOCKER = "blocker"
    REQUIRED = "required"
    SHOULD = "should"
    LATER = "later"


@dataclass(slots=True)
class RemediationAction:
    action_type: RemediationActionType
    priority: RemediationPriority
    ledger_id: str = ""
    owner_unit: str = ""
    source_repo: str = ""
    target_path: str = ""
    title: str = ""
    rationale: str = ""
    command_hint: str = ""
    acceptance_check: str = ""
    related_findings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class RemediationPlan:
    ok_to_continue: bool
    total_actions: int
    blocker_actions: int
    required_actions: int
    by_owner_unit: dict[str, int]
    by_action_type: dict[str, int]
    actions: list[RemediationAction]

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


def build_remediation_plan(report: LedgerAuditReport, *, owner_unit: str = "", include_warnings: bool = True) -> RemediationPlan:
    actions: list[RemediationAction] = []
    for finding in report.findings:
        if owner_unit and finding.owner_unit != owner_unit and finding.owner_unit:
            continue
        if finding.severity == AuditSeverity.INFO:
            continue
        if finding.severity == AuditSeverity.WARNING and not include_warnings:
            continue
        actions.append(action_for_finding(finding))
    coalesced = coalesce_actions(actions)
    by_owner = Counter(action.owner_unit or "global" for action in coalesced)
    by_type = Counter(str(action.action_type) for action in coalesced)
    blocker_actions = sum(1 for action in coalesced if action.priority == RemediationPriority.BLOCKER)
    required_actions = sum(1 for action in coalesced if action.priority in {RemediationPriority.BLOCKER, RemediationPriority.REQUIRED})
    return RemediationPlan(
        ok_to_continue=blocker_actions == 0,
        total_actions=len(coalesced),
        blocker_actions=blocker_actions,
        required_actions=required_actions,
        by_owner_unit=dict(sorted(by_owner.items())),
        by_action_type=dict(sorted(by_type.items())),
        actions=coalesced,
    )


def action_for_finding(finding: LedgerAuditFinding) -> RemediationAction:
    action_type = _action_type_for_code(finding.code)
    priority = _priority_for_finding(finding)
    title = _title_for_action(action_type, finding)
    acceptance = _acceptance_for_action(action_type, finding)
    command = _command_hint_for_action(action_type, finding)
    return RemediationAction(
        action_type=action_type,
        priority=priority,
        ledger_id=finding.ledger_id,
        owner_unit=finding.owner_unit,
        source_repo=finding.source_repo,
        target_path=finding.target_path,
        title=title,
        rationale=finding.message,
        command_hint=command,
        acceptance_check=acceptance,
        related_findings=[str(finding.code)],
        metadata={
            "severity": str(finding.severity),
            "source_path": finding.source_path,
            "remediation": finding.remediation,
            **finding.metadata,
        },
    )


def coalesce_actions(actions: Iterable[RemediationAction]) -> list[RemediationAction]:
    grouped: dict[tuple[str, str, str, str], RemediationAction] = {}
    for action in actions:
        key = (str(action.action_type), action.ledger_id, action.owner_unit, action.target_path)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = action
            continue
        existing.related_findings.extend(action.related_findings)
        existing.related_findings = sorted(set(existing.related_findings))
        existing.rationale = _merge_text(existing.rationale, action.rationale)
        existing.metadata.update(action.metadata)
        if _priority_rank(action.priority) > _priority_rank(existing.priority):
            existing.priority = action.priority
    return sorted(
        grouped.values(),
        key=lambda action: (
            -_priority_rank(action.priority),
            action.owner_unit,
            str(action.action_type),
            action.ledger_id,
            action.target_path,
        ),
    )


def remediation_markdown(plan: RemediationPlan) -> str:
    lines = [
        "# Internalization Ledger Remediation Plan",
        "",
        f"- ok_to_continue: {plan.ok_to_continue}",
        f"- total_actions: {plan.total_actions}",
        f"- blocker_actions: {plan.blocker_actions}",
        f"- required_actions: {plan.required_actions}",
        "",
    ]
    for action in plan.actions:
        lines.extend(
            [
                f"## {action.priority} {action.action_type}",
                "",
                f"- ledger_id: {action.ledger_id or 'global'}",
                f"- owner_unit: {action.owner_unit or 'global'}",
                f"- target_path: {action.target_path or 'n/a'}",
                f"- title: {action.title}",
                f"- rationale: {action.rationale}",
                f"- acceptance_check: {action.acceptance_check}",
                "",
            ]
        )
        if action.command_hint:
            lines.extend(["```powershell", action.command_hint, "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def _action_type_for_code(code: AuditFindingCode) -> RemediationActionType:
    mapping = {
        AuditFindingCode.MISSING_TARGET_PATH: RemediationActionType.CREATE_TARGET,
        AuditFindingCode.TARGET_PATH_NOT_FOUND: RemediationActionType.CREATE_TARGET,
        AuditFindingCode.PLANNED_TARGET_NOT_MATERIALIZED: RemediationActionType.REVIEW_PLANNED_TARGET,
        AuditFindingCode.MISSING_TEST_ENTRY: RemediationActionType.ADD_TEST,
        AuditFindingCode.MISSING_RUNTIME_ENTRY: RemediationActionType.ADD_RUNTIME,
        AuditFindingCode.FALSE_CONNECTED_STATUS: RemediationActionType.ADD_MAIN_PATH,
        AuditFindingCode.MISSING_MAIN_PATH_STATUS: RemediationActionType.ADD_MAIN_PATH,
        AuditFindingCode.MISSING_LICENSE_NOTICE: RemediationActionType.RESOLVE_NOTICE,
        AuditFindingCode.FORBIDDEN_RELATIVE_SOURCE_DEP: RemediationActionType.REMOVE_PARENT_DEP,
        AuditFindingCode.SCHEMA_VALIDATION_ERROR: RemediationActionType.FIX_SCHEMA,
        AuditFindingCode.INVALID_LINE_COUNT_POLICY: RemediationActionType.UPDATE_LINE_COUNT_POLICY,
        AuditFindingCode.SOURCE_PATH_NOT_VERIFIED: RemediationActionType.REFRESH_SOURCE_EVIDENCE,
        AuditFindingCode.CONFLICTING_TARGET_OWNER: RemediationActionType.SPLIT_TARGET_OWNER,
        AuditFindingCode.EXECUTION_UNIT_COVERAGE_INCOMPLETE: RemediationActionType.ADD_UNIT_LEDGER_RECORDS,
        AuditFindingCode.MATERIALIZED_TARGET_IS_DATA_ONLY: RemediationActionType.CREATE_TARGET,
    }
    return mapping.get(code, RemediationActionType.MANUAL_REVIEW)


def _priority_for_finding(finding: LedgerAuditFinding) -> RemediationPriority:
    if finding.severity == AuditSeverity.BLOCKER:
        return RemediationPriority.BLOCKER
    if finding.severity == AuditSeverity.ERROR:
        return RemediationPriority.REQUIRED
    if finding.severity == AuditSeverity.WARNING:
        return RemediationPriority.SHOULD
    return RemediationPriority.LATER


def _title_for_action(action_type: RemediationActionType, finding: LedgerAuditFinding) -> str:
    titles = {
        RemediationActionType.CREATE_TARGET: "Create or correctly classify the target module",
        RemediationActionType.ADD_TEST: "Add a test entry and runnable verification command",
        RemediationActionType.ADD_RUNTIME: "Add runtime entry metadata",
        RemediationActionType.ADD_MAIN_PATH: "Bind the entry to a real main-path surface",
        RemediationActionType.RESOLVE_NOTICE: "Resolve license and NOTICE state",
        RemediationActionType.REMOVE_PARENT_DEP: "Remove parent workspace source dependency",
        RemediationActionType.FIX_SCHEMA: "Fix ledger schema fields",
        RemediationActionType.UPDATE_LINE_COUNT_POLICY: "Correct the line-count policy",
        RemediationActionType.REFRESH_SOURCE_EVIDENCE: "Refresh source evidence against the workspace",
        RemediationActionType.REVIEW_PLANNED_TARGET: "Review planned target materialization debt",
        RemediationActionType.SPLIT_TARGET_OWNER: "Split or document shared target ownership",
        RemediationActionType.ADD_UNIT_LEDGER_RECORDS: "Add source-to-target records for missing execution units",
        RemediationActionType.MANUAL_REVIEW: "Review audit finding manually",
    }
    return titles[action_type] + (f" for {finding.ledger_id}" if finding.ledger_id else "")


def _acceptance_for_action(action_type: RemediationActionType, finding: LedgerAuditFinding) -> str:
    if action_type == RemediationActionType.CREATE_TARGET:
        return "Target path exists under zyra and is not seed/inventory-only."
    if action_type == RemediationActionType.ADD_TEST:
        return "Ledger entry contains at least one test path and command; relevant tests pass."
    if action_type == RemediationActionType.ADD_RUNTIME:
        return "runtime_entry has command/module/function/protocol/health_check as appropriate."
    if action_type == RemediationActionType.ADD_MAIN_PATH:
        return "main_path records API/event/control/worker/UI binding matching the status."
    if action_type == RemediationActionType.RESOLVE_NOTICE:
        return "license_notice.status is recorded or not_required with notes."
    if action_type == RemediationActionType.REMOVE_PARENT_DEP:
        return "Strict audit finds no ../source-repo or absolute workspace dependency."
    if action_type == RemediationActionType.ADD_UNIT_LEDGER_RECORDS:
        return "Unit matrix shows every execution unit has ledger coverage or an explicit documented exception."
    return finding.remediation or "Audit finding disappears or is downgraded with a documented reason."


def _command_hint_for_action(action_type: RemediationActionType, finding: LedgerAuditFinding) -> str:
    if action_type == RemediationActionType.REFRESH_SOURCE_EVIDENCE:
        return ".\\.venv\\Scripts\\python.exe scripts\\build_integration_ledger_seed.py"
    if action_type == RemediationActionType.REMOVE_PARENT_DEP:
        return ".\\.venv\\Scripts\\python.exe scripts\\verify_submission_boundary.py"
    if action_type == RemediationActionType.ADD_TEST:
        return ".\\.venv\\Scripts\\python.exe -m unittest discover -s tests"
    if action_type == RemediationActionType.UPDATE_LINE_COUNT_POLICY:
        return ".\\.venv\\Scripts\\python.exe scripts\\verify_internalization_ledger.py --unit M1-01A --fail-on-shortfall"
    return ""


def _priority_rank(priority: RemediationPriority) -> int:
    return {
        RemediationPriority.LATER: 0,
        RemediationPriority.SHOULD: 1,
        RemediationPriority.REQUIRED: 2,
        RemediationPriority.BLOCKER: 3,
    }[priority]


def _merge_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right or right in left:
        return left
    return f"{left}; {right}"
