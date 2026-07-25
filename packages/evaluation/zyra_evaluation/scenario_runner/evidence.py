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
from .models import MetricSample, OwnerExecutionResult, ScenarioConfiguration


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
        manifest = {
            "schema": "zyra.scenario-evidence-manifest/v1",
            "manifest_id": new_identity("manifest"),
            "scenario_run_id": scenario_run_id,
            "scenario_id": configuration.scenario_id,
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
            "claims": {
                "formal_foundation": True,
                "human_intervention_count": 0,
                "legacy_demo_fallback": False,
                "replay_evidence": False,
                "long_live_scenario_complete": False,
                "two_thousand_step_gate_complete": False,
                "edge_cloud_dispatch_complete": False,
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
        verification = self.verify(manifest)
        manifest["verification_receipt"] = verification
        return manifest

    def verify(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
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
        if int(claims.get("human_intervention_count") or 0) != 0:
            failures.append({"code": "human_intervention_count_nonzero"})
        if claims.get("legacy_demo_fallback") is not False:
            failures.append({"code": "legacy_demo_fallback_present"})
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
