from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .compact_restore_runtime import CompactRestoreReport, RestoreSegmentKind
from .runtime_budget_state import CODEWORKER_API_FOUNDATION_RUNTIME_ID, M1_02D_OWNER_UNIT, RuntimeBudgetSnapshot


class CompactRestorePolicyStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class CompactRestorePolicySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class CompactRestorePolicySurface(StrEnum):
    BOUNDARY = "boundary"
    RESTORE_CONTRACT = "restore_contract"
    PRESERVED_SEGMENT = "preserved_segment"
    RESTORE_SEGMENT = "restore_segment"
    BUDGET_STATE = "budget_state"
    SOURCE_DECISION = "source_decision"


@dataclass(frozen=True, slots=True)
class CompactRestorePolicyRule:
    rule_id: str
    name: str
    surface: CompactRestorePolicySurface
    required: bool
    satisfied: bool
    evidence: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.required and not self.satisfied

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "surface": str(self.surface),
            "required": self.required,
            "satisfied": self.satisfied,
            "blocking": self.blocking,
            "evidence": self.evidence,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactRestorePolicyFinding:
    code: str
    severity: CompactRestorePolicySeverity
    surface: CompactRestorePolicySurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == CompactRestorePolicySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactRestorePolicyReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    rules: tuple[CompactRestorePolicyRule, ...]
    findings: tuple[CompactRestorePolicyFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(rule.blocking for rule in self.rules) and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> CompactRestorePolicyStatus:
        if not self.ok:
            return CompactRestorePolicyStatus.BLOCKED
        if self.findings:
            return CompactRestorePolicyStatus.DEGRADED
        return CompactRestorePolicyStatus.READY

    @property
    def blocked_rules(self) -> int:
        return sum(1 for rule in self.rules if rule.blocking)

    @property
    def satisfied_rules(self) -> int:
        return sum(1 for rule in self.rules if rule.satisfied)

    @property
    def required_segment_count(self) -> int:
        return sum(1 for rule in self.rules if rule.required and rule.surface == CompactRestorePolicySurface.RESTORE_SEGMENT)

    @property
    def compact_needed(self) -> bool:
        return any(rule.name == "boundary_or_compact_needed_present" and rule.satisfied for rule in self.rules)

    @property
    def restore_contract_present(self) -> bool:
        return any(rule.name == "next_turn_restore_contract_present" and rule.satisfied for rule in self.rules)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.compact_restore_policy.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "rule_count": len(self.rules),
            "satisfied_rules": self.satisfied_rules,
            "blocked_rules": self.blocked_rules,
            "required_segment_count": self.required_segment_count,
            "compact_needed": self.compact_needed,
            "restore_contract_present": self.restore_contract_present,
            "rules": [rule.to_dict() for rule in self.rules],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "compact_restore_policy_report_id": self.report_id,
            "compact_restore_policy_owner_unit": self.owner_unit,
            "compact_restore_policy_runtime_id": self.runtime_id,
            "compact_restore_policy_ok": str(self.ok).lower(),
            "compact_restore_policy_status": str(self.status),
            "compact_restore_policy_rules": str(len(self.rules)),
            "compact_restore_policy_satisfied_rules": str(self.satisfied_rules),
            "compact_restore_policy_blocked_rules": str(self.blocked_rules),
            "compact_restore_policy_required_segments": str(self.required_segment_count),
            "compact_restore_policy_compact_needed": str(self.compact_needed).lower(),
            "compact_restore_policy_restore_contract_present": str(self.restore_contract_present).lower(),
            "compact_restore_policy_findings": str(len(self.findings)),
        }


class CompactRestorePolicyRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        compact_restore: CompactRestoreReport,
        budget_snapshot: RuntimeBudgetSnapshot,
    ) -> CompactRestorePolicyReport:
        contract = compact_restore.restore_contract
        boundary = compact_restore.boundary
        restore_kinds = {
            str(segment.kind).split(".")[-1]
            for segment in (contract.restore_segments if contract is not None else ())
        }
        rules = (
            CompactRestorePolicyRule(
                rule_id=new_id("compact_rule"),
                name="compact_restore_report_ok",
                surface=CompactRestorePolicySurface.RESTORE_CONTRACT,
                required=True,
                satisfied=compact_restore.ok,
                evidence=compact_restore.report_id,
            ),
            CompactRestorePolicyRule(
                rule_id=new_id("compact_rule"),
                name="boundary_or_compact_needed_present",
                surface=CompactRestorePolicySurface.BOUNDARY,
                required=True,
                satisfied=boundary is not None or compact_restore.compact_needed,
                evidence=boundary.boundary_id if boundary else "compact-needed",
            ),
            CompactRestorePolicyRule(
                rule_id=new_id("compact_rule"),
                name="next_turn_restore_contract_present",
                surface=CompactRestorePolicySurface.RESTORE_CONTRACT,
                required=True,
                satisfied=contract is not None and contract.ok,
                evidence=contract.contract_id if contract else "",
            ),
            CompactRestorePolicyRule(
                rule_id=new_id("compact_rule"),
                name="runtime_budget_state_segment_present",
                surface=CompactRestorePolicySurface.BUDGET_STATE,
                required=True,
                satisfied="budget_state" in restore_kinds and budget_snapshot.ok,
                evidence=budget_snapshot.snapshot_id,
            ),
            CompactRestorePolicyRule(
                rule_id=new_id("compact_rule"),
                name="tool_or_file_or_plan_restore_segment_present",
                surface=CompactRestorePolicySurface.RESTORE_SEGMENT,
                required=True,
                satisfied=bool({"tool_result", "file_attachment", "active_plan"} & restore_kinds),
                evidence=",".join(sorted(restore_kinds)),
            ),
            CompactRestorePolicyRule(
                rule_id=new_id("compact_rule"),
                name="skill_or_mcp_restore_segment_present",
                surface=CompactRestorePolicySurface.RESTORE_SEGMENT,
                required=False,
                satisfied=bool({"invoked_skill", "mcp_instruction_delta"} & restore_kinds),
                evidence=",".join(sorted(restore_kinds)),
            ),
            CompactRestorePolicyRule(
                rule_id=new_id("compact_rule"),
                name="preserved_segments_present",
                surface=CompactRestorePolicySurface.PRESERVED_SEGMENT,
                required=True,
                satisfied=len(compact_restore.preserved_segments) > 0,
                evidence=str(len(compact_restore.preserved_segments)),
            ),
            CompactRestorePolicyRule(
                rule_id=new_id("compact_rule"),
                name="source_decisions_are_zyra_targets",
                surface=CompactRestorePolicySurface.SOURCE_DECISION,
                required=True,
                satisfied=all(_source_decision_ok(item) for item in compact_restore.source_decisions),
                evidence=str(len(compact_restore.source_decisions)),
            ),
        )
        findings = [
            CompactRestorePolicyFinding(
                code="COMPACT_RESTORE_POLICY_RULE_BLOCKED",
                severity=CompactRestorePolicySeverity.BLOCKER,
                surface=rule.surface,
                message=f"Compact restore policy rule {rule.name} is not satisfied.",
                metadata=rule.to_dict(),
            )
            for rule in rules
            if rule.blocking
        ]
        return CompactRestorePolicyReport(
            report_id=new_id("compact_policy"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=compact_restore.session_id,
            worker_request_id=compact_restore.worker_request_id,
            rules=rules,
            findings=tuple(findings),
            source_decisions=default_compact_restore_policy_source_decisions(),
        )

    def event_for_report(
        self,
        report: CompactRestorePolicyReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "compact_restore_policy",
                    "compact_restore_policy": report.to_dict(),
                }
            },
        )


def compact_restore_policy_metadata(report: CompactRestorePolicyReport | None) -> dict[str, str]:
    if report is None:
        return {"compact_restore_policy_ok": "false", "compact_restore_policy_status": "missing", "compact_restore_policy_report_id": ""}
    return report.metadata()


def render_compact_restore_policy_markdown(report: CompactRestorePolicyReport) -> str:
    lines = [
        "# Compact Restore Policy",
        "",
        f"- report_id: {report.report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- satisfied_rules: {report.satisfied_rules}",
        f"- blocked_rules: {report.blocked_rules}",
        "",
        "## Rules",
    ]
    for rule in report.rules:
        lines.append(f"- {rule.name}: satisfied={str(rule.satisfied).lower()} required={str(rule.required).lower()} evidence={rule.evidence}")
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def default_compact_restore_policy_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/postCompactCleanup.ts",
            "target_path": "packages/runtime/zyra_runtime/compact_restore_policy_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "restore attachment and post-compact required segment validation",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/sessionMemoryCompact.ts",
            "target_path": "packages/runtime/zyra_runtime/compact_restore_policy_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "message-pair and budget-state restore policy",
        },
    )


def _source_decision_ok(item: Mapping[str, str]) -> bool:
    target = str(item.get("target_path") or "").replace("\\", "/").lower()
    decision = str(item.get("decision") or "")
    if not target.startswith("packages/") and not target.startswith("apps/"):
        return False
    if any(part in target for part in ("vendor", "vendor-runtimes", "source-pool", "runtime-sources")):
        return False
    return decision in {"zyra_module_migrated", "adapter_encapsulated"}
