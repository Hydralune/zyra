from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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
from .environment_encoder import (
    CARDBaseEdge,
    CARDEncodedEnvironment,
    CARDNodeFeature,
    CARDReplacementCandidate,
)
from .hysteresis import (
    CARDEdgeHysteresisState,
    CARDHysteresisController,
    CARDHysteresisDecision,
)


CARD_CONFIG_SCHEMA = "zyra.card-directional-residual-config/v1"
CARD_RESIDUAL_SCHEMA = "zyra.card-residual/v1"


class CARDResidualError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _round(value: float) -> float:
    return round(float(value), 8)


def _tokens(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        selected: Sequence[Any] = (value,)
    elif isinstance(value, Mapping):
        selected = tuple(value.keys())
    else:
        selected = tuple(value)
    return tuple(
        sorted(
            {
                str(item).strip().lower()
                for item in selected
                if str(item).strip()
            }
        )
    )


@dataclass(frozen=True, slots=True)
class CARDResidualCorrectorConfig:
    mechanism_id: str
    mechanism_version: str
    environment_schema_version: str
    encoded_schema_version: str
    residual_schema_version: str
    fallback_profile: str
    input_precheck_report_digest: str
    proposal_ttl_seconds: int
    required_categories: tuple[str, ...]
    optional_categories: tuple[str, ...]
    required_observation_fields: tuple[str, ...]
    minimum_confidence: float
    stale_confidence_multiplier: float
    latency_reference_ms: float
    cost_reference_usd: float
    load_reference: float
    score_weights: FrozenDict
    add_threshold: float
    drop_threshold: float
    switch_cost: float
    minimum_dwell_seconds: int
    switch_confirmations: int
    reweight_epsilon: float
    estimated_edge_bytes: int
    no_policy_training: FrozenDict
    digest: str
    path: str = ""

    @classmethod
    def load(cls, path: Path) -> CARDResidualCorrectorConfig:
        selected = path.resolve()
        try:
            value = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CARDResidualError(
                "card_config_invalid",
                f"CARD configuration is missing or corrupt: {selected}",
            ) from exc
        if not isinstance(value, Mapping) or value.get("schema") != CARD_CONFIG_SCHEMA:
            raise CARDResidualError(
                "card_config_schema_invalid",
                "unsupported CARD directional residual configuration",
            )
        no_training = value.get("no_policy_training")
        if not isinstance(no_training, Mapping):
            raise CARDResidualError(
                "card_config_no_training_missing",
                "CARD configuration requires the no-policy-training declaration",
            )
        if (
            no_training.get("training_allowed") is not False
            or no_training.get("sampling_allowed") is not False
            or no_training.get("datasets") not in ([], ())
            or no_training.get("checkpoints") not in ([], ())
            or no_training.get("mutable_learned_parameters") not in ([], ())
            or no_training.get("learned_gcn_weights") not in ([], ())
        ):
            raise CARDResidualError(
                "card_config_training_forbidden",
                "CARD cannot enable training, sampling, datasets, checkpoints, or learned GCN weights",
            )
        weights = value.get("score_weights")
        required_weights = {
            "capability",
            "health",
            "latency",
            "load",
            "cost",
            "privacy",
            "freshness",
        }
        if (
            not isinstance(weights, Mapping)
            or set(weights) != required_weights
            or any(float(item) < 0 for item in weights.values())
            or sum(float(item) for item in weights.values()) <= 0
        ):
            raise CARDResidualError(
                "card_config_weights_invalid",
                "CARD requires the fixed seven non-negative residual weights",
            )
        add_threshold = float(value.get("add_threshold") or 0)
        drop_threshold = float(value.get("drop_threshold") or 0)
        if not -1 <= drop_threshold < add_threshold <= 1:
            raise CARDResidualError(
                "card_config_thresholds_invalid",
                "CARD drop threshold must be below add threshold within [-1, 1]",
            )
        report_digest = str(value.get("input_precheck_report_digest") or "")
        if (
            len(report_digest) != 64
            or any(character not in "0123456789abcdef" for character in report_digest)
        ):
            raise CARDResidualError(
                "card_config_readiness_digest_invalid",
                "CARD input-precheck report digest must be SHA-256",
            )
        confidence = float(value.get("minimum_confidence") or 0)
        stale_multiplier = float(value.get("stale_confidence_multiplier") or 0)
        if not 0 <= confidence <= 1 or not 0 <= stale_multiplier <= 1:
            raise CARDResidualError(
                "card_config_confidence_invalid",
                "CARD confidence thresholds must be within [0, 1]",
            )
        return cls(
            mechanism_id=str(value.get("mechanism_id") or ""),
            mechanism_version=str(value.get("mechanism_version") or ""),
            environment_schema_version=str(
                value.get("environment_schema_version") or ""
            ),
            encoded_schema_version=str(value.get("encoded_schema_version") or ""),
            residual_schema_version=str(value.get("residual_schema_version") or ""),
            fallback_profile=str(value.get("fallback_profile") or ""),
            input_precheck_report_digest=report_digest,
            proposal_ttl_seconds=max(
                1, int(value.get("proposal_ttl_seconds") or 1)
            ),
            required_categories=_tokens(value.get("required_categories")),
            optional_categories=_tokens(value.get("optional_categories")),
            required_observation_fields=_tokens(
                value.get("required_observation_fields")
            ),
            minimum_confidence=confidence,
            stale_confidence_multiplier=stale_multiplier,
            latency_reference_ms=max(
                1.0, float(value.get("latency_reference_ms") or 1)
            ),
            cost_reference_usd=max(
                0.000001, float(value.get("cost_reference_usd") or 0.000001)
            ),
            load_reference=max(
                0.000001, float(value.get("load_reference") or 0.000001)
            ),
            score_weights=FrozenDict(
                {key: float(item) for key, item in weights.items()}
            ),
            add_threshold=add_threshold,
            drop_threshold=drop_threshold,
            switch_cost=max(0.0, float(value.get("switch_cost") or 0)),
            minimum_dwell_seconds=max(
                0, int(value.get("minimum_dwell_seconds") or 0)
            ),
            switch_confirmations=max(
                1, int(value.get("switch_confirmations") or 1)
            ),
            reweight_epsilon=max(
                0.0, float(value.get("reweight_epsilon") or 0)
            ),
            estimated_edge_bytes=max(
                0, int(value.get("estimated_edge_bytes") or 0)
            ),
            no_policy_training=FrozenDict(no_training),
            digest=canonical_digest(value),
            path=selected.as_posix(),
        )


@dataclass(frozen=True, slots=True)
class CARDScoreComponents:
    capability: float
    health: float
    latency: float
    load: float
    cost: float
    privacy: float
    freshness: float
    weighted_total: float
    switch_cost: float
    total: float

    def to_dict(self) -> dict[str, float]:
        return {
            "capability": _round(self.capability),
            "health": _round(self.health),
            "latency": _round(self.latency),
            "load": _round(self.load),
            "cost": _round(self.cost),
            "privacy": _round(self.privacy),
            "freshness": _round(self.freshness),
            "weighted_total": _round(self.weighted_total),
            "switch_cost": _round(self.switch_cost),
            "total": _round(self.total),
        }


@dataclass(frozen=True, slots=True)
class CARDResidualDecision:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation: str
    edge_type: str
    required_capabilities: tuple[str, ...]
    candidate: bool
    active_before: bool
    active_after: bool
    action: str
    requested_action: str
    score: CARDScoreComponents
    confidence: float
    feature_contributions: FrozenDict
    feature_lineage: tuple[FrozenDict, ...]
    rejection_reasons: tuple[str, ...]
    reasons: tuple[str, ...]
    hysteresis: CARDHysteresisDecision

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "relation": self.relation,
            "edge_type": self.edge_type,
            "required_capabilities": list(self.required_capabilities),
            "candidate": self.candidate,
            "active_before": self.active_before,
            "active_after": self.active_after,
            "action": self.action,
            "requested_action": self.requested_action,
            "score": self.score.to_dict(),
            "confidence": _round(self.confidence),
            "feature_contributions": dict(self.feature_contributions),
            "feature_lineage": [dict(item) for item in self.feature_lineage],
            "rejection_reasons": list(self.rejection_reasons),
            "reasons": list(self.reasons),
            "hysteresis": self.hysteresis.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CARDResidualCorrection:
    schema_version: str
    mechanism_version: str
    configuration_digest: str
    encoded_environment_digest: str
    policy_input_digest: str
    arg_base_proposal_id: str
    arg_base_proposal_digest: str
    environment_snapshot_digest: str
    observed_at: str
    decisions: tuple[CARDResidualDecision, ...]
    effective_spatial_edge_ids: tuple[str, ...]
    effective_temporal_edge_ids: tuple[str, ...]
    missing_optional_categories: tuple[str, ...]
    trigger_flags: tuple[str, ...]

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @property
    def hysteresis_state(self) -> Mapping[str, CARDEdgeHysteresisState]:
        return {
            item.edge_id: item.hysteresis.next_state
            for item in self.decisions
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mechanism_version": self.mechanism_version,
            "configuration_digest": self.configuration_digest,
            "encoded_environment_digest": self.encoded_environment_digest,
            "policy_input_digest": self.policy_input_digest,
            "arg_base_proposal_id": self.arg_base_proposal_id,
            "arg_base_proposal_digest": self.arg_base_proposal_digest,
            "environment_snapshot_digest": self.environment_snapshot_digest,
            "observed_at": self.observed_at,
            "decisions": [item.to_dict() for item in self.decisions],
            "effective_spatial_edge_ids": list(self.effective_spatial_edge_ids),
            "effective_temporal_edge_ids": list(self.effective_temporal_edge_ids),
            "missing_optional_categories": list(self.missing_optional_categories),
            "trigger_flags": list(self.trigger_flags),
        }


class CARDResidualCorrector:
    """Deterministic, directional CARD residual without GCN or trained weights."""

    def __init__(self, config: CARDResidualCorrectorConfig) -> None:
        self.config = config

    def correct(
        self,
        encoded: CARDEncodedEnvironment,
        *,
        hysteresis_state: Mapping[str, CARDEdgeHysteresisState] | None = None,
    ) -> CARDResidualCorrection:
        if encoded.schema_version != self.config.encoded_schema_version:
            raise CARDResidualError(
                "card_encoded_schema_drift",
                "encoded CARD environment schema differs from configuration",
            )
        if (
            encoded.mechanism_version != self.config.mechanism_version
            or encoded.configuration_digest != self.config.digest
        ):
            raise CARDResidualError(
                "card_configuration_drift",
                "encoded CARD mechanism/configuration binding is stale",
            )
        state = dict(hysteresis_state or {})
        decisions: list[CARDResidualDecision] = []
        for edge in encoded.base_edges:
            decisions.append(
                self._decision(
                    encoded,
                    edge=edge,
                    candidate=False,
                    state=state.get(edge.edge_id),
                )
            )
        for candidate in encoded.replacement_candidates:
            decisions.append(
                self._decision(
                    encoded,
                    edge=candidate,
                    candidate=True,
                    state=state.get(candidate.candidate_id),
                )
            )
        if not decisions:
            raise CARDResidualError(
                "card_empty_correction",
                "CARD cannot produce residuals without ARG base edges or constrained candidates",
            )
        spatial = tuple(
            sorted(
                item.edge_id
                for item in decisions
                if item.edge_type == "spatial" and item.active_after
            )
        )
        temporal = tuple(
            sorted(
                item.edge_id
                for item in decisions
                if item.edge_type == "temporal" and item.active_after
            )
        )
        return CARDResidualCorrection(
            schema_version=CARD_RESIDUAL_SCHEMA,
            mechanism_version=self.config.mechanism_version,
            configuration_digest=self.config.digest,
            encoded_environment_digest=encoded.digest,
            policy_input_digest=encoded.policy_input.digest,
            arg_base_proposal_id=encoded.arg_base_proposal_id,
            arg_base_proposal_digest=encoded.arg_base_proposal_digest,
            environment_snapshot_digest=encoded.environment_snapshot_digest,
            observed_at=encoded.observed_at,
            decisions=tuple(
                sorted(
                    decisions,
                    key=lambda item: (
                        item.edge_type,
                        item.source_node_id,
                        item.target_node_id,
                        item.edge_id,
                    ),
                )
            ),
            effective_spatial_edge_ids=spatial,
            effective_temporal_edge_ids=temporal,
            missing_optional_categories=encoded.missing_optional_categories,
            trigger_flags=encoded.trigger_flags,
        )

    def build_proposal(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        arg_base: TopologyProposalArtifact,
        encoded: CARDEncodedEnvironment,
        correction: CARDResidualCorrection,
        readiness_stage: str,
        readiness_status: str,
        readiness_report_digest: str,
    ) -> TopologyProposalArtifact | None:
        operations = tuple(
            operation
            for decision in correction.decisions
            if (
                operation := self._operation(
                    decision,
                    encoded=encoded,
                )
            )
            is not None
        )
        if not operations:
            return None
        proposal_id = "card_proposal_" + canonical_digest(
            (
                policy_input.digest,
                arg_base.digest,
                correction.digest,
                self.config.digest,
            )
        )[:20]
        created = datetime.fromisoformat(
            policy_input.header.created_at.replace("Z", "+00:00")
        ).astimezone(UTC)
        counts = {
            action: sum(
                item.action == action for item in correction.decisions
            )
            for action in ("add", "drop", "reweight", "hold", "reject")
        }
        expected = FrozenDict(
            {
                "mechanism": "CARD deterministic directional residual",
                "arg_base_proposal_id": arg_base.proposal_id,
                "arg_base_proposal_digest": arg_base.digest,
                "environment_snapshot_digest": encoded.environment_snapshot_digest,
                "encoded_environment_digest": encoded.digest,
                "correction_digest": correction.digest,
                "readiness_stage": readiness_stage,
                "readiness_status": readiness_status,
                "readiness_report_digest": readiness_report_digest,
                "spatial_diff": [
                    item.to_dict()
                    for item in correction.decisions
                    if item.edge_type == "spatial"
                ],
                "temporal_diff": [
                    item.to_dict()
                    for item in correction.decisions
                    if item.edge_type == "temporal"
                ],
                "effective_spatial_edge_ids": list(
                    correction.effective_spatial_edge_ids
                ),
                "effective_temporal_edge_ids": list(
                    correction.effective_temporal_edge_ids
                ),
                "action_counts": counts,
                "missing_optional_categories": list(
                    correction.missing_optional_categories
                ),
                "trigger_flags": list(correction.trigger_flags),
                "expected_effect": (
                    "adapt ARG directional routes to real environment conditions "
                    "without replacing the ARG base topology"
                ),
                "tokens": 0,
                "cost_usd": 0.0,
                "time_ms": len(correction.decisions),
                "communication_bytes": sum(
                    operation.communication_bytes for operation in operations
                ),
                "pending_side_effects": [],
                "proposal_signal_mode": "deterministic_only",
            }
        )
        return TopologyProposalArtifact(
            header=ContractHeader(
                contract_id=proposal_id,
                created_at=policy_input.header.created_at,
                source_event_id=policy_input.header.source_event_id,
                correlation_id=policy_input.header.correlation_id,
                causation_id=arg_base.proposal_id,
                mechanism_id=self.config.mechanism_id,
                mechanism_version=self.config.mechanism_version,
                input_version=self.config.encoded_schema_version,
                idempotency_key=(
                    f"card:{arg_base.digest}:{encoded.environment_snapshot_digest}:"
                    f"{self.config.digest}"
                ),
                configuration_digest=self.config.digest,
            ),
            proposal_id=proposal_id,
            input_snapshot_digest=policy_input.digest,
            base_graph=arg_base.base_graph,
            operations=operations,
            expected_outcome=expected,
            alternatives=(),
            reasons=tuple(
                f"{item.edge_type}:{item.source_node_id}->{item.target_node_id} "
                f"{item.action} score={_round(item.score.total)}"
                for item in correction.decisions
            ),
            constraint_assumptions=(
                "ARG remains the sole base-topology proposal mechanism",
                "CARD operations are residuals over ARG edges or upstream constrained replacements",
                "GraphStateCustody remains the sole canonical graph owner",
                "the symbolic composer must revalidate endpoints, privacy, DAG, budget, capacity, and readiness",
                "implementation_validated and evidence_only correction proposals are non-committing",
            ),
            expires_at=(
                created + timedelta(seconds=self.config.proposal_ttl_seconds)
            ).isoformat().replace("+00:00", "Z"),
            fallback_profile=self.config.fallback_profile,
        )

    def _decision(
        self,
        encoded: CARDEncodedEnvironment,
        *,
        edge: CARDBaseEdge | CARDReplacementCandidate,
        candidate: bool,
        state: CARDEdgeHysteresisState | None,
    ) -> CARDResidualDecision:
        edge_id = (
            edge.candidate_id
            if isinstance(edge, CARDReplacementCandidate)
            else edge.edge_id
        )
        source = encoded.node_map.get(edge.source_node_id)
        target = encoded.node_map.get(edge.target_node_id)
        if source is None or target is None:
            raise CARDResidualError(
                "card_edge_endpoint_outside_arg_base",
                "CARD residual edge endpoint is not in the ARG role-node hypothesis",
            )
        components, hard_reasons, stale_reasons = self._score(
            encoded,
            source=source,
            target=target,
            required_capabilities=edge.required_capabilities,
            candidate=candidate,
        )
        hard = bool(hard_reasons)
        if hard:
            requested = "reject" if candidate else "drop"
        elif stale_reasons:
            requested = "reject" if candidate else "hold"
        elif candidate:
            requested = (
                "add"
                if components.total >= self.config.add_threshold
                else "reject"
            )
        else:
            requested = (
                "drop"
                if components.total <= self.config.drop_threshold
                else "reweight"
            )
        active_before = state.active if state is not None else not candidate
        hysteresis = CARDHysteresisController.evaluate(
            edge_id=edge_id,
            requested_action=requested,
            score=components.total,
            observed_at=encoded.observed_at,
            active_before=active_before,
            last_topology_change_at=encoded.policy_input.last_topology_change_at,
            state=state,
            hard_constraint=hard and requested == "drop",
            minimum_dwell_seconds=max(
                self.config.minimum_dwell_seconds,
                encoded.policy_input.budget.minimum_dwell_seconds,
            ),
            switch_confirmations=self.config.switch_confirmations,
            reweight_epsilon=self.config.reweight_epsilon,
            switch_cost=self.config.switch_cost,
        )
        action = hysteresis.applied_action
        active_after = hysteresis.next_state.active
        confidence = min(
            item.effective_confidence
            for item in (*source.observations, *target.observations)
        )
        contributions = FrozenDict(
            {
                key: _round(
                    float(self.config.score_weights[key])
                    * float(getattr(components, key))
                )
                for key in self.config.score_weights
            }
        )
        lineage = tuple(
            FrozenDict(
                {
                    "observation_id": item.observation_id,
                    "resource_id": item.resource_id,
                    "category": item.category,
                    "source": item.observation_source,
                    "source_event_id": item.source_event_id,
                    "observed_at": item.observed_at,
                    "fresh_until": item.fresh_until,
                    "stale": item.stale,
                    "confidence": item.confidence,
                    "effective_confidence": item.effective_confidence,
                    "missing_required": list(item.missing_required),
                    "missing_optional": list(item.missing_optional),
                }
            )
            for item in sorted(
                {
                    item.observation_id: item
                    for item in (*source.observations, *target.observations)
                }.values(),
                key=lambda item: (
                    item.category,
                    item.resource_id,
                    item.observation_id,
                ),
            )
        )
        reasons = (
            f"directional source={source.node_id} target={target.node_id}",
            f"capability={_round(components.capability)} health={_round(components.health)}",
            f"latency={_round(components.latency)} load={_round(components.load)} cost={_round(components.cost)}",
            f"privacy={_round(components.privacy)} freshness={_round(components.freshness)}",
            f"hysteresis={hysteresis.reason_code}",
        )
        return CARDResidualDecision(
            edge_id=edge_id,
            source_node_id=edge.source_node_id,
            target_node_id=edge.target_node_id,
            relation=edge.relation,
            edge_type=edge.edge_type,
            required_capabilities=edge.required_capabilities,
            candidate=candidate,
            active_before=active_before,
            active_after=active_after,
            action=action,
            requested_action=requested,
            score=components,
            confidence=confidence,
            feature_contributions=contributions,
            feature_lineage=lineage,
            rejection_reasons=tuple((*hard_reasons, *stale_reasons)),
            reasons=reasons,
            hysteresis=hysteresis,
        )

    def _score(
        self,
        encoded: CARDEncodedEnvironment,
        *,
        source: CARDNodeFeature,
        target: CARDNodeFeature,
        required_capabilities: tuple[str, ...],
        candidate: bool,
    ) -> tuple[CARDScoreComponents, tuple[str, ...], tuple[str, ...]]:
        source_primary = source.primary
        target_primary = target.primary
        hard_reasons: list[str] = []
        if not source_primary.available:
            hard_reasons.append("source_unavailable")
        if not target_primary.available:
            hard_reasons.append("target_unavailable")
        if not target_primary.lease_available or target_primary.capacity_available <= 0:
            hard_reasons.append("target_capacity_or_lease_unavailable")
        if not source_primary.network_connected:
            hard_reasons.append("source_network_disconnected")
        if not target_primary.network_connected:
            hard_reasons.append("target_network_disconnected")
        privacy_legal = self._privacy_legal(
            encoded,
            source=source,
            target=target,
        )
        if not privacy_legal:
            hard_reasons.append("privacy_or_placement_forbidden")
        stale_reasons = tuple(
            sorted(
                {
                    (
                        f"stale_telemetry:{item.resource_id}"
                        if item.stale
                        else f"low_confidence:{item.resource_id}"
                    )
                    for item in (*source.observations, *target.observations)
                    if (
                        item.stale
                        or item.effective_confidence < self.config.minimum_confidence
                    )
                }
            )
        )

        required = set(required_capabilities)
        source_capabilities = set(source.capabilities)
        target_capabilities = set(target.capabilities)
        if required:
            target_fit = len(required.intersection(target_capabilities)) / len(required)
            source_support = len(required.intersection(source_capabilities)) / len(required)
        else:
            target_fit = 1.0
            source_support = 0.5
        novelty = len(target_capabilities - source_capabilities) / max(
            1, len(target_capabilities)
        )
        capability = 2.0 * (
            0.65 * target_fit + 0.25 * novelty + 0.10 * source_support
        ) - 1.0

        source_health = (
            1.0
            if source_primary.available and source_primary.healthy
            else (0.0 if source_primary.available else -1.0)
        )
        target_health = (
            1.0
            if target_primary.available and target_primary.healthy
            else (0.0 if target_primary.available else -1.0)
        )
        failure_penalty = min(
            1.0,
            (
                source_primary.recent_failures
                + 2 * target_primary.recent_failures
            )
            / 6.0,
        )
        health = (
            0.35 * source_health
            + 0.65 * target_health
            - failure_penalty
        )

        directional_latency = (
            0.25 * source_primary.latency_p50_ms
            + 0.75 * max(
                target_primary.latency_p50_ms,
                target_primary.latency_p95_ms,
            )
        )
        latency = 1.0 - 2.0 * min(
            1.0,
            directional_latency / self.config.latency_reference_ms,
        )

        target_load = (
            target_primary.load
            + target_primary.queue_depth
            / max(1.0, target_primary.capacity_available)
        )
        load = 1.0 - 2.0 * min(
            1.0,
            target_load / self.config.load_reference,
        )
        target_cost = sum(item.cost_usd for item in target.observations)
        cost = 1.0 - 2.0 * min(
            1.0,
            target_cost / self.config.cost_reference_usd,
        )
        privacy = 1.0 if privacy_legal else -1.0
        confidence = sum(
            item.effective_confidence
            for item in (*source.observations, *target.observations)
        ) / max(1, len(source.observations) + len(target.observations))
        freshness = 2.0 * confidence - 1.0
        components = {
            "capability": max(-1.0, min(1.0, capability)),
            "health": max(-1.0, min(1.0, health)),
            "latency": max(-1.0, min(1.0, latency)),
            "load": max(-1.0, min(1.0, load)),
            "cost": max(-1.0, min(1.0, cost)),
            "privacy": privacy,
            "freshness": max(-1.0, min(1.0, freshness)),
        }
        total_weight = sum(
            float(item) for item in self.config.score_weights.values()
        )
        weighted = sum(
            float(self.config.score_weights[key]) * value
            for key, value in components.items()
        ) / total_weight
        applied_switch_cost = 0.0
        total = weighted
        if candidate:
            applied_switch_cost = self.config.switch_cost
            total -= applied_switch_cost
        elif weighted <= self.config.drop_threshold:
            applied_switch_cost = self.config.switch_cost
            total += applied_switch_cost
        if hard_reasons:
            total = -1.0
        score = CARDScoreComponents(
            capability=components["capability"],
            health=components["health"],
            latency=components["latency"],
            load=components["load"],
            cost=components["cost"],
            privacy=components["privacy"],
            freshness=components["freshness"],
            weighted_total=weighted,
            switch_cost=applied_switch_cost,
            total=max(-1.0, min(1.0, total)),
        )
        return score, tuple(sorted(set(hard_reasons))), stale_reasons

    @staticmethod
    def _privacy_legal(
        encoded: CARDEncodedEnvironment,
        *,
        source: CARDNodeFeature,
        target: CARDNodeFeature,
    ) -> bool:
        privacy = encoded.policy_input.privacy_class.lower()
        allowed = set(encoded.policy_input.allowed_placements)
        for node in (source, target):
            primary = node.primary
            placement = (node.requested_placement or primary.location).lower()
            if (
                privacy not in set(primary.privacy_classes)
                or placement not in allowed
                or placement not in set(primary.allowed_placements)
            ):
                return False
        return True

    def _operation(
        self,
        decision: CARDResidualDecision,
        *,
        encoded: CARDEncodedEnvironment,
    ) -> TopologyOperation | None:
        if decision.action not in {"add", "drop", "reweight"}:
            return None
        target = encoded.node_map[decision.target_node_id]
        if decision.action == "drop":
            return TopologyOperation(
                kind=TopologyOperationKind.REMOVE_EDGE,
                entity_id=decision.edge_id,
                required_permissions=("graph.write",),
                reason=(
                    "CARD directional residual drop: "
                    + ", ".join(decision.rejection_reasons or decision.reasons)
                ),
            )
        kind = (
            TopologyOperationKind.ADD_EDGE
            if decision.action == "add"
            else TopologyOperationKind.REPLACE_EDGE
        )
        return TopologyOperation(
            kind=kind,
            entity_id=decision.edge_id,
            value=FrozenDict(
                {
                    "source_node_id": decision.source_node_id,
                    "target_node_id": decision.target_node_id,
                    "relation": decision.relation,
                    "edge_type": decision.edge_type,
                    "required_capabilities": list(
                        decision.required_capabilities
                    ),
                    "condition": {
                        "environment_snapshot_digest": encoded.environment_snapshot_digest,
                        "residual_score": decision.score.total,
                        "confidence": decision.confidence,
                    },
                    "labels": {
                        "arg_base_owner": "arg_designer",
                        "card_residual": "card",
                    },
                    "metadata": {
                        "card_action": decision.action,
                        "card_score": decision.score.to_dict(),
                        "card_feature_contributions": dict(
                            decision.feature_contributions
                        ),
                        "card_hysteresis": decision.hysteresis.to_dict(),
                    },
                }
            ),
            required_permissions=("graph.write",),
            requested_placement=(
                target.requested_placement or target.primary.location
            ),
            resource_id=target.worker_id,
            required_capacity=1.0,
            communication_bytes=self.config.estimated_edge_bytes,
            reason=decision.reasons[-1],
        )
