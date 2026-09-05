from __future__ import annotations

import hmac
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .canonical import (
    canonical_json,
    canonicalize,
    digest,
    file_digest,
    new_identity,
    path_within,
    require_digest,
    utc_now,
)
from .effective_steps import StepBatch
from .causal import CausalEvidenceValidator
from .errors import conflict, invalid, unavailable
from .metrics import ScenarioMetricCollector, verify_metric_dimensions
from .models import MetricSample, OwnerExecutionResult, ScenarioConfiguration, ScenarioMode


class EvidenceCollector:
    def __init__(
        self,
        *,
        artifact_root: str | Path,
        maximum_artifacts: int = 100_000,
        maximum_manifest_bytes: int = 64 * 1024 * 1024,
        disabled: bool | None = None,
    ) -> None:
        self._artifact_root = Path(artifact_root).resolve(strict=False)
        self._maximum_artifacts = maximum_artifacts
        self._maximum_manifest_bytes = maximum_manifest_bytes
        self._disabled = (
            disabled
            if disabled is not None
            else _disabled("ZYRA_SCENARIO_EVIDENCE_COLLECTOR_DISABLED")
        )

    def collect(
        self,
        *,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
        owner: OwnerExecutionResult,
        step_batch: StepBatch,
        policy_receipt: Mapping[str, Any],
        preflight_receipt: Mapping[str, Any],
        metric_samples: Iterable[MetricSample],
        metric_summary: Mapping[str, Any],
        coverage_receipt: Mapping[str, Any],
        source_audit: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require_enabled()
        samples = tuple(metric_samples)
        artifact_receipts = self._artifact_receipts(owner.artifacts)
        event_receipts = self._event_receipts(owner.events)
        dimension_receipt = verify_metric_dimensions(samples)
        if not dimension_receipt["valid"]:
            raise conflict(
                "scenario_metric_dimensions_invalid",
                "Formal evidence metric sample is missing required dimensions.",
                phase="evidence",
                detail=dimension_receipt,
            )
        causal_receipt = CausalEvidenceValidator().require_valid(
            step_batch,
            expected_run_id=owner.owner_run_id,
            expected_task_id=owner.task_id,
        )
        live_domain = owner.task.get("live_domain")
        live_domain = (
            dict(live_domain) if isinstance(live_domain, Mapping) else {}
        )
        domain_verification = owner.task.get("domain_verification")
        domain_verification = (
            dict(domain_verification)
            if isinstance(domain_verification, Mapping)
            else {}
        )
        placement = owner.task.get("placement")
        placement = dict(placement) if isinstance(placement, Mapping) else {}
        placement_verification = placement.get("verification")
        placement_verification = (
            dict(placement_verification)
            if isinstance(placement_verification, Mapping)
            else {}
        )
        causal_archive = owner.task.get("causal_archive")
        causal_archive = (
            dict(causal_archive)
            if isinstance(causal_archive, Mapping)
            else {}
        )
        live = configuration.scenario_id in {
            "live.software-delivery",
            "live.cross-source-research",
        }
        transition_count = len(step_batch.admitted)
        tier_counts = live_domain.get("tier_counts")
        tier_counts = dict(tier_counts) if isinstance(tier_counts, Mapping) else {}
        provider_counts = live_domain.get("provider_model_counts")
        provider_counts = (
            dict(provider_counts) if isinstance(provider_counts, Mapping) else {}
        )
        live_complete = bool(
            live
            and domain_verification.get("valid") is True
            and placement_verification.get("valid") is True
            and int(live_domain.get("recovered_fault_count") or 0)
            == int(live_domain.get("fault_count") or -1)
            and int(live_domain.get("fault_count") or 0) >= 5
            and causal_archive.get("manifest_digest")
        )
        manifest = {
            "schema": "zyra.scenario-evidence-manifest/v1",
            "manifest_id": new_identity("manifest"),
            "scenario_run_id": scenario_run_id,
            "scenario_id": configuration.scenario_id,
            "mode": configuration.mode.value,
            "definition_version": configuration.definition_version,
            "definition_digest": configuration.definition_digest,
            "configuration_digest": configuration.configuration_digest,
            "input_digest": configuration.input_digest,
            "seed": configuration.seed,
            "profile": configuration.profile.to_dict(),
            "policy_digest": configuration.policy.policy_digest,
            "owner": {
                "run_id": owner.owner_run_id,
                "task_id": owner.task_id,
                "started_at": owner.started_at,
                "completed_at": owner.completed_at,
                "task_digest": digest(owner.task),
                "owner_receipt_digest": digest(owner.owner_receipts),
            },
            "preflight_receipt": canonicalize(preflight_receipt),
            "policy_receipt": canonicalize(policy_receipt),
            "coverage_receipt": canonicalize(coverage_receipt),
            "effective_steps": step_batch.to_dict(),
            "causal_verification": causal_receipt,
            "metrics": {
                "summary": canonicalize(metric_summary),
                "dimension_receipt": dimension_receipt,
                "raw_samples": [item.to_dict() for item in samples],
                "raw_sample_digest": digest(
                    [item.to_dict() for item in samples]
                ),
            },
            "canonical_events": event_receipts,
            "artifacts": artifact_receipts,
            "source_audit": canonicalize(source_audit),
            "live_domain": {
                "summary": canonicalize(live_domain),
                "domain_verification": canonicalize(domain_verification),
                "placement_verification": canonicalize(
                    placement_verification
                ),
                "causal_archive": canonicalize(causal_archive),
                "provider_model_capability_count": len(provider_counts),
                "tier_count": len(tier_counts),
            },
            "claims": {
                "formal_foundation": configuration.mode is ScenarioMode.SEALED,
                "human_intervention_count": 0,
                "legacy_demo_fallback": False,
                "replay_evidence": False,
                "long_live_scenario_complete": live_complete,
                "two_thousand_step_gate_complete": (
                    live and transition_count >= 2_000
                ),
                "edge_cloud_dispatch_complete": bool(
                    False
                ),
                "provider_model_capabilities_complete": bool(
                    False
                ),
                "external_provider_execution_excluded": bool(
                    live
                    and configuration.profile.metadata.get(
                        "authenticated_provider_cli_allowed"
                    )
                    is False
                ),
                "authenticated_provider_cli_invoked": False,
                "fault_change_matrix_complete": bool(
                    live
                    and int(live_domain.get("fault_count") or 0) >= 5
                    and int(live_domain.get("recovered_fault_count") or 0)
                    == int(live_domain.get("fault_count") or -1)
                ),
                "domain_verifier_complete": bool(
                    live and domain_verification.get("valid") is True
                ),
                "causal_archive_complete": bool(
                    live and causal_archive.get("manifest_digest")
                ),
                "m2_exit_complete": False,
            },
            "collected_at": utc_now(),
        }
        serialized = canonical_json(manifest).encode("utf-8")
        if len(serialized) > self._maximum_manifest_bytes:
            raise conflict(
                "scenario_manifest_too_large",
                "Evidence manifest exceeds its configured byte budget.",
                phase="evidence",
                detail={
                    "size": len(serialized),
                    "maximum": self._maximum_manifest_bytes,
                },
            )
        manifest["manifest_digest"] = digest(manifest)
        verification = self.verify(manifest, mode=configuration.mode)
        manifest["verification_receipt"] = verification
        return manifest

    def verify(self, manifest: Mapping[str, Any], *, mode: ScenarioMode = ScenarioMode.SEALED) -> dict[str, Any]:
        self._require_enabled()
        copy = dict(manifest)
        expected = require_digest(
            copy.pop("manifest_digest", ""),
            "evidence manifest digest",
        )
        copy.pop("verification_receipt", None)
        actual = digest(copy)
        failures: list[dict[str, Any]] = []
        if not hmac.compare_digest(expected, actual):
            failures.append(
                {
                    "code": "manifest_digest_mismatch",
                    "expected": expected,
                    "actual": actual,
                }
            )
        claims = manifest.get("claims")
        claims = claims if isinstance(claims, Mapping) else {}
        # The caller supplies mode from canonical configuration. A manifest
        # cannot downgrade a formal verification by changing its own label.
        if str(manifest.get("mode") or "sealed") != mode.value:
            failures.append({"code": "scenario_mode_binding_mismatch"})
        if "mode" in manifest and claims.get("formal_foundation") is not (mode is ScenarioMode.SEALED):
            failures.append({"code": "formal_claim_mode_mismatch"})
        if int(claims.get("human_intervention_count") or 0) != 0:
            failures.append({"code": "human_intervention_count_nonzero"})
        if claims.get("legacy_demo_fallback") is not False:
            failures.append({"code": "legacy_demo_fallback_present"})
        scenario_id = str(manifest.get("scenario_id") or "")
        if scenario_id in {
            "live.software-delivery",
            "live.cross-source-research",
        }:
            for claim in (
                "long_live_scenario_complete",
                "two_thousand_step_gate_complete",
                "external_provider_execution_excluded",
                "fault_change_matrix_complete",
                "domain_verifier_complete",
                "causal_archive_complete",
            ):
                if claims.get(claim) is not True:
                    failures.append(
                        {"code": "live_claim_incomplete", "claim": claim}
                    )
            if claims.get("authenticated_provider_cli_invoked") is not False:
                failures.append(
                    {"code": "authenticated_provider_cli_invoked"}
                )
            live_domain = manifest.get("live_domain")
            live_domain = (
                live_domain if isinstance(live_domain, Mapping) else {}
            )
            domain_verification = live_domain.get("domain_verification")
            if (
                not isinstance(domain_verification, Mapping)
                or domain_verification.get("valid") is not True
            ):
                failures.append(
                    {"code": "live_domain_verification_invalid"}
                )
            placement_verification = live_domain.get(
                "placement_verification"
            )
            if (
                not isinstance(placement_verification, Mapping)
                or placement_verification.get("valid") is not True
            ):
                failures.append(
                    {"code": "live_placement_verification_invalid"}
                )
        steps = manifest.get("effective_steps")
        steps = steps if isinstance(steps, Mapping) else {}
        if steps.get("formal_valid") is not True:
            failures.append({"code": "effective_step_batch_invalid"})
        causal = manifest.get("causal_verification")
        if not isinstance(causal, Mapping) or causal.get("valid") is not True:
            failures.append({"code": "causal_verification_invalid"})
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            failures.append({"code": "artifact_evidence_missing"})
        else:
            failures.extend(self._verify_artifact_receipts(artifacts))
        events = manifest.get("canonical_events")
        if not isinstance(events, list) or not events:
            failures.append({"code": "canonical_event_evidence_missing"})
        else:
            failures.extend(self._verify_event_receipts(events))
        source_audit = manifest.get("source_audit")
        if not isinstance(source_audit, Mapping) or source_audit.get("valid") is not True:
            failures.append({"code": "source_role_audit_invalid"})
        failures.extend(self._verify_owner_bindings(manifest, mode=mode))
        failures.extend(self._verify_metric_receipts(manifest))
        receipt = {
            "schema": "zyra.scenario-evidence-verification/v1",
            "receipt_id": new_identity("evidence_verify"),
            "manifest_id": str(manifest.get("manifest_id") or ""),
            "manifest_digest": expected,
            "recomputed_manifest_digest": actual,
            "valid": not failures,
            "failures": failures,
            "verified_at": utc_now(),
            "fallback": False,
        }
        receipt["receipt_digest"] = digest(receipt)
        if failures:
            raise conflict(
                "scenario_evidence_verification_failed",
                "Scenario evidence manifest failed verification.",
                phase="evidence",
                detail=receipt,
            )
        return receipt

    def _artifact_receipts(
        self,
        artifacts: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for index, artifact in enumerate(artifacts):
            if index >= self._maximum_artifacts:
                raise conflict(
                    "scenario_artifact_count_exceeded",
                    "Artifact evidence exceeds the configured count budget.",
                    phase="evidence",
                )
            artifact_id = str(artifact.get("artifact_id") or "").strip()
            uri = str(
                artifact.get("uri")
                or artifact.get("path")
                or artifact.get("absolute_path")
                or ""
            ).strip()
            if not artifact_id or not uri:
                raise conflict(
                    "scenario_artifact_identity_missing",
                    "Artifact evidence is missing canonical identity or owner URI.",
                    phase="evidence",
                    detail={"index": index},
                )
            path = Path(uri).resolve(strict=False)
            if not path_within(path, self._artifact_root):
                raise conflict(
                    "scenario_artifact_outside_owner",
                    "Artifact evidence points outside the configured artifact owner.",
                    phase="evidence",
                    detail={
                        "artifact_id": artifact_id,
                        "path": str(path),
                        "owner_root": str(self._artifact_root),
                    },
                )
            if not path.is_file():
                raise conflict(
                    "scenario_artifact_missing",
                    "Artifact evidence bytes are unavailable.",
                    phase="evidence",
                    detail={"artifact_id": artifact_id, "path": str(path)},
                )
            checksum, size = file_digest(path)
            declared = str(
                artifact.get("sha256")
                or (artifact.get("metadata") or {}).get("sha256")
                or ""
            ).strip().lower()
            if declared and declared != checksum:
                raise conflict(
                    "scenario_artifact_checksum_mismatch",
                    "Artifact evidence checksum does not match owner bytes.",
                    phase="evidence",
                    detail={
                        "artifact_id": artifact_id,
                        "declared": declared,
                        "observed": checksum,
                    },
                )
            output.append(
                {
                    "artifact_id": artifact_id,
                    "kind": str(artifact.get("kind") or "file"),
                    "uri": str(path),
                    "sha256": checksum,
                    "size": size,
                    "producer_node_id": str(
                        artifact.get("producer_node_id") or ""
                    ),
                    "revision": str(
                        artifact.get("revision")
                        or (artifact.get("metadata") or {}).get("revision")
                        or ""
                    ),
                    "owner_metadata_digest": digest(
                        artifact.get("metadata") or {}
                    ),
                }
            )
        return output

    def _event_receipts(
        self,
        events: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        previous_digest = ""
        for sequence, event in enumerate(events, start=1):
            event_id = str(event.get("event_id") or "").strip()
            event_type = str(event.get("event_type") or "").strip()
            run_id = str(event.get("run_id") or "").strip()
            task_id = str(event.get("task_id") or "").strip()
            if not all((event_id, event_type, run_id, task_id)):
                raise conflict(
                    "scenario_canonical_event_identity_missing",
                    "Canonical event evidence is missing identity fields.",
                    phase="evidence",
                    detail={"sequence": sequence},
                )
            event_digest = digest(event)
            chain_digest = digest(
                {
                    "sequence": sequence,
                    "previous_digest": previous_digest,
                    "event_digest": event_digest,
                }
            )
            output.append(
                {
                    "sequence": sequence,
                    "event_id": event_id,
                    "event_type": event_type,
                    "run_id": run_id,
                    "task_id": task_id,
                    "node_id": str(event.get("node_id") or ""),
                    "created_at": str(event.get("created_at") or ""),
                    "event_digest": event_digest,
                    "previous_digest": previous_digest,
                    "chain_digest": chain_digest,
                }
            )
            previous_digest = chain_digest
        return output

    def _verify_artifact_receipts(
        self,
        receipts: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        seen: set[str] = set()
        for receipt in receipts:
            artifact_id = str(receipt.get("artifact_id") or "")
            if not artifact_id or artifact_id in seen:
                failures.append(
                    {
                        "code": "artifact_identity_invalid",
                        "artifact_id": artifact_id,
                    }
                )
                continue
            seen.add(artifact_id)
            path = Path(str(receipt.get("uri") or "")).resolve(strict=False)
            if not path.is_file() or not path_within(path, self._artifact_root):
                failures.append(
                    {
                        "code": "artifact_bytes_unavailable",
                        "artifact_id": artifact_id,
                    }
                )
                continue
            checksum, size = file_digest(path)
            if checksum != receipt.get("sha256") or size != receipt.get("size"):
                failures.append(
                    {
                        "code": "artifact_receipt_stale",
                        "artifact_id": artifact_id,
                    }
                )
        return failures

    def _verify_event_receipts(
        self,
        receipts: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        previous = ""
        seen: set[str] = set()
        for sequence, receipt in enumerate(receipts, start=1):
            event_id = str(receipt.get("event_id") or "")
            if not event_id or event_id in seen:
                failures.append(
                    {"code": "event_identity_invalid", "event_id": event_id}
                )
            seen.add(event_id)
            if int(receipt.get("sequence") or 0) != sequence:
                failures.append(
                    {"code": "event_sequence_invalid", "event_id": event_id}
                )
            if str(receipt.get("previous_digest") or "") != previous:
                failures.append(
                    {"code": "event_chain_broken", "event_id": event_id}
                )
            expected = digest(
                {
                    "sequence": sequence,
                    "previous_digest": previous,
                    "event_digest": str(receipt.get("event_digest") or ""),
                }
            )
            if expected != receipt.get("chain_digest"):
                failures.append(
                    {"code": "event_chain_digest_mismatch", "event_id": event_id}
                )
            previous = str(receipt.get("chain_digest") or "")
        return failures

    def _verify_owner_bindings(
        self,
        manifest: Mapping[str, Any],
        *, mode: ScenarioMode = ScenarioMode.SEALED,
    ) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        scenario_run_id = str(manifest.get("scenario_run_id") or "")
        input_digest = str(manifest.get("input_digest") or "")
        policy_digest = str(manifest.get("policy_digest") or "")
        owner = manifest.get("owner")
        owner = owner if isinstance(owner, Mapping) else {}
        owner_run_id = str(owner.get("run_id") or "")
        owner_task_id = str(owner.get("task_id") or "")
        preflight = manifest.get("preflight_receipt")
        preflight = preflight if isinstance(preflight, Mapping) else {}
        policy = manifest.get("policy_receipt")
        policy = policy if isinstance(policy, Mapping) else {}

        required = {
            "scenario_run_id": scenario_run_id,
            "input_digest": input_digest,
            "policy_digest": policy_digest,
            "owner_run_id": owner_run_id,
            "owner_task_id": owner_task_id,
        }
        for field, value in required.items():
            if not value:
                failures.append(
                    {"code": "evidence_binding_missing", "field": field}
                )
        if mode is ScenarioMode.SEALED and preflight.get("clean") is not True:
            failures.append({"code": "preflight_clean_binding_invalid"})
        if mode is ScenarioMode.SEALED and preflight.get("new_input") is not True:
            failures.append({"code": "preflight_input_binding_invalid"})
        if str(preflight.get("scenario_run_id") or "") != scenario_run_id:
            failures.append({"code": "preflight_run_binding_mismatch"})
        if str(preflight.get("input_digest") or "") != input_digest:
            failures.append({"code": "preflight_digest_binding_mismatch"})
        if policy.get("valid") is not True:
            failures.append({"code": "policy_receipt_invalid"})
        if str(policy.get("policy_digest") or "") != policy_digest:
            failures.append({"code": "policy_digest_binding_mismatch"})
        if int(policy.get("human_intervention_count") or 0) != 0:
            failures.append({"code": "policy_human_count_nonzero"})
        if int(policy.get("operator_intervention_attempt_count") or 0) != 0:
            failures.append({"code": "policy_operator_attempt_present"})

        events = manifest.get("canonical_events")
        for event in events if isinstance(events, list) else ():
            if not isinstance(event, Mapping):
                failures.append({"code": "canonical_event_receipt_invalid"})
                continue
            if (
                str(event.get("run_id") or "") != owner_run_id
                or str(event.get("task_id") or "") != owner_task_id
            ):
                failures.append(
                    {
                        "code": "canonical_event_owner_binding_mismatch",
                        "event_id": str(event.get("event_id") or ""),
                    }
                )
        steps = manifest.get("effective_steps")
        steps = steps if isinstance(steps, Mapping) else {}
        for step in steps.get("steps") or ():
            if not isinstance(step, Mapping):
                failures.append({"code": "effective_step_receipt_invalid"})
                continue
            if (
                str(step.get("run_id") or "") != owner_run_id
                or str(step.get("task_id") or "") != owner_task_id
            ):
                failures.append(
                    {
                        "code": "effective_step_owner_binding_mismatch",
                        "step_id": str(step.get("step_id") or ""),
                    }
                )
        return failures

    def _verify_metric_receipts(
        self,
        manifest: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        metrics = manifest.get("metrics")
        metrics = metrics if isinstance(metrics, Mapping) else {}
        samples = metrics.get("raw_samples")
        samples = samples if isinstance(samples, list) else []
        failures: list[dict[str, Any]] = []
        if digest(samples) != str(metrics.get("raw_sample_digest") or ""):
            failures.append({"code": "metric_raw_sample_digest_mismatch"})
        dimensions = metrics.get("dimension_receipt")
        dimensions = dimensions if isinstance(dimensions, Mapping) else {}
        if dimensions.get("valid") is not True:
            failures.append({"code": "metric_dimension_receipt_invalid"})
        if int(dimensions.get("sample_count") or 0) != len(samples):
            failures.append({"code": "metric_dimension_sample_count_mismatch"})

        scenario_run_id = str(manifest.get("scenario_run_id") or "")
        owner = manifest.get("owner")
        owner = owner if isinstance(owner, Mapping) else {}
        expected = {
            "scenario_run_id": scenario_run_id,
            "owner_run_id": str(owner.get("run_id") or ""),
            "task_id": str(owner.get("task_id") or ""),
        }
        for sample in samples:
            if not isinstance(sample, Mapping):
                failures.append({"code": "metric_sample_invalid"})
                continue
            observed = sample.get("dimensions")
            observed = observed if isinstance(observed, Mapping) else {}
            mismatches = sorted(
                key
                for key, value in expected.items()
                if str(observed.get(key) or "") != value
            )
            if mismatches:
                failures.append(
                    {
                        "code": "metric_owner_binding_mismatch",
                        "sample_id": str(sample.get("sample_id") or ""),
                        "dimensions": mismatches,
                    }
                )
        return failures

    def _require_enabled(self) -> None:
        if self._disabled:
            raise unavailable(
                "scenario_evidence_collector_disabled",
                "Formal scenario evidence collector is disabled.",
                phase="evidence",
            )


def compare_manifests(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> dict[str, Any]:
    left_steps = left.get("effective_steps")
    right_steps = right.get("effective_steps")
    left_steps = left_steps if isinstance(left_steps, Mapping) else {}
    right_steps = right_steps if isinstance(right_steps, Mapping) else {}
    left_metrics = left.get("metrics")
    right_metrics = right.get("metrics")
    left_metrics = left_metrics if isinstance(left_metrics, Mapping) else {}
    right_metrics = right_metrics if isinstance(right_metrics, Mapping) else {}
    comparison = {
        "schema": "zyra.scenario-evidence-comparison/v1",
        "left_manifest_id": str(left.get("manifest_id") or ""),
        "right_manifest_id": str(right.get("manifest_id") or ""),
        "same_definition": left.get("definition_digest") == right.get("definition_digest"),
        "same_input": left.get("input_digest") == right.get("input_digest"),
        "same_policy": left.get("policy_digest") == right.get("policy_digest"),
        "effective_step_delta": len(right_steps.get("admitted_step_ids") or [])
        - len(left_steps.get("admitted_step_ids") or []),
        "raw_sample_digest_equal": (
            left_metrics.get("raw_sample_digest")
            == right_metrics.get("raw_sample_digest")
        ),
        "artifact_digest_equal": digest(left.get("artifacts") or [])
        == digest(right.get("artifacts") or []),
    }
    comparison["comparison_digest"] = digest(comparison)
    return comparison


def _disabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}
