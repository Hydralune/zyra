from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .component_runtime import RecoveryComponent, RecoveryComponentControl
from .contracts import (
    LayeredRouteDecision,
    RecoveryAction,
    RecoveryContext,
    RecoveryPlan,
    RouteLayer,
    stable_digest,
    utc_now,
)
from .memory_feedback import RouteScore, RoutingMemoryFeedback
from .route_runtime import LayeredRouteRuntime, RouteOwnerRegistry, RouteOwnerUnavailable, RouteRuntimeError


class RouteMemoryError(RuntimeError):
    pass


class RouteIsolationError(RouteMemoryError):
    pass


class RouteEscalationError(RouteMemoryError):
    pass


@dataclass(frozen=True, slots=True)
class RouteLayerSnapshot:
    layer: RouteLayer
    owner: str
    value: Mapping[str, Any]
    available: bool
    observed_at: str = field(default_factory=utc_now)

    @property
    def digest(self) -> str:
        return stable_digest({
            "layer": self.layer.value,
            "owner": self.owner,
            "value": dict(self.value),
            "available": self.available,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "owner": self.owner,
            "value": copy.deepcopy(dict(self.value)),
            "available": self.available,
            "observed_at": self.observed_at,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class MemoryRouteCandidate:
    layer: RouteLayer
    route_id: str
    available: bool
    base_priority: float
    memory_score: float
    effective_priority: float
    penalty_active: bool
    evidence_refs: tuple[str, ...]
    candidate: Mapping[str, Any]
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "route_id": self.route_id,
            "available": self.available,
            "base_priority": self.base_priority,
            "memory_score": self.memory_score,
            "effective_priority": self.effective_priority,
            "penalty_active": self.penalty_active,
            "evidence_refs": list(self.evidence_refs),
            "candidate": copy.deepcopy(dict(self.candidate)),
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class RouteIsolationReport:
    plan_id: str
    action: RecoveryAction
    expected_layers: tuple[RouteLayer, ...]
    changed_layers: tuple[RouteLayer, ...]
    stable_layers: tuple[RouteLayer, ...]
    unavailable_layers: tuple[RouteLayer, ...]
    explicit_escalation: bool
    violations: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    def require_ok(self) -> "RouteIsolationReport":
        if self.violations:
            raise RouteIsolationError("; ".join(self.violations))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-route-isolation-report/v1",
            "plan_id": self.plan_id,
            "action": self.action.value,
            "expected_layers": [item.value for item in self.expected_layers],
            "changed_layers": [item.value for item in self.changed_layers],
            "stable_layers": [item.value for item in self.stable_layers],
            "unavailable_layers": [item.value for item in self.unavailable_layers],
            "explicit_escalation": self.explicit_escalation,
            "violations": list(self.violations),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class EscalationStep:
    index: int
    action: RecoveryAction
    layer: RouteLayer
    reason: str
    requires_prior_failure: RecoveryAction | None
    maximum_attempts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action.value,
            "layer": self.layer.value,
            "reason": self.reason,
            "requires_prior_failure": self.requires_prior_failure.value if self.requires_prior_failure else "",
            "maximum_attempts": self.maximum_attempts,
        }


@dataclass(frozen=True, slots=True)
class ExplicitEscalationPlan:
    escalation_id: str
    run_id: str
    task_id: str
    source_plan_id: str
    source_action: RecoveryAction
    steps: tuple[EscalationStep, ...]
    failed_owner_receipt_ids: tuple[str, ...]
    policy_rule: str
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.explicit-route-escalation-plan/v1",
            "escalation_id": self.escalation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "source_plan_id": self.source_plan_id,
            "source_action": self.source_action.value,
            "steps": [item.to_dict() for item in self.steps],
            "failed_owner_receipt_ids": list(self.failed_owner_receipt_ids),
            "policy_rule": self.policy_rule,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


class MemoryAwareRouteRuntime:
    ACTION_COMPONENT: Mapping[RecoveryAction, RecoveryComponent] = {
        RecoveryAction.REPLAN: RecoveryComponent.GRAPH_ROUTER,
        RecoveryAction.REROUTE: RecoveryComponent.WORKER_ROUTER,
        RecoveryAction.SWITCH_BACKEND: RecoveryComponent.BACKEND_ROUTER,
        RecoveryAction.SWITCH_PROVIDER: RecoveryComponent.PROVIDER_ROUTER,
        RecoveryAction.DEGRADE_MODEL: RecoveryComponent.PROVIDER_ROUTER,
    }
    IDENTITY_KEYS: Mapping[RouteLayer, tuple[str, ...]] = {
        RouteLayer.GRAPH: ("commit_id", "revision", "graph_id"),
        RouteLayer.WORKER: ("lease_id", "worker_lease_id", "worker_id", "attempt_id"),
        RouteLayer.BACKEND: ("backend_lease_id", "lease_id", "backend_id"),
        RouteLayer.WORKSPACE: ("workspace_id", "root", "revision"),
        RouteLayer.PROVIDER: ("route_id", "provider_route_id", "provider_id"),
        RouteLayer.MODEL: ("model_id", "route_id", "provider_id"),
        RouteLayer.CREDENTIAL: ("credential_id", "route_id", "provider_id"),
        RouteLayer.TRANSPORT: ("transport_id", "route_id", "provider_id"),
    }

    def __init__(
        self,
        routes: LayeredRouteRuntime,
        feedback: RoutingMemoryFeedback,
        *,
        components: RecoveryComponentControl | None = None,
        penalty_threshold: float = -0.25,
        maximum_penalized_exclusions: int = 32,
    ) -> None:
        self.routes = routes
        self.feedback = feedback
        self.components = components or RecoveryComponentControl()
        self.penalty_threshold = float(penalty_threshold)
        self.maximum_penalized_exclusions = int(maximum_penalized_exclusions)
        if self.maximum_penalized_exclusions < 1:
            raise ValueError("maximum penalized route exclusions must be positive")

    @property
    def owners(self) -> RouteOwnerRegistry:
        return self.routes.owners

    @property
    def store(self) -> Any:
        return self.routes.store

    def apply(
        self,
        plan: RecoveryPlan,
        action: RecoveryAction,
        *,
        constraints: Mapping[str, Any] | None = None,
        excluded_refs: Sequence[str] = (),
        layers: Sequence[RouteLayer] | None = None,
        stop_after_first_change: bool = False,
    ) -> LayeredRouteDecision:
        component = self.ACTION_COMPONENT.get(action)
        if component is None:
            raise RouteMemoryError(f"{action.value} is not a memory-aware route action")
        self.components.require(component, operation=f"apply {action.value} recovery route")
        requested = tuple(layers or self.routes.ACTION_LAYERS.get(action, ()))
        if not requested:
            raise RouteMemoryError(f"{action.value} has no route layer")
        explicit = bool(plan.provenance.get("explicit_escalation") or plan.signal.details.get("explicit_escalation"))
        if len(requested) > 1 and not explicit:
            raise RouteEscalationError("a multi-layer route request requires explicit escalation")
        before = self.snapshot(plan.signal.refs.run_id, plan.signal.refs.task_id)
        memory_constraints, memory_exclusions = self._memory_policy(plan, requested, constraints or {})
        decision = self.routes.apply(
            plan,
            action,
            constraints=memory_constraints,
            excluded_refs=tuple(dict.fromkeys((*excluded_refs, *memory_exclusions))),
            layers=requested,
            stop_after_first_change=stop_after_first_change,
        )
        after = self.snapshot(plan.signal.refs.run_id, plan.signal.refs.task_id)
        report = self.audit_isolation(plan, decision, before, after, explicit_escalation=explicit)
        report.require_ok()
        return decision

    def current_route(self, run_id: str, task_id: str) -> dict[str, Any]:
        return self.routes.current_route(run_id, task_id)

    def alternatives(
        self,
        action: RecoveryAction,
        *,
        failed_layers: Sequence[RouteLayer],
    ) -> tuple[RecoveryAction, ...]:
        return self.routes.alternatives(action, failed_layers=failed_layers)

    def snapshot(self, run_id: str, task_id: str) -> tuple[RouteLayerSnapshot, ...]:
        result: list[RouteLayerSnapshot] = []
        for layer in RouteLayer:
            try:
                owner = self.owners.require(layer)
                value = copy.deepcopy(dict(owner.current(run_id, task_id)))
                result.append(RouteLayerSnapshot(layer, owner.owner, value, True))
            except RouteOwnerUnavailable:
                result.append(RouteLayerSnapshot(layer, "unregistered", {}, False))
        return tuple(result)

    def rank_candidates(
        self,
        task_id: str,
        layer: RouteLayer,
        candidates: Sequence[Mapping[str, Any]],
        *,
        identity_keys: Sequence[str] | None = None,
    ) -> tuple[MemoryRouteCandidate, ...]:
        keys = tuple(identity_keys or self.IDENTITY_KEYS[layer])
        scores = {item.route_id: item for item in self.feedback.scores(task_id, layer=layer)}
        result: list[MemoryRouteCandidate] = []
        for raw in candidates:
            value = copy.deepcopy(dict(raw))
            route_id = self._route_id(value, keys)
            if not route_id:
                continue
            score = scores.get(route_id)
            memory_score = score.score if score else 0.0
            penalty_active = bool(score and score.metadata.get("penalty_active"))
            available = bool(value.get("available", True)) and not penalty_active
            base = self._priority(value)
            cost_penalty = max(0.0, float(value.get("estimated_cost") or value.get("cost") or 0.0))
            latency_penalty = max(0.0, float(value.get("latency_ms") or 0.0)) / 10_000.0
            effective = base + memory_score - cost_penalty - latency_penalty
            blockers: list[str] = []
            if not bool(value.get("available", True)):
                blockers.append("owner reports route unavailable")
            if penalty_active:
                blockers.append("active recovery memory penalty")
            result.append(MemoryRouteCandidate(
                layer=layer,
                route_id=route_id,
                available=available,
                base_priority=base,
                memory_score=memory_score,
                effective_priority=round(effective, 6),
                penalty_active=penalty_active,
                evidence_refs=score.evidence_refs if score else (),
                candidate=value,
                blockers=tuple(blockers),
            ))
        return tuple(sorted(result, key=lambda item: (
            not item.available,
            -item.effective_priority,
            item.route_id,
        )))

    def audit_isolation(
        self,
        plan: RecoveryPlan,
        decision: LayeredRouteDecision,
        before: Sequence[RouteLayerSnapshot],
        after: Sequence[RouteLayerSnapshot],
        *,
        explicit_escalation: bool,
    ) -> RouteIsolationReport:
        before_by_layer = {item.layer: item for item in before}
        after_by_layer = {item.layer: item for item in after}
        expected = tuple(change.layer for change in decision.changes if change.requested)
        changed: list[RouteLayer] = []
        stable: list[RouteLayer] = []
        unavailable: list[RouteLayer] = []
        violations: list[str] = []
        for layer in RouteLayer:
            left = before_by_layer[layer]
            right = after_by_layer[layer]
            if not left.available or not right.available:
                unavailable.append(layer)
                continue
            if left.digest != right.digest:
                changed.append(layer)
            else:
                stable.append(layer)
        applied = tuple(change.layer for change in decision.changes if change.applied)
        for layer in applied:
            if layer not in changed:
                violations.append(f"route receipt claims {layer.value} changed but owner projection is stable")
        for layer in changed:
            if layer not in expected:
                violations.append(f"unrequested route layer changed: {layer.value}")
        if len(changed) > 1 and not explicit_escalation:
            violations.append("cross-layer route mutation lacks explicit escalation")
        selected_layer = plan.decision.selected.route_layer
        selected_layers = (
            (selected_layer,)
            if selected_layer is not None
            else self.routes.ACTION_LAYERS.get(plan.decision.selected.action, ())
        )
        for layer in selected_layers:
            if layer not in applied:
                violations.append(f"selected route layer was not applied: {layer.value}")
        return RouteIsolationReport(
            plan_id=plan.plan_id,
            action=decision.action,
            expected_layers=expected,
            changed_layers=tuple(changed),
            stable_layers=tuple(stable),
            unavailable_layers=tuple(unavailable),
            explicit_escalation=explicit_escalation,
            violations=tuple(violations),
        )

    def build_escalation(
        self,
        plan: RecoveryPlan,
        *,
        failed_owner_receipt_ids: Sequence[str],
        allowed_actions: Sequence[RecoveryAction],
        reason: str,
    ) -> ExplicitEscalationPlan:
        if not failed_owner_receipt_ids:
            raise RouteEscalationError("escalation requires a failed canonical owner receipt")
        policy_alternatives = self.routes.alternatives(
            plan.decision.selected.action,
            failed_layers=self.routes.ACTION_LAYERS.get(plan.decision.selected.action, ()),
        )
        permitted = [item for item in policy_alternatives if item in allowed_actions]
        if not permitted:
            raise RouteEscalationError("RecoveryDecisionRuntime policy did not authorize an escalation action")
        steps: list[EscalationStep] = []
        previous = plan.decision.selected.action
        for index, action in enumerate(permitted, start=1):
            layers = self.routes.ACTION_LAYERS.get(action, ())
            if len(layers) != 1:
                continue
            steps.append(EscalationStep(
                index=index,
                action=action,
                layer=layers[0],
                reason=str(reason)[:1000],
                requires_prior_failure=previous,
                maximum_attempts=1,
            ))
            previous = action
        escalation_id = "routeescalation:" + stable_digest({
            "plan_id": plan.plan_id,
            "failed_receipts": sorted(failed_owner_receipt_ids),
            "steps": [item.to_dict() for item in steps],
        })[:40]
        return ExplicitEscalationPlan(
            escalation_id=escalation_id,
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            source_plan_id=plan.plan_id,
            source_action=plan.decision.selected.action,
            steps=tuple(steps),
            failed_owner_receipt_ids=tuple(dict.fromkeys(str(item) for item in failed_owner_receipt_ids)),
            policy_rule=plan.decision.selected.policy_rule,
            metadata={
                "decision_owner": "RecoveryDecisionRuntime",
                "route_owner_selected_here": False,
            },
        )

    def _memory_policy(
        self,
        plan: RecoveryPlan,
        layers: Sequence[RouteLayer],
        constraints: Mapping[str, Any],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        scores = self.feedback.scores(plan.signal.refs.task_id)
        relevant = [item for item in scores if item.layer in layers]
        exclusions = [
            item.route_id for item in relevant
            if item.score <= self.penalty_threshold and bool(item.metadata.get("penalty_active"))
        ][: self.maximum_penalized_exclusions]
        result = copy.deepcopy(dict(constraints))
        result["recovery_memory"] = {
            "task_id": plan.signal.refs.task_id,
            "layers": [item.value for item in layers],
            "scores": [item.to_dict() for item in relevant],
            "excluded_route_ids": exclusions,
            "selection_owner": "canonical route owner",
            "policy_owner": "RecoveryDecisionRuntime",
        }
        return result, tuple(exclusions)

    @staticmethod
    def _route_id(value: Mapping[str, Any], keys: Sequence[str]) -> str:
        for key in keys:
            candidate = str(value.get(key) or "")
            if candidate:
                return candidate
        return ""

    @staticmethod
    def _priority(value: Mapping[str, Any]) -> float:
        for key in ("priority", "score", "weight"):
            raw = value.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return float(raw)
        return 0.0


def route_memory_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-route-memory-runtime-contract/v1",
        "route_layers": [item.value for item in RouteLayer],
        "default_mutation": "one route layer",
        "cross_layer_mutation": "explicit escalation only",
        "memory_effect": "penalized routes are excluded and remaining candidates are reordered",
        "lease_owner": "canonical graph/worker/backend/provider owner",
        "decision_owner": "RecoveryDecisionRuntime",
    }


__all__ = [
    "EscalationStep",
    "ExplicitEscalationPlan",
    "MemoryAwareRouteRuntime",
    "MemoryRouteCandidate",
    "RouteEscalationError",
    "RouteIsolationError",
    "RouteIsolationReport",
    "RouteLayerSnapshot",
    "RouteMemoryError",
    "route_memory_runtime_contract",
]
