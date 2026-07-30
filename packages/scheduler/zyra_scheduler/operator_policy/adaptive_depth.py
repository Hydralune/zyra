from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from zyra_core import EventRecord, EventType
from zyra_orchestration.topology_policy.contracts import (
    ContractHeader,
    FrozenDict,
    PolicyOutcome,
    StableArtifactRef,
    canonical_digest,
    thaw_json,
)

from .early_exit import (
    DeterministicEarlyExitGate,
    EarlyExitCheckpointBinding,
    EarlyExitError,
    ExitDecision,
    ExitDecisionReceipt,
    ExitEligibilitySnapshot,
    minimum_operator_path,
)
from .selector import (
    OperatorCandidate,
    OperatorLayerProposal,
    OperatorSelectionProposal,
)


ADAPTIVE_DEPTH_COST_SCHEMA = "zyra.adaptive-depth-cost-receipt/v1"
ADAPTIVE_DEPTH_RESULT_SCHEMA = "zyra.adaptive-depth-result/v1"
LAYER_EXECUTION_SCHEMA = "zyra.operator-layer-execution-receipt/v1"


EventSink = Callable[[EventRecord], None]


class OperatorLayerExecutionPort(Protocol):
    """Existing execution owner used by adaptive depth.

    Implementations retain placement, permission, lease, tool/provider, artifact
    and task-state ownership. Adaptive depth only chooses whether another
    proposal layer should be requested after the returned receipt is admitted.
    """

    def execute_layer(
        self,
        *,
        proposal: OperatorSelectionProposal,
        layer: OperatorLayerProposal,
    ) -> "LayerExecutionReceipt": ...


class EligibilityCapturePort(Protocol):
    """Rebuilds eligibility from canonical owners after each completed layer."""

    def capture(
        self,
        *,
        remaining_candidates: Sequence[OperatorCandidate],
        minimum_operator_refs: Sequence[str],
        minimum_verification_refs: Sequence[str],
    ) -> ExitEligibilitySnapshot: ...


@dataclass(frozen=True, slots=True)
class LayerExecutionReceipt:
    layer_index: int
    operator_refs: tuple[str, ...]
    owner_receipt_ref: str
    decision_refs: tuple[str, ...]
    artifact_refs: tuple[StableArtifactRef, ...]
    verification_refs: tuple[str, ...]
    actual_tokens: int
    actual_cost_usd: float
    actual_latency_ms: int
    completed: bool = True
    schema_version: str = LAYER_EXECUTION_SCHEMA

    def __post_init__(self) -> None:
        if self.layer_index < 1:
            raise EarlyExitError(
                "adaptive_depth_layer_invalid",
                "layer index must be positive",
            )
        for name in ("operator_refs", "decision_refs", "verification_refs"):
            object.__setattr__(
                self,
                name,
                tuple(
                    sorted(
                        {
                            str(item).strip()
                            for item in getattr(self, name)
                            if str(item).strip()
                        }
                    )
                ),
            )
        if not self.operator_refs or not str(self.owner_receipt_ref).strip():
            raise EarlyExitError(
                "adaptive_depth_execution_receipt_incomplete",
                "completed layer receipt requires operator and owner references",
            )
        if not self.decision_refs:
            raise EarlyExitError(
                "adaptive_depth_execution_receipt_incomplete",
                "completed layer receipt requires a canonical decision reference",
            )
        object.__setattr__(
            self,
            "artifact_refs",
            tuple(sorted(self.artifact_refs, key=lambda item: item.ref_id)),
        )
        if (
            int(self.actual_tokens) < 0
            or float(self.actual_cost_usd) < 0
            or int(self.actual_latency_ms) < 0
        ):
            raise EarlyExitError(
                "adaptive_depth_execution_cost_invalid",
                "execution owner cost, token, and latency values cannot be negative",
            )
        object.__setattr__(self, "actual_tokens", int(self.actual_tokens))
        object.__setattr__(self, "actual_cost_usd", float(self.actual_cost_usd))
        object.__setattr__(self, "actual_latency_ms", int(self.actual_latency_ms))

    @property
    def digest(self) -> str:
        return canonical_digest(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "layer_index": self.layer_index,
            "operator_refs": list(self.operator_refs),
            "owner_receipt_ref": self.owner_receipt_ref,
            "decision_refs": list(self.decision_refs),
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "verification_refs": list(self.verification_refs),
            "actual_tokens": self.actual_tokens,
            "actual_cost_usd": self.actual_cost_usd,
            "actual_latency_ms": self.actual_latency_ms,
            "completed": self.completed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "digest": self.digest}


@dataclass(frozen=True, slots=True)
class AdaptiveDepthCostReceipt:
    proposal_id: str
    proposal_digest: str
    decision_ref: str
    proposed_depth: int
    executed_depth: int
    proposed_operator_count: int
    executed_operator_count: int
    avoided_operator_refs: tuple[str, ...]
    actual_tokens: int
    estimated_avoided_tokens: int
    actual_cost_usd: float
    estimated_avoided_cost_usd: float
    actual_latency_ms: int
    task_completed: bool
    verifier_passed: bool
    artifact_complete: bool
    schema_version: str = ADAPTIVE_DEPTH_COST_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "avoided_operator_refs",
            tuple(sorted({str(item) for item in self.avoided_operator_refs})),
        )
        if self.executed_depth > self.proposed_depth:
            raise EarlyExitError(
                "adaptive_depth_executed_depth_invalid",
                "executed depth exceeds the proposal",
            )
        if self.executed_operator_count > self.proposed_operator_count:
            raise EarlyExitError(
                "adaptive_depth_executed_operator_count_invalid",
                "executed operator count exceeds the proposal",
            )
        if (
            self.proposed_depth < 1
            or self.executed_depth < 1
            or self.proposed_operator_count < 1
            or self.executed_operator_count < 1
            or self.actual_tokens < 0
            or self.estimated_avoided_tokens < 0
            or self.actual_cost_usd < 0
            or self.estimated_avoided_cost_usd < 0
            or self.actual_latency_ms < 0
        ):
            raise EarlyExitError(
                "adaptive_depth_cost_receipt_invalid",
                "adaptive-depth cost receipt contains invalid counts or costs",
            )

    @property
    def digest(self) -> str:
        return canonical_digest(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "decision_ref": self.decision_ref,
            "proposed_depth": self.proposed_depth,
            "executed_depth": self.executed_depth,
            "proposed_operator_count": self.proposed_operator_count,
            "executed_operator_count": self.executed_operator_count,
            "avoided_operator_refs": list(self.avoided_operator_refs),
            "actual_tokens": self.actual_tokens,
            "estimated_avoided_tokens": self.estimated_avoided_tokens,
            "actual_cost_usd": self.actual_cost_usd,
            "estimated_avoided_cost_usd": self.estimated_avoided_cost_usd,
            "actual_latency_ms": self.actual_latency_ms,
            "task_completed": self.task_completed,
            "verifier_passed": self.verifier_passed,
            "artifact_complete": self.artifact_complete,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "digest": self.digest}


@dataclass(frozen=True, slots=True)
class AdaptiveDepthResult:
    proposal: OperatorSelectionProposal
    executed_layers: tuple[LayerExecutionReceipt, ...]
    eligibility_snapshots: tuple[ExitEligibilitySnapshot, ...]
    decision_receipts: tuple[ExitDecisionReceipt, ...]
    final_decision: ExitDecisionReceipt
    cost_receipt: AdaptiveDepthCostReceipt
    policy_outcome: PolicyOutcome
    checkpoint_binding: EarlyExitCheckpointBinding
    events: tuple[EventRecord, ...]
    early_exit_enabled: bool
    exited_early: bool
    schema_version: str = ADAPTIVE_DEPTH_RESULT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal": self.proposal.to_dict(),
            "executed_layers": [item.to_dict() for item in self.executed_layers],
            "eligibility_snapshots": [
                item.to_dict() for item in self.eligibility_snapshots
            ],
            "decision_receipts": [
                item.to_dict() for item in self.decision_receipts
            ],
            "final_decision": self.final_decision.to_dict(),
            "cost_receipt": self.cost_receipt.to_dict(),
            "policy_outcome": self.policy_outcome.to_dict(),
            "checkpoint_binding": self.checkpoint_binding.to_dict(),
            "events": [
                {
                    "event_id": item.event_id,
                    "event_type": item.event_type.value,
                    "run_id": item.run_id,
                    "task_id": item.task_id,
                    "payload": dict(item.payload),
                }
                for item in self.events
            ],
            "early_exit_enabled": self.early_exit_enabled,
            "exited_early": self.exited_early,
        }


class AdaptiveDepthRuntime:
    """Executes proposal layers until the deterministic verifier gate permits exit."""

    def __init__(
        self,
        gate: DeterministicEarlyExitGate,
        *,
        admit_event: EventSink | None = None,
    ) -> None:
        self.gate = gate
        self.admit_event = admit_event or (lambda event: None)

    def execute(
        self,
        *,
        proposal: OperatorSelectionProposal,
        execution_layers: Sequence[OperatorLayerProposal] | None = None,
        execution_port: OperatorLayerExecutionPort,
        eligibility_port: EligibilityCapturePort,
        early_exit_enabled: bool = True,
        evaluated_at: str | None = None,
    ) -> AdaptiveDepthResult:
        if not proposal.layers:
            raise EarlyExitError(
                "adaptive_depth_proposal_empty",
                "adaptive depth requires a non-empty operator proposal",
            )
        layers = (
            tuple(execution_layers)
            if execution_layers is not None
            else proposal.layers
        )
        self._validate_execution_layers(proposal, layers)
        minimum_operators, minimum_verification = (
            self._minimum_operator_path(layers)
        )
        executed: list[LayerExecutionReceipt] = []
        snapshots: list[ExitEligibilitySnapshot] = []
        decisions: list[ExitDecisionReceipt] = []
        events: list[EventRecord] = []
        all_candidates = tuple(
            item for layer in layers for item in layer.candidates
        )
        candidate_refs = {
            self._candidate_ref(item): item for item in all_candidates
        }
        executed_refs: set[str] = set()
        for layer in layers:
            receipt = execution_port.execute_layer(
                proposal=proposal,
                layer=layer,
            )
            self._validate_layer_receipt(layer, receipt)
            duplicate_refs = executed_refs.intersection(receipt.operator_refs)
            if duplicate_refs:
                raise EarlyExitError(
                    "adaptive_depth_operator_replayed",
                    f"operator execution repeated across layers: {sorted(duplicate_refs)}",
                )
            executed_refs.update(receipt.operator_refs)
            executed.append(receipt)
            remaining = tuple(
                item
                for ref, item in candidate_refs.items()
                if ref not in executed_refs
            )
            snapshot = eligibility_port.capture(
                remaining_candidates=remaining,
                minimum_operator_refs=minimum_operators,
                minimum_verification_refs=minimum_verification,
            )
            if (
                snapshot.proposal_id != proposal.proposal_id
                or snapshot.proposal_digest != proposal.digest
            ):
                raise EarlyExitError(
                    "adaptive_depth_snapshot_proposal_mismatch",
                    "eligibility snapshot belongs to another operator proposal",
                )
            decision = self.gate.evaluate(
                snapshot,
                enabled=early_exit_enabled,
                evaluated_at=evaluated_at,
            )
            snapshots.append(snapshot)
            decisions.append(decision)
            event = self._decision_event(
                proposal=proposal,
                receipt=receipt,
                snapshot=snapshot,
                decision=decision,
                proposed_depth=len(layers),
            )
            self.admit_event(event)
            events.append(event)
            if decision.decision is ExitDecision.EXIT:
                break
        if not executed or not snapshots or not decisions:
            raise EarlyExitError(
                "adaptive_depth_execution_missing",
                "adaptive depth produced no execution or eligibility receipt",
            )
        exited = decisions[-1].decision is ExitDecision.EXIT
        avoided = tuple(
            item
            for ref, item in candidate_refs.items()
            if ref not in executed_refs
        )
        artifact_complete = bool(
            snapshots[-1].required_artifact_ids
            and not snapshots[-1].invalid_artifact_ids
            and len(snapshots[-1].verified_artifact_refs)
            >= len(snapshots[-1].required_artifact_ids)
        )
        final_condition_map = {
            item.condition_id: item.passed for item in decisions[-1].conditions
        }
        completion_conditions = {
            "snapshot_fresh",
            "canonical_owner_evidence_complete",
            "final_verifier_passed",
            "critical_obligations_resolved",
            "required_artifacts_complete",
            "permission_settled",
            "side_effects_settled",
            "minimum_operator_path_executed",
            "checkpoint_requirement_current",
            "memory_continuity_passed",
        }
        task_completed = bool(
            artifact_complete
            and completion_conditions.issubset(final_condition_map)
            and all(final_condition_map[item] for item in completion_conditions)
        )
        provisional_cost = AdaptiveDepthCostReceipt(
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest,
            decision_ref=decisions[-1].decision_id,
            proposed_depth=len(layers),
            executed_depth=len(executed),
            proposed_operator_count=len(all_candidates),
            executed_operator_count=len(executed_refs),
            avoided_operator_refs=tuple(
                self._candidate_ref(item) for item in avoided
            ),
            actual_tokens=sum(item.actual_tokens for item in executed),
            estimated_avoided_tokens=(
                sum(item.estimated_tokens for item in avoided) if exited else 0
            ),
            actual_cost_usd=round(
                sum(item.actual_cost_usd for item in executed),
                12,
            ),
            estimated_avoided_cost_usd=(
                round(sum(item.estimated_cost_usd for item in avoided), 12)
                if exited
                else 0.0
            ),
            actual_latency_ms=sum(item.actual_latency_ms for item in executed),
            task_completed=task_completed,
            verifier_passed=snapshots[-1].final_verifier_passed,
            artifact_complete=artifact_complete,
        )
        outcome = self._policy_outcome(
            proposal=proposal,
            planned_layers=layers,
            planned_candidates=all_candidates,
            final_snapshot=snapshots[-1],
            final_decision=decisions[-1],
            executed=tuple(executed),
            cost=provisional_cost,
            early_exit_enabled=early_exit_enabled,
            exited=exited,
        )
        finalized = self.gate.finalize_posterior(decisions[-1], outcome)
        decisions[-1] = finalized
        cost = replace(provisional_cost, decision_ref=finalized.decision_id)
        outcome = self._policy_outcome(
            proposal=proposal,
            planned_layers=layers,
            planned_candidates=all_candidates,
            final_snapshot=snapshots[-1],
            final_decision=finalized,
            executed=tuple(executed),
            cost=cost,
            early_exit_enabled=early_exit_enabled,
            exited=exited,
        )
        # The outcome id is stable across posterior finalization because its
        # header is derived from the final execution and decision identities.
        finalized = self.gate.finalize_posterior(finalized, outcome)
        decisions[-1] = finalized
        binding = self.gate.checkpoint_binding(snapshots[-1], finalized)
        outcome_event = self._outcome_event(
            outcome=outcome,
            cost=cost,
            final_decision=finalized,
            snapshot=snapshots[-1],
        )
        self.admit_event(outcome_event)
        events.append(outcome_event)
        return AdaptiveDepthResult(
            proposal=proposal,
            executed_layers=tuple(executed),
            eligibility_snapshots=tuple(snapshots),
            decision_receipts=tuple(decisions),
            final_decision=finalized,
            cost_receipt=cost,
            policy_outcome=outcome,
            checkpoint_binding=binding,
            events=tuple(events),
            early_exit_enabled=early_exit_enabled,
            exited_early=exited,
        )

    @staticmethod
    def _candidate_ref(candidate: OperatorCandidate) -> str:
        return f"{candidate.operator_id}@{candidate.version}"

    def _validate_layer_receipt(
        self,
        layer: OperatorLayerProposal,
        receipt: LayerExecutionReceipt,
    ) -> None:
        expected = {
            self._candidate_ref(item)
            for item in layer.candidates
        }
        if receipt.layer_index != layer.layer_index:
            raise EarlyExitError(
                "adaptive_depth_layer_receipt_mismatch",
                "execution receipt belongs to another layer",
            )
        if set(receipt.operator_refs) != expected:
            raise EarlyExitError(
                "adaptive_depth_operator_receipt_mismatch",
                "execution owner did not return receipts for the exact proposed layer",
            )
        if not receipt.completed:
            raise EarlyExitError(
                "adaptive_depth_layer_incomplete",
                "incomplete execution cannot advance adaptive depth",
            )

    @staticmethod
    def _validate_execution_layers(
        proposal: OperatorSelectionProposal,
        layers: Sequence[OperatorLayerProposal],
    ) -> None:
        if not layers or any(not item.candidates for item in layers):
            raise EarlyExitError(
                "adaptive_depth_execution_plan_empty",
                "ResourceScheduler execution plan must contain non-empty layers",
            )
        proposal_refs = {
            f"{item.operator_id}@{item.version}" for item in proposal.candidates
        }
        scheduled_refs = [
            f"{item.operator_id}@{item.version}"
            for layer in layers
            for item in layer.candidates
        ]
        if (
            not set(scheduled_refs).issubset(proposal_refs)
            or len(scheduled_refs) != len(set(scheduled_refs))
        ):
            raise EarlyExitError(
                "adaptive_depth_execution_plan_invalid",
                "scheduler execution plan must be a unique subset of MaAS candidates",
            )
        indexes = [item.layer_index for item in layers]
        if indexes != sorted(indexes) or len(indexes) != len(set(indexes)):
            raise EarlyExitError(
                "adaptive_depth_execution_plan_order_invalid",
                "scheduler execution layers must have stable unique ordering",
            )

    @staticmethod
    def _minimum_operator_path(
        layers: Sequence[OperatorLayerProposal],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        required = [
            f"{layers[0].candidates[0].operator_id}@"
            f"{layers[0].candidates[0].version}"
        ]
        verifier_candidate = next(
            (
                item
                for layer in layers
                for item in layer.candidates
                if item.score_components.verifier_necessity >= 10_000
            ),
            None,
        )
        if verifier_candidate is not None:
            required.append(
                f"{verifier_candidate.operator_id}@{verifier_candidate.version}"
            )
        return tuple(sorted(set(required))), ("final_verifier",)

    def _policy_outcome(
        self,
        *,
        proposal: OperatorSelectionProposal,
        planned_layers: Sequence[OperatorLayerProposal],
        planned_candidates: Sequence[OperatorCandidate],
        final_snapshot: ExitEligibilitySnapshot,
        final_decision: ExitDecisionReceipt,
        executed: tuple[LayerExecutionReceipt, ...],
        cost: AdaptiveDepthCostReceipt,
        early_exit_enabled: bool,
        exited: bool,
    ) -> PolicyOutcome:
        artifact_complete = bool(
            final_snapshot.required_artifact_ids
            and not final_snapshot.invalid_artifact_ids
            and len(final_snapshot.verified_artifact_refs)
            >= len(final_snapshot.required_artifact_ids)
        )
        condition_map = {
            item.condition_id: item.passed for item in final_decision.conditions
        }
        outcome_seed = canonical_digest(
            {
                "proposal": proposal.digest,
                "decision": final_decision.decision_id,
                "snapshot": final_snapshot.digest,
                "executed": [item.digest for item in executed],
                "cost": cost.digest,
            }
        )
        header = ContractHeader(
            contract_id=f"adaptive-depth-outcome-{outcome_seed[:24]}",
            created_at=final_decision.header.created_at,
            source_event_id=proposal.header.source_event_id,
            correlation_id=proposal.header.correlation_id,
            causation_id=final_decision.decision_id,
            mechanism_id=self.gate.config.mechanism_id,
            mechanism_version=self.gate.config.mechanism_version,
            input_version=final_snapshot.schema_version,
            idempotency_key=f"adaptive-depth-outcome:{outcome_seed}",
            configuration_digest=self.gate.config.digest,
        )
        pending_side_effect_count = len(final_snapshot.side_effect_pending_ids) + len(
            final_snapshot.side_effect_unknown_ids
        )
        metrics = FrozenDict(
            {
                "early_exit": {
                    "enabled": early_exit_enabled,
                    "decision": final_decision.decision.value,
                    "exited": exited,
                    "posterior_result": final_decision.posterior_result.value,
                    "proposed_depth": len(planned_layers),
                    "executed_depth": len(executed),
                    "proposed_operator_count": len(planned_candidates),
                    "executed_operator_count": sum(
                        len(item.operator_refs) for item in executed
                    ),
                    "avoided_operator_count": cost.proposed_operator_count
                    - cost.executed_operator_count
                    if exited
                    else 0,
                    "artifact_complete": artifact_complete,
                    "unresolved_critical_obligation_count": len(
                        final_snapshot.unresolved_critical_obligation_ids
                    ),
                    "permission_pending_count": len(
                        final_snapshot.permission_pending_ids
                    ),
                    "side_effect_pending_or_unknown_count": pending_side_effect_count,
                    "condition_results": condition_map,
                    "decision_receipt_digest": final_decision.digest,
                    "snapshot_digest": final_snapshot.digest,
                },
                "cost": {
                    **cost.payload(),
                    "cost_receipt_digest": cost.digest,
                },
                "operator_execution_receipt_digests": [
                    item.digest for item in executed
                ],
                "verifier_refs": [
                    final_snapshot.final_verifier_ref,
                    final_snapshot.continuity_ref,
                ],
                "checkpoint_ref": final_snapshot.checkpoint_ref,
            }
        )
        causal_refs = tuple(
            item
            for item in (
                proposal.proposal_id,
                final_decision.decision_id,
                final_snapshot.snapshot_id,
                final_snapshot.checkpoint_ref,
                final_snapshot.final_verifier_ref,
                final_snapshot.continuity_ref,
                *(receipt.owner_receipt_ref for receipt in executed),
                *(
                    decision_ref
                    for receipt in executed
                    for decision_ref in receipt.decision_refs
                ),
            )
            if item
        )
        return PolicyOutcome(
            header=header,
            proposal_ref=proposal.proposal_id,
            decision_ref=final_decision.decision_id,
            commit_ref=proposal.committed_graph_commit_id,
            verifier_result=(
                "passed" if final_snapshot.final_verifier_passed else "failed"
            ),
            artifact_refs=final_snapshot.verified_artifact_refs,
            metrics=metrics,
            permission_result=(
                "settled"
                if not final_snapshot.permission_pending_ids
                else "pending"
            ),
            recovery_result=(
                "not_required"
                if final_snapshot.continuity_passed
                and final_snapshot.checkpoint_requirement_revision
                == final_snapshot.requirement_revision
                else "replan_required"
            ),
            causal_refs=causal_refs,
        )

    @staticmethod
    def _decision_event(
        *,
        proposal: OperatorSelectionProposal,
        receipt: LayerExecutionReceipt,
        snapshot: ExitEligibilitySnapshot,
        decision: ExitDecisionReceipt,
        proposed_depth: int,
    ) -> EventRecord:
        return EventRecord(
            run_id=snapshot.run_id,
            task_id=snapshot.task_id,
            event_id="event_early_exit_" + decision.digest[:24],
            event_type=EventType.EVALUATION,
            payload={
                "schema": EXIT_EVENT_SCHEMA,
                "proposal_id": proposal.proposal_id,
                "proposal_digest": proposal.digest,
                "layer_index": receipt.layer_index,
                "proposed_depth": proposed_depth,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_digest": snapshot.digest,
                "decision_id": decision.decision_id,
                "decision_digest": decision.digest,
                "decision": decision.decision.value,
                "failed_conditions": list(decision.failed_conditions),
                "verifier_refs": list(decision.verifier_refs),
                "artifact_refs": list(decision.artifact_refs),
                "avoided_operator_count": decision.avoided_operator_count,
                "avoided_tokens": decision.avoided_tokens,
                "avoided_cost_usd": decision.avoided_cost_usd,
                "placement_owner": "ResourceScheduler",
                "lease_owner": "WorkerPoolFoundationRuntime",
                "physical_attempt_owner_transferred": False,
            },
        )

    @staticmethod
    def _outcome_event(
        *,
        outcome: PolicyOutcome,
        cost: AdaptiveDepthCostReceipt,
        final_decision: ExitDecisionReceipt,
        snapshot: ExitEligibilitySnapshot,
    ) -> EventRecord:
        return EventRecord(
            run_id=snapshot.run_id,
            task_id=snapshot.task_id,
            event_id="event_adaptive_depth_outcome_" + outcome.digest[:24],
            event_type=EventType.EVALUATION,
            payload={
                "schema": "zyra.adaptive-depth-outcome-event/v1",
                "outcome_id": outcome.header.contract_id,
                "outcome_digest": outcome.digest,
                "decision_id": final_decision.decision_id,
                "decision_digest": final_decision.digest,
                "posterior_result": final_decision.posterior_result.value,
                "cost_receipt_digest": cost.digest,
                "task_completed": cost.task_completed,
                "verifier_passed": cost.verifier_passed,
                "artifact_complete": cost.artifact_complete,
            },
        )


EXIT_EVENT_SCHEMA = "zyra.early-exit-decision-event/v1"


__all__ = [
    "ADAPTIVE_DEPTH_COST_SCHEMA",
    "ADAPTIVE_DEPTH_RESULT_SCHEMA",
    "EXIT_EVENT_SCHEMA",
    "LAYER_EXECUTION_SCHEMA",
    "AdaptiveDepthCostReceipt",
    "AdaptiveDepthResult",
    "AdaptiveDepthRuntime",
    "EligibilityCapturePort",
    "LayerExecutionReceipt",
    "OperatorLayerExecutionPort",
]
