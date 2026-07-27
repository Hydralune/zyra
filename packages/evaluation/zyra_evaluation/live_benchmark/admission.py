from __future__ import annotations

from collections import Counter
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
    parse_utc,
    require_digest,
    require_keys,
    sequence,
    utc_now,
)
from .matrix import assert_result_conditions
from .models import BenchmarkCell, Campaign, EvidenceMode
from .semantic_steps import SemanticStepVerifier


REQUIRED_LIVE_FIELDS = (
    "run_id",
    "cell_id",
    "campaign_id",
    "domain",
    "variant_id",
    "repetition",
    "seed",
    "input_revision",
    "task_family_digest",
    "commit_sha",
    "condition_digest",
    "sealed_policy_digest",
    "environment_digest",
    "hardware_digest",
    "deployment_digest",
    "provider_policy_digest",
    "verifier_digest",
    "failure_schedule_digest",
    "budget_digest",
    "source_evidence_digest",
    "evidence_mode",
    "fresh_input",
    "clean_state",
    "started_at",
    "completed_at",
    "human_intervention_count",
    "operator_intervention_count",
    "policy_decisions",
    "events",
    "effective_event_ids",
    "artifacts",
    "verification",
    "deployment",
    "faults",
    "metrics",
)


class LiveRunAdmission:
    def __init__(self, *, step_verifier: SemanticStepVerifier | None = None) -> None:
        self.step_verifier = step_verifier or SemanticStepVerifier()

    def admit(
        self,
        campaign: Campaign,
        cell: BenchmarkCell,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        require_keys(receipt, REQUIRED_LIVE_FIELDS, "live run receipt")
        self._identity(campaign, cell, receipt)
        condition_receipt = assert_result_conditions(campaign, cell, receipt)
        live_receipt = self._live_freshness(receipt)
        clean_state_receipt = self._clean_state(receipt)
        autonomy_receipt = self._autonomy(receipt)
        temporal_receipt = self._temporal(receipt)
        artifact_receipt = self._artifacts(receipt)
        events = tuple(
            mapping(item, "canonical event")
            for item in sequence(receipt.get("events"), "canonical events")
        )
        effective_event_ids = tuple(
            identity(item, "effective event id")
            for item in sequence(
                receipt.get("effective_event_ids"),
                "effective event ids",
            )
        )
        semantic_receipt = self.step_verifier.verify(
            events,
            declared_effective_event_ids=effective_event_ids,
            minimum_effective_steps=campaign.minimum_effective_steps,
            run_id=str(receipt["run_id"]),
        )
        declared_effective_count = receipt.get("effective_step_count")
        if declared_effective_count is not None:
            selected = bounded_integer(
                declared_effective_count,
                "declared effective step count",
                minimum=0,
                maximum=10_000_000,
            )
            if selected != semantic_receipt["effective_step_count"]:
                raise invalid(
                    "benchmark_effective_step_count_mismatch",
                    "Declared effective-step count differs from semantic classification.",
                    phase="admission",
                    detail={
                        "declared": selected,
                        "verified": semantic_receipt["effective_step_count"],
                    },
                )
        receipt_projection = dict(receipt)
        declared_digest = receipt_projection.pop("receipt_digest", None)
        computed_digest = digest(receipt_projection)
        if declared_digest is not None and require_digest(
            declared_digest,
            "live receipt digest",
        ) != computed_digest:
            raise invalid(
                "benchmark_live_receipt_digest_mismatch",
                "Live run receipt digest does not match its canonical payload.",
                phase="integrity",
            )
        source = mapping(
            receipt.get("source") or {},
            "run source",
        )
        output = {
            "schema": "zyra.live-benchmark-run-admission/v1",
            "valid": True,
            "campaign_id": campaign.campaign_id,
            "cell_id": cell.cell_id,
            "run_id": receipt["run_id"],
            "domain": cell.domain.value,
            "variant_id": cell.variant_id,
            "repetition": cell.repetition,
            "input_revision": cell.input_revision,
            "source_run_id": identity(
                source.get("source_live_run_id"),
                "source live run id",
            ),
            "source_archive_digest": require_digest(
                source.get("source_archive_digest"),
                "source archive digest",
            ),
            "variant_execution_digest": require_digest(
                source.get("variant_execution_digest"),
                "variant execution digest",
            ),
            "live_receipt_digest": computed_digest,
            "condition_receipt": condition_receipt,
            "live_freshness_receipt": live_receipt,
            "clean_state_receipt": clean_state_receipt,
            "autonomy_receipt": autonomy_receipt,
            "temporal_receipt": temporal_receipt,
            "semantic_step_receipt": semantic_receipt,
            "artifact_receipt": artifact_receipt,
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _identity(
        self,
        campaign: Campaign,
        cell: BenchmarkCell,
        receipt: Mapping[str, Any],
    ) -> None:
        expected = {
            "campaign_id": campaign.campaign_id,
            "cell_id": cell.cell_id,
            "domain": cell.domain.value,
            "variant_id": cell.variant_id,
            "repetition": cell.repetition,
            "seed": cell.seed,
        }
        findings = [
            {
                "field": key,
                "expected": value,
                "observed": receipt.get(key),
            }
            for key, value in expected.items()
            if receipt.get(key) != value
        ]
        identity(receipt.get("run_id"), "run id")
        if findings:
            raise invalid(
                "benchmark_live_identity_mismatch",
                "Live run receipt belongs to a different benchmark cell.",
                phase="admission",
                detail={"findings": findings},
            )

    def _live_freshness(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        try:
            mode = EvidenceMode(str(receipt.get("evidence_mode")))
        except ValueError as error:
            raise invalid(
                "benchmark_evidence_mode_invalid",
                "Live run receipt uses an unknown evidence mode.",
                phase="admission",
            ) from error
        findings: list[dict[str, Any]] = []
        if mode is not EvidenceMode.LIVE:
            findings.append(
                {"code": "formal-run-not-live", "evidence_mode": mode.value}
            )
        if receipt.get("fresh_input") is not True:
            findings.append({"code": "fresh-input-not-proven"})
        for marker in ("replay", "fixture", "synthetic", "pre_recorded"):
            if receipt.get(marker) is True:
                findings.append({"code": "non-live-marker", "marker": marker})
        source = mapping(
            receipt.get("source") or {"kind": "owner-execution"},
            "run source",
        )
        if source.get("live") is not True:
            findings.append({"code": "live-source-not-proven"})
        if source.get("replay") is True or source.get("fixture") is True:
            findings.append({"code": "source-declared-non-live"})
        if str(source.get("owner") or "").strip() == "":
            findings.append({"code": "live-source-owner-missing"})
        if findings:
            raise invalid(
                "benchmark_live_freshness_invalid",
                "Formal benchmark run is not a fresh live execution.",
                phase="admission",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-freshness-verification/v1",
            "valid": True,
            "evidence_mode": mode.value,
            "fresh_input": True,
            "source_owner": source["owner"],
            "source_digest": digest(source),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _clean_state(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        state = mapping(receipt.get("clean_state"), "clean state")
        require_keys(
            state,
            (
                "fresh_root",
                "before_digest",
                "input_digest",
                "cache_hits",
                "reused_database",
                "reused_artifacts",
                "hidden_output_count",
            ),
            "clean state",
        )
        before = require_digest(state.get("before_digest"), "state before digest")
        input_digest = require_digest(state.get("input_digest"), "input digest")
        findings: list[dict[str, Any]] = []
        if state.get("fresh_root") is not True:
            findings.append({"code": "fresh-root-not-proven"})
        count_fields = (
            "cache_hits",
            "reused_artifacts",
            "hidden_output_count",
        )
        for key in count_fields:
            value = bounded_integer(
                state.get(key),
                key,
                minimum=0,
                maximum=10_000_000,
            )
            if value != 0:
                findings.append({"code": f"{key}-nonzero", "observed": value})
        if state.get("reused_database") is True:
            findings.append({"code": "database-reused"})
        if input_digest != require_digest(
            receipt.get("task_input_digest") or input_digest,
            "task input digest",
        ):
            findings.append({"code": "clean-state-input-digest-mismatch"})
        if findings:
            raise invalid(
                "benchmark_clean_state_invalid",
                "Formal live run reused state or undeclared output.",
                phase="admission",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-clean-state-verification/v1",
            "valid": True,
            "before_digest": before,
            "input_digest": input_digest,
            "fresh_root": True,
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _autonomy(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        human_count = bounded_integer(
            receipt.get("human_intervention_count"),
            "human intervention count",
            minimum=0,
            maximum=1_000_000,
        )
        operator_count = bounded_integer(
            receipt.get("operator_intervention_count"),
            "operator intervention count",
            minimum=0,
            maximum=1_000_000,
        )
        decisions = tuple(
            mapping(item, "policy decision")
            for item in sequence(
                receipt.get("policy_decisions"),
                "policy decisions",
            )
        )
        if not decisions:
            raise invalid(
                "benchmark_policy_decisions_empty",
                "Sealed run must contain deterministic policy decisions.",
                phase="autonomy",
            )
        findings: list[dict[str, Any]] = []
        if human_count != 0:
            findings.append(
                {"code": "human-intervention", "count": human_count}
            )
        if operator_count != 0:
            findings.append(
                {"code": "operator-intervention", "count": operator_count}
            )
        policy_digest = require_digest(
            receipt.get("sealed_policy_digest"),
            "sealed policy digest",
        )
        action_counts: Counter[str] = Counter()
        risk_counts: Counter[str] = Counter()
        for index, decision in enumerate(decisions):
            decision_id = identity(
                decision.get("decision_id"),
                f"policy decision[{index}] id",
            )
            observed_policy = require_digest(
                decision.get("policy_digest"),
                f"policy decision {decision_id} digest",
            )
            if observed_policy != policy_digest:
                findings.append(
                    {
                        "code": "policy-digest-mismatch",
                        "decision_id": decision_id,
                    }
                )
            risk = bounded_text(
                decision.get("risk"),
                f"policy decision {decision_id} risk",
                maximum_bytes=64,
            ).lower()
            action = bounded_text(
                decision.get("action"),
                f"policy decision {decision_id} action",
                maximum_bytes=64,
            ).lower()
            risk_counts[risk] += 1
            action_counts[action] += 1
            if risk in {"high", "unknown"} and action not in {
                "deny",
                "deny-replan",
                "deny-recover-replan",
            }:
                findings.append(
                    {
                        "code": "sealed-risk-not-denied",
                        "decision_id": decision_id,
                        "risk": risk,
                        "action": action,
                    }
                )
            if action in {"ask", "approve", "manual-retry", "steer"}:
                findings.append(
                    {
                        "code": "interactive-action-in-sealed-run",
                        "decision_id": decision_id,
                        "action": action,
                    }
                )
            if decision.get("human") is True or decision.get("operator") is True:
                findings.append(
                    {
                        "code": "manual-policy-decision",
                        "decision_id": decision_id,
                    }
                )
        if findings:
            raise invalid(
                "benchmark_sealed_autonomy_invalid",
                "Formal run was not unattended under sealed policy.",
                phase="autonomy",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-autonomy-verification/v1",
            "valid": True,
            "sealed_policy_digest": policy_digest,
            "human_intervention_count": 0,
            "operator_intervention_count": 0,
            "decision_count": len(decisions),
            "risk_counts": dict(sorted(risk_counts.items())),
            "action_counts": dict(sorted(action_counts.items())),
            "decision_digest": digest(decisions),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _temporal(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        started = parse_utc(receipt.get("started_at"), "run started_at")
        completed = parse_utc(receipt.get("completed_at"), "run completed_at")
        duration = elapsed_ms(
            receipt.get("started_at"),
            receipt.get("completed_at"),
            "live run",
        )
        if duration <= 0:
            raise invalid(
                "benchmark_run_duration_invalid",
                "Formal live run must have a positive observed duration.",
                phase="admission",
            )
        now = parse_utc(utc_now(), "current time")
        if completed > now:
            raise invalid(
                "benchmark_run_completed_in_future",
                "Live run completion time is in the future.",
                phase="admission",
            )
        output = {
            "schema": "zyra.live-benchmark-temporal-verification/v1",
            "valid": True,
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "completed_at": completed.isoformat().replace("+00:00", "Z"),
            "duration_ms": duration,
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output

    def _artifacts(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        artifacts = tuple(
            mapping(item, "artifact")
            for item in sequence(receipt.get("artifacts"), "artifacts")
        )
        if not artifacts:
            raise invalid(
                "benchmark_artifacts_empty",
                "Formal live run must deliver at least one artifact.",
                phase="artifact",
            )
        identifiers: set[str] = set()
        digests: set[str] = set()
        findings: list[dict[str, Any]] = []
        for index, artifact in enumerate(artifacts):
            artifact_id = identity(
                artifact.get("artifact_id"),
                f"artifact[{index}] id",
            )
            artifact_digest = require_digest(
                artifact.get("sha256") or artifact.get("digest"),
                f"artifact {artifact_id} digest",
            )
            if artifact_id in identifiers:
                findings.append(
                    {"code": "artifact-id-duplicate", "artifact_id": artifact_id}
                )
            if artifact_digest in digests:
                findings.append(
                    {
                        "code": "artifact-content-duplicate",
                        "artifact_id": artifact_id,
                    }
                )
            identifiers.add(artifact_id)
            digests.add(artifact_digest)
            if artifact.get("stable") is not True:
                findings.append(
                    {"code": "artifact-not-stable", "artifact_id": artifact_id}
                )
            if artifact.get("final") is not True:
                findings.append(
                    {"code": "artifact-not-final", "artifact_id": artifact_id}
                )
            if artifact.get("verified") is not True:
                findings.append(
                    {"code": "artifact-not-verified", "artifact_id": artifact_id}
                )
        if findings:
            raise invalid(
                "benchmark_artifacts_invalid",
                "Formal live run artifacts failed stability or integrity admission.",
                phase="artifact",
                detail={"findings": findings},
            )
        output = {
            "schema": "zyra.live-benchmark-artifact-verification/v1",
            "valid": True,
            "artifact_count": len(artifacts),
            "artifact_ids": sorted(identifiers),
            "artifact_digest": digest(artifacts),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output


def verify_campaign_run_uniqueness(
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    unique_dimensions = {
        "run_id": [],
        "live_receipt_digest": [],
        "event_stream_digest": [],
        "variant_execution_digest": [],
    }
    paired_groups: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for receipt in receipts:
        unique_dimensions["run_id"].append(str(receipt.get("run_id") or ""))
        unique_dimensions["live_receipt_digest"].append(
            str(receipt.get("live_receipt_digest") or "")
        )
        unique_dimensions["variant_execution_digest"].append(
            str(receipt.get("variant_execution_digest") or "")
        )
        semantic = mapping(
            receipt.get("semantic_step_receipt") or {},
            "semantic receipt",
        )
        unique_dimensions["event_stream_digest"].append(
            str(semantic.get("event_stream_digest") or "")
        )
        group = (
            str(receipt.get("domain") or ""),
            int(receipt.get("repetition") or 0),
        )
        paired_groups.setdefault(group, []).append(receipt)
    for field, values in unique_dimensions.items():
        duplicates = sorted(
            item for item, count in Counter(values).items() if item and count > 1
        )
        if duplicates:
            findings.append(
                {
                    "code": f"{field.replace('_', '-')}-reused",
                    "values": duplicates[:100],
                }
            )
        if any(not item for item in values):
            findings.append({"code": f"{field.replace('_', '-')}-missing"})
    source_run_ids: list[str] = []
    source_archives: list[str] = []
    input_revisions: list[str] = []
    expected_variants = {
        "single-agent",
        "static-full-connect-multi-agent",
        "dynamic-heterogeneous-swarm",
        "no-scheduler",
        "no-memory-compact",
        "no-recovery",
        "no-low-entropy-communication",
    }
    for (domain, repetition), group in sorted(paired_groups.items()):
        source_runs = {str(item.get("source_run_id") or "") for item in group}
        archives = {
            str(item.get("source_archive_digest") or "") for item in group
        }
        revisions = {str(item.get("input_revision") or "") for item in group}
        variants = {str(item.get("variant_id") or "") for item in group}
        if (
            len(group) != len(expected_variants)
            or len(source_runs) != 1
            or len(archives) != 1
            or len(revisions) != 1
            or variants != expected_variants
        ):
            findings.append(
                {
                    "code": "paired-source-block-invalid",
                    "domain": domain,
                    "repetition": repetition,
                    "cell_count": len(group),
                    "source_run_count": len(source_runs),
                    "archive_count": len(archives),
                    "input_revision_count": len(revisions),
                    "missing_variants": sorted(expected_variants - variants),
                    "unexpected_variants": sorted(variants - expected_variants),
                }
            )
            continue
        source_run_ids.extend(source_runs)
        source_archives.extend(archives)
        input_revisions.extend(revisions)
    for field, values in (
        ("source-run-id", source_run_ids),
        ("source-archive-digest", source_archives),
        ("input-revision", input_revisions),
    ):
        duplicates = sorted(
            item for item, count in Counter(values).items() if item and count > 1
        )
        if duplicates:
            findings.append({"code": f"{field}-reused-across-pairs", "values": duplicates})
        if any(not item for item in values):
            findings.append({"code": f"{field}-missing"})
    if findings:
        raise invalid(
            "benchmark_run_uniqueness_invalid",
            "Formal repetitions are not independently identifiable.",
            phase="admission",
            detail={"findings": findings},
        )
    output = {
        "schema": "zyra.live-benchmark-run-uniqueness/v1",
        "valid": True,
        "run_count": len(receipts),
        "source_run_count": len(source_run_ids),
        "paired_block_count": len(paired_groups),
        "run_identity_digest": digest(
            {
                "unique_dimensions": unique_dimensions,
                "source_run_ids": source_run_ids,
                "source_archives": source_archives,
                "input_revisions": input_revisions,
            }
        ),
        "verified_at": utc_now(),
    }
    output["receipt_digest"] = digest(output)
    return output
