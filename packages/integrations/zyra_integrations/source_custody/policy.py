from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .model import (
    AuditSection,
    Disposition,
    Finding,
    Severity,
    deduplicate_findings,
    identity,
    stable_digest,
)


class AuditMode(StrEnum):
    INVENTORY = "inventory"
    CANDIDATE = "candidate"
    FREEZE = "freeze"


class QueuePriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

    @property
    def rank(self) -> int:
        return {self.P0: 0, self.P1: 1, self.P2: 2, self.P3: 3}[self]


class QueueAction(StrEnum):
    REMOVE = "remove"
    ABSORB = "absorb"
    EXTERNALIZE = "externalize"
    DECLARE = "declare"
    REPAIR = "repair"
    VERIFY = "verify"
    TRACK = "track"


@dataclass(frozen=True, slots=True)
class PolicyRule:
    code: str
    priority: QueuePriority
    action: QueueAction
    owner_unit: str
    freeze_blocking: bool
    title: str


RULES: tuple[PolicyRule, ...] = (
    PolicyRule(
        "catalog_*",
        QueuePriority.P0,
        QueueAction.REPAIR,
        "M3-01A",
        True,
        "Repair source-custody catalog",
    ),
    PolicyRule(
        "active_capability_*",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01B",
        True,
        "Resolve active source-role ownership",
    ),
    PolicyRule(
        "required_source_repository_missing",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01A",
        True,
        "Complete the all-source freeze map",
    ),
    PolicyRule(
        "source_license_unresolved",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01B",
        True,
        "Resolve active source license",
    ),
    PolicyRule(
        "openclaw_*",
        QueuePriority.P0,
        QueueAction.REMOVE,
        "M3-01B",
        True,
        "Restore OpenClaw forward exclusion",
    ),
    PolicyRule(
        "langgraph_*",
        QueuePriority.P0,
        QueueAction.REMOVE,
        "M3-01B",
        True,
        "Remove broad LangGraph runtime custody",
    ),
    PolicyRule(
        "*parent_source*",
        QueuePriority.P0,
        QueueAction.ABSORB,
        "M3-01B",
        True,
        "Remove parent source-repository dependency",
    ),
    PolicyRule(
        "*source_path*",
        QueuePriority.P0,
        QueueAction.ABSORB,
        "M3-01B",
        True,
        "Remove root source-repository path",
    ),
    PolicyRule(
        "*dynamic_import*",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01B",
        True,
        "Replace undeclared dynamic import",
    ),
    PolicyRule(
        "*runtime_installer*",
        QueuePriority.P0,
        QueueAction.REMOVE,
        "M3-01B",
        True,
        "Remove runtime dependency installation",
    ),
    PolicyRule(
        "*dynamic_installer*",
        QueuePriority.P0,
        QueueAction.REMOVE,
        "M3-01B",
        True,
        "Remove runtime dependency installation",
    ),
    PolicyRule(
        "*undeclared_download*",
        QueuePriority.P0,
        QueueAction.EXTERNALIZE,
        "M3-01B",
        True,
        "Externalize and checksum runtime download",
    ),
    PolicyRule(
        "path_dependency_declared",
        QueuePriority.P0,
        QueueAction.ABSORB,
        "M3-01B",
        True,
        "Replace filesystem dependency",
    ),
    PolicyRule(
        "*dependency_undeclared",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01B",
        True,
        "Declare direct runtime dependency",
    ),
    PolicyRule(
        "process_*",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01B",
        True,
        "Resolve process custody",
    ),
    PolicyRule(
        "*process*undeclared",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01B",
        True,
        "Declare runtime process",
    ),
    PolicyRule(
        "listener_port_undeclared",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01B",
        True,
        "Declare listener port",
    ),
    PolicyRule(
        "mcp_plugin_*",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01B",
        True,
        "Declare extension process custody",
    ),
    PolicyRule(
        "opaque_*",
        QueuePriority.P0,
        QueueAction.EXTERNALIZE,
        "M3-01B",
        True,
        "Remove opaque runtime custody",
    ),
    PolicyRule(
        "source_similarity_*",
        QueuePriority.P1,
        QueueAction.VERIFY,
        "M3-01B",
        False,
        "Review vendor-like source similarity",
    ),
    PolicyRule(
        "vendor_*",
        QueuePriority.P1,
        QueueAction.ABSORB,
        "M3-01B",
        True,
        "Resolve vendor/source-pool debt",
    ),
    PolicyRule(
        "active_package_*",
        QueuePriority.P0,
        QueueAction.DECLARE,
        "M3-01A",
        True,
        "Add package source annotation",
    ),
    PolicyRule(
        "annotation_*",
        QueuePriority.P1,
        QueueAction.DECLARE,
        "M3-01A",
        True,
        "Align package source annotation",
    ),
    PolicyRule(
        "vendor_map_*",
        QueuePriority.P1,
        QueueAction.DECLARE,
        "M3-01A",
        True,
        "Align human vendor/source map",
    ),
    PolicyRule(
        "*notice*",
        QueuePriority.P1,
        QueueAction.DECLARE,
        "M3-01B",
        True,
        "Resolve source notice status",
    ),
    PolicyRule(
        "ledger_*",
        QueuePriority.P1,
        QueueAction.REPAIR,
        "M3-01B",
        True,
        "Reconcile historical ledger custody",
    ),
    PolicyRule(
        "structural_*",
        QueuePriority.P0,
        QueueAction.VERIFY,
        "M3-01B",
        True,
        "Restore required owner behavior evidence",
    ),
    PolicyRule(
        "openhands_*",
        QueuePriority.P0,
        QueueAction.EXTERNALIZE,
        "M3-01B",
        True,
        "Resolve OpenHands external SDK/runtime risk",
    ),
    PolicyRule(
        "hermes_*",
        QueuePriority.P1,
        QueueAction.EXTERNALIZE,
        "M3-01B",
        True,
        "Resolve Hermes ambient state/process risk",
    ),
    PolicyRule(
        "opencode_*",
        QueuePriority.P1,
        QueueAction.ABSORB,
        "M3-01B",
        True,
        "Resolve OpenCode version/process risk",
    ),
    PolicyRule(
        "omp_*",
        QueuePriority.P1,
        QueueAction.EXTERNALIZE,
        "M3-01B",
        True,
        "Resolve Oh My Pi state/native/process risk",
    ),
    PolicyRule(
        "*lockfile_missing",
        QueuePriority.P1,
        QueueAction.DECLARE,
        "M3-02B",
        True,
        "Freeze release dependency lock",
    ),
    PolicyRule(
        "*floating",
        QueuePriority.P2,
        QueueAction.DECLARE,
        "M3-02B",
        False,
        "Verify lock resolution for floating manifest range",
    ),
    PolicyRule(
        "*parse_failed",
        QueuePriority.P1,
        QueueAction.REPAIR,
        "M3-01B",
        True,
        "Repair unauditable source or manifest",
    ),
    PolicyRule(
        "*",
        QueuePriority.P2,
        QueueAction.TRACK,
        "M3-01B",
        False,
        "Review source-custody finding",
    ),
)


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_id: str
    priority: QueuePriority
    action: QueueAction
    owner_unit: str
    title: str
    finding_codes: tuple[str, ...]
    finding_fingerprints: tuple[str, ...]
    capabilities: tuple[str, ...]
    source_repositories: tuple[str, ...]
    paths: tuple[str, ...]
    default_path_impacts: tuple[str, ...]
    remediations: tuple[str, ...]
    release_blocking: bool
    acceptance: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "priority": self.priority.value,
            "action": self.action.value,
            "owner_unit": self.owner_unit,
            "title": self.title,
            "finding_codes": list(self.finding_codes),
            "finding_fingerprints": list(self.finding_fingerprints),
            "capabilities": list(self.capabilities),
            "source_repositories": list(self.source_repositories),
            "paths": list(self.paths),
            "default_path_impacts": list(self.default_path_impacts),
            "remediations": list(self.remediations),
            "release_blocking": self.release_blocking,
            "acceptance": list(self.acceptance),
        }


@dataclass(frozen=True, slots=True)
class PolicyResult:
    mode: AuditMode
    valid: bool
    release_ready: bool
    findings: tuple[Finding, ...]
    work_queue: tuple[WorkItem, ...]
    severity_counts: Mapping[str, int]
    rule_group_counts: Mapping[str, int]
    disposition_counts: Mapping[str, int]
    blocker_count: int
    error_count: int
    warning_count: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "valid": self.valid,
            "release_ready": self.release_ready,
            "severity_counts": dict(self.severity_counts),
            "rule_group_counts": dict(self.rule_group_counts),
            "disposition_counts": dict(self.disposition_counts),
            "blocker_count": self.blocker_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "findings": [item.to_dict() for item in self.findings],
            "work_queue": [item.to_dict() for item in self.work_queue],
            "digest": self.digest,
        }


class FindingPolicy:
    def __init__(self, *, rules: Sequence[PolicyRule] = RULES) -> None:
        self.rules = tuple(rules)
        if not self.rules or self.rules[-1].code != "*":
            raise ValueError("policy requires a final catch-all rule")

    def apply(
        self,
        sections: Iterable[AuditSection],
        *,
        mode: AuditMode | str,
    ) -> PolicyResult:
        selected_mode = AuditMode(mode)
        raw_findings = deduplicate_findings(
            finding for section in sections for finding in section.findings
        )
        classified = tuple(self._classify(item) for item in raw_findings)
        severity_counts = Counter(item.severity.value for item in classified)
        group_counts = Counter(item.rule_group for item in classified)
        disposition_counts = Counter(item.disposition.value for item in classified)
        blockers = tuple(item for item in classified if item.blocking)
        errors = tuple(
            item for item in classified if item.severity is Severity.ERROR
        )
        warnings = tuple(
            item for item in classified if item.severity is Severity.WARNING
        )
        queue = self._work_queue(classified)
        release_ready = not blockers and not (
            selected_mode is AuditMode.FREEZE and errors
        )
        valid = True if selected_mode is AuditMode.INVENTORY else release_ready
        payload = {
            "mode": selected_mode.value,
            "valid": valid,
            "release_ready": release_ready,
            "findings": [item.to_dict() for item in classified],
            "work_queue": [item.to_dict() for item in queue],
            "severity_counts": dict(sorted(severity_counts.items())),
            "rule_group_counts": dict(sorted(group_counts.items())),
            "disposition_counts": dict(sorted(disposition_counts.items())),
        }
        return PolicyResult(
            mode=selected_mode,
            valid=valid,
            release_ready=release_ready,
            findings=classified,
            work_queue=queue,
            severity_counts=dict(sorted(severity_counts.items())),
            rule_group_counts=dict(sorted(group_counts.items())),
            disposition_counts=dict(sorted(disposition_counts.items())),
            blocker_count=len(blockers),
            error_count=len(errors),
            warning_count=len(warnings),
            digest=stable_digest(payload),
        )

    def _classify(self, item: Finding) -> Finding:
        rule = self.rule_for(item.code)
        severity = item.severity
        disposition = item.disposition
        owner_unit = item.owner_unit
        runtime_scope = item.attributes.get("runtime_scope")
        if (
            rule.freeze_blocking
            and runtime_scope is not False
            and severity.rank < Severity.BLOCKER.rank
        ):
            severity = Severity.BLOCKER
        if disposition is Disposition.TRACK:
            disposition = {
                QueueAction.REMOVE: Disposition.REMOVE,
                QueueAction.ABSORB: Disposition.ABSORB,
                QueueAction.EXTERNALIZE: Disposition.EXTERNALIZE,
                QueueAction.DECLARE: Disposition.DECLARE,
                QueueAction.REPAIR: Disposition.DECLARE,
                QueueAction.VERIFY: Disposition.TRACK,
                QueueAction.TRACK: Disposition.TRACK,
            }[rule.action]
        if item.owner_unit == "M3-01B" or not item.owner_unit:
            owner_unit = rule.owner_unit
        return Finding(
            code=item.code,
            message=item.message,
            rule_group=item.rule_group,
            severity=severity,
            capability=item.capability,
            source_repo=item.source_repo,
            path=item.path,
            line=item.line,
            owner_unit=owner_unit,
            disposition=disposition,
            default_path_impact=item.default_path_impact,
            remediation=item.remediation,
            evidence=item.evidence,
            attributes=item.attributes,
        )

    def rule_for(self, code: str) -> PolicyRule:
        for rule in self.rules:
            if match_code(rule.code, code):
                return rule
        raise AssertionError("catch-all policy rule was not applied")

    def _work_queue(self, findings: Sequence[Finding]) -> tuple[WorkItem, ...]:
        grouped: dict[tuple[str, str, str, str], list[Finding]] = defaultdict(list)
        for item in findings:
            rule = self.rule_for(item.code)
            source_or_capability = (
                item.capability or item.source_repo.casefold() or item.path or item.code
            )
            key = (
                rule.owner_unit,
                rule.action.value,
                rule.title,
                source_or_capability,
            )
            grouped[key].append(item)
        result: list[WorkItem] = []
        for key, selected in grouped.items():
            owner_unit, action_value, title, scope = key
            rules = [self.rule_for(item.code) for item in selected]
            priority = min(
                (rule.priority for rule in rules),
                key=lambda item: item.rank,
            )
            action = QueueAction(action_value)
            release_blocking = any(item.blocking for item in selected)
            acceptance = acceptance_for(action, selected)
            material = {
                "owner_unit": owner_unit,
                "action": action.value,
                "scope": scope,
                "findings": sorted(item.fingerprint for item in selected),
            }
            work_id = (
                f"m3q_{stable_digest(material).split(':', 1)[1][:20]}"
            )
            result.append(
                WorkItem(
                    work_id=work_id,
                    priority=priority,
                    action=action,
                    owner_unit=owner_unit,
                    title=title,
                    finding_codes=tuple(
                        sorted({item.code for item in selected})
                    ),
                    finding_fingerprints=tuple(
                        sorted(item.fingerprint for item in selected)
                    ),
                    capabilities=tuple(
                        sorted({item.capability for item in selected if item.capability})
                    ),
                    source_repositories=tuple(
                        sorted(
                            {item.source_repo for item in selected if item.source_repo},
                            key=lambda value: (value.casefold(), value),
                        )
                    ),
                    paths=tuple(
                        sorted({item.path for item in selected if item.path})
                    ),
                    default_path_impacts=tuple(
                        sorted(
                            {
                                item.default_path_impact
                                for item in selected
                                if item.default_path_impact
                            }
                        )
                    ),
                    remediations=tuple(
                        sorted(
                            {
                                item.remediation
                                for item in selected
                                if item.remediation
                            }
                        )
                    ),
                    release_blocking=release_blocking,
                    acceptance=acceptance,
                )
            )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.priority.rank,
                    not item.release_blocking,
                    item.owner_unit,
                    item.title,
                    item.work_id,
                ),
            )
        )


def match_code(pattern: str, code: str) -> bool:
    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == code
    parts = pattern.split("*")
    cursor = 0
    anchored_start = not pattern.startswith("*")
    anchored_end = not pattern.endswith("*")
    for index, part in enumerate(parts):
        if not part:
            continue
        position = code.find(part, cursor)
        if position < 0:
            return False
        if index == 0 and anchored_start and position != 0:
            return False
        cursor = position + len(part)
    if anchored_end and parts[-1] and not code.endswith(parts[-1]):
        return False
    return True


def acceptance_for(
    action: QueueAction,
    findings: Sequence[Finding],
) -> tuple[str, ...]:
    common = [
        "The exact finding fingerprints are absent from a candidate-mode rerun.",
        "Focused behavior and failure-path tests pass without a fallback owner.",
    ]
    if action is QueueAction.REMOVE:
        common.extend(
            [
                "Dependency, path, process, config and package scans show no residual reachability.",
                "Removal does not delete the Zyra-owned capability or its required evidence.",
            ]
        )
    elif action is QueueAction.ABSORB:
        common.extend(
            [
                "Core behavior lands in a formal Zyra module with package annotation.",
                "The default path and disable test prove the absorbed owner is executable.",
            ]
        )
    elif action is QueueAction.EXTERNALIZE:
        common.extend(
            [
                "The external boundary has a locked version/checksum, owner and semantic healthcheck.",
                "Opaque or ambient state cannot become canonical Zyra state.",
            ]
        )
    elif action is QueueAction.DECLARE:
        common.extend(
            [
                "Catalog, manifest/process profile, package annotation and human map agree.",
                "Clean install/start uses only the declared dependency and process graph.",
            ]
        )
    elif action is QueueAction.REPAIR:
        common.extend(
            [
                "The repaired source/manifest/ledger input parses and round-trips deterministically.",
                "Protected historical facts are not rewritten to backfill later planning.",
            ]
        )
    elif action is QueueAction.VERIFY:
        common.extend(
            [
                "Executable owner, adversarial behavior and disable evidence are checksum-bound.",
                "The evidence demonstrates semantic effect, not file presence or fixed fixtures.",
            ]
        )
    else:
        common.append("The risk is explicitly accepted or routed before the freeze report.")
    if any(item.source_repo.casefold() == "openclaw" for item in findings):
        common.append(
            "OpenClaw remains historical-only and its deleted repository is not restored or read."
        )
    if any(item.source_repo.casefold() == "langgraph" for item in findings):
        common.append(
            "Only narrow exact-resume semantics remain; broad LangGraph runtime is unreachable."
        )
    return tuple(common)


def queue_summary(queue: Sequence[WorkItem]) -> dict[str, Any]:
    return {
        "items": len(queue),
        "blocking": sum(item.release_blocking for item in queue),
        "by_priority": dict(
            sorted(Counter(item.priority.value for item in queue).items())
        ),
        "by_action": dict(
            sorted(Counter(item.action.value for item in queue).items())
        ),
        "by_owner_unit": dict(
            sorted(Counter(item.owner_unit for item in queue).items())
        ),
    }
