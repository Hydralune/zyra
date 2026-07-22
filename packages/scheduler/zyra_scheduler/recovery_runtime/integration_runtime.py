from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .application import RecoveryApplication, RecoveryRunResult
from .causal_runtime import RecoveryCausalTrace, RecoveryCausalTraceRuntime
from .component_runtime import (
    ComponentRequirement,
    RecoveryComponent,
    RecoveryComponentControl,
)
from .contracts import (
    DecisionMode,
    RecoveryAction,
    RecoveryPlan,
    RecoverySignal,
    RecoverySource,
    stable_digest,
    utc_now,
)
from .exact_recovery_runtime import ExactRecoveryRuntime, exact_recovery_runtime_contract
from .feedback_integration_runtime import FeedbackInfluenceProof, RecoveryFeedbackIntegrationRuntime
from .ingress_runtime import (
    ObservationDomain,
    RecoveryAdmission,
    RecoveryAdmissionBatch,
    RecoveryIngressRuntime,
)
from .restart_runtime import RecoveryRestartRuntime, RestartExecution, RestartSweep
from .route_memory_runtime import ExplicitEscalationPlan, MemoryAwareRouteRuntime, RouteEscalationError
from .state_fusion_runtime import FusedRecoveryState, RecoveryStateFusionRuntime
from .store import RecoveryPlanStore


class RecoveryIntegrationError(RuntimeError):
    pass


class RecoveryIntegrationDisabled(RecoveryIntegrationError):
    pass


class RecoveryEscalationExhausted(RecoveryIntegrationError):
    def __init__(self, attempts: Sequence["IntegratedAttempt"]) -> None:
        self.attempts = tuple(attempts)
        messages = [item.message for item in attempts if item.message]
        super().__init__("recovery escalation exhausted: " + "; ".join(messages[-5:]))


class RecoveryIntegrationEventSink(Protocol):
    def emit(self, event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CallbackRecoveryIntegrationEventSink:
    def __init__(self, callback: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]) -> None:
        self._callback = callback

    def emit(self, event_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return copy.deepcopy(dict(self._callback(event_type, payload) or {}))


@dataclass(frozen=True, slots=True)
class IntegratedAttempt:
    ordinal: int
    plan_id: str
    selected_action: RecoveryAction | None
    success: bool
    result: RecoveryRunResult | None
    error_code: str
    message: str
    escalation_id: str = ""
    started_at: str = field(default_factory=utc_now)
    completed_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "plan_id": self.plan_id,
            "selected_action": self.selected_action.value if self.selected_action else "",
            "success": self.success,
            "result": self.result.to_dict() if self.result else None,
            "error_code": self.error_code,
            "message": self.message,
            "escalation_id": self.escalation_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class IntegratedRecoveryResult:
    integration_id: str
    admission: RecoveryAdmission
    fused_state: FusedRecoveryState
    attempts: tuple[IntegratedAttempt, ...]
    escalations: tuple[ExplicitEscalationPlan, ...]
    feedback_influence: FeedbackInfluenceProof | None
    causal_trace: RecoveryCausalTrace | None
    event_receipts: tuple[Mapping[str, Any], ...]
    started_at: str
    completed_at: str

    @property
    def success(self) -> bool:
        return bool(self.attempts and self.attempts[-1].success)

    @property
    def result(self) -> RecoveryRunResult | None:
        return self.attempts[-1].result if self.attempts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.integrated-recovery-result/v1",
            "integration_id": self.integration_id,
            "admission": self.admission.to_dict(),
            "fused_state": self.fused_state.to_dict(),
            "attempts": [item.to_dict() for item in self.attempts],
            "escalations": [item.to_dict() for item in self.escalations],
            "feedback_influence": self.feedback_influence.to_dict() if self.feedback_influence else None,
            "causal_trace": self.causal_trace.to_dict() if self.causal_trace else None,
            "event_receipts": [copy.deepcopy(dict(item)) for item in self.event_receipts],
            "success": self.success,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class RecoveryBatchResult:
    batch: RecoveryAdmissionBatch
    results: tuple[IntegratedRecoveryResult, ...]
    stopped_signal_id: str
    started_at: str
    completed_at: str

    @property
    def success(self) -> bool:
        return all(item.success for item in self.results) and not self.stopped_signal_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.integrated-recovery-batch-result/v1",
            "batch": self.batch.to_dict(),
            "results": [item.to_dict() for item in self.results],
            "stopped_signal_id": self.stopped_signal_id,
            "success": self.success,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class RecoveryIntegrationRuntime:
    def __init__(
        self,
        store: RecoveryPlanStore,
        application: RecoveryApplication,
        ingress: RecoveryIngressRuntime,
        state_fusion: RecoveryStateFusionRuntime,
        routes: MemoryAwareRouteRuntime,
        exact: ExactRecoveryRuntime,
        restart: RecoveryRestartRuntime,
        feedback_integration: RecoveryFeedbackIntegrationRuntime,
        causality: RecoveryCausalTraceRuntime,
        *,
        components: RecoveryComponentControl | None = None,
        event_sinks: Sequence[RecoveryIntegrationEventSink] = (),
        enabled: Callable[[], bool] | None = None,
        maximum_escalations: int = 3,
    ) -> None:
        self.store = store
        self.application = application
        self.ingress = ingress
        self.state_fusion = state_fusion
        self.routes = routes
        self.exact = exact
        self.restart = restart
        self.feedback_integration = feedback_integration
        self.causality = causality
        self.components = components or RecoveryComponentControl()
        self.event_sinks = tuple(event_sinks)
        self.enabled = enabled or (lambda: True)
        self.maximum_escalations = int(maximum_escalations)
        if self.maximum_escalations < 0 or self.maximum_escalations > 16:
            raise ValueError("maximum recovery escalations must be between zero and sixteen")
        self._lock = threading.RLock()

    def observe_and_recover(
        self,
        domain: ObservationDomain | str,
        payload: Mapping[str, Any],
        *,
        owner: str = "",
        owner_revision: str = "",
        event_ids: Sequence[str] = (),
        span_id: str = "",
        context_overrides: Mapping[str, Any] | None = None,
        apply: bool = True,
        allow_escalation: bool = False,
        require_causal_trace: bool = True,
        idempotency_key: str = "",
    ) -> IntegratedRecoveryResult:
        self._require_enabled()
        admission = self.ingress.observe(
            domain,
            payload,
            owner=owner,
            owner_revision=owner_revision,
            event_ids=event_ids,
            span_id=span_id,
        )
        return self.recover_admission(
            admission,
            context_overrides=context_overrides,
            apply=apply,
            allow_escalation=allow_escalation,
            require_causal_trace=require_causal_trace,
            idempotency_key=idempotency_key,
        )

    def recover_admission(
        self,
        admission: RecoveryAdmission,
        *,
        context_overrides: Mapping[str, Any] | None = None,
        apply: bool = True,
        allow_escalation: bool = False,
        require_causal_trace: bool = True,
        idempotency_key: str = "",
    ) -> IntegratedRecoveryResult:
        self._require_enabled()
        started = utc_now()
        overrides = copy.deepcopy(dict(context_overrides or {}))
        fused = self.state_fusion.fuse(admission.signal, overrides)
        if len(fused.families) < 3:
            raise RecoveryIntegrationError("integrated recovery requires at least three real state families")
        receipts = list(self._emit("recovery_observation_admitted", {
            "run_id": admission.signal.refs.run_id,
            "task_id": admission.signal.refs.task_id,
            "signal_id": admission.signal.signal_id,
            "observation_digest": admission.observation.digest,
            "domain": admission.observation.domain.value,
            "owner": admission.observation.owner,
            "span_id": admission.observation.span_id,
            "state_families": [item.value for item in fused.families],
            "state_fusion_digest": fused.digest,
        }))
        attempts: list[IntegratedAttempt] = []
        escalations: list[ExplicitEscalationPlan] = []
        forbidden = [RecoveryAction(str(item)) for item in overrides.get("forbidden_actions") or ()]
        with self._lock:
            for ordinal in range(1, self.maximum_escalations + 2):
                attempt_started = utc_now()
                plan_ids_before = {item.plan_id for item in self.store.plans(task_id=admission.signal.refs.task_id, limit=5000)}
                local_overrides = {
                    **overrides,
                    "forbidden_actions": [item.value for item in forbidden],
                    "metadata": {
                        **dict(overrides.get("metadata") or {}),
                        "integration_idempotency_key": idempotency_key,
                        "observation_digest": admission.observation.digest,
                        "state_fusion_digest": fused.digest,
                        "explicit_escalation": bool(escalations),
                        "escalation_ids": [item.escalation_id for item in escalations],
                    },
                }
                try:
                    result = self.application.recover_signal(
                        admission.signal,
                        context_overrides=local_overrides,
                        apply=apply,
                        idempotency_key=(
                            f"{idempotency_key}:attempt:{ordinal}" if idempotency_key else ""
                        ),
                    )
                    attempts.append(IntegratedAttempt(
                        ordinal=ordinal,
                        plan_id=result.plan.plan_id,
                        selected_action=result.plan.decision.selected.action,
                        success=True,
                        result=result,
                        error_code="",
                        message="recovery plan applied" if result.execution else "recovery plan created",
                        escalation_id=escalations[-1].escalation_id if escalations else "",
                        started_at=attempt_started,
                        completed_at=utc_now(),
                    ))
                    receipts.extend(self._emit("recovery_integration_applied", {
                        "run_id": admission.signal.refs.run_id,
                        "task_id": admission.signal.refs.task_id,
                        "signal_id": admission.signal.signal_id,
                        "plan_id": result.plan.plan_id,
                        "action": result.plan.decision.selected.action.value,
                        "outcome_id": result.execution.outcome.outcome_id if result.execution else "",
                        "applied_proof_id": (
                            str(result.execution.applied_proof.get("proof_id") or "")
                            if result.execution else ""
                        ),
                        "routing_memory_id": result.execution.routing_memory_id if result.execution else "",
                        "escalation_ids": [item.escalation_id for item in escalations],
                    }))
                    break
                except Exception as error:
                    failed_plan = self._newest_plan(
                        admission.signal,
                        excluded_plan_ids=plan_ids_before,
                    ) or self._latest_signal_plan(admission.signal)
                    attempts.append(IntegratedAttempt(
                        ordinal=ordinal,
                        plan_id=failed_plan.plan_id if failed_plan else "",
                        selected_action=failed_plan.decision.selected.action if failed_plan else None,
                        success=False,
                        result=None,
                        error_code=type(error).__name__,
                        message=str(error)[:2000],
                        escalation_id=escalations[-1].escalation_id if escalations else "",
                        started_at=attempt_started,
                        completed_at=utc_now(),
                    ))
                    receipts.extend(self._emit("recovery_integration_attempt_failed", {
                        "run_id": admission.signal.refs.run_id,
                        "task_id": admission.signal.refs.task_id,
                        "signal_id": admission.signal.signal_id,
                        "plan_id": failed_plan.plan_id if failed_plan else "",
                        "error_type": type(error).__name__,
                        "message": str(error)[:2000],
                        "ordinal": ordinal,
                    }))
                    if not apply or not allow_escalation or ordinal > self.maximum_escalations:
                        raise
                    if failed_plan is None:
                        raise RecoveryIntegrationError("failed recovery did not persist a plan for escalation") from error
                    escalation = self._escalation(failed_plan, error)
                    escalations.append(escalation)
                    forbidden.append(failed_plan.decision.selected.action)
                    receipts.extend(self._emit("recovery_route_escalation_authorized", {
                        "run_id": admission.signal.refs.run_id,
                        "task_id": admission.signal.refs.task_id,
                        "signal_id": admission.signal.signal_id,
                        "plan_id": failed_plan.plan_id,
                        "escalation": escalation.to_dict(),
                    }))
            else:
                raise RecoveryEscalationExhausted(attempts)
        if not attempts or not attempts[-1].success:
            raise RecoveryEscalationExhausted(attempts)
        final_plan_id = attempts[-1].plan_id
        feedback_influence: FeedbackInfluenceProof | None = None
        final_result = attempts[-1].result
        if apply and final_result is not None and final_result.execution is not None:
            feedback_influence = self.feedback_integration.verify_execution(
                final_result.plan,
                final_result.execution,
                fused.context,
            )
            receipts.extend(self._emit("recovery_feedback_influence_proven", {
                "run_id": admission.signal.refs.run_id,
                "task_id": admission.signal.refs.task_id,
                "signal_id": admission.signal.signal_id,
                "plan_id": final_plan_id,
                "proof_id": feedback_influence.proof_id,
                "routing_memory_id": feedback_influence.routing_memory_id,
                "changed_later_decision": feedback_influence.changed_later_decision,
            }))
        trace: RecoveryCausalTrace | None = None
        if apply:
            trace = self.causality.trace_plan(final_plan_id, require_complete=require_causal_trace)
            receipts.extend(self._emit("recovery_causal_trace_closed", {
                "run_id": admission.signal.refs.run_id,
                "task_id": admission.signal.refs.task_id,
                "signal_id": admission.signal.signal_id,
                "plan_id": final_plan_id,
                "trace_id": trace.trace_id,
                "complete": trace.complete,
                "fact_kinds": [item.value for item in trace.fact_kinds],
            }))
        completed = utc_now()
        integration_id = "recoveryintegration:" + stable_digest({
            "observation": admission.observation.digest,
            "fusion": fused.digest,
            "attempts": [item.to_dict() for item in attempts],
            "feedback_proof_id": feedback_influence.proof_id if feedback_influence else "",
            "trace_id": trace.trace_id if trace else "",
        })[:40]
        return IntegratedRecoveryResult(
            integration_id=integration_id,
            admission=admission,
            fused_state=fused,
            attempts=tuple(attempts),
            escalations=tuple(escalations),
            feedback_influence=feedback_influence,
            causal_trace=trace,
            event_receipts=tuple(receipts),
            started_at=started,
            completed_at=completed,
        )

    def recover_batch(
        self,
        observations: Sequence[Mapping[str, Any]],
        *,
        context_overrides: Mapping[str, Any] | None = None,
        apply: bool = True,
        allow_escalation: bool = False,
        stop_on_error: bool = True,
    ) -> RecoveryBatchResult:
        self._require_enabled()
        started = utc_now()
        batch = self.ingress.batch(observations)
        results: list[IntegratedRecoveryResult] = []
        stopped = ""
        for admission in batch.admissions:
            try:
                result = self.recover_admission(
                    admission,
                    context_overrides=context_overrides,
                    apply=apply,
                    allow_escalation=allow_escalation,
                    require_causal_trace=apply,
                )
            except Exception:
                stopped = admission.signal.signal_id
                if stop_on_error:
                    break
                continue
            results.append(result)
        return RecoveryBatchResult(
            batch=batch,
            results=tuple(results),
            stopped_signal_id=stopped,
            started_at=started,
            completed_at=utc_now(),
        )

    def restart_plan(
        self,
        plan_id: str,
        *,
        context_overrides: Mapping[str, Any] | None = None,
    ) -> RestartExecution:
        self._require_enabled()
        result = self.restart.drive(plan_id, context_overrides=context_overrides)
        self._emit("recovery_restart_plan_driven", {
            "run_id": result.candidate.run_id,
            "task_id": result.candidate.task_id,
            "plan_id": result.candidate.plan_id,
            "success": result.success,
            "replayed": result.replayed,
            "error_code": result.error_code,
        })
        return result

    def restart_sweep(
        self,
        *,
        task_id: str = "",
        run_id: str = "",
        maximum_plans: int = 100,
        context_overrides: Mapping[str, Any] | None = None,
    ) -> RestartSweep:
        self._require_enabled()
        sweep = self.restart.sweep(
            task_id=task_id,
            run_id=run_id,
            maximum_plans=maximum_plans,
            context_overrides=context_overrides,
        )
        for execution in sweep.executions:
            self._emit("recovery_restart_plan_driven", {
                "run_id": execution.candidate.run_id,
                "task_id": execution.candidate.task_id,
                "plan_id": execution.candidate.plan_id,
                "sweep_id": sweep.sweep_id,
                "success": execution.success,
                "replayed": execution.replayed,
                "error_code": execution.error_code,
            })
        return sweep

    def task_view(self, task_id: str) -> dict[str, Any]:
        base = self.application.task_view(task_id)
        base["restart_candidates"] = [item.to_dict() for item in self.restart.discover(task_id=task_id)]
        base["causal_traces"] = [item.to_dict() for item in self.causality.task_traces(task_id)]
        base["component_matrix"] = self.components.matrix()
        return base

    def plan_view(self, plan_id: str) -> dict[str, Any]:
        base = self.application.plan_view(plan_id)
        base["restart_candidate"] = self.restart.readiness(plan_id).to_dict()
        base["causal_trace"] = self.causality.trace_plan(plan_id).to_dict()
        return base

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-integration-runtime-contract/v1",
            "enabled": bool(self.enabled()),
            "main_path": (
                "typed owner observation -> RecoverySignalClassifier -> three-plus owner state fusion -> "
                "RecoveryDecisionRuntime -> RecoveryPlanStore -> canonical owner action -> continuation proof -> "
                "RoutingMemoryFeedback -> causal trace"
            ),
            "ingress": self.ingress.contract(),
            "state_fusion": self.state_fusion.contract(),
            "exact_recovery": exact_recovery_runtime_contract(),
            "restart": self.restart.contract(),
            "feedback_integration": self.feedback_integration.contract(),
            "causality": self.causality.contract(),
            "components": self.components.matrix(),
            "maximum_escalations": self.maximum_escalations,
            "independent_policy_owner": False,
            "legacy_fallback": False,
        }

    def _escalation(self, plan: RecoveryPlan, error: Exception) -> ExplicitEscalationPlan:
        failed_receipts = [
            item.receipt_id
            for item in self.store.action_receipts(plan_id=plan.plan_id)
            if not item.ok or not item.changed_execution
        ]
        if not failed_receipts:
            failed_receipts = [f"failed-plan:{plan.plan_id}"]
        allowed = [
            item.action for item in plan.decision.candidates
            if item.eligible and item.action is not plan.decision.selected.action
        ]
        try:
            return self.routes.build_escalation(
                plan,
                failed_owner_receipt_ids=failed_receipts,
                allowed_actions=allowed,
                reason=f"{type(error).__name__}: {error}",
            )
        except RouteEscalationError as escalation_error:
            raise RecoveryIntegrationError(str(escalation_error)) from error

    def _newest_plan(
        self,
        signal: RecoverySignal,
        *,
        excluded_plan_ids: set[str],
    ) -> RecoveryPlan | None:
        candidates = [
            plan for plan in self.store.plans(task_id=signal.refs.task_id, limit=5000)
            if plan.signal.signal_id == signal.signal_id and plan.plan_id not in excluded_plan_ids
        ]
        return max(candidates, key=lambda plan: (plan.created_at, plan.plan_id), default=None)

    def _latest_signal_plan(self, signal: RecoverySignal) -> RecoveryPlan | None:
        candidates = [
            plan for plan in self.store.plans(task_id=signal.refs.task_id, limit=5000)
            if plan.signal.signal_id == signal.signal_id
        ]
        return max(candidates, key=lambda plan: (plan.updated_at, plan.plan_id), default=None)

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        receipts: list[Mapping[str, Any]] = []
        for sink in self.event_sinks:
            receipt = sink.emit(event_type, payload)
            if receipt:
                receipts.append(copy.deepcopy(dict(receipt)))
        return tuple(receipts)

    def _require_enabled(self) -> None:
        if not self.enabled():
            raise RecoveryIntegrationDisabled("recovery integration runtime is disabled")
        self.components.require_all((
            ComponentRequirement(RecoveryComponent.CLASSIFIER, "classify integrated recovery observation"),
            ComponentRequirement(RecoveryComponent.DECISION_RUNTIME, "select integrated recovery action"),
            ComponentRequirement(RecoveryComponent.PLAN_STORE, "persist integrated recovery state"),
            ComponentRequirement(RecoveryComponent.MEMORY_FEEDBACK, "apply routing memory feedback"),
            ComponentRequirement(RecoveryComponent.CONTINUATION_DISPATCH, "prove changed downstream execution"),
        ))


def integration_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-integration-surface/v1",
        "required_runtime_types": [
            "RecoverySignalClassifier",
            "RecoveryDecisionRuntime",
            "RecoveryPlanStore",
            "CheckpointResumeBridge",
            "RoutingMemoryFeedback",
        ],
        "minimum_state_families": 3,
        "route_layers": ["graph", "worker", "backend", "provider/model"],
        "memory_after_applied_continuation_only": True,
        "restart_from": "RecoveryPlanStore plus canonical owner refs",
        "llm_applied_action_authority": False,
    }


__all__ = [
    "CallbackRecoveryIntegrationEventSink",
    "IntegratedAttempt",
    "IntegratedRecoveryResult",
    "RecoveryBatchResult",
    "RecoveryEscalationExhausted",
    "RecoveryIntegrationDisabled",
    "RecoveryIntegrationError",
    "RecoveryIntegrationEventSink",
    "RecoveryIntegrationRuntime",
    "integration_runtime_contract",
]
