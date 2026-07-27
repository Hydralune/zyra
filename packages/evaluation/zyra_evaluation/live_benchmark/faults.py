from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import (
    bounded_integer,
    bounded_text,
    digest,
    elapsed_ms,
    identity,
    invalid,
    mapping,
    require_digest,
    require_keys,
    sequence,
    utc_now,
)


REQUIRED_FAULT_KINDS = frozenset(
    {
        "exception",
        "worker-loss",
        "node-loss",
        "provider-failure",
        "edge-disconnect",
        "network-failure",
        "tool-failure",
    }
)
REQUIRED_CHANGE_KINDS = frozenset(
    {
        "scope-change",
        "acceptance-criteria-change",
    }
)


class FaultCoverageVerifier:
    def verify(self, value: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
        selected_run_id = identity(run_id, "run id")
        recovery_expected = value.get("recovery_expected") is not False
        injections = tuple(
            mapping(item, "fault injection")
            for item in sequence(value.get("injections"), "fault injections")
        )
        changes = tuple(
            mapping(item, "requirement change")
            for item in sequence(
                value.get("requirement_changes"),
                "requirement changes",
            )
        )
        fault_receipt = self._faults(
            injections,
            selected_run_id,
            recovery_expected=recovery_expected,
        )
        change_receipt = self._changes(
            changes,
            selected_run_id,
            recovery_expected=recovery_expected,
        )
        delivery_succeeded = value.get("final_delivery_succeeded") is True
        if recovery_expected and not delivery_succeeded:
            raise invalid(
                "benchmark_delivery_after_fault_failed",
                "Run did not complete its final delivery after faults.",
                phase="fault",
            )
        if not recovery_expected and delivery_succeeded:
            raise invalid(
                "benchmark_disabled_recovery_delivery_inconsistent",
                "No-recovery ablation incorrectly reported successful post-fault delivery.",
                phase="fault",
            )
        final_artifact_digest = require_digest(
            value.get("final_artifact_digest"),
            "final artifact digest after faults",
        )
        output = {
            "schema": "zyra.live-benchmark-fault-coverage/v1",
            "valid": True,
            "run_id": selected_run_id,
            "fault_receipt": fault_receipt,
            "requirement_change_receipt": change_receipt,
            "recovery_expected": recovery_expected,
            "final_delivery_succeeded": delivery_succeeded,
            "final_artifact_digest": final_artifact_digest,
            "fault_bundle_digest": digest(value),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _faults(
        self,
        injections: Sequence[Mapping[str, Any]],
        run_id: str,
        *,
        recovery_expected: bool,
    ) -> dict[str, Any]:
        if not injections:
            raise invalid(
                "benchmark_fault_injections_empty",
                "Formal run contains no fault injections.",
                phase="fault",
            )
        findings: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        kinds: Counter[str] = Counter()
        stage_counts: Counter[str] = Counter()
        mttr_values: list[int] = []
        detection_values: list[int] = []
        recovery_values: list[int] = []
        route_change_count = 0
        checkpoint_restore_count = 0
        for index, item in enumerate(injections):
            injection_id = identity(
                item.get("injection_id"),
                f"fault injection[{index}] id",
            )
            if injection_id in identifiers:
                findings.append(
                    {
                        "code": "fault-injection-duplicate",
                        "injection_id": injection_id,
                    }
                )
            identifiers.add(injection_id)
            kind = (
                bounded_text(
                    item.get("kind"),
                    f"fault injection {injection_id} kind",
                    maximum_bytes=128,
                )
                .lower()
                .replace("_", "-")
            )
            stage = (
                bounded_text(
                    item.get("stage"),
                    f"fault injection {injection_id} stage",
                    maximum_bytes=128,
                )
                .lower()
                .replace("_", "-")
            )
            kinds[kind] += 1
            stage_counts[stage] += 1
            if item.get("run_id") not in {None, run_id}:
                findings.append(
                    {
                        "code": "fault-run-mismatch",
                        "injection_id": injection_id,
                    }
                )
            if item.get("observed") is not True:
                findings.append(
                    {
                        "code": "fault-not-observed",
                        "injection_id": injection_id,
                    }
                )
            recovered = item.get("recovered") is True
            resumed = item.get("resumed") is True
            if recovery_expected and not recovered:
                findings.append(
                    {
                        "code": "fault-not-recovered",
                        "injection_id": injection_id,
                    }
                )
            if recovery_expected and not resumed:
                findings.append(
                    {
                        "code": "fault-not-resumed",
                        "injection_id": injection_id,
                    }
                )
            if not recovery_expected and (recovered or resumed):
                findings.append(
                    {
                        "code": "disabled-recovery-fault-inconsistent",
                        "injection_id": injection_id,
                    }
                )
            if item.get("manual_intervention") is True:
                findings.append(
                    {
                        "code": "fault-manually-recovered",
                        "injection_id": injection_id,
                    }
                )
            require_digest(
                item.get("state_before_digest"),
                f"fault {injection_id} state before digest",
            )
            require_digest(
                item.get("state_after_digest"),
                f"fault {injection_id} state after digest",
            )
            injected_to_detected = elapsed_ms(
                item.get("injected_at"),
                item.get("detected_at"),
                f"fault {injection_id} detection",
            )
            detected_to_recovered = elapsed_ms(
                item.get("detected_at"),
                item.get("recovered_at"),
                f"fault {injection_id} recovery",
            )
            injected_to_resumed = elapsed_ms(
                item.get("injected_at"),
                item.get("resumed_at"),
                f"fault {injection_id} MTTR",
            )
            declared_mttr = bounded_integer(
                item.get("mttr_ms"),
                f"fault {injection_id} declared MTTR",
                minimum=0,
                maximum=7 * 24 * 60 * 60 * 1000,
            )
            if declared_mttr != injected_to_resumed:
                findings.append(
                    {
                        "code": "fault-mttr-mismatch",
                        "injection_id": injection_id,
                        "declared": declared_mttr,
                        "observed": injected_to_resumed,
                    }
                )
            if item.get("route_before") and item.get("route_after"):
                if item["route_before"] != item["route_after"]:
                    route_change_count += 1
            if item.get("checkpoint_restored") is True:
                checkpoint_restore_count += 1
            detection_values.append(injected_to_detected)
            recovery_values.append(detected_to_recovered)
            mttr_values.append(injected_to_resumed)
        if recovery_expected and route_change_count == 0:
            findings.append({"code": "fault-route-change-missing"})
        if recovery_expected and checkpoint_restore_count == 0:
            findings.append({"code": "fault-checkpoint-restore-missing"})
        if len(stage_counts) < 3:
            findings.append(
                {
                    "code": "fault-stage-diversity-insufficient",
                    "stages": sorted(stage_counts),
                }
            )
        if findings:
            raise invalid(
                "benchmark_fault_coverage_invalid",
                "Fault campaign did not prove autonomous recovery coverage.",
                phase="fault",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-fault-injection-verification/v1",
            "valid": True,
            "fault_count": len(injections),
            "kind_counts": dict(sorted(kinds.items())),
            "stage_counts": dict(sorted(stage_counts.items())),
            "route_change_count": route_change_count,
            "checkpoint_restore_count": checkpoint_restore_count,
            "recovery_expected": recovery_expected,
            "recovered_count": sum(
                item.get("recovered") is True for item in injections
            ),
            "detection_ms": range_summary(detection_values),
            "recovery_ms": range_summary(recovery_values),
            "mttr_ms": range_summary(mttr_values),
            "fault_digest": digest(injections),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _changes(
        self,
        changes: Sequence[Mapping[str, Any]],
        run_id: str,
        *,
        recovery_expected: bool,
    ) -> dict[str, Any]:
        if not changes:
            raise invalid(
                "benchmark_requirement_changes_empty",
                "Formal run contains no requirement changes.",
                phase="fault",
            )
        findings: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        kinds: Counter[str] = Counter()
        latencies: list[int] = []
        for index, item in enumerate(changes):
            change_id = identity(
                item.get("change_id"),
                f"requirement change[{index}] id",
            )
            if change_id in identifiers:
                findings.append(
                    {
                        "code": "requirement-change-duplicate",
                        "change_id": change_id,
                    }
                )
            identifiers.add(change_id)
            kind = (
                bounded_text(
                    item.get("kind"),
                    f"requirement change {change_id} kind",
                    maximum_bytes=128,
                )
                .lower()
                .replace("_", "-")
            )
            kinds[kind] += 1
            if item.get("run_id") not in {None, run_id}:
                findings.append(
                    {
                        "code": "requirement-change-run-mismatch",
                        "change_id": change_id,
                    }
                )
            accepted = item.get("accepted") is True
            replanned = item.get("replanned") is True
            reverified = item.get("reverified") is True
            delivered = item.get("delivered") is True
            if not accepted:
                findings.append(
                    {
                        "code": "requirement-change-not-accepted",
                        "change_id": change_id,
                    }
                )
            if recovery_expected and not replanned:
                findings.append(
                    {
                        "code": "requirement-change-not-replanned",
                        "change_id": change_id,
                    }
                )
            if recovery_expected and not reverified:
                findings.append(
                    {
                        "code": "requirement-change-not-reverified",
                        "change_id": change_id,
                    }
                )
            if recovery_expected and not delivered:
                findings.append(
                    {
                        "code": "requirement-change-not-delivered",
                        "change_id": change_id,
                    }
                )
            if not recovery_expected and (replanned or reverified or delivered):
                findings.append(
                    {
                        "code": "disabled-recovery-change-inconsistent",
                        "change_id": change_id,
                    }
                )
            before = require_digest(
                item.get("requirement_before_digest"),
                f"requirement change {change_id} before digest",
            )
            after = require_digest(
                item.get("requirement_after_digest"),
                f"requirement change {change_id} after digest",
            )
            if before == after:
                findings.append(
                    {
                        "code": "requirement-change-no-semantic-delta",
                        "change_id": change_id,
                    }
                )
            latencies.append(
                elapsed_ms(
                    item.get("observed_at"),
                    item.get("delivered_at"),
                    f"requirement change {change_id}",
                )
            )
        if findings:
            raise invalid(
                "benchmark_requirement_change_coverage_invalid",
                "Requirement-change campaign did not replan and deliver.",
                phase="fault",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-requirement-change-verification/v1",
            "valid": True,
            "change_count": len(changes),
            "kind_counts": dict(sorted(kinds.items())),
            "recovery_expected": recovery_expected,
            "delivered_count": sum(
                item.get("delivered") is True for item in changes
            ),
            "delivery_latency_ms": range_summary(latencies),
            "change_digest": digest(changes),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output


def verify_campaign_fault_coverage(
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not receipts:
        raise invalid(
            "benchmark_campaign_fault_receipts_empty",
            "Formal campaign contains no admitted fault receipts.",
            phase="fault",
        )
    fault_kinds: Counter[str] = Counter()
    change_kinds: Counter[str] = Counter()
    recovery_modes: Counter[str] = Counter()
    successful_delivery_count = 0
    expected_failure_count = 0
    for receipt in receipts:
        fault = mapping(receipt.get("fault_receipt"), "fault receipt")
        changes = mapping(
            receipt.get("requirement_change_receipt"),
            "requirement change receipt",
        )
        fault_kinds.update(
            {
                str(key): int(value)
                for key, value in dict(fault.get("kind_counts") or {}).items()
            }
        )
        change_kinds.update(
            {
                str(key): int(value)
                for key, value in dict(changes.get("kind_counts") or {}).items()
            }
        )
        recovery_expected = receipt.get("recovery_expected") is not False
        recovery_modes["enabled" if recovery_expected else "disabled"] += 1
        successful_delivery_count += int(
            receipt.get("final_delivery_succeeded") is True
        )
        expected_failure_count += int(
            not recovery_expected
            and receipt.get("final_delivery_succeeded") is not True
        )
    findings: list[dict[str, Any]] = []
    missing_faults = sorted(REQUIRED_FAULT_KINDS - set(fault_kinds))
    missing_changes = sorted(REQUIRED_CHANGE_KINDS - set(change_kinds))
    if missing_faults:
        findings.append({"code": "fault-kinds-missing", "kinds": missing_faults})
    if missing_changes:
        findings.append(
            {"code": "requirement-change-kinds-missing", "kinds": missing_changes}
        )
    if recovery_modes["enabled"] == 0 or recovery_modes["disabled"] == 0:
        findings.append({"code": "recovery-ablation-mode-missing"})
    if successful_delivery_count == 0:
        findings.append({"code": "successful-post-fault-delivery-missing"})
    if expected_failure_count == 0:
        findings.append({"code": "no-recovery-effect-missing"})
    if findings:
        raise invalid(
            "benchmark_campaign_fault_coverage_invalid",
            "Formal campaign fault and requirement-change coverage is incomplete.",
            phase="fault",
            detail={"findings": findings},
        )
    output = {
        "schema": "zyra.live-benchmark-campaign-fault-coverage/v1",
        "valid": True,
        "receipt_count": len(receipts),
        "fault_kind_counts": dict(sorted(fault_kinds.items())),
        "requirement_change_kind_counts": dict(sorted(change_kinds.items())),
        "recovery_modes": dict(sorted(recovery_modes.items())),
        "successful_delivery_count": successful_delivery_count,
        "expected_no_recovery_failure_count": expected_failure_count,
        "verified_at": utc_now(),
    }
    output["receipt_digest"] = digest(output)
    return output


def range_summary(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        raise invalid(
            "benchmark_range_values_empty",
            "Cannot summarize an empty measurement series.",
            phase="fault",
        )
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "maximum": ordered[-1],
        "mean": sum(ordered) / len(ordered),
        "p50": percentile(ordered, 0.50),
        "p95": percentile(ordered, 0.95),
    }


def percentile(values: Sequence[int], fraction: float) -> float:
    if not values:
        raise invalid(
            "benchmark_percentile_values_empty",
            "Cannot compute a percentile for an empty series.",
            phase="fault",
        )
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)
