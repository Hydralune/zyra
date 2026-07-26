from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .causality import CausalEffectVerifier
from .contracts import (
    ABSORPTION_RECEIPT_SCHEMA,
    M3WorkItem,
    M3WorkQueue,
    ProductizationContractError,
    ResolutionStatus,
    RuntimeDomain,
    WorkAction,
    WorkResolution,
    canonicalize,
    digest_payload,
    stable_id,
)
from .defaults import DefaultEntryProbeResult, DefaultEntryRegistry
from .owner_registry import RuntimeOwnerRegistry
from .source_boundary import RepositoryBoundaryInspector, RepositoryBoundaryReport


@dataclass(frozen=True, slots=True)
class FindingRecord:
    fingerprint: str
    code: str
    section: str
    path: str
    line: int
    blocking: bool
    owner_unit: str
    message: str
    attributes: Mapping[str, Any]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        section: str,
    ) -> FindingRecord:
        return cls(
            fingerprint=str(payload.get("fingerprint", "")),
            code=str(payload.get("code", "")),
            section=section,
            path=str(payload.get("path", "")),
            line=int(payload.get("line", 0) or 0),
            blocking=bool(payload.get("blocking", False)),
            owner_unit=str(payload.get("owner_unit", "")),
            message=str(payload.get("message", "")),
            attributes=(
                dict(payload.get("attributes", {}))
                if isinstance(payload.get("attributes"), Mapping)
                else {}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "code": self.code,
            "section": self.section,
            "path": self.path,
            "line": self.line,
            "blocking": self.blocking,
            "owner_unit": self.owner_unit,
            "message": self.message,
            "attributes": canonicalize(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class M3AuditInput:
    queue: M3WorkQueue
    receipt_digest: str
    receipt_revision: str
    records: Mapping[str, FindingRecord]
    receipt_path: str
    queue_path: str

    def findings_for(self, item: M3WorkItem) -> tuple[FindingRecord, ...]:
        missing = [
            fingerprint
            for fingerprint in item.finding_fingerprints
            if fingerprint not in self.records
        ]
        if missing:
            raise ProductizationContractError(
                "work_finding_record_missing",
                f"{item.work_id} references findings absent from the parent receipt",
                details={"missing": missing},
            )
        return tuple(self.records[value] for value in item.finding_fingerprints)


@dataclass(frozen=True, slots=True)
class OwnerLossProof:
    domain: RuntimeDomain
    ready_before: bool
    rejected_while_disabled: bool
    fallback_success: bool
    ready_after: bool
    generation_before: int
    generation_after: int
    error_code: str
    digest: str

    @property
    def valid(self) -> bool:
        return (
            self.ready_before
            and self.rejected_while_disabled
            and not self.fallback_success
            and self.ready_after
            and self.generation_after > self.generation_before
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain.value,
            "ready_before": self.ready_before,
            "rejected_while_disabled": self.rejected_while_disabled,
            "fallback_success": self.fallback_success,
            "ready_after": self.ready_after,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "error_code": self.error_code,
            "valid": self.valid,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class AbsorptionSummary:
    total: int
    blocking: int
    resolved: int
    unresolved: int
    blocking_resolved: int
    blocking_unresolved: int
    by_action: Mapping[str, Mapping[str, int]]
    by_status: Mapping[str, int]

    @property
    def ready(self) -> bool:
        return self.blocking_unresolved == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "blocking": self.blocking,
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "blocking_resolved": self.blocking_resolved,
            "blocking_unresolved": self.blocking_unresolved,
            "by_action": canonicalize(self.by_action),
            "by_status": dict(sorted(self.by_status.items())),
            "ready": self.ready,
        }


@dataclass(frozen=True, slots=True)
class RuntimeAbsorptionReceipt:
    revision: str
    source_revision: str
    source_queue_digest: str
    source_receipt_digest: str
    started_at_ns: int
    completed_at_ns: int
    owner_registry_digest: str
    default_registry_digest: str
    source_boundary_digest: str
    owner_loss_proofs: tuple[OwnerLossProof, ...]
    owner_probes: tuple[Mapping[str, Any], ...]
    default_probes: tuple[Mapping[str, Any], ...]
    causal_verifications: tuple[Mapping[str, Any], ...]
    resolutions: tuple[WorkResolution, ...]
    summary: AbsorptionSummary
    source_boundary_ready: bool
    owner_boundary_ready: bool
    default_boundary_ready: bool
    causal_boundary_ready: bool
    module_enabled: bool
    receipt_digest: str

    @property
    def release_ready(self) -> bool:
        return (
            self.module_enabled
            and self.summary.ready
            and self.source_boundary_ready
            and self.owner_boundary_ready
            and self.default_boundary_ready
            and self.causal_boundary_ready
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ABSORPTION_RECEIPT_SCHEMA,
            "revision": self.revision,
            "source_revision": self.source_revision,
            "source_queue_digest": self.source_queue_digest,
            "source_receipt_digest": self.source_receipt_digest,
            "started_at_ns": self.started_at_ns,
            "completed_at_ns": self.completed_at_ns,
            "owner_registry_digest": self.owner_registry_digest,
            "default_registry_digest": self.default_registry_digest,
            "source_boundary_digest": self.source_boundary_digest,
            "owner_loss_proofs": [item.to_dict() for item in self.owner_loss_proofs],
            "owner_probes": [canonicalize(item) for item in self.owner_probes],
            "default_probes": [canonicalize(item) for item in self.default_probes],
            "causal_verifications": [
                canonicalize(item) for item in self.causal_verifications
            ],
            "resolutions": [item.to_dict() for item in self.resolutions],
            "summary": self.summary.to_dict(),
            "source_boundary_ready": self.source_boundary_ready,
            "owner_boundary_ready": self.owner_boundary_ready,
            "default_boundary_ready": self.default_boundary_ready,
            "causal_boundary_ready": self.causal_boundary_ready,
            "module_enabled": self.module_enabled,
            "release_ready": self.release_ready,
            "receipt_digest": self.receipt_digest,
        }

    def write(self, path: Path) -> Path:
        destination = path.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        return destination


def load_m3_audit_input(
    queue_path: Path,
    receipt_path: Path,
) -> M3AuditInput:
    queue_absolute = queue_path.expanduser().resolve()
    receipt_absolute = receipt_path.expanduser().resolve()
    try:
        queue_payload = json.loads(queue_absolute.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProductizationContractError(
            "m3_queue_read_failed",
            f"cannot read M3-01B work queue: {error}",
        ) from error
    if not isinstance(queue_payload, Mapping):
        raise ProductizationContractError(
            "m3_queue_payload_invalid",
            "M3-01B work queue must be a JSON object",
        )
    queue = M3WorkQueue.from_dict(queue_payload)
    try:
        receipt_payload = json.loads(receipt_absolute.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProductizationContractError(
            "m3_receipt_read_failed",
            f"cannot read M3-01A receipt: {error}",
        ) from error
    if not isinstance(receipt_payload, Mapping):
        raise ProductizationContractError(
            "m3_receipt_payload_invalid",
            "M3-01A receipt must be a JSON object",
        )
    receipt_digest = str(receipt_payload.get("receipt_digest", ""))
    receipt_revision = str(receipt_payload.get("revision", ""))
    if receipt_digest != queue.receipt_digest:
        raise ProductizationContractError(
            "m3_parent_receipt_digest_mismatch",
            "M3-01B queue is not bound to the supplied M3-01A receipt",
            details={
                "queue": queue.receipt_digest,
                "receipt": receipt_digest,
            },
        )
    if receipt_revision != queue.revision:
        raise ProductizationContractError(
            "m3_parent_revision_mismatch",
            "M3-01B queue and M3-01A receipt revisions differ",
        )
    records: dict[str, FindingRecord] = {}
    sections = receipt_payload.get("sections", ())
    if not isinstance(sections, list):
        raise ProductizationContractError(
            "m3_receipt_sections_invalid",
            "M3-01A receipt sections must be a list",
        )
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        section_name = str(section.get("name", ""))
        findings = section.get("findings", ())
        if not isinstance(findings, list):
            continue
        for raw in findings:
            if not isinstance(raw, Mapping):
                continue
            record = FindingRecord.from_dict(raw, section=section_name)
            if not record.fingerprint:
                continue
            existing = records.get(record.fingerprint)
            if existing is not None and existing != record:
                raise ProductizationContractError(
                    "m3_finding_fingerprint_duplicate",
                    f"finding fingerprint {record.fingerprint} is not unique",
                )
            records[record.fingerprint] = record
    expected_fingerprints = {
        fingerprint
        for item in queue.items
        for fingerprint in item.finding_fingerprints
    }
    missing = sorted(expected_fingerprints - set(records))
    if missing:
        raise ProductizationContractError(
            "m3_queue_finding_disconnected",
            "M3-01B queue references findings absent from the receipt",
            details={"missing": missing},
        )
    return M3AuditInput(
        queue=queue,
        receipt_digest=receipt_digest,
        receipt_revision=receipt_revision,
        records=records,
        receipt_path=receipt_absolute.as_posix(),
        queue_path=queue_absolute.as_posix(),
    )


def _event_name_from_finding(message: str) -> str:
    match = re.search(
        r"(?:not causality-bound|event)\s*:\s*(?P<event>.+?)\.?\s*$",
        str(message),
        flags=re.IGNORECASE,
    )
    return str(match.group("event")).strip().rstrip(".") if match else ""


class RuntimeAbsorptionCoordinator:
    def __init__(
        self,
        *,
        project_root: Path,
        revision: str,
        audit_input: M3AuditInput,
        owner_registry: RuntimeOwnerRegistry,
        default_registry: DefaultEntryRegistry,
        causal_verifier: CausalEffectVerifier,
        source_inspector: RepositoryBoundaryInspector | None = None,
        enabled: bool = True,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.revision = revision
        self.audit_input = audit_input
        self.owner_registry = owner_registry
        self.default_registry = default_registry
        self.causal_verifier = causal_verifier
        self.source_inspector = source_inspector or RepositoryBoundaryInspector(
            self.project_root,
            revision=revision,
        )
        self.enabled = bool(enabled)

    def run(
        self,
        *,
        verify_owner_loss: bool = True,
    ) -> RuntimeAbsorptionReceipt:
        started = time.time_ns()
        if not self.enabled:
            return self._disabled_receipt(started)
        owner_results = self.owner_registry.probe_all()
        entry_results = self.default_registry.probe_all()
        source_report = self.source_inspector.audit(self.audit_input.queue.items)
        owner_loss_proofs = (
            self._prove_owner_loss()
            if verify_owner_loss
            else ()
        )
        causal_results = self.causal_verifier.verify_all()
        source_by_work = {
            item.work_id: item for item in source_report.item_resolutions
        }
        owner_by_domain = {item.domain: item for item in owner_results}
        entries_by_domain: dict[RuntimeDomain, tuple[DefaultEntryProbeResult, ...]] = {
            domain: tuple(
                result for result in entry_results if result.domain is domain
            )
            for domain in RuntimeDomain
        }
        proof_by_domain = {item.domain: item for item in owner_loss_proofs}
        causal_by_domain = {
            self.causal_verifier.contract(item.link_id).domain: item
            for item in causal_results
        }
        resolutions: list[WorkResolution] = []
        for item in self.audit_input.queue.items:
            records = self.audit_input.findings_for(item)
            if item.action is WorkAction.DISPOSE_SOURCE_RISK:
                resolution = self._resolve_source_item(
                    item,
                    records,
                    source_by_work.get(item.work_id),
                )
            elif item.action is WorkAction.RESOLVE_OWNER:
                resolution = self._resolve_owner_item(
                    item,
                    records,
                    owner_by_domain,
                    proof_by_domain,
                    owner_loss_required=verify_owner_loss,
                )
            elif item.action is WorkAction.REWIRE_DEFAULT:
                resolution = self._resolve_default_item(
                    item,
                    records,
                    entries_by_domain,
                )
            elif item.action is WorkAction.REPAIR_CAUSALITY:
                resolution = self._resolve_causal_item(
                    item,
                    records,
                    causal_by_domain,
                )
            else:
                resolution = WorkResolution(
                    work_id=item.work_id,
                    status=ResolutionStatus.OPEN,
                    evidence_codes=("unsupported_work_action",),
                    evidence_paths=item.paths,
                    reason=f"unsupported work action {item.action.value}",
                )
            resolutions.append(resolution)
        summary = summarize(self.audit_input.queue.items, resolutions)
        owner_ready = (
            len(owner_results) == len(RuntimeDomain)
            and all(item.ready for item in owner_results)
            and (
                not verify_owner_loss
                or (
                    len(owner_loss_proofs) == len(RuntimeDomain)
                    and all(item.valid for item in owner_loss_proofs)
                )
            )
        )
        default_ready = (
            len(entry_results) >= len(RuntimeDomain)
            and all(
                any(item.ready for item in entry_results if item.domain is domain)
                for domain in RuntimeDomain
            )
        )
        causal_ready = all(
            resolution.resolved
            for item, resolution in zip(self.audit_input.queue.items, resolutions)
            if item.action is WorkAction.REPAIR_CAUSALITY and item.blocking
        )
        completed = time.time_ns()
        payload = {
            "schema": ABSORPTION_RECEIPT_SCHEMA,
            "revision": self.revision,
            "source_revision": self.audit_input.queue.revision,
            "source_queue_digest": self.audit_input.queue.digest,
            "source_receipt_digest": self.audit_input.receipt_digest,
            "started_at_ns": started,
            "completed_at_ns": completed,
            "owner_registry_digest": self.owner_registry.configuration_digest(),
            "default_registry_digest": self.default_registry.configuration_digest(),
            "source_boundary_digest": source_report.digest,
            "owner_loss_proofs": [item.to_dict() for item in owner_loss_proofs],
            "owner_probes": [item.to_dict() for item in owner_results],
            "default_probes": [item.to_dict() for item in entry_results],
            "causal_verifications": [item.to_dict() for item in causal_results],
            "resolutions": [item.to_dict() for item in resolutions],
            "summary": summary.to_dict(),
            "source_boundary_ready": source_report.ready,
            "owner_boundary_ready": owner_ready,
            "default_boundary_ready": default_ready,
            "causal_boundary_ready": causal_ready,
            "module_enabled": True,
        }
        return RuntimeAbsorptionReceipt(
            revision=self.revision,
            source_revision=self.audit_input.queue.revision,
            source_queue_digest=self.audit_input.queue.digest,
            source_receipt_digest=self.audit_input.receipt_digest,
            started_at_ns=started,
            completed_at_ns=completed,
            owner_registry_digest=str(payload["owner_registry_digest"]),
            default_registry_digest=str(payload["default_registry_digest"]),
            source_boundary_digest=source_report.digest,
            owner_loss_proofs=tuple(owner_loss_proofs),
            owner_probes=tuple(item.to_dict() for item in owner_results),
            default_probes=tuple(item.to_dict() for item in entry_results),
            causal_verifications=tuple(item.to_dict() for item in causal_results),
            resolutions=tuple(resolutions),
            summary=summary,
            source_boundary_ready=source_report.ready,
            owner_boundary_ready=owner_ready,
            default_boundary_ready=default_ready,
            causal_boundary_ready=causal_ready,
            module_enabled=True,
            receipt_digest=digest_payload(payload),
        )

    def _prove_owner_loss(self) -> tuple[OwnerLossProof, ...]:
        proofs: list[OwnerLossProof] = []
        for domain in RuntimeDomain:
            before = self.owner_registry.require_ready(domain)
            self.owner_registry.disable_domain(
                domain,
                "M3-S01B-01 owner-loss proof",
            )
            rejected = False
            fallback_success = False
            error_code = ""
            try:
                self.owner_registry.require_ready(domain)
                fallback_success = True
            except ProductizationContractError as error:
                error_code = error.code
                rejected = error.code == "canonical_owner_unavailable"
            finally:
                self.owner_registry.enable_domain(domain)
            after = self.owner_registry.require_ready(domain)
            payload = {
                "domain": domain.value,
                "ready_before": before.ready,
                "rejected_while_disabled": rejected,
                "fallback_success": fallback_success,
                "ready_after": after.ready,
                "generation_before": before.generation,
                "generation_after": after.generation,
                "error_code": error_code,
            }
            proofs.append(
                OwnerLossProof(
                    domain=domain,
                    ready_before=before.ready,
                    rejected_while_disabled=rejected,
                    fallback_success=fallback_success,
                    ready_after=after.ready,
                    generation_before=before.generation,
                    generation_after=after.generation,
                    error_code=error_code,
                    digest=digest_payload(payload),
                )
            )
        return tuple(proofs)

    @staticmethod
    def _resolve_source_item(
        item: M3WorkItem,
        records: Sequence[FindingRecord],
        result: Any,
    ) -> WorkResolution:
        if result is None:
            return WorkResolution(
                work_id=item.work_id,
                status=ResolutionStatus.OPEN,
                evidence_codes=("source_risk_result_missing",),
                evidence_paths=item.paths,
                reason="source boundary inspector did not return this work item",
            )
        return WorkResolution(
            work_id=item.work_id,
            status=result.status if result.resolved else ResolutionStatus.OPEN,
            evidence_codes=tuple(
                dict.fromkeys(
                    [
                        "structural_source_boundary",
                        *(record.code for record in records),
                        *(
                            evidence.decision.value
                            for evidence in result.path_evidence
                        ),
                    ]
                )
            ),
            evidence_paths=item.paths,
            reason=result.reason,
            details={"source_resolution": result.to_dict()},
        )

    @staticmethod
    def _resolve_owner_item(
        item: M3WorkItem,
        records: Sequence[FindingRecord],
        owner_by_domain: Mapping[RuntimeDomain, Any],
        proof_by_domain: Mapping[RuntimeDomain, OwnerLossProof],
        *,
        owner_loss_required: bool,
    ) -> WorkResolution:
        if len(item.domains) != 1:
            return WorkResolution(
                work_id=item.work_id,
                status=ResolutionStatus.OPEN,
                evidence_codes=("owner_domain_cardinality_invalid",),
                evidence_paths=item.paths,
                reason="owner resolution must bind exactly one canonical domain",
            )
        domain = item.domains[0]
        owner = owner_by_domain.get(domain)
        proof = proof_by_domain.get(domain)
        owner_ready = bool(owner and owner.ready)
        proof_ready = bool(proof and proof.valid) if owner_loss_required else True
        if owner_ready and proof_ready:
            status = ResolutionStatus.RESOLVED
            reason = "unique canonical owner is ready and owner loss rejects without fallback"
        else:
            status = ResolutionStatus.OPEN
            reason = "canonical owner readiness or owner-loss proof is incomplete"
        return WorkResolution(
            work_id=item.work_id,
            status=status,
            evidence_codes=tuple(
                dict.fromkeys(
                    [
                        "canonical_owner_probe",
                        (
                            "owner_loss_rejects_fallback"
                            if proof_ready
                            else "owner_loss_proof_missing"
                        ),
                        *(record.code for record in records),
                    ]
                )
            ),
            evidence_paths=item.paths,
            owner_receipt_ids=(
                (
                    stable_id(
                        "owner_probe",
                        domain.value,
                        owner.owner_token,
                        owner.generation,
                        owner.checked_at_ns,
                    ),
                )
                if owner is not None
                else ()
            ),
            reason=reason,
            details={
                "domain": domain.value,
                "owner_probe": owner.to_dict() if owner else None,
                "owner_loss_proof": proof.to_dict() if proof else None,
            },
        )

    @staticmethod
    def _resolve_default_item(
        item: M3WorkItem,
        records: Sequence[FindingRecord],
        entries_by_domain: Mapping[
            RuntimeDomain,
            Sequence[DefaultEntryProbeResult],
        ],
    ) -> WorkResolution:
        if len(item.domains) != 1:
            return WorkResolution(
                work_id=item.work_id,
                status=ResolutionStatus.OPEN,
                evidence_codes=("default_domain_cardinality_invalid",),
                evidence_paths=item.paths,
                reason="default rewiring must bind exactly one canonical domain",
            )
        domain = item.domains[0]
        entries = tuple(entries_by_domain.get(domain, ()))
        accepted = tuple(entry for entry in entries if entry.ready)
        status = ResolutionStatus.RESOLVED if accepted else ResolutionStatus.OPEN
        return WorkResolution(
            work_id=item.work_id,
            status=status,
            evidence_codes=tuple(
                dict.fromkeys(
                    [
                        (
                            "default_entry_owner_write_path_verified"
                            if accepted
                            else "default_entry_unreachable"
                        ),
                        *(record.code for record in records),
                    ]
                )
            ),
            evidence_paths=item.paths,
            owner_receipt_ids=tuple(
                stable_id(
                    "default_probe",
                    entry.entry_id,
                    entry.checked_at_ns,
                    entry.ready,
                )
                for entry in accepted
            ),
            reason=(
                "default entry reaches the selected owner and write path"
                if accepted
                else "no default entry reaches the selected owner and write path"
            ),
            details={"entries": [entry.to_dict() for entry in entries]},
        )

    def _resolve_causal_item(
        self,
        item: M3WorkItem,
        records: Sequence[FindingRecord],
        causal_by_domain: Mapping[RuntimeDomain, Any],
    ) -> WorkResolution:
        if item.domains:
            if len(item.domains) != 1:
                return WorkResolution(
                    work_id=item.work_id,
                    status=ResolutionStatus.OPEN,
                    evidence_codes=("causal_domain_cardinality_invalid",),
                    evidence_paths=item.paths,
                    reason="blocking causal repair must bind one canonical domain",
                )
            domain = item.domains[0]
            verification = causal_by_domain.get(domain)
            valid = bool(verification and verification.valid)
            return WorkResolution(
                work_id=item.work_id,
                status=(
                    ResolutionStatus.RESOLVED if valid else ResolutionStatus.OPEN
                ),
                evidence_codes=tuple(
                    dict.fromkeys(
                        [
                            (
                                "canonical_event_effect_verified"
                                if valid
                                else "canonical_event_effect_unverified"
                            ),
                            *(record.code for record in records),
                        ]
                    )
                ),
                evidence_paths=item.paths,
                causal_verification_ids=(
                    (verification.digest,) if verification is not None else ()
                ),
                reason=(
                    "canonical event is identity-bound to a committed mutation"
                    if valid
                    else "canonical event lacks a valid mutation binding"
                ),
                details={
                    "domain": domain.value,
                    "verification": (
                        verification.to_dict() if verification is not None else None
                    ),
                },
            )
        classifications: list[str] = []
        unresolved_names: list[str] = []
        for record in records:
            event_name = str(
                record.attributes.get("event_name")
                or record.attributes.get("event")
                or _event_name_from_finding(record.message)
                or ""
            )
            semantic = self.causal_verifier.classify_unregistered(
                event_name,
                {
                    **dict(record.attributes),
                    "source_path": record.path,
                    "finding_code": record.code,
                },
            )
            classifications.append(f"{event_name or record.fingerprint}:{semantic.value}")
            if semantic.value in {"unknown", "canonical_effect"}:
                unresolved_names.append(event_name or record.fingerprint)
        status = (
            ResolutionStatus.RESOLVED
            if not unresolved_names
            else ResolutionStatus.OPEN
        )
        return WorkResolution(
            work_id=item.work_id,
            status=status,
            evidence_codes=tuple(
                dict.fromkeys(
                    [
                        "semantic_event_classification",
                        *(record.code for record in records),
                    ]
                )
            ),
            evidence_paths=item.paths,
            reason=(
                "events are derived/diagnostic and do not claim canonical mutation"
                if status is ResolutionStatus.RESOLVED
                else "one or more unregistered events still look canonical or unknown"
            ),
            details={
                "classifications": classifications,
                "unresolved": unresolved_names,
            },
        )

    def _disabled_receipt(self, started: int) -> RuntimeAbsorptionReceipt:
        resolutions = tuple(
            WorkResolution(
                work_id=item.work_id,
                status=ResolutionStatus.OPEN,
                evidence_codes=("runtime_absorption_module_disabled",),
                evidence_paths=item.paths,
                reason="M3 runtime absorption module is disabled; no fallback is allowed",
            )
            for item in self.audit_input.queue.items
        )
        summary = summarize(self.audit_input.queue.items, resolutions)
        completed = time.time_ns()
        payload = {
            "schema": ABSORPTION_RECEIPT_SCHEMA,
            "revision": self.revision,
            "source_revision": self.audit_input.queue.revision,
            "source_queue_digest": self.audit_input.queue.digest,
            "source_receipt_digest": self.audit_input.receipt_digest,
            "started_at_ns": started,
            "completed_at_ns": completed,
            "owner_registry_digest": self.owner_registry.configuration_digest(),
            "default_registry_digest": self.default_registry.configuration_digest(),
            "source_boundary_digest": digest_payload("disabled"),
            "owner_loss_proofs": [],
            "owner_probes": [],
            "default_probes": [],
            "causal_verifications": [],
            "resolutions": [item.to_dict() for item in resolutions],
            "summary": summary.to_dict(),
            "source_boundary_ready": False,
            "owner_boundary_ready": False,
            "default_boundary_ready": False,
            "causal_boundary_ready": False,
            "module_enabled": False,
        }
        return RuntimeAbsorptionReceipt(
            revision=self.revision,
            source_revision=self.audit_input.queue.revision,
            source_queue_digest=self.audit_input.queue.digest,
            source_receipt_digest=self.audit_input.receipt_digest,
            started_at_ns=started,
            completed_at_ns=completed,
            owner_registry_digest=str(payload["owner_registry_digest"]),
            default_registry_digest=str(payload["default_registry_digest"]),
            source_boundary_digest=str(payload["source_boundary_digest"]),
            owner_loss_proofs=(),
            owner_probes=(),
            default_probes=(),
            causal_verifications=(),
            resolutions=resolutions,
            summary=summary,
            source_boundary_ready=False,
            owner_boundary_ready=False,
            default_boundary_ready=False,
            causal_boundary_ready=False,
            module_enabled=False,
            receipt_digest=digest_payload(payload),
        )


def summarize(
    items: Sequence[M3WorkItem],
    resolutions: Sequence[WorkResolution],
) -> AbsorptionSummary:
    if len(items) != len(resolutions):
        raise ProductizationContractError(
            "absorption_result_population_mismatch",
            "work-item and resolution populations differ",
        )
    item_by_id = {item.work_id: item for item in items}
    if len(item_by_id) != len(items):
        raise ProductizationContractError(
            "absorption_work_id_duplicate",
            "work-item population contains duplicate ids",
        )
    seen: set[str] = set()
    by_action: dict[str, dict[str, int]] = {}
    by_status: dict[str, int] = {}
    resolved = 0
    blocking_resolved = 0
    blocking = sum(item.blocking for item in items)
    for resolution in resolutions:
        if resolution.work_id in seen:
            raise ProductizationContractError(
                "absorption_resolution_duplicate",
                f"duplicate resolution for {resolution.work_id}",
            )
        seen.add(resolution.work_id)
        item = item_by_id.get(resolution.work_id)
        if item is None:
            raise ProductizationContractError(
                "absorption_resolution_unknown",
                f"resolution references unknown work item {resolution.work_id}",
            )
        action = item.action.value
        action_counts = by_action.setdefault(
            action,
            {"total": 0, "resolved": 0, "unresolved": 0},
        )
        action_counts["total"] += 1
        by_status[resolution.status.value] = by_status.get(
            resolution.status.value,
            0,
        ) + 1
        if resolution.resolved:
            resolved += 1
            action_counts["resolved"] += 1
            if item.blocking:
                blocking_resolved += 1
        else:
            action_counts["unresolved"] += 1
    missing = sorted(set(item_by_id) - seen)
    if missing:
        raise ProductizationContractError(
            "absorption_resolution_missing",
            "one or more work items have no resolution",
            details={"work_ids": missing},
        )
    return AbsorptionSummary(
        total=len(items),
        blocking=blocking,
        resolved=resolved,
        unresolved=len(items) - resolved,
        blocking_resolved=blocking_resolved,
        blocking_unresolved=blocking - blocking_resolved,
        by_action=by_action,
        by_status=by_status,
    )
