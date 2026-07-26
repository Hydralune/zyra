from __future__ import annotations

import copy
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .action_runtime import ExecutionResult, RecoveryActionRuntime
from .audit_runtime import RecoveryInvariantAuditor
from .checkpoint_runtime import (
    CheckpointCommitRequest,
    CheckpointCommitRuntime,
    CheckpointResumeBridge,
    ResumeExpectations,
)
from .contracts import (
    CheckpointWrite,
    DecisionMode,
    InFlightMessage,
    PendingRequestInfo,
    RecoveryBudget,
    RecoveryContext,
    RecoveryPlan,
    RecoveryRefs,
    RecoverySignal,
    RecoverySource,
    stable_digest,
)
from .delta_journal import BranchDeltaBuilder, DeltaCommitResult, DeterministicCommitRuntime
from .memory_feedback import RoutingMemoryFeedback
from .policy import RecoveryDecisionRuntime
from .route_runtime import LayeredRouteRuntime
from .signal_classifier import RecoverySignalClassifier
from .store import RecoveryPlanStore


class RecoveryApplicationError(RuntimeError):
    pass


class RecoveryRuntimeDisabled(RecoveryApplicationError):
    pass


class RecoveryContextResolver(Protocol):
    def resolve(self, signal: RecoverySignal, overrides: Mapping[str, Any]) -> RecoveryContext: ...


class RecoveryEventSink(Protocol):
    def emit(self, event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CallbackContextResolver:
    def __init__(self, callback: Callable[[RecoverySignal, Mapping[str, Any]], RecoveryContext]) -> None:
        self._callback = callback

    def resolve(self, signal: RecoverySignal, overrides: Mapping[str, Any]) -> RecoveryContext:
        return self._callback(signal, overrides)


class CallbackRecoveryEventSink:
    def __init__(self, callback: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]) -> None:
        self._callback = callback

    def emit(self, event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(self._callback(event_type, payload) or {})


@dataclass(frozen=True, slots=True)
class RecoveryRunResult:
    signal: RecoverySignal
    plan: RecoveryPlan
    execution: ExecutionResult | None
    event_receipts: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-run-result/v1",
            "signal": self.signal.to_dict(),
            "plan": self.plan.to_dict(),
            "execution": self.execution.to_dict() if self.execution else None,
            "event_receipts": [copy.deepcopy(dict(item)) for item in self.event_receipts],
        }


class StaticRecoveryContextResolver:
    def __init__(
        self,
        *,
        session_state: Mapping[str, Any] | None = None,
        permission_state: Mapping[str, Any] | None = None,
        worker_state: Mapping[str, Any] | None = None,
        backend_state: Mapping[str, Any] | None = None,
        provider_state: Mapping[str, Any] | None = None,
        checkpoint_state: Mapping[str, Any] | None = None,
        mode: DecisionMode = DecisionMode.INTERACTIVE,
    ) -> None:
        self.values = {
            "session_state": dict(session_state or {}),
            "permission_state": dict(permission_state or {}),
            "worker_state": dict(worker_state or {}),
            "backend_state": dict(backend_state or {}),
            "provider_state": dict(provider_state or {}),
            "checkpoint_state": dict(checkpoint_state or {}),
        }
        self.mode = mode

    def resolve(self, signal: RecoverySignal, overrides: Mapping[str, Any]) -> RecoveryContext:
        return RecoveryContext.from_dict({
            "refs": signal.refs.to_dict(),
            "mode": str(overrides.get("mode") or self.mode.value),
            **{
                key: {**copy.deepcopy(value), **dict(overrides.get(key) or {})}
                for key, value in self.values.items()
            },
            "memory_evidence": list(overrides.get("memory_evidence") or ()),
            "privacy_constraints": list(overrides.get("privacy_constraints") or ()),
            "allowed_locations": list(overrides.get("allowed_locations") or ()),
            "forbidden_actions": list(overrides.get("forbidden_actions") or ()),
            "budget": dict(overrides.get("budget") or {}),
            "metadata": dict(overrides.get("metadata") or {}),
        })


class RecoveryApplication:
    def __init__(
        self,
        store: RecoveryPlanStore,
        classifier: RecoverySignalClassifier,
        policy: RecoveryDecisionRuntime,
        actions: RecoveryActionRuntime,
        routes: LayeredRouteRuntime,
        feedback: RoutingMemoryFeedback,
        checkpoints: CheckpointCommitRuntime,
        deltas: DeterministicCommitRuntime,
        *,
        context_resolver: RecoveryContextResolver,
        resume_bridge: CheckpointResumeBridge | None = None,
        event_sinks: Sequence[RecoveryEventSink] = (),
        enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.classifier = classifier
        self.policy = policy
        self.actions = actions
        self.routes = routes
        self.feedback = feedback
        self.checkpoints = checkpoints
        self.deltas = deltas
        self.context_resolver = context_resolver
        self.resume_bridge = resume_bridge
        self.event_sinks = tuple(event_sinks)
        self.enabled = enabled or (lambda: True)
        self._lock = threading.RLock()

    def recover(
        self,
        payload: Mapping[str, Any],
        *,
        source: RecoverySource | str | None = None,
        context_overrides: Mapping[str, Any] | None = None,
        apply: bool = True,
        idempotency_key: str = "",
    ) -> RecoveryRunResult:
        self._require_enabled()
        signal = self.classifier.classify(payload, source=source)
        return self.recover_signal(
            signal,
            context_overrides=context_overrides,
            apply=apply,
            idempotency_key=idempotency_key,
        )

    def recover_fault_handoff(
        self,
        handoff: Mapping[str, Any],
        *,
        context_overrides: Mapping[str, Any] | None = None,
        apply: bool = True,
    ) -> RecoveryRunResult:
        self._require_enabled()
        signal = self.classifier.from_fault_handoff(handoff)
        return self.recover_signal(signal, context_overrides=context_overrides, apply=apply)

    def recover_worker_handoff(
        self,
        handoff: Mapping[str, Any],
        *,
        context_overrides: Mapping[str, Any] | None = None,
        apply: bool = True,
    ) -> tuple[RecoveryRunResult, ...]:
        self._require_enabled()
        results: list[RecoveryRunResult] = []
        for signal in self.classifier.from_worker_handoff(handoff):
            results.append(self.recover_signal(signal, context_overrides=context_overrides, apply=apply))
        return tuple(results)

    def recover_signal(
        self,
        signal: RecoverySignal,
        *,
        context_overrides: Mapping[str, Any] | None = None,
        apply: bool = True,
        idempotency_key: str = "",
    ) -> RecoveryRunResult:
        self._require_enabled()
        context = self.context_resolver.resolve(signal, dict(context_overrides or {}))
        context = self.feedback.enrich_context(context)
        with self._lock:
            plan, created = self.policy.plan(signal, context, idempotency_key=idempotency_key)
            receipts = list(self._emit("recovery_planned", {
                "run_id": signal.refs.run_id,
                "task_id": signal.refs.task_id,
                "signal_id": signal.signal_id,
                "plan_id": plan.plan_id,
                "created": created,
                "action": plan.decision.selected.action.value,
                "policy_rule": plan.decision.selected.policy_rule,
                "decision_key": plan.decision.deterministic_key,
            }))
            if not apply:
                return RecoveryRunResult(signal=signal, plan=plan, execution=None, event_receipts=tuple(receipts))
            execution = self.actions.execute(plan.plan_id, context)
            receipts.extend(self._emit("recovery_applied", {
                "run_id": signal.refs.run_id,
                "task_id": signal.refs.task_id,
                "signal_id": signal.signal_id,
                "plan_id": execution.plan.plan_id,
                "attempt_id": (
                    execution.receipts[-1].receipt_id
                    if execution.receipts
                    else execution.outcome.outcome_id
                ),
                "mutation_id": execution.outcome.outcome_id,
                "revision": execution.plan.revision,
                "action": execution.outcome.action.value,
                "success": execution.outcome.success,
                "outcome_id": execution.outcome.outcome_id,
                "route_decision_id": execution.outcome.route_decision_id,
                "checkpoint_id": execution.outcome.checkpoint_id,
                "routing_memory_id": execution.routing_memory_id,
                "changed_execution": any(item.changed_execution for item in execution.receipts),
            }))
            return RecoveryRunResult(
                signal=signal,
                plan=execution.plan,
                execution=execution,
                event_receipts=tuple(receipts),
            )

    def resume_waiting(
        self,
        plan_id: str,
        *,
        context_overrides: Mapping[str, Any] | None = None,
    ) -> ExecutionResult:
        self._require_enabled()
        plan = self.store.plan(plan_id)
        if plan is None:
            raise RecoveryApplicationError(f"recovery plan not found: {plan_id}")
        context = self.context_resolver.resolve(plan.signal, dict(context_overrides or {}))
        context = self.feedback.enrich_context(context)
        result = self.actions.resume_waiting(plan_id, context)
        self._emit("recovery_resumed", {
            "run_id": plan.signal.refs.run_id,
            "task_id": plan.signal.refs.task_id,
            "plan_id": plan_id,
            "success": result.outcome.success,
            "outcome_id": result.outcome.outcome_id,
        })
        return result

    def commit_checkpoint(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        request = self._checkpoint_request(payload)
        checkpoint, receipt, created = self.checkpoints.commit(request)
        event_receipts = self._emit("recovery_checkpoint_committed", {
            "run_id": checkpoint.run_id,
            "task_id": checkpoint.task_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "commit_revision": checkpoint.commit_revision,
            "signature": checkpoint.signature,
            "content_digest": checkpoint.content_digest,
            "created": created,
        })
        return {
            "checkpoint": checkpoint.to_dict(),
            "receipt": receipt.to_dict(),
            "created": created,
            "event_receipts": [dict(item) for item in event_receipts],
        }

    def resume_checkpoint(self, checkpoint_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        if self.resume_bridge is None:
            raise RecoveryApplicationError("checkpoint resume bridge is unavailable")
        expected = ResumeExpectations(
            run_id=str(payload["run_id"]),
            task_id=str(payload["task_id"]),
            session_id=str(payload.get("session_id") or ""),
            workflow_signature=str(payload.get("workflow_signature") or ""),
            graph_signature=str(payload.get("graph_signature") or ""),
            topology_signature=str(payload.get("topology_signature") or ""),
            owner_refs=dict(payload.get("owner_refs") or {}),
            version_refs=dict(payload.get("version_refs") or {}),
            allow_version_advancement=tuple(payload.get("allow_version_advancement") or ()),
            required_completed_step_ids=tuple(payload.get("required_completed_step_ids") or ()),
            required_fence_keys=tuple(payload.get("required_fence_keys") or ()),
        )
        receipt, owners = self.resume_bridge.resume(
            checkpoint_id,
            expected,
            candidate_step_ids=tuple(payload.get("candidate_step_ids") or ()),
            compact_first=bool(payload.get("compact_first", False)),
            rebind_worker=bool(payload.get("rebind_worker", False)),
            rebind_graph=bool(payload.get("rebind_graph", False)),
            idempotency_key=str(payload.get("idempotency_key") or ""),
        )
        return {"receipt": receipt.to_dict(), "owner_receipts": [item.to_dict() for item in owners]}

    def commit_delta(self, payload: Mapping[str, Any]) -> DeltaCommitResult:
        self._require_enabled()
        task_id = str(payload["task_id"])
        head = self.store.checkpoint_head(task_id)
        if head is None:
            raise RecoveryApplicationError(f"task has no checkpoint head: {task_id}")
        builder = BranchDeltaBuilder(
            head,
            owner=str(payload.get("owner") or "recovery-api"),
            branch_id=str(payload.get("branch_id") or "branch_" + stable_digest(payload)[:24]),
            metadata=dict(payload.get("metadata") or {}),
        )
        for operation in payload.get("operations") or ():
            value = dict(operation)
            name = str(value.get("operation") or "set")
            key = str(value["key"])
            expected = str(value.get("expected_digest") or "")
            metadata = dict(value.get("metadata") or {})
            if name == "set":
                builder.set(key, value.get("value"), expected_digest=expected, metadata=metadata)
            elif name == "delete":
                builder.delete(key, expected_digest=expected, metadata=metadata)
            elif name == "append_unique":
                builder.append_unique(key, value.get("value"), expected_digest=expected, metadata=metadata)
            elif name == "increment":
                builder.increment(key, value.get("value", 1), expected_digest=expected, metadata=metadata)
            elif name == "merge_mapping":
                builder.merge_mapping(key, dict(value.get("value") or {}), expected_digest=expected, metadata=metadata)
            else:
                raise RecoveryApplicationError(f"unsupported delta operation: {name}")
        delta = builder.build()
        self.deltas.persist(delta)
        return self.deltas.commit(delta)

    def task_view(self, task_id: str) -> dict[str, Any]:
        snapshot = self.store.task_snapshot(task_id)
        snapshot["route_scores"] = [item.to_dict() for item in self.feedback.scores(task_id)]
        snapshot["route"] = self.routes.current_route(
            str(snapshot.get("run_id") or self._run_id(task_id)),
            task_id,
        )
        snapshot["integrity"] = self.store.integrity_report(task_id=task_id)
        snapshot["runtime_audit"] = RecoveryInvariantAuditor(self.store).audit_task(task_id).to_dict()
        return snapshot

    def plan_view(self, plan_id: str) -> dict[str, Any]:
        plan = self.store.plan(plan_id)
        if plan is None:
            raise RecoveryApplicationError(f"recovery plan not found: {plan_id}")
        return {
            "plan": plan.to_dict(),
            "receipts": [item.to_dict() for item in self.store.action_receipts(plan_id=plan_id)],
            "outcomes": [item.to_dict() for item in self.store.outcomes(plan_id=plan_id)],
            "route_decisions": [item.to_dict() for item in self.store.route_decisions(plan_id=plan_id)],
        }

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-application-contract/v1",
            "enabled": bool(self.enabled()),
            "classifier": self.classifier.classification_contract(),
            "policy": self.policy.policy_contract(),
            "routes": self.routes.owners.contract(),
            "main_path": "signal -> classify -> decide -> action -> feedback -> subsequent context/route",
            "checkpoint_path": "commit -> atomic head -> exact resume -> canonical owners",
        }

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        receipts: list[Mapping[str, Any]] = []
        for sink in self.event_sinks:
            receipt = sink.emit(event_type, payload)
            if receipt:
                receipts.append(copy.deepcopy(dict(receipt)))
        return tuple(receipts)

    def _require_enabled(self) -> None:
        if not self.enabled():
            raise RecoveryRuntimeDisabled("recovery application is disabled")

    def _run_id(self, task_id: str) -> str:
        plans = self.store.plans(task_id=task_id, limit=1)
        return plans[0].signal.refs.run_id if plans else "unknown-run"

    @staticmethod
    def _checkpoint_request(payload: Mapping[str, Any]) -> CheckpointCommitRequest:
        return CheckpointCommitRequest(
            refs=RecoveryRefs.from_dict({
                **dict(payload.get("refs") or {}),
                "run_id": str(payload["run_id"]),
                "task_id": str(payload["task_id"]),
                "session_id": str(payload.get("session_id") or ""),
            }),
            workflow_signature=str(payload["workflow_signature"]),
            graph_signature=str(payload["graph_signature"]),
            topology_signature=str(payload["topology_signature"]),
            owner_refs=dict(payload.get("owner_refs") or {}),
            version_refs=dict(payload.get("version_refs") or {}),
            state_payload=dict(payload.get("state_payload") or {}),
            committed_refs=tuple(payload.get("committed_refs") or ()),
            completed_step_ids=tuple(payload.get("completed_step_ids") or ()),
            committed_writes=tuple(CheckpointWrite.from_dict(item) for item in payload.get("committed_writes") or ()),
            pending_writes=tuple(CheckpointWrite.from_dict(item) for item in payload.get("pending_writes") or ()),
            in_flight_messages=tuple(InFlightMessage.from_dict(item) for item in payload.get("in_flight_messages") or ()),
            pending_requests=tuple(PendingRequestInfo.from_dict(item) for item in payload.get("pending_requests") or ()),
            processed_response_ids=tuple(payload.get("processed_response_ids") or ()),
            side_effect_fence_keys=tuple(payload.get("side_effect_fence_keys") or ()),
            expected_revision=(
                int(payload["expected_revision"])
                if payload.get("expected_revision") is not None
                else None
            ),
            metadata=dict(payload.get("metadata") or {}),
        )


def recovery_application_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-application-surface/v1",
        "commands": [
            "recover",
            "recover_fault_handoff",
            "recover_worker_handoff",
            "resume_waiting",
            "commit_checkpoint",
            "resume_checkpoint",
            "commit_delta",
            "task_view",
            "plan_view",
        ],
        "disable_semantics": "requests fail closed with RecoveryRuntimeDisabled",
        "event_causality": ["recovery_planned", "recovery_applied", "recovery_checkpoint_committed"],
    }
