from __future__ import annotations

import copy
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .checkpoint_runtime import CheckpointResumeBridge, ResumeExpectations
from .contracts import (
    DecisionMode,
    RecoveryAction,
    RecoveryActionReceipt,
    RecoveryAttemptStatus,
    RecoveryContext,
    RecoveryOutcome,
    RecoveryOutcomeKind,
    RecoveryPlan,
    RecoveryPlanStatus,
    RouteLayer,
    stable_digest,
)
from .memory_feedback import RoutingMemoryFeedback
from .route_runtime import LayeredRouteRuntime, RouteOwnerRejected
from .store import RecoveryLeaseError, RecoveryPlanStore, RecoveryStoreConflict


class RecoveryActionError(RuntimeError):
    pass


class RecoveryActionUnavailable(RecoveryActionError):
    pass


class RecoveryActionRejected(RecoveryActionError):
    def __init__(self, action: RecoveryAction, code: str, message: str, *, retryable: bool = False) -> None:
        self.action = action
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ActionRequest:
    plan: RecoveryPlan
    context: RecoveryContext
    action: RecoveryAction
    sequence_index: int
    request_digest: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-action-request/v1",
            "plan_id": self.plan.plan_id,
            "run_id": self.plan.signal.refs.run_id,
            "task_id": self.plan.signal.refs.task_id,
            "signal_id": self.plan.signal.signal_id,
            "action": self.action.value,
            "sequence_index": self.sequence_index,
            "request_digest": self.request_digest,
            "context": self.context.to_dict(),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class ActionOwnerResult:
    owner: str
    status: RecoveryAttemptStatus
    changed_execution: bool
    before: Mapping[str, Any] = field(default_factory=dict)
    after: Mapping[str, Any] = field(default_factory=dict)
    receipt_ref: str = ""
    error_code: str = ""
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class RecoveryActionPort(Protocol):
    action: RecoveryAction
    owner: str

    def apply(self, request: ActionRequest) -> ActionOwnerResult: ...


class CallbackActionPort:
    def __init__(
        self,
        action: RecoveryAction,
        owner: str,
        callback: Callable[[ActionRequest], ActionOwnerResult | Mapping[str, Any]],
    ) -> None:
        if not owner.strip():
            raise ValueError("action port owner is required")
        self.action = action
        self.owner = owner
        self._callback = callback

    def apply(self, request: ActionRequest) -> ActionOwnerResult:
        if request.action is not self.action:
            raise RecoveryActionError(f"{self.owner} cannot apply {request.action.value}")
        result = self._callback(request)
        if isinstance(result, ActionOwnerResult):
            return result
        value = dict(result)
        status = RecoveryAttemptStatus(str(value.get("status") or RecoveryAttemptStatus.APPLIED.value))
        return ActionOwnerResult(
            owner=str(value.get("owner") or self.owner),
            status=status,
            changed_execution=bool(value.get("changed_execution", status in {
                RecoveryAttemptStatus.APPLIED,
                RecoveryAttemptStatus.SUCCEEDED,
            })),
            before=dict(value.get("before") or {}),
            after=dict(value.get("after") or {}),
            receipt_ref=str(value.get("receipt_ref") or ""),
            error_code=str(value.get("error_code") or ""),
            message=str(value.get("message") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


class ActionPortRegistry:
    def __init__(self, ports: Sequence[RecoveryActionPort] = ()) -> None:
        self._ports: dict[RecoveryAction, RecoveryActionPort] = {}
        for port in ports:
            self.register(port)

    def register(self, port: RecoveryActionPort) -> None:
        if port.action in self._ports:
            raise ValueError(f"action already has a port: {port.action.value}")
        self._ports[port.action] = port

    def replace(self, port: RecoveryActionPort) -> None:
        self._ports[port.action] = port

    def require(self, action: RecoveryAction) -> RecoveryActionPort:
        port = self._ports.get(action)
        if port is None:
            raise RecoveryActionUnavailable(f"no concrete action owner for {action.value}")
        return port

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-action-port-registry/v1",
            "ports": {
                action.value: port.owner
                for action, port in sorted(self._ports.items(), key=lambda item: item[0].value)
            },
        }


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    plan: RecoveryPlan
    receipts: tuple[RecoveryActionReceipt, ...]
    outcome: RecoveryOutcome
    routing_memory_id: str
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "receipts": [item.to_dict() for item in self.receipts],
            "outcome": self.outcome.to_dict(),
            "routing_memory_id": self.routing_memory_id,
            "replayed": self.replayed,
        }


class RecoveryActionRuntime:
    ROUTE_ACTIONS = {
        RecoveryAction.REROUTE,
        RecoveryAction.SWITCH_BACKEND,
        RecoveryAction.SWITCH_PROVIDER,
        RecoveryAction.DEGRADE_MODEL,
        RecoveryAction.REPLAN,
    }

    def __init__(
        self,
        store: RecoveryPlanStore,
        routes: LayeredRouteRuntime,
        feedback: RoutingMemoryFeedback,
        *,
        ports: ActionPortRegistry | None = None,
        resume_bridge: CheckpointResumeBridge | None = None,
        executor_id: str = "recovery-action-runtime",
        lease_seconds: float = 90.0,
    ) -> None:
        self.store = store
        self.routes = routes
        self.feedback = feedback
        self.ports = ports or ActionPortRegistry()
        self.resume_bridge = resume_bridge
        self.executor_id = executor_id
        self.lease_seconds = lease_seconds

    def execute(self, plan_id: str, context: RecoveryContext) -> ExecutionResult:
        plan = self.store.plan(plan_id)
        if plan is None:
            raise RecoveryActionError(f"recovery plan not found: {plan_id}")
        self._validate_context(plan, context)
        if plan.status is RecoveryPlanStatus.SUCCEEDED:
            return self._replayed_result(plan)
        claimed = self.store.claim_plan(plan_id, owner=self.executor_id, lease_seconds=self.lease_seconds)
        applying = self._transition(claimed, RecoveryPlanStatus.APPLYING, "applying")
        sequence = self._sequence(applying)
        receipts: list[RecoveryActionReceipt] = []
        started = time.monotonic()
        route_decision = None
        checkpoint_id = ""
        try:
            for index, action in enumerate(sequence[applying.action_cursor :], start=applying.action_cursor):
                digest = stable_digest({
                    "plan_id": applying.plan_id,
                    "action": action.value,
                    "index": index,
                    "signal": applying.signal.fingerprint,
                    "context": context.to_dict(),
                })
                existing = self.store.action_receipt(plan_id=applying.plan_id, request_digest=digest)
                if existing is not None:
                    receipt = existing
                elif action in self.ROUTE_ACTIONS:
                    receipt = self._apply_route(applying, context, action, digest)
                elif action is RecoveryAction.RESUME_CHECKPOINT:
                    receipt = self._resume(applying, context, digest)
                elif action is RecoveryAction.NOOP:
                    receipt = self._receipt(
                        applying,
                        action,
                        digest,
                        owner="RecoveryActionRuntime",
                        status=RecoveryAttemptStatus.SUCCEEDED,
                        changed=False,
                        before={},
                        after={},
                        message="no recovery mutation requested",
                    )
                else:
                    receipt = self._apply_port(applying, context, action, index, digest)
                receipts.append(receipt)
                if receipt.route_decision is not None:
                    route_decision = receipt.route_decision
                if receipt.checkpoint_receipt is not None:
                    checkpoint_id = receipt.checkpoint_receipt.checkpoint_id
                if receipt.status in {RecoveryAttemptStatus.FAILED, RecoveryAttemptStatus.REJECTED}:
                    raise RecoveryActionRejected(
                        action,
                        receipt.error_code or "action_rejected",
                        receipt.error_message or f"{action.value} rejected",
                    )
                applying = self._advance_cursor(applying, index + 1)
                if receipt.status is RecoveryAttemptStatus.DEFERRED:
                    waiting = self._waiting_status(action, context)
                    applying = self._transition(applying, waiting, "deferred")
                    outcome = self._outcome(
                        applying,
                        receipts,
                        kind=RecoveryOutcomeKind.WAITING,
                        success=False,
                        summary=f"recovery waits for {action.value}",
                        started=started,
                        route_decision_id=route_decision.route_decision_id if route_decision else "",
                        checkpoint_id=checkpoint_id,
                    )
                    memory = self.feedback.record(applying, outcome, route_decision=route_decision)
                    return ExecutionResult(applying, tuple(receipts), outcome, memory.record_id)
            applied = self._transition(applying, RecoveryPlanStatus.APPLIED, "applied")
            succeeded = self._transition(applied, RecoveryPlanStatus.SUCCEEDED, "succeeded")
            outcome = self._outcome(
                succeeded,
                receipts,
                kind=RecoveryOutcomeKind.RECOVERED,
                success=True,
                summary=self._success_summary(succeeded, receipts),
                started=started,
                route_decision_id=route_decision.route_decision_id if route_decision else "",
                checkpoint_id=checkpoint_id,
            )
            memory = self.feedback.record(succeeded, outcome, route_decision=route_decision)
            return ExecutionResult(succeeded, tuple(receipts), outcome, memory.record_id)
        except Exception as error:
            current = self.store.plan(plan_id) or applying
            if not current.status.terminal and current.status is not RecoveryPlanStatus.FAILED:
                try:
                    current = self._transition(current, RecoveryPlanStatus.FAILED, "failed")
                except RecoveryStoreConflict:
                    current = self.store.plan(plan_id) or current
            outcome = self._outcome(
                current,
                receipts,
                kind=RecoveryOutcomeKind.FAILED,
                success=False,
                summary=f"recovery failed: {type(error).__name__}: {error}",
                started=started,
                route_decision_id=route_decision.route_decision_id if route_decision else "",
                checkpoint_id=checkpoint_id,
                error=error,
            )
            memory = self.feedback.record(current, outcome, route_decision=route_decision)
            if isinstance(error, RecoveryActionError):
                raise
            raise RecoveryActionError(str(error)) from error

    def resume_waiting(self, plan_id: str, context: RecoveryContext) -> ExecutionResult:
        plan = self.store.plan(plan_id)
        if plan is None:
            raise RecoveryActionError(f"recovery plan not found: {plan_id}")
        if plan.status not in {
            RecoveryPlanStatus.WAITING_PERMISSION,
            RecoveryPlanStatus.WAITING_AUTH,
            RecoveryPlanStatus.WAITING_BACKOFF,
        }:
            raise RecoveryActionError(f"plan is not waiting: {plan.status.value}")
        planned = self._transition(plan, RecoveryPlanStatus.PLANNED, "wait_condition_resolved")
        return self.execute(planned.plan_id, context)

    def cancel(self, plan_id: str, *, reason: str) -> RecoveryPlan:
        plan = self.store.plan(plan_id)
        if plan is None:
            raise RecoveryActionError(f"recovery plan not found: {plan_id}")
        if plan.status.terminal:
            return plan
        return self._transition(
            plan,
            RecoveryPlanStatus.CANCELLED,
            "cancelled",
            provenance={**dict(plan.provenance), "cancel_reason": reason},
        )

    def _apply_route(
        self,
        plan: RecoveryPlan,
        context: RecoveryContext,
        action: RecoveryAction,
        digest: str,
    ) -> RecoveryActionReceipt:
        constraints = self._route_constraints(context, action)
        exclusions = self._route_exclusions(plan, action)
        try:
            decision = self.routes.apply(
                plan,
                action,
                constraints=constraints,
                excluded_refs=exclusions,
            )
            changed = any(item.applied for item in decision.changes)
            status = RecoveryAttemptStatus.APPLIED if changed else RecoveryAttemptStatus.REJECTED
            return self._receipt(
                plan,
                action,
                digest,
                owner="LayeredRouteRuntime",
                status=status,
                changed=changed,
                before={item.layer.value: dict(item.before_ref) for item in decision.changes},
                after={item.layer.value: dict(item.after_ref) for item in decision.changes},
                message="layered route applied" if changed else "no route layer changed",
                route_decision=decision,
                error_code="" if changed else "route_unchanged",
            )
        except RouteOwnerRejected as error:
            return self._receipt(
                plan,
                action,
                digest,
                owner="LayeredRouteRuntime",
                status=RecoveryAttemptStatus.REJECTED,
                changed=False,
                before={},
                after={},
                message=str(error),
                error_code=error.code,
                metadata={"failed_layer": error.layer.value},
            )

    def _apply_port(
        self,
        plan: RecoveryPlan,
        context: RecoveryContext,
        action: RecoveryAction,
        index: int,
        digest: str,
    ) -> RecoveryActionReceipt:
        port = self.ports.require(action)
        request = ActionRequest(
            plan=plan,
            context=context,
            action=action,
            sequence_index=index,
            request_digest=digest,
            metadata={
                "signal_kind": plan.signal.kind.value,
                "retry_delay_ms": plan.decision.selected.delay_ms,
                "decision_mode": context.mode.value,
            },
        )
        result = port.apply(request)
        if result.owner != port.owner:
            raise RecoveryActionError(f"action owner receipt mismatch for {action.value}")
        return self._receipt(
            plan,
            action,
            digest,
            owner=result.owner,
            status=result.status,
            changed=result.changed_execution,
            before=result.before,
            after=result.after,
            external_ref=result.receipt_ref,
            error_code=result.error_code,
            message=result.message,
            metadata=result.metadata,
        )

    def _resume(self, plan: RecoveryPlan, context: RecoveryContext, digest: str) -> RecoveryActionReceipt:
        if self.resume_bridge is None:
            raise RecoveryActionUnavailable("checkpoint resume bridge is unavailable")
        checkpoint_id = str(
            context.checkpoint_state.get("checkpoint_id")
            or plan.signal.details.get("checkpoint_id")
            or ""
        )
        if not checkpoint_id:
            raise RecoveryActionRejected(RecoveryAction.RESUME_CHECKPOINT, "checkpoint_missing", "checkpoint id is required")
        expected = ResumeExpectations(
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            session_id=plan.signal.refs.session_id,
            workflow_signature=str(context.checkpoint_state.get("workflow_signature") or ""),
            graph_signature=str(context.checkpoint_state.get("graph_signature") or ""),
            topology_signature=str(context.checkpoint_state.get("topology_signature") or ""),
            owner_refs=dict(context.checkpoint_state.get("owner_refs") or {}),
            version_refs=dict(context.checkpoint_state.get("version_refs") or {}),
            allow_version_advancement=tuple(context.checkpoint_state.get("allow_version_advancement") or ()),
            required_completed_step_ids=tuple(context.checkpoint_state.get("required_completed_step_ids") or ()),
            required_fence_keys=tuple(context.checkpoint_state.get("required_fence_keys") or ()),
        )
        receipt, owner_receipts = self.resume_bridge.resume(
            checkpoint_id,
            expected,
            candidate_step_ids=tuple(context.checkpoint_state.get("candidate_step_ids") or ()),
            compact_first=bool(context.checkpoint_state.get("compact_first", False)),
            rebind_worker=bool(context.checkpoint_state.get("rebind_worker", False)),
            rebind_graph=bool(context.checkpoint_state.get("rebind_graph", False)),
            idempotency_key=digest,
        )
        return self._receipt(
            plan,
            RecoveryAction.RESUME_CHECKPOINT,
            digest,
            owner="CheckpointResumeBridge",
            status=RecoveryAttemptStatus.SUCCEEDED,
            changed=any(item.changed for item in owner_receipts),
            before={"checkpoint_id": checkpoint_id},
            after={"resume_token": receipt.resume_token},
            external_ref=receipt.receipt_id,
            message="checkpoint resumed through canonical owners",
            checkpoint_receipt=receipt,
            metadata={"owner_receipts": [item.to_dict() for item in owner_receipts]},
        )

    def _receipt(
        self,
        plan: RecoveryPlan,
        action: RecoveryAction,
        digest: str,
        *,
        owner: str,
        status: RecoveryAttemptStatus,
        changed: bool,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        external_ref: str = "",
        checkpoint_receipt: Any = None,
        route_decision: Any = None,
        error_code: str = "",
        message: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> RecoveryActionReceipt:
        receipt = RecoveryActionReceipt(
            plan_id=plan.plan_id,
            action=action,
            status=status,
            owner=owner,
            request_digest=digest,
            changed_execution=changed,
            external_receipt_ref=external_ref,
            checkpoint_receipt=checkpoint_receipt,
            route_decision=route_decision,
            before=dict(before),
            after=dict(after),
            error_code=error_code,
            error_message=message if error_code else "",
            metadata={**dict(metadata or {}), "message": message},
        )
        stored, _ = self.store.append_action_receipt(receipt)
        return stored

    def _transition(
        self,
        plan: RecoveryPlan,
        status: RecoveryPlanStatus,
        operation: str,
        **changes: Any,
    ) -> RecoveryPlan:
        updated = plan.evolve(status=status, **changes)
        return self.store.update_plan(
            updated,
            expected_revision=plan.revision,
            operation=operation,
            causation_id=self.executor_id,
        )

    def _advance_cursor(self, plan: RecoveryPlan, cursor: int) -> RecoveryPlan:
        return self.store.update_plan(
            plan.evolve(action_cursor=cursor),
            expected_revision=plan.revision,
            operation="action_cursor_advanced",
            causation_id=self.executor_id,
        )

    def _outcome(
        self,
        plan: RecoveryPlan,
        receipts: Sequence[RecoveryActionReceipt],
        *,
        kind: RecoveryOutcomeKind,
        success: bool,
        summary: str,
        started: float,
        route_decision_id: str,
        checkpoint_id: str,
        error: Exception | None = None,
    ) -> RecoveryOutcome:
        outcome = RecoveryOutcome(
            outcome_id="recoveryoutcome_" + stable_digest({
                "plan_id": plan.plan_id,
                "kind": kind.value,
                "receipts": [item.receipt_id for item in receipts],
                "cursor": plan.action_cursor,
            })[:40],
            plan_id=plan.plan_id,
            signal_id=plan.signal.signal_id,
            kind=kind,
            action=plan.decision.selected.action,
            summary=summary,
            success=success,
            receipt_ids=tuple(item.receipt_id for item in receipts),
            route_decision_id=route_decision_id,
            checkpoint_id=checkpoint_id,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            metadata={
                "plan_status": plan.status.value,
                "action_sequence": [item.value for item in self._sequence(plan)],
                "error_type": type(error).__name__ if error else "",
                "error_code": getattr(error, "code", "") if error else "",
            },
        )
        stored, _ = self.store.append_outcome(outcome)
        return stored

    def _replayed_result(self, plan: RecoveryPlan) -> ExecutionResult:
        outcomes = self.store.outcomes(plan_id=plan.plan_id)
        if not outcomes:
            raise RecoveryActionError("terminal recovery plan has no durable outcome")
        outcome = outcomes[-1]
        feedback = self.store.feedback(task_id=plan.signal.refs.task_id)
        matching = next(
            (
                item for item in reversed(feedback)
                if outcome.outcome_id in item.evidence_refs and item.action is outcome.action
            ),
            None,
        )
        return ExecutionResult(
            plan=plan,
            receipts=self.store.action_receipts(plan_id=plan.plan_id),
            outcome=outcome,
            routing_memory_id=matching.record_id if matching else "",
            replayed=True,
        )

    @staticmethod
    def _sequence(plan: RecoveryPlan) -> tuple[RecoveryAction, ...]:
        raw = plan.provenance.get("action_sequence") or [plan.decision.selected.action.value]
        return tuple(RecoveryAction(str(item)) for item in raw)

    @staticmethod
    def _validate_context(plan: RecoveryPlan, context: RecoveryContext) -> None:
        if plan.signal.refs.run_id != context.refs.run_id or plan.signal.refs.task_id != context.refs.task_id:
            raise RecoveryActionError("recovery context identity mismatch")
        if plan.signal.refs.session_id and plan.signal.refs.session_id != context.refs.session_id:
            raise RecoveryActionError("recovery session identity mismatch")

    @staticmethod
    def _route_constraints(context: RecoveryContext, action: RecoveryAction) -> dict[str, Any]:
        return {
            "allowed_locations": list(context.allowed_locations),
            "privacy_constraints": list(context.privacy_constraints),
            "worker": copy.deepcopy(dict(context.worker_state)),
            "backend": copy.deepcopy(dict(context.backend_state)),
            "provider": copy.deepcopy(dict(context.provider_state)),
            "action": action.value,
        }

    @staticmethod
    def _route_exclusions(plan: RecoveryPlan, action: RecoveryAction) -> tuple[str, ...]:
        refs = plan.signal.refs
        mapping = {
            RecoveryAction.REROUTE: (refs.worker_id,),
            RecoveryAction.SWITCH_BACKEND: (refs.backend_id,),
            RecoveryAction.SWITCH_PROVIDER: (refs.provider_id,),
            RecoveryAction.DEGRADE_MODEL: (str(plan.signal.details.get("model_id") or ""),),
        }
        return tuple(item for item in mapping.get(action, ()) if item)

    @staticmethod
    def _waiting_status(action: RecoveryAction, context: RecoveryContext) -> RecoveryPlanStatus:
        if action is RecoveryAction.ASK_PERMISSION:
            if context.mode is DecisionMode.SEALED_AUTONOMOUS:
                raise RecoveryActionRejected(action, "human_intervention_forbidden", "sealed mode cannot wait for approval")
            return RecoveryPlanStatus.WAITING_PERMISSION
        if action is RecoveryAction.AUTHENTICATE_MCP:
            return RecoveryPlanStatus.WAITING_AUTH
        return RecoveryPlanStatus.WAITING_BACKOFF

    @staticmethod
    def _success_summary(plan: RecoveryPlan, receipts: Sequence[RecoveryActionReceipt]) -> str:
        changed = [item.action.value for item in receipts if item.changed_execution]
        if changed:
            return f"recovery applied execution changes: {', '.join(changed)}"
        return f"recovery sequence completed without route mutation: {plan.decision.selected.action.value}"


def recovery_action_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-action-runtime-contract/v1",
        "owner": "python.RecoveryActionRuntime",
        "actions": [action.value for action in RecoveryAction],
        "route_actions": sorted(action.value for action in RecoveryActionRuntime.ROUTE_ACTIONS),
        "idempotency": "plan+action+cursor+signal+context digest",
        "claiming": "durable compare-and-swap plan lease",
        "outcome_feedback": "every terminal/waiting execution records routing memory",
        "sealed_mode": "never waits for human approval",
    }
