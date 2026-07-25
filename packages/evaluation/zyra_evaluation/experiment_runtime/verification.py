from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .canonical import (
    content_digest,
    digest,
    require_digest,
    stable_unique,
    utc_now,
)
from .errors import invalid
from .matrix import REQUIRED_VARIANTS
from .models import (
    CellPhase,
    ExperimentRun,
    RawMetricSample,
    VariantDefinition,
)
from .source import SourceArchive
from .workload import WorkloadObservation


class ExternalEvidenceVerifier:
    def __init__(
        self,
        *,
        project_root: str | Path,
        maximum_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.maximum_bytes = maximum_bytes

    def verify(
        self,
        entries: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        selected_entries = dict(entries or {})
        output: dict[str, Any] = {}
        findings: list[dict[str, Any]] = []
        for requirement_id, raw in sorted(selected_entries.items()):
            if not isinstance(raw, Mapping):
                findings.append(
                    {
                        "code": "external_evidence_shape_invalid",
                        "requirement_id": requirement_id,
                    }
                )
                continue
            path_value = str(raw.get("path") or "").strip()
            path = Path(path_value)
            if not path.is_absolute():
                path = self.project_root / path
            path = path.resolve(strict=False)
            try:
                relative = path.relative_to(self.project_root)
            except ValueError:
                findings.append(
                    {
                        "code": "external_evidence_outside_project",
                        "requirement_id": requirement_id,
                        "path": str(path),
                    }
                )
                continue
            if not path.is_file():
                findings.append(
                    {
                        "code": "external_evidence_missing",
                        "requirement_id": requirement_id,
                        "path": str(relative),
                    }
                )
                continue
            size = path.stat().st_size
            if size <= 0 or size > self.maximum_bytes:
                findings.append(
                    {
                        "code": "external_evidence_size_invalid",
                        "requirement_id": requirement_id,
                        "path": str(relative),
                        "size": size,
                    }
                )
                continue
            payload = path.read_bytes()
            actual_digest = content_digest(payload)
            expected_digest = str(raw.get("sha256") or "").strip()
            if expected_digest:
                try:
                    expected_digest = require_digest(
                        expected_digest,
                        "external evidence digest",
                    )
                except Exception as error:
                    findings.append(
                        {
                            "code": "external_evidence_digest_invalid",
                            "requirement_id": requirement_id,
                            "reason": str(error),
                        }
                    )
                    continue
                if expected_digest != actual_digest:
                    findings.append(
                        {
                            "code": "external_evidence_digest_mismatch",
                            "requirement_id": requirement_id,
                            "expected": expected_digest,
                            "observed": actual_digest,
                        }
                    )
                    continue
            try:
                document = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                findings.append(
                    {
                        "code": "external_evidence_json_invalid",
                        "requirement_id": requirement_id,
                        "path": str(relative),
                        "reason": str(error),
                    }
                )
                continue
            semantic = self._semantic_checks(requirement_id, document)
            if semantic:
                findings.extend(
                    {
                        **item,
                        "requirement_id": requirement_id,
                        "path": str(relative).replace("\\", "/"),
                    }
                    for item in semantic
                )
                continue
            output[requirement_id] = {
                "verified": True,
                "path": str(relative).replace("\\", "/"),
                "sha256": actual_digest,
                "size": size,
                "claim": str(raw.get("claim") or "").strip(),
                "commit": str(raw.get("commit") or "").strip(),
                "schema": (
                    str(document.get("schema") or "")
                    if isinstance(document, Mapping)
                    else ""
                ),
                "verification_digest": digest(
                    {
                        "requirement_id": requirement_id,
                        "path": str(relative).replace("\\", "/"),
                        "sha256": actual_digest,
                        "claim": str(raw.get("claim") or "").strip(),
                        "commit": str(raw.get("commit") or "").strip(),
                    }
                ),
            }
        receipt = {
            "schema": "zyra.experiment-external-evidence-verification/v1",
            "valid": not findings,
            "entries": output,
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        if findings:
            raise invalid(
                "experiment_external_evidence_invalid",
                "Frozen external evidence failed verification.",
                phase="source_admission",
                detail=receipt,
            )
        return receipt

    def _semantic_checks(
        self,
        requirement_id: str,
        document: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(document, Mapping):
            return [{"code": "external_evidence_document_shape_invalid"}]
        text = json.dumps(document, ensure_ascii=False, sort_keys=True)
        findings: list[dict[str, Any]] = []
        if requirement_id in {"REQ-EDGE-01", "SCORE-COMPAT"}:
            required_tokens = (
                "local",
                "edge",
                "cloud",
                "provider",
                "model",
            )
            missing = [token for token in required_tokens if token not in text.casefold()]
            if missing:
                findings.append(
                    {
                        "code": "dispatch_evidence_semantics_missing",
                        "tokens": missing,
                    }
                )
        if requirement_id == "SCORE-TASKS":
            required_tokens = (
                "software",
                "research",
                "scenario_",
                "effective_step_count",
            )
            missing = [token for token in required_tokens if token not in text.casefold()]
            if missing:
                findings.append(
                    {
                        "code": "dual_task_evidence_semantics_missing",
                        "tokens": missing,
                    }
                )
        return findings


class WorkloadObservationVerifier:
    def verify(
        self,
        *,
        observation: WorkloadObservation,
        variant: VariantDefinition,
        source: SourceArchive,
        expected_envelope_digest: str,
        expected_repetition: int,
        expected_seed: int,
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        if observation.variant_id != variant.variant_id:
            findings.append({"code": "variant_binding_mismatch"})
        if observation.repetition != expected_repetition:
            findings.append({"code": "repetition_binding_mismatch"})
        if observation.seed != expected_seed:
            findings.append({"code": "seed_binding_mismatch"})
        if observation.envelope_digest != expected_envelope_digest:
            findings.append({"code": "envelope_binding_mismatch"})
        if observation.source_archive_digest != source.archive_digest:
            findings.append({"code": "source_archive_binding_mismatch"})
        if observation.scenario_run_id != source.scenario_run_id:
            findings.append({"code": "scenario_run_binding_mismatch"})
        if observation.owner_run_id != source.owner_run_id:
            findings.append({"code": "owner_run_binding_mismatch"})
        if observation.task_id != source.task_id:
            findings.append({"code": "task_binding_mismatch"})
        if len(observation.processed_event_ids) != len(source.events):
            findings.append(
                {
                    "code": "event_coverage_invalid",
                    "expected": len(source.events),
                    "observed": len(observation.processed_event_ids),
                }
            )
        if tuple(observation.processed_event_ids) != tuple(
            item.event_id for item in source.events
        ):
            findings.append({"code": "event_order_or_identity_mismatch"})
        if len(observation.routes) != len(source.events):
            findings.append({"code": "route_coverage_invalid"})
        if observation.human_intervention_count != 0:
            findings.append({"code": "human_intervention_nonzero"})
        if observation.claims.get("source_live") is not True:
            findings.append({"code": "source_live_claim_missing"})
        if observation.claims.get("source_replay") is not False:
            findings.append({"code": "source_replay_claim_invalid"})
        if observation.claims.get("new_external_model_request") is not False:
            findings.append({"code": "external_model_request_claim_invalid"})
        if (
            observation.claims.get("authenticated_provider_cli_invoked")
            is not False
        ):
            findings.append({"code": "authenticated_cli_claim_invalid"})
        if variant.variant_id == "single_agent":
            workers = {item.worker_id for item in observation.routes}
            if workers != {"single-agent"}:
                findings.append(
                    {
                        "code": "single_agent_route_invalid",
                        "workers": sorted(workers),
                    }
                )
        if variant.variant_id == "static_full_connect_multi_agent":
            nodes = len(observation.topology_nodes)
            expected_edges = nodes * max(0, nodes - 1)
            if nodes > 1 and len(observation.topology_edges) != expected_edges:
                findings.append(
                    {
                        "code": "static_full_connect_topology_invalid",
                        "nodes": nodes,
                        "expected_edges": expected_edges,
                        "observed_edges": len(observation.topology_edges),
                    }
                )
        if variant.variant_id == "dynamic_heterogeneous_swarm":
            if not observation.topology_nodes or not observation.topology_edges:
                findings.append({"code": "dynamic_topology_missing"})
            if observation.topology_mutations < 1:
                findings.append({"code": "dynamic_topology_churn_missing"})
        if variant.expected_disabled_capability == "memory_compact":
            if (
                observation.memory_writes != 0
                or observation.memory_hits != 0
                or observation.compact_operations != 0
            ):
                findings.append({"code": "memory_ablation_ineffective"})
        if variant.expected_disabled_capability == "recovery":
            if any(item.recovered for item in observation.recoveries):
                findings.append({"code": "recovery_ablation_ineffective"})
            if source.fault_events and not observation.unresolved_fault_ids:
                findings.append({"code": "recovery_ablation_has_no_fault_effect"})
        if variant.expected_disabled_capability == "low_entropy_communication":
            if observation.messages and not any(
                len(item.recipients) > 1 for item in observation.messages
            ):
                findings.append({"code": "communication_ablation_ineffective"})
        if variant.expected_disabled_capability == "scheduler":
            reasons = {item.reason for item in observation.routes}
            if not any("scheduler disabled" in item for item in reasons):
                findings.append({"code": "scheduler_ablation_ineffective"})
        receipt_digests: set[str] = set()
        for receipt in observation.execution_receipts:
            declared = str(receipt.get("receipt_digest") or "")
            body = dict(receipt)
            body.pop("receipt_digest", None)
            body.pop("created_at", None)
            # Workload receipts hash their body before the timestamp is added.
            calculated = digest(body)
            if declared != calculated:
                findings.append(
                    {
                        "code": "execution_receipt_digest_mismatch",
                        "receipt_id": receipt.get("receipt_id"),
                    }
                )
            if declared in receipt_digests:
                findings.append(
                    {
                        "code": "execution_receipt_duplicate",
                        "receipt_id": receipt.get("receipt_id"),
                    }
                )
            receipt_digests.add(declared)
            if receipt.get("valid") is not True:
                if not (
                    variant.expected_disabled_capability == "recovery"
                    and receipt.get("schema")
                    == "zyra.experiment-recovery-receipt/v1"
                ):
                    findings.append(
                        {
                            "code": "execution_receipt_invalid",
                            "receipt_id": receipt.get("receipt_id"),
                        }
                    )
        receipt = {
            "schema": "zyra.experiment-workload-verification/v1",
            "valid": not findings,
            "observation_id": observation.observation_id,
            "observation_digest": observation.observation_digest,
            "variant_id": variant.variant_id,
            "repetition": expected_repetition,
            "seed": expected_seed,
            "event_count": len(observation.processed_event_ids),
            "route_count": len(observation.routes),
            "message_count": len(observation.messages),
            "recovery_count": len(observation.recoveries),
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        if findings:
            raise invalid(
                "experiment_workload_observation_invalid",
                "Controlled workload observation failed verification.",
                phase="execution",
                detail=receipt,
            )
        return receipt


class MatrixEffectVerifier:
    def verify(
        self,
        *,
        run: ExperimentRun,
        observations: Iterable[WorkloadObservation],
        samples: Iterable[RawMetricSample],
    ) -> dict[str, Any]:
        observations = tuple(observations)
        samples = tuple(samples)
        findings: list[dict[str, Any]] = []
        variants = Counter(item.variant_id for item in observations)
        for variant in REQUIRED_VARIANTS:
            if variants[variant] != run.repetitions:
                findings.append(
                    {
                        "code": "variant_repetition_count_invalid",
                        "variant_id": variant,
                        "expected": run.repetitions,
                        "observed": variants[variant],
                    }
                )
        dynamic = [
            item
            for item in observations
            if item.variant_id == "dynamic_heterogeneous_swarm"
        ]
        no_scheduler = [
            item for item in observations if item.variant_id == "no_scheduler"
        ]
        no_memory = [
            item for item in observations if item.variant_id == "no_memory_compact"
        ]
        no_recovery = [
            item for item in observations if item.variant_id == "no_recovery"
        ]
        no_entropy = [
            item
            for item in observations
            if item.variant_id == "no_low_entropy_communication"
        ]
        if no_scheduler and all(
            all(route.policy_compliant for route in item.routes)
            for item in no_scheduler
        ):
            findings.append({"code": "scheduler_ablation_no_semantic_effect"})
        if no_memory and any(
            item.memory_writes or item.memory_hits or item.compact_operations
            for item in no_memory
        ):
            findings.append({"code": "memory_ablation_no_semantic_effect"})
        if no_recovery and all(not item.unresolved_fault_ids for item in no_recovery):
            findings.append({"code": "recovery_ablation_no_semantic_effect"})
        if no_entropy and dynamic:
            dynamic_deliveries = sum(
                len(message.recipients)
                for item in dynamic
                for message in item.messages
            )
            ablated_deliveries = sum(
                len(message.recipients)
                for item in no_entropy
                for message in item.messages
            )
            if ablated_deliveries <= dynamic_deliveries:
                findings.append(
                    {
                        "code": "communication_ablation_no_semantic_effect",
                        "dynamic_deliveries": dynamic_deliveries,
                        "ablated_deliveries": ablated_deliveries,
                    }
                )
        sample_pairs = {
            (item.variant_id, item.repetition, item.metric) for item in samples
        }
        metric_names = {item.metric for item in samples}
        missing_samples: list[str] = []
        for variant in REQUIRED_VARIANTS:
            for repetition in range(1, run.repetitions + 1):
                for metric in metric_names:
                    if (variant, repetition, metric) not in sample_pairs:
                        missing_samples.append(f"{variant}:{repetition}:{metric}")
        if missing_samples:
            findings.append(
                {
                    "code": "raw_sample_matrix_incomplete",
                    "samples": missing_samples[:100],
                    "count": len(missing_samples),
                }
            )
        receipt = {
            "schema": "zyra.experiment-matrix-effect-verification/v1",
            "valid": not findings,
            "experiment_id": run.experiment_id,
            "variant_counts": dict(sorted(variants.items())),
            "sample_count": len(samples),
            "metric_count": len(metric_names),
            "observation_digest": digest(
                [item.observation_digest for item in observations]
            ),
            "sample_digest": digest([item.to_dict() for item in samples]),
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        if findings:
            raise invalid(
                "experiment_matrix_effect_invalid",
                "Experiment matrix did not produce required semantic effects.",
                phase="verification",
                detail=receipt,
            )
        return receipt


def verify_run_completion(
    run: ExperimentRun,
    *,
    receipt_kinds: Iterable[str],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    required_receipts = {
        "source_admission",
        "variant_catalog",
        "envelope",
        "cell_plan",
        "matrix_effect",
        "aggregation",
        "requirements",
        "report",
        "source_role_audit",
        "bundle",
    }
    actual_receipts = set(receipt_kinds)
    missing_receipts = sorted(required_receipts - actual_receipts)
    if missing_receipts:
        findings.append(
            {
                "code": "completion_receipts_missing",
                "kinds": missing_receipts,
            }
        )
    if any(item.phase is not CellPhase.SUCCEEDED for item in run.cells):
        findings.append({"code": "completion_cells_not_succeeded"})
    if not run.report:
        findings.append({"code": "completion_report_missing"})
    if not run.bundle_manifest:
        findings.append({"code": "completion_bundle_missing"})
    receipt = {
        "schema": "zyra.experiment-completion-verification/v1",
        "valid": not findings,
        "experiment_id": run.experiment_id,
        "phase": run.phase.value,
        "cell_count": len(run.cells),
        "receipt_kinds": sorted(actual_receipts),
        "missing_receipt_kinds": missing_receipts,
        "findings": findings,
        "verified_at": utc_now(),
    }
    receipt["receipt_digest"] = digest(receipt)
    if findings:
        raise invalid(
            "experiment_completion_invalid",
            "Experiment completion verification failed.",
            phase="verification",
            detail=receipt,
        )
    return receipt
