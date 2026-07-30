from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zyra_orchestration.topology_policy.contracts import (
    FrozenDict,
    PhysicalDispatchReceipt,
    canonical_digest,
)
from zyra_scheduler.dispatch_evidence import (
    PhysicalDispatchReceiptValidator,
    PhysicalDispatchValidationReport,
)


PHYSICAL_DISPATCH_GATE_SCHEMA = "zyra.physical-dispatch-gate/v1"
PHYSICAL_DISPATCH_EVIDENCE_INDEX_SCHEMA = (
    "zyra.physical-dispatch-evidence-index/v1"
)


@dataclass(frozen=True, slots=True)
class DispatchLaneEvidence:
    condition: str
    receipt: PhysicalDispatchReceipt
    validation: PhysicalDispatchValidationReport

    def to_dict(self) -> dict[str, Any]:
        identity = dict(self.receipt.physical_identity)
        provider = dict(self.receipt.provider_evidence)
        return {
            "condition": self.condition,
            "receipt_digest": self.receipt.digest,
            "location": identity.get("location"),
            "physical_attempt_id": self.receipt.physical_attempt_id,
            "node_id": identity.get("node_id"),
            "pid": identity.get("pid"),
            "failure_boundary_id": identity.get("failure_boundary_id"),
            "provider_request_id": provider.get("request_id"),
            "artifact_ref": self.receipt.artifact_ref.ref_id,
            "verifier_ref": self.receipt.verifier_ref.ref_id,
            "real_gate_closed": self.validation.real_gate_closed,
            "blockers": list(self.validation.blockers),
        }


@dataclass(frozen=True, slots=True)
class PhysicalDispatchGateReport:
    gate_closed: bool
    checks: FrozenDict
    blockers: tuple[str, ...]
    lanes: tuple[DispatchLaneEvidence, ...]
    privacy_cloud_dispatch_count: int
    real_location_count: int
    receipt_digest: str
    schema: str = PHYSICAL_DISPATCH_GATE_SCHEMA

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "gate_closed": self.gate_closed,
            "checks": dict(self.checks),
            "blockers": list(self.blockers),
            "lanes": [item.to_dict() for item in self.lanes],
            "privacy_cloud_dispatch_count": self.privacy_cloud_dispatch_count,
            "real_location_count": self.real_location_count,
        }
        if include_digest:
            value["receipt_digest"] = self.receipt_digest
        return value


def evaluate_physical_dispatch_gate(
    lanes: Sequence[tuple[str, PhysicalDispatchReceipt]],
    *,
    require_conditions: Sequence[str] = (
        "normal",
        "privacy_local_only",
        "local_load",
        "edge_health",
        "edge_disconnect",
        "cloud_unavailable",
        "latency_cost",
    ),
) -> PhysicalDispatchGateReport:
    validator = PhysicalDispatchReceiptValidator()
    evaluated = tuple(
        DispatchLaneEvidence(
            condition=str(condition),
            receipt=receipt,
            validation=validator.validate(receipt),
        )
        for condition, receipt in lanes
    )
    by_condition = {item.condition: item for item in evaluated}
    by_location: dict[str, list[DispatchLaneEvidence]] = {}
    for item in evaluated:
        location = str(item.receipt.physical_identity.get("location") or "")
        by_location.setdefault(location, []).append(item)
    privacy_cloud_dispatch_count = sum(
        1
        for item in evaluated
        if item.receipt.privacy_class in {"restricted", "local-only"}
        and item.receipt.physical_identity.get("location") == "cloud"
    )
    identities = {
        (
            str(item.receipt.physical_identity.get("location") or ""),
            str(item.receipt.physical_identity.get("node_id") or ""),
            int(item.receipt.physical_identity.get("pid") or 0),
            str(item.receipt.provider_evidence.get("request_id") or ""),
        )
        for item in evaluated
    }
    conditions_present = all(
        str(condition) in by_condition for condition in require_conditions
    )
    recovery_linked = any(item.receipt.recovery_evidence for item in evaluated)
    checks = {
        "local_real": any(
            item.validation.real_gate_closed for item in by_location.get("local", ())
        ),
        "edge_real": any(
            item.validation.real_gate_closed for item in by_location.get("edge", ())
        ),
        "cloud_real": any(
            item.validation.real_gate_closed for item in by_location.get("cloud", ())
        ),
        "all_receipts_real": bool(evaluated)
        and all(item.validation.real_gate_closed for item in evaluated),
        "condition_lanes_present": conditions_present,
        "privacy_cloud_dispatch_zero": privacy_cloud_dispatch_count == 0,
        "physical_identity_changes": len(identities) >= 3,
        "fault_recovery_linked": recovery_linked,
        "simulated_receipt_zero": all(
            not item.receipt.simulated and not item.receipt.semantic_only
            for item in evaluated
        ),
    }
    blockers = tuple(
        sorted(name for name, passed in checks.items() if not passed)
    )
    payload = {
        "schema": PHYSICAL_DISPATCH_GATE_SCHEMA,
        "checks": checks,
        "blockers": list(blockers),
        "lanes": [item.to_dict() for item in evaluated],
        "privacy_cloud_dispatch_count": privacy_cloud_dispatch_count,
        "real_location_count": len(by_location),
    }
    return PhysicalDispatchGateReport(
        gate_closed=not blockers,
        checks=FrozenDict(checks),
        blockers=blockers,
        lanes=evaluated,
        privacy_cloud_dispatch_count=privacy_cloud_dispatch_count,
        real_location_count=len(by_location),
        receipt_digest=canonical_digest(payload),
    )


def build_redacted_dispatch_evidence_index(
    *,
    report: PhysicalDispatchGateReport,
    environment_profile: Mapping[str, Any],
    target_commit: str,
) -> dict[str, Any]:
    profile = {
        str(key): value
        for key, value in environment_profile.items()
        if not any(
            marker in str(key).casefold()
            for marker in ("secret", "password", "token", "api_key")
        )
    }
    value = {
        "schema": PHYSICAL_DISPATCH_EVIDENCE_INDEX_SCHEMA,
        "target_commit": target_commit,
        "gate_report_digest": report.receipt_digest,
        "gate_closed": report.gate_closed,
        "environment_profile": profile,
        "lanes": [item.to_dict() for item in report.lanes],
        "credential_material_persisted": False,
        "simulated_evidence_closes_gate": False,
    }
    value["digest"] = canonical_digest(value)
    return value


__all__ = [
    "DispatchLaneEvidence",
    "PHYSICAL_DISPATCH_EVIDENCE_INDEX_SCHEMA",
    "PHYSICAL_DISPATCH_GATE_SCHEMA",
    "PhysicalDispatchGateReport",
    "build_redacted_dispatch_evidence_index",
    "evaluate_physical_dispatch_gate",
]
