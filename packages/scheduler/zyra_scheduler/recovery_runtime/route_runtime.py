from __future__ import annotations

import copy
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .contracts import (
    LayeredRouteDecision,
    RecoveryAction,
    RecoveryPlan,
    RouteLayer,
    RouteLayerChange,
    stable_digest,
)
from .store import RecoveryPlanStore


class RouteRuntimeError(RuntimeError):
    pass


class RouteOwnerUnavailable(RouteRuntimeError):
    pass


class RouteOwnerRejected(RouteRuntimeError):
    def __init__(self, layer: RouteLayer, code: str, message: str) -> None:
        self.layer = layer
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RouteRequest:
    run_id: str
    task_id: str
    plan_id: str
    signal_id: str
    action: RecoveryAction
    layer: RouteLayer
    current_ref: Mapping[str, Any]
    constraints: Mapping[str, Any] = field(default_factory=dict)
    excluded_refs: tuple[str, ...] = ()
    reason: str = ""
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-route-request/v1",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "signal_id": self.signal_id,
            "action": self.action.value,
            "layer": self.layer.value,
            "current_ref": copy.deepcopy(dict(self.current_ref)),
            "constraints": copy.deepcopy(dict(self.constraints)),
            "excluded_refs": list(self.excluded_refs),
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class RouteOwnerReceipt:
    layer: RouteLayer
    owner: str
    accepted: bool
    changed: bool
    before_ref: Mapping[str, Any]
    after_ref: Mapping[str, Any]
    receipt_id: str
    error_code: str = ""
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "owner": self.owner,
            "accepted": self.accepted,
            "changed": self.changed,
            "before_ref": copy.deepcopy(dict(self.before_ref)),
            "after_ref": copy.deepcopy(dict(self.after_ref)),
            "receipt_id": self.receipt_id,
            "error_code": self.error_code,
            "message": self.message,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


class RouteOwnerPort(Protocol):
    layer: RouteLayer
    owner: str

    def current(self, run_id: str, task_id: str) -> Mapping[str, Any]: ...

    def route(self, request: RouteRequest) -> RouteOwnerReceipt: ...


class CallbackRouteOwner:
    def __init__(
        self,
        layer: RouteLayer,
        owner: str,
        *,
        current: Callable[[str, str], Mapping[str, Any]],
        route: Callable[[RouteRequest], Mapping[str, Any] | RouteOwnerReceipt],
    ) -> None:
        if not owner.strip():
            raise ValueError("route owner is required")
        self.layer = layer
        self.owner = owner
        self._current = current
        self._route = route

    def current(self, run_id: str, task_id: str) -> Mapping[str, Any]:
        return copy.deepcopy(dict(self._current(run_id, task_id)))

    def route(self, request: RouteRequest) -> RouteOwnerReceipt:
        if request.layer is not self.layer:
            raise RouteRuntimeError(
                f"owner {self.owner} for {self.layer.value} received {request.layer.value} request"
            )
        result = self._route(request)
        if isinstance(result, RouteOwnerReceipt):
            if result.layer is not self.layer:
                raise RouteRuntimeError("route owner returned a receipt for another layer")
            return result
        value = dict(result)
        before_ref = dict(value.get("before_ref") or request.current_ref)
        after_ref = dict(value.get("after_ref") or before_ref)
        accepted = bool(value.get("accepted", not value.get("error_code")))
        changed = bool(value.get("changed", accepted and before_ref != after_ref))
        receipt_id = str(value.get("receipt_id") or stable_digest({
            "owner": self.owner,
            "request": request.to_dict(),
            "after_ref": after_ref,
        }))
        return RouteOwnerReceipt(
            layer=self.layer,
            owner=self.owner,
            accepted=accepted,
            changed=changed,
            before_ref=before_ref,
            after_ref=after_ref,
            receipt_id=receipt_id,
            error_code=str(value.get("error_code") or ""),
            message=str(value.get("message") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


class RouteOwnerRegistry:
    def __init__(self, owners: Sequence[RouteOwnerPort] = ()) -> None:
        self._owners: dict[RouteLayer, RouteOwnerPort] = {}
        for owner in owners:
            self.register(owner)

    def register(self, owner: RouteOwnerPort) -> None:
        if owner.layer in self._owners:
            raise ValueError(f"route layer already has an owner: {owner.layer.value}")
        self._owners[owner.layer] = owner

    def replace(self, owner: RouteOwnerPort) -> None:
        self._owners[owner.layer] = owner

    def require(self, layer: RouteLayer) -> RouteOwnerPort:
        owner = self._owners.get(layer)
        if owner is None:
            raise RouteOwnerUnavailable(f"no route owner registered for {layer.value}")
        return owner

    def current(self, layer: RouteLayer, run_id: str, task_id: str) -> Mapping[str, Any]:
        return self.require(layer).current(run_id, task_id)

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-route-owner-registry/v1",
            "owners": {
                layer.value: owner.owner
                for layer, owner in sorted(self._owners.items(), key=lambda item: item[0].value)
            },
            "canonical_state_owner_is_external": True,
            "registry_is_not_a_route_store": True,
        }


class LayeredRouteRuntime:
    ACTION_LAYERS: Mapping[RecoveryAction, tuple[RouteLayer, ...]] = {
        RecoveryAction.REROUTE: (RouteLayer.WORKER,),
        RecoveryAction.SWITCH_BACKEND: (RouteLayer.BACKEND,),
        RecoveryAction.SWITCH_PROVIDER: (RouteLayer.PROVIDER,),
        RecoveryAction.DEGRADE_MODEL: (RouteLayer.MODEL,),
        RecoveryAction.REPLAN: (RouteLayer.GRAPH,),
    }

    def __init__(self, store: RecoveryPlanStore, owners: RouteOwnerRegistry) -> None:
        self.store = store
        self.owners = owners

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
        if os.environ.get("ZYRA_LAYERED_SCHEDULER_DISABLED", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise RouteRuntimeError("layered_scheduler_disabled")
        requested_layers = tuple(layers or self.ACTION_LAYERS.get(action, ()))
        if not requested_layers:
            raise RouteRuntimeError(f"{action.value} is not a layered route action")
        prior = self.store.route_decisions(plan_id=plan.plan_id)
        deterministic_id = stable_digest({
            "plan_id": plan.plan_id,
            "action": action.value,
            "cursor": plan.action_cursor,
            "layers": [layer.value for layer in requested_layers],
            "constraints": dict(constraints or {}),
            "excluded_refs": sorted(str(item) for item in excluded_refs),
        })
        for decision in prior:
            if str(decision.metadata.get("request_digest") or "") == deterministic_id:
                return decision
        changes: list[RouteLayerChange] = []
        errors: list[dict[str, str]] = []
        for layer in requested_layers:
            owner = self.owners.require(layer)
            before = owner.current(plan.signal.refs.run_id, plan.signal.refs.task_id)
            request = RouteRequest(
                run_id=plan.signal.refs.run_id,
                task_id=plan.signal.refs.task_id,
                plan_id=plan.plan_id,
                signal_id=plan.signal.signal_id,
                action=action,
                layer=layer,
                current_ref=before,
                constraints=dict(constraints or {}),
                excluded_refs=tuple(str(item) for item in excluded_refs),
                reason=plan.decision.rationale,
                idempotency_key=f"route:{deterministic_id}:{layer.value}",
            )
            receipt = owner.route(request)
            change = RouteLayerChange(
                layer=layer,
                owner=receipt.owner,
                before_ref=receipt.before_ref,
                after_ref=receipt.after_ref,
                requested=True,
                applied=receipt.accepted and receipt.changed,
                receipt_id=receipt.receipt_id,
                reason=receipt.message or plan.decision.rationale,
                metadata={
                    **dict(receipt.metadata),
                    "accepted": receipt.accepted,
                    "error_code": receipt.error_code,
                },
            )
            changes.append(change)
            if not receipt.accepted:
                errors.append({"layer": layer.value, "code": receipt.error_code, "message": receipt.message})
            if stop_after_first_change and change.applied:
                break
        decision = LayeredRouteDecision(
            route_decision_id=f"route_{deterministic_id[:40]}",
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            plan_id=plan.plan_id,
            action=action,
            trigger_signal_id=plan.signal.signal_id,
            policy_rule=plan.decision.selected.policy_rule,
            changes=tuple(changes),
            escalation=self._escalation(changes, errors),
            metadata={
                "request_digest": deterministic_id,
                "errors": errors,
                "changed_execution": any(change.applied for change in changes),
                "owner_count": len(changes),
                "route_state_is_owned_by_registered_ports": True,
            },
        )
        stored, _ = self.store.append_route_decision(decision)
        if errors and not any(change.applied for change in stored.changes):
            first = errors[0]
            raise RouteOwnerRejected(RouteLayer(first["layer"]), first["code"], first["message"])
        return stored

    def current_route(self, run_id: str, task_id: str) -> dict[str, Any]:
        route: dict[str, Any] = {}
        for layer in RouteLayer:
            try:
                route[layer.value] = dict(self.owners.current(layer, run_id, task_id))
            except RouteOwnerUnavailable:
                route[layer.value] = {"available": False}
        return route

    def alternatives(
        self,
        action: RecoveryAction,
        *,
        failed_layers: Sequence[RouteLayer],
    ) -> tuple[RecoveryAction, ...]:
        failed = set(failed_layers)
        alternatives: list[RecoveryAction] = []
        if action is RecoveryAction.REROUTE and RouteLayer.WORKER in failed:
            alternatives.extend((RecoveryAction.SWITCH_BACKEND, RecoveryAction.REPLAN))
        elif action is RecoveryAction.SWITCH_BACKEND:
            alternatives.extend((RecoveryAction.SWITCH_PROVIDER, RecoveryAction.REPLAN))
        elif action is RecoveryAction.SWITCH_PROVIDER:
            alternatives.extend((RecoveryAction.DEGRADE_MODEL, RecoveryAction.REPLAN))
        elif action is RecoveryAction.DEGRADE_MODEL:
            alternatives.extend((RecoveryAction.SWITCH_PROVIDER, RecoveryAction.REPLAN))
        elif action is RecoveryAction.REPLAN:
            alternatives.append(RecoveryAction.ABORT)
        return tuple(dict.fromkeys(alternatives))

    @staticmethod
    def _escalation(changes: Sequence[RouteLayerChange], errors: Sequence[Mapping[str, str]]) -> str:
        if not errors:
            return "none"
        if any(change.applied for change in changes):
            return "partial_route_applied"
        return "route_owner_rejected"


class InMemoryRouteOwner:
    def __init__(
        self,
        layer: RouteLayer,
        owner: str,
        *,
        candidates: Sequence[Mapping[str, Any]],
        identity_key: str,
    ) -> None:
        self.layer = layer
        self.owner = owner
        self.identity_key = identity_key
        self._candidates = [copy.deepcopy(dict(item)) for item in candidates]
        self._current: dict[tuple[str, str], dict[str, Any]] = {}

    def seed(self, run_id: str, task_id: str, value: Mapping[str, Any]) -> None:
        self._current[(run_id, task_id)] = copy.deepcopy(dict(value))

    def current(self, run_id: str, task_id: str) -> Mapping[str, Any]:
        return copy.deepcopy(self._current.get((run_id, task_id), {}))

    def route(self, request: RouteRequest) -> RouteOwnerReceipt:
        excluded = set(request.excluded_refs)
        current_id = str(request.current_ref.get(self.identity_key) or "")
        candidate = next(
            (
                item
                for item in self._candidates
                if str(item.get(self.identity_key) or "") not in excluded
                and str(item.get(self.identity_key) or "") != current_id
                and bool(item.get("available", True))
            ),
            None,
        )
        if candidate is None:
            return RouteOwnerReceipt(
                layer=self.layer,
                owner=self.owner,
                accepted=False,
                changed=False,
                before_ref=request.current_ref,
                after_ref=request.current_ref,
                receipt_id=stable_digest({"request": request.to_dict(), "result": "unavailable"}),
                error_code="route_candidate_unavailable",
                message=f"no eligible {self.layer.value} route candidate",
            )
        after = copy.deepcopy(candidate)
        self._current[(request.run_id, request.task_id)] = after
        return RouteOwnerReceipt(
            layer=self.layer,
            owner=self.owner,
            accepted=True,
            changed=dict(request.current_ref) != after,
            before_ref=request.current_ref,
            after_ref=after,
            receipt_id=stable_digest({"request": request.to_dict(), "after": after}),
            message=f"{self.layer.value} owner applied successor route",
        )


def route_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.layered-recovery-route-contract/v1",
        "planner_owner": "python.RecoveryDecisionRuntime",
        "route_orchestrator": "python.LayeredRouteRuntime",
        "canonical_owners": {
            "graph": "GraphStateCustody",
            "worker": "WorkerPoolFoundationRuntime",
            "backend": "BackendRegistry",
            "workspace": "WorkspaceManager",
            "provider": "ProviderControlPlane",
            "model": "ProviderControlPlane",
            "credential": "CredentialStore",
            "transport": "ProviderStreamRuntime",
        },
        "invariants": [
            "one owner per route layer",
            "recovery runtime stores decision receipts but not canonical route state",
            "requirement changes enter graph replan rather than failure retry",
            "worker backend and provider faults request leases from their respective owner",
            "a language model cannot apply a route",
        ],
    }
