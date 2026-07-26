from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    AuditCatalog,
    AuditSection,
    CatalogError,
    Disposition,
    EvidencePointer,
    Finding,
    OwnerContract,
    RequirementEvidence,
    RuleSwitches,
    Severity,
    content_digest,
    deduplicate_findings,
    finding,
    identity,
    mapping,
    requirement_id,
    required_text,
    repository_path,
    resolve_within,
    section,
    stable_digest,
)


CATALOG_SCHEMA = "zyra.state-owner-evidence-catalog/v1"
DEFAULT_CATALOG_PATH = (
    "packages/evaluation/zyra_evaluation/data/state_owner_evidence_catalog.json"
)
REQUIRED_DOMAINS = (
    "session_event_projection",
    "permission",
    "memory_compact",
    "scheduler_recovery",
    "artifact",
    "worker_route",
    "graph_checkpoint",
    "provider_credential_failover",
    "mcp_plugin_registry",
    "gateway_lease_busy",
    "terminal_browser_session",
)
REQUIRED_REQUIREMENTS = (
    "REQ-CLOSE-01",
    "REQ-MEM-01",
    "REQ-TOPO-01",
    "REQ-COMM-01",
    "REQ-EDGE-01",
    "REQ-FAULT-01",
    "REQ-TRACE-01",
    "REQ-DOC-01",
    "SCORE-LOOP",
    "SCORE-ORG",
    "SCORE-TASKS",
    "SCORE-SCENE",
    "SCORE-VALUE",
    "SCORE-UX",
    "SCORE-NOISE",
    "SCORE-ALGO",
    "SCORE-ROBUST",
    "SCORE-EFF",
    "SCORE-COMPAT",
)
MATRIX_ROW_PATTERN = re.compile(
    r"^\|\s*((?:REQ|SCORE)-[A-Z0-9-]+)\s*\|",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class CatalogLoadResult:
    catalog: AuditCatalog | None
    section: AuditSection
    raw_digest: str


class CatalogLoader:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()

    def load(
        self,
        catalog_path: str = DEFAULT_CATALOG_PATH,
        *,
        requirement_matrix_path: str | Path | None = None,
    ) -> CatalogLoadResult:
        findings: list[Finding] = []
        evidence: list[EvidencePointer] = []
        metrics: dict[str, Any] = {
            "catalog_path": catalog_path,
            "schema": CATALOG_SCHEMA,
            "owner_count": 0,
            "requirement_count": 0,
            "required_domain_count": len(REQUIRED_DOMAINS),
            "required_requirement_count": len(REQUIRED_REQUIREMENTS),
            "matrix_requirement_count": 0,
        }
        try:
            selected_path = resolve_within(self.root, catalog_path)
        except CatalogError as exc:
            findings.append(
                self._catalog_failure(
                    "catalog_path_invalid",
                    str(exc),
                    path=catalog_path,
                )
            )
            return CatalogLoadResult(
                catalog=None,
                section=section("state_catalog", metrics=metrics, findings=findings),
                raw_digest="",
            )
        if not selected_path.is_file():
            findings.append(
                self._catalog_failure(
                    "catalog_missing",
                    "State-owner evidence catalog is missing.",
                    path=catalog_path,
                    remediation=(
                        "Restore the version-controlled M3 state-owner catalog "
                        "before running release or freeze validation."
                    ),
                )
            )
            return CatalogLoadResult(
                catalog=None,
                section=section("state_catalog", metrics=metrics, findings=findings),
                raw_digest="",
            )
        try:
            raw_bytes = selected_path.read_bytes()
            raw_digest = content_digest(raw_bytes)
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            findings.append(
                self._catalog_failure(
                    "catalog_unreadable",
                    f"State-owner catalog cannot be parsed: {exc}",
                    path=catalog_path,
                )
            )
            return CatalogLoadResult(
                catalog=None,
                section=section("state_catalog", metrics=metrics, findings=findings),
                raw_digest="",
            )
        if not self.switches.catalog:
            return CatalogLoadResult(
                catalog=self._unsafe_parse(payload, raw_digest),
                section=section(
                    "state_catalog",
                    metrics={**metrics, "rule_enabled": False},
                    evidence=(
                        EvidencePointer(
                            kind="catalog",
                            path=catalog_path,
                            digest=raw_digest,
                            attributes={"validation_disabled": True},
                        ),
                    ),
                ),
                raw_digest=raw_digest,
            )
        try:
            catalog = self._parse(payload, raw_digest)
        except (CatalogError, TypeError, ValueError) as exc:
            findings.append(
                self._catalog_failure(
                    "catalog_schema_invalid",
                    f"State-owner catalog violates its schema: {exc}",
                    path=catalog_path,
                )
            )
            return CatalogLoadResult(
                catalog=None,
                section=section("state_catalog", metrics=metrics, findings=findings),
                raw_digest=raw_digest,
            )
        metrics.update(
            {
                "owner_count": len(catalog.owners),
                "requirement_count": len(catalog.requirements),
                "default_entry_count": sum(
                    entry.default
                    for owner_contract in catalog.owners
                    for entry in owner_contract.entries
                ),
                "event_mutation_count": sum(
                    len(owner_contract.events) for owner_contract in catalog.owners
                ),
                "catalog_digest": catalog.catalog_digest,
                "raw_digest": raw_digest,
                "rule_enabled": True,
            }
        )
        findings.extend(self._validate_catalog(catalog))
        matrix_ids, matrix_evidence, matrix_findings = self._matrix_ids(
            requirement_matrix_path
        )
        evidence.extend(matrix_evidence)
        findings.extend(matrix_findings)
        metrics["matrix_requirement_count"] = len(matrix_ids)
        if matrix_ids:
            findings.extend(self._validate_matrix_parity(catalog, matrix_ids))
        evidence.append(
            EvidencePointer(
                kind="catalog",
                path=catalog_path,
                digest=raw_digest,
                attributes={
                    "catalog_digest": catalog.catalog_digest,
                    "owners": len(catalog.owners),
                    "requirements": len(catalog.requirements),
                },
            )
        )
        return CatalogLoadResult(
            catalog=catalog,
            section=section(
                "state_catalog",
                metrics=metrics,
                findings=findings,
                evidence=evidence,
                records=(
                    {
                        "domains": [item.domain for item in catalog.owners],
                        "requirements": [
                            item.requirement_id for item in catalog.requirements
                        ],
                    },
                ),
            ),
            raw_digest=raw_digest,
        )

    def _parse(self, raw: Any, raw_digest: str) -> AuditCatalog:
        payload = mapping(raw, field_name="catalog")
        schema = required_text(payload.get("schema"), field_name="schema")
        if schema != CATALOG_SCHEMA:
            raise CatalogError(
                f"schema must be {CATALOG_SCHEMA}, received {schema!r}"
            )
        owners_raw = self._array(payload.get("owners"), "owners")
        requirements_raw = self._array(
            payload.get("requirements"),
            "requirements",
        )
        owners = tuple(
            OwnerContract.parse(item, field_name=f"owners[{index}]")
            for index, item in enumerate(owners_raw)
        )
        requirements = tuple(
            RequirementEvidence.parse(
                item,
                field_name=f"requirements[{index}]",
            )
            for index, item in enumerate(requirements_raw)
        )
        required_domains = tuple(
            identity(item, field_name=f"required_domains[{index}]")
            for index, item in enumerate(
                self._array(payload.get("required_domains"), "required_domains")
            )
        )
        required_requirements = tuple(
            requirement_id(
                item,
                field_name=f"required_requirements[{index}]",
            )
            for index, item in enumerate(
                self._array(
                    payload.get("required_requirements"),
                    "required_requirements",
                )
            )
        )
        material = {
            "schema": schema,
            "required_domains": list(required_domains),
            "required_requirements": list(required_requirements),
            "owners": [item.to_dict() for item in owners],
            "requirements": [item.to_dict() for item in requirements],
            "raw_digest": raw_digest,
        }
        return AuditCatalog(
            schema=schema,
            owners=owners,
            requirements=requirements,
            required_domains=required_domains,
            required_requirements=required_requirements,
            catalog_digest=stable_digest(material),
        )

    def _unsafe_parse(self, raw: Any, raw_digest: str) -> AuditCatalog | None:
        try:
            return self._parse(raw, raw_digest)
        except (CatalogError, TypeError, ValueError):
            return None

    def _validate_catalog(self, catalog: AuditCatalog) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        findings.extend(
            self._duplicates(
                (item.domain for item in catalog.owners),
                code="catalog_duplicate_state_domain",
                message="State-owner catalog contains duplicate state domains.",
            )
        )
        findings.extend(
            self._duplicates(
                (
                    entry.entry_id
                    for owner_contract in catalog.owners
                    for entry in owner_contract.entries
                ),
                code="catalog_duplicate_entry_id",
                message="Default-entry identities must be globally unique.",
            )
        )
        findings.extend(
            self._duplicates(
                (
                    event.link_id
                    for owner_contract in catalog.owners
                    for event in owner_contract.events
                ),
                code="catalog_duplicate_event_link",
                message="Event-to-mutation link identities must be globally unique.",
            )
        )
        findings.extend(
            self._duplicates(
                (item.requirement_id for item in catalog.requirements),
                code="catalog_duplicate_requirement",
                message="Requirement evidence identities must be globally unique.",
            )
        )
        expected_domains = set(REQUIRED_DOMAINS)
        declared_required_domains = set(catalog.required_domains)
        actual_domains = set(catalog.owner_by_domain)
        for domain in sorted(expected_domains - declared_required_domains):
            findings.append(
                self._coverage_failure(
                    "catalog_required_domain_policy_missing",
                    f"Catalog policy omits required state domain {domain}.",
                    domain=domain,
                )
            )
        for domain in sorted(declared_required_domains - expected_domains):
            findings.append(
                finding(
                    "catalog_required_domain_policy_extra",
                    f"Catalog declares an additional required state domain: {domain}.",
                    "catalog",
                    severity=Severity.WARNING,
                    domain=domain,
                    owner_unit="M3-01A",
                    remediation=(
                        "Confirm the new canonical state boundary in the parent "
                        "unit before making it a release requirement."
                    ),
                )
            )
        for domain in sorted(declared_required_domains - actual_domains):
            findings.append(
                self._coverage_failure(
                    "catalog_required_domain_missing",
                    f"Required state domain has no owner contract: {domain}.",
                    domain=domain,
                )
            )
        expected_requirements = set(REQUIRED_REQUIREMENTS)
        declared_required_requirements = set(catalog.required_requirements)
        actual_requirements = set(catalog.requirement_by_id)
        for item in sorted(expected_requirements - declared_required_requirements):
            findings.append(
                self._requirement_failure(
                    "catalog_requirement_policy_missing",
                    f"Catalog policy omits competition identity {item}.",
                    requirement=item,
                )
            )
        for item in sorted(declared_required_requirements - actual_requirements):
            findings.append(
                self._requirement_failure(
                    "catalog_requirement_evidence_missing",
                    f"Required competition identity has no evidence row: {item}.",
                    requirement=item,
                )
            )
        entry_ids = set(catalog.entry_by_id)
        event_ids = set(catalog.event_by_id)
        for requirement in catalog.requirements:
            for domain in sorted(
                set(requirement.owner_domains) - set(catalog.owner_by_domain)
            ):
                findings.append(
                    self._requirement_failure(
                        "catalog_requirement_owner_unknown",
                        (
                            f"{requirement.requirement_id} refers to unknown "
                            f"state domain {domain}."
                        ),
                        requirement=requirement.requirement_id,
                        attributes={"domain": domain},
                    )
                )
            for entry_id in sorted(
                set(requirement.default_entry_ids) - entry_ids
            ):
                findings.append(
                    self._requirement_failure(
                        "catalog_requirement_entry_unknown",
                        (
                            f"{requirement.requirement_id} refers to unknown "
                            f"default entry {entry_id}."
                        ),
                        requirement=requirement.requirement_id,
                        attributes={"entry_id": entry_id},
                    )
                )
            for event_id in sorted(set(requirement.event_links) - event_ids):
                findings.append(
                    self._requirement_failure(
                        "catalog_requirement_event_unknown",
                        (
                            f"{requirement.requirement_id} refers to unknown "
                            f"event link {event_id}."
                        ),
                        requirement=requirement.requirement_id,
                        attributes={"event_link": event_id},
                    )
                )
        findings.extend(self._validate_owner_aliases(catalog))
        findings.extend(self._validate_language_precision(catalog))
        return deduplicate_findings(findings)

    def _validate_owner_aliases(
        self,
        catalog: AuditCatalog,
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        by_owner: dict[str, list[OwnerContract]] = defaultdict(list)
        for owner_contract in catalog.owners:
            by_owner[owner_contract.owner.key].append(owner_contract)
        for owner_key, contracts in sorted(by_owner.items()):
            if len(contracts) <= 1:
                continue
            shared_groups = {
                item.shared_owner_group for item in contracts if item.shared_owner_group
            }
            if len(shared_groups) == 1 and all(
                item.shared_owner_group for item in contracts
            ):
                continue
            findings.append(
                self._coverage_failure(
                    "catalog_owner_reused_without_group",
                    (
                        f"Canonical owner {owner_key} is assigned to multiple "
                        "state domains without one shared-owner group."
                    ),
                    domain=",".join(item.domain for item in contracts),
                    attributes={
                        "owner": owner_key,
                        "domains": [item.domain for item in contracts],
                    },
                )
            )
        return deduplicate_findings(findings)

    def _validate_language_precision(
        self,
        catalog: AuditCatalog,
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        for owner_contract in catalog.owners:
            for reference in owner_contract.all_refs():
                suffix = Path(reference.path).suffix.casefold()
                expected = {
                    ".py": "python",
                    ".ts": "typescript",
                    ".tsx": "tsx",
                    ".js": "javascript",
                    ".jsx": "jsx",
                    ".rs": "rust",
                }.get(suffix)
                if expected and reference.language.value != expected:
                    findings.append(
                        self._coverage_failure(
                            "catalog_reference_language_mismatch",
                            (
                                f"{reference.key} is {suffix} but catalog labels "
                                f"it {reference.language.value}."
                            ),
                            domain=owner_contract.domain,
                            path=reference.path,
                            attributes={
                                "expected_language": expected,
                                "actual_language": reference.language.value,
                            },
                        )
                    )
        return deduplicate_findings(findings)

    def _matrix_ids(
        self,
        requested_path: str | Path | None,
    ) -> tuple[set[str], tuple[EvidencePointer, ...], tuple[Finding, ...]]:
        if requested_path is None:
            candidate = self.root.parent / "docs" / "比赛要求追踪矩阵.md"
        else:
            requested = Path(requested_path)
            candidate = (
                requested.resolve(strict=False)
                if requested.is_absolute()
                else (self.root / requested).resolve(strict=False)
            )
        if not candidate.is_file():
            return (
                set(),
                (),
                (
                    finding(
                        "requirement_matrix_unavailable",
                        (
                            "Authoritative requirement matrix is unavailable; "
                            "catalog parity cannot be independently checked."
                        ),
                        "catalog",
                        severity=Severity.WARNING,
                        path=candidate.as_posix(),
                        owner_unit="M3-03",
                        disposition=Disposition.ADD_EVIDENCE,
                        remediation=(
                            "Provide the root requirement matrix to the release "
                            "audit or run from the competition workspace."
                        ),
                    ),
                ),
            )
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return (
                set(),
                (),
                (
                    self._catalog_failure(
                        "requirement_matrix_unreadable",
                        f"Requirement matrix cannot be read: {exc}",
                        path=candidate.as_posix(),
                        owner_unit="M3-03",
                    ),
                ),
            )
        identities = {match.group(1) for match in MATRIX_ROW_PATTERN.finditer(text)}
        return (
            identities,
            (
                EvidencePointer(
                    kind="requirement_matrix",
                    path=candidate.as_posix(),
                    digest=content_digest(text),
                    attributes={"identities": sorted(identities)},
                ),
            ),
            (),
        )

    def _validate_matrix_parity(
        self,
        catalog: AuditCatalog,
        matrix_ids: set[str],
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        catalog_ids = set(catalog.requirement_by_id)
        for item in sorted(matrix_ids - catalog_ids):
            findings.append(
                self._requirement_failure(
                    "requirement_matrix_row_unmapped",
                    f"Requirement matrix identity has no runtime evidence row: {item}.",
                    requirement=item,
                )
            )
        for item in sorted(catalog_ids - matrix_ids):
            findings.append(
                finding(
                    "requirement_catalog_row_not_in_matrix",
                    f"Evidence catalog identity is not present in matrix: {item}.",
                    "catalog",
                    severity=Severity.WARNING,
                    requirement_id=item,
                    owner_unit="M3-03",
                    disposition=Disposition.TRACK,
                    remediation=(
                        "Confirm whether the evidence row is an internal gate or "
                        "restore its authoritative matrix row."
                    ),
                )
            )
        return deduplicate_findings(findings)

    @staticmethod
    def _array(value: Any, field_name: str) -> Sequence[Any]:
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            raise CatalogError(f"{field_name} must be an array")
        return value

    @staticmethod
    def _duplicates(
        values: Iterable[str],
        *,
        code: str,
        message: str,
    ) -> tuple[Finding, ...]:
        counts = Counter(values)
        return tuple(
            finding(
                code,
                f"{message} identity={value!r}, count={count}.",
                "catalog",
                severity=Severity.BLOCKER,
                owner_unit="M3-01B",
                disposition=Disposition.BLOCK_RELEASE,
                default_path_impact=(
                    "Authority cannot be resolved deterministically for release."
                ),
                remediation="Remove the duplicate and retain one authoritative row.",
                attributes={"identity": value, "count": count},
            )
            for value, count in sorted(counts.items())
            if count > 1
        )

    @staticmethod
    def _catalog_failure(
        code: str,
        message: str,
        *,
        path: str = "",
        owner_unit: str = "M3-01B",
        remediation: str = "",
    ) -> Finding:
        return finding(
            code,
            message,
            "catalog",
            severity=Severity.BLOCKER,
            path=path,
            owner_unit=owner_unit,
            disposition=Disposition.BLOCK_RELEASE,
            default_path_impact=(
                "Release/freeze audit cannot establish state custody authority."
            ),
            remediation=remediation or "Repair the catalog before candidate mode.",
        )

    @staticmethod
    def _coverage_failure(
        code: str,
        message: str,
        *,
        domain: str,
        path: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> Finding:
        return finding(
            code,
            message,
            "catalog",
            severity=Severity.BLOCKER,
            domain=domain,
            path=path,
            owner_unit="M3-01B",
            disposition=Disposition.BLOCK_RELEASE,
            default_path_impact=(
                "One canonical state domain has no deterministic authority."
            ),
            remediation=(
                "Record one real owner/store/write/recovery contract and remove "
                "ambiguous authority."
            ),
            attributes=dict(attributes or {}),
        )

    @staticmethod
    def _requirement_failure(
        code: str,
        message: str,
        *,
        requirement: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> Finding:
        return finding(
            code,
            message,
            "catalog",
            severity=Severity.BLOCKER,
            requirement_id=requirement,
            owner_unit="M3-03",
            disposition=Disposition.BLOCK_RELEASE,
            default_path_impact=(
                "Competition evidence cannot be traced to executable runtime facts."
            ),
            remediation=(
                "Bind the matrix row to canonical owners, default entries, live "
                "evidence, mutations, tests, commits and configuration."
            ),
            attributes=dict(attributes or {}),
        )
