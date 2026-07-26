from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog import CatalogLoader, SourceCatalog, repository_role_summary
from .custody import CustodyAuditor, CustodyIndex
from .dependencies import DependencyAuditor, DependencyGraph
from .javascript_analyzer import JavaScriptAnalyzer, JavaScriptFileAnalysis
from .model import (
    AuditSection,
    RuleSwitches,
    stable_digest,
    stable_json,
)
from .policy import AuditMode, FindingPolicy, PolicyResult, queue_summary
from .processes import ProcessAuditor, ProcessCatalog, ProcessUse
from .python_analyzer import PythonAnalyzer, PythonFileAnalysis
from .repository import (
    RepositoryInventory,
    RepositoryScanner,
    inventory_manifest,
)
from .risks import RiskAuditor, RiskIndex


RECEIPT_SCHEMA = "zyra.source-custody-audit-receipt/v1"
DEFAULT_CATALOG_PATH = (
    "packages/integrations/zyra_integrations/data/source_custody_catalog.json"
)
DEFAULT_PROCESS_CATALOG_PATH = (
    "packages/integrations/zyra_integrations/data/source_custody_processes.json"
)
DEFAULT_VENDOR_MAP_PATH = "docs/vendor-map.md"
DEFAULT_NOTICE_PATH = "third_party/NOTICE.md"
DEFAULT_LEDGER_PATH = (
    "packages/integrations/zyra_integrations/data/internalization_ledger_seed.json"
)


@dataclass(frozen=True, slots=True)
class SourceCustodyConfig:
    project_root: Path
    mode: AuditMode
    catalog_path: str
    process_catalog_path: str
    vendor_map_path: str
    notice_path: str
    ledger_path: str
    revision: str
    baseline_revision: str
    switches: RuleSwitches
    include_vendor: bool = True
    similarity_threshold: float = 0.86

    @classmethod
    def build(
        cls,
        project_root: str | Path,
        *,
        mode: AuditMode | str = AuditMode.CANDIDATE,
        catalog_path: str = DEFAULT_CATALOG_PATH,
        process_catalog_path: str = DEFAULT_PROCESS_CATALOG_PATH,
        vendor_map_path: str = DEFAULT_VENDOR_MAP_PATH,
        notice_path: str = DEFAULT_NOTICE_PATH,
        ledger_path: str = DEFAULT_LEDGER_PATH,
        revision: str = "",
        baseline_revision: str = "",
        switches: RuleSwitches | None = None,
        include_vendor: bool = True,
        similarity_threshold: float = 0.86,
    ) -> "SourceCustodyConfig":
        root = Path(project_root).resolve(strict=False)
        if not root.exists() or not root.is_dir():
            raise ValueError(f"project root is not a directory: {root}")
        selected_revision = revision.strip() or resolve_git_revision(root)
        selected_baseline = baseline_revision.strip()
        if selected_revision and not _revision(selected_revision):
            raise ValueError("revision must be a 7-64 character hexadecimal commit")
        if selected_baseline and not _revision(selected_baseline):
            raise ValueError(
                "baseline revision must be a 7-64 character hexadecimal commit"
            )
        if not 0.5 <= similarity_threshold <= 1.0:
            raise ValueError("similarity threshold must be between 0.5 and 1.0")
        return cls(
            project_root=root,
            mode=AuditMode(mode),
            catalog_path=catalog_path.replace("\\", "/"),
            process_catalog_path=process_catalog_path.replace("\\", "/"),
            vendor_map_path=vendor_map_path.replace("\\", "/"),
            notice_path=notice_path.replace("\\", "/"),
            ledger_path=ledger_path.replace("\\", "/"),
            revision=selected_revision,
            baseline_revision=selected_baseline,
            switches=switches or RuleSwitches(),
            include_vendor=include_vendor,
            similarity_threshold=similarity_threshold,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root.as_posix(),
            "mode": self.mode.value,
            "catalog_path": self.catalog_path,
            "process_catalog_path": self.process_catalog_path,
            "vendor_map_path": self.vendor_map_path,
            "notice_path": self.notice_path,
            "ledger_path": self.ledger_path,
            "revision": self.revision,
            "baseline_revision": self.baseline_revision,
            "rule_switches": {
                name: getattr(self.switches, name)
                for name in self.switches.__dataclass_fields__
            },
            "include_vendor": self.include_vendor,
            "similarity_threshold": self.similarity_threshold,
        }


@dataclass(frozen=True, slots=True)
class SourceCustodyArtifacts:
    catalog: SourceCatalog | None
    inventory: RepositoryInventory
    python: tuple[PythonFileAnalysis, ...]
    javascript: tuple[JavaScriptFileAnalysis, ...]
    dependencies: DependencyGraph
    process_catalog: ProcessCatalog | None
    process_uses: tuple[ProcessUse, ...]
    custody: CustodyIndex
    risks: RiskIndex


@dataclass(frozen=True, slots=True)
class SourceCustodyResult:
    config: SourceCustodyConfig
    sections: tuple[AuditSection, ...]
    policy: PolicyResult
    artifacts: SourceCustodyArtifacts
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

    def receipt(self, *, include_findings: bool = True) -> dict[str, Any]:
        source_rows = (
            repository_role_summary(self.artifacts.catalog)
            if self.artifacts.catalog is not None
            else []
        )
        payload: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "slice_id": "M3-S01A-01",
            "valid": self.valid,
            "release_ready": self.release_ready,
            "mode": self.config.mode.value,
            "revision": self.config.revision,
            "baseline_revision": self.config.baseline_revision,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "config": self.config.to_dict(),
            "sections": [item.to_dict() for item in self.sections],
            "policy": self.policy.to_dict(),
            "source_repository_rows": source_rows,
            "repository_manifest": inventory_manifest(self.artifacts.inventory),
            "dependency_summary": self.artifacts.dependencies.to_summary(),
            "custody_summary": self.artifacts.custody.to_summary(),
            "risk_summary": self.artifacts.risks.to_summary(),
            "process_uses": [item.to_dict() for item in self.artifacts.process_uses],
            "work_queue": [item.to_dict() for item in self.policy.work_queue],
            "work_queue_summary": queue_summary(self.policy.work_queue),
            "openclaw_boundary": (
                [
                    boundary.to_dict()
                    for boundary in self.artifacts.catalog.historical_boundaries
                ]
                if self.artifacts.catalog is not None
                else []
            ),
            "receipt_digest": self.receipt_digest,
        }
        if not include_findings:
            for item in payload["sections"]:
                item.pop("findings", None)
                item.pop("evidence", None)
            payload["policy"].pop("findings", None)
        return payload

    def summary(self) -> dict[str, Any]:
        return {
            "schema": RECEIPT_SCHEMA,
            "slice_id": "M3-S01A-01",
            "valid": self.valid,
            "release_ready": self.release_ready,
            "mode": self.config.mode.value,
            "revision": self.config.revision,
            "baseline_revision": self.config.baseline_revision,
            "sections": {
                item.name: {
                    "valid": item.valid,
                    "finding_count": len(item.findings),
                    "metrics": dict(item.metrics),
                }
                for item in self.sections
            },
            "policy": {
                "blockers": self.policy.blocker_count,
                "errors": self.policy.error_count,
                "warnings": self.policy.warning_count,
                "findings": len(self.policy.findings),
                "work_queue": queue_summary(self.policy.work_queue),
                "digest": self.policy.digest,
            },
            "receipt_digest": self.receipt_digest,
        }

    def write_receipt(self, path: str | Path) -> Path:
        return atomic_write_json(path, self.receipt())

    def write_summary(self, path: str | Path) -> Path:
        return atomic_write_json(path, self.summary())

    def write_work_queue(self, path: str | Path) -> Path:
        payload = {
            "schema": "zyra.m3-01b-source-custody-work-queue/v1",
            "source_receipt_digest": self.receipt_digest,
            "revision": self.config.revision,
            "release_ready": self.release_ready,
            "summary": queue_summary(self.policy.work_queue),
            "items": [item.to_dict() for item in self.policy.work_queue],
        }
        payload["digest"] = stable_digest(payload)
        return atomic_write_json(path, payload)


class SourceCustodyEngine:
    def __init__(
        self,
        project_root: str | Path,
        *,
        mode: AuditMode | str = AuditMode.CANDIDATE,
        catalog_path: str = DEFAULT_CATALOG_PATH,
        process_catalog_path: str = DEFAULT_PROCESS_CATALOG_PATH,
        vendor_map_path: str = DEFAULT_VENDOR_MAP_PATH,
        notice_path: str = DEFAULT_NOTICE_PATH,
        ledger_path: str = DEFAULT_LEDGER_PATH,
        revision: str = "",
        baseline_revision: str = "",
        switches: RuleSwitches | None = None,
        include_vendor: bool = True,
        similarity_threshold: float = 0.86,
    ) -> None:
        self.config = SourceCustodyConfig.build(
            project_root,
            mode=mode,
            catalog_path=catalog_path,
            process_catalog_path=process_catalog_path,
            vendor_map_path=vendor_map_path,
            notice_path=notice_path,
            ledger_path=ledger_path,
            revision=revision,
            baseline_revision=baseline_revision,
            switches=switches,
            include_vendor=include_vendor,
            similarity_threshold=similarity_threshold,
        )

    def run(self) -> SourceCustodyResult:
        started_at = utc_now()
        sections: list[AuditSection] = []
        catalog_result = CatalogLoader(
            self.config.project_root,
            switches=self.config.switches,
        ).load(self.config.catalog_path)
        sections.append(catalog_result.section)
        inventory, repository_section = RepositoryScanner(
            self.config.project_root,
            include_vendor=self.config.include_vendor,
            switches=self.config.switches,
        ).scan()
        sections.append(repository_section)
        python, python_section = PythonAnalyzer(
            self.config.project_root,
            switches=self.config.switches,
        ).analyze(inventory)
        sections.append(python_section)
        javascript, javascript_section = JavaScriptAnalyzer(
            self.config.project_root,
            switches=self.config.switches,
        ).analyze(inventory)
        sections.append(javascript_section)
        dependencies, dependency_section = DependencyAuditor(
            self.config.project_root,
            switches=self.config.switches,
        ).audit(inventory, python, javascript)
        sections.append(dependency_section)
        process_catalog, process_uses, process_section = ProcessAuditor(
            self.config.project_root,
            switches=self.config.switches,
        ).audit(
            inventory,
            dependencies,
            python,
            javascript,
            self.config.process_catalog_path,
        )
        sections.append(process_section)
        custody, custody_section = CustodyAuditor(
            self.config.project_root,
            switches=self.config.switches,
        ).audit(
            inventory,
            catalog_result.catalog,
            vendor_map_path=self.config.vendor_map_path,
            notice_path=self.config.notice_path,
            ledger_path=self.config.ledger_path,
        )
        sections.append(custody_section)
        risks, risk_section = RiskAuditor(
            self.config.project_root,
            switches=self.config.switches,
            similarity_threshold=self.config.similarity_threshold,
        ).audit(
            inventory,
            catalog_result.catalog,
            dependencies,
            process_catalog,
            process_uses,
            custody,
            python,
            javascript,
        )
        sections.append(risk_section)
        policy = FindingPolicy().apply(sections, mode=self.config.mode)
        completed_at = utc_now()
        artifacts = SourceCustodyArtifacts(
            catalog=catalog_result.catalog,
            inventory=inventory,
            python=python,
            javascript=javascript,
            dependencies=dependencies,
            process_catalog=process_catalog,
            process_uses=process_uses,
            custody=custody,
            risks=risks,
        )
        receipt_material = {
            "schema": RECEIPT_SCHEMA,
            "slice_id": "M3-S01A-01",
            "mode": self.config.mode.value,
            "revision": self.config.revision,
            "baseline_revision": self.config.baseline_revision,
            "config": self.config.to_dict(),
            "section_digests": {
                item.name: stable_digest(item.to_dict()) for item in sections
            },
            "policy_digest": policy.digest,
            "catalog_digest": (
                catalog_result.catalog.digest if catalog_result.catalog else ""
            ),
            "inventory_digest": stable_digest(inventory.to_summary()),
            "dependency_summary": dependencies.to_summary(),
            "custody_digest": custody.digest,
            "risk_digest": risks.digest,
        }
        return SourceCustodyResult(
            config=self.config,
            sections=tuple(sections),
            policy=policy,
            artifacts=artifacts,
            started_at=started_at,
            completed_at=completed_at,
            receipt_digest=stable_digest(receipt_material),
        )


def resolve_git_revision(root: Path) -> str:
    git_entry = root / ".git"
    if not git_entry.exists():
        return ""
    git_directory = git_entry
    if git_entry.is_file():
        try:
            content = git_entry.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
        prefix = "gitdir:"
        if not content.casefold().startswith(prefix):
            return ""
        selected = content[len(prefix) :].strip()
        git_directory = (
            Path(selected)
            if Path(selected).is_absolute()
            else (root / selected).resolve(strict=False)
        )
    head_path = git_directory / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if _revision(head):
        return head.casefold()
    prefix = "ref:"
    if not head.startswith(prefix):
        return ""
    reference = head[len(prefix) :].strip()
    loose = git_directory / reference
    try:
        value = loose.read_text(encoding="utf-8").strip()
        if _revision(value):
            return value.casefold()
    except (OSError, UnicodeDecodeError):
        pass
    packed = git_directory / "packed-refs"
    try:
        for line in packed.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            value, separator, selected_reference = line.partition(" ")
            if separator and selected_reference == reference and _revision(value):
                return value.casefold()
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def _revision(value: str) -> bool:
    return 7 <= len(value) <= 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    destination = Path(path).resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = stable_json(payload) + "\n"
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
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
