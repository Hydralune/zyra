from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import canonical_digest


class OutcomeAttributionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PolicyOutcomeAttribution:
    receipt_id: str
    run_id: str
    task_id: str
    requested_mode: str
    actual_profile_id: str
    actual_version: str
    attribution_class: str
    actual_outcome_recorded: bool
    diagnostic_executed: bool
    degraded: bool
    strongest_success_eligible: bool
    receipt_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.phase2-policy-outcome-attribution/v1",
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "requested_mode": self.requested_mode,
            "actual_profile_id": self.actual_profile_id,
            "actual_version": self.actual_version,
            "attribution_class": self.attribution_class,
            "actual_outcome_recorded": self.actual_outcome_recorded,
            "diagnostic_executed": self.diagnostic_executed,
            "degraded": self.degraded,
            "strongest_success_eligible": self.strongest_success_eligible,
            "receipt_digest": self.receipt_digest,
        }


def attribute_runtime_receipt(
    receipt: Mapping[str, Any],
    *,
    diagnostic_receipt: Mapping[str, Any] | None = None,
) -> PolicyOutcomeAttribution:
    if receipt.get("schema") != "zyra.phase2-mechanism-execution-receipt/v1":
        raise OutcomeAttributionError("unsupported mechanism execution receipt")
    expected_id = str(receipt.get("receipt_id") or "")
    payload = dict(receipt)
    payload.pop("schema", None)
    payload.pop("receipt_id", None)
    observed_id = "mechanism_execution_" + canonical_digest(payload)[:24]
    if expected_id != observed_id:
        raise OutcomeAttributionError("mechanism execution receipt digest mismatch")

    degraded = receipt.get("degraded") is True
    actual_outcome = receipt.get("actual_outcome_recorded") is True
    actual_profile = str(receipt.get("actual_profile_id") or "")
    attribution_class = str(receipt.get("outcome_attribution") or "")
    eligible = receipt.get("strongest_success_eligible") is True

    diagnostic_executed = False
    if diagnostic_receipt is not None:
        if (
            diagnostic_receipt.get("schema")
            != "zyra.phase2-mechanism-diagnostic-receipt/v1"
        ):
            raise OutcomeAttributionError("unsupported diagnostic receipt")
        diagnostic_executed = diagnostic_receipt.get("executed") is True
        if diagnostic_executed:
            raise OutcomeAttributionError(
                "diagnostic proposals cannot be attributed as executed"
            )
        if diagnostic_receipt.get("actual_outcome_recorded") is not False:
            raise OutcomeAttributionError(
                "diagnostic receipts cannot claim an actual outcome"
            )
        if attribution_class != "baseline_with_read_only_diagnostic":
            raise OutcomeAttributionError(
                "diagnostic receipt is mixed with the wrong outcome class"
            )

    if degraded and eligible:
        raise OutcomeAttributionError(
            "degraded fallback cannot count as strongest success"
        )
    if eligible and (
        actual_profile != "phase2_strongest_v1"
        or attribution_class != "strongest_default"
        or not actual_outcome
    ):
        raise OutcomeAttributionError(
            "strongest success attribution lacks a real strongest outcome"
        )
    if attribution_class in {
        "baseline",
        "degraded_baseline",
        "baseline_with_read_only_diagnostic",
    } and actual_profile != "phase1_deterministic_baseline":
        raise OutcomeAttributionError(
            "baseline attribution names a non-baseline actual profile"
        )

    return PolicyOutcomeAttribution(
        receipt_id=expected_id,
        run_id=str(receipt.get("run_id") or ""),
        task_id=str(receipt.get("task_id") or ""),
        requested_mode=str(receipt.get("requested_mode") or ""),
        actual_profile_id=actual_profile,
        actual_version=str(receipt.get("actual_version") or ""),
        attribution_class=attribution_class,
        actual_outcome_recorded=actual_outcome,
        diagnostic_executed=diagnostic_executed,
        degraded=degraded,
        strongest_success_eligible=eligible,
        receipt_digest=canonical_digest(receipt),
    )


__all__ = [
    "OutcomeAttributionError",
    "PolicyOutcomeAttribution",
    "attribute_runtime_receipt",
]
