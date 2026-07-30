from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..contracts import FrozenDict, canonical_digest


AGENTPRUNE_OUTCOME_SCHEMA = "zyra.agentprune-communication-outcome/v1"
AGENTPRUNE_STATS_SCHEMA = "zyra.agentprune-edge-contribution/v1"
AGENTPRUNE_MASK_SCHEMA = "zyra.agentprune-deterministic-mask/v1"
AGENTPRUNE_DELIVERY_SCHEMA = "zyra.agentprune-delivery-receipt/v1"


class AgentPruneContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CommunicationEdgeType(StrEnum):
    SPATIAL = "spatial"
    TEMPORAL = "temporal"


def _required(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise AgentPruneContractError(
            f"agentprune_{name}_missing",
            f"{name} is required",
        )
    return normalized


def _tokens(value: Sequence[Any] | str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    selected: Sequence[Any] = (value,) if isinstance(value, str) else value
    return tuple(
        sorted(
            {
                str(item).strip()
                for item in selected
                if str(item).strip()
            }
        )
    )


def _sha256(value: str, name: str) -> str:
    normalized = _required(value, name).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise AgentPruneContractError(
            f"agentprune_{name}_invalid",
            f"{name} must be a lowercase SHA-256 digest",
        )
    return normalized


def _edge_type(value: CommunicationEdgeType | str) -> CommunicationEdgeType:
    try:
        return CommunicationEdgeType(value)
    except ValueError as exc:
        raise AgentPruneContractError(
            "agentprune_edge_type_invalid",
            "edge_type must be spatial or temporal",
        ) from exc


def _round(value: float) -> float:
    return round(float(value), 8)


@dataclass(frozen=True, slots=True)
class CommunicationEdgeCandidate:
    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: CommunicationEdgeType
    relation: str
    required_capabilities: tuple[str, ...] = ()
    source_proposal_ref: str = ""
    metadata: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        for name in ("edge_id", "source_node_id", "target_node_id", "relation"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.source_node_id == self.target_node_id:
            raise AgentPruneContractError(
                "agentprune_self_edge_forbidden",
                "communication candidate endpoints must differ",
            )
        object.__setattr__(self, "edge_type", _edge_type(self.edge_type))
        object.__setattr__(
            self,
            "required_capabilities",
            _tokens(self.required_capabilities),
        )
        object.__setattr__(self, "metadata", FrozenDict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "edge_type": self.edge_type.value,
            "relation": self.relation,
            "required_capabilities": list(self.required_capabilities),
            "source_proposal_ref": self.source_proposal_ref,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CommunicationOutcomeObservation:
    observation_id: str
    run_id: str
    task_id: str
    window_id: str
    completed_at: str
    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: CommunicationEdgeType
    round_index: int
    message_id: str
    payload_digest: str
    delivered: bool
    delivery_receipt_ref: str
    usage_receipt_ref: str
    message_bytes: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    evidence_refs: tuple[str, ...] = ()
    utilized_evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    verifier_result: str = "not_run"
    failure_count: int = 0
    retry_count: int = 0
    redundant_with_message_id: str = ""
    permission_result: str = "allowed"
    malicious: bool = False
    causal_refs: tuple[str, ...] = ()
    schema_version: str = AGENTPRUNE_OUTCOME_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "observation_id",
            "run_id",
            "task_id",
            "window_id",
            "completed_at",
            "edge_id",
            "source_node_id",
            "target_node_id",
            "message_id",
            "delivery_receipt_ref",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.source_node_id == self.target_node_id:
            raise AgentPruneContractError(
                "agentprune_outcome_self_edge",
                "communication outcome endpoints must differ",
            )
        object.__setattr__(self, "edge_type", _edge_type(self.edge_type))
        object.__setattr__(
            self,
            "payload_digest",
            _sha256(self.payload_digest, "payload_digest"),
        )
        object.__setattr__(self, "round_index", max(0, int(self.round_index)))
        for name in (
            "message_bytes",
            "prompt_tokens",
            "completion_tokens",
            "failure_count",
            "retry_count",
        ):
            object.__setattr__(self, name, max(0, int(getattr(self, name))))
        object.__setattr__(self, "cost_usd", max(0.0, float(self.cost_usd)))
        if self.delivered and self.message_bytes <= 0:
            raise AgentPruneContractError(
                "agentprune_delivered_bytes_missing",
                "a delivered message requires actual envelope bytes",
            )
        if (
            self.prompt_tokens > 0
            or self.completion_tokens > 0
            or self.cost_usd > 0
        ) and not str(self.usage_receipt_ref).strip():
            raise AgentPruneContractError(
                "agentprune_actual_usage_receipt_missing",
                "token or cost usage requires an actual usage receipt",
            )
        if self.verifier_result not in {"passed", "failed", "not_run"}:
            raise AgentPruneContractError(
                "agentprune_verifier_result_invalid",
                "verifier_result must be passed, failed, or not_run",
            )
        if self.permission_result not in {"allowed", "denied", "not_required"}:
            raise AgentPruneContractError(
                "agentprune_permission_result_invalid",
                "permission_result must be allowed, denied, or not_required",
            )
        for name in (
            "evidence_refs",
            "utilized_evidence_refs",
            "artifact_refs",
            "causal_refs",
        ):
            object.__setattr__(self, name, _tokens(getattr(self, name)))
        if not set(self.utilized_evidence_refs).issubset(set(self.evidence_refs)):
            raise AgentPruneContractError(
                "agentprune_utilized_evidence_unbound",
                "utilized evidence must be carried by the message",
            )
        if self.redundant_with_message_id == self.message_id:
            raise AgentPruneContractError(
                "agentprune_redundancy_self_reference",
                "a message cannot be redundant with itself",
            )
        if self.schema_version != AGENTPRUNE_OUTCOME_SCHEMA:
            raise AgentPruneContractError(
                "agentprune_outcome_schema_invalid",
                "unsupported communication outcome schema",
            )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "window_id": self.window_id,
            "completed_at": self.completed_at,
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "edge_type": self.edge_type.value,
            "round_index": self.round_index,
            "message_id": self.message_id,
            "payload_digest": self.payload_digest,
            "delivered": self.delivered,
            "delivery_receipt_ref": self.delivery_receipt_ref,
            "usage_receipt_ref": self.usage_receipt_ref,
            "message_bytes": self.message_bytes,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": _round(self.cost_usd),
            "evidence_refs": list(self.evidence_refs),
            "utilized_evidence_refs": list(self.utilized_evidence_refs),
            "artifact_refs": list(self.artifact_refs),
            "verifier_result": self.verifier_result,
            "failure_count": self.failure_count,
            "retry_count": self.retry_count,
            "redundant_with_message_id": self.redundant_with_message_id,
            "permission_result": self.permission_result,
            "malicious": self.malicious,
            "causal_refs": list(self.causal_refs),
        }


@dataclass(frozen=True, slots=True)
class EdgeContributionStats:
    edge: CommunicationEdgeCandidate
    window_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    observed_messages: int
    delivered_messages: int
    delivered_bytes: int
    delivered_tokens: int
    delivered_cost_usd: float
    evidence_refs: int
    utilized_evidence_refs: int
    artifact_contributions: int
    verifier_passes: int
    verifier_failures: int
    failed_deliveries: int
    retries: int
    duplicate_messages: int
    malicious_messages: int
    first_round: int
    last_round: int
    source_receipt_refs: tuple[str, ...]
    schema_version: str = AGENTPRUNE_STATS_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "window_ids", _tokens(self.window_ids))
        object.__setattr__(self, "observation_ids", _tokens(self.observation_ids))
        object.__setattr__(
            self,
            "source_receipt_refs",
            _tokens(self.source_receipt_refs),
        )
        for name in (
            "observed_messages",
            "delivered_messages",
            "delivered_bytes",
            "delivered_tokens",
            "evidence_refs",
            "utilized_evidence_refs",
            "artifact_contributions",
            "verifier_passes",
            "verifier_failures",
            "failed_deliveries",
            "retries",
            "duplicate_messages",
            "malicious_messages",
            "first_round",
            "last_round",
        ):
            object.__setattr__(self, name, max(0, int(getattr(self, name))))
        object.__setattr__(
            self,
            "delivered_cost_usd",
            max(0.0, float(self.delivered_cost_usd)),
        )
        if self.delivered_messages > self.observed_messages:
            raise AgentPruneContractError(
                "agentprune_stats_delivery_count_invalid",
                "delivered_messages cannot exceed observed_messages",
            )
        if self.schema_version != AGENTPRUNE_STATS_SCHEMA:
            raise AgentPruneContractError(
                "agentprune_stats_schema_invalid",
                "unsupported edge-contribution schema",
            )

    @property
    def delivery_ratio(self) -> float:
        return self.delivered_messages / max(1, self.observed_messages)

    @property
    def evidence_utilization_ratio(self) -> float:
        return self.utilized_evidence_refs / max(1, self.evidence_refs)

    @property
    def artifact_contribution_ratio(self) -> float:
        return self.artifact_contributions / max(1, self.delivered_messages)

    @property
    def verifier_pass_ratio(self) -> float:
        return self.verifier_passes / max(
            1,
            self.verifier_passes + self.verifier_failures,
        )

    @property
    def failure_ratio(self) -> float:
        return min(
            1.0,
            self.failed_deliveries / max(1, self.observed_messages),
        )

    @property
    def retry_ratio(self) -> float:
        return min(1.0, self.retries / max(1, self.observed_messages))

    @property
    def redundancy_ratio(self) -> float:
        return self.duplicate_messages / max(1, self.delivered_messages)

    @property
    def malicious_ratio(self) -> float:
        return self.malicious_messages / max(1, self.observed_messages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "edge": self.edge.to_dict(),
            "window_ids": list(self.window_ids),
            "observation_ids": list(self.observation_ids),
            "observed_messages": self.observed_messages,
            "delivered_messages": self.delivered_messages,
            "delivered_bytes": self.delivered_bytes,
            "delivered_tokens": self.delivered_tokens,
            "delivered_cost_usd": _round(self.delivered_cost_usd),
            "evidence_refs": self.evidence_refs,
            "utilized_evidence_refs": self.utilized_evidence_refs,
            "artifact_contributions": self.artifact_contributions,
            "verifier_passes": self.verifier_passes,
            "verifier_failures": self.verifier_failures,
            "failed_deliveries": self.failed_deliveries,
            "retries": self.retries,
            "duplicate_messages": self.duplicate_messages,
            "malicious_messages": self.malicious_messages,
            "first_round": self.first_round,
            "last_round": self.last_round,
            "source_receipt_refs": list(self.source_receipt_refs),
            "delivery_ratio": _round(self.delivery_ratio),
            "evidence_utilization_ratio": _round(
                self.evidence_utilization_ratio
            ),
            "artifact_contribution_ratio": _round(
                self.artifact_contribution_ratio
            ),
            "verifier_pass_ratio": _round(self.verifier_pass_ratio),
            "failure_ratio": _round(self.failure_ratio),
            "retry_ratio": _round(self.retry_ratio),
            "redundancy_ratio": _round(self.redundancy_ratio),
            "malicious_ratio": _round(self.malicious_ratio),
        }


@dataclass(frozen=True, slots=True)
class ProtectedEdgeConstraint:
    edge_id: str
    critical_path: bool = False
    unique_evidence_source: bool = False
    unresolved_obligation_refs: tuple[str, ...] = ()
    recovery_edge: bool = False
    continuity_edge: bool = False
    verifier_required: bool = False
    evidence_refs: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "edge_id", _required(self.edge_id, "edge_id"))
        object.__setattr__(
            self,
            "unresolved_obligation_refs",
            _tokens(self.unresolved_obligation_refs),
        )
        object.__setattr__(self, "evidence_refs", _tokens(self.evidence_refs))

    @property
    def hard_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.critical_path:
            reasons.append("critical_path")
        if self.unique_evidence_source:
            reasons.append("unique_evidence_source")
        if self.unresolved_obligation_refs:
            reasons.append("unresolved_obligation")
        if self.recovery_edge:
            reasons.append("recovery_edge")
        if self.continuity_edge:
            reasons.append("continuity_edge")
        if self.verifier_required:
            reasons.append("verifier_required")
        return tuple(reasons)

    @property
    def protected(self) -> bool:
        return bool(self.hard_reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "critical_path": self.critical_path,
            "unique_evidence_source": self.unique_evidence_source,
            "unresolved_obligation_refs": list(
                self.unresolved_obligation_refs
            ),
            "recovery_edge": self.recovery_edge,
            "continuity_edge": self.continuity_edge,
            "verifier_required": self.verifier_required,
            "evidence_refs": list(self.evidence_refs),
            "hard_reasons": list(self.hard_reasons),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CommunicationBudget:
    max_delivered_messages: int
    max_delivered_bytes: int
    max_delivered_tokens: int
    max_cost_usd: float

    def __post_init__(self) -> None:
        for name in (
            "max_delivered_messages",
            "max_delivered_bytes",
            "max_delivered_tokens",
        ):
            object.__setattr__(self, name, max(0, int(getattr(self, name))))
        object.__setattr__(self, "max_cost_usd", max(0.0, float(self.max_cost_usd)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_delivered_messages": self.max_delivered_messages,
            "max_delivered_bytes": self.max_delivered_bytes,
            "max_delivered_tokens": self.max_delivered_tokens,
            "max_cost_usd": _round(self.max_cost_usd),
        }


@dataclass(frozen=True, slots=True)
class PruningScoreComponents:
    delivery: float
    evidence_utilization: float
    artifact_contribution: float
    verifier: float
    redundancy_penalty: float
    failure_penalty: float
    retry_penalty: float
    malicious_penalty: float
    resource_penalty: float
    total: float

    def to_dict(self) -> dict[str, float]:
        return {
            "delivery": _round(self.delivery),
            "evidence_utilization": _round(self.evidence_utilization),
            "artifact_contribution": _round(self.artifact_contribution),
            "verifier": _round(self.verifier),
            "redundancy_penalty": _round(self.redundancy_penalty),
            "failure_penalty": _round(self.failure_penalty),
            "retry_penalty": _round(self.retry_penalty),
            "malicious_penalty": _round(self.malicious_penalty),
            "resource_penalty": _round(self.resource_penalty),
            "total": _round(self.total),
        }


@dataclass(frozen=True, slots=True)
class PruningDecision:
    edge: CommunicationEdgeCandidate
    keep: bool
    action: str
    stats: EdgeContributionStats
    score: PruningScoreComponents
    hard_constraints: tuple[str, ...]
    reasons: tuple[str, ...]
    expected_savings: FrozenDict
    verifier_risk: float
    isolation: bool = False

    def __post_init__(self) -> None:
        if self.action not in {"keep", "drop"}:
            raise AgentPruneContractError(
                "agentprune_decision_action_invalid",
                "pruning action must be keep or drop",
            )
        if self.keep != (self.action == "keep"):
            raise AgentPruneContractError(
                "agentprune_decision_keep_drift",
                "keep flag and action disagree",
            )
        object.__setattr__(self, "hard_constraints", _tokens(self.hard_constraints))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(self, "expected_savings", FrozenDict(self.expected_savings))
        object.__setattr__(
            self,
            "verifier_risk",
            max(0.0, min(1.0, float(self.verifier_risk))),
        )
        if not self.keep and self.hard_constraints:
            raise AgentPruneContractError(
                "agentprune_protected_edge_dropped",
                "an edge with hard constraints cannot be dropped",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge": self.edge.to_dict(),
            "keep": self.keep,
            "action": self.action,
            "stats": self.stats.to_dict(),
            "score": self.score.to_dict(),
            "hard_constraints": list(self.hard_constraints),
            "reasons": list(self.reasons),
            "expected_savings": dict(self.expected_savings),
            "verifier_risk": _round(self.verifier_risk),
            "isolation": self.isolation,
        }


@dataclass(frozen=True, slots=True)
class PruningMask:
    run_id: str
    task_id: str
    mechanism_version: str
    mechanism_epoch: str
    configuration_digest: str
    input_digest: str
    candidate_digest: str
    stats_digest: str
    budget: CommunicationBudget
    decisions: tuple[PruningDecision, ...]
    baseline_totals: FrozenDict
    retained_totals: FrozenDict
    schema_version: str = AGENTPRUNE_MASK_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "task_id",
            "mechanism_version",
            "mechanism_epoch",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        for name in (
            "configuration_digest",
            "input_digest",
            "candidate_digest",
            "stats_digest",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            "decisions",
            tuple(
                sorted(
                    self.decisions,
                    key=lambda item: (
                        item.edge.edge_type.value,
                        item.edge.source_node_id,
                        item.edge.target_node_id,
                        item.edge.edge_id,
                    ),
                )
            ),
        )
        edge_ids = [item.edge.edge_id for item in self.decisions]
        if len(edge_ids) != len(set(edge_ids)):
            raise AgentPruneContractError(
                "agentprune_mask_duplicate_edge",
                "a pruning mask must contain one decision per edge",
            )
        if not self.decisions:
            raise AgentPruneContractError(
                "agentprune_empty_mask",
                "a pruning mask requires at least one decision",
            )
        object.__setattr__(self, "baseline_totals", FrozenDict(self.baseline_totals))
        object.__setattr__(self, "retained_totals", FrozenDict(self.retained_totals))
        if self.schema_version != AGENTPRUNE_MASK_SCHEMA:
            raise AgentPruneContractError(
                "agentprune_mask_schema_invalid",
                "unsupported pruning mask schema",
            )

    @property
    def kept_edge_ids(self) -> tuple[str, ...]:
        return tuple(
            item.edge.edge_id for item in self.decisions if item.keep
        )

    @property
    def dropped_edge_ids(self) -> tuple[str, ...]:
        return tuple(
            item.edge.edge_id for item in self.decisions if not item.keep
        )

    @property
    def kept_spatial_edge_ids(self) -> tuple[str, ...]:
        return tuple(
            item.edge.edge_id
            for item in self.decisions
            if item.keep and item.edge.edge_type is CommunicationEdgeType.SPATIAL
        )

    @property
    def kept_temporal_edge_ids(self) -> tuple[str, ...]:
        return tuple(
            item.edge.edge_id
            for item in self.decisions
            if item.keep and item.edge.edge_type is CommunicationEdgeType.TEMPORAL
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "mechanism_version": self.mechanism_version,
            "mechanism_epoch": self.mechanism_epoch,
            "configuration_digest": self.configuration_digest,
            "input_digest": self.input_digest,
            "candidate_digest": self.candidate_digest,
            "stats_digest": self.stats_digest,
            "budget": self.budget.to_dict(),
            "decisions": [item.to_dict() for item in self.decisions],
            "baseline_totals": dict(self.baseline_totals),
            "retained_totals": dict(self.retained_totals),
            "kept_edge_ids": list(self.kept_edge_ids),
            "dropped_edge_ids": list(self.dropped_edge_ids),
            "kept_spatial_edge_ids": list(self.kept_spatial_edge_ids),
            "kept_temporal_edge_ids": list(self.kept_temporal_edge_ids),
        }


@dataclass(frozen=True, slots=True)
class RuntimeCommunicationEnvelope:
    message_id: str
    run_id: str
    task_id: str
    edge_id: str
    edge_type: CommunicationEdgeType
    round_index: int
    source_node_id: str
    target_node_id: str
    payload: str
    message_bytes: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    state_delta: FrozenDict = field(default_factory=FrozenDict)
    metadata: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        for name in (
            "message_id",
            "run_id",
            "task_id",
            "edge_id",
            "source_node_id",
            "target_node_id",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "edge_type", _edge_type(self.edge_type))
        object.__setattr__(self, "round_index", max(0, int(self.round_index)))
        object.__setattr__(self, "message_bytes", max(0, int(self.message_bytes)))
        if self.message_bytes != len(self.payload.encode("utf-8")):
            raise AgentPruneContractError(
                "agentprune_envelope_byte_count_invalid",
                "message_bytes must equal the UTF-8 payload size",
            )
        object.__setattr__(self, "prompt_tokens", max(0, int(self.prompt_tokens)))
        object.__setattr__(
            self,
            "completion_tokens",
            max(0, int(self.completion_tokens)),
        )
        object.__setattr__(self, "cost_usd", max(0.0, float(self.cost_usd)))
        object.__setattr__(self, "evidence_refs", _tokens(self.evidence_refs))
        object.__setattr__(self, "artifact_refs", _tokens(self.artifact_refs))
        object.__setattr__(self, "state_delta", FrozenDict(self.state_delta))
        object.__setattr__(self, "metadata", FrozenDict(self.metadata))

    @property
    def payload_digest(self) -> str:
        return canonical_digest(self.payload)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class DeliveryAttemptReceipt:
    message_id: str
    delivery_receipt_ref: str
    delivered: bool
    actual_bytes: int
    actual_prompt_tokens: int = 0
    actual_completion_tokens: int = 0
    actual_cost_usd: float = 0.0
    artifact_refs: tuple[str, ...] = ()
    evidence_used_refs: tuple[str, ...] = ()
    failure_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", _required(self.message_id, "message_id"))
        object.__setattr__(
            self,
            "delivery_receipt_ref",
            _required(self.delivery_receipt_ref, "delivery_receipt_ref"),
        )
        for name in (
            "actual_bytes",
            "actual_prompt_tokens",
            "actual_completion_tokens",
        ):
            object.__setattr__(self, name, max(0, int(getattr(self, name))))
        object.__setattr__(
            self,
            "actual_cost_usd",
            max(0.0, float(self.actual_cost_usd)),
        )
        object.__setattr__(self, "artifact_refs", _tokens(self.artifact_refs))
        object.__setattr__(
            self,
            "evidence_used_refs",
            _tokens(self.evidence_used_refs),
        )

    @property
    def actual_tokens(self) -> int:
        return self.actual_prompt_tokens + self.actual_completion_tokens


@dataclass(frozen=True, slots=True)
class MessageDeliveryResult:
    message_id: str
    edge_id: str
    edge_type: CommunicationEdgeType
    delivered: bool
    dropped_by_mask: bool
    actual_bytes: int
    actual_tokens: int
    actual_cost_usd: float
    delivery_receipt_ref: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "edge_id": self.edge_id,
            "edge_type": self.edge_type.value,
            "delivered": self.delivered,
            "dropped_by_mask": self.dropped_by_mask,
            "actual_bytes": self.actual_bytes,
            "actual_tokens": self.actual_tokens,
            "actual_cost_usd": _round(self.actual_cost_usd),
            "delivery_receipt_ref": self.delivery_receipt_ref,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CommunicationVerification:
    passed: bool
    verifier_ref: str
    artifact_refs: tuple[str, ...] = ()
    unresolved_obligations: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "verifier_ref",
            _required(self.verifier_ref, "verifier_ref"),
        )
        object.__setattr__(self, "artifact_refs", _tokens(self.artifact_refs))
        object.__setattr__(
            self,
            "unresolved_obligations",
            _tokens(self.unresolved_obligations),
        )
        if self.passed and self.unresolved_obligations:
            raise AgentPruneContractError(
                "agentprune_verifier_obligation_drift",
                "a passing verifier cannot retain unresolved obligations",
            )


@dataclass(frozen=True, slots=True)
class PrunedCommunicationDeliveryReceipt:
    run_id: str
    task_id: str
    mechanism_epoch: str
    mask_digest: str
    mode: str
    results: tuple[MessageDeliveryResult, ...]
    verification: CommunicationVerification
    diagnostic_only: bool
    counterfactual_claimed_as_actual: bool = False
    schema_version: str = AGENTPRUNE_DELIVERY_SCHEMA

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "mechanism_epoch", "mode"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.mask_digest:
            object.__setattr__(
                self,
                "mask_digest",
                _sha256(self.mask_digest, "mask_digest"),
            )
        object.__setattr__(
            self,
            "results",
            tuple(sorted(self.results, key=lambda item: item.message_id)),
        )
        if self.counterfactual_claimed_as_actual:
            raise AgentPruneContractError(
                "agentprune_counterfactual_actual_claim_forbidden",
                "an unexecuted counterfactual cannot be reported as actual savings",
            )
        if self.schema_version != AGENTPRUNE_DELIVERY_SCHEMA:
            raise AgentPruneContractError(
                "agentprune_delivery_schema_invalid",
                "unsupported delivery receipt schema",
            )

    @property
    def delivered_messages(self) -> int:
        return sum(item.delivered for item in self.results)

    @property
    def delivered_bytes(self) -> int:
        return sum(item.actual_bytes for item in self.results)

    @property
    def delivered_tokens(self) -> int:
        return sum(item.actual_tokens for item in self.results)

    @property
    def delivered_cost_usd(self) -> float:
        return sum(item.actual_cost_usd for item in self.results)

    @property
    def dropped_messages(self) -> int:
        return sum(item.dropped_by_mask for item in self.results)

    @property
    def efficiency_gain_valid(self) -> bool:
        return (
            self.verification.passed
            and self.mode in {"validation", "default"}
            and not self.diagnostic_only
            and not self.counterfactual_claimed_as_actual
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "mechanism_epoch": self.mechanism_epoch,
            "mask_digest": self.mask_digest,
            "mode": self.mode,
            "results": [item.to_dict() for item in self.results],
            "verification": {
                "passed": self.verification.passed,
                "verifier_ref": self.verification.verifier_ref,
                "artifact_refs": list(self.verification.artifact_refs),
                "unresolved_obligations": list(
                    self.verification.unresolved_obligations
                ),
                "reason": self.verification.reason,
            },
            "diagnostic_only": self.diagnostic_only,
            "counterfactual_claimed_as_actual": (
                self.counterfactual_claimed_as_actual
            ),
            "delivered_messages": self.delivered_messages,
            "delivered_bytes": self.delivered_bytes,
            "delivered_tokens": self.delivered_tokens,
            "delivered_cost_usd": _round(self.delivered_cost_usd),
            "dropped_messages": self.dropped_messages,
            "efficiency_gain_valid": self.efficiency_gain_valid,
        }


def candidate_digest(
    candidates: Sequence[CommunicationEdgeCandidate],
) -> str:
    return canonical_digest(
        [
            item.to_dict()
            for item in sorted(candidates, key=lambda value: value.edge_id)
        ]
    )


def stats_digest(stats: Sequence[EdgeContributionStats]) -> str:
    return canonical_digest(
        [item.to_dict() for item in sorted(stats, key=lambda value: value.edge.edge_id)]
    )


def totals(stats: Sequence[EdgeContributionStats]) -> FrozenDict:
    return FrozenDict(
        {
            "messages": sum(item.delivered_messages for item in stats),
            "bytes": sum(item.delivered_bytes for item in stats),
            "tokens": sum(item.delivered_tokens for item in stats),
            "cost_usd": _round(
                sum(item.delivered_cost_usd for item in stats)
            ),
        }
    )


def over_budget(
    value: Mapping[str, Any],
    budget: CommunicationBudget,
) -> bool:
    return (
        int(value.get("messages") or 0) > budget.max_delivered_messages
        or int(value.get("bytes") or 0) > budget.max_delivered_bytes
        or int(value.get("tokens") or 0) > budget.max_delivered_tokens
        or float(value.get("cost_usd") or 0) > budget.max_cost_usd + 1e-12
    )


__all__ = [
    "AGENTPRUNE_DELIVERY_SCHEMA",
    "AGENTPRUNE_MASK_SCHEMA",
    "AGENTPRUNE_OUTCOME_SCHEMA",
    "AGENTPRUNE_STATS_SCHEMA",
    "AgentPruneContractError",
    "CommunicationBudget",
    "CommunicationEdgeCandidate",
    "CommunicationEdgeType",
    "CommunicationOutcomeObservation",
    "CommunicationVerification",
    "DeliveryAttemptReceipt",
    "EdgeContributionStats",
    "MessageDeliveryResult",
    "ProtectedEdgeConstraint",
    "PrunedCommunicationDeliveryReceipt",
    "PruningDecision",
    "PruningMask",
    "PruningScoreComponents",
    "RuntimeCommunicationEnvelope",
    "candidate_digest",
    "over_budget",
    "stats_digest",
    "totals",
]
