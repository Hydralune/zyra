from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .contracts import (
    DecisionMode,
    RecoveryAction,
    RecoveryBudget,
    RecoveryContext,
    RecoveryRefs,
    RecoverySignal,
    RouteLayer,
    stable_digest,
)
from .memory_feedback import RoutingMemoryFeedback
from .route_runtime import RouteOwnerRegistry, RouteOwnerUnavailable
from .store import RecoveryPlanStore


class RecoveryContextError(RuntimeError):
    pass


class StateSnapshotPort(Protocol):
    owner: str

    def snapshot(self, signal: RecoverySignal) -> Mapping[str, Any]: ...


class CallbackStateSnapshotPort:
    def __init__(self, owner: str, callback: Callable[[RecoverySignal], Mapping[str, Any]]) -> None:
        if not owner.strip():
            raise ValueError("snapshot owner is required")
        self.owner = owner
        self._callback = callback

    def snapshot(self, signal: RecoverySignal) -> Mapping[str, Any]:
        return copy.deepcopy(dict(self._callback(signal)))


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    default_mode: DecisionMode = DecisionMode.INTERACTIVE
    retry_limit: int = 3
    route_limit: int = 3
    replan_limit: int = 2
    compact_limit: int = 2
    resume_limit: int = 3
    maximum_actions: int = 8
    maximum_total_delay_ms: int = 120_000
    required_owner_snapshots: tuple[str, ...] = ("session",)
    fail_closed_on_snapshot_error: bool = True
    allow_user_overrides: bool = True

    def __post_init__(self) -> None:
        for name in (
            "retry_limit", "route_limit", "replan_limit", "compact_limit",
            "resume_limit", "maximum_actions", "maximum_total_delay_ms",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class ContextSnapshotReceipt:
    owner: str
    digest: str
    available: bool
    state_revision: str
    error_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "digest": self.digest,
            "available": self.available,
            "state_revision": self.state_revision,
            "error_code": self.error_code,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


class RecoveryContextRuntime:
    SNAPSHOT_FIELDS = {
        "session": "session_state",
        "permission": "permission_state",
        "worker": "worker_state",
        "backend": "backend_state",
        "provider": "provider_state",
        "checkpoint": "checkpoint_state",
    }

    def __init__(
        self,
        store: RecoveryPlanStore,
        feedback: RoutingMemoryFeedback,
        *,
        snapshot_ports: Mapping[str, StateSnapshotPort],
        route_owners: RouteOwnerRegistry | None = None,
        policy: ContextPolicy | None = None,
        mode_resolver: Callable[[RecoverySignal], DecisionMode] | None = None,
    ) -> None:
        self.store = store
        self.feedback = feedback
        self.snapshot_ports = dict(snapshot_ports)
        self.route_owners = route_owners
        self.policy = policy or ContextPolicy()
        self.mode_resolver = mode_resolver
        unknown = set(self.snapshot_ports) - set(self.SNAPSHOT_FIELDS)
        if unknown:
            raise ValueError(f"unknown context snapshot owners: {sorted(unknown)}")

    def resolve(self, signal: RecoverySignal, overrides: Mapping[str, Any]) -> RecoveryContext:
        snapshots, receipts = self._snapshots(signal)
        missing = [
            owner for owner in self.policy.required_owner_snapshots
            if not snapshots.get(self.SNAPSHOT_FIELDS.get(owner, ""))
        ]
        if missing:
            raise RecoveryContextError(f"required context owners are unavailable: {', '.join(missing)}")
        if self.route_owners is not None:
            snapshots = self._merge_routes(signal, snapshots)
        checkpoint = self._checkpoint(signal, snapshots.get("checkpoint_state", {}))
        snapshots["checkpoint_state"] = checkpoint
        mode = self._mode(signal, overrides)
        budget = self._budget(overrides)
        forbidden = self._forbidden(signal, mode, overrides)
        metadata = {
            "context_owner_receipts": [item.to_dict() for item in receipts],
            "context_digest": stable_digest({
                "signal_id": signal.signal_id,
                "snapshots": snapshots,
                "mode": mode.value,
                "budget": budget.to_dict(),
            }),
            "structured_identity_only": True,
            "llm_authority": "advisory_only",
            **dict(overrides.get("metadata") or {}),
        }
        context = RecoveryContext(
            refs=signal.refs,
            mode=mode,
            session_state=self._overlay(snapshots.get("session_state"), overrides.get("session_state")),
            permission_state=self._overlay(snapshots.get("permission_state"), overrides.get("permission_state")),
            worker_state=self._overlay(snapshots.get("worker_state"), overrides.get("worker_state")),
            backend_state=self._overlay(snapshots.get("backend_state"), overrides.get("backend_state")),
            provider_state=self._overlay(snapshots.get("provider_state"), overrides.get("provider_state")),
            checkpoint_state=self._overlay(snapshots.get("checkpoint_state"), overrides.get("checkpoint_state")),
            memory_evidence=tuple(
                [*self.feedback.evidence(signal.refs.task_id), *self._evidence(overrides)]
            ),
            privacy_constraints=tuple(str(item) for item in overrides.get("privacy_constraints") or ()),
            allowed_locations=tuple(str(item) for item in overrides.get("allowed_locations") or ()),
            forbidden_actions=forbidden,
            budget=budget,
            metadata=metadata,
        )
        return context

    def validate(self, signal: RecoverySignal, context: RecoveryContext) -> tuple[str, ...]:
        failures: list[str] = []
        if signal.refs.run_id != context.refs.run_id:
            failures.append("run_id mismatch")
        if signal.refs.task_id != context.refs.task_id:
            failures.append("task_id mismatch")
        if signal.refs.session_id and signal.refs.session_id != context.refs.session_id:
            failures.append("session_id mismatch")
        if context.mode is DecisionMode.SEALED_AUTONOMOUS and RecoveryAction.ASK_PERMISSION not in context.forbidden_actions:
            failures.append("sealed mode must forbid ask_permission")
        if signal.observable_side_effect and not context.checkpoint_state.get("checkpoint_id"):
            failures.append("observable side effect recovery requires a checkpoint")
        if signal.partial_output and not context.checkpoint_state.get("checkpoint_id"):
            failures.append("partial output recovery requires a checkpoint")
        return tuple(failures)

    def explain(self, signal: RecoverySignal, overrides: Mapping[str, Any]) -> dict[str, Any]:
        context = self.resolve(signal, overrides)
        return {
            "context": context.to_dict(),
            "validation_failures": list(self.validate(signal, context)),
            "owner_count": len(context.metadata.get("context_owner_receipts") or ()),
            "memory_evidence_count": len(context.memory_evidence),
            "checkpoint_available": bool(context.checkpoint_state.get("checkpoint_id")),
        }

    def _snapshots(
        self,
        signal: RecoverySignal,
    ) -> tuple[dict[str, Mapping[str, Any]], tuple[ContextSnapshotReceipt, ...]]:
        values: dict[str, Mapping[str, Any]] = {}
        receipts: list[ContextSnapshotReceipt] = []
        for owner, field_name in self.SNAPSHOT_FIELDS.items():
            port = self.snapshot_ports.get(owner)
            if port is None:
                values[field_name] = {}
                receipts.append(ContextSnapshotReceipt(
                    owner=owner,
                    digest=stable_digest({}),
                    available=False,
                    state_revision="",
                    error_code="owner_unregistered",
                ))
                continue
            try:
                snapshot = copy.deepcopy(dict(port.snapshot(signal)))
                values[field_name] = snapshot
                receipts.append(ContextSnapshotReceipt(
                    owner=port.owner,
                    digest=stable_digest(snapshot),
                    available=True,
                    state_revision=str(snapshot.get("revision") or snapshot.get("version") or ""),
                ))
            except Exception as error:
                if self.policy.fail_closed_on_snapshot_error and owner in self.policy.required_owner_snapshots:
                    raise RecoveryContextError(f"{owner} context owner failed: {error}") from error
                values[field_name] = {}
                receipts.append(ContextSnapshotReceipt(
                    owner=port.owner,
                    digest=stable_digest({}),
                    available=False,
                    state_revision="",
                    error_code=type(error).__name__,
                    metadata={"message": str(error)[:1000]},
                ))
        return values, tuple(receipts)

    def _merge_routes(
        self,
        signal: RecoverySignal,
        snapshots: dict[str, Mapping[str, Any]],
    ) -> dict[str, Mapping[str, Any]]:
        result = dict(snapshots)
        targets = {
            RouteLayer.WORKER: "worker_state",
            RouteLayer.BACKEND: "backend_state",
            RouteLayer.PROVIDER: "provider_state",
            RouteLayer.MODEL: "provider_state",
        }
        for layer, field_name in targets.items():
            try:
                current = self.route_owners.current(layer, signal.refs.run_id, signal.refs.task_id)
            except RouteOwnerUnavailable:
                continue
            merged = copy.deepcopy(dict(result.get(field_name) or {}))
            merged["current_route"] = copy.deepcopy(dict(current))
            result[field_name] = merged
        return result

    def _checkpoint(self, signal: RecoverySignal, owner_snapshot: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(owner_snapshot))
        head = self.store.checkpoint_head(signal.refs.task_id)
        if head is None:
            return result
        result.update({
            "checkpoint_id": head.checkpoint_id,
            "commit_revision": head.commit_revision,
            "workflow_signature": head.workflow_signature,
            "graph_signature": head.graph_signature,
            "topology_signature": head.topology_signature,
            "owner_refs": copy.deepcopy(dict(head.owner_refs)),
            "version_refs": copy.deepcopy(dict(head.version_refs)),
            "completed_step_ids": list(head.completed_step_ids),
            "pending_write_count": len(head.pending_writes),
            "committed_write_count": len(head.committed_writes),
            "processed_response_ids": list(head.processed_response_ids),
            "side_effect_fence_keys": list(head.side_effect_fence_keys),
            "phase": head.phase.value,
        })
        return result

    def _mode(self, signal: RecoverySignal, overrides: Mapping[str, Any]) -> DecisionMode:
        value = overrides.get("mode")
        if value is not None:
            return DecisionMode(str(value))
        if self.mode_resolver is not None:
            return self.mode_resolver(signal)
        return self.policy.default_mode

    def _budget(self, overrides: Mapping[str, Any]) -> RecoveryBudget:
        base = {
            "maximum_retries": self.policy.retry_limit,
            "maximum_reroutes": self.policy.route_limit,
            "maximum_backend_switches": self.policy.route_limit,
            "maximum_provider_switches": self.policy.route_limit,
            "maximum_compactions": self.policy.compact_limit,
            "maximum_resumes": self.policy.resume_limit,
            "maximum_total_actions": self.policy.maximum_actions,
            "maximum_delay_ms": self.policy.maximum_total_delay_ms,
        }
        if self.policy.allow_user_overrides:
            base.update(dict(overrides.get("budget") or {}))
        return RecoveryBudget.from_dict(base)

    @staticmethod
    def _forbidden(
        signal: RecoverySignal,
        mode: DecisionMode,
        overrides: Mapping[str, Any],
    ) -> tuple[RecoveryAction, ...]:
        actions = [RecoveryAction(str(item)) for item in overrides.get("forbidden_actions") or ()]
        if mode is DecisionMode.SEALED_AUTONOMOUS:
            actions.append(RecoveryAction.ASK_PERMISSION)
        if signal.observable_side_effect or signal.partial_output:
            actions.append(RecoveryAction.RETRY)
        return tuple(dict.fromkeys(actions))

    @staticmethod
    def _overlay(left: Any, right: Any) -> dict[str, Any]:
        base = copy.deepcopy(dict(left or {}))
        if right is not None:
            base.update(copy.deepcopy(dict(right)))
        return base

    @staticmethod
    def _evidence(overrides: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        return tuple(copy.deepcopy(dict(item)) for item in overrides.get("memory_evidence") or ())


def context_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-context-runtime-contract/v1",
        "structured_owners": list(RecoveryContextRuntime.SNAPSHOT_FIELDS),
        "identity_source": "RecoverySignal.refs",
        "checkpoint_source": "RecoveryPlanStore atomic task head",
        "memory_source": "RoutingMemoryFeedback evidence",
        "sealed_mode": "ask_permission is forbidden",
        "partial_or_side_effect": "retry is forbidden without exact resume",
    }
