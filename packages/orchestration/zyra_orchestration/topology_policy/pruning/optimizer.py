from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..contracts import (
    ContractHeader,
    FrozenDict,
    PolicyInputSnapshot,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
)
from .contracts import (
    AGENTPRUNE_MASK_SCHEMA,
    AgentPruneContractError,
    CommunicationBudget,
    CommunicationEdgeCandidate,
    EdgeContributionStats,
    ProtectedEdgeConstraint,
    PruningDecision,
    PruningMask,
    PruningScoreComponents,
    candidate_digest,
    over_budget,
    stats_digest,
    totals,
)


AGENTPRUNE_CONFIG_SCHEMA = "zyra.agentprune-pruning-config/v1"


class AgentPruneOptimizerError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentPruneOptimizerConfig:
    mechanism_id: str
    mechanism_version: str
    mask_schema_version: str
    outcome_schema_version: str
    stats_schema_version: str
    fallback_profile: str
    input_precheck_report_digest: str
    proposal_ttl_seconds: int
    maximum_outcome_age_seconds: int
    score_weights: FrozenDict
    low_value_threshold: float
    malicious_isolation_ratio: float
    failure_isolation_ratio: float
    maximum_verifier_risk: float
    no_policy_training: FrozenDict
    digest: str
    path: str = ""

    @classmethod
    def load(cls, path: Path) -> AgentPruneOptimizerConfig:
        selected = path.resolve()
        try:
            value = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentPruneOptimizerError(
                "agentprune_config_invalid",
                f"AgentPrune configuration is missing or corrupt: {selected}",
            ) from exc
        if (
            not isinstance(value, Mapping)
            or value.get("schema") != AGENTPRUNE_CONFIG_SCHEMA
        ):
            raise AgentPruneOptimizerError(
                "agentprune_config_schema_invalid",
                "unsupported AgentPrune deterministic configuration",
            )
        if value.get("mechanism_id") != "agentprune":
            raise AgentPruneOptimizerError(
                "agentprune_config_mechanism_invalid",
                "AgentPrune configuration has the wrong mechanism identifier",
            )
        no_training = value.get("no_policy_training")
        if not isinstance(no_training, Mapping) or (
            no_training.get("training_allowed") is not False
            or no_training.get("sampling_allowed") is not False
            or no_training.get("datasets") not in ([], ())
            or no_training.get("checkpoints") not in ([], ())
            or no_training.get("mutable_learned_parameters") not in ([], ())
            or no_training.get("policy_gradient") is not False
        ):
            raise AgentPruneOptimizerError(
                "agentprune_config_training_forbidden",
                "AgentPrune cannot enable policy gradient, sampling, datasets, checkpoints, or learned parameters",
            )
        weights = value.get("score_weights")
        required_weights = {
            "delivery",
            "evidence_utilization",
            "artifact_contribution",
            "verifier",
            "redundancy",
            "failure",
            "retry",
            "malicious",
            "resource",
        }
        if (
            not isinstance(weights, Mapping)
            or set(weights) != required_weights
            or any(float(item) < 0 for item in weights.values())
            or sum(float(item) for item in weights.values()) <= 0
        ):
            raise AgentPruneOptimizerError(
                "agentprune_config_weights_invalid",
                "AgentPrune requires the fixed non-negative score weights",
            )
        report_digest = str(value.get("input_precheck_report_digest") or "")
        if len(report_digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in report_digest
        ):
            raise AgentPruneOptimizerError(
                "agentprune_config_readiness_digest_invalid",
                "input-precheck report digest must be SHA-256",
            )
        thresholds = {
            name: float(value.get(name) or 0)
            for name in (
                "low_value_threshold",
                "malicious_isolation_ratio",
                "failure_isolation_ratio",
                "maximum_verifier_risk",
            )
        }
        if not -1 <= thresholds["low_value_threshold"] <= 1:
            raise AgentPruneOptimizerError(
                "agentprune_low_value_threshold_invalid",
                "low_value_threshold must be in [-1, 1]",
            )
        if any(
            not 0 <= thresholds[name] <= 1
            for name in (
                "malicious_isolation_ratio",
                "failure_isolation_ratio",
                "maximum_verifier_risk",
            )
        ):
            raise AgentPruneOptimizerError(
                "agentprune_ratio_threshold_invalid",
                "AgentPrune ratio thresholds must be in [0, 1]",
            )
        mechanism_version = str(value.get("mechanism_version") or "").strip()
        mask_schema = str(value.get("mask_schema_version") or "").strip()
        outcome_schema = str(value.get("outcome_schema_version") or "").strip()
        stats_schema = str(value.get("stats_schema_version") or "").strip()
        fallback_profile = str(value.get("fallback_profile") or "").strip()
        if not all(
            (
                mechanism_version,
                mask_schema,
                outcome_schema,
                stats_schema,
                fallback_profile,
            )
        ):
            raise AgentPruneOptimizerError(
                "agentprune_config_contract_missing",
                "AgentPrune version, schemas, and fallback profile are required",
            )
        return cls(
            mechanism_id="agentprune",
            mechanism_version=mechanism_version,
            mask_schema_version=mask_schema,
            outcome_schema_version=outcome_schema,
            stats_schema_version=stats_schema,
            fallback_profile=fallback_profile,
            input_precheck_report_digest=report_digest,
            proposal_ttl_seconds=max(
                1,
                int(value.get("proposal_ttl_seconds") or 1),
            ),
            maximum_outcome_age_seconds=max(
                1,
                int(value.get("maximum_outcome_age_seconds") or 1),
            ),
            score_weights=FrozenDict(
                {key: float(item) for key, item in weights.items()}
            ),
            low_value_threshold=thresholds["low_value_threshold"],
            malicious_isolation_ratio=thresholds[
                "malicious_isolation_ratio"
            ],
            failure_isolation_ratio=thresholds["failure_isolation_ratio"],
            maximum_verifier_risk=thresholds["maximum_verifier_risk"],
            no_policy_training=FrozenDict(no_training),
            digest=canonical_digest(value),
            path=selected.as_posix(),
        )


class DeterministicCommunicationOptimizer:
    """One-shot deterministic edge mask; it never mutates canonical graph state."""

    def __init__(self, config: AgentPruneOptimizerConfig) -> None:
        self.config = config
        if config.mask_schema_version != AGENTPRUNE_MASK_SCHEMA:
            raise AgentPruneOptimizerError(
                "agentprune_mask_schema_drift",
                "configured mask schema differs from the implementation",
            )
        if config.outcome_schema_version != "zyra.agentprune-communication-outcome/v1":
            raise AgentPruneOptimizerError(
                "agentprune_outcome_schema_drift",
                "configured outcome schema differs from the implementation",
            )
        if config.stats_schema_version != "zyra.agentprune-edge-contribution/v1":
            raise AgentPruneOptimizerError(
                "agentprune_stats_schema_drift",
                "configured stats schema differs from the implementation",
            )

    def optimize(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        candidates: Iterable[CommunicationEdgeCandidate],
        stats: Iterable[EdgeContributionStats],
        protections: Iterable[ProtectedEdgeConstraint],
        budget: CommunicationBudget,
        mechanism_epoch: str,
        prior_mask: PruningMask | None = None,
    ) -> PruningMask:
        candidate_values = tuple(
            sorted(candidates, key=lambda item: item.edge_id)
        )
        stat_values = tuple(sorted(stats, key=lambda item: item.edge.edge_id))
        protection_values = tuple(
            sorted(protections, key=lambda item: item.edge_id)
        )
        self._validate_inputs(
            policy_input=policy_input,
            candidates=candidate_values,
            stats=stat_values,
            protections=protection_values,
            budget=budget,
        )
        candidate_hash = candidate_digest(candidate_values)
        stats_hash = stats_digest(stat_values)
        input_hash = canonical_digest(
            {
                "policy_input_digest": policy_input.digest,
                "mechanism_version": self.config.mechanism_version,
                "mechanism_epoch": mechanism_epoch,
                "configuration_digest": self.config.digest,
                "candidate_digest": candidate_hash,
                "stats_digest": stats_hash,
                "protections": [
                    item.to_dict() for item in protection_values
                ],
                "budget": budget.to_dict(),
            }
        )
        if prior_mask is not None:
            self._validate_prior_mask(
                policy_input=policy_input,
                mechanism_epoch=mechanism_epoch,
                candidate_hash=candidate_hash,
                stats_hash=stats_hash,
                input_hash=input_hash,
                prior_mask=prior_mask,
            )
            return prior_mask

        stat_map = {item.edge.edge_id: item for item in stat_values}
        protection_map = {
            item.edge_id: item for item in protection_values
        }
        score_map = {
            item.edge_id: self._score(stat_map[item.edge_id], stat_values)
            for item in candidate_values
        }
        source_totals: dict[str, dict[str, int]] = {}
        for edge in candidate_values:
            value = source_totals.setdefault(
                edge.source_node_id,
                {"messages": 0, "malicious": 0, "failures": 0},
            )
            edge_stats = stat_map[edge.edge_id]
            value["messages"] += edge_stats.observed_messages
            value["malicious"] += edge_stats.malicious_messages
            value["failures"] += edge_stats.failed_deliveries
        keep: dict[str, bool] = {}
        isolation: dict[str, bool] = {}
        forced_reasons: dict[str, tuple[str, ...]] = {}
        for edge in candidate_values:
            edge_stats = stat_map[edge.edge_id]
            protection = protection_map.get(edge.edge_id)
            protected = protection.protected if protection is not None else False
            source = source_totals[edge.source_node_id]
            source_malicious_ratio = source["malicious"] / max(
                1,
                source["messages"],
            )
            source_failure_ratio = min(
                1.0,
                source["failures"] / max(1, source["messages"]),
            )
            malicious_isolation = (
                edge_stats.malicious_ratio
                >= self.config.malicious_isolation_ratio
                or source_malicious_ratio
                >= self.config.malicious_isolation_ratio
            )
            failure_isolation = (
                (
                    edge_stats.failure_ratio
                    >= self.config.failure_isolation_ratio
                    or source_failure_ratio
                    >= self.config.failure_isolation_ratio
                )
                and edge_stats.artifact_contribution_ratio == 0
                and edge_stats.evidence_utilization_ratio == 0
            )
            low_value = (
                score_map[edge.edge_id].total
                <= self.config.low_value_threshold
            )
            isolation[edge.edge_id] = (
                malicious_isolation or failure_isolation
            ) and not protected
            reasons = []
            if malicious_isolation:
                reasons.append("malicious_or_permission_denied_source_isolation")
            if failure_isolation:
                reasons.append("fault_source_without_downstream_contribution")
            if low_value:
                reasons.append("low_value_edge")
            forced_reasons[edge.edge_id] = tuple(reasons)
            keep[edge.edge_id] = protected or not (
                isolation[edge.edge_id] or (low_value and not protected)
            )

        current = self._selected_totals(stat_values, keep)
        if over_budget(current, budget):
            droppable = sorted(
                (
                    item
                    for item in candidate_values
                    if keep[item.edge_id]
                    and not (
                        protection_map.get(item.edge_id)
                        and protection_map[item.edge_id].protected
                    )
                ),
                key=lambda item: (
                    score_map[item.edge_id].total,
                    -stat_map[item.edge_id].redundancy_ratio,
                    -stat_map[item.edge_id].failure_ratio,
                    item.edge_type.value,
                    item.source_node_id,
                    item.target_node_id,
                    item.edge_id,
                ),
            )
            for edge in droppable:
                keep[edge.edge_id] = False
                current = self._selected_totals(stat_values, keep)
                if not over_budget(current, budget):
                    break
        if over_budget(current, budget):
            protected_edges = tuple(
                item.edge_id
                for item in candidate_values
                if keep[item.edge_id]
                and protection_map.get(item.edge_id)
                and protection_map[item.edge_id].protected
            )
            raise AgentPruneOptimizerError(
                "agentprune_protected_budget_infeasible",
                "communication budget cannot be met without dropping protected edges: "
                + ", ".join(protected_edges),
            )

        decisions = tuple(
            self._decision(
                edge=edge,
                edge_stats=stat_map[edge.edge_id],
                score=score_map[edge.edge_id],
                protection=protection_map.get(edge.edge_id),
                keep=keep[edge.edge_id],
                isolation=isolation[edge.edge_id],
                forced_reasons=forced_reasons[edge.edge_id],
                budget=budget,
            )
            for edge in candidate_values
        )
        return PruningMask(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            mechanism_version=self.config.mechanism_version,
            mechanism_epoch=mechanism_epoch,
            configuration_digest=self.config.digest,
            input_digest=input_hash,
            candidate_digest=candidate_hash,
            stats_digest=stats_hash,
            budget=budget,
            decisions=decisions,
            baseline_totals=totals(stat_values),
            retained_totals=self._selected_totals(stat_values, keep),
        )

    def build_proposal(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        upstream_proposal: TopologyProposalArtifact,
        mask: PruningMask,
        readiness_stage: str,
        readiness_status: str,
        readiness_report_digest: str,
    ) -> TopologyProposalArtifact | None:
        dropped = tuple(item for item in mask.decisions if not item.keep)
        if not dropped:
            return None
        operations = tuple(
            TopologyOperation(
                kind=TopologyOperationKind.REMOVE_EDGE,
                entity_id=item.edge.edge_id,
                value=FrozenDict(
                    {
                        "source_node_id": item.edge.source_node_id,
                        "target_node_id": item.edge.target_node_id,
                        "edge_type": item.edge.edge_type.value,
                        "pruning_mask_digest": mask.digest,
                        "mechanism_epoch": mask.mechanism_epoch,
                        "isolation": item.isolation,
                    }
                ),
                required_permissions=("graph.write",),
                reason="AgentPrune proposal only: " + "; ".join(item.reasons),
            )
            for item in dropped
        )
        proposal_id = "agentprune_proposal_" + canonical_digest(
            (
                policy_input.digest,
                upstream_proposal.digest,
                mask.digest,
                self.config.digest,
            )
        )[:20]
        created = datetime.fromisoformat(
            policy_input.header.created_at.replace("Z", "+00:00")
        ).astimezone(UTC)
        expected_savings = {
            "messages": int(mask.baseline_totals["messages"])
            - int(mask.retained_totals["messages"]),
            "bytes": int(mask.baseline_totals["bytes"])
            - int(mask.retained_totals["bytes"]),
            "tokens": int(mask.baseline_totals["tokens"])
            - int(mask.retained_totals["tokens"]),
            "cost_usd": round(
                float(mask.baseline_totals["cost_usd"])
                - float(mask.retained_totals["cost_usd"]),
                8,
            ),
        }
        return TopologyProposalArtifact(
            header=ContractHeader(
                contract_id=proposal_id,
                created_at=policy_input.header.created_at,
                source_event_id=policy_input.header.source_event_id,
                correlation_id=policy_input.header.correlation_id,
                causation_id=upstream_proposal.proposal_id,
                mechanism_id=self.config.mechanism_id,
                mechanism_version=self.config.mechanism_version,
                input_version=self.config.outcome_schema_version,
                idempotency_key=(
                    f"agentprune:{policy_input.run_id}:{mask.mechanism_epoch}:"
                    f"{mask.digest}"
                ),
                configuration_digest=self.config.digest,
            ),
            proposal_id=proposal_id,
            input_snapshot_digest=policy_input.digest,
            base_graph=upstream_proposal.base_graph,
            operations=operations,
            expected_outcome=FrozenDict(
                {
                    "mechanism": (
                        "AgentPrune deterministic spatial/temporal budget mask"
                    ),
                    "upstream_proposal_id": upstream_proposal.proposal_id,
                    "upstream_proposal_digest": upstream_proposal.digest,
                    "mask_digest": mask.digest,
                    "mechanism_epoch": mask.mechanism_epoch,
                    "readiness_stage": readiness_stage,
                    "readiness_status": readiness_status,
                    "readiness_report_digest": readiness_report_digest,
                    "decisions": [
                        item.to_dict() for item in mask.decisions
                    ],
                    "kept_spatial_edge_ids": list(
                        mask.kept_spatial_edge_ids
                    ),
                    "kept_temporal_edge_ids": list(
                        mask.kept_temporal_edge_ids
                    ),
                    "dropped_edge_ids": list(mask.dropped_edge_ids),
                    "baseline_totals": dict(mask.baseline_totals),
                    "retained_totals": dict(mask.retained_totals),
                    "expected_savings": expected_savings,
                    "expected_savings_are_counterfactual": True,
                    "actual_savings_require_paired_delivery_receipts": True,
                    "maximum_dropped_edge_verifier_risk": max(
                        item.verifier_risk for item in dropped
                    ),
                    "proposal_signal_mode": "deterministic_only",
                    "canonical_mutation_attempted": False,
                    "pending_side_effects": [],
                }
            ),
            alternatives=(),
            reasons=tuple(
                f"{item.edge.edge_type.value}:{item.edge.source_node_id}"
                f"->{item.edge.target_node_id} {item.action} "
                f"score={item.score.total:.8f}"
                for item in mask.decisions
            ),
            constraint_assumptions=(
                "critical path, unique evidence, unresolved obligation, recovery, continuity, and verifier edges are hard protected",
                "spatial and temporal outcome windows remain separately typed",
                "expected savings are diagnostic until actual paired delivery receipts and a passing verifier exist",
                "the unified symbolic projector must revalidate endpoints, DAG, permission, privacy, capacity, budget, and readiness",
                "GraphStateCustody remains the sole canonical graph owner",
            ),
            expires_at=(
                created + timedelta(seconds=self.config.proposal_ttl_seconds)
            ).isoformat().replace("+00:00", "Z"),
            fallback_profile=self.config.fallback_profile,
        )

    def _score(
        self,
        value: EdgeContributionStats,
        all_stats: tuple[EdgeContributionStats, ...],
    ) -> PruningScoreComponents:
        total_bytes = max(
            1,
            sum(item.delivered_bytes for item in all_stats),
        )
        total_tokens = max(
            1,
            sum(item.delivered_tokens for item in all_stats),
        )
        total_cost = max(
            0.00000001,
            sum(item.delivered_cost_usd for item in all_stats),
        )
        resource = min(
            1.0,
            (
                value.delivered_bytes / total_bytes
                + value.delivered_tokens / total_tokens
                + value.delivered_cost_usd / total_cost
            )
            / 3.0,
        )
        components = {
            "delivery": value.delivery_ratio,
            "evidence_utilization": value.evidence_utilization_ratio,
            "artifact_contribution": value.artifact_contribution_ratio,
            "verifier": value.verifier_pass_ratio,
            "redundancy": value.redundancy_ratio,
            "failure": value.failure_ratio,
            "retry": value.retry_ratio,
            "malicious": value.malicious_ratio,
            "resource": resource,
        }
        positive = sum(
            float(self.config.score_weights[key]) * components[key]
            for key in (
                "delivery",
                "evidence_utilization",
                "artifact_contribution",
                "verifier",
            )
        )
        negative = sum(
            float(self.config.score_weights[key]) * components[key]
            for key in (
                "redundancy",
                "failure",
                "retry",
                "malicious",
                "resource",
            )
        )
        denominator = sum(
            float(item) for item in self.config.score_weights.values()
        )
        score = max(-1.0, min(1.0, (positive - negative) / denominator))
        return PruningScoreComponents(
            delivery=components["delivery"],
            evidence_utilization=components["evidence_utilization"],
            artifact_contribution=components["artifact_contribution"],
            verifier=components["verifier"],
            redundancy_penalty=components["redundancy"],
            failure_penalty=components["failure"],
            retry_penalty=components["retry"],
            malicious_penalty=components["malicious"],
            resource_penalty=components["resource"],
            total=score,
        )

    def _decision(
        self,
        *,
        edge: CommunicationEdgeCandidate,
        edge_stats: EdgeContributionStats,
        score: PruningScoreComponents,
        protection: ProtectedEdgeConstraint | None,
        keep: bool,
        isolation: bool,
        forced_reasons: tuple[str, ...],
        budget: CommunicationBudget,
    ) -> PruningDecision:
        hard = protection.hard_reasons if protection is not None else ()
        verifier_risk = max(
            0.0,
            min(
                1.0,
                1.0
                - (
                    0.45 * edge_stats.verifier_pass_ratio
                    + 0.35 * edge_stats.evidence_utilization_ratio
                    + 0.20 * edge_stats.artifact_contribution_ratio
                ),
            ),
        )
        reasons = list(forced_reasons)
        if hard:
            reasons.append("hard_protection:" + ",".join(hard))
        if not keep and not reasons:
            reasons.append("communication_budget_pressure")
        elif keep and not reasons:
            reasons.append("retained_by_deterministic_utility_order")
        if not keep and verifier_risk > self.config.maximum_verifier_risk:
            reasons.append(
                "verifier_risk_requires_downstream_hard_gate"
            )
        return PruningDecision(
            edge=edge,
            keep=keep,
            action="keep" if keep else "drop",
            stats=edge_stats,
            score=score,
            hard_constraints=hard,
            reasons=tuple(reasons),
            expected_savings=FrozenDict(
                {
                    "messages": 0 if keep else edge_stats.delivered_messages,
                    "bytes": 0 if keep else edge_stats.delivered_bytes,
                    "tokens": 0 if keep else edge_stats.delivered_tokens,
                    "cost_usd": (
                        0.0
                        if keep
                        else round(edge_stats.delivered_cost_usd, 8)
                    ),
                    "counterfactual_only": not keep,
                    "actual_requires_delivery_receipt_and_verifier": not keep,
                    "budget": budget.to_dict(),
                }
            ),
            verifier_risk=verifier_risk,
            isolation=isolation,
        )

    @staticmethod
    def _selected_totals(
        stats: tuple[EdgeContributionStats, ...],
        keep: Mapping[str, bool],
    ) -> FrozenDict:
        return totals(
            tuple(item for item in stats if keep.get(item.edge.edge_id, False))
        )

    def _validate_inputs(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        candidates: tuple[CommunicationEdgeCandidate, ...],
        stats: tuple[EdgeContributionStats, ...],
        protections: tuple[ProtectedEdgeConstraint, ...],
        budget: CommunicationBudget,
    ) -> None:
        edge_ids = [item.edge_id for item in candidates]
        if not edge_ids or len(edge_ids) != len(set(edge_ids)):
            raise AgentPruneOptimizerError(
                "agentprune_candidate_set_invalid",
                "AgentPrune requires a non-empty, unique candidate edge set",
            )
        edge_types = {item.edge_type.value for item in candidates}
        if edge_types != {"spatial", "temporal"}:
            raise AgentPruneOptimizerError(
                "agentprune_spatial_temporal_input_incomplete",
                "deterministic readiness requires separately typed spatial and temporal candidates",
            )
        stat_ids = [item.edge.edge_id for item in stats]
        if sorted(stat_ids) != sorted(edge_ids):
            raise AgentPruneOptimizerError(
                "agentprune_stats_coverage_invalid",
                "every candidate requires exactly one contribution statistic",
            )
        if len(stat_ids) != len(set(stat_ids)):
            raise AgentPruneOptimizerError(
                "agentprune_stats_duplicate_edge",
                "edge contribution statistics must be unique",
            )
        protection_ids = [item.edge_id for item in protections]
        if len(protection_ids) != len(set(protection_ids)) or any(
            edge_id not in set(edge_ids) for edge_id in protection_ids
        ):
            raise AgentPruneOptimizerError(
                "agentprune_protection_binding_invalid",
                "protected-edge constraints must bind unique candidate edges",
            )
        obligation_set = set(policy_input.unresolved_obligations)
        for item in protections:
            if not set(item.unresolved_obligation_refs).issubset(obligation_set):
                raise AgentPruneOptimizerError(
                    "agentprune_obligation_protection_stale",
                    "protected obligation references differ from the policy input",
                )
        if (
            budget.max_delivered_bytes
            > policy_input.budget.max_communication_bytes
        ):
            raise AgentPruneOptimizerError(
                "agentprune_budget_exceeds_policy_input",
                "pruning byte budget cannot exceed the policy snapshot budget",
            )

    def _validate_prior_mask(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        mechanism_epoch: str,
        candidate_hash: str,
        stats_hash: str,
        input_hash: str,
        prior_mask: PruningMask,
    ) -> None:
        expected = {
            "run_id": policy_input.run_id,
            "task_id": policy_input.task_id,
            "mechanism_version": self.config.mechanism_version,
            "mechanism_epoch": mechanism_epoch,
            "configuration_digest": self.config.digest,
            "candidate_digest": candidate_hash,
            "stats_digest": stats_hash,
            "input_digest": input_hash,
        }
        drift = tuple(
            name
            for name, value in expected.items()
            if getattr(prior_mask, name) != value
        )
        if drift:
            raise AgentPruneOptimizerError(
                "agentprune_epoch_mask_drift",
                "a frozen mechanism epoch cannot be resampled or rebound: "
                + ", ".join(drift),
            )


__all__ = [
    "AGENTPRUNE_CONFIG_SCHEMA",
    "AgentPruneOptimizerConfig",
    "AgentPruneOptimizerError",
    "DeterministicCommunicationOptimizer",
]
