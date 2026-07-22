from __future__ import annotations

import copy
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .action_runtime import (
    ActionOwnerResult,
    ActionPortRegistry,
    ActionRequest,
    CallbackActionPort,
)
from .checkpoint_runtime import CallbackResumeOwner, ResumeOwnerPort
from .contracts import (
    DecisionMode,
    RecoveryAction,
    RecoveryAttemptStatus,
    RecoverySignal,
    RouteLayer,
    stable_digest,
    utc_now,
)
from .context_runtime import CallbackStateSnapshotPort, RecoveryContextRuntime, StateSnapshotPort
from .route_runtime import CallbackRouteOwner, RouteOwnerReceipt, RouteOwnerRegistry, RouteRequest


class CanonicalOwnerIntegrationError(RuntimeError):
    pass


class TaskStateNotFound(CanonicalOwnerIntegrationError):
    pass


class CanonicalOwnerReceiptRejected(CanonicalOwnerIntegrationError):
    def __init__(self, owner: str, code: str, message: str) -> None:
        self.owner = owner
        self.code = code
        super().__init__(message)


class TaskStateStorePort(Protocol):
    def load_task(self, task_id: str) -> Any | None: ...

    def save_checkpoint(self, state: Any) -> None: ...

    def task_events(self, task_id: str) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class OwnerMutationReceipt:
    owner: str
    operation: str
    receipt_id: str
    accepted: bool
    changed: bool
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    canonical_ref: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "operation": self.operation,
            "receipt_id": self.receipt_id,
            "accepted": self.accepted,
            "changed": self.changed,
            "before": copy.deepcopy(dict(self.before)),
            "after": copy.deepcopy(dict(self.after)),
            "canonical_ref": copy.deepcopy(dict(self.canonical_ref)),
            "error_code": self.error_code,
            "message": self.message,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


OwnerCallback = Callable[[Mapping[str, Any]], Mapping[str, Any] | OwnerMutationReceipt]


@dataclass(slots=True)
class CanonicalOwnerCallbacks:
    worker_successor: OwnerCallback | None = None
    backend_successor: OwnerCallback | None = None
    workspace_successor: OwnerCallback | None = None
    provider_successor: OwnerCallback | None = None
    model_degrade: OwnerCallback | None = None
    credential_successor: OwnerCallback | None = None
    graph_replan: OwnerCallback | None = None
    retry_request: OwnerCallback | None = None
    permission_request: OwnerCallback | None = None
    mcp_authenticate: OwnerCallback | None = None
    compact_context: OwnerCallback | None = None
    abort_task: OwnerCallback | None = None
    session_resume: OwnerCallback | None = None
    compact_restore: OwnerCallback | None = None
    worker_rebind: OwnerCallback | None = None
    graph_rebind: OwnerCallback | None = None
    permission_snapshot: Callable[[RecoverySignal], Mapping[str, Any]] | None = None
    worker_snapshot: Callable[[RecoverySignal], Mapping[str, Any]] | None = None
    backend_snapshot: Callable[[RecoverySignal], Mapping[str, Any]] | None = None
    provider_snapshot: Callable[[RecoverySignal], Mapping[str, Any]] | None = None


class CanonicalTaskStateRuntime:
    def __init__(self, store: TaskStateStorePort) -> None:
        self.store = store
        self._lock = threading.RLock()

    def require(self, task_id: str) -> Any:
        state = self.store.load_task(task_id)
        if state is None:
            raise TaskStateNotFound(f"task state does not exist: {task_id}")
        return state

    def snapshot(self, signal: RecoverySignal) -> dict[str, Any]:
        state = self.require(signal.refs.task_id)
        if str(state.run_id) != signal.refs.run_id:
            raise CanonicalOwnerIntegrationError("task state run identity mismatch")
        metadata = copy.deepcopy(dict(getattr(state, "metadata", {}) or {}))
        recovery = copy.deepcopy(dict(metadata.get("recovery_runtime") or {}))
        return {
            "run_id": str(state.run_id),
            "task_id": str(state.task_id),
            "session_id": str(metadata.get("query_session_id") or signal.refs.session_id),
            "task_status": str(getattr(state, "status", "")),
            "root_node_id": str(getattr(state, "root_node_id", "")),
            "revision": int(recovery.get("revision") or 0),
            "active_recovery_plan_id": str(recovery.get("active_plan_id") or ""),
            "retry_generation": int(recovery.get("retry_generation") or 0),
            "compact_generation": int(recovery.get("compact_generation") or 0),
            "resume_generation": int(recovery.get("resume_generation") or 0),
            "worker_pool": copy.deepcopy(dict(metadata.get("worker_pool") or {})),
            "backend_route": copy.deepcopy(dict(metadata.get("backend_route") or {})),
            "provider_route": copy.deepcopy(dict(metadata.get("provider_route") or {})),
            "dynamic_graph_ref": copy.deepcopy(dict(metadata.get("dynamic_graph_ref") or {})),
            "last_recovery": copy.deepcopy(dict(recovery.get("last_mutation") or {})),
        }

    def projection(self, run_id: str, task_id: str, layer: RouteLayer) -> dict[str, Any]:
        state = self.require(task_id)
        self._assert_identity(state, run_id, task_id)
        metadata = dict(getattr(state, "metadata", {}) or {})
        if layer is RouteLayer.WORKER:
            return copy.deepcopy(dict(metadata.get("worker_pool") or {}))
        if layer is RouteLayer.BACKEND:
            value = metadata.get("backend_route") or metadata.get("worker_pool") or {}
            return copy.deepcopy(dict(value))
        if layer is RouteLayer.WORKSPACE:
            return copy.deepcopy(dict(metadata.get("workspace") or {}))
        if layer in {RouteLayer.PROVIDER, RouteLayer.MODEL, RouteLayer.CREDENTIAL, RouteLayer.TRANSPORT}:
            value = copy.deepcopy(dict(metadata.get("provider_route") or {}))
            if layer is RouteLayer.MODEL:
                return {"model_id": value.get("model_id", ""), "provider_id": value.get("provider_id", "")}
            if layer is RouteLayer.CREDENTIAL:
                return {"credential_id": value.get("credential_id", ""), "provider_id": value.get("provider_id", "")}
            if layer is RouteLayer.TRANSPORT:
                return {"transport_id": value.get("transport_id", ""), "provider_id": value.get("provider_id", "")}
            return value
        if layer is RouteLayer.GRAPH:
            return copy.deepcopy(dict(metadata.get("dynamic_graph_ref") or {}))
        return {}

    def project_owner_receipt(
        self,
        run_id: str,
        task_id: str,
        plan_id: str,
        action: RecoveryAction,
        receipt: OwnerMutationReceipt,
        *,
        layer: RouteLayer | None = None,
    ) -> dict[str, Any]:
        if not receipt.accepted:
            raise CanonicalOwnerReceiptRejected(receipt.owner, receipt.error_code, receipt.message)
        with self._lock:
            state = self.require(task_id)
            self._assert_identity(state, run_id, task_id)
            metadata = getattr(state, "metadata", None)
            if not isinstance(metadata, dict):
                raise CanonicalOwnerIntegrationError("task metadata is not mutable canonical state")
            recovery = copy.deepcopy(dict(metadata.get("recovery_runtime") or {}))
            revision = int(recovery.get("revision") or 0) + 1
            mutation = {
                "plan_id": plan_id,
                "action": action.value,
                "owner": receipt.owner,
                "operation": receipt.operation,
                "receipt_id": receipt.receipt_id,
                "changed": receipt.changed,
                "canonical_ref": copy.deepcopy(dict(receipt.canonical_ref)),
                "revision": revision,
                "applied_at": utc_now(),
            }
            recovery.update({
                "revision": revision,
                "active_plan_id": plan_id,
                "last_mutation": mutation,
            })
            if action is RecoveryAction.RETRY:
                recovery["retry_generation"] = int(recovery.get("retry_generation") or 0) + 1
            if action is RecoveryAction.COMPACT:
                recovery["compact_generation"] = int(recovery.get("compact_generation") or 0) + 1
            if action is RecoveryAction.RESUME_CHECKPOINT:
                recovery["resume_generation"] = int(recovery.get("resume_generation") or 0) + 1
            metadata["recovery_runtime"] = recovery
            if layer is not None:
                self._project_layer(metadata, layer, receipt.after, receipt.canonical_ref)
            state.updated_at = utc_now()
            self.store.save_checkpoint(state)
            return mutation

    def record_plan(self, plan_id: str, signal: RecoverySignal, action: RecoveryAction) -> dict[str, Any]:
        with self._lock:
            state = self.require(signal.refs.task_id)
            self._assert_identity(state, signal.refs.run_id, signal.refs.task_id)
            metadata = state.metadata
            recovery = copy.deepcopy(dict(metadata.get("recovery_runtime") or {}))
            revision = int(recovery.get("revision") or 0) + 1
            recovery.update({
                "revision": revision,
                "active_plan_id": plan_id,
                "last_signal_id": signal.signal_id,
                "last_signal_kind": signal.kind.value,
                "selected_action": action.value,
                "planned_at": utc_now(),
            })
            metadata["recovery_runtime"] = recovery
            state.updated_at = utc_now()
            self.store.save_checkpoint(state)
            return {"task_id": state.task_id, "plan_id": plan_id, "revision": revision}

    @staticmethod
    def _project_layer(
        metadata: dict[str, Any],
        layer: RouteLayer,
        after: Mapping[str, Any],
        canonical_ref: Mapping[str, Any],
    ) -> None:
        value = {**copy.deepcopy(dict(after)), "canonical_ref": copy.deepcopy(dict(canonical_ref))}
        if layer is RouteLayer.WORKER:
            metadata["worker_pool"] = value
        elif layer is RouteLayer.BACKEND:
            metadata["backend_route"] = value
        elif layer is RouteLayer.WORKSPACE:
            metadata["workspace"] = value
        elif layer in {RouteLayer.PROVIDER, RouteLayer.MODEL, RouteLayer.CREDENTIAL, RouteLayer.TRANSPORT}:
            current = copy.deepcopy(dict(metadata.get("provider_route") or {}))
            current.update(value)
            metadata["provider_route"] = current
        elif layer is RouteLayer.GRAPH:
            metadata["dynamic_graph_ref"] = value

    @staticmethod
    def _assert_identity(state: Any, run_id: str, task_id: str) -> None:
        if str(getattr(state, "run_id", "")) != run_id or str(getattr(state, "task_id", "")) != task_id:
            raise CanonicalOwnerIntegrationError("canonical task identity mismatch")


class RecoveryOwnerRuntime:
    ROUTE_CALLBACKS = {
        RouteLayer.WORKER: ("WorkerPoolFoundationRuntime", "worker_successor"),
        RouteLayer.BACKEND: ("BackendRegistry", "backend_successor"),
        RouteLayer.WORKSPACE: ("WorkspaceManager", "workspace_successor"),
        RouteLayer.PROVIDER: ("ProviderControlPlane", "provider_successor"),
        RouteLayer.MODEL: ("ProviderControlPlane", "model_degrade"),
        RouteLayer.CREDENTIAL: ("CredentialStore", "credential_successor"),
        RouteLayer.GRAPH: ("GraphStateCustody", "graph_replan"),
    }
    ACTION_CALLBACKS = {
        RecoveryAction.RETRY: ("QueryEngine", "retry_request"),
        RecoveryAction.ASK_PERMISSION: ("PermissionControlPlane", "permission_request"),
        RecoveryAction.AUTHENTICATE_MCP: ("McpControlRuntime", "mcp_authenticate"),
        RecoveryAction.COMPACT: ("MemoryFabric", "compact_context"),
        RecoveryAction.ABORT: ("TaskState", "abort_task"),
    }

    def __init__(self, state: CanonicalTaskStateRuntime, callbacks: CanonicalOwnerCallbacks) -> None:
        self.state = state
        self.callbacks = callbacks

    def route_registry(self) -> RouteOwnerRegistry:
        owners = RouteOwnerRegistry()
        for layer, (owner_name, callback_name) in self.ROUTE_CALLBACKS.items():
            callback = getattr(self.callbacks, callback_name)
            if callback is None:
                continue
            owners.register(CallbackRouteOwner(
                layer,
                owner_name,
                current=lambda run_id, task_id, layer=layer: self.state.projection(run_id, task_id, layer),
                route=lambda request, layer=layer, owner_name=owner_name, callback=callback: self._route(
                    layer, owner_name, callback, request
                ),
            ))
        return owners

    def action_registry(self) -> ActionPortRegistry:
        ports = ActionPortRegistry()
        for action, (owner_name, callback_name) in self.ACTION_CALLBACKS.items():
            callback = getattr(self.callbacks, callback_name)
            if callback is None:
                continue
            ports.register(CallbackActionPort(
                action,
                owner_name,
                lambda request, owner_name=owner_name, callback=callback: self._action(
                    owner_name, callback, request
                ),
            ))
        return ports

    def snapshot_ports(self) -> dict[str, StateSnapshotPort]:
        result: dict[str, StateSnapshotPort] = {
            "session": CallbackStateSnapshotPort("TaskState", self.state.snapshot),
        }
        mappings = {
            "permission": ("PermissionControlPlane", self.callbacks.permission_snapshot),
            "worker": ("WorkerPoolFoundationRuntime", self.callbacks.worker_snapshot),
            "backend": ("BackendRegistry", self.callbacks.backend_snapshot),
            "provider": ("ProviderControlPlane", self.callbacks.provider_snapshot),
        }
        for key, (owner, callback) in mappings.items():
            if callback is not None:
                result[key] = CallbackStateSnapshotPort(owner, callback)
        return result

    def resume_owners(self) -> dict[str, ResumeOwnerPort]:
        result: dict[str, ResumeOwnerPort] = {}
        definitions = {
            "session": ("SessionLifecycleRuntime", self.callbacks.session_resume),
            "compact": ("MemoryFabric", self.callbacks.compact_restore),
            "worker": ("WorkerPoolFoundationRuntime", self.callbacks.worker_rebind),
            "graph": ("GraphStateCustody", self.callbacks.graph_rebind),
        }
        for key, (owner, callback) in definitions.items():
            if callback is not None:
                result[key] = CallbackResumeOwner(owner, lambda request, callback=callback: self._resume(callback, request))
        return result

    def _route(
        self,
        layer: RouteLayer,
        owner_name: str,
        callback: OwnerCallback,
        request: RouteRequest,
    ) -> RouteOwnerReceipt:
        owner_request = {
            **request.to_dict(),
            "canonical_owner": owner_name,
            "before_ref": copy.deepcopy(dict(request.current_ref)),
        }
        receipt = self._receipt(owner_name, request.action.value, callback(owner_request), owner_request)
        mutation = {}
        if receipt.accepted:
            mutation = self.state.project_owner_receipt(
                request.run_id,
                request.task_id,
                request.plan_id,
                request.action,
                receipt,
                layer=layer,
            )
        return RouteOwnerReceipt(
            layer=layer,
            owner=owner_name,
            accepted=receipt.accepted,
            changed=receipt.changed,
            before_ref=receipt.before,
            after_ref=receipt.after,
            receipt_id=receipt.receipt_id,
            error_code=receipt.error_code,
            message=receipt.message,
            metadata={**dict(receipt.metadata), "task_state_projection": mutation},
        )

    def _action(
        self,
        owner_name: str,
        callback: OwnerCallback,
        request: ActionRequest,
    ) -> ActionOwnerResult:
        owner_request = request.to_dict()
        owner_request["canonical_owner"] = owner_name
        receipt = self._receipt(owner_name, request.action.value, callback(owner_request), owner_request)
        if receipt.accepted:
            self.state.project_owner_receipt(
                request.plan.signal.refs.run_id,
                request.plan.signal.refs.task_id,
                request.plan.plan_id,
                request.action,
                receipt,
            )
        if not receipt.accepted:
            status = RecoveryAttemptStatus.REJECTED
        elif receipt.metadata.get("deferred"):
            status = RecoveryAttemptStatus.DEFERRED
        else:
            status = RecoveryAttemptStatus.APPLIED
        return ActionOwnerResult(
            owner=owner_name,
            status=status,
            changed_execution=receipt.changed,
            before=receipt.before,
            after=receipt.after,
            receipt_ref=receipt.receipt_id,
            error_code=receipt.error_code,
            message=receipt.message,
            metadata={**dict(receipt.metadata), "canonical_ref": dict(receipt.canonical_ref)},
        )

    def _resume(self, callback: OwnerCallback, request: Mapping[str, Any]) -> Mapping[str, Any]:
        owner_name = str(request.get("owner") or "resume-owner")
        receipt = self._receipt(owner_name, str(request.get("operation") or "resume"), callback(request), request)
        if not receipt.accepted:
            raise CanonicalOwnerReceiptRejected(receipt.owner, receipt.error_code, receipt.message)
        return {
            "receipt_ref": receipt.receipt_id,
            "changed": receipt.changed,
            "before": copy.deepcopy(dict(receipt.before)),
            "after": copy.deepcopy(dict(receipt.after)),
            "canonical_ref": copy.deepcopy(dict(receipt.canonical_ref)),
            "metadata": copy.deepcopy(dict(receipt.metadata)),
        }

    @staticmethod
    def _receipt(
        owner: str,
        operation: str,
        value: Mapping[str, Any] | OwnerMutationReceipt,
        request: Mapping[str, Any],
    ) -> OwnerMutationReceipt:
        if isinstance(value, OwnerMutationReceipt):
            if value.owner != owner:
                raise CanonicalOwnerIntegrationError("canonical owner receipt identity mismatch")
            return value
        result = dict(value)
        accepted = bool(result.get("accepted", not result.get("error_code")))
        before = copy.deepcopy(dict(result.get("before") or result.get("before_ref") or {}))
        after = copy.deepcopy(dict(result.get("after") or result.get("after_ref") or before))
        canonical_ref = copy.deepcopy(dict(result.get("canonical_ref") or {}))
        receipt_id = str(result.get("receipt_id") or stable_digest({
            "owner": owner,
            "operation": operation,
            "request": dict(request),
            "after": after,
            "canonical_ref": canonical_ref,
        }))
        return OwnerMutationReceipt(
            owner=owner,
            operation=operation,
            receipt_id=receipt_id,
            accepted=accepted,
            changed=bool(result.get("changed", accepted and before != after)),
            before=before,
            after=after,
            canonical_ref=canonical_ref,
            error_code=str(result.get("error_code") or ""),
            message=str(result.get("message") or ""),
            metadata=copy.deepcopy(dict(result.get("metadata") or {})),
        )


def task_owner_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-task-owner-integration/v1",
        "task_projection_owner": "SQLiteStore.TaskState",
        "planner_owner": "RecoveryDecisionRuntime",
        "route_owners": {
            layer.value: owner for layer, (owner, _) in RecoveryOwnerRuntime.ROUTE_CALLBACKS.items()
        },
        "action_owners": {
            action.value: owner for action, (owner, _) in RecoveryOwnerRuntime.ACTION_CALLBACKS.items()
        },
        "projection_is_not_canonical_lease": True,
        "owner_receipt_required_before_projection": True,
        "structured_identity_only": True,
    }
