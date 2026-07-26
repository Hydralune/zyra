from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    AuditSection,
    Disposition,
    Evidence,
    EvidenceKind,
    Finding,
    LandingStatus,
    RuleSwitches,
    Severity,
    SourceEntry,
    SourceRole,
    deduplicate_findings,
    finding,
    identity,
    normalize_repo_path,
    relative_path,
    resolve_within,
    section,
    stable_digest,
    text,
)


CATALOG_SCHEMA = "zyra.source-custody-catalog/v1"
REQUIRED_SOURCE_REPOSITORIES = frozenset(
    {
        "zyra",
        "claude-code-best",
        "opencode",
        "browser-use",
        "OpenHands",
        "agentscope",
        "agent-framework",
        "hermes-agent",
        "langgraph",
        "oh-my-pi",
        "claudecode-related/claude-reviews-claude",
        "claudecode-related/Dive-into-Claude-Code",
    }
)
RELATED_CLAUDE_REFERENCES = frozenset(
    {
        "claudecode-related/claude-reviews-claude",
        "claudecode-related/Dive-into-Claude-Code",
    }
)
INACTIVE_LANDINGS = frozenset(
    {
        LandingStatus.CONFORMANCE,
        LandingStatus.REFERENCE,
        LandingStatus.EXPERIMENTAL,
        LandingStatus.DEFERRED,
        LandingStatus.REJECTED,
        LandingStatus.NOT_APPLICABLE,
    }
)


@dataclass(frozen=True, slots=True)
class HistoricalBoundary:
    source_repo: str
    status: str
    effective_from: str
    allowed_facts: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    notice_path: str
    reason: str

    @classmethod
    def parse(cls, raw: Any) -> "HistoricalBoundary":
        if not isinstance(raw, Mapping):
            raise ValueError("historical boundary must be an object")
        source_repo = identity(raw.get("source_repo"), field_name="historical source")
        if source_repo.casefold() != "openclaw":
            raise ValueError("only OpenClaw uses the forward historical boundary")
        status = identity(raw.get("status"), field_name="historical status")
        if status != "excluded_forward_only":
            raise ValueError("OpenClaw status must be excluded_forward_only")
        effective_from = identity(
            raw.get("effective_from"), field_name="historical effective_from"
        )
        allowed = _identity_array(raw.get("allowed_facts"), "allowed_facts")
        forbidden = _identity_array(raw.get("forbidden_actions"), "forbidden_actions")
        notice_path = normalize_repo_path(raw.get("notice_path"))
        reason = text(raw.get("reason"), field_name="historical reason")
        if len(reason) < 24:
            raise ValueError("historical boundary reason is too short")
        required_allowed = {
            "pre_boundary_provenance",
            "license_notice",
            "no_root_runtime_dependency_audit",
        }
        required_forbidden = {
            "source_read",
            "source_restore",
            "runtime_role",
            "migration_quota",
            "runtime_dependency",
        }
        if not required_allowed.issubset(allowed):
            raise ValueError("OpenClaw allowed facts are incomplete")
        if not required_forbidden.issubset(forbidden):
            raise ValueError("OpenClaw forbidden actions are incomplete")
        return cls(
            source_repo=source_repo,
            status=status,
            effective_from=effective_from,
            allowed_facts=allowed,
            forbidden_actions=forbidden,
            notice_path=notice_path,
            reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repo": self.source_repo,
            "status": self.status,
            "effective_from": self.effective_from,
            "allowed_facts": list(self.allowed_facts),
            "forbidden_actions": list(self.forbidden_actions),
            "notice_path": self.notice_path,
            "reason": self.reason,
        }


def _identity_array(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    result: list[str] = []
    for item in value:
        candidate = identity(item, field_name=field_name)
        if candidate not in result:
            result.append(candidate)
    if not result:
        raise ValueError(f"{field_name} cannot be empty")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SourceCatalog:
    entries: tuple[SourceEntry, ...]
    historical_boundaries: tuple[HistoricalBoundary, ...]
    metadata: Mapping[str, Any]
    source_path: str
    digest: str

    def by_capability(self) -> dict[str, tuple[SourceEntry, ...]]:
        grouped: dict[str, list[SourceEntry]] = defaultdict(list)
        for entry in self.entries:
            grouped[entry.capability].append(entry)
        return {
            key: tuple(sorted(value, key=_entry_order))
            for key, value in sorted(grouped.items())
        }

    def by_repository(self) -> dict[str, tuple[SourceEntry, ...]]:
        grouped: dict[str, list[SourceEntry]] = defaultdict(list)
        for entry in self.entries:
            grouped[entry.source_repo].append(entry)
        return {
            key: tuple(sorted(value, key=_entry_order))
            for key, value in sorted(grouped.items(), key=lambda item: item[0].casefold())
        }

    def active_entries(self) -> tuple[SourceEntry, ...]:
        return tuple(entry for entry in self.entries if entry.active)

    def inactive_entries(self) -> tuple[SourceEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.active)

    def entry(self, entry_id: str) -> SourceEntry:
        matches = [entry for entry in self.entries if entry.entry_id == entry_id]
        if len(matches) != 1:
            raise KeyError(entry_id)
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CATALOG_SCHEMA,
            "metadata": dict(self.metadata),
            "entries": [entry.to_dict() for entry in self.entries],
            "historical_boundaries": [
                boundary.to_dict() for boundary in self.historical_boundaries
            ],
        }


def _entry_order(entry: SourceEntry) -> tuple[int, str, str]:
    role_order = {
        SourceRole.PRIMARY: 0,
        SourceRole.SUPPLEMENTARY: 1,
        SourceRole.CONFORMANCE: 2,
        SourceRole.REFERENCE: 3,
        SourceRole.EXPERIMENTAL: 4,
        SourceRole.DEFERRED: 5,
        SourceRole.REJECTED: 6,
    }
    return role_order[entry.role], entry.source_repo.casefold(), entry.entry_id


@dataclass(frozen=True, slots=True)
class CatalogLoadResult:
    catalog: SourceCatalog | None
    section: AuditSection


class CatalogLoader:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()

    def load(self, path: str | Path) -> CatalogLoadResult:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        candidate = candidate.resolve(strict=False)
        display = relative_path(self.project_root, candidate)
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        if not candidate.exists():
            findings.append(
                finding(
                    "catalog_missing",
                    "The source-custody catalog is missing.",
                    "catalog",
                    severity=Severity.BLOCKER,
                    disposition=Disposition.BLOCK_RELEASE,
                    path=display,
                    remediation="Add the machine-readable M3 source-custody catalog.",
                    default_path_impact="Release audit cannot determine source ownership.",
                )
            )
            return CatalogLoadResult(
                catalog=None,
                section=section(
                    "catalog",
                    metrics={"entries": 0, "loaded": False},
                    findings=findings,
                ),
            )
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            findings.append(
                finding(
                    "catalog_unreadable",
                    f"The source-custody catalog cannot be decoded: {exc}",
                    "catalog",
                    severity=Severity.BLOCKER,
                    disposition=Disposition.BLOCK_RELEASE,
                    path=display,
                    remediation="Repair the UTF-8 JSON catalog.",
                    default_path_impact="Release audit cannot establish source roles.",
                )
            )
            return CatalogLoadResult(
                catalog=None,
                section=section(
                    "catalog",
                    metrics={"entries": 0, "loaded": False},
                    findings=findings,
                ),
            )
        if not isinstance(payload, Mapping):
            findings.append(self._schema_finding(display, "catalog root must be an object"))
            payload = {}
        schema_value = payload.get("schema")
        if schema_value != CATALOG_SCHEMA:
            findings.append(
                self._schema_finding(
                    display,
                    f"catalog schema must be {CATALOG_SCHEMA!r}, got {schema_value!r}",
                )
            )
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            findings.append(self._schema_finding(display, "entries must be an array"))
            raw_entries = []
        entries: list[SourceEntry] = []
        for index, raw in enumerate(raw_entries):
            try:
                entries.append(SourceEntry.parse(raw))
            except (TypeError, ValueError) as exc:
                findings.append(
                    finding(
                        "catalog_entry_invalid",
                        f"Source catalog entry {index} is invalid: {exc}",
                        "catalog",
                        severity=Severity.BLOCKER,
                        disposition=Disposition.BLOCK_RELEASE,
                        path=display,
                        line=index + 1,
                        remediation="Correct the source entry schema and semantics.",
                        default_path_impact="The affected source decision is unauditable.",
                        attributes={"entry_index": index},
                    )
                )
        raw_boundaries = payload.get("historical_boundaries")
        if not isinstance(raw_boundaries, list):
            findings.append(
                self._schema_finding(display, "historical_boundaries must be an array")
            )
            raw_boundaries = []
        boundaries: list[HistoricalBoundary] = []
        for index, raw in enumerate(raw_boundaries):
            try:
                boundaries.append(HistoricalBoundary.parse(raw))
            except (TypeError, ValueError) as exc:
                findings.append(
                    finding(
                        "historical_boundary_invalid",
                        f"Historical boundary {index} is invalid: {exc}",
                        "catalog",
                        severity=Severity.BLOCKER,
                        disposition=Disposition.BLOCK_RELEASE,
                        path=display,
                        remediation="Restore the forward-only OpenClaw boundary record.",
                        default_path_impact="Forward source exclusion cannot be proven.",
                        attributes={"boundary_index": index},
                    )
                )
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            findings.append(self._schema_finding(display, "metadata must be an object"))
            metadata = {}
        normalized = {
            "schema": CATALOG_SCHEMA,
            "metadata": dict(metadata),
            "entries": [entry.to_dict() for entry in entries],
            "historical_boundaries": [
                boundary.to_dict() for boundary in boundaries
            ],
        }
        catalog = SourceCatalog(
            entries=tuple(entries),
            historical_boundaries=tuple(boundaries),
            metadata=dict(metadata),
            source_path=display,
            digest=stable_digest(normalized),
        )
        if self.switches.catalog:
            findings.extend(self._catalog_integrity(catalog))
        if self.switches.roles:
            findings.extend(self._role_integrity(catalog))
        evidence.append(
            Evidence(
                kind=EvidenceKind.CONFIG,
                path=display,
                excerpt_digest=catalog.digest,
                attributes={"entry_count": len(entries)},
            )
        )
        return CatalogLoadResult(
            catalog=catalog,
            section=section(
                "catalog",
                metrics={
                    "loaded": True,
                    "entries": len(entries),
                    "active": sum(entry.active for entry in entries),
                    "inactive": sum(not entry.active for entry in entries),
                    "capabilities": len(catalog.by_capability()),
                    "repositories": len(catalog.by_repository()),
                    "digest": catalog.digest,
                },
                findings=findings,
                evidence=evidence,
            ),
        )

    def _schema_finding(self, path: str, message: str) -> Finding:
        return finding(
            "catalog_schema_invalid",
            message,
            "catalog",
            severity=Severity.BLOCKER,
            disposition=Disposition.BLOCK_RELEASE,
            path=path,
            remediation="Correct the catalog schema before release.",
            default_path_impact="Source decisions cannot be normalized.",
        )

    def _catalog_integrity(self, catalog: SourceCatalog) -> list[Finding]:
        findings: list[Finding] = []
        ids: Counter[str] = Counter(entry.entry_id for entry in catalog.entries)
        for entry_id, count in ids.items():
            if count > 1:
                findings.append(
                    finding(
                        "catalog_entry_duplicate",
                        f"Entry id {entry_id!r} occurs {count} times.",
                        "catalog",
                        severity=Severity.BLOCKER,
                        disposition=Disposition.BLOCK_RELEASE,
                        attributes={"entry_id": entry_id, "count": count},
                        remediation="Give every source decision a unique stable entry id.",
                        default_path_impact="Role decisions are ambiguous.",
                    )
                )
        pair_counts = Counter(
            (entry.source_key, entry.capability, entry.role.value)
            for entry in catalog.entries
        )
        for (source_repo, capability, role), count in pair_counts.items():
            if count > 1:
                findings.append(
                    finding(
                        "catalog_source_capability_role_duplicate",
                        "The same source/capability/role decision appears more than once.",
                        "catalog",
                        severity=Severity.ERROR,
                        source_repo=source_repo,
                        capability=capability,
                        disposition=Disposition.DECLARE,
                        remediation="Merge duplicate role records into one decision.",
                        default_path_impact="Duplicate rows can conceal conflicting landing state.",
                        attributes={"role": role, "count": count},
                    )
                )
        repositories = {entry.source_repo for entry in catalog.entries}
        missing = sorted(
            REQUIRED_SOURCE_REPOSITORIES - repositories, key=str.casefold
        )
        for repository in missing:
            findings.append(
                finding(
                    "required_source_repository_missing",
                    f"Required source repository {repository!r} has no catalog row.",
                    "catalog",
                    severity=Severity.BLOCKER,
                    source_repo=repository,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Add a bounded active or inactive source decision.",
                    default_path_impact="The all-source freeze table is incomplete.",
                )
            )
        unexpected_openclaw = [
            entry for entry in catalog.entries if entry.source_key == "openclaw"
        ]
        if unexpected_openclaw:
            findings.append(
                finding(
                    "openclaw_forward_role_present",
                    "OpenClaw appears in the forward source-role catalog.",
                    "catalog",
                    severity=Severity.BLOCKER,
                    source_repo="openclaw",
                    disposition=Disposition.REMOVE,
                    remediation="Remove the role row and retain only the historical boundary.",
                    default_path_impact="Forward-excluded source has re-entered planning.",
                )
            )
        openclaw_boundaries = [
            item
            for item in catalog.historical_boundaries
            if item.source_repo.casefold() == "openclaw"
        ]
        if len(openclaw_boundaries) != 1:
            findings.append(
                finding(
                    "openclaw_historical_boundary_count",
                    "Exactly one OpenClaw historical boundary is required.",
                    "catalog",
                    severity=Severity.BLOCKER,
                    source_repo="openclaw",
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Keep one forward-exclusion boundary outside source roles.",
                    default_path_impact="Historical provenance and forward exclusion are ambiguous.",
                    attributes={"count": len(openclaw_boundaries)},
                )
            )
        for repository in RELATED_CLAUDE_REFERENCES:
            for entry in catalog.by_repository().get(repository, ()):
                if entry.role is not SourceRole.REFERENCE or entry.active:
                    findings.append(
                        finding(
                            "claude_related_repository_not_reference_only",
                            f"{repository} must remain inactive reference-only.",
                            "catalog",
                            severity=Severity.BLOCKER,
                            source_repo=repository,
                            capability=entry.capability,
                            disposition=Disposition.REMOVE,
                            remediation="Set the related analysis repository to inactive reference-only.",
                            default_path_impact="Analysis material would be misclassified as runtime source.",
                        )
                    )
        for entry in catalog.entries:
            if entry.active:
                findings.extend(self._active_path_findings(entry))
                if entry.license_status != "resolved":
                    findings.append(
                        finding(
                            "source_license_unresolved",
                            "Active source entry has no resolved release license decision.",
                            "catalog",
                            severity=Severity.BLOCKER,
                            source_repo=entry.source_repo,
                            capability=entry.capability,
                            disposition=Disposition.BLOCK_RELEASE,
                            remediation="Resolve the source license or remove the active role.",
                            default_path_impact=(
                                "Release attribution cannot authorize the active source."
                            ),
                            attributes={
                                "entry_id": entry.entry_id,
                                "license_id": entry.license_id,
                                "license_status": entry.license_status,
                            },
                        )
                    )
            else:
                findings.extend(self._inactive_reason_findings(entry))
        return findings

    def _active_path_findings(self, entry: SourceEntry) -> list[Finding]:
        findings: list[Finding] = []
        for category, paths in (
            ("target", entry.target_paths),
            ("test", entry.test_paths),
        ):
            for relative in paths:
                candidate = resolve_within(self.project_root, relative)
                if not candidate.exists():
                    findings.append(
                        finding(
                            f"active_{category}_missing",
                            f"Active source entry references missing {category} path {relative!r}.",
                            "catalog",
                            severity=Severity.BLOCKER,
                            source_repo=entry.source_repo,
                            capability=entry.capability,
                            path=relative,
                            disposition=Disposition.BLOCK_RELEASE,
                            remediation=f"Restore the declared {category} or correct the catalog.",
                            default_path_impact="An active source decision has no executable proof.",
                            attributes={"entry_id": entry.entry_id},
                        )
                    )
        if entry.license_status == "resolved" and entry.license_id.casefold() in {
            "unknown",
            "unresolved",
        }:
            findings.append(
                finding(
                    "license_resolution_contradiction",
                    "Resolved license status uses an unresolved license identifier.",
                    "catalog",
                    severity=Severity.ERROR,
                    source_repo=entry.source_repo,
                    capability=entry.capability,
                    disposition=Disposition.DECLARE,
                    remediation="Record the actual license identifier or unresolved status.",
                    default_path_impact="Notice generation would be misleading.",
                )
            )
        return findings

    def _inactive_reason_findings(self, entry: SourceEntry) -> list[Finding]:
        findings: list[Finding] = []
        if entry.landing_status not in INACTIVE_LANDINGS:
            findings.append(
                finding(
                    "inactive_landing_invalid",
                    "Inactive source has a production landing status.",
                    "catalog",
                    severity=Severity.BLOCKER,
                    source_repo=entry.source_repo,
                    capability=entry.capability,
                    disposition=Disposition.REMOVE,
                    remediation="Make the source active with proof or use an inactive landing.",
                    default_path_impact="Inactive source could conceal runtime custody.",
                )
            )
        if not any(
            phrase in entry.reason.casefold()
            for phrase in (
                "no runtime",
                "does not",
                "not selected",
                "conformance",
                "reference",
                "rejected",
                "deferred",
                "analysis",
            )
        ):
            findings.append(
                finding(
                    "inactive_reason_unbounded",
                    "Inactive source reason does not state its no-runtime boundary.",
                    "catalog",
                    severity=Severity.WARNING,
                    source_repo=entry.source_repo,
                    capability=entry.capability,
                    disposition=Disposition.DECLARE,
                    remediation="Explain why non-migration is intentional and who owns the capability.",
                    default_path_impact="No release block unless runtime reachability contradicts the row.",
                )
            )
        return findings

    def _role_integrity(self, catalog: SourceCatalog) -> list[Finding]:
        findings: list[Finding] = []
        for capability, entries in catalog.by_capability().items():
            active = [entry for entry in entries if entry.active]
            primary = [entry for entry in active if entry.primary]
            supplements = [entry for entry in active if entry.supplementary]
            if active and len(primary) != 1:
                findings.append(
                    finding(
                        "active_capability_primary_count",
                        f"Active capability {capability!r} has {len(primary)} primary sources.",
                        "roles",
                        severity=Severity.BLOCKER,
                        capability=capability,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Select exactly one primary implementation source.",
                        default_path_impact="Canonical source custody is ambiguous or absent.",
                        attributes={
                            "primary_sources": [
                                entry.source_repo for entry in primary
                            ],
                            "active_sources": [entry.source_repo for entry in active],
                        },
                    )
                )
            if len(supplements) > 2:
                findings.append(
                    finding(
                        "active_capability_supplement_limit",
                        f"Active capability {capability!r} has more than two supplements.",
                        "roles",
                        severity=Severity.BLOCKER,
                        capability=capability,
                        disposition=Disposition.REMOVE,
                        remediation="Retain at most two bounded supplementary sources.",
                        default_path_impact="Duplicated implementation custody increases freeze risk.",
                        attributes={
                            "supplementary_sources": [
                                entry.source_repo for entry in supplements
                            ]
                        },
                    )
                )
            owners = {
                entry.owner.casefold(): entry.owner
                for entry in active
                if entry.owner.casefold() not in {"none", "n/a"}
            }
            if active and not owners:
                findings.append(
                    finding(
                        "active_capability_owner_missing",
                        f"Active capability {capability!r} has no Zyra owner.",
                        "roles",
                        severity=Severity.BLOCKER,
                        capability=capability,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Declare the Zyra canonical implementation owner.",
                        default_path_impact="Release cannot route remediation or state responsibility.",
                    )
                )
            roles = Counter(entry.role for entry in entries)
            inactive_primary_like = [
                entry
                for entry in entries
                if not entry.active and entry.role.active_capable
            ]
            if inactive_primary_like:
                findings.append(
                    finding(
                        "inactive_implementation_role",
                        "Implementation role is marked inactive.",
                        "roles",
                        severity=Severity.BLOCKER,
                        capability=capability,
                        disposition=Disposition.DECLARE,
                        remediation="Use an inactive role or provide active landing evidence.",
                        default_path_impact="Source-role semantics are contradictory.",
                        attributes={
                            "entries": [entry.entry_id for entry in inactive_primary_like],
                            "role_counts": {
                                role.value: count for role, count in roles.items()
                            },
                        },
                    )
                )
        return findings


def merge_catalog_sections(*sections: AuditSection) -> AuditSection:
    findings: list[Finding] = []
    evidence: list[Evidence] = []
    metrics: dict[str, Any] = {}
    for item in sections:
        findings.extend(item.findings)
        evidence.extend(item.evidence)
        metrics[item.name] = dict(item.metrics)
    return section(
        "catalog_combined",
        metrics=metrics,
        findings=deduplicate_findings(findings),
        evidence=evidence,
    )


def repository_role_summary(catalog: SourceCatalog) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for repository, entries in catalog.by_repository().items():
        rows.append(
            {
                "source_repo": repository,
                "source_commits": sorted({entry.source_commit for entry in entries}),
                "source_languages": sorted(
                    {language for entry in entries for language in entry.source_languages}
                ),
                "roles": sorted({entry.role.value for entry in entries}),
                "landing_statuses": sorted(
                    {entry.landing_status.value for entry in entries}
                ),
                "active_capabilities": sorted(
                    entry.capability for entry in entries if entry.active
                ),
                "inactive_capabilities": sorted(
                    entry.capability for entry in entries if not entry.active
                ),
                "owners": sorted({entry.owner for entry in entries}),
                "license_ids": sorted({entry.license_id for entry in entries}),
            }
        )
    return rows
