from __future__ import annotations

import copy
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from .contracts import (
    LayeredRouteDecision,
    RecoveryAction,
    RecoveryContext,
    RecoveryOutcome,
    RecoveryPlan,
    RouteLayer,
    RoutingMemoryRecord,
    stable_digest,
)
from .store import RecoveryPlanStore


class RoutingMemoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RouteScore:
    layer: RouteLayer
    route_id: str
    score: float
    successes: int
    failures: int
    observations: int
    last_seen_at: str
    evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "route_id": self.route_id,
            "score": self.score,
            "successes": self.successes,
            "failures": self.failures,
            "observations": self.observations,
            "last_seen_at": self.last_seen_at,
            "evidence_refs": list(self.evidence_refs),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


class RoutingMemorySink(Protocol):
    def persist(self, record: RoutingMemoryRecord) -> Mapping[str, Any]: ...


class CallbackRoutingMemorySink:
    def __init__(self, callback: Callable[[RoutingMemoryRecord], Mapping[str, Any] | None]) -> None:
        self._callback = callback

    def persist(self, record: RoutingMemoryRecord) -> Mapping[str, Any]:
        return dict(self._callback(record) or {})


class RoutingMemoryFeedback:
    def __init__(
        self,
        store: RecoveryPlanStore,
        *,
        sinks: Sequence[RoutingMemorySink] = (),
        penalty_seconds: int = 900,
    ) -> None:
        if penalty_seconds < 0:
            raise ValueError("penalty_seconds cannot be negative")
        self.store = store
        self.sinks = tuple(sinks)
        self.penalty_seconds = penalty_seconds

    def record(
        self,
        plan: RecoveryPlan,
        outcome: RecoveryOutcome,
        *,
        route_decision: LayeredRouteDecision | None = None,
        extra_evidence_refs: Sequence[str] = (),
    ) -> RoutingMemoryRecord:
        if outcome.plan_id != plan.plan_id:
            raise RoutingMemoryError("outcome belongs to another recovery plan")
        decision = route_decision or self._route_decision(plan.plan_id, outcome.route_decision_id)
        layers = tuple(change.layer for change in decision.changes) if decision else ()
        route_refs = self._route_refs(plan, decision)
        evidence = tuple(dict.fromkeys((
            plan.signal.signal_id,
            outcome.outcome_id,
            *outcome.receipt_ids,
            *((decision.route_decision_id,) if decision else ()),
            *(str(item) for item in extra_evidence_refs if str(item).strip()),
        )))
        delta = self._score_delta(outcome, decision)
        penalty_until = ""
        if delta < 0 and self.penalty_seconds:
            penalty_until = (datetime.now(UTC) + timedelta(seconds=self.penalty_seconds)).isoformat()
        record_id = "routingmemory_" + stable_digest({
            "plan_id": plan.plan_id,
            "outcome_id": outcome.outcome_id,
            "route_decision_id": decision.route_decision_id if decision else "",
            "route_refs": route_refs,
        })[:40]
        record = RoutingMemoryRecord(
            record_id=record_id,
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            signal_kind=plan.signal.kind,
            action=outcome.action,
            success=outcome.success,
            route_layers=layers,
            evidence_refs=evidence,
            summary=outcome.summary,
            worker_id=route_refs["worker_id"],
            backend_id=route_refs["backend_id"],
            provider_id=route_refs["provider_id"],
            model_id=route_refs["model_id"],
            score_delta=delta,
            penalty_until=penalty_until,
            metadata={
                "outcome_kind": outcome.kind.value,
                "changed_execution": bool(decision and any(change.applied for change in decision.changes)),
                "policy_rule": plan.decision.selected.policy_rule,
                "route_decision_id": decision.route_decision_id if decision else "",
                "source": "RecoveryPlanStore+canonical-owner-receipts",
            },
        )
        stored, created = self.store.append_feedback(record)
        if created:
            sink_receipts: list[dict[str, Any]] = []
            for sink in self.sinks:
                receipt = dict(sink.persist(stored))
                if receipt:
                    sink_receipts.append(receipt)
        return stored

    def scores(
        self,
        task_id: str,
        *,
        layer: RouteLayer | None = None,
        include_expired: bool = True,
    ) -> tuple[RouteScore, ...]:
        records = self.store.feedback(task_id=task_id, limit=5000)
        accumulators: dict[tuple[RouteLayer, str], dict[str, Any]] = defaultdict(
            lambda: {"score": 0.0, "successes": 0, "failures": 0, "refs": [], "last": "", "penalty": ""}
        )
        for record in records:
            for route_layer, route_id in self._record_refs(record):
                if layer is not None and route_layer is not layer:
                    continue
                item = accumulators[(route_layer, route_id)]
                weight = self._age_weight(record.created_at)
                item["score"] += record.score_delta * weight
                item["successes"] += int(record.success)
                item["failures"] += int(not record.success)
                item["refs"].extend(record.evidence_refs)
                item["last"] = max(item["last"], record.created_at)
                item["penalty"] = max(item["penalty"], record.penalty_until)
        now = datetime.now(UTC)
        scores: list[RouteScore] = []
        for (route_layer, route_id), item in accumulators.items():
            penalty_active = self._future(item["penalty"], now)
            if not include_expired and not penalty_active and item["score"] < 0:
                continue
            scores.append(RouteScore(
                layer=route_layer,
                route_id=route_id,
                score=round(float(item["score"]), 6),
                successes=int(item["successes"]),
                failures=int(item["failures"]),
                observations=int(item["successes"] + item["failures"]),
                last_seen_at=str(item["last"]),
                evidence_refs=tuple(dict.fromkeys(item["refs"]))[-64:],
                metadata={"penalty_active": penalty_active, "penalty_until": item["penalty"]},
            ))
        return tuple(sorted(scores, key=lambda item: (item.layer.value, -item.score, item.route_id)))

    def evidence(self, task_id: str, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        scores = self.scores(task_id)
        return tuple({
            "kind": "recovery_route_outcome",
            "route_layer": score.layer.value,
            "route_id": score.route_id,
            "score": score.score,
            "successes": score.successes,
            "failures": score.failures,
            "evidence_refs": list(score.evidence_refs),
            "metadata": copy.deepcopy(dict(score.metadata)),
        } for score in scores[: max(0, min(limit, 1000))])

    def enrich_context(self, context: RecoveryContext) -> RecoveryContext:
        learned = self.evidence(context.refs.task_id)
        existing = tuple(copy.deepcopy(dict(item)) for item in context.memory_evidence)
        known = {stable_digest(item) for item in existing}
        additions = tuple(item for item in learned if stable_digest(item) not in known)
        return replace(context, memory_evidence=existing + additions)

    def rank_candidates(
        self,
        task_id: str,
        layer: RouteLayer,
        candidates: Sequence[Mapping[str, Any]],
        *,
        identity_key: str,
        unavailable_key: str = "available",
    ) -> tuple[dict[str, Any], ...]:
        by_id = {score.route_id: score for score in self.scores(task_id, layer=layer)}
        ranked: list[tuple[bool, float, str, dict[str, Any]]] = []
        for raw in candidates:
            value = copy.deepcopy(dict(raw))
            route_id = str(value.get(identity_key) or "")
            if not route_id:
                continue
            score = by_id.get(route_id)
            learned = score.score if score else 0.0
            penalty_active = bool(score and score.metadata.get("penalty_active"))
            available = bool(value.get(unavailable_key, True)) and not penalty_active
            value["recovery_memory_score"] = learned
            value["recovery_penalty_active"] = penalty_active
            value["recovery_evidence_refs"] = list(score.evidence_refs) if score else []
            ranked.append((not available, -learned, route_id, value))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        return tuple(item[3] for item in ranked)

    def route_penalties(self, task_id: str) -> dict[str, float]:
        return {score.route_id: score.score for score in self.scores(task_id) if score.score < 0}

    def _route_decision(self, plan_id: str, decision_id: str) -> LayeredRouteDecision | None:
        for decision in self.store.route_decisions(plan_id=plan_id):
            if not decision_id or decision.route_decision_id == decision_id:
                return decision
        return None

    @staticmethod
    def _route_refs(plan: RecoveryPlan, decision: LayeredRouteDecision | None) -> dict[str, str]:
        result = {
            "worker_id": plan.signal.refs.worker_id,
            "backend_id": plan.signal.refs.backend_id,
            "provider_id": plan.signal.refs.provider_id,
            "model_id": str(plan.signal.details.get("model_id") or ""),
        }
        if decision is None:
            return result
        keys = {
            RouteLayer.WORKER: "worker_id",
            RouteLayer.BACKEND: "backend_id",
            RouteLayer.PROVIDER: "provider_id",
            RouteLayer.MODEL: "model_id",
        }
        for change in decision.changes:
            key = keys.get(change.layer)
            if key is not None:
                result[key] = str(change.after_ref.get(key) or change.before_ref.get(key) or result[key])
        return result

    @staticmethod
    def _record_refs(record: RoutingMemoryRecord) -> tuple[tuple[RouteLayer, str], ...]:
        refs = (
            (RouteLayer.WORKER, record.worker_id),
            (RouteLayer.BACKEND, record.backend_id),
            (RouteLayer.PROVIDER, record.provider_id),
            (RouteLayer.MODEL, record.model_id),
        )
        return tuple((layer, route_id) for layer, route_id in refs if route_id)

    @staticmethod
    def _score_delta(outcome: RecoveryOutcome, decision: LayeredRouteDecision | None) -> float:
        base = 1.0 if outcome.success else -1.0
        if outcome.kind.value == "partial":
            base *= 0.45
        if outcome.kind.value == "waiting":
            base = -0.1
        if decision is not None:
            applied = sum(change.applied for change in decision.changes)
            rejected = sum(not bool(change.metadata.get("accepted", True)) for change in decision.changes)
            base += applied * (0.15 if outcome.success else -0.1)
            base -= rejected * 0.25
        return round(max(-3.0, min(3.0, base)), 6)

    @staticmethod
    def _age_weight(created_at: str) -> float:
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_days = max((datetime.now(UTC) - created).total_seconds() / 86400.0, 0.0)
        except (TypeError, ValueError):
            return 1.0
        return max(0.25, math.exp(-age_days / 30.0))

    @staticmethod
    def _future(value: str, now: datetime) -> bool:
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed > now
        except ValueError:
            return False


def routing_memory_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.routing-memory-feedback-contract/v1",
        "canonical_record_owner": "RecoveryPlanStore.routing_feedback",
        "downstream_sinks": ["event_log", "MemoryFabric", "worker/backend/provider owner metadata"],
        "selection_effect": "subsequent RecoveryContext memory_evidence and route candidate ordering",
        "signals": ["success", "failure", "partial", "waiting", "route-owner-rejection"],
        "invariants": [
            "feedback references the applied plan and concrete receipts",
            "negative outcomes create bounded route penalties",
            "expired penalties remain historical evidence but stop excluding a route",
            "feedback is derived from outcomes rather than model suggestions",
        ],
    }
