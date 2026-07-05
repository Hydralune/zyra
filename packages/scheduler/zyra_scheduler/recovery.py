from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from zyra_core import EventRecord, EventType, PlanNode, TaskState, to_jsonable

from .models import FailureKind, FailureSignal, RecoveryAction, RecoveryPlan
from .scheduler import ResourceScheduler


RECOVERY_SOURCE_MODULES = {
    "openclaw": ["fault injection", "active-memory recovery", "trajectory"],
    "OpenHands": ["controller recovery loop", "event stream"],
    "agent-framework": ["workflow middleware recovery"],
    "claude-code-best": ["permission runtime", "rewind/resume/compact commands"],
    "browser-use": ["browser session recovery"],
}


class RecoveryPlanner:
    def __init__(self, scheduler: ResourceScheduler | None = None) -> None:
        self.scheduler = scheduler or ResourceScheduler()

    def plan(
        self,
        state: TaskState,
        signal: FailureSignal,
        *,
        node: PlanNode | None = None,
        cause_event: EventRecord | Mapping[str, Any] | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
        memory_records: Sequence[Any] | None = None,
    ) -> RecoveryPlan:
        actions = self._actions(signal)
        avoid = [signal.failed_worker] if signal.failed_worker else []
        decision = self.scheduler.decide(
            state,
            node=node,
            cause_event=cause_event,
            events=events or [],
            memory_records=memory_records or [],
            avoid_workers=avoid,
        )
        self.scheduler.attach_decision_to_state(state, decision, node=node)
        summary = self._summary(signal, actions, decision.selected_worker)
        plan = RecoveryPlan(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=None if node is None else node.node_id,
            failure_signal_id=signal.signal_id,
            actions=actions,
            summary=summary,
            selected_worker=decision.selected_worker,
            selected_manifest_id=decision.selected_manifest_id,
            decision=decision,
            can_continue=signal.retryable or RecoveryAction.LOCAL_REPLAN in actions,
            affected_node_ids=[item for item in [signal.node_id, None if node is None else node.node_id] if item],
            source_modules=RECOVERY_SOURCE_MODULES,
            metadata={
                "failure_kind": str(signal.kind),
                "failed_worker": signal.failed_worker,
                "severity": signal.severity,
                "decision_id": decision.decision_id,
            },
        )
        self.attach_plan_to_state(state, plan)
        return plan

    def event_for_plan(self, plan: RecoveryPlan, signal: FailureSignal) -> EventRecord:
        return EventRecord(
            run_id=plan.run_id,
            task_id=plan.task_id,
            event_type=EventType.RECOVERY_PLANNED,
            node_id=plan.node_id,
            payload={
                "failure_signal": to_jsonable(signal),
                "recovery_plan": to_jsonable(plan),
                "selected_worker": plan.selected_worker,
                "selected_manifest_id": plan.selected_manifest_id,
                "actions": [str(action) for action in plan.actions],
            },
        )

    def attach_plan_to_state(self, state: TaskState, plan: RecoveryPlan) -> None:
        state.metadata["last_recovery_plan"] = to_jsonable(plan)
        state.metadata.setdefault("recovery_plans", []).append(to_jsonable(plan))

    def _actions(self, signal: FailureSignal) -> list[RecoveryAction]:
        if signal.kind == FailureKind.PERMISSION_DENIED:
            return [RecoveryAction.LOCAL_REPLAN, RecoveryAction.EXPLAIN_FAILURE]
        if signal.kind in {FailureKind.BROWSER_CRASH, FailureKind.WORKER_UNAVAILABLE, FailureKind.TOOL_TIMEOUT}:
            return [RecoveryAction.CHECKPOINT_RESUME, RecoveryAction.REROUTE_WORKER]
        if signal.kind == FailureKind.MODEL_ERROR:
            return [RecoveryAction.FALLBACK_MODEL, RecoveryAction.REROUTE_WORKER]
        if signal.kind in {FailureKind.SCHEMA_ERROR, FailureKind.VALIDATION_FAILED, FailureKind.REQUIREMENT_DRIFT}:
            return [RecoveryAction.LOCAL_REPLAN, RecoveryAction.FALLBACK_MODEL]
        if signal.kind == FailureKind.NODE_FAILED:
            return [RecoveryAction.CHECKPOINT_RESUME, RecoveryAction.REROUTE_WORKER]
        return [RecoveryAction.LOCAL_REPLAN, RecoveryAction.REROUTE_WORKER]

    def _summary(
        self,
        signal: FailureSignal,
        actions: Sequence[RecoveryAction],
        selected_worker: str,
    ) -> str:
        action_text = ", ".join(str(action) for action in actions)
        return f"{signal.summary} Recovery will use {action_text} with {selected_worker}."
