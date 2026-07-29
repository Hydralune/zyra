from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zyra_orchestration.topology_policy import MemoryContinuityReceipt


class ContinuityEvidenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ContinuityEvidenceReport:
    receipt_count: int
    critical_fact_count: int
    critical_fact_recall: float
    critical_provenance_coverage: float
    obligation_count: int
    obligation_retention: float
    stale_requirement_execution_count: int
    duplicate_completed_work_count: int
    failed_receipt_count: int
    downstream_usage_coverage: float

    @property
    def hard_gates_passed(self) -> bool:
        return (
            self.receipt_count > 0
            and self.critical_fact_recall == 1.0
            and self.critical_provenance_coverage == 1.0
            and self.obligation_retention == 1.0
            and self.stale_requirement_execution_count == 0
            and self.duplicate_completed_work_count == 0
            and self.failed_receipt_count == 0
            and self.downstream_usage_coverage == 1.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.memory-continuity-evidence-report/v1",
            "receipt_count": self.receipt_count,
            "critical_fact_count": self.critical_fact_count,
            "critical_fact_recall": self.critical_fact_recall,
            "critical_provenance_coverage": self.critical_provenance_coverage,
            "obligation_count": self.obligation_count,
            "obligation_retention": self.obligation_retention,
            "stale_requirement_execution_count": (
                self.stale_requirement_execution_count
            ),
            "duplicate_completed_work_count": self.duplicate_completed_work_count,
            "failed_receipt_count": self.failed_receipt_count,
            "downstream_usage_coverage": self.downstream_usage_coverage,
            "hard_gates_passed": self.hard_gates_passed,
        }


def build_continuity_evidence_report(
    receipts: tuple[MemoryContinuityReceipt, ...] | list[MemoryContinuityReceipt],
) -> ContinuityEvidenceReport:
    critical_total = 0
    critical_present = 0
    provenance_present = 0
    obligation_total = 0
    obligation_retained = 0
    stale_execution = 0
    duplicates = 0
    failed = 0
    downstream_total = 0
    downstream_present = 0
    for receipt in receipts:
        critical = dict(receipt.critical_fact_results)
        obligations = dict(receipt.obligation_results)
        provenance_ids = {item.ref_id for item in receipt.provenance_refs}
        critical_total += len(critical)
        for fact_id, raw in critical.items():
            value = dict(raw) if isinstance(raw, dict) else dict(raw or {})
            if value.get("present_after") is True:
                critical_present += 1
            if fact_id in provenance_ids and value.get("provenance_ref"):
                provenance_present += 1
            downstream_total += 1
            if value.get("consumed") is True and value.get("usage_event_refs"):
                downstream_present += 1
        before_obligations = obligations.get("before_digest")
        after_obligations = obligations.get("after_digest")
        retained = obligations.get("retained") is True
        consumed = tuple(obligations.get("consumed_ids") or ())
        missing_consumption = tuple(obligations.get("missing_consumption") or ())
        if before_obligations or after_obligations:
            obligation_total += len(consumed) + len(missing_consumption)
            obligation_retained += len(consumed) if retained else 0
        stale_execution += int(
            obligations.get("stale_requirement_execution") is True
        )
        duplicates += len(obligations.get("duplicate_work_artifact_ids") or ())
        if receipt.continuity_result != "passed":
            failed += 1
    return ContinuityEvidenceReport(
        receipt_count=len(receipts),
        critical_fact_count=critical_total,
        critical_fact_recall=critical_present / max(critical_total, 1),
        critical_provenance_coverage=provenance_present / max(critical_total, 1),
        obligation_count=obligation_total,
        obligation_retention=obligation_retained / max(obligation_total, 1),
        stale_requirement_execution_count=stale_execution,
        duplicate_completed_work_count=duplicates,
        failed_receipt_count=failed,
        downstream_usage_coverage=downstream_present / max(downstream_total, 1),
    )


def require_continuity_hard_gates(report: ContinuityEvidenceReport) -> None:
    if not report.hard_gates_passed:
        raise ContinuityEvidenceError(
            "memory continuity evidence does not satisfy the Phase 2 hard gates"
        )


__all__ = [
    "ContinuityEvidenceError",
    "ContinuityEvidenceReport",
    "build_continuity_evidence_report",
    "require_continuity_hard_gates",
]
