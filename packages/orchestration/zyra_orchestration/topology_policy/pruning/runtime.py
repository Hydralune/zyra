from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType

from ...graph_custody import GraphStateSnapshot
from ..condition import CARDTopologyRuntimeResult
from ..contracts import PolicyInputSnapshot, TopologyProposalArtifact, canonical_digest
from ..evidence import PolicyEvidencePublisher, PublishedPolicyEvidence
from .contracts import (
    AGENTPRUNE_DELIVERY_SCHEMA,
    AGENTPRUNE_MASK_SCHEMA,
    AGENTPRUNE_OUTCOME_SCHEMA,
    AGENTPRUNE_STATS_SCHEMA,
    CommunicationBudget,
    CommunicationEdgeCandidate,
    CommunicationEdgeType,
    CommunicationOutcomeObservation,
    CommunicationVerification,
    DeliveryAttemptReceipt,
    MessageDeliveryResult,
    ProtectedEdgeConstraint,
    PrunedCommunicationDeliveryReceipt,
    PruningMask,
    RuntimeCommunicationEnvelope,
)
from .optimizer import (
    AgentPruneOptimizerConfig,
    DeterministicCommunicationOptimizer,
)
from .outcome_stats import CommunicationOutcomeAggregator


READINESS_REPORT_SCHEMA = "zyra.mechanism-evidence-readiness-report/v1"
ALLOWED_STAGES = {
    "input_precheck",
    "implementation_validated",
    "activation_ready",
}
ALLOWED_STATUSES = {
    "deterministic_ready",
    "evidence_only",
    "unavailable",
}


EventSink = Callable[[EventRecord], None]
MessageDeliverer = Callable[
    [RuntimeCommunicationEnvelope],
    DeliveryAttemptReceipt,
]
DeliveryVerifier = Callable[
    [tuple[DeliveryAttemptReceipt, ...]],
    CommunicationVerification,
]


@dataclass(frozen=True, slots=True)
class AgentPruneReadinessResolution:
    stage: str
    status: str
    mode: str
    report_digest: str
    canonical_mutation_allowed: bool
    delivery_mask_allowed: bool
    fallback_profile: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_id": "agentprune",
            "stage": self.stage,
            "status": self.status,
            "mode": self.mode,
            "report_digest": self.report_digest,
            "canonical_mutation_allowed": self.canonical_mutation_allowed,
            "delivery_mask_allowed": self.delivery_mask_allowed,
            "fallback_profile": self.fallback_profile,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AgentPruneRuntimeResult:
    run_id: str
    task_id: str
    mode: str
    readiness: AgentPruneReadinessResolution
    mechanism_epoch: str
    upstream_proposal_id: str
    upstream_proposal_digest: str
    mask: PruningMask | None
    pruning_proposal: TopologyProposalArtifact | None
    published_evidence: PublishedPolicyEvidence | None
    input_spatial_edge_ids: tuple[str, ...]
    input_temporal_edge_ids: tuple[str, ...]
    effective_spatial_edge_ids: tuple[str, ...]
    effective_temporal_edge_ids: tuple[str, ...]
    pruning_effective: bool
    composer_pruning_eligible: bool
    commit_input_proposal_digest: str
    graph_revision_before: int
    graph_revision_after: int
    degraded: bool
    degraded_reason: str
    events: tuple[EventRecord, ...]

    @property
    def canonical_graph_unchanged(self) -> bool:
        return self.graph_revision_before == self.graph_revision_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "mode": self.mode,
            "readiness": self.readiness.to_dict(),
            "mechanism_epoch": self.mechanism_epoch,
            "upstream_proposal_id": self.upstream_proposal_id,
            "upstream_proposal_digest": self.upstream_proposal_digest,
            "mask": self.mask.to_dict() if self.mask is not None else None,
            "mask_digest": self.mask.digest if self.mask is not None else "",
            "pruning_proposal": (
                self.pruning_proposal.to_dict()
                if self.pruning_proposal is not None
                else None
            ),
            "published_artifact_id": (
                self.published_evidence.artifact.artifact_id
                if self.published_evidence is not None
                else ""
            ),
            "input_spatial_edge_ids": list(self.input_spatial_edge_ids),
            "input_temporal_edge_ids": list(self.input_temporal_edge_ids),
            "effective_spatial_edge_ids": list(
                self.effective_spatial_edge_ids
            ),
            "effective_temporal_edge_ids": list(
                self.effective_temporal_edge_ids
            ),
            "pruning_effective": self.pruning_effective,
            "composer_pruning_eligible": self.composer_pruning_eligible,
            "commit_input_proposal_digest": self.commit_input_proposal_digest,
            "graph_revision_before": self.graph_revision_before,
            "graph_revision_after": self.graph_revision_after,
            "canonical_graph_unchanged": self.canonical_graph_unchanged,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "events": [
                {
                    "run_id": item.run_id,
                    "task_id": item.task_id,
                    "event_id": item.event_id,
                    "event_type": item.event_type.value,
                    "node_id": item.node_id,
                    "created_at": item.created_at,
                    "payload": dict(item.payload),
                }
                for item in self.events
            ],
        }


class AgentPruneRuntime:
    """Readiness-bound prune/delivery policy; graph and event stores stay external."""

    def __init__(
        self,
        *,
        config: AgentPruneOptimizerConfig | None,
        readiness: AgentPruneReadinessResolution,
        evidence_publisher: PolicyEvidencePublisher | None = None,
        admit_event: EventSink | None = None,
        disconnected_reason: str = "",
    ) -> None:
        self.config = config
        self.readiness = readiness
        self.evidence_publisher = evidence_publisher
        self.admit_event = admit_event or (lambda event: None)
        self.disconnected_reason = disconnected_reason
        self.aggregator = CommunicationOutcomeAggregator()
        self.optimizer = (
            DeterministicCommunicationOptimizer(config)
            if config is not None
            else None
        )
        self._frozen_masks: dict[tuple[str, str], PruningMask] = {}

    @classmethod
    def from_repository(
        cls,
        repository_root: Path,
        *,
        config_path: Path | None = None,
        report_path: Path | None = None,
        evidence_publisher: PolicyEvidencePublisher | None = None,
        admit_event: EventSink | None = None,
    ) -> AgentPruneRuntime:
        root = repository_root.resolve()
        selected_config = (
            config_path.resolve()
            if config_path is not None
            else root
            / "config"
            / "phase2"
            / "agentprune-pruning.json"
        )
        try:
            config = AgentPruneOptimizerConfig.load(selected_config)
        except Exception as exc:  # noqa: BLE001 - explicit targeted baseline.
            reason = getattr(
                exc,
                "code",
                f"agentprune_config:{type(exc).__name__}",
            )
            return cls(
                config=None,
                readiness=cls._unavailable(
                    fallback_profile="phase1_targeted_communication_baseline",
                    reason=str(reason),
                ),
                evidence_publisher=evidence_publisher,
                admit_event=admit_event,
                disconnected_reason=str(reason),
            )
        selected_report = (
            report_path.resolve()
            if report_path is not None
            else root
            / "docs"
            / "reviews"
            / "evidence"
            / "P2-S03-03"
            / "MechanismEvidenceReadinessReport.json"
        )
        try:
            readiness = cls._load_readiness(
                root,
                config=config,
                report_path=selected_report,
            )
            disconnected = ""
        except Exception as exc:  # noqa: BLE001 - corrupt/unknown unavailable.
            disconnected = getattr(
                exc,
                "code",
                f"agentprune_readiness:{type(exc).__name__}",
            )
            readiness = cls._unavailable(
                fallback_profile=config.fallback_profile,
                reason=str(disconnected),
            )
        return cls(
            config=config,
            readiness=readiness,
            evidence_publisher=evidence_publisher,
            admit_event=admit_event,
            disconnected_reason=str(disconnected),
        )

    def execute(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        card_result: CARDTopologyRuntimeResult,
        upstream_proposal: TopologyProposalArtifact,
        current_graph: GraphStateSnapshot,
        observations: Iterable[CommunicationOutcomeObservation],
        protections: Iterable[ProtectedEdgeConstraint],
        budget: CommunicationBudget,
        mechanism_epoch: str,
        prior_mask: PruningMask | None = None,
        enabled: bool = True,
        publish: bool = True,
    ) -> AgentPruneRuntimeResult:
        events: list[EventRecord] = []
        input_spatial = tuple(card_result.effective_spatial_edge_ids)
        input_temporal = tuple(card_result.effective_temporal_edge_ids)
        if not enabled:
            return self._baseline_result(
                policy_input=policy_input,
                upstream_proposal=upstream_proposal,
                current_graph=current_graph,
                mechanism_epoch=mechanism_epoch,
                input_spatial=input_spatial,
                input_temporal=input_temporal,
                reason="agentprune_disabled",
                events=events,
            )
        if (
            self.config is None
            or self.optimizer is None
            or self.readiness.mode == "baseline"
        ):
            return self._baseline_result(
                policy_input=policy_input,
                upstream_proposal=upstream_proposal,
                current_graph=current_graph,
                mechanism_epoch=mechanism_epoch,
                input_spatial=input_spatial,
                input_temporal=input_temporal,
                reason=(
                    self.disconnected_reason
                    or self.readiness.reason
                    or "agentprune_unavailable"
                ),
                events=events,
            )
        try:
            self._validate_graph(
                policy_input=policy_input,
                current_graph=current_graph,
            )
            self._validate_input_readiness(policy_input)
            self._validate_card_result(
                policy_input=policy_input,
                card_result=card_result,
                upstream_proposal=upstream_proposal,
            )
            candidates = self._candidates(
                card_result=card_result,
                upstream_proposal=upstream_proposal,
            )
            stats = self.aggregator.aggregate(
                candidates=candidates,
                observations=observations,
                run_id=policy_input.run_id,
                task_id=policy_input.task_id,
                completed_before=policy_input.header.created_at,
                maximum_age_seconds=(
                    self.config.maximum_outcome_age_seconds
                ),
            )
            epoch_key = (policy_input.run_id, mechanism_epoch)
            frozen_prior = (
                prior_mask
                if prior_mask is not None
                else self._frozen_masks.get(epoch_key)
            )
            mask = self.optimizer.optimize(
                policy_input=policy_input,
                candidates=candidates,
                stats=stats,
                protections=protections,
                budget=budget,
                mechanism_epoch=mechanism_epoch,
                prior_mask=frozen_prior,
            )
            self._frozen_masks[epoch_key] = mask
            proposal = self.optimizer.build_proposal(
                policy_input=policy_input,
                upstream_proposal=upstream_proposal,
                mask=mask,
                readiness_stage=self.readiness.stage,
                readiness_status=self.readiness.status,
                readiness_report_digest=self.readiness.report_digest,
            )
            if proposal is not None:
                self.validate_proposal(
                    policy_input=policy_input,
                    upstream_proposal=upstream_proposal,
                    mask=mask,
                    proposal=proposal,
                )
        except Exception as exc:  # noqa: BLE001 - fail to targeted baseline.
            reason = getattr(
                exc,
                "code",
                f"agentprune_exception:{type(exc).__name__}",
            )
            return self._baseline_result(
                policy_input=policy_input,
                upstream_proposal=upstream_proposal,
                current_graph=current_graph,
                mechanism_epoch=mechanism_epoch,
                input_spatial=input_spatial,
                input_temporal=input_temporal,
                reason=str(reason),
                events=events,
            )

        published = None
        if (
            publish
            and proposal is not None
            and self.evidence_publisher is not None
        ):
            published = self.evidence_publisher.publish(
                proposal,
                run_id=policy_input.run_id,
                task_id=policy_input.task_id,
            )
        effective = self.readiness.mode in {"validation", "default"}
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_agentprune_" + canonical_digest(
                (
                    policy_input.digest,
                    upstream_proposal.digest,
                    mask.digest,
                    self.readiness.mode,
                    current_graph.revision,
                )
            )[:24],
            event_type=EventType.RESOURCE_DECISION,
            payload={
                "schema": "zyra.agentprune-runtime-result/v1",
                "mechanism_id": "agentprune",
                "mode": self.readiness.mode,
                "readiness_stage": self.readiness.stage,
                "readiness_status": self.readiness.status,
                "readiness_report_digest": self.readiness.report_digest,
                "mechanism_epoch": mechanism_epoch,
                "mask_digest": mask.digest,
                "upstream_proposal_id": upstream_proposal.proposal_id,
                "upstream_proposal_digest": upstream_proposal.digest,
                "pruning_proposal_id": (
                    proposal.proposal_id if proposal is not None else ""
                ),
                "pruning_proposal_digest": (
                    proposal.digest if proposal is not None else ""
                ),
                "input_spatial_edge_count": len(input_spatial),
                "input_temporal_edge_count": len(input_temporal),
                "kept_spatial_edge_count": len(mask.kept_spatial_edge_ids),
                "kept_temporal_edge_count": len(mask.kept_temporal_edge_ids),
                "dropped_edge_count": len(mask.dropped_edge_ids),
                "pruning_effective": effective,
                "composer_pruning_eligible": effective,
                "expected_savings_are_counterfactual": True,
                "actual_savings_require_paired_delivery_receipts": True,
                "canonical_mutation_attempted": False,
                "canonical_graph_revision_before": current_graph.revision,
                "canonical_graph_revision_after": current_graph.revision,
                "fallback_profile": self.config.fallback_profile,
            },
        )
        self.admit_event(event)
        events.append(event)
        return AgentPruneRuntimeResult(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            mode=self.readiness.mode,
            readiness=self.readiness,
            mechanism_epoch=mechanism_epoch,
            upstream_proposal_id=upstream_proposal.proposal_id,
            upstream_proposal_digest=upstream_proposal.digest,
            mask=mask,
            pruning_proposal=proposal,
            published_evidence=published,
            input_spatial_edge_ids=input_spatial,
            input_temporal_edge_ids=input_temporal,
            effective_spatial_edge_ids=(
                mask.kept_spatial_edge_ids if effective else input_spatial
            ),
            effective_temporal_edge_ids=(
                mask.kept_temporal_edge_ids if effective else input_temporal
            ),
            pruning_effective=effective,
            composer_pruning_eligible=effective,
            commit_input_proposal_digest=upstream_proposal.digest,
            graph_revision_before=current_graph.revision,
            graph_revision_after=current_graph.revision,
            degraded=False,
            degraded_reason="",
            events=tuple(events),
        )

    def deliver(
        self,
        *,
        runtime_result: AgentPruneRuntimeResult,
        envelopes: Iterable[RuntimeCommunicationEnvelope],
        deliver_message: MessageDeliverer,
        verify_delivery: DeliveryVerifier,
    ) -> PrunedCommunicationDeliveryReceipt:
        envelope_values = tuple(envelopes)
        decision_by_edge = (
            {
                item.edge.edge_id: item
                for item in runtime_result.mask.decisions
            }
            if runtime_result.mask is not None
            else {}
        )
        attempts: list[DeliveryAttemptReceipt] = []
        results: list[MessageDeliveryResult] = []
        message_ids: set[str] = set()
        for envelope in envelope_values:
            if (
                envelope.run_id != runtime_result.run_id
                or envelope.task_id != runtime_result.task_id
            ):
                raise ValueError(
                    "AgentPrune delivery envelope belongs to another run/task"
                )
            if envelope.message_id in message_ids:
                raise ValueError(
                    "AgentPrune delivery envelope identifiers must be unique"
                )
            message_ids.add(envelope.message_id)
            decision = decision_by_edge.get(envelope.edge_id)
            if runtime_result.mask is not None and decision is None:
                raise ValueError(
                    "AgentPrune delivery edge is absent from the frozen mask"
                )
            if decision is not None and (
                decision.edge.edge_type is not envelope.edge_type
                or decision.edge.source_node_id != envelope.source_node_id
                or decision.edge.target_node_id != envelope.target_node_id
            ):
                raise ValueError(
                    "AgentPrune delivery envelope differs from the frozen mask"
                )
            dropped = bool(
                runtime_result.pruning_effective
                and decision is not None
                and not decision.keep
            )
            if dropped:
                results.append(
                    MessageDeliveryResult(
                        message_id=envelope.message_id,
                        edge_id=envelope.edge_id,
                        edge_type=envelope.edge_type,
                        delivered=False,
                        dropped_by_mask=True,
                        actual_bytes=0,
                        actual_tokens=0,
                        actual_cost_usd=0,
                        delivery_receipt_ref="",
                        reason=(
                            "frozen AgentPrune mask drop; avoided usage remains "
                            "counterfactual until compared with an executed baseline"
                        ),
                    )
                )
                continue
            attempt = deliver_message(envelope)
            if attempt.message_id != envelope.message_id:
                raise ValueError(
                    "delivery receipt belongs to another message"
                )
            attempts.append(attempt)
            results.append(
                MessageDeliveryResult(
                    message_id=envelope.message_id,
                    edge_id=envelope.edge_id,
                    edge_type=envelope.edge_type,
                    delivered=attempt.delivered,
                    dropped_by_mask=False,
                    actual_bytes=attempt.actual_bytes,
                    actual_tokens=attempt.actual_tokens,
                    actual_cost_usd=attempt.actual_cost_usd,
                    delivery_receipt_ref=attempt.delivery_receipt_ref,
                    reason=(
                        "delivered through the existing communication owner"
                        if attempt.delivered
                        else attempt.failure_reason
                    ),
                )
            )
        verification = verify_delivery(tuple(attempts))
        receipt = PrunedCommunicationDeliveryReceipt(
            run_id=runtime_result.run_id,
            task_id=runtime_result.task_id,
            mechanism_epoch=runtime_result.mechanism_epoch,
            mask_digest=(
                runtime_result.mask.digest
                if runtime_result.mask is not None
                else ""
            ),
            mode=runtime_result.mode,
            results=tuple(results),
            verification=verification,
            diagnostic_only=runtime_result.mode == "diagnostic",
        )
        event = EventRecord(
            run_id=receipt.run_id,
            task_id=receipt.task_id,
            event_id="event_agentprune_delivery_" + receipt.digest[:24],
            event_type=EventType.RESOURCE_DECISION,
            payload={
                "schema": AGENTPRUNE_DELIVERY_SCHEMA,
                "mechanism_id": "agentprune",
                "mode": receipt.mode,
                "mechanism_epoch": receipt.mechanism_epoch,
                "mask_digest": receipt.mask_digest,
                "delivery_receipt_digest": receipt.digest,
                "delivered_messages": receipt.delivered_messages,
                "delivered_bytes": receipt.delivered_bytes,
                "delivered_tokens": receipt.delivered_tokens,
                "delivered_cost_usd": receipt.delivered_cost_usd,
                "dropped_messages": receipt.dropped_messages,
                "verifier_passed": receipt.verification.passed,
                "verifier_ref": receipt.verification.verifier_ref,
                "efficiency_gain_valid": receipt.efficiency_gain_valid,
                "counterfactual_claimed_as_actual": False,
            },
        )
        self.admit_event(event)
        return receipt

    @staticmethod
    def validate_proposal(
        *,
        policy_input: PolicyInputSnapshot,
        upstream_proposal: TopologyProposalArtifact,
        mask: PruningMask,
        proposal: TopologyProposalArtifact,
    ) -> None:
        if proposal.header.mechanism_id != "agentprune":
            raise ValueError(
                "AgentPrune runtime rejects another mechanism proposal"
            )
        if proposal.input_snapshot_digest != policy_input.digest:
            raise ValueError(
                "AgentPrune proposal belongs to another policy input"
            )
        if proposal.header.causation_id != upstream_proposal.proposal_id:
            raise ValueError(
                "AgentPrune proposal is not caused by the CARD/ARG input"
            )
        if (
            proposal.expected_outcome.get("upstream_proposal_digest")
            != upstream_proposal.digest
            or proposal.expected_outcome.get("mask_digest") != mask.digest
        ):
            raise ValueError(
                "AgentPrune proposal upstream or mask digest is stale"
            )
        if any(
            operation.kind.value != "remove_edge"
            for operation in proposal.operations
        ):
            raise ValueError(
                "AgentPrune may propose edge removal only"
            )

    def _validate_input_readiness(
        self,
        policy_input: PolicyInputSnapshot,
    ) -> None:
        matches = tuple(
            item
            for item in policy_input.readiness_refs
            if item.header.mechanism_id == "agentprune"
        )
        if len(matches) != 1:
            raise ValueError("agentprune_input_readiness_ref_missing")
        selected = matches[0]
        if (
            selected.report_digest != self.readiness.report_digest
            or selected.readiness_stage != self.readiness.stage
            or selected.status != self.readiness.status
        ):
            raise ValueError("agentprune_input_readiness_ref_drift")

    @staticmethod
    def _validate_graph(
        *,
        policy_input: PolicyInputSnapshot,
        current_graph: GraphStateSnapshot,
    ) -> None:
        if (
            policy_input.graph.graph_id != current_graph.graph_id
            or policy_input.graph.revision != current_graph.revision
            or policy_input.graph.signature != current_graph.signature
        ):
            raise ValueError("agentprune_current_graph_binding_drift")

    @staticmethod
    def _validate_card_result(
        *,
        policy_input: PolicyInputSnapshot,
        card_result: CARDTopologyRuntimeResult,
        upstream_proposal: TopologyProposalArtifact,
    ) -> None:
        if not card_result.canonical_graph_unchanged:
            raise ValueError("agentprune_card_mutated_canonical_graph")
        if not card_result.composer_residual_eligible:
            raise ValueError("agentprune_card_residual_not_eligible")
        if card_result.correction is None:
            raise ValueError("agentprune_card_correction_missing")
        if (
            upstream_proposal.input_snapshot_digest
            != policy_input.digest
        ):
            raise ValueError("agentprune_upstream_policy_input_drift")
        if card_result.correction_proposal is not None and (
            upstream_proposal.digest
            != card_result.correction_proposal.digest
        ):
            raise ValueError("agentprune_upstream_card_proposal_drift")

    @staticmethod
    def _candidates(
        *,
        card_result: CARDTopologyRuntimeResult,
        upstream_proposal: TopologyProposalArtifact,
    ) -> tuple[CommunicationEdgeCandidate, ...]:
        correction = card_result.correction
        if correction is None:
            raise ValueError("agentprune_card_correction_missing")
        effective_spatial = set(card_result.effective_spatial_edge_ids)
        effective_temporal = set(card_result.effective_temporal_edge_ids)
        candidates = []
        for decision in correction.decisions:
            effective = (
                decision.edge_id in effective_temporal
                if decision.edge_type == "temporal"
                else decision.edge_id in effective_spatial
            )
            if not effective:
                continue
            candidates.append(
                CommunicationEdgeCandidate(
                    edge_id=decision.edge_id,
                    source_node_id=decision.source_node_id,
                    target_node_id=decision.target_node_id,
                    edge_type=CommunicationEdgeType(decision.edge_type),
                    relation=decision.relation,
                    required_capabilities=decision.required_capabilities,
                    source_proposal_ref=upstream_proposal.proposal_id,
                    metadata={
                        "card_action": decision.action,
                        "card_score": decision.score.total,
                        "card_confidence": decision.confidence,
                        "card_correction_digest": correction.digest,
                    },
                )
            )
        return tuple(candidates)

    def _baseline_result(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        upstream_proposal: TopologyProposalArtifact,
        current_graph: GraphStateSnapshot,
        mechanism_epoch: str,
        input_spatial: tuple[str, ...],
        input_temporal: tuple[str, ...],
        reason: str,
        events: list[EventRecord],
    ) -> AgentPruneRuntimeResult:
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_agentprune_degraded_" + canonical_digest(
                (
                    policy_input.digest,
                    upstream_proposal.digest,
                    current_graph.signature,
                    mechanism_epoch,
                    reason,
                )
            )[:24],
            event_type=EventType.RECOVERY_PLANNED,
            payload={
                "schema": "zyra.agentprune-runtime-degraded/v1",
                "mechanism_id": "agentprune",
                "reason": reason,
                "fallback_profile": self.readiness.fallback_profile,
                "readiness_stage": self.readiness.stage,
                "readiness_status": self.readiness.status,
                "readiness_report_digest": self.readiness.report_digest,
                "mechanism_epoch": mechanism_epoch,
                "upstream_proposal_id": upstream_proposal.proposal_id,
                "upstream_proposal_digest": upstream_proposal.digest,
                "canonical_mutation_attempted": False,
                "graph_revision": current_graph.revision,
            },
        )
        self.admit_event(event)
        events.append(event)
        return AgentPruneRuntimeResult(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            mode="baseline",
            readiness=self.readiness,
            mechanism_epoch=mechanism_epoch,
            upstream_proposal_id=upstream_proposal.proposal_id,
            upstream_proposal_digest=upstream_proposal.digest,
            mask=None,
            pruning_proposal=None,
            published_evidence=None,
            input_spatial_edge_ids=input_spatial,
            input_temporal_edge_ids=input_temporal,
            effective_spatial_edge_ids=input_spatial,
            effective_temporal_edge_ids=input_temporal,
            pruning_effective=False,
            composer_pruning_eligible=False,
            commit_input_proposal_digest=upstream_proposal.digest,
            graph_revision_before=current_graph.revision,
            graph_revision_after=current_graph.revision,
            degraded=True,
            degraded_reason=reason,
            events=tuple(events),
        )

    @classmethod
    def _load_readiness(
        cls,
        repository_root: Path,
        *,
        config: AgentPruneOptimizerConfig,
        report_path: Path,
    ) -> AgentPruneReadinessResolution:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                "agentprune_readiness_report_missing_or_corrupt"
            ) from exc
        if not isinstance(report, Mapping):
            raise ValueError("agentprune_readiness_report_invalid")
        supplied_digest = str(report.get("report_digest") or "")
        unsigned = dict(report)
        unsigned.pop("report_digest", None)
        if (
            len(supplied_digest) != 64
            or canonical_digest(unsigned) != supplied_digest
        ):
            raise ValueError("agentprune_readiness_report_digest_mismatch")
        if report.get("schema") != READINESS_REPORT_SCHEMA:
            raise ValueError("agentprune_readiness_report_schema_unknown")
        stage = str(report.get("readiness_stage") or "")
        if stage not in ALLOWED_STAGES:
            raise ValueError("agentprune_readiness_stage_unknown")
        readiness_path = (
            repository_root / "config" / "phase2" / "mechanism-readiness.json"
        )
        readiness_config = json.loads(
            readiness_path.read_text(encoding="utf-8")
        )
        if (
            report.get("p2_base_commit")
            != readiness_config.get("p2_base_commit")
            or (
                report.get("baseline_manifest") or {}
            ).get("manifest_digest")
            != readiness_config.get("baseline_manifest_digest")
            or (report.get("contract") or {}).get("sha256")
            != _file_digest(readiness_path)
        ):
            raise ValueError(
                "agentprune_readiness_frozen_binding_mismatch"
            )
        no_training = report.get("no_policy_training_audit")
        if (
            not isinstance(no_training, Mapping)
            or no_training.get("passed") is not True
        ):
            raise ValueError(
                "agentprune_readiness_no_training_audit_failed"
            )
        mechanisms = report.get("mechanisms")
        if not isinstance(mechanisms, Mapping):
            raise ValueError("agentprune_readiness_mechanisms_missing")
        agentprune = mechanisms.get("agentprune")
        if not isinstance(agentprune, Mapping):
            raise ValueError("agentprune_readiness_verdict_missing")
        status = str(agentprune.get("status") or "")
        if status not in ALLOWED_STATUSES:
            raise ValueError("agentprune_readiness_status_unknown")
        if (
            agentprune.get("readiness_stage") != stage
            or (
                report.get("mechanism_statuses") or {}
            ).get("agentprune")
            != status
        ):
            raise ValueError("agentprune_readiness_stage_or_status_drift")
        if stage == "input_precheck":
            if supplied_digest != config.input_precheck_report_digest:
                raise ValueError(
                    "agentprune_input_precheck_digest_mismatch"
                )
        else:
            if (
                report.get("supersedes_report_digest")
                != config.input_precheck_report_digest
            ):
                raise ValueError("agentprune_readiness_lineage_mismatch")
            validation = agentprune.get("implementation_validation")
            if status == "deterministic_ready" and (
                not isinstance(validation, Mapping)
                or validation.get("passed") is not True
                or validation.get("mechanism_version")
                != config.mechanism_version
                or validation.get("configuration_digest") != config.digest
                or validation.get("outcome_schema_version")
                != AGENTPRUNE_OUTCOME_SCHEMA
                or validation.get("stats_schema_version")
                != AGENTPRUNE_STATS_SCHEMA
                or validation.get("mask_schema_version")
                != AGENTPRUNE_MASK_SCHEMA
                or validation.get("canonical_mutation_attempted") is not False
                or validation.get("training_sample_count") != 0
            ):
                raise ValueError(
                    "agentprune_implementation_validation_incomplete"
                )
        if status == "unavailable":
            return cls._unavailable(
                fallback_profile=config.fallback_profile,
                reason="readiness status is unavailable",
                stage=stage,
                report_digest=supplied_digest,
            )
        if status == "evidence_only":
            return AgentPruneReadinessResolution(
                stage=stage,
                status=status,
                mode="diagnostic",
                report_digest=supplied_digest,
                canonical_mutation_allowed=False,
                delivery_mask_allowed=False,
                fallback_profile=config.fallback_profile,
                reason=(
                    "evidence_only records would-prune diagnostics while "
                    "the targeted communication baseline delivers all edges"
                ),
            )
        if stage == "activation_ready":
            return AgentPruneReadinessResolution(
                stage=stage,
                status=status,
                mode="default",
                report_digest=supplied_digest,
                canonical_mutation_allowed=False,
                delivery_mask_allowed=True,
                fallback_profile=config.fallback_profile,
                reason=(
                    "activation_ready mask may enter the symbolic composer "
                    "and communication delivery gate, while only custody may "
                    "commit graph state"
                ),
            )
        return AgentPruneReadinessResolution(
            stage=stage,
            status=status,
            mode="validation",
            report_digest=supplied_digest,
            canonical_mutation_allowed=False,
            delivery_mask_allowed=True,
            fallback_profile=config.fallback_profile,
            reason=(
                "deterministic_ready AgentPrune runs in isolated validation "
                "until the parent composer reaches activation_ready"
            ),
        )

    @staticmethod
    def _unavailable(
        *,
        fallback_profile: str,
        reason: str,
        stage: str = "input_precheck",
        report_digest: str = "",
    ) -> AgentPruneReadinessResolution:
        return AgentPruneReadinessResolution(
            stage=stage,
            status="unavailable",
            mode="baseline",
            report_digest=report_digest,
            canonical_mutation_allowed=False,
            delivery_mask_allowed=False,
            fallback_profile=fallback_profile,
            reason=reason,
        )


def _file_digest(path: Path) -> str:
    payload = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "AgentPruneReadinessResolution",
    "AgentPruneRuntime",
    "AgentPruneRuntimeResult",
]
