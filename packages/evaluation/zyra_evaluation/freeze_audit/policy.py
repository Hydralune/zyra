from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .model import (
    AuditMode,
    AuditSection,
    Disposition,
    Finding,
    Severity,
    deduplicate_findings,
    stable_digest,
)


class QueuePriority(StrEnum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


class QueueAction(StrEnum):
    RESOLVE_OWNER = "resolve_owner"
    REWIRE_DEFAULT = "rewire_default"
    REPAIR_CAUSALITY = "repair_causality"
    DISPOSE_SOURCE_RISK = "dispose_source_risk"
    CORRECT_LINE_BUCKET = "correct_line_bucket"
    ADD_REQUIREMENT_EVIDENCE = "add_requirement_evidence"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_id: str
    owner_unit: str
    priority: QueuePriority
    action: QueueAction
    blocking: bool
    finding_codes: tuple[str, ...]
    finding_fingerprints: tuple[str, ...]
    domains: tuple[str, ...]
    requirements: tuple[str, ...]
    paths: tuple[str, ...]
    default_path_impact: str
    remediation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "owner_unit": self.owner_unit,
            "priority": self.priority.value,
            "action": self.action.value,
            "blocking": self.blocking,
            "finding_codes": list(self.finding_codes),
            "finding_fingerprints": list(self.finding_fingerprints),
            "domains": list(self.domains),
            "requirements": list(self.requirements),
            "paths": list(self.paths),
            "default_path_impact": self.default_path_impact,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class PolicyResult:
    mode: AuditMode
    findings: tuple[Finding, ...]
    work_queue: tuple[WorkItem, ...]
    valid: bool
    release_ready: bool
    blocker_count: int
    error_count: int
    warning_count: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "valid": self.valid,
            "release_ready": self.release_ready,
            "blocker_count": self.blocker_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "finding_count": len(self.findings),
            "work_item_count": len(self.work_queue),
            "findings": [item.to_dict() for item in self.findings],
            "work_queue": [item.to_dict() for item in self.work_queue],
            "digest": self.digest,
        }


class FreezeFindingPolicy:
    def apply(
        self,
        sections: Sequence[AuditSection],
        *,
        mode: AuditMode | str,
    ) -> PolicyResult:
        selected_mode = AuditMode(mode)
        findings = deduplicate_findings(
            finding
            for audit_section in sections
            for finding in audit_section.findings
        )
        queue = self._queue(findings)
        blocker_count = sum(item.blocking for item in findings)
        error_count = sum(item.severity is Severity.ERROR for item in findings)
        warning_count = sum(item.severity is Severity.WARNING for item in findings)
        release_ready = blocker_count == 0 and error_count == 0
        valid = (
            True
            if selected_mode is AuditMode.INVENTORY
            else blocker_count == 0
            if selected_mode is AuditMode.CANDIDATE
            else release_ready
        )
        material = {
            "mode": selected_mode.value,
            "valid": valid,
            "release_ready": release_ready,
            "section_digests": {
                item.name: item.digest for item in sorted(sections, key=lambda value: value.name)
            },
            "findings": [item.to_dict() for item in findings],
            "work_queue": [item.to_dict() for item in queue],
        }
        return PolicyResult(
            mode=selected_mode,
            findings=findings,
            work_queue=queue,
            valid=valid,
            release_ready=release_ready,
            blocker_count=blocker_count,
            error_count=error_count,
            warning_count=warning_count,
            digest=stable_digest(material),
        )

    def _queue(self, findings: Sequence[Finding]) -> tuple[WorkItem, ...]:
        grouped: dict[tuple[str, QueueAction, str, str], list[Finding]] = {}
        for item in findings:
            action = action_for(item)
            grouping_identity = item.domain or item.requirement_id or item.path or item.code
            key = (
                normalized_owner_unit(item),
                action,
                grouping_identity,
                item.disposition.value,
            )
            grouped.setdefault(key, []).append(item)
        queue: list[WorkItem] = []
        for (owner_unit, action, _, _), group in sorted(
            grouped.items(),
            key=lambda item: (
                item[0][0],
                item[0][1].value,
                item[0][2],
                item[0][3],
            ),
        ):
            selected = deduplicate_findings(group)
            priority = priority_for(selected)
            payload = {
                "owner_unit": owner_unit,
                "action": action.value,
                "finding_fingerprints": [item.fingerprint for item in selected],
                "domains": sorted({item.domain for item in selected if item.domain}),
                "requirements": sorted(
                    {
                        item.requirement_id
                        for item in selected
                        if item.requirement_id
                    }
                ),
                "paths": sorted({item.path for item in selected if item.path}),
            }
            queue.append(
                WorkItem(
                    work_id=(
                        "m3_work_"
                        + stable_digest(payload).split(":", 1)[1][:20]
                    ),
                    owner_unit=owner_unit,
                    priority=priority,
                    action=action,
                    blocking=any(item.blocking for item in selected),
                    finding_codes=tuple(sorted({item.code for item in selected})),
                    finding_fingerprints=tuple(
                        sorted(item.fingerprint for item in selected)
                    ),
                    domains=tuple(payload["domains"]),
                    requirements=tuple(payload["requirements"]),
                    paths=tuple(payload["paths"]),
                    default_path_impact=first_nonempty(
                        item.default_path_impact for item in selected
                    ),
                    remediation=first_nonempty(
                        item.remediation for item in selected
                    ),
                )
            )
        return tuple(
            sorted(
                queue,
                key=lambda item: (
                    priority_rank(item.priority),
                    item.owner_unit,
                    item.action.value,
                    item.work_id,
                ),
            )
        )


def action_for(item: Finding) -> QueueAction:
    group = item.rule_group
    if group in {"catalog", "ownership"}:
        return QueueAction.RESOLVE_OWNER
    if group == "reachability":
        return QueueAction.REWIRE_DEFAULT
    if group == "causality":
        return QueueAction.REPAIR_CAUSALITY
    if group == "source_risks":
        return QueueAction.DISPOSE_SOURCE_RISK
    if group == "effective_lines":
        return QueueAction.CORRECT_LINE_BUCKET
    if group == "requirements":
        return QueueAction.ADD_REQUIREMENT_EVIDENCE
    return QueueAction.REVIEW


def normalized_owner_unit(item: Finding) -> str:
    if item.owner_unit in {"M3-01B", "M3-02A", "M3-02B", "M3-03"}:
        return item.owner_unit
    if item.rule_group in {
        "catalog",
        "ownership",
        "reachability",
        "causality",
        "source_risks",
        "effective_lines",
    }:
        return "M3-01B"
    return "M3-03"


def priority_for(findings: Sequence[Finding]) -> QueuePriority:
    if any(item.blocking for item in findings):
        if any(
            item.rule_group
            in {"catalog", "ownership", "reachability", "causality"}
            for item in findings
        ):
            return QueuePriority.P0
        return QueuePriority.P1
    if any(item.severity is Severity.ERROR for item in findings):
        return QueuePriority.P2
    return QueuePriority.P3


def priority_rank(priority: QueuePriority) -> int:
    return {
        QueuePriority.P0: 0,
        QueuePriority.P1: 1,
        QueuePriority.P2: 2,
        QueuePriority.P3: 3,
    }[priority]


def first_nonempty(values: Iterable[str]) -> str:
    return next((item for item in values if item), "")


def queue_summary(queue: Sequence[WorkItem]) -> dict[str, Any]:
    return {
        "items": len(queue),
        "blocking": sum(item.blocking for item in queue),
        "by_owner_unit": dict(
            sorted(Counter(item.owner_unit for item in queue).items())
        ),
        "by_priority": dict(
            sorted(Counter(item.priority.value for item in queue).items())
        ),
        "by_action": dict(
            sorted(Counter(item.action.value for item in queue).items())
        ),
    }
