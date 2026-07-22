from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable

from .action_runtime import ExecutionResult
from .contracts import (
    RecoveryContext,
    RecoveryOutcomeKind,
    RecoveryPlan,
    RouteLayer,
    RoutingMemoryRecord,
    stable_digest,
    utc_now,
)
from .memory_feedback import RouteScore, RoutingMemoryFeedback
from .route_memory_runtime import MemoryAwareRouteRuntime, MemoryRouteCandidate
from .store import RecoveryPlanStore


class FeedbackIntegrationError(RuntimeError):
    pass


class FeedbackNotApplied(FeedbackIntegrationError):
    pass


class FeedbackInfluenceMissing(FeedbackIntegrationError):
    pass


class FeedbackInfluenceKind(StrEnum):
    APPLIED_PROOF_LINK = "applied_proof_link"
    OUTCOME_LINK = "outcome_link"
    RECEIPT_LINK = "receipt_link"
    ROUTE_SCORE = "route_score"
    CONTEXT_EVIDENCE = "context_evidence"
    CANDIDATE_ORDER = "candidate_order"
    PENALTY_EXCLUSION = "penalty_exclusion"


@dataclass(frozen=True, slots=True)
class FeedbackInfluenceCheck:
    kind: FeedbackInfluenceKind
    passed: bool
    message: str
    evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "passed": self.passed,
            "message": self.message,
            "evidence_refs": list(self.evidence_refs),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class CandidateInfluence:
    layer: RouteLayer
    route_id: str
    ordinal_before: int
    ordinal_after: int
    base_priority: float
    memory_score: float
    available_before: bool
    available_after: bool
    penalty_active: bool
    evidence_refs: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return (
            self.ordinal_before != self.ordinal_after
            or self.available_before != self.available_after
            or self.memory_score != 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "route_id": self.route_id,
            "ordinal_before": self.ordinal_before,
            "ordinal_after": self.ordinal_after,
            "base_priority": self.base_priority,
            "memory_score": self.memory_score,
            "available_before": self.available_before,
            "available_after": self.available_after,
            "penalty_active": self.penalty_active,
            "evidence_refs": list(self.evidence_refs),
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class FeedbackInfluenceProof:
    proof_id: str
    run_id: str
    task_id: str
    plan_id: str
    outcome_id: str
    routing_memory_id: str
    applied_proof_id: str
    checks: tuple[FeedbackInfluenceCheck, ...]
    route_scores: tuple[RouteScore, ...]
    candidate_influences: tuple[CandidateInfluence, ...]
    context_evidence_digests: tuple[str, ...]
    created_at: str = field(default_factory=utc_now)

    @property
    def accepted(self) -> bool:
        return all(item.passed for item in self.checks)

    @property
    def changed_later_decision(self) -> bool:
        return any(item.changed for item in self.candidate_influences) or any(
            item.kind is FeedbackInfluenceKind.CONTEXT_EVIDENCE and item.passed
            for item in self.checks
        )

    def require_accepted(self) -> "FeedbackInfluenceProof":
        if not self.accepted:
            raise FeedbackInfluenceMissing(
                "; ".join(item.message for item in self.checks if not item.passed)
            )
        if not self.changed_later_decision:
            raise FeedbackInfluenceMissing("routing feedback has no observable downstream influence")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-feedback-influence-proof/v1",
            "proof_id": self.proof_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "outcome_id": self.outcome_id,
            "routing_memory_id": self.routing_memory_id,
            "applied_proof_id": self.applied_proof_id,
            "checks": [item.to_dict() for item in self.checks],
            "route_scores": [item.to_dict() for item in self.route_scores],
            "candidate_influences": [item.to_dict() for item in self.candidate_influences],
            "context_evidence_digests": list(self.context_evidence_digests),
            "accepted": self.accepted,
            "changed_later_decision": self.changed_later_decision,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class FeedbackTaskSnapshot:
    task_id: str
    record_count: int
    applied_record_count: int
    orphan_record_ids: tuple[str, ...]
    proof_refs: tuple[str, ...]
    scores_by_layer: Mapping[str, tuple[RouteScore, ...]]
    latest_record_at: str

    @property
    def consistent(self) -> bool:
        return not self.orphan_record_ids and self.record_count == self.applied_record_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-feedback-task-snapshot/v1",
            "task_id": self.task_id,
            "record_count": self.record_count,
            "applied_record_count": self.applied_record_count,
            "orphan_record_ids": list(self.orphan_record_ids),
            "proof_refs": list(self.proof_refs),
            "scores_by_layer": {
                layer: [item.to_dict() for item in values]
                for layer, values in self.scores_by_layer.items()
            },
            "latest_record_at": self.latest_record_at,
            "consistent": self.consistent,
        }


class RecoveryFeedbackIntegrationRuntime:
    """Closes applied recovery outcomes into later route and policy inputs.

    RoutingMemoryFeedback remains the canonical memory writer.  This runtime is
    an integration verifier: it rejects pre-application learning, proves the
    durable record is linked to the owner receipts and continuation proof, and
    demonstrates that a subsequent context/ranking consumes that record.
    """

    IDENTITY_KEYS: Mapping[RouteLayer, tuple[str, ...]] = {
        RouteLayer.GRAPH: ("commit_id", "graph_id", "revision"),
        RouteLayer.WORKER: ("lease_id", "worker_lease_id", "worker_id"),
        RouteLayer.BACKEND: ("backend_lease_id", "lease_id", "backend_id"),
        RouteLayer.WORKSPACE: ("workspace_id", "root", "revision"),
        RouteLayer.PROVIDER: ("route_id", "provider_route_id", "provider_id"),
        RouteLayer.MODEL: ("model_id", "route_id"),
        RouteLayer.CREDENTIAL: ("credential_id", "route_id"),
        RouteLayer.TRANSPORT: ("transport_id", "route_id"),
    }

    def __init__(
        self,
        store: RecoveryPlanStore,
        feedback: RoutingMemoryFeedback,
        routes: MemoryAwareRouteRuntime,
        *,
        proof_sink: Callable[[FeedbackInfluenceProof], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.store = store
        self.feedback = feedback
        self.routes = routes
        self.proof_sink = proof_sink

    def verify_execution(
        self,
        plan: RecoveryPlan,
        execution: ExecutionResult,
        decision_context: RecoveryContext,
        *,
        candidates: Mapping[RouteLayer | str, Sequence[Mapping[str, Any]]] | None = None,
        require_candidate_change: bool = False,
    ) -> FeedbackInfluenceProof:
        if execution.plan.plan_id != plan.plan_id:
            raise FeedbackNotApplied("feedback execution belongs to another plan")
        if execution.outcome.kind is not RecoveryOutcomeKind.RECOVERED or not execution.outcome.success:
            raise FeedbackNotApplied("only an applied recovered outcome may influence routing memory")
        applied = copy.deepcopy(dict(execution.applied_proof))
        applied_proof_id = str(applied.get("proof_id") or "")
        if not applied_proof_id or not bool(applied.get("applied", False)):
            raise FeedbackNotApplied("routing memory was requested without an applied continuation proof")
        record = self._record(execution.routing_memory_id)
        checks = list(self._link_checks(plan, execution, record, applied_proof_id))
        subsequent = self.feedback.enrich_context(replace(
            decision_context,
            memory_evidence=tuple(
                item
                for item in decision_context.memory_evidence
                if execution.routing_memory_id not in item.get("evidence_refs", ())
            ),
        ))
        evidence = tuple(
            item for item in subsequent.memory_evidence
            if execution.outcome.outcome_id in item.get("evidence_refs", ())
            or applied_proof_id in item.get("evidence_refs", ())
        )
        checks.append(FeedbackInfluenceCheck(
            FeedbackInfluenceKind.CONTEXT_EVIDENCE,
            bool(evidence),
            "subsequent recovery context consumes the applied outcome memory"
            if evidence else "applied routing memory is absent from the subsequent context",
            evidence_refs=tuple(
                str(ref)
                for item in evidence
                for ref in item.get("evidence_refs", ())
                if str(ref)
            ),
        ))
        route_scores = tuple(self.feedback.scores(plan.signal.refs.task_id))
        score_refs = {ref for score in route_scores for ref in score.evidence_refs}
        expected_score = bool(record.route_layers) and record.record_id != ""
        checks.append(FeedbackInfluenceCheck(
            FeedbackInfluenceKind.ROUTE_SCORE,
            not expected_score or execution.outcome.outcome_id in score_refs,
            "route scores include the applied outcome"
            if not expected_score or execution.outcome.outcome_id in score_refs
            else "route score aggregation dropped the applied outcome",
            evidence_refs=tuple(sorted(score_refs & set(record.evidence_refs))),
        ))
        candidate_influences = self.compare_candidates(
            plan.signal.refs.task_id,
            candidates or self._synthetic_candidates(record),
        )
        if require_candidate_change:
            checks.append(FeedbackInfluenceCheck(
                FeedbackInfluenceKind.CANDIDATE_ORDER,
                any(item.changed for item in candidate_influences),
                "routing memory changes later candidate order or eligibility"
                if any(item.changed for item in candidate_influences)
                else "routing memory did not change later candidate order or eligibility",
                evidence_refs=tuple(ref for item in candidate_influences for ref in item.evidence_refs),
            ))
        proof_id = "recoveryfeedbackproof:" + stable_digest({
            "plan_id": plan.plan_id,
            "outcome_id": execution.outcome.outcome_id,
            "memory_id": record.record_id,
            "applied_proof_id": applied_proof_id,
            "checks": [item.to_dict() for item in checks],
            "candidate_influences": [item.to_dict() for item in candidate_influences],
        })[:40]
        proof = FeedbackInfluenceProof(
            proof_id=proof_id,
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            plan_id=plan.plan_id,
            outcome_id=execution.outcome.outcome_id,
            routing_memory_id=record.record_id,
            applied_proof_id=applied_proof_id,
            checks=tuple(checks),
            route_scores=route_scores,
            candidate_influences=candidate_influences,
            context_evidence_digests=tuple(stable_digest(copy.deepcopy(dict(item))) for item in evidence),
        ).require_accepted()
        if self.proof_sink is not None:
            receipt = dict(self.proof_sink(proof) or {})
            if receipt and not str(receipt.get("event_id") or receipt.get("receipt_id") or ""):
                raise FeedbackInfluenceMissing("feedback proof sink returned an unidentifiable receipt")
        return proof

    def compare_candidates(
        self,
        task_id: str,
        candidates: Mapping[RouteLayer | str, Sequence[Mapping[str, Any]]],
    ) -> tuple[CandidateInfluence, ...]:
        influences: list[CandidateInfluence] = []
        for raw_layer, raw_candidates in sorted(candidates.items(), key=lambda item: str(item[0])):
            layer = raw_layer if isinstance(raw_layer, RouteLayer) else RouteLayer(str(raw_layer))
            values = [copy.deepcopy(dict(item)) for item in raw_candidates]
            baseline = sorted(values, key=lambda item: (-self._priority(item), self._route_id(layer, item)))
            ranked = self.routes.rank_candidates(task_id, layer, values)
            before_index = {self._route_id(layer, item): index for index, item in enumerate(baseline)}
            after_index = {item.route_id: index for index, item in enumerate(ranked)}
            by_id = {item.route_id: item for item in ranked}
            for route_id in sorted(set(before_index) | set(after_index)):
                candidate = by_id.get(route_id)
                if candidate is None:
                    continue
                original = next((item for item in values if self._route_id(layer, item) == route_id), {})
                influences.append(CandidateInfluence(
                    layer=layer,
                    route_id=route_id,
                    ordinal_before=before_index.get(route_id, -1),
                    ordinal_after=after_index.get(route_id, -1),
                    base_priority=self._priority(original),
                    memory_score=candidate.memory_score,
                    available_before=bool(original.get("available", True)),
                    available_after=candidate.available,
                    penalty_active=candidate.penalty_active,
                    evidence_refs=candidate.evidence_refs,
                ))
        return tuple(influences)

    def task_snapshot(self, task_id: str) -> FeedbackTaskSnapshot:
        records = self.store.feedback(task_id=task_id, limit=5000)
        orphan: list[str] = []
        applied = 0
        proof_refs: set[str] = set()
        for record in records:
            outcomes = [
                outcome
                for plan in self.store.plans(task_id=task_id, limit=5000)
                for outcome in self.store.outcomes(plan_id=plan.plan_id)
                if outcome.outcome_id in record.evidence_refs
            ]
            proofs = tuple(ref for ref in record.evidence_refs if ref.startswith("recoveryproof:"))
            proof_refs.update(proofs)
            if outcomes and all(item.success and item.kind is RecoveryOutcomeKind.RECOVERED for item in outcomes) and proofs:
                applied += 1
            else:
                orphan.append(record.record_id)
        grouped: defaultdict[str, list[RouteScore]] = defaultdict(list)
        for score in self.feedback.scores(task_id):
            grouped[score.layer.value].append(score)
        return FeedbackTaskSnapshot(
            task_id=task_id,
            record_count=len(records),
            applied_record_count=applied,
            orphan_record_ids=tuple(orphan),
            proof_refs=tuple(sorted(proof_refs)),
            scores_by_layer={key: tuple(value) for key, value in sorted(grouped.items())},
            latest_record_at=max((item.created_at for item in records), default=""),
        )

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-feedback-integration-contract/v1",
            "canonical_writer": "RoutingMemoryFeedback",
            "write_gate": "applied continuation proof",
            "subsequent_consumers": ["RecoveryContext.memory_evidence", "MemoryAwareRouteRuntime.rank_candidates"],
            "pre_application_learning": False,
            "model_suggestion_is_outcome": False,
            "failure_penalty_changes_eligibility": True,
            "positive_outcome_changes_ranking": True,
        }

    def _record(self, record_id: str) -> RoutingMemoryRecord:
        if not record_id:
            raise FeedbackNotApplied("applied recovery did not return a routing memory id")
        records = self.store.feedback(limit=5000)
        record = next((item for item in records if item.record_id == record_id), None)
        if record is None:
            raise FeedbackNotApplied(f"routing memory record is not durable: {record_id}")
        return record

    @staticmethod
    def _link_checks(
        plan: RecoveryPlan,
        execution: ExecutionResult,
        record: RoutingMemoryRecord,
        applied_proof_id: str,
    ) -> tuple[FeedbackInfluenceCheck, ...]:
        evidence = set(record.evidence_refs)
        receipt_ids = {item.receipt_id for item in execution.receipts}
        return (
            FeedbackInfluenceCheck(
                FeedbackInfluenceKind.APPLIED_PROOF_LINK,
                applied_proof_id in evidence,
                "memory record links the applied continuation proof"
                if applied_proof_id in evidence else "memory record is not linked to the applied continuation proof",
                evidence_refs=(applied_proof_id,),
            ),
            FeedbackInfluenceCheck(
                FeedbackInfluenceKind.OUTCOME_LINK,
                execution.outcome.outcome_id in evidence,
                "memory record links the recovered outcome"
                if execution.outcome.outcome_id in evidence else "memory record is not linked to the recovered outcome",
                evidence_refs=(execution.outcome.outcome_id,),
            ),
            FeedbackInfluenceCheck(
                FeedbackInfluenceKind.RECEIPT_LINK,
                bool(receipt_ids) and receipt_ids.issubset(evidence),
                "memory record links every applied owner receipt"
                if receipt_ids and receipt_ids.issubset(evidence) else "memory record omits an applied owner receipt",
                evidence_refs=tuple(sorted(receipt_ids)),
            ),
            FeedbackInfluenceCheck(
                FeedbackInfluenceKind.OUTCOME_LINK,
                record.run_id == plan.signal.refs.run_id and record.task_id == plan.signal.refs.task_id,
                "memory record remains in plan run/task custody"
                if record.run_id == plan.signal.refs.run_id and record.task_id == plan.signal.refs.task_id
                else "memory record crosses plan run/task custody",
                evidence_refs=(record.record_id,),
            ),
        )

    def _synthetic_candidates(
        self,
        record: RoutingMemoryRecord,
    ) -> Mapping[RouteLayer, Sequence[Mapping[str, Any]]]:
        refs = {
            RouteLayer.WORKER: record.worker_id,
            RouteLayer.BACKEND: record.backend_id,
            RouteLayer.PROVIDER: record.provider_id,
            RouteLayer.MODEL: record.model_id,
        }
        result: dict[RouteLayer, Sequence[Mapping[str, Any]]] = {}
        for layer in record.route_layers:
            route_id = refs.get(layer, "")
            if not route_id:
                continue
            key = self.IDENTITY_KEYS[layer][0]
            result[layer] = (
                {key: route_id, "available": True, "priority": 0},
                {key: f"unchanged-alternative:{layer.value}", "available": True, "priority": 0},
            )
        return result

    def _route_id(self, layer: RouteLayer, value: Mapping[str, Any]) -> str:
        for key in self.IDENTITY_KEYS[layer]:
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        return ""

    @staticmethod
    def _priority(value: Mapping[str, Any]) -> float:
        raw = value.get("priority", value.get("weight", value.get("score", 0.0)))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0


def feedback_integration_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-feedback-integration-surface/v1",
        "input": "RecoveryActionRuntime applied outcome plus continuation proof",
        "durable_record_owner": "RecoveryPlanStore.routing_feedback",
        "effects": ["subsequent context evidence", "route score", "candidate ordering", "bounded penalty exclusion"],
        "pre_apply_feedback_rejected": True,
        "feedback_without_owner_receipts_rejected": True,
    }


__all__ = [
    "CandidateInfluence",
    "FeedbackInfluenceCheck",
    "FeedbackInfluenceKind",
    "FeedbackInfluenceMissing",
    "FeedbackInfluenceProof",
    "FeedbackIntegrationError",
    "FeedbackNotApplied",
    "FeedbackTaskSnapshot",
    "RecoveryFeedbackIntegrationRuntime",
    "feedback_integration_runtime_contract",
]
