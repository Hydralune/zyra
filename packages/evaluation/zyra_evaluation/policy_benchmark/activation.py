from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import canonical_digest


class OutcomeAttributionError(ValueError):
    pass


STRONGEST_PREFLIGHT_ACTIVATION_SCHEMA = (
    "zyra.strongest-preflight-activation-report/v1"
)
_ACTIVATION_MECHANISMS = (
    "arg_designer",
    "card",
    "agentprune",
    "maas",
)


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


@dataclass(frozen=True, slots=True)
class StrongestPreflightActivationReport:
    preflight_id: str
    profile_family: str
    profile_version: str
    preflight_report_digest: str
    readiness_report_digest: str
    sealed_run_admission_eligible: bool
    default_activation_allowed: bool
    conclusion: str
    blockers: tuple[str, ...]
    passed_gates: tuple[str, ...]
    readiness: Mapping[str, Mapping[str, str]]
    resolver_before: str
    resolver_after: str
    raw_receipt_refs: tuple[str, ...]
    schema: str = STRONGEST_PREFLIGHT_ACTIVATION_SCHEMA

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        active_revalidation = (
            self.conclusion == "phase2_strongest_v1_revalidated"
        )
        value = {
            "schema": self.schema,
            "preflight_id": self.preflight_id,
            "profile_family": self.profile_family,
            "profile_version": self.profile_version,
            "preflight_report_digest": self.preflight_report_digest,
            "readiness_report_digest": self.readiness_report_digest,
            "sealed_run_admission_eligible": (
                self.sealed_run_admission_eligible
            ),
            "default_activation_allowed": self.default_activation_allowed,
            "conclusion": self.conclusion,
            "blockers": list(self.blockers),
            "passed_gates": list(self.passed_gates),
            "readiness": {
                key: dict(self.readiness[key]) for key in sorted(self.readiness)
            },
            "resolver_before": self.resolver_before,
            "resolver_after": self.resolver_after,
            "raw_receipt_refs": list(self.raw_receipt_refs),
            "activation_semantics": {
                "scope": (
                    "final_phase2_strongest_v1_revalidation"
                    if active_revalidation
                    else "admission_to_P2-S06-02_sealed_runs"
                ),
                "normal_resolver_mutated": False,
                "default_profile_activated": active_revalidation,
                "explicit_activation_transition_required_later": (
                    not active_revalidation
                ),
                "baseline_retained_until_transition": (
                    not active_revalidation
                ),
            },
        }
        if include_digest:
            value["activation_report_digest"] = self.digest
        return value


def build_strongest_preflight_activation_report(
    *,
    preflight_report: Mapping[str, Any],
    readiness_report: Mapping[str, Any],
    raw_receipt_refs: tuple[str, ...],
) -> StrongestPreflightActivationReport:
    blockers: list[str] = []
    passed: list[str] = []
    if preflight_report.get("status") != "completed":
        blockers.append("preflight_status")
    else:
        passed.append("preflight_status")

    hard_gates = preflight_report.get("hard_gates")
    if not isinstance(hard_gates, Mapping):
        blockers.append("hard_gates")
    else:
        for gate_id in preflight_report.get("hard_gate_order", ()):
            if hard_gates.get(gate_id) is True:
                passed.append(f"hard_gate:{gate_id}")
            else:
                blockers.append(f"hard_gate:{gate_id}")

    mechanisms = readiness_report.get("mechanisms")
    readiness: dict[str, dict[str, str]] = {}
    if not isinstance(mechanisms, Mapping):
        blockers.append("readiness_report")
    else:
        for mechanism_id in _ACTIVATION_MECHANISMS:
            value = mechanisms.get(mechanism_id)
            if not isinstance(value, Mapping):
                blockers.append(f"readiness:{mechanism_id}:missing")
                continue
            stage = str(value.get("readiness_stage") or "")
            status = str(value.get("status") or "")
            readiness[mechanism_id] = {
                "stage": stage,
                "status": status,
            }
            if stage != "activation_ready":
                blockers.append(f"readiness:{mechanism_id}:stage")
            elif status != "deterministic_ready":
                blockers.append(f"readiness:{mechanism_id}:status")
            else:
                passed.append(f"readiness:{mechanism_id}")

    resolver_before = str(preflight_report.get("resolver_before") or "")
    resolver_after = str(preflight_report.get("resolver_after") or "")
    execution_mode = str(
        preflight_report.get("execution_mode")
        or "pre_activation_validation"
    )
    expected_resolver = (
        "phase2_strongest_v1"
        if execution_mode == "active_default_revalidation"
        else "phase1_deterministic_baseline"
    )
    if (
        resolver_before != expected_resolver
        or resolver_after != expected_resolver
    ):
        blockers.append("resolver_retention")
    else:
        passed.append("resolver_retention")

    eligible = not blockers
    active_revalidation = (
        execution_mode == "active_default_revalidation"
    )
    return StrongestPreflightActivationReport(
        preflight_id=str(preflight_report.get("preflight_id") or ""),
        profile_family=str(preflight_report.get("profile_family") or ""),
        profile_version=str(preflight_report.get("profile_version") or ""),
        preflight_report_digest=str(
            preflight_report.get("report_digest") or ""
        ),
        readiness_report_digest=str(
            readiness_report.get("report_digest") or ""
        ),
        sealed_run_admission_eligible=eligible,
        default_activation_allowed=eligible and active_revalidation,
        conclusion=(
            "phase2_strongest_v1_revalidated"
            if eligible and active_revalidation
            else "admit_to_P2-S06-02"
            if eligible
            else "retain_phase1_baseline"
        ),
        blockers=tuple(sorted(set(blockers))),
        passed_gates=tuple(sorted(set(passed))),
        readiness=readiness,
        resolver_before=resolver_before,
        resolver_after=resolver_after,
        raw_receipt_refs=raw_receipt_refs,
    )


__all__ = [
    "OutcomeAttributionError",
    "PolicyOutcomeAttribution",
    "STRONGEST_PREFLIGHT_ACTIVATION_SCHEMA",
    "StrongestPreflightActivationReport",
    "attribute_runtime_receipt",
    "build_strongest_preflight_activation_report",
]
