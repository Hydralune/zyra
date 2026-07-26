from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zyra_integrations.source_custody.engine import SourceCustodyEngine
from zyra_integrations.source_custody.policy import AuditMode as SourceAuditMode
from zyra_integrations.source_custody.repository import (
    RepositoryInventory,
    RepositoryScanner,
)

from .catalog import DEFAULT_CATALOG_PATH, CatalogLoader
from .causality import CausalityAuditor, CausalityResult
from .lines import EffectiveLineAuditor, LineAuditResult
from .model import (
    AuditCatalog,
    AuditMode,
    AuditSection,
    CatalogError,
    Disposition,
    EvidencePointer,
    Finding,
    RuleSwitches,
    Severity,
    stable_digest,
)
from .ownership import OwnershipAuditor, OwnershipResult
from .policy import (
    FreezeFindingPolicy,
    PolicyResult,
    WorkItem,
    queue_summary,
)
from .python_graph import PythonGraphAnalyzer, PythonGraphResult
from .reachability import ReachabilityAuditor, ReachabilityResult
from .requirements import RequirementAuditResult, RequirementEvidenceAuditor
from .script_graph import ScriptGraphAnalyzer, ScriptGraphResult
from .source_bridge import SourceBridgeResult, SourceRiskBridge


RECEIPT_SCHEMA = "zyra.state-owner-reachability-evidence-audit/v1"
DOWNSTREAM_SCHEMA = "zyra.m3-freeze-audit-work-queue/v1"


@dataclass(frozen=True, slots=True)
class FreezeAuditConfig:
    project_root: Path
    mode: AuditMode
    catalog_path: str
    requirement_matrix_path: str
    revision: str
    baseline_revision: str
    parent_baseline_revision: str
    slice_minimum: int
    parent_minimum: int
    source_receipt_path: str
    run_source_custody: bool
    include_vendor: bool
    switches: RuleSwitches

    @classmethod
    def build(
        cls,
        project_root: str | Path,
        *,
        mode: AuditMode | str = AuditMode.CANDIDATE,
        catalog_path: str = DEFAULT_CATALOG_PATH,
        requirement_matrix_path: str | Path | None = None,
        revision: str = "HEAD",
        baseline_revision: str = "",
        parent_baseline_revision: str = "",
        slice_minimum: int = 0,
        parent_minimum: int = 0,
        source_receipt_path: str | Path | None = None,
        run_source_custody: bool = True,
        include_vendor: bool = True,
        switches: RuleSwitches | None = None,
    ) -> "FreezeAuditConfig":
        root = Path(project_root).resolve(strict=False)
        if not root.is_dir():
            raise ValueError(f"project root is not a directory: {root}")
        catalog = str(catalog_path).replace("\\", "/")
        matrix = (
            str(requirement_matrix_path)
            if requirement_matrix_path is not None
            else ""
        )
        source_receipt = (
            str(source_receipt_path).replace("\\", "/")
            if source_receipt_path is not None
            else ""
        )
        if slice_minimum < 0 or parent_minimum < 0:
            raise ValueError("effective-line minimum cannot be negative")
        return cls(
            project_root=root,
            mode=AuditMode(mode),
            catalog_path=catalog,
            requirement_matrix_path=matrix,
            revision=revision.strip() or "HEAD",
            baseline_revision=baseline_revision.strip(),
            parent_baseline_revision=parent_baseline_revision.strip(),
            slice_minimum=slice_minimum,
            parent_minimum=parent_minimum,
            source_receipt_path=source_receipt,
            run_source_custody=run_source_custody,
            include_vendor=include_vendor,
            switches=switches or RuleSwitches(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root.as_posix(),
            "mode": self.mode.value,
            "catalog_path": self.catalog_path,
            "requirement_matrix_path": self.requirement_matrix_path,
            "revision": self.revision,
            "baseline_revision": self.baseline_revision,
            "parent_baseline_revision": self.parent_baseline_revision,
            "slice_minimum": self.slice_minimum,
            "parent_minimum": self.parent_minimum,
            "source_receipt_path": self.source_receipt_path,
            "run_source_custody": self.run_source_custody,
            "include_vendor": self.include_vendor,
            "switches": self.switches.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class FreezeAuditArtifacts:
    catalog: AuditCatalog | None
    inventory: RepositoryInventory
    python: PythonGraphResult
    script: ScriptGraphResult
    reachability: ReachabilityResult | None
    ownership: OwnershipResult | None
    causality: CausalityResult | None
    source_bridge: SourceBridgeResult
    slice_lines: LineAuditResult | None
    parent_lines: LineAuditResult | None
    requirements: RequirementAuditResult | None


@dataclass(frozen=True, slots=True)
class FreezeAuditResult:
    config: FreezeAuditConfig
    revision: str
    sections: tuple[AuditSection, ...]
    policy: PolicyResult
    artifacts: FreezeAuditArtifacts
    started_at: str
    completed_at: str
    receipt_digest: str

    @property
    def valid(self) -> bool:
        return self.policy.valid

    @property
    def release_ready(self) -> bool:
        return self.policy.release_ready

    @property
    def exit_code(self) -> int:
        return 0 if self.valid else 2

    def receipt(self, *, include_records: bool = True) -> dict[str, Any]:
        sections = [item.to_dict() for item in self.sections]
        if not include_records:
            for item in sections:
                item.pop("records", None)
                item.pop("evidence", None)
                item.pop("findings", None)
        payload = {
            "schema": RECEIPT_SCHEMA,
            "slice_id": "M3-S01A-02",
            "valid": self.valid,
            "release_ready": self.release_ready,
            "mode": self.config.mode.value,
            "revision": self.revision,
            "baseline_revision": self.config.baseline_revision,
            "parent_baseline_revision": self.config.parent_baseline_revision,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "config": self.config.to_dict(),
            "sections": sections,
            "policy": self.policy.to_dict(),
            "state_owner_summary": (
                {
                    "domains": len(self.artifacts.catalog.owners),
                    "owner_count": len(self.artifacts.catalog.owners),
                    "requirement_count": len(
                        self.artifacts.catalog.requirements
                    ),
                    "valid_domains": sorted(
                        self.artifacts.ownership.valid_domains
                        if self.artifacts.ownership
                        else ()
                    ),
                    "catalog_digest": self.artifacts.catalog.catalog_digest,
                }
                if self.artifacts.catalog is not None
                else {}
            ),
            "reachability_summary": (
                {
                    "reachable_entry_ids": sorted(
                        self.artifacts.reachability.reachable_entry_ids
                    ),
                    "graph_digest": self.artifacts.reachability.graph.digest(),
                }
                if self.artifacts.reachability is not None
                else {}
            ),
            "causality_summary": (
                {
                    "valid_link_ids": sorted(
                        self.artifacts.causality.valid_link_ids
                    )
                }
                if self.artifacts.causality is not None
                else {}
            ),
            "requirement_summary": (
                {
                    "valid_requirement_ids": sorted(
                        self.artifacts.requirements.valid_requirement_ids
                    )
                }
                if self.artifacts.requirements is not None
                else {}
            ),
            "source_receipt_digest": (
                self.artifacts.source_bridge.source_receipt_digest
            ),
            "work_queue_summary": queue_summary(self.policy.work_queue),
            "downstream_inputs": self.downstream_inputs(),
            "receipt_digest": self.receipt_digest,
        }
        return payload

    def summary(self) -> dict[str, Any]:
        return {
            "schema": RECEIPT_SCHEMA,
            "slice_id": "M3-S01A-02",
            "valid": self.valid,
            "release_ready": self.release_ready,
            "mode": self.config.mode.value,
            "revision": self.revision,
            "sections": {
                item.name: {
                    "valid": item.valid,
                    "findings": len(item.findings),
                    "metrics": dict(item.metrics),
                    "digest": item.digest,
                }
                for item in self.sections
            },
            "policy": {
                "findings": len(self.policy.findings),
                "blockers": self.policy.blocker_count,
                "errors": self.policy.error_count,
                "warnings": self.policy.warning_count,
                "work_queue": queue_summary(self.policy.work_queue),
                "digest": self.policy.digest,
            },
            "receipt_digest": self.receipt_digest,
        }

    def downstream_inputs(self) -> dict[str, Any]:
        by_owner: dict[str, list[dict[str, Any]]] = {
            "M3-01B": [],
            "M3-02A": [],
            "M3-02B": [],
            "M3-03": [],
        }
        for item in self.policy.work_queue:
            by_owner.setdefault(item.owner_unit, []).append(item.to_dict())
        result: dict[str, Any] = {}
        for owner_unit, items in sorted(by_owner.items()):
            payload = {
                "schema": DOWNSTREAM_SCHEMA,
                "source_slice": "M3-S01A-02",
                "revision": self.revision,
                "receipt_digest": self.receipt_digest,
                "owner_unit": owner_unit,
                "items": items,
            }
            payload["digest"] = stable_digest(payload)
            result[owner_unit] = payload
        return result

    def write_receipt(self, path: str | Path) -> Path:
        return atomic_write_json(path, self.receipt())

    def write_summary(self, path: str | Path) -> Path:
        return atomic_write_json(path, self.summary())

    def write_work_queue(self, path: str | Path) -> Path:
        payload = {
            "schema": DOWNSTREAM_SCHEMA,
            "source_slice": "M3-S01A-02",
            "revision": self.revision,
            "receipt_digest": self.receipt_digest,
            "summary": queue_summary(self.policy.work_queue),
            "items": [item.to_dict() for item in self.policy.work_queue],
        }
        payload["digest"] = stable_digest(payload)
        return atomic_write_json(path, payload)

    def write_downstream_directory(self, path: str | Path) -> tuple[Path, ...]:
        root = Path(path).resolve(strict=False)
        outputs: list[Path] = []
        for owner_unit, payload in self.downstream_inputs().items():
            filename = owner_unit.casefold().replace("-", "_") + ".json"
            outputs.append(atomic_write_json(root / filename, payload))
        return tuple(outputs)


class FreezeAuditEngine:
    def __init__(
        self,
        project_root: str | Path,
        **kwargs: Any,
    ) -> None:
        self.config = FreezeAuditConfig.build(project_root, **kwargs)

    def run(self, *, source_result: Any | None = None) -> FreezeAuditResult:
        started_at = utc_now()
        sections: list[AuditSection] = []
        catalog_result = CatalogLoader(
            self.config.project_root,
            switches=self.config.switches,
        ).load(
            self.config.catalog_path,
            requirement_matrix_path=(
                self.config.requirement_matrix_path or None
            ),
        )
        sections.append(catalog_result.section)
        resolved_source_result = source_result
        if resolved_source_result is None and self.config.run_source_custody:
            resolved_source_result = SourceCustodyEngine(
                self.config.project_root,
                mode=SourceAuditMode.INVENTORY,
                revision=self._resolved_revision(),
                baseline_revision=self.config.baseline_revision,
                include_vendor=self.config.include_vendor,
            ).run()
            inventory = resolved_source_result.artifacts.inventory
        else:
            inventory, source_inventory_section = RepositoryScanner(
                self.config.project_root,
                include_vendor=False,
            ).scan()
            sections.append(adapt_source_section(source_inventory_section))
        source_paths = [
            item.path
            for item in inventory.files
            if item.kind == "source"
            and not item.path.startswith(("vendor/", "vendor-runtimes/"))
        ]
        python = PythonGraphAnalyzer(
            self.config.project_root,
            switches=self.config.switches,
        ).analyze(source_paths)
        script = ScriptGraphAnalyzer(
            self.config.project_root,
            switches=self.config.switches,
        ).analyze(source_paths)
        sections.extend((python.section, script.section))
        source_bridge = SourceRiskBridge(
            self.config.project_root,
            switches=self.config.switches,
        ).audit(
            resolved_source_result,
            receipt_path=self.config.source_receipt_path or None,
            expected_revision=self._resolved_revision(),
        )
        sections.append(source_bridge.section)
        reachability: ReachabilityResult | None = None
        ownership: OwnershipResult | None = None
        causality: CausalityResult | None = None
        requirements: RequirementAuditResult | None = None
        if catalog_result.catalog is not None:
            reachability = ReachabilityAuditor(
                self.config.project_root,
                switches=self.config.switches,
            ).audit(catalog_result.catalog, python, script)
            sections.append(reachability.section)
            ownership = OwnershipAuditor(
                self.config.project_root,
                switches=self.config.switches,
            ).audit(
                catalog_result.catalog,
                reachability.graph,
                python,
                script,
            )
            sections.append(ownership.section)
            causality = CausalityAuditor(
                self.config.project_root,
                switches=self.config.switches,
            ).audit(
                catalog_result.catalog,
                reachability.graph,
                python,
                script,
            )
            sections.append(causality.section)
            requirements = RequirementEvidenceAuditor(
                self.config.project_root,
                switches=self.config.switches,
            ).audit(
                catalog_result.catalog,
                ownership,
                reachability,
                causality,
            )
            sections.append(requirements.section)
        slice_lines = self._line_audit(
            baseline=self.config.baseline_revision,
            minimum=self.config.slice_minimum,
            audit_id="slice",
        )
        if slice_lines is not None:
            sections.append(slice_lines.section)
        parent_lines = self._line_audit(
            baseline=self.config.parent_baseline_revision,
            minimum=self.config.parent_minimum,
            audit_id="parent",
        )
        if parent_lines is not None:
            sections.append(parent_lines.section)
        policy = FreezeFindingPolicy().apply(
            sections,
            mode=self.config.mode,
        )
        completed_at = utc_now()
        revision = self._resolved_revision()
        artifacts = FreezeAuditArtifacts(
            catalog=catalog_result.catalog,
            inventory=inventory,
            python=python,
            script=script,
            reachability=reachability,
            ownership=ownership,
            causality=causality,
            source_bridge=source_bridge,
            slice_lines=slice_lines,
            parent_lines=parent_lines,
            requirements=requirements,
        )
        receipt_material = {
            "schema": RECEIPT_SCHEMA,
            "slice_id": "M3-S01A-02",
            "mode": self.config.mode.value,
            "revision": revision,
            "config": self.config.to_dict(),
            "section_digests": {
                item.name: item.digest
                for item in sorted(sections, key=lambda value: value.name)
            },
            "policy_digest": policy.digest,
            "catalog_digest": (
                catalog_result.catalog.catalog_digest
                if catalog_result.catalog is not None
                else ""
            ),
            "source_receipt_digest": source_bridge.source_receipt_digest,
        }
        return FreezeAuditResult(
            config=self.config,
            revision=revision,
            sections=tuple(sections),
            policy=policy,
            artifacts=artifacts,
            started_at=started_at,
            completed_at=completed_at,
            receipt_digest=stable_digest(receipt_material),
        )

    def _line_audit(
        self,
        *,
        baseline: str,
        minimum: int,
        audit_id: str,
    ) -> LineAuditResult | None:
        if not baseline:
            return None
        return EffectiveLineAuditor(
            self.config.project_root,
            switches=self.config.switches,
        ).audit(
            baseline,
            head=self.config.revision,
            minimum=minimum,
            audit_id=audit_id,
        )

    def _resolved_revision(self) -> str:
        try:
            return EffectiveLineAuditor(
                self.config.project_root
            ).git.revision(self.config.revision)
        except ValueError:
            return self.config.revision


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def adapt_source_section(source_section: Any) -> AuditSection:
    """Translate the protected S01A-01 section into the freeze-audit model.

    The source-custody package intentionally owns its own immutable evidence
    schema.  Reusing its scanner is safe, but leaking its Finding objects into
    this slice would make policy sorting and receipt serialization depend on a
    foreign dataclass shape.  The adapter keeps the boundary explicit and
    preserves every actionable field.
    """

    findings: list[Finding] = []
    for item in source_section.findings:
        evidence = tuple(
            EvidencePointer(
                kind=entry.kind.value,
                path=entry.path,
                line=entry.line,
                symbol=entry.symbol,
                digest=entry.excerpt_digest,
                attributes=dict(entry.attributes),
            )
            for entry in item.evidence
        )
        attributes = dict(item.attributes)
        if item.source_repo:
            attributes.setdefault("source_repo", item.source_repo)
        findings.append(
            Finding(
                code=item.code,
                message=item.message,
                rule_group=item.rule_group,
                severity=Severity(item.severity.value),
                domain=item.capability,
                path=item.path,
                line=item.line,
                owner_unit=item.owner_unit,
                disposition=Disposition(item.disposition.value),
                default_path_impact=item.default_path_impact,
                remediation=item.remediation,
                evidence=evidence,
                attributes=attributes,
            )
        )
    section_evidence = tuple(
        EvidencePointer(
            kind=item.kind.value,
            path=item.path,
            line=item.line,
            symbol=item.symbol,
            digest=item.excerpt_digest,
            attributes=dict(item.attributes),
        )
        for item in source_section.evidence
    )
    return AuditSection(
        name=source_section.name,
        metrics=dict(source_section.metrics),
        findings=tuple(findings),
        evidence=section_evidence,
    )


def atomic_write_json(
    path: str | Path,
    payload: Mapping[str, Any],
) -> Path:
    destination = Path(path).resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise
    return destination
