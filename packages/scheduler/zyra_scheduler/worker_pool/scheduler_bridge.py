from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .integration_models import (
    AdmissionResult,
    CapacityObservation,
    DispatchAdmissionRequest,
    DispatchMode,
    RouteHealth,
    make_foreign_refs,
)
from .models import CapabilityRequirement, ResourceVector, WorkerLocation, stable_digest


def _signal_values(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, Mapping):
        return tuple(value)
    return ()


@dataclass(frozen=True, slots=True)
class MemoryRoutingDirective:
    signal_id: str
    kind: str
    priority: str
    preferred_worker_ids: tuple[str, ...] = ()
    excluded_worker_ids: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    forbidden_capabilities: tuple[str, ...] = ()
    allowed_locations: tuple[WorkerLocation, ...] = ()
    context_epoch: int = 0
    reason: str = ""
    payload_digest: str = ""

    @classmethod
    def from_signal(cls, value: Mapping[str, Any]) -> "MemoryRoutingDirective":
        payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
        raw_locations = _signal_values(
            payload.get("allowed_locations") or payload.get("locations")
        )
        locations: list[WorkerLocation] = []
        for item in raw_locations:
            try:
                locations.append(WorkerLocation(str(item)))
            except ValueError:
                continue
        privacy = str(payload.get("privacy") or payload.get("privacy_class") or "").lower()
        if privacy in {"private", "secret", "local_only"}:
            locations = [WorkerLocation.LOCAL]
        elif privacy in {"edge_only", "device_private"}:
            locations = [WorkerLocation.EDGE]
        signal_id = str(value.get("signalId") or value.get("signal_id") or "")
        kind = str(value.get("kind") or "unknown")
        return cls(
            signal_id=signal_id,
            kind=kind,
            priority=str(value.get("priority") or "normal"),
            preferred_worker_ids=tuple(
                sorted(set(str(item) for item in _signal_values(payload.get("preferred_worker_ids")) if str(item)))
            ),
            excluded_worker_ids=tuple(
                sorted(set(str(item) for item in _signal_values(payload.get("excluded_worker_ids")) if str(item)))
            ),
            required_capabilities=tuple(
                sorted(set(str(item) for item in _signal_values(payload.get("required_capabilities")) if str(item)))
            ),
            forbidden_capabilities=tuple(
                sorted(set(str(item) for item in _signal_values(payload.get("forbidden_capabilities")) if str(item)))
            ),
            allowed_locations=tuple(sorted(set(locations), key=lambda item: item.value)),
            context_epoch=max(0, int(payload.get("context_epoch") or value.get("sequence") or 0)),
            reason=str(payload.get("reason") or f"memory signal {kind}"),
            payload_digest=str(value.get("payloadDigest") or value.get("payload_digest") or stable_digest(payload)),
        )

    @property
    def routing_effect(self) -> bool:
        return bool(
            self.preferred_worker_ids
            or self.excluded_worker_ids
            or self.required_capabilities
            or self.forbidden_capabilities
            or self.allowed_locations
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "kind": self.kind,
            "priority": self.priority,
            "preferred_worker_ids": list(self.preferred_worker_ids),
            "excluded_worker_ids": list(self.excluded_worker_ids),
            "required_capabilities": list(self.required_capabilities),
            "forbidden_capabilities": list(self.forbidden_capabilities),
            "allowed_locations": [item.value for item in self.allowed_locations],
            "context_epoch": self.context_epoch,
            "reason": self.reason,
            "payload_digest": self.payload_digest,
            "routing_effect": self.routing_effect,
        }


@dataclass(frozen=True, slots=True)
class SchedulerDispatchContext:
    task_id: str
    run_id: str
    owner_session_id: str
    logical_attempt: int
    logical_task_revision: int
    graph_id: str
    graph_revision: int
    graph_node_id: str
    workspace_ref: str
    gateway_ref: str
    backend_route_id: str
    required_capabilities: tuple[str, ...]
    tool_ids: tuple[str, ...] = ()
    backend_kinds: tuple[str, ...] = ()
    locations: tuple[WorkerLocation, ...] = ()
    resources: ResourceVector = field(default_factory=ResourceVector)
    preferred_worker_ids: tuple[str, ...] = ()
    excluded_worker_ids: tuple[str, ...] = ()
    execution_mode: DispatchMode = DispatchMode.BACKGROUND
    edge_only: bool = False
    lease_ttl_seconds: float = 30.0
    checkpoint_ref: str = ""
    route_health: RouteHealth = RouteHealth.HEALTHY
    idempotency_key: str = ""
    causation_id: str = ""
    correlation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "owner_session_id": self.owner_session_id,
            "graph_id": self.graph_id,
            "graph_node_id": self.graph_node_id,
            "workspace_ref": self.workspace_ref,
            "gateway_ref": self.gateway_ref,
            "backend_route_id": self.backend_route_id,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"scheduler dispatch context is missing: {', '.join(missing)}")
        if min(self.logical_attempt, max(1, self.logical_task_revision + 1), max(1, self.graph_revision)) < 1:
            raise ValueError("scheduler dispatch revisions are invalid")
        if self.edge_only and self.locations and set(self.locations) != {WorkerLocation.EDGE}:
            raise ValueError("edge-only scheduler context cannot include local/cloud locations")
        object.__setattr__(self, "required_capabilities", tuple(sorted(set(self.required_capabilities))))
        object.__setattr__(self, "tool_ids", tuple(sorted(set(self.tool_ids))))
        object.__setattr__(self, "backend_kinds", tuple(sorted(set(self.backend_kinds))))
        object.__setattr__(self, "locations", tuple(sorted(set(self.locations), key=lambda item: item.value)))
        object.__setattr__(self, "preferred_worker_ids", tuple(sorted(set(self.preferred_worker_ids))))
        object.__setattr__(self, "excluded_worker_ids", tuple(sorted(set(self.excluded_worker_ids))))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class SchedulerAdmissionPlan:
    request: DispatchAdmissionRequest
    candidates: tuple[CapacityObservation, ...]
    directives: tuple[MemoryRoutingDirective, ...]
    selected_worker_id: str
    accepted: bool
    reasons: tuple[str, ...]
    plan_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "directives": [item.to_dict() for item in self.directives],
            "selected_worker_id": self.selected_worker_id,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "plan_digest": self.plan_digest,
        }


class WorkerPoolSchedulerBridge:
    """Deterministic scheduler/backend/memory input bridge into 07A admission."""

    def __init__(self, integration: Any) -> None:
        self.integration = integration

    def plan(
        self,
        context: SchedulerDispatchContext,
        *,
        memory_signals: Sequence[Mapping[str, Any]] = (),
    ) -> SchedulerAdmissionPlan:
        directives = tuple(
            sorted(
                (MemoryRoutingDirective.from_signal(item) for item in memory_signals),
                key=lambda item: (self._priority(item.priority), item.context_epoch, item.signal_id),
                reverse=True,
            )
        )
        required = set(context.required_capabilities)
        forbidden: set[str] = set()
        preferred = set(context.preferred_worker_ids)
        excluded = set(context.excluded_worker_ids)
        locations = set(context.locations)
        location_conflict = False
        routing_reasons: list[str] = []
        for directive in directives:
            if not directive.routing_effect:
                continue
            required.update(directive.required_capabilities)
            forbidden.update(directive.forbidden_capabilities)
            preferred.update(directive.preferred_worker_ids)
            excluded.update(directive.excluded_worker_ids)
            if directive.allowed_locations:
                requested = set(directive.allowed_locations)
                if locations:
                    locations = locations.intersection(requested)
                    location_conflict = location_conflict or not locations
                else:
                    locations = requested
            routing_reasons.append(directive.reason)
        if context.edge_only:
            locations = {WorkerLocation.EDGE}
        latest_signal = max(directives, key=lambda item: item.context_epoch, default=None)
        requirement = CapabilityRequirement(
            required=tuple(sorted(required)),
            forbidden=tuple(sorted(forbidden)),
            tool_ids=context.tool_ids,
            backend_kinds=context.backend_kinds,
            locations=tuple(sorted(locations, key=lambda item: item.value)),
            resources=context.resources,
            # CapabilityRequirement.labels are hard manifest constraints.  Route
            # and memory provenance belong in admission metadata; treating them
            # as labels would reject every otherwise capable worker.
            labels={},
        )
        request = DispatchAdmissionRequest(
            task_id=context.task_id,
            run_id=context.run_id,
            owner_session_id=context.owner_session_id,
            logical_attempt=context.logical_attempt,
            attempt_number=context.logical_attempt,
            requirement=requirement,
            foreign_refs=make_foreign_refs(
                task_id=context.task_id,
                task_revision=context.logical_task_revision,
                backend_route_id=context.backend_route_id,
                workspace_ref=context.workspace_ref,
                gateway_ref=context.gateway_ref,
                graph_id=context.graph_id,
                graph_revision=context.graph_revision,
                checkpoint_ref=context.checkpoint_ref,
                memory_signal_ref=latest_signal.signal_id if latest_signal else "",
            ),
            execution_mode=context.execution_mode,
            preferred_worker_ids=tuple(sorted(preferred)),
            excluded_worker_ids=tuple(sorted(excluded)),
            lease_ttl_seconds=context.lease_ttl_seconds,
            edge_only=context.edge_only,
            idempotency_key=context.idempotency_key or f"scheduler-admit:{context.task_id}:{context.logical_attempt}",
            causation_id=context.causation_id or f"scheduler-route:{context.task_id}",
            correlation_id=context.correlation_id or context.owner_session_id,
            metadata={
                **dict(context.metadata),
                "graph_node_id": context.graph_node_id,
                "scheduler_route_health": context.route_health.value,
                "memory_context_epoch": latest_signal.context_epoch if latest_signal else 0,
                "memory_directives": [item.to_dict() for item in directives],
                "scheduler_input_bridge": True,
            },
        )
        candidates = self.integration.admission.capacity.observe(request)
        selected = next((item for item in candidates if item.accepted), None)
        reasons = list(routing_reasons)
        if location_conflict:
            reasons.append("memory and scheduler location fences have an empty intersection")
            selected = None
        if not context.route_health.dispatchable:
            reasons.append(f"declared backend route is {context.route_health.value}")
            selected = None
        if selected is None:
            reasons.append("no worker satisfies scheduler capacity and route constraints")
        payload = {
            "request": request.to_dict(),
            "candidates": [item.to_dict() for item in candidates],
            "directives": [item.to_dict() for item in directives],
            "selected_worker_id": selected.worker_id if selected else "",
            "accepted": selected is not None,
            "reasons": reasons,
        }
        return SchedulerAdmissionPlan(
            request=request,
            candidates=candidates,
            directives=directives,
            selected_worker_id=selected.worker_id if selected else "",
            accepted=selected is not None,
            reasons=tuple(reasons),
            plan_digest=stable_digest(payload),
        )

    def admit(
        self,
        context: SchedulerDispatchContext,
        *,
        memory_signals: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[SchedulerAdmissionPlan, AdmissionResult]:
        plan = self.plan(context, memory_signals=memory_signals)
        if not plan.accepted:
            raise ValueError("scheduler admission plan was rejected: " + "; ".join(plan.reasons))
        result = self.integration.admit(plan.request, graph_node_id=context.graph_node_id)
        return plan, result

    def worker_health_signals(self) -> tuple[Mapping[str, Any], ...]:
        report = self.integration.health_bridge.sweep()
        return tuple(item.to_dict() for item in report.signals)

    @staticmethod
    def _priority(value: str) -> int:
        return {"low": 0, "normal": 1, "high": 2, "critical": 3}.get(value, 1)


__all__ = [
    "MemoryRoutingDirective",
    "SchedulerAdmissionPlan",
    "SchedulerDispatchContext",
    "WorkerPoolSchedulerBridge",
]
