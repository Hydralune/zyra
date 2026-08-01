from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .component_runtime import RecoveryComponent, RecoveryComponentControl
from .contracts import RecoveryAction, RouteLayer, stable_digest, utc_now
from .verification_runtime import ContinuationReceipt, ContinuationRequest


class RecoveryContinuationError(RuntimeError):
    pass


class RecoveryContinuationRejected(RecoveryContinuationError):
    pass


class ContinuationPhase(StrEnum):
    QUEUED = "queued"
    BLOCKED_PERMISSION = "blocked_permission"
    BLOCKED_AUTH = "blocked_auth"
    CONTEXT_RESTORED = "context_restored"
    DISPATCHED = "dispatched"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class ContinuationProjection:
    run_id: str
    task_id: str
    plan_id: str
    generation: int
    phase: ContinuationPhase
    action: RecoveryAction
    dispatch_id: str
    tool_dispatch_allowed: bool
    context_revision: int
    retry_generation: int
    route_generation: int
    graph_generation: int
    worker_generation: int
    backend_generation: int
    provider_generation: int
    route_refs: Mapping[str, Any]
    checkpoint_id: str
    action_receipt_ids: tuple[str, ...]
    continuation_receipt_id: str
    updated_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-continuation-projection/v1",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "generation": self.generation,
            "phase": self.phase.value,
            "action": self.action.value,
            "dispatch_id": self.dispatch_id,
            "tool_dispatch_allowed": self.tool_dispatch_allowed,
            "context_revision": self.context_revision,
            "retry_generation": self.retry_generation,
            "route_generation": self.route_generation,
            "graph_generation": self.graph_generation,
            "worker_generation": self.worker_generation,
            "backend_generation": self.backend_generation,
            "provider_generation": self.provider_generation,
            "route_refs": copy.deepcopy(dict(self.route_refs)),
            "checkpoint_id": self.checkpoint_id,
            "action_receipt_ids": list(self.action_receipt_ids),
            "continuation_receipt_id": self.continuation_receipt_id,
            "updated_at": self.updated_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ContinuationProjection":
        return cls(
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            plan_id=str(value["plan_id"]),
            generation=int(value.get("generation") or 0),
            phase=ContinuationPhase(str(value.get("phase") or ContinuationPhase.QUEUED.value)),
            action=RecoveryAction(str(value["action"])),
            dispatch_id=str(value.get("dispatch_id") or ""),
            tool_dispatch_allowed=bool(value.get("tool_dispatch_allowed", False)),
            context_revision=int(value.get("context_revision") or 0),
            retry_generation=int(value.get("retry_generation") or 0),
            route_generation=int(value.get("route_generation") or 0),
            graph_generation=int(value.get("graph_generation") or 0),
            worker_generation=int(value.get("worker_generation") or 0),
            backend_generation=int(value.get("backend_generation") or 0),
            provider_generation=int(value.get("provider_generation") or 0),
            route_refs=copy.deepcopy(dict(value.get("route_refs") or {})),
            checkpoint_id=str(value.get("checkpoint_id") or ""),
            action_receipt_ids=tuple(value.get("action_receipt_ids") or ()),
            continuation_receipt_id=str(value.get("continuation_receipt_id") or ""),
            updated_at=str(value.get("updated_at") or utc_now()),
            metadata=copy.deepcopy(dict(value.get("metadata") or {})),
        )


class ContinuationTaskStore(Protocol):
    def load_task(self, task_id: str) -> Any | None: ...

    def save_checkpoint(self, state: Any) -> None: ...


class ContinuationOwnerPort(Protocol):
    owner: str

    def continue_execution(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CallbackContinuationOwner:
    def __init__(self, owner: str, callback: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> None:
        self.owner = str(owner).strip()
        if not self.owner:
            raise ValueError("continuation owner is required")
        self._callback = callback

    def continue_execution(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._callback(copy.deepcopy(dict(request)))
        if not isinstance(result, Mapping):
            raise RecoveryContinuationRejected(f"{self.owner} returned a non-mapping continuation receipt")
        return copy.deepcopy(dict(result))


@dataclass(frozen=True, slots=True)
class ContinuationOwnerResult:
    owner: str
    accepted: bool
    changed: bool
    dispatch_id: str
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    canonical_ref: Mapping[str, Any]
    error_code: str = ""
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class RecoveryContinuationRuntime:
    owner = "RecoveryContinuationRuntime"
    ROUTE_LAYER_BY_ACTION: Mapping[RecoveryAction, RouteLayer] = {
        RecoveryAction.REPLAN: RouteLayer.GRAPH,
        RecoveryAction.REROUTE: RouteLayer.WORKER,
        RecoveryAction.SWITCH_BACKEND: RouteLayer.BACKEND,
        RecoveryAction.SWITCH_PROVIDER: RouteLayer.PROVIDER,
        RecoveryAction.DEGRADE_MODEL: RouteLayer.MODEL,
    }
    OWNER_KEY_BY_ACTION: Mapping[RecoveryAction, str] = {
        RecoveryAction.RETRY: "query",
        RecoveryAction.REROUTE: "scheduler",
        RecoveryAction.SWITCH_BACKEND: "scheduler",
        RecoveryAction.SWITCH_PROVIDER: "query",
        RecoveryAction.DEGRADE_MODEL: "query",
        RecoveryAction.REPLAN: "graph",
        RecoveryAction.COMPACT: "query",
        RecoveryAction.RESUME_CHECKPOINT: "query",
        RecoveryAction.ASK_PERMISSION: "permission",
        RecoveryAction.AUTHENTICATE_MCP: "mcp",
        RecoveryAction.ABORT: "task",
    }

    def __init__(
        self,
        store: ContinuationTaskStore,
        *,
        owners: Mapping[str, ContinuationOwnerPort],
        components: RecoveryComponentControl | None = None,
        event_sink: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.store = store
        self.owners = dict(owners)
        self.components = components or RecoveryComponentControl()
        self.event_sink = event_sink
        self._lock = threading.RLock()

    def dispatch(self, request: ContinuationRequest) -> ContinuationReceipt:
        self.components.require(
            RecoveryComponent.CONTINUATION_DISPATCH,
            operation=f"dispatch continuation after {request.action.value}",
        )
        if not request.action_receipt_ids:
            raise RecoveryContinuationRejected("continuation requires an applied action receipt")
        with self._lock:
            state = self.store.load_task(request.task_id)
            if state is None:
                raise RecoveryContinuationRejected(f"task state not found: {request.task_id}")
            if str(getattr(state, "run_id", "")) != request.run_id:
                raise RecoveryContinuationRejected("continuation request run identity mismatch")
            metadata = getattr(state, "metadata", None)
            if not isinstance(metadata, dict):
                raise RecoveryContinuationRejected("task metadata cannot hold the continuation projection")
            existing = dict(metadata.get("recovery_continuation") or {})
            request_digest = stable_digest(request.to_dict())
            if existing.get("idempotency_key") == request.idempotency_key:
                if existing.get("request_digest") != request_digest:
                    raise RecoveryContinuationRejected("continuation idempotency key changed request content")
                projection = ContinuationProjection.from_dict(existing["projection"])
                return self._receipt(request, projection, projection, replayed=True)
            before = self._snapshot(state, existing)
            owner_result = self._owner_continue(request, before)
            if not owner_result.accepted:
                code = owner_result.error_code or "continuation_owner_rejected"
                message = owner_result.message or "continuation owner rejected"
                raise RecoveryContinuationRejected(f"{code}: {message}")
            projection = self._project(request, before, owner_result)
            if projection.generation <= before.generation:
                raise RecoveryContinuationRejected("continuation generation did not advance")
            if projection.phase is ContinuationPhase.DISPATCHED and not projection.dispatch_id:
                raise RecoveryContinuationRejected("dispatched continuation lacks a dispatch id")
            if request.action is RecoveryAction.ASK_PERMISSION and projection.tool_dispatch_allowed:
                raise RecoveryContinuationRejected("permission-blocked continuation cannot dispatch a tool")
            if request.action in {RecoveryAction.COMPACT, RecoveryAction.RESUME_CHECKPOINT} and projection.context_revision <= before.context_revision:
                raise RecoveryContinuationRejected("compact/resume continuation did not change context revision")
            # The canonical owner may have completed a graph/worker dispatch and
            # persisted a newer TaskState while this runtime was awaiting its
            # receipt.  Merge the projection into that latest state; saving the
            # pre-dispatch snapshot would otherwise erase the real execution.
            latest_state = self.store.load_task(request.task_id)
            if latest_state is None:
                raise RecoveryContinuationRejected(
                    f"task state disappeared after continuation dispatch: {request.task_id}"
                )
            if str(getattr(latest_state, "run_id", "")) != request.run_id:
                raise RecoveryContinuationRejected("continuation owner changed run identity")
            latest_metadata = getattr(latest_state, "metadata", None)
            if not isinstance(latest_metadata, dict):
                raise RecoveryContinuationRejected(
                    "latest task metadata cannot hold the continuation projection"
                )
            state = latest_state
            metadata = latest_metadata
            metadata["recovery_continuation"] = {
                "idempotency_key": request.idempotency_key,
                "request_digest": request_digest,
                "projection": projection.to_dict(),
                "owner_receipt": {
                    "owner": owner_result.owner,
                    "dispatch_id": owner_result.dispatch_id,
                    "canonical_ref": copy.deepcopy(dict(owner_result.canonical_ref)),
                },
            }
            state.updated_at = utc_now()
            self.store.save_checkpoint(state)
            receipt = self._receipt(request, before, projection, owner_result=owner_result)
            metadata["recovery_continuation"]["projection"]["continuation_receipt_id"] = receipt.receipt_id
            self.store.save_checkpoint(state)
            self._emit("recovery_continuation_dispatched", {
                "run_id": request.run_id,
                "task_id": request.task_id,
                "plan_id": request.plan_id,
                "signal_id": request.signal_id,
                "action": request.action.value,
                "receipt_id": receipt.receipt_id,
                "dispatch_id": projection.dispatch_id,
                "phase": projection.phase.value,
                "tool_dispatch_allowed": projection.tool_dispatch_allowed,
                "canonical_ref": dict(owner_result.canonical_ref),
            })
            return receipt

    def current(self, task_id: str) -> ContinuationProjection | None:
        state = self.store.load_task(task_id)
        if state is None:
            return None
        metadata = dict(getattr(state, "metadata", {}) or {})
        value = metadata.get("recovery_continuation")
        if not isinstance(value, Mapping) or not isinstance(value.get("projection"), Mapping):
            return None
        return ContinuationProjection.from_dict(value["projection"])

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-continuation-runtime-contract/v1",
            "owner": self.owner,
            "continuation_owners": {key: port.owner for key, port in self.owners.items()},
            "actions": {action.value: owner for action, owner in self.OWNER_KEY_BY_ACTION.items()},
            "task_projection_owner": "SQLiteStore.TaskState",
            "tool_dispatch_after_permission_ask": False,
            "idempotent_restart": True,
        }

    def _owner_continue(
        self,
        request: ContinuationRequest,
        before: ContinuationProjection,
    ) -> ContinuationOwnerResult:
        key = self.OWNER_KEY_BY_ACTION.get(request.action)
        if key is None:
            raise RecoveryContinuationRejected(f"no continuation owner mapping for {request.action.value}")
        port = self.owners.get(key)
        if port is None:
            raise RecoveryContinuationRejected(f"continuation owner is unavailable: {key}")
        owner_request = {
            **request.to_dict(),
            "continuation_owner": port.owner,
            "previous_projection": before.to_dict(),
            "route_layer": self.ROUTE_LAYER_BY_ACTION.get(request.action, "").value
            if request.action in self.ROUTE_LAYER_BY_ACTION else "",
        }
        raw = dict(port.continue_execution(owner_request))
        accepted = bool(raw.get("accepted", not raw.get("error_code")))
        dispatch_id = str(raw.get("dispatch_id") or raw.get("event_id") or raw.get("receipt_id") or "")
        changed = bool(raw.get("changed", accepted and bool(dispatch_id)))
        return ContinuationOwnerResult(
            owner=port.owner,
            accepted=accepted,
            changed=changed,
            dispatch_id=dispatch_id,
            before=copy.deepcopy(dict(raw.get("before") or {})),
            after=copy.deepcopy(dict(raw.get("after") or {})),
            canonical_ref=copy.deepcopy(dict(raw.get("canonical_ref") or {})),
            error_code=str(raw.get("error_code") or ""),
            message=str(raw.get("message") or ""),
            metadata=copy.deepcopy(dict(raw.get("metadata") or {})),
        )

    def _project(
        self,
        request: ContinuationRequest,
        before: ContinuationProjection,
        owner: ContinuationOwnerResult,
    ) -> ContinuationProjection:
        phase = ContinuationPhase.DISPATCHED
        tool_allowed = True
        context_revision = before.context_revision
        retry_generation = before.retry_generation
        route_generation = before.route_generation
        graph_generation = before.graph_generation
        worker_generation = before.worker_generation
        backend_generation = before.backend_generation
        provider_generation = before.provider_generation
        route_refs = copy.deepcopy(dict(before.route_refs))
        if request.action is RecoveryAction.ASK_PERMISSION:
            phase = ContinuationPhase.BLOCKED_PERMISSION
            tool_allowed = False
        elif request.action is RecoveryAction.AUTHENTICATE_MCP:
            phase = ContinuationPhase.BLOCKED_AUTH
            tool_allowed = False
        elif request.action in {RecoveryAction.COMPACT, RecoveryAction.RESUME_CHECKPOINT}:
            phase = ContinuationPhase.CONTEXT_RESTORED
            context_revision += 1
        elif request.action is RecoveryAction.RETRY:
            retry_generation += 1
        elif request.action is RecoveryAction.ABORT:
            phase = ContinuationPhase.TERMINAL
            tool_allowed = False
        requested_route_layers: tuple[RouteLayer, ...] = ()
        raw_route_layers = request.metadata.get("route_layers")
        if isinstance(raw_route_layers, Sequence) and not isinstance(raw_route_layers, (str, bytes)):
            try:
                requested_route_layers = tuple(RouteLayer(str(item)) for item in raw_route_layers)
            except ValueError as error:
                raise RecoveryContinuationRejected(f"invalid applied route layer: {error}") from error
        if not requested_route_layers and request.action in self.ROUTE_LAYER_BY_ACTION:
            requested_route_layers = (self.ROUTE_LAYER_BY_ACTION[request.action],)
        if requested_route_layers:
            route_generation += 1
            for layer in requested_route_layers:
                route_refs[layer.value] = copy.deepcopy(dict(owner.canonical_ref))
                if layer is RouteLayer.GRAPH:
                    graph_generation += 1
                elif layer is RouteLayer.WORKER:
                    worker_generation += 1
                elif layer is RouteLayer.BACKEND:
                    backend_generation += 1
                elif layer in {RouteLayer.PROVIDER, RouteLayer.MODEL, RouteLayer.CREDENTIAL, RouteLayer.TRANSPORT}:
                    provider_generation += 1
        return ContinuationProjection(
            run_id=request.run_id,
            task_id=request.task_id,
            plan_id=request.plan_id,
            generation=before.generation + 1,
            phase=phase,
            action=request.action,
            dispatch_id=owner.dispatch_id,
            tool_dispatch_allowed=tool_allowed,
            context_revision=context_revision,
            retry_generation=retry_generation,
            route_generation=route_generation,
            graph_generation=graph_generation,
            worker_generation=worker_generation,
            backend_generation=backend_generation,
            provider_generation=provider_generation,
            route_refs=route_refs,
            checkpoint_id=request.checkpoint_id,
            action_receipt_ids=request.action_receipt_ids,
            continuation_receipt_id="",
            updated_at=utc_now(),
            metadata={
                "expected_effects": list(request.expected_effects),
                "context_digest": request.context_digest,
                "owner": owner.owner,
                "owner_changed": owner.changed,
                "owner_metadata": copy.deepcopy(dict(owner.metadata)),
            },
        )

    def _snapshot(self, state: Any, existing: Mapping[str, Any]) -> ContinuationProjection:
        if isinstance(existing.get("projection"), Mapping):
            return ContinuationProjection.from_dict(existing["projection"])
        return ContinuationProjection(
            run_id=str(state.run_id),
            task_id=str(state.task_id),
            plan_id="bootstrap",
            generation=0,
            phase=ContinuationPhase.QUEUED,
            action=RecoveryAction.NOOP,
            dispatch_id="",
            tool_dispatch_allowed=False,
            context_revision=0,
            retry_generation=0,
            route_generation=0,
            graph_generation=0,
            worker_generation=0,
            backend_generation=0,
            provider_generation=0,
            route_refs={},
            checkpoint_id="",
            action_receipt_ids=(),
            continuation_receipt_id="",
            updated_at=utc_now(),
        )

    def _receipt(
        self,
        request: ContinuationRequest,
        before: ContinuationProjection,
        after: ContinuationProjection,
        *,
        owner_result: ContinuationOwnerResult | None = None,
        replayed: bool = False,
    ) -> ContinuationReceipt:
        receipt_id = after.continuation_receipt_id or "recoverycontinuation:" + stable_digest({
            "request": request.to_dict(),
            "generation": after.generation,
            "dispatch_id": after.dispatch_id,
        })[:40]
        canonical_ref = copy.deepcopy(dict(owner_result.canonical_ref)) if owner_result else {"dispatch_id": after.dispatch_id}
        canonical_ref.setdefault("dispatch_id", after.dispatch_id)
        return ContinuationReceipt(
            owner=self.owner,
            receipt_id=receipt_id,
            accepted=True,
            changed_execution=after.generation > before.generation or replayed,
            dispatch_kind=after.phase.value,
            before=before.to_dict(),
            after=after.to_dict(),
            canonical_ref=canonical_ref,
            message="replayed continuation receipt" if replayed else "canonical continuation projection advanced",
            metadata={
                "replayed": replayed,
                "owner": owner_result.owner if owner_result else self.owner,
                "owner_receipt": owner_result.dispatch_id if owner_result else after.dispatch_id,
                "tool_dispatch_allowed": after.tool_dispatch_allowed,
            },
        )

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.event_sink is None:
            return {}
        return copy.deepcopy(dict(self.event_sink(event_type, payload) or {}))


def continuation_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-continuation-surface/v1",
        "state_owner": "SQLiteStore.TaskState",
        "post_action_effect": "canonical continuation projection and owner dispatch receipt",
        "permission_denied_tool_dispatch": False,
        "compact_resume_context_revision": True,
        "route_dispatch_generations": ["graph", "worker", "backend", "provider"],
        "memory_feedback_gate": "continuation receipt required",
    }


__all__ = [
    "CallbackContinuationOwner",
    "ContinuationOwnerPort",
    "ContinuationOwnerResult",
    "ContinuationPhase",
    "ContinuationProjection",
    "ContinuationTaskStore",
    "RecoveryContinuationError",
    "RecoveryContinuationRejected",
    "RecoveryContinuationRuntime",
    "continuation_runtime_contract",
]
