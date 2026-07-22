from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from .action_runtime import ExecutionResult, RecoveryActionRuntime
from .audit_runtime import RecoveryInvariantAuditor
from .component_runtime import RecoveryComponent, RecoveryComponentControl
from .contracts import RecoveryPlan, RecoveryPlanStatus, stable_digest, utc_now
from .store import RecoveryLeaseError, RecoveryPlanStore


class RecoveryRestartError(RuntimeError):
    pass


class RecoveryRestartRejected(RecoveryRestartError):
    pass


class RestartDisposition(StrEnum):
    EXECUTE = "execute"
    RESUME_WAITING = "resume_waiting"
    TAKE_OVER_EXPIRED = "take_over_expired"
    VERIFY_APPLIED = "verify_applied"
    WAIT = "wait"
    TERMINAL = "terminal"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class WaitingCondition:
    resolved: bool
    owner: str
    condition: str
    revision: str
    evidence_ref: str
    checked_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "owner": self.owner,
            "condition": self.condition,
            "revision": self.revision,
            "evidence_ref": self.evidence_ref,
            "checked_at": self.checked_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


class WaitingConditionPort(Protocol):
    def check(self, plan: RecoveryPlan) -> WaitingCondition | Mapping[str, Any]: ...


class CallbackWaitingCondition:
    def __init__(self, callback: Callable[[RecoveryPlan], WaitingCondition | Mapping[str, Any]]) -> None:
        self._callback = callback

    def check(self, plan: RecoveryPlan) -> WaitingCondition:
        value = self._callback(plan)
        if isinstance(value, WaitingCondition):
            return value
        result = dict(value)
        return WaitingCondition(
            resolved=bool(result.get("resolved", False)),
            owner=str(result.get("owner") or "unknown-owner"),
            condition=str(result.get("condition") or plan.status.value),
            revision=str(result.get("revision") or ""),
            evidence_ref=str(result.get("evidence_ref") or ""),
            checked_at=str(result.get("checked_at") or utc_now()),
            metadata=dict(result.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RestartCandidate:
    plan_id: str
    run_id: str
    task_id: str
    status: RecoveryPlanStatus
    disposition: RestartDisposition
    ready: bool
    action_cursor: int
    receipt_count: int
    outcome_count: int
    claim_owner: str
    claim_expires_at: str
    reason: str
    waiting_condition: WaitingCondition | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-restart-candidate/v1",
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status.value,
            "disposition": self.disposition.value,
            "ready": self.ready,
            "action_cursor": self.action_cursor,
            "receipt_count": self.receipt_count,
            "outcome_count": self.outcome_count,
            "claim_owner": self.claim_owner,
            "claim_expires_at": self.claim_expires_at,
            "reason": self.reason,
            "waiting_condition": self.waiting_condition.to_dict() if self.waiting_condition else None,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class RestartExecution:
    candidate: RestartCandidate
    result: ExecutionResult | None
    success: bool
    replayed: bool
    error_code: str = ""
    message: str = ""
    completed_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-restart-execution/v1",
            "candidate": self.candidate.to_dict(),
            "result": self.result.to_dict() if self.result else None,
            "success": self.success,
            "replayed": self.replayed,
            "error_code": self.error_code,
            "message": self.message,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class RestartSweep:
    sweep_id: str
    executor_id: str
    candidates: tuple[RestartCandidate, ...]
    executions: tuple[RestartExecution, ...]
    skipped_plan_ids: tuple[str, ...]
    started_at: str
    completed_at: str

    @property
    def failures(self) -> tuple[RestartExecution, ...]:
        return tuple(item for item in self.executions if not item.success)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-restart-sweep/v1",
            "sweep_id": self.sweep_id,
            "executor_id": self.executor_id,
            "candidates": [item.to_dict() for item in self.candidates],
            "executions": [item.to_dict() for item in self.executions],
            "skipped_plan_ids": list(self.skipped_plan_ids),
            "failure_count": len(self.failures),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class RecoveryRestartRuntime:
    WAITING_STATUSES = {
        RecoveryPlanStatus.WAITING_PERMISSION,
        RecoveryPlanStatus.WAITING_AUTH,
        RecoveryPlanStatus.WAITING_BACKOFF,
    }
    TAKEOVER_STATUSES = {
        RecoveryPlanStatus.CLAIMED,
        RecoveryPlanStatus.APPLYING,
    }

    def __init__(
        self,
        store: RecoveryPlanStore,
        actions: RecoveryActionRuntime,
        context_resolver: Any,
        *,
        waiting_conditions: Mapping[RecoveryPlanStatus, WaitingConditionPort] | None = None,
        components: RecoveryComponentControl | None = None,
        executor_id: str = "recovery-restart-runtime",
    ) -> None:
        self.store = store
        self.actions = actions
        self.context_resolver = context_resolver
        self.waiting_conditions = dict(waiting_conditions or {})
        self.components = components or RecoveryComponentControl()
        self.executor_id = executor_id
        self._lock = threading.RLock()

    def discover(
        self,
        *,
        task_id: str = "",
        run_id: str = "",
        include_terminal: bool = False,
        limit: int = 5000,
        now: datetime | None = None,
    ) -> tuple[RestartCandidate, ...]:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        plans = self.store.plans(task_id=task_id, limit=limit)
        candidates: list[RestartCandidate] = []
        for plan in plans:
            if run_id and plan.signal.refs.run_id != run_id:
                continue
            if plan.status.terminal and not include_terminal:
                continue
            candidates.append(self._candidate(plan, moment))
        return tuple(sorted(candidates, key=lambda item: (
            not item.ready,
            item.task_id,
            item.status.value,
            item.plan_id,
        )))

    def drive(
        self,
        plan_id: str,
        *,
        context_overrides: Mapping[str, Any] | None = None,
        allow_waiting: bool = True,
    ) -> RestartExecution:
        self._require_components()
        with self._lock:
            plan = self.store.plan(plan_id)
            if plan is None:
                raise RecoveryRestartRejected(f"recovery plan not found: {plan_id}")
            candidate = self._candidate(plan, datetime.now(UTC))
            if not candidate.ready:
                raise RecoveryRestartRejected(candidate.reason)
            RecoveryInvariantAuditor(self.store).require_valid(plan.signal.refs.task_id)
            context = self.context_resolver.resolve(plan.signal, dict(context_overrides or {}))
            try:
                if candidate.disposition is RestartDisposition.RESUME_WAITING:
                    if not allow_waiting:
                        raise RecoveryRestartRejected("waiting recovery continuation is disabled for this drive")
                    result = self.actions.resume_waiting(plan.plan_id, context)
                else:
                    result = self.actions.execute(plan.plan_id, context)
                return RestartExecution(
                    candidate=candidate,
                    result=result,
                    success=True,
                    replayed=result.replayed,
                )
            except Exception as error:
                return RestartExecution(
                    candidate=candidate,
                    result=None,
                    success=False,
                    replayed=False,
                    error_code=type(error).__name__,
                    message=str(error)[:2000],
                )

    def sweep(
        self,
        *,
        task_id: str = "",
        run_id: str = "",
        maximum_plans: int = 100,
        stop_on_error: bool = False,
        context_overrides: Mapping[str, Any] | None = None,
    ) -> RestartSweep:
        self._require_components()
        if maximum_plans < 1:
            raise ValueError("maximum restart plans must be positive")
        started = utc_now()
        candidates = self.discover(task_id=task_id, run_id=run_id, limit=max(maximum_plans * 4, 100))
        executions: list[RestartExecution] = []
        skipped: list[str] = []
        for candidate in candidates:
            if len(executions) >= maximum_plans:
                skipped.append(candidate.plan_id)
                continue
            if not candidate.ready:
                skipped.append(candidate.plan_id)
                continue
            execution = self.drive(candidate.plan_id, context_overrides=context_overrides)
            executions.append(execution)
            if stop_on_error and not execution.success:
                skipped.extend(item.plan_id for item in candidates if item.plan_id not in {
                    *(entry.candidate.plan_id for entry in executions),
                    *skipped,
                })
                break
        completed = utc_now()
        sweep_id = "recoverysweep:" + stable_digest({
            "executor_id": self.executor_id,
            "started": started,
            "candidates": [item.plan_id for item in candidates],
            "executions": [item.to_dict() for item in executions],
        })[:40]
        return RestartSweep(
            sweep_id=sweep_id,
            executor_id=self.executor_id,
            candidates=candidates,
            executions=tuple(executions),
            skipped_plan_ids=tuple(dict.fromkeys(skipped)),
            started_at=started,
            completed_at=completed,
        )

    def readiness(self, plan_id: str) -> RestartCandidate:
        plan = self.store.plan(plan_id)
        if plan is None:
            raise RecoveryRestartRejected(f"recovery plan not found: {plan_id}")
        return self._candidate(plan, datetime.now(UTC))

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-restart-runtime-contract/v1",
            "executor_id": self.executor_id,
            "recoverable_statuses": [
                RecoveryPlanStatus.PLANNED.value,
                RecoveryPlanStatus.CLAIMED.value,
                RecoveryPlanStatus.APPLYING.value,
                RecoveryPlanStatus.APPLIED.value,
                *(item.value for item in sorted(self.WAITING_STATUSES, key=lambda status: status.value)),
            ],
            "waiting_owners": {status.value: type(port).__name__ for status, port in self.waiting_conditions.items()},
            "restart_source": "RecoveryPlanStore plus canonical owner refs",
            "copied_owner_mutable_state": False,
            "action_replay": "RecoveryActionRuntime durable receipt idempotency",
        }

    def _candidate(self, plan: RecoveryPlan, now: datetime) -> RestartCandidate:
        receipts = self.store.action_receipts(plan_id=plan.plan_id)
        outcomes = self.store.outcomes(plan_id=plan.plan_id)
        waiting: WaitingCondition | None = None
        if plan.status.terminal:
            disposition = RestartDisposition.TERMINAL
            ready = False
            reason = f"plan is terminal: {plan.status.value}"
        elif plan.status is RecoveryPlanStatus.PLANNED:
            disposition = RestartDisposition.EXECUTE
            ready = True
            reason = "planned recovery has not started"
        elif plan.status in self.TAKEOVER_STATUSES:
            if self._lease_active(plan.claim_expires_at, now) and plan.claimed_by != self.executor_id:
                disposition = RestartDisposition.WAIT
                ready = False
                reason = f"plan claim remains active for {plan.claimed_by}"
            else:
                disposition = RestartDisposition.TAKE_OVER_EXPIRED
                ready = True
                reason = "expired or local recovery claim can be acquired after restart"
        elif plan.status in self.WAITING_STATUSES:
            waiting = self._waiting(plan)
            disposition = RestartDisposition.RESUME_WAITING if waiting.resolved else RestartDisposition.WAIT
            ready = waiting.resolved
            reason = "canonical wait condition resolved" if waiting.resolved else "canonical wait condition is unresolved"
        elif plan.status is RecoveryPlanStatus.APPLIED:
            disposition = RestartDisposition.VERIFY_APPLIED
            ready = bool(receipts) and not any(item.success for item in outcomes)
            reason = "applied owner receipt requires continuation proof" if ready else "applied plan lacks a resumable receipt state"
        else:
            disposition = RestartDisposition.BLOCKED
            ready = False
            reason = f"unsupported restart status: {plan.status.value}"
        checkpoint = self.store.checkpoint_head(plan.signal.refs.task_id)
        if plan.signal.observable_side_effect and checkpoint is None:
            disposition = RestartDisposition.BLOCKED
            ready = False
            reason = "observable side-effect recovery lacks a checkpoint"
        if plan.signal.partial_output and checkpoint is None:
            disposition = RestartDisposition.BLOCKED
            ready = False
            reason = "partial-output recovery lacks a checkpoint"
        return RestartCandidate(
            plan_id=plan.plan_id,
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            status=plan.status,
            disposition=disposition,
            ready=ready,
            action_cursor=plan.action_cursor,
            receipt_count=len(receipts),
            outcome_count=len(outcomes),
            claim_owner=plan.claimed_by,
            claim_expires_at=plan.claim_expires_at,
            reason=reason,
            waiting_condition=waiting,
            metadata={
                "checkpoint_id": checkpoint.checkpoint_id if checkpoint else "",
                "checkpoint_revision": checkpoint.commit_revision if checkpoint else 0,
                "selected_action": plan.decision.selected.action.value,
                "decision_key": plan.decision.deterministic_key,
            },
        )

    def _waiting(self, plan: RecoveryPlan) -> WaitingCondition:
        port = self.waiting_conditions.get(plan.status)
        if port is None:
            return WaitingCondition(
                resolved=False,
                owner="unregistered",
                condition=plan.status.value,
                revision="",
                evidence_ref="",
                metadata={"error": "waiting_condition_owner_unavailable"},
            )
        value = port.check(plan)
        condition = value if isinstance(value, WaitingCondition) else CallbackWaitingCondition(lambda _: value).check(plan)
        if condition.resolved and not condition.evidence_ref:
            raise RecoveryRestartRejected("resolved wait condition lacks canonical evidence ref")
        return condition

    def _require_components(self) -> None:
        self.components.require(RecoveryComponent.PLAN_STORE, operation="restart recovery plan")
        self.components.require(RecoveryComponent.DECISION_RUNTIME, operation="restart recovery plan")
        self.components.require(RecoveryComponent.CONTINUATION_DISPATCH, operation="restart recovery plan")

    @staticmethod
    def _lease_active(value: str, now: datetime) -> bool:
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
        except ValueError:
            return False
        return parsed > now


def restart_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-restart-runtime-surface/v1",
        "durable_source": "RecoveryPlanStore",
        "canonical_owner_refs_only": True,
        "takeover": "expired lease only",
        "waiting_resume": "canonical permission/auth/backoff evidence required",
        "receipt_replay": False,
        "side_effect_replay": False,
    }


__all__ = [
    "CallbackWaitingCondition",
    "RecoveryRestartError",
    "RecoveryRestartRejected",
    "RecoveryRestartRuntime",
    "RestartCandidate",
    "RestartDisposition",
    "RestartExecution",
    "RestartSweep",
    "WaitingCondition",
    "WaitingConditionPort",
    "restart_runtime_contract",
]
