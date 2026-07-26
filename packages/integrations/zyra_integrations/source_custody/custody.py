from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..ledger_store import InternalizationLedger
from .catalog import SourceCatalog
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
    content_digest,
    finding,
    normalize_repo_path,
    relative_path,
    resolve_within,
    section,
    stable_digest,
    text,
)
from .repository import RepositoryInventory


PACKAGE_ANNOTATION_SCHEMA = "zyra.package-source-annotation/v1"
VENDOR_MAP_SCHEMA = "zyra.vendor-map/v1"
NOTICE_SCHEMA = "zyra.third-party-notice/v1"
VENDOR_ROOT_SOURCE_ALIASES = {
    "vendor-runtimes/claude-code-runtime": "claude-code-best",
}
VENDOR_MAP_MARKER = re.compile(
    r"<!--\s*zyra-source-entry\s+"
    r"id=\"([^\"]+)\"\s+digest=\"([^\"]+)\"\s*-->",
    re.IGNORECASE,
)
VENDOR_BOUNDARY_MARKER = re.compile(
    r"<!--\s*zyra-historical-boundary\s+"
    r"source=\"([^\"]+)\"\s+status=\"([^\"]+)\"\s*-->",
    re.IGNORECASE,
)
NOTICE_MARKER = re.compile(
    r"<!--\s*zyra-notice\s+"
    r"source=\"([^\"]+)\"\s+commit=\"([^\"]+)\"\s+"
    r"license=\"([^\"]+)\"\s+status=\"([^\"]+)\"\s*-->",
    re.IGNORECASE,
)
PARENT_SOURCE_PATH = re.compile(
    r"(?i)(?:^|[/\\])\.\.[/\\](claude-code-best|browser-use|OpenHands|opencode|"
    r"agentscope|agent-framework|hermes-agent|langgraph|oh-my-pi|openclaw)(?:[/\\]|$)"
)
ABSOLUTE_SOURCE_PATH = re.compile(
    r"(?i)[A-Z]:[/\\][^\r\n'\"]*(claude-code-best|browser-use|OpenHands|"
    r"opencode|agentscope|agent-framework|hermes-agent|langgraph|oh-my-pi|openclaw)"
)
LEDGER_SOURCE_ALIASES: Mapping[str, str] = {
    "agent-framework": "agent-framework",
    "agentscope": "agentscope",
    "browser-use": "browser-use",
    "claude-code-best": "claude-code-best",
    "hermes-agent": "hermes-agent",
    "langgraph": "langgraph",
    "oh-my-pi": "oh-my-pi",
    "openclaw": "openclaw",
    "opencode": "opencode",
    "openhands": "OpenHands",
    "zyra": "zyra",
}


@dataclass(frozen=True, slots=True)
class PackageAnnotation:
    annotation_path: str
    package_path: str
    package_name: str
    entry_ids: tuple[str, ...]
    owners: tuple[str, ...]
    default_reachable: bool
    build_entry: str
    health_entry: str
    state_custody: tuple[str, ...]
    notes: str
    digest: str

    @classmethod
    def parse(
        cls,
        raw: Any,
        *,
        annotation_path: str,
        digest: str,
    ) -> "PackageAnnotation":
        if not isinstance(raw, Mapping):
            raise ValueError("package annotation must be an object")
        if raw.get("schema") != PACKAGE_ANNOTATION_SCHEMA:
            raise ValueError(f"schema must be {PACKAGE_ANNOTATION_SCHEMA}")
        package_path = normalize_repo_path(raw.get("package_path"))
        entry_ids = _identity_array(raw.get("source_entry_ids"), "source_entry_ids")
        owners = _string_array(raw.get("owners"), "owners")
        state_custody = _string_array(
            raw.get("state_custody") or [],
            "state_custody",
            allow_empty=True,
        )
        notes = text(raw.get("notes"), field_name="annotation notes")
        if len(notes) < 20:
            raise ValueError("annotation notes are too short")
        return cls(
            annotation_path=annotation_path,
            package_path=package_path,
            package_name=text(raw.get("package_name"), field_name="package_name"),
            entry_ids=entry_ids,
            owners=owners,
            default_reachable=bool(raw.get("default_reachable")),
            build_entry=text(
                raw.get("build_entry"), field_name="build_entry", allow_empty=True
            ),
            health_entry=text(
                raw.get("health_entry"), field_name="health_entry", allow_empty=True
            ),
            state_custody=state_custody,
            notes=notes,
            digest=digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "annotation_path": self.annotation_path,
            "package_path": self.package_path,
            "package_name": self.package_name,
            "source_entry_ids": list(self.entry_ids),
            "owners": list(self.owners),
            "default_reachable": self.default_reachable,
            "build_entry": self.build_entry,
            "health_entry": self.health_entry,
            "state_custody": list(self.state_custody),
            "notes": self.notes,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class VendorMapEntry:
    entry_id: str
    digest: str
    line: int


@dataclass(frozen=True, slots=True)
class NoticeEntry:
    source_repo: str
    source_commit: str
    license_id: str
    status: str
    line: int


@dataclass(frozen=True, slots=True)
class CustodyIndex:
    annotations: tuple[PackageAnnotation, ...]
    vendor_map_entries: tuple[VendorMapEntry, ...]
    notices: tuple[NoticeEntry, ...]
    ledger_entries: int
    ledger_sources: Mapping[str, int]
    ledger_target_paths: tuple[str, ...]
    digest: str

    def to_summary(self) -> dict[str, Any]:
        return {
            "package_annotations": len(self.annotations),
            "vendor_map_entries": len(self.vendor_map_entries),
            "notices": len(self.notices),
            "ledger_entries": self.ledger_entries,
            "ledger_sources": dict(self.ledger_sources),
            "ledger_target_paths": len(self.ledger_target_paths),
            "digest": self.digest,
        }


class CustodyAuditor:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()

    def audit(
        self,
        inventory: RepositoryInventory,
        catalog: SourceCatalog | None,
        *,
        vendor_map_path: str = "docs/vendor-map.md",
        notice_path: str = "third_party/NOTICE.md",
        ledger_path: str = (
            "packages/integrations/zyra_integrations/data/"
            "internalization_ledger_seed.json"
        ),
    ) -> tuple[CustodyIndex, AuditSection]:
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        annotations, annotation_findings = self._load_annotations(inventory)
        findings.extend(annotation_findings)
        vendor_entries, boundaries, vendor_findings = self._load_vendor_map(
            vendor_map_path
        )
        findings.extend(vendor_findings)
        notices, notice_findings = self._load_notices(notice_path)
        findings.extend(notice_findings)
        ledger_payload, ledger_findings = self._load_ledger(ledger_path)
        findings.extend(ledger_findings)
        if isinstance(ledger_payload, Mapping):
            try:
                ledger_payload = InternalizationLedger.from_dict(
                    dict(ledger_payload),
                    normalize_current_policy=True,
                ).to_dict()
            except (TypeError, ValueError):
                # _load_ledger already emitted the authoritative schema
                # finding. Keep auditing instead of replacing it with a
                # normalization exception.
                pass
        ledger_entries = (
            ledger_payload.get("entries") if isinstance(ledger_payload, Mapping) else []
        )
        if not isinstance(ledger_entries, list):
            ledger_entries = []
        ledger_sources = Counter(
            str(item.get("source_repo") or "")
            for item in ledger_entries
            if isinstance(item, Mapping)
        )
        ledger_targets = tuple(
            sorted(
                {
                    str(binding.get("target_path") or "").replace("\\", "/")
                    for item in ledger_entries
                    if isinstance(item, Mapping)
                    for binding in item.get("target_bindings") or []
                    if isinstance(binding, Mapping)
                    and str(binding.get("target_path") or "")
                }
            )
        )
        if self.switches.custody and catalog is not None:
            findings.extend(
                self._catalog_annotation_findings(catalog, annotations)
            )
            findings.extend(
                self._vendor_map_findings(catalog, vendor_entries, boundaries)
            )
            findings.extend(self._notice_findings(catalog, notices))
            findings.extend(self._ledger_findings(catalog, ledger_entries))
            findings.extend(self._vendor_tree_findings(inventory, catalog))
        digest_payload = {
            "annotations": [item.to_dict() for item in annotations],
            "vendor_map": [
                {
                    "entry_id": item.entry_id,
                    "digest": item.digest,
                    "line": item.line,
                }
                for item in vendor_entries
            ],
            "notices": [
                {
                    "source_repo": item.source_repo,
                    "source_commit": item.source_commit,
                    "license_id": item.license_id,
                    "status": item.status,
                    "line": item.line,
                }
                for item in notices
            ],
            "ledger_entries": len(ledger_entries),
            "ledger_sources": dict(sorted(ledger_sources.items())),
            "ledger_targets": ledger_targets,
        }
        index = CustodyIndex(
            annotations=tuple(annotations),
            vendor_map_entries=tuple(vendor_entries),
            notices=tuple(notices),
            ledger_entries=len(ledger_entries),
            ledger_sources=dict(sorted(ledger_sources.items())),
            ledger_target_paths=ledger_targets,
            digest=stable_digest(digest_payload),
        )
        for annotation in annotations:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.PACKAGE_ANNOTATION,
                    path=annotation.annotation_path,
                    excerpt_digest=annotation.digest,
                    attributes={"entry_ids": list(annotation.entry_ids)},
                )
            )
        if resolve_within(self.project_root, vendor_map_path).exists():
            evidence.append(
                Evidence(
                    kind=EvidenceKind.VENDOR_MAP,
                    path=vendor_map_path,
                    attributes={"entries": len(vendor_entries)},
                )
            )
        if resolve_within(self.project_root, notice_path).exists():
            evidence.append(
                Evidence(
                    kind=EvidenceKind.NOTICE,
                    path=notice_path,
                    attributes={"entries": len(notices)},
                )
            )
        if resolve_within(self.project_root, ledger_path).exists():
            evidence.append(
                Evidence(
                    kind=EvidenceKind.LEDGER,
                    path=ledger_path,
                    attributes={
                        "entries": len(ledger_entries),
                        "sources": dict(ledger_sources),
                    },
                )
            )
        return index, section(
            "custody",
            metrics=index.to_summary(),
            findings=findings,
            evidence=evidence,
        )

    def _load_annotations(
        self, inventory: RepositoryInventory
    ) -> tuple[list[PackageAnnotation], list[Finding]]:
        annotations: list[PackageAnnotation] = []
        findings: list[Finding] = []
        for record in inventory.files:
            if PurePosixPath(record.path).name != "zyra-source.json":
                continue
            try:
                payload = json.loads(
                    (self.project_root / record.path).read_text(encoding="utf-8")
                )
                annotation = PackageAnnotation.parse(
                    payload,
                    annotation_path=record.path,
                    digest=record.digest,
                )
                annotations.append(annotation)
                expected = PurePosixPath(record.path).parent.as_posix()
                if annotation.package_path != expected:
                    findings.append(
                        finding(
                            "package_annotation_path_mismatch",
                            "Package annotation declares a different package path.",
                            "custody",
                            severity=Severity.BLOCKER,
                            path=record.path,
                            disposition=Disposition.DECLARE,
                            remediation="Set package_path to the containing package directory.",
                            default_path_impact="Source decisions can be attached to the wrong package.",
                            attributes={
                                "declared": annotation.package_path,
                                "actual": expected,
                            },
                        )
                    )
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                findings.append(
                    finding(
                        "package_annotation_invalid",
                        f"Package source annotation is invalid: {exc}",
                        "custody",
                        severity=Severity.BLOCKER,
                        path=record.path,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Repair the package-level source annotation.",
                        default_path_impact="Package source custody cannot be established.",
                    )
                )
        return annotations, findings

    def _load_vendor_map(
        self, path: str
    ) -> tuple[list[VendorMapEntry], list[tuple[str, str, int]], list[Finding]]:
        candidate = resolve_within(self.project_root, path)
        if not candidate.exists():
            return [], [], [
                finding(
                    "vendor_map_missing",
                    "docs/vendor-map.md is missing.",
                    "custody",
                    severity=Severity.BLOCKER,
                    path=path,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Generate the human-readable source-role and landing map.",
                    default_path_impact="Reviewers cannot trace source decisions.",
                )
            ]
        try:
            source = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return [], [], [
                finding(
                    "vendor_map_unreadable",
                    f"Vendor map cannot be read: {exc}",
                    "custody",
                    severity=Severity.BLOCKER,
                    path=path,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Repair the UTF-8 vendor map.",
                    default_path_impact="Human source custody is unavailable.",
                )
            ]
        entries = [
            VendorMapEntry(
                entry_id=match.group(1),
                digest=match.group(2),
                line=source[: match.start()].count("\n") + 1,
            )
            for match in VENDOR_MAP_MARKER.finditer(source)
        ]
        boundaries = [
            (
                match.group(1),
                match.group(2),
                source[: match.start()].count("\n") + 1,
            )
            for match in VENDOR_BOUNDARY_MARKER.finditer(source)
        ]
        findings: list[Finding] = []
        if VENDOR_MAP_SCHEMA not in source:
            findings.append(
                finding(
                    "vendor_map_schema_marker_missing",
                    f"Vendor map does not identify schema {VENDOR_MAP_SCHEMA}.",
                    "custody",
                    severity=Severity.ERROR,
                    path=path,
                    disposition=Disposition.DECLARE,
                    remediation="Add the vendor-map schema marker.",
                    default_path_impact="Map parser/version compatibility is ambiguous.",
                )
            )
        return entries, boundaries, findings

    def _load_notices(
        self, path: str
    ) -> tuple[list[NoticeEntry], list[Finding]]:
        candidate = resolve_within(self.project_root, path)
        if not candidate.exists():
            return [], [
                finding(
                    "third_party_notice_missing",
                    "third_party/NOTICE.md is missing.",
                    "custody",
                    severity=Severity.BLOCKER,
                    path=path,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Create a source/commit/license/status notice index.",
                    default_path_impact="Release provenance and license status are incomplete.",
                )
            ]
        try:
            source = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return [], [
                finding(
                    "third_party_notice_unreadable",
                    f"Third-party notice cannot be read: {exc}",
                    "custody",
                    severity=Severity.BLOCKER,
                    path=path,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Repair the UTF-8 notice.",
                    default_path_impact="Release provenance cannot be verified.",
                )
            ]
        entries = [
            NoticeEntry(
                source_repo=match.group(1),
                source_commit=match.group(2),
                license_id=match.group(3),
                status=match.group(4),
                line=source[: match.start()].count("\n") + 1,
            )
            for match in NOTICE_MARKER.finditer(source)
        ]
        findings: list[Finding] = []
        if NOTICE_SCHEMA not in source:
            findings.append(
                finding(
                    "notice_schema_marker_missing",
                    f"NOTICE does not identify schema {NOTICE_SCHEMA}.",
                    "custody",
                    severity=Severity.ERROR,
                    path=path,
                    disposition=Disposition.DECLARE,
                    remediation="Add the NOTICE schema marker.",
                    default_path_impact="Notice parser/version compatibility is ambiguous.",
                )
            )
        return entries, findings

    def _load_ledger(
        self, path: str
    ) -> tuple[Mapping[str, Any], list[Finding]]:
        candidate = resolve_within(self.project_root, path)
        if not candidate.exists():
            return {}, [
                finding(
                    "internalization_ledger_missing",
                    "Bundled internalization ledger is missing.",
                    "custody",
                    severity=Severity.BLOCKER,
                    path=path,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Restore the protected ledger seed.",
                    default_path_impact="Historical source-to-target facts cannot be reconciled.",
                )
            ]
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {}, [
                finding(
                    "internalization_ledger_unreadable",
                    f"Bundled internalization ledger cannot be decoded: {exc}",
                    "custody",
                    severity=Severity.BLOCKER,
                    path=path,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Repair the protected JSON ledger.",
                    default_path_impact="Historical source-to-target facts cannot be reconciled.",
                )
            ]
        if not isinstance(payload, Mapping):
            return {}, [
                finding(
                    "internalization_ledger_schema_invalid",
                    "Bundled ledger root must be an object.",
                    "custody",
                    severity=Severity.BLOCKER,
                    path=path,
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation="Restore the ledger object schema.",
                    default_path_impact="Historical source-to-target facts cannot be reconciled.",
                )
            ]
        return payload, []

    def _catalog_annotation_findings(
        self,
        catalog: SourceCatalog,
        annotations: Sequence[PackageAnnotation],
    ) -> list[Finding]:
        findings: list[Finding] = []
        entry_index = {entry.entry_id: entry for entry in catalog.entries}
        annotation_by_package = {
            annotation.package_path: annotation for annotation in annotations
        }
        annotated_ids: dict[str, list[PackageAnnotation]] = defaultdict(list)
        for annotation in annotations:
            package_path = resolve_within(self.project_root, annotation.package_path)
            if not package_path.exists():
                findings.append(
                    finding(
                        "annotated_package_missing",
                        "Package annotation refers to a missing package path.",
                        "custody",
                        severity=Severity.BLOCKER,
                        path=annotation.annotation_path,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Restore the package or remove the stale annotation.",
                        default_path_impact="Source decision has no package landing.",
                        attributes={"package_path": annotation.package_path},
                    )
                )
            for entry_id in annotation.entry_ids:
                annotated_ids[entry_id].append(annotation)
                if entry_id not in entry_index:
                    findings.append(
                        finding(
                            "annotation_unknown_source_entry",
                            f"Package annotation references unknown entry {entry_id!r}.",
                            "custody",
                            severity=Severity.BLOCKER,
                            path=annotation.annotation_path,
                            disposition=Disposition.DECLARE,
                            remediation="Use a valid catalog entry id.",
                            default_path_impact="Package provenance cannot be joined.",
                        )
                    )
            expected_owners = {
                entry_index[entry_id].owner
                for entry_id in annotation.entry_ids
                if entry_id in entry_index
            }
            missing_owners = expected_owners - set(annotation.owners)
            if missing_owners:
                findings.append(
                    finding(
                        "annotation_owner_mismatch",
                        "Package annotation omits catalog owner(s).",
                        "custody",
                        severity=Severity.ERROR,
                        path=annotation.annotation_path,
                        disposition=Disposition.DECLARE,
                        remediation="Align package and catalog owner declarations.",
                        default_path_impact="Remediation routing could target the wrong owner.",
                        attributes={"missing_owners": sorted(missing_owners)},
                    )
                )
        for entry in catalog.active_entries():
            expected_packages = {
                package_root(path)
                for path in entry.target_paths
                if package_root(path)
            }
            for package in expected_packages:
                annotation = annotation_by_package.get(package)
                if annotation is None:
                    findings.append(
                        finding(
                            "active_package_annotation_missing",
                            "Active source landing package has no source annotation.",
                            "custody",
                            severity=Severity.BLOCKER,
                            source_repo=entry.source_repo,
                            capability=entry.capability,
                            path=package,
                            disposition=Disposition.DECLARE,
                            remediation="Add zyra-source.json to the formal package.",
                            default_path_impact="Release package loses source-role custody metadata.",
                            attributes={"entry_id": entry.entry_id},
                        )
                    )
                elif entry.entry_id not in annotation.entry_ids:
                    findings.append(
                        finding(
                            "active_entry_not_annotated",
                            "Active source entry is absent from its package annotation.",
                            "custody",
                            severity=Severity.BLOCKER,
                            source_repo=entry.source_repo,
                            capability=entry.capability,
                            path=annotation.annotation_path,
                            disposition=Disposition.DECLARE,
                            remediation="Add the source entry id to the package annotation.",
                            default_path_impact="Catalog and package provenance disagree.",
                            attributes={"entry_id": entry.entry_id},
                        )
                    )
        for entry_id, selected in annotated_ids.items():
            if len(selected) > 1:
                entry = entry_index.get(entry_id)
                target_packages = (
                    {
                        package_root(path)
                        for path in entry.target_paths
                        if package_root(path)
                    }
                    if entry
                    else set()
                )
                actual = {item.package_path for item in selected}
                if not actual.issubset(target_packages):
                    findings.append(
                        finding(
                            "source_entry_annotation_spread",
                            "Source entry is attached to undeclared packages.",
                            "custody",
                            severity=Severity.ERROR,
                            source_repo=entry.source_repo if entry else "",
                            capability=entry.capability if entry else "",
                            disposition=Disposition.DECLARE,
                            remediation="Restrict annotations to declared landing packages.",
                            default_path_impact="Source decision scope is broader than its catalog.",
                            attributes={
                                "entry_id": entry_id,
                                "packages": sorted(actual),
                            },
                        )
                    )
        return findings

    def _vendor_map_findings(
        self,
        catalog: SourceCatalog,
        entries: Sequence[VendorMapEntry],
        boundaries: Sequence[tuple[str, str, int]],
    ) -> list[Finding]:
        findings: list[Finding] = []
        mapped = {item.entry_id: item for item in entries}
        for entry in catalog.entries:
            expected_digest = stable_digest(entry.to_dict())
            item = mapped.get(entry.entry_id)
            if item is None:
                findings.append(
                    finding(
                        "vendor_map_entry_missing",
                        "Catalog source entry is missing from docs/vendor-map.md.",
                        "custody",
                        severity=Severity.BLOCKER,
                        source_repo=entry.source_repo,
                        capability=entry.capability,
                        path="docs/vendor-map.md",
                        disposition=Disposition.DECLARE,
                        remediation="Add the generated entry marker and human-readable row.",
                        default_path_impact="Human map is incomplete.",
                        attributes={"entry_id": entry.entry_id},
                    )
                )
            elif item.digest != expected_digest:
                findings.append(
                    finding(
                        "vendor_map_entry_stale",
                        "Vendor-map entry digest does not match the catalog.",
                        "custody",
                        severity=Severity.BLOCKER,
                        source_repo=entry.source_repo,
                        capability=entry.capability,
                        path="docs/vendor-map.md",
                        line=item.line,
                        disposition=Disposition.DECLARE,
                        remediation="Regenerate the map from the catalog.",
                        default_path_impact="Human source role can contradict machine policy.",
                        attributes={"entry_id": entry.entry_id},
                    )
                )
        unknown = sorted(set(mapped) - {entry.entry_id for entry in catalog.entries})
        for entry_id in unknown:
            findings.append(
                finding(
                    "vendor_map_unknown_entry",
                    f"Vendor map contains unknown entry {entry_id!r}.",
                    "custody",
                    severity=Severity.ERROR,
                    path="docs/vendor-map.md",
                    disposition=Disposition.REMOVE,
                    remediation="Remove stale map rows.",
                    default_path_impact="Human map claims a non-authoritative source decision.",
                )
            )
        openclaw = [
            item
            for item in boundaries
            if item[0].casefold() == "openclaw"
            and item[1] == "excluded_forward_only"
        ]
        if len(openclaw) != 1:
            findings.append(
                finding(
                    "vendor_map_openclaw_boundary_missing",
                    "Vendor map must contain one OpenClaw historical-boundary marker.",
                    "custody",
                    severity=Severity.BLOCKER,
                    source_repo="openclaw",
                    path="docs/vendor-map.md",
                    disposition=Disposition.DECLARE,
                    remediation="Record historical provenance without a source-role row.",
                    default_path_impact="Forward exclusion is not visible in the freeze map.",
                )
            )
        return findings

    def _notice_findings(
        self,
        catalog: SourceCatalog,
        notices: Sequence[NoticeEntry],
    ) -> list[Finding]:
        findings: list[Finding] = []
        by_source: dict[str, list[NoticeEntry]] = defaultdict(list)
        for notice in notices:
            by_source[notice.source_repo.casefold()].append(notice)
        required = {
            entry.source_repo.casefold(): entry.source_repo for entry in catalog.entries
        }
        required["openclaw"] = "openclaw"
        for key, display in required.items():
            selected = by_source.get(key, [])
            if not selected:
                findings.append(
                    finding(
                        "source_notice_missing",
                        f"Source repository {display!r} has no NOTICE marker.",
                        "custody",
                        severity=Severity.BLOCKER,
                        source_repo=display,
                        path="third_party/NOTICE.md",
                        disposition=Disposition.DECLARE,
                        remediation="Record source revision, license id, and resolution status.",
                        default_path_impact="Release attribution is incomplete.",
                    )
                )
                continue
            if len(selected) > 1:
                findings.append(
                    finding(
                        "source_notice_duplicate",
                        f"Source repository {display!r} has duplicate NOTICE markers.",
                        "custody",
                        severity=Severity.ERROR,
                        source_repo=display,
                        path="third_party/NOTICE.md",
                        disposition=Disposition.DECLARE,
                        remediation="Consolidate source notice status.",
                        default_path_impact="License resolution is ambiguous.",
                    )
                )
            catalog_entries = [
                entry for entry in catalog.entries if entry.source_key == key
            ]
            commits = {entry.source_commit for entry in catalog_entries}
            for notice in selected:
                if commits and notice.source_commit not in commits:
                    findings.append(
                        finding(
                            "source_notice_commit_mismatch",
                            f"NOTICE revision for {display!r} does not match the catalog.",
                            "custody",
                            severity=Severity.BLOCKER,
                            source_repo=display,
                            path="third_party/NOTICE.md",
                            line=notice.line,
                            disposition=Disposition.DECLARE,
                            remediation="Update NOTICE to the frozen source commit.",
                            default_path_impact="Attribution points to different source bytes.",
                        )
                    )
                if notice.status not in {"resolved", "unresolved", "historical_only"}:
                    findings.append(
                        finding(
                            "source_notice_status_invalid",
                            f"NOTICE status {notice.status!r} is unsupported.",
                            "custody",
                            severity=Severity.ERROR,
                            source_repo=display,
                            path="third_party/NOTICE.md",
                            line=notice.line,
                            disposition=Disposition.DECLARE,
                            remediation="Use resolved, unresolved, or historical_only.",
                            default_path_impact="License policy cannot interpret the notice.",
                        )
                    )
        return findings

    def _ledger_findings(
        self,
        catalog: SourceCatalog,
        ledger_entries: Sequence[Any],
    ) -> list[Finding]:
        findings: list[Finding] = []
        catalog_sources = {entry.source_key for entry in catalog.entries}
        for index, raw in enumerate(ledger_entries):
            if not isinstance(raw, Mapping):
                findings.append(
                    finding(
                        "ledger_entry_not_object",
                        "Historical ledger contains a non-object entry.",
                        "custody",
                        severity=Severity.BLOCKER,
                        path=(
                            "packages/integrations/zyra_integrations/data/"
                            "internalization_ledger_seed.json"
                        ),
                        line=index + 1,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Repair the protected ledger schema.",
                        default_path_impact="Historical custody cannot be normalized.",
                    )
                )
                continue
            source = str(raw.get("source_repo") or "").strip()
            canonical = LEDGER_SOURCE_ALIASES.get(source.casefold())
            if canonical is None:
                findings.append(
                    finding(
                        "ledger_source_unknown",
                        f"Ledger source repository {source!r} is not recognized.",
                        "custody",
                        severity=Severity.ERROR,
                        source_repo=source,
                        disposition=Disposition.DECLARE,
                        remediation="Normalize the historical source identity without rewriting facts.",
                        default_path_impact="Ledger cannot join the freeze source map.",
                        attributes={"ledger_id": raw.get("ledger_id")},
                    )
                )
            elif canonical.casefold() != "openclaw" and canonical.casefold() not in catalog_sources:
                findings.append(
                    finding(
                        "ledger_source_not_cataloged",
                        f"Ledger source {canonical!r} has no forward catalog decision.",
                        "custody",
                        severity=Severity.BLOCKER,
                        source_repo=canonical,
                        disposition=Disposition.DECLARE,
                        remediation="Add an active or inactive source decision.",
                        default_path_impact="Historical source facts are outside freeze policy.",
                    )
                )
            for value_path, value in _walk_strings(raw):
                normalized = value.replace("\\", "/")
                parent = PARENT_SOURCE_PATH.search(normalized)
                absolute = ABSOLUTE_SOURCE_PATH.search(value)
                if parent or absolute:
                    historical_source_evidence = (
                        value_path.startswith("source_evidence")
                        or value_path == "source_path"
                    )
                    if not historical_source_evidence:
                        findings.append(
                            finding(
                                "ledger_runtime_source_path",
                                "Ledger runtime/target/config value refers to a root source repository.",
                                "custody",
                                severity=Severity.BLOCKER,
                                source_repo=(
                                    parent.group(1) if parent else absolute.group(1)
                                ),
                                disposition=Disposition.BLOCK_RELEASE,
                                remediation="Keep source paths in provenance fields only.",
                                default_path_impact="Ledger can authorize a non-clean runtime path.",
                                attributes={
                                    "ledger_id": raw.get("ledger_id"),
                                    "field": value_path,
                                },
                            )
                        )
            notice = raw.get("license_notice")
            if isinstance(notice, Mapping):
                notice_path = str(notice.get("notice_path") or "")
                if notice_path and not resolve_within(
                    self.project_root, notice_path
                ).exists():
                    findings.append(
                        finding(
                            "ledger_notice_target_missing",
                            "Ledger license notice path is missing.",
                            "custody",
                            severity=Severity.ERROR,
                            source_repo=source,
                            path=notice_path,
                            disposition=Disposition.DECLARE,
                            remediation="Restore the shared NOTICE file.",
                            default_path_impact="Historical attribution points to a missing file.",
                            attributes={"ledger_id": raw.get("ledger_id")},
                        )
                    )
            for binding in raw.get("target_bindings") or []:
                if not isinstance(binding, Mapping):
                    continue
                target = str(binding.get("target_path") or "").replace("\\", "/")
                if not target:
                    continue
                if not resolve_within(self.project_root, target).exists():
                    statuses = binding.get("must_exist_for_statuses") or []
                    current_status = str(raw.get("main_path_status") or "")
                    if current_status in statuses:
                        findings.append(
                            finding(
                                "ledger_active_target_missing",
                                "Ledger active target binding is missing.",
                                "custody",
                                severity=Severity.BLOCKER,
                                source_repo=source,
                                path=target,
                                disposition=Disposition.BLOCK_RELEASE,
                                remediation="Restore target or advance the historical ledger honestly.",
                                default_path_impact="Claimed active internalization has no target.",
                                attributes={"ledger_id": raw.get("ledger_id")},
                            )
                        )
        return findings

    def _vendor_tree_findings(
        self,
        inventory: RepositoryInventory,
        catalog: SourceCatalog,
    ) -> list[Finding]:
        findings: list[Finding] = []
        vendor_roots = {
            "/".join(PurePosixPath(item.path).parts[:2])
            for item in inventory.files
            if item.path.startswith(("vendor/", "vendor-runtimes/"))
            and len(PurePosixPath(item.path).parts) >= 2
        }
        active_targets = {
            target
            for entry in catalog.active_entries()
            for target in entry.target_paths
        }
        for target in active_targets:
            if target.startswith(("vendor/", "vendor-runtimes/")):
                findings.append(
                    finding(
                        "active_target_in_vendor_tree",
                        "Active source entry lands inside a vendor/source-pool tree.",
                        "custody",
                        severity=Severity.BLOCKER,
                        path=target,
                        disposition=Disposition.ABSORB,
                        remediation="Move owned implementation into a formal Zyra module.",
                        default_path_impact="Vendor-like source cannot be counted as deep internalization.",
                    )
                )
        for root in sorted(vendor_roots):
            if root == "vendor/README.md":
                continue
            source_name = VENDOR_ROOT_SOURCE_ALIASES.get(
                root,
                PurePosixPath(root).name.casefold(),
            )
            matching = [
                entry
                for entry in catalog.entries
                if entry.source_key.replace("_", "-") in source_name
                or source_name in entry.source_key.replace("_", "-")
            ]
            if not matching:
                findings.append(
                    finding(
                        "vendor_tree_uncataloged",
                        "Vendor/runtime-source tree has no source-custody decision.",
                        "custody",
                        severity=Severity.ERROR,
                        path=root,
                        disposition=Disposition.DECLARE,
                        remediation="Declare debt/status and ensure it is not a default runtime owner.",
                        default_path_impact="Opaque source debt is absent from the freeze map.",
                    )
                )
        return findings


def package_root(path: str) -> str:
    parts = PurePosixPath(path).parts
    if len(parts) >= 2 and parts[0] == "apps":
        return "/".join(parts[:2])
    if len(parts) >= 3 and parts[0] == "packages":
        return "/".join(parts[:3])
    if len(parts) >= 2 and parts[0] == "skills":
        return "/".join(parts[:2])
    return ""


def _identity_array(value: Any, field_name: str) -> tuple[str, ...]:
    from .model import identity

    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a non-empty array")
    return tuple(
        sorted({identity(item, field_name=field_name) for item in value})
    )


def _string_array(
    value: Any, field_name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    result = tuple(
        sorted({text(item, field_name=field_name) for item in value})
    )
    if not result and not allow_empty:
        raise ValueError(f"{field_name} cannot be empty")
    return result


def _walk_strings(
    value: Any,
    prefix: str = "",
) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_strings(nested, child)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            child = f"{prefix}[{index}]"
            yield from _walk_strings(nested, child)
