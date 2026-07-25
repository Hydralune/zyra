from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import digest, stable_unique, utc_now
from .errors import invalid
from .models import (
    DistributionSummary,
    EvidenceStatus,
    ExperimentRun,
    RequirementEvidence,
    VariantComparison,
)


@dataclass(frozen=True, slots=True)
class RequirementDefinition:
    requirement_id: str
    score: int
    title: str
    owner: str
    required_metrics: tuple[str, ...]
    required_variant_ids: tuple[str, ...]
    evidence_categories: tuple[str, ...]
    stage_gate: bool
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "score": self.score,
            "title": self.title,
            "owner": self.owner,
            "required_metrics": list(self.required_metrics),
            "required_variant_ids": list(self.required_variant_ids),
            "evidence_categories": list(self.evidence_categories),
            "stage_gate": self.stage_gate,
            "description": self.description,
        }


def requirement_definitions() -> tuple[RequirementDefinition, ...]:
    baselines = (
        "single_agent",
        "static_full_connect_multi_agent",
        "dynamic_heterogeneous_swarm",
    )
    ablations = (
        "no_scheduler",
        "no_memory_compact",
        "no_recovery",
        "no_low_entropy_communication",
    )
    all_variants = baselines + ablations
    return (
        RequirementDefinition(
            requirement_id="REQ-CLOSE-01",
            score=0,
            title="Autonomous long-horizon multi-agent closed loop",
            owner="M1-08/M2-05/M3-02A",
            required_metrics=(
                "success_rate",
                "quality_score",
                "effective_step_count",
                "human_intervention_count",
            ),
            required_variant_ids=all_variants,
            evidence_categories=("source_archive", "matrix", "report", "artifact"),
            stage_gate=True,
            description=(
                "New-input delivery, owner-bound transitions, final artifact and "
                "zero human intervention."
            ),
        ),
        RequirementDefinition(
            requirement_id="REQ-MEM-01",
            score=0,
            title="Distributed memory, compaction and wake/restore",
            owner="M1-02D/06A-C/07C/08",
            required_metrics=(
                "memory_hit_ratio",
                "compact_ratio",
                "restore_success_rate",
            ),
            required_variant_ids=(
                "dynamic_heterogeneous_swarm",
                "no_memory_compact",
            ),
            evidence_categories=("memory", "metric", "comparison"),
            stage_gate=True,
            description="Memory/compact behavior must change downstream outcomes.",
        ),
        RequirementDefinition(
            requirement_id="REQ-TOPO-01",
            score=0,
            title="Dynamic heterogeneous sparse topology",
            owner="M1-03D/05C/07A-C/08/M2-02A",
            required_metrics=(
                "topology_node_count",
                "topology_edge_count",
                "topology_sparsity",
                "topology_churn",
            ),
            required_variant_ids=baselines,
            evidence_categories=("topology", "metric", "comparison"),
            stage_gate=True,
            description="Runtime topology changes and sparse/full-connect contrast.",
        ),
        RequirementDefinition(
            requirement_id="REQ-COMM-01",
            score=0,
            title="Structured low-entropy communication",
            owner="M1-04B/05C/08/M3-02A",
            required_metrics=(
                "communication_entropy",
                "useful_communication_ratio",
                "communication_delivery_count",
            ),
            required_variant_ids=(
                "dynamic_heterogeneous_swarm",
                "static_full_connect_multi_agent",
                "no_low_entropy_communication",
            ),
            evidence_categories=("communication", "metric", "comparison"),
            stage_gate=True,
            description="Targeted versus broadcast communication effectiveness.",
        ),
        RequirementDefinition(
            requirement_id="REQ-EDGE-01",
            score=0,
            title="Device, edge and cloud dynamic placement",
            owner="M1-05B/D/07A-C/08/M2-04B/05/M3-02B",
            required_metrics=(
                "placement_policy_compliance",
                "privacy_policy_compliance",
                "provider_count",
                "model_count",
            ),
            required_variant_ids=(
                "dynamic_heterogeneous_swarm",
                "no_scheduler",
            ),
            evidence_categories=("placement", "prior_verified_dispatch", "metric"),
            stage_gate=True,
            description=(
                "Placement policy plus frozen non-simulated dispatch and model receipts."
            ),
        ),
        RequirementDefinition(
            requirement_id="REQ-FAULT-01",
            score=0,
            title="Fault, requirement change and node-loss recovery",
            owner="M1-07B/C/08/M2-05",
            required_metrics=(
                "fault_recovery_rate",
                "recovery_mttr_units",
                "unresolved_fault_count",
            ),
            required_variant_ids=(
                "dynamic_heterogeneous_swarm",
                "no_recovery",
            ),
            evidence_categories=("fault", "recovery", "comparison"),
            stage_gate=True,
            description="Injected faults and recovery/no-recovery contrast.",
        ),
        RequirementDefinition(
            requirement_id="REQ-TRACE-01",
            score=0,
            title="Causal decisions and reasoning trace",
            owner="M1-05C/08/M2-01B/02A/B/03A/B/05",
            required_metrics=(
                "valid_transition_ratio",
                "artifact_drift_ratio",
            ),
            required_variant_ids=all_variants,
            evidence_categories=(
                "canonical_event",
                "span",
                "mutation",
                "checkpoint",
                "artifact",
            ),
            stage_gate=True,
            description="Every result navigates back to canonical source evidence.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-LOOP",
            score=15,
            title="Perception-plan-execute-feedback loop",
            owner="M1-08/M2-05/M3-02A",
            required_metrics=(
                "effective_step_count",
                "success_rate",
                "human_intervention_count",
            ),
            required_variant_ids=all_variants,
            evidence_categories=("source_archive", "report", "artifact"),
            stage_gate=True,
            description="Thousands of effective transitions without manual progress.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-ORG",
            score=15,
            title="Hierarchy, specialization and dynamic teaming",
            owner="M1-03D/05C/07A-C/M2-02A",
            required_metrics=(
                "route_worker_count",
                "topology_sparsity",
                "topology_churn",
            ),
            required_variant_ids=baselines,
            evidence_categories=("topology", "route", "comparison"),
            stage_gate=True,
            description="Role-aware routing and dynamic worker/topology changes.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-TASKS",
            score=10,
            title="Two or more cross-domain long tasks",
            owner="M2-05/M3-02A/03",
            required_metrics=("success_rate", "quality_score"),
            required_variant_ids=("dynamic_heterogeneous_swarm",),
            evidence_categories=("portfolio", "source_archive", "artifact"),
            stage_gate=True,
            description="Software delivery and cross-source research live evidence.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-SCENE",
            score=10,
            title="Business-relevant scenario coverage",
            owner="M2-05/M3-03",
            required_metrics=("quality_score", "artifact_drift_ratio"),
            required_variant_ids=("dynamic_heterogeneous_swarm",),
            evidence_categories=("scenario", "report", "artifact"),
            stage_gate=True,
            description="Real input/output workflows and reusable boundaries.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-VALUE",
            score=10,
            title="Social and economic value",
            owner="M2-05/M3-03",
            required_metrics=("wall_time_ms", "cost_microunits"),
            required_variant_ids=baselines,
            evidence_categories=("metric", "assumption", "report"),
            stage_gate=True,
            description="Time, cycle and failure-cost metrics with stated assumptions.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-UX",
            score=5,
            title="Observable and operable workbench",
            owner="M2/M3-03",
            required_metrics=("success_rate",),
            required_variant_ids=all_variants,
            evidence_categories=("workbench", "navigation", "bundle"),
            stage_gate=True,
            description="Reviewer can navigate evidence without backend logs.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-NOISE",
            score=10,
            title="Architecture and interaction denoising",
            owner="M1-04B/05C/M3-02A",
            required_metrics=(
                "topology_sparsity",
                "communication_entropy",
                "communication_delivery_count",
                "success_rate",
            ),
            required_variant_ids=(
                "static_full_connect_multi_agent",
                "dynamic_heterogeneous_swarm",
                "no_low_entropy_communication",
            ),
            evidence_categories=("comparison", "metric", "topology"),
            stage_gate=True,
            description="Dynamic sparse and full-connect/no-low-entropy contrast.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-ALGO",
            score=10,
            title="Underlying algorithmic innovation",
            owner="M1-06/07/08/M3-02A/03",
            required_metrics=(
                "memory_hit_ratio",
                "compact_ratio",
                "fault_recovery_rate",
                "topology_sparsity",
            ),
            required_variant_ids=all_variants,
            evidence_categories=(
                "ablation",
                "pseudocode",
                "complexity",
                "source_role",
            ),
            stage_gate=True,
            description="Ablation plus algorithm pseudocode and complexity entry.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-ROBUST",
            score=5,
            title="Success, correction and robustness",
            owner="M1-07B/C/08/M3-02A",
            required_metrics=(
                "success_rate",
                "fault_recovery_rate",
                "recovery_mttr_units",
            ),
            required_variant_ids=all_variants,
            evidence_categories=("p50_p95", "fault", "comparison"),
            stage_gate=True,
            description="Repeated runs, P50/P95, MTTR and recovery matrix.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-EFF",
            score=5,
            title="Token, time and compute efficiency",
            owner="M1-04B/05C/07A/M3-02A",
            required_metrics=(
                "token_units",
                "wall_time_ms",
                "cost_microunits",
                "communication_delivery_count",
            ),
            required_variant_ids=all_variants,
            evidence_categories=("metric", "p50_p95", "comparison"),
            stage_gate=True,
            description="Efficiency distributions and baseline contrasts.",
        ),
        RequirementDefinition(
            requirement_id="SCORE-COMPAT",
            score=5,
            title="Multi-model compatibility and dynamic roles",
            owner="M1-03D/05D/07A/M3-02A/B",
            required_metrics=(
                "provider_count",
                "model_count",
                "route_worker_count",
            ),
            required_variant_ids=("dynamic_heterogeneous_swarm",),
            evidence_categories=(
                "prior_verified_dispatch",
                "provider_receipt",
                "topology",
            ),
            stage_gate=True,
            description="Frozen real provider/model receipts and dynamic role evidence.",
        ),
    )


class RequirementEvidenceMapper:
    def __init__(
        self,
        definitions: Iterable[RequirementDefinition] | None = None,
    ) -> None:
        selected = tuple(definitions or requirement_definitions())
        self.definitions = {item.requirement_id: item for item in selected}
        if len(self.definitions) != len(selected):
            raise invalid(
                "experiment_requirement_duplicate",
                "Requirement evidence policy contains duplicate identifiers.",
            )

    def map(
        self,
        *,
        run: ExperimentRun,
        summaries: Iterable[DistributionSummary],
        comparisons: Iterable[VariantComparison],
        evidence_paths: Mapping[str, Iterable[str]],
        external_evidence: Mapping[str, Any] | None = None,
    ) -> tuple[RequirementEvidence, ...]:
        summaries = tuple(summaries)
        comparisons = tuple(comparisons)
        external = dict(external_evidence or {})
        metric_variants = {
            (item.metric, item.variant_id)
            for item in summaries
            if item.observed_sample_count >= 1
        }
        comparison_metrics = {
            item.metric for item in comparisons if item.comparable
        }
        output: list[RequirementEvidence] = []
        source_run_ids = stable_unique(
            cell.scenario_run_id for cell in run.cells if cell.scenario_run_id
        )
        for definition in self.definitions.values():
            missing_metrics = sorted(
                metric
                for metric in definition.required_metrics
                if not all(
                    (metric, variant) in metric_variants
                    for variant in definition.required_variant_ids
                )
            )
            missing_variants = sorted(
                variant
                for variant in definition.required_variant_ids
                if variant not in {item.variant_id for item in run.variants}
            )
            paths = stable_unique(
                str(path)
                for category in definition.evidence_categories
                for path in evidence_paths.get(category, ())
            )
            external_entry = external.get(definition.requirement_id)
            external_valid = (
                isinstance(external_entry, Mapping)
                and external_entry.get("verified") is True
            )
            needs_external = definition.requirement_id in {
                "REQ-EDGE-01",
                "SCORE-TASKS",
                "SCORE-COMPAT",
            }
            complete = (
                not missing_metrics
                and not missing_variants
                and bool(paths)
                and (not needs_external or external_valid)
            )
            reason_parts: list[str] = []
            if missing_metrics:
                reason_parts.append(f"missing metrics: {', '.join(missing_metrics)}")
            if missing_variants:
                reason_parts.append(f"missing variants: {', '.join(missing_variants)}")
            if not paths:
                reason_parts.append("no evidence path")
            if needs_external and not external_valid:
                reason_parts.append("verified external milestone receipt missing")
            status = EvidenceStatus.PRESENT if complete else EvidenceStatus.MISSING
            claim = (
                f"{definition.title} has metric and artifact evidence."
                if complete
                else f"{definition.title} is not yet complete."
            )
            source_digest = digest(
                {
                    "definition": definition.to_dict(),
                    "metric_variants": sorted(
                        [list(item) for item in metric_variants]
                    ),
                    "comparison_metrics": sorted(comparison_metrics),
                    "paths": list(paths),
                    "external": external_entry,
                    "run": run.experiment_id,
                }
            )
            output.append(
                RequirementEvidence(
                    requirement_id=definition.requirement_id,
                    score=definition.score,
                    status=status,
                    owner=definition.owner,
                    evidence_paths=paths,
                    metric_names=definition.required_metrics,
                    run_ids=source_run_ids,
                    claim=claim,
                    reason="; ".join(reason_parts) or "all required evidence present",
                    verified=complete,
                    source_digest=source_digest,
                )
            )
        return tuple(output)

    def verify(
        self,
        rows: Iterable[RequirementEvidence],
        *,
        require_all: bool = True,
    ) -> dict[str, Any]:
        selected = tuple(rows)
        actual = {item.requirement_id for item in selected}
        missing = sorted(set(self.definitions) - actual)
        duplicate = sorted(
            item
            for item in actual
            if sum(row.requirement_id == item for row in selected) > 1
        )
        incomplete = sorted(
            item.requirement_id for item in selected if not item.verified
        )
        score_total = sum(item.score for item in selected)
        score_verified = sum(item.score for item in selected if item.verified)
        findings: list[dict[str, Any]] = []
        if missing:
            findings.append({"code": "requirements_missing", "ids": missing})
        if duplicate:
            findings.append({"code": "requirements_duplicate", "ids": duplicate})
        if score_total != 100:
            findings.append(
                {
                    "code": "score_total_invalid",
                    "expected": 100,
                    "observed": score_total,
                }
            )
        if require_all and incomplete:
            findings.append(
                {
                    "code": "requirements_incomplete",
                    "ids": incomplete,
                }
            )
        receipt = {
            "schema": "zyra.experiment-requirement-evidence-verification/v1",
            "valid": not findings,
            "require_all": require_all,
            "requirement_count": len(selected),
            "score_total": score_total,
            "score_verified": score_verified,
            "incomplete_requirement_ids": incomplete,
            "rows_digest": digest([item.to_dict() for item in selected]),
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        if findings:
            raise invalid(
                "experiment_requirement_evidence_invalid",
                "Requirement evidence matrix failed verification.",
                phase="exit",
                detail=receipt,
            )
        return receipt
