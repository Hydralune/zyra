from __future__ import annotations

import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .canonical import canonicalize, digest, new_identity, utc_now
from .errors import conflict, invalid, unavailable
from .live_models import (
    FaultKind,
    FaultObservation,
    FaultState,
    LiveDomain,
    LivePlan,
)
from .models import FaultInjection


_MIGRATION_FAULTS = {
    FaultKind.WORKER_LOSS,
    FaultKind.NODE_LOSS,
    FaultKind.PROVIDER_RATE_LIMIT,
    FaultKind.PROVIDER_FAILURE,
    FaultKind.EDGE_NETWORK_LOSS,
    FaultKind.NETWORK_LOSS,
}
_CHECKPOINT_FAULTS = {
    FaultKind.REQUIREMENT_CHANGE,
    FaultKind.TOOL_EXCEPTION,
    FaultKind.WORKER_LOSS,
    FaultKind.NODE_LOSS,
    FaultKind.TOOL_TIMEOUT,
    FaultKind.PROVIDER_RATE_LIMIT,
    FaultKind.PROVIDER_FAILURE,
    FaultKind.EDGE_NETWORK_LOSS,
    FaultKind.NETWORK_LOSS,
}


class FaultOwnerPort(Protocol):
    def checkpoint(
        self,
        *,
        injection: FaultInjection,
        effective_step: int,
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def inject(
        self,
        *,
        injection: FaultInjection,
        checkpoint: Mapping[str, Any],
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def recover(
        self,
        *,
        injection: FaultInjection,
        checkpoint: Mapping[str, Any],
        fault_receipt: Mapping[str, Any],
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def migrate(
        self,
        *,
        injection: FaultInjection,
        fault_receipt: Mapping[str, Any],
        recovery_receipt: Mapping[str, Any],
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def reverify(
        self,
        *,
        injection: FaultInjection,
        checkpoint: Mapping[str, Any],
        fault_receipt: Mapping[str, Any],
        recovery_receipt: Mapping[str, Any],
        migration_receipt: Mapping[str, Any],
        scenario_state: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class CallbackFaultOwnerPort:
    def __init__(
        self,
        *,
        checkpoint: Callable[..., Mapping[str, Any]],
        inject: Callable[..., Mapping[str, Any]],
        recover: Callable[..., Mapping[str, Any]],
        migrate: Callable[..., Mapping[str, Any]],
        reverify: Callable[..., Mapping[str, Any]],
    ) -> None:
        self._checkpoint = checkpoint
        self._inject = inject
        self._recover = recover
        self._migrate = migrate
        self._reverify = reverify

    def checkpoint(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._checkpoint(**kwargs)

    def inject(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._inject(**kwargs)

    def recover(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._recover(**kwargs)

    def migrate(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._migrate(**kwargs)

    def reverify(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._reverify(**kwargs)


class UnboundFaultOwnerPort:
    def _fail(self) -> Mapping[str, Any]:
        raise unavailable(
            "live_fault_owner_unbound",
            "Live fault campaign is not bound to canonical checkpoint/fault/recovery owners.",
            phase="fault-campaign",
        )

    def checkpoint(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()

    def inject(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()

    def recover(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()

    def migrate(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()

    def reverify(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()


@dataclass(frozen=True, slots=True)
class FaultExecution:
    injection: FaultInjection
    state: FaultState
    checkpoint_receipt: dict[str, Any]
    fault_receipt: dict[str, Any]
    recovery_receipt: dict[str, Any]
    migration_receipt: dict[str, Any]
    verification_receipt: dict[str, Any]
    observed_step: int
    started_at: str
    completed_at: str
    execution_digest: str

    @classmethod
    def create(
        cls,
        *,
        injection: FaultInjection,
        state: FaultState,
        checkpoint_receipt: Mapping[str, Any],
        fault_receipt: Mapping[str, Any],
        recovery_receipt: Mapping[str, Any],
        migration_receipt: Mapping[str, Any],
        verification_receipt: Mapping[str, Any],
        observed_step: int,
        started_at: str,
        completed_at: str,
    ) -> "FaultExecution":
        payload = {
            "injection": injection.to_dict(),
            "state": state.value,
            "checkpoint_receipt": canonicalize(checkpoint_receipt),
            "fault_receipt": canonicalize(fault_receipt),
            "recovery_receipt": canonicalize(recovery_receipt),
            "migration_receipt": canonicalize(migration_receipt),
            "verification_receipt": canonicalize(verification_receipt),
            "observed_step": observed_step,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        return cls(
            injection=injection,
            state=state,
            checkpoint_receipt=dict(checkpoint_receipt),
            fault_receipt=dict(fault_receipt),
            recovery_receipt=dict(recovery_receipt),
            migration_receipt=dict(migration_receipt),
            verification_receipt=dict(verification_receipt),
            observed_step=observed_step,
            started_at=started_at,
            completed_at=completed_at,
            execution_digest=digest(payload),
        )

    @property
    def recovered(self) -> bool:
        return self.state in {FaultState.RECOVERED, FaultState.REJECTED}

    def to_observation(self) -> FaultObservation:
        fault_event_id = str(
            self.fault_receipt.get("event_id")
            or self.fault_receipt.get("signal_event_id")
            or self.fault_receipt.get("receipt_id")
            or ""
        )
        recovery_event_ids = tuple(
            str(item)
            for item in self.recovery_receipt.get("event_ids")
            or self.recovery_receipt.get("recovery_event_ids")
            or (
                self.recovery_receipt.get("event_id")
                or self.recovery_receipt.get("receipt_id")
                or "",
            )
            if str(item)
        )
        checkpoint_id = str(
            self.checkpoint_receipt.get("checkpoint_id")
            or self.checkpoint_receipt.get("receipt_id")
            or ""
        )
        route_before = str(
            self.migration_receipt.get("route_before")
            or (self.migration_receipt.get("before") or {}).get("route_id")
            or (self.migration_receipt.get("before") or {}).get("routeId")
            or ""
        )
        route_after = str(
            self.migration_receipt.get("route_after")
            or (self.migration_receipt.get("after") or {}).get("route_id")
            or (self.migration_receipt.get("after") or {}).get("routeId")
            or ""
        )
        verifier_event_id = str(
            self.verification_receipt.get("event_id")
            or self.verification_receipt.get("receipt_id")
            or ""
        )
        return FaultObservation(
            injection_id=self.injection.injection_id,
            kind=FaultKind(self.injection.kind),
            state=self.state,
            target=self.injection.target,
            scheduled_after_step=self.injection.after_effective_step,
            observed_step=self.observed_step,
            fault_event_id=fault_event_id,
            recovery_event_ids=recovery_event_ids,
            checkpoint_id=checkpoint_id,
            route_before=route_before,
            route_after=route_after,
            verifier_event_id=verifier_event_id,
            reason=str(
                self.recovery_receipt.get("reason")
                or self.fault_receipt.get("reason")
                or "deterministic fault campaign"
            ),
            metadata={
                "execution_digest": self.execution_digest,
                "checkpoint_receipt_digest": digest(self.checkpoint_receipt),
                "fault_receipt_digest": digest(self.fault_receipt),
                "recovery_receipt_digest": digest(self.recovery_receipt),
                "migration_receipt_digest": digest(self.migration_receipt),
                "verification_receipt_digest": digest(self.verification_receipt),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.live-fault-execution/v1",
            "injection": self.injection.to_dict(),
            "state": self.state.value,
            "checkpoint_receipt": self.checkpoint_receipt,
            "fault_receipt": self.fault_receipt,
            "recovery_receipt": self.recovery_receipt,
            "migration_receipt": self.migration_receipt,
            "verification_receipt": self.verification_receipt,
            "observed_step": self.observed_step,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "execution_digest": self.execution_digest,
        }


class FaultSchedule:
    def __init__(self, injections: Sequence[FaultInjection]) -> None:
        self.injections = tuple(
            sorted(
                injections,
                key=lambda item: (
                    item.after_effective_step,
                    item.injection_id,
                ),
            )
        )
        self._validate()

    @classmethod
    def for_domain(
        cls,
        domain: LiveDomain,
        *,
        overrides: Sequence[FaultInjection] = (),
    ) -> "FaultSchedule":
        selected = tuple(overrides) or (
            _software_defaults()
            if domain is LiveDomain.SOFTWARE_DELIVERY
            else _research_defaults()
        )
        return cls(selected)

    def due(
        self,
        effective_step: int,
        *,
        completed_ids: Iterable[str] = (),
    ) -> tuple[FaultInjection, ...]:
        completed = set(completed_ids)
        return tuple(
            item
            for item in self.injections
            if item.injection_id not in completed
            and item.after_effective_step <= effective_step
        )

    def remaining(self, completed_ids: Iterable[str]) -> tuple[FaultInjection, ...]:
        completed = set(completed_ids)
        return tuple(
            item for item in self.injections if item.injection_id not in completed
        )

    def require_complete(self, completed_ids: Iterable[str]) -> None:
        remaining = self.remaining(completed_ids)
        if remaining:
            raise conflict(
                "fault_schedule_incomplete",
                "Formal scenario ended before every scheduled fault was observed.",
                phase="fault-campaign",
                detail={
                    "remaining": [item.injection_id for item in remaining],
                },
            )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": "zyra.live-fault-schedule/v1",
            "injections": [item.to_dict() for item in self.injections],
        }
        value["schedule_digest"] = digest(value)
        return value

    def _validate(self) -> None:
        identities: set[str] = set()
        previous_step = -1
        for item in self.injections:
            if item.injection_id in identities:
                raise invalid(
                    "fault_schedule_duplicate",
                    "Fault schedule injection identities must be unique.",
                    phase="fault-campaign",
                    detail={"injection_id": item.injection_id},
                )
            identities.add(item.injection_id)
            try:
                FaultKind(item.kind)
            except ValueError as error:
                raise invalid(
                    "fault_schedule_kind_invalid",
                    "Fault schedule contains an unsupported fault kind.",
                    phase="fault-campaign",
                    detail={"kind": item.kind},
                ) from error
            if item.after_effective_step < 1:
                raise invalid(
                    "fault_schedule_step_invalid",
                    "Live fault must be scheduled after a positive effective step.",
                    phase="fault-campaign",
                )
            if item.after_effective_step < previous_step:
                raise invalid(
                    "fault_schedule_order_invalid",
                    "Fault schedule must be monotonic.",
                    phase="fault-campaign",
                )
            previous_step = item.after_effective_step
        kinds = {FaultKind(item.kind) for item in self.injections}
        required_groups = (
            {FaultKind.REQUIREMENT_CHANGE},
            {FaultKind.TOOL_EXCEPTION, FaultKind.TOOL_TIMEOUT},
            {FaultKind.WORKER_LOSS, FaultKind.NODE_LOSS},
            {FaultKind.PROVIDER_RATE_LIMIT, FaultKind.PROVIDER_FAILURE},
            {FaultKind.EDGE_NETWORK_LOSS, FaultKind.NETWORK_LOSS},
        )
        missing = [
            sorted(item.value for item in group)
            for group in required_groups
            if not group.intersection(kinds)
        ]
        if missing:
            raise invalid(
                "fault_schedule_matrix_incomplete",
                "Fault schedule does not cover every representative fault group.",
                phase="fault-campaign",
                detail={"missing_groups": missing},
            )


class RequirementChangeRuntime:
    def apply(
        self,
        *,
        plan: LivePlan,
        injection: FaultInjection,
        completed_action_ids: Sequence[str],
    ) -> dict[str, Any]:
        if FaultKind(injection.kind) is not FaultKind.REQUIREMENT_CHANGE:
            raise invalid(
                "requirement_change_kind_mismatch",
                "Requirement change runtime received another fault kind.",
                phase="fault-campaign",
            )
        requirement = str(
            injection.payload.get("requirement")
            or "Preserve deterministic causal evidence after replanning."
        ).strip()
        if not requirement:
            raise invalid(
                "requirement_change_empty",
                "Requirement change must contain a concrete constraint.",
                phase="fault-campaign",
            )
        affected = tuple(
            action_id
            for action_id in plan.descendants(
                str(
                    injection.payload.get("after_action_id")
                    or next(
                        (
                            item.action_id
                            for item in plan.actions
                            if item.kind.value in {"patch", "transform"}
                        ),
                        plan.actions[0].action_id,
                    )
                )
            )
            if action_id in completed_action_ids
            or action_id not in completed_action_ids
        )
        receipt = {
            "schema": "zyra.live-requirement-change/v1",
            "change_id": injection.injection_id,
            "requirement": requirement,
            "plan_id": plan.plan_id,
            "plan_revision_before": plan.revision,
            "plan_revision_after": plan.revision + 1,
            "completed_action_ids": list(completed_action_ids),
            "invalidated_action_ids": list(affected),
            "replan_required": True,
            "human_intervention_count": 0,
            "applied_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt


class FaultCampaignRuntime:
    def __init__(
        self,
        *,
        owner: FaultOwnerPort,
        schedule: FaultSchedule,
    ) -> None:
        self.owner = owner
        self.schedule = schedule
        self._executions: dict[str, FaultExecution] = {}
        self._requirement_runtime = RequirementChangeRuntime()

    @property
    def executions(self) -> tuple[FaultExecution, ...]:
        return tuple(
            self._executions[item.injection_id]
            for item in self.schedule.injections
            if item.injection_id in self._executions
        )

    @property
    def observations(self) -> tuple[FaultObservation, ...]:
        return tuple(item.to_observation() for item in self.executions)

    def advance(
        self,
        *,
        effective_step: int,
        plan: LivePlan,
        scenario_state: Mapping[str, Any],
        completed_action_ids: Sequence[str],
    ) -> tuple[FaultExecution, ...]:
        due = self.schedule.due(
            effective_step,
            completed_ids=self._executions,
        )
        output: list[FaultExecution] = []
        for injection in due:
            output.append(
                self._execute(
                    injection,
                    effective_step=effective_step,
                    plan=plan,
                    scenario_state=scenario_state,
                    completed_action_ids=completed_action_ids,
                )
            )
        return tuple(output)

    def drain(
        self,
        *,
        effective_step: int,
        plan: LivePlan,
        scenario_state: Mapping[str, Any],
        completed_action_ids: Sequence[str],
    ) -> tuple[FaultExecution, ...]:
        output: list[FaultExecution] = []
        cursor = effective_step
        for injection in self.schedule.remaining(self._executions):
            cursor = max(cursor, injection.after_effective_step)
            output.append(
                self._execute(
                    injection,
                    effective_step=cursor,
                    plan=plan,
                    scenario_state=scenario_state,
                    completed_action_ids=completed_action_ids,
                )
            )
        self.schedule.require_complete(self._executions)
        return tuple(output)

    def require_formal(self) -> dict[str, Any]:
        self.schedule.require_complete(self._executions)
        observations = self.observations
        failures: list[dict[str, Any]] = []
        for item in observations:
            try:
                item.validate()
            except Exception as error:
                failures.append(
                    {
                        "injection_id": item.injection_id,
                        "error": str(error),
                    }
                )
            if not item.resolved:
                failures.append(
                    {
                        "injection_id": item.injection_id,
                        "error": "unresolved",
                    }
                )
        kinds = Counter(item.kind.value for item in observations)
        receipt = {
            "schema": "zyra.live-fault-campaign-verification/v1",
            "valid": not failures,
            "schedule_digest": self.schedule.to_dict()["schedule_digest"],
            "execution_count": len(self._executions),
            "resolved_count": sum(item.resolved for item in observations),
            "kind_counts": dict(kinds),
            "failures": failures,
            "human_intervention_count": 0,
        }
        receipt["receipt_digest"] = digest(receipt)
        if failures:
            raise conflict(
                "fault_campaign_verification_failed",
                "Live fault campaign failed verification.",
                phase="fault-campaign",
                detail=receipt,
            )
        return receipt

    def _execute(
        self,
        injection: FaultInjection,
        *,
        effective_step: int,
        plan: LivePlan,
        scenario_state: Mapping[str, Any],
        completed_action_ids: Sequence[str],
    ) -> FaultExecution:
        if injection.injection_id in self._executions:
            return self._executions[injection.injection_id]
        self._require_components(injection)
        started_at = utc_now()
        checkpoint: Mapping[str, Any] = {}
        if FaultKind(injection.kind) in _CHECKPOINT_FAULTS:
            checkpoint = self.owner.checkpoint(
                injection=injection,
                effective_step=effective_step,
                scenario_state=scenario_state,
            )
            self._require_checkpoint(checkpoint, injection)
        fault_receipt = self.owner.inject(
            injection=injection,
            checkpoint=checkpoint,
            scenario_state=scenario_state,
        )
        self._require_fault_receipt(fault_receipt, injection)
        requirement_receipt: Mapping[str, Any] = {}
        if FaultKind(injection.kind) is FaultKind.REQUIREMENT_CHANGE:
            requirement_receipt = self._requirement_runtime.apply(
                plan=plan,
                injection=injection,
                completed_action_ids=completed_action_ids,
            )
        recovery_state = {
            **dict(scenario_state),
            "requirement_change": dict(requirement_receipt),
        }
        recovery_receipt = self.owner.recover(
            injection=injection,
            checkpoint=checkpoint,
            fault_receipt=fault_receipt,
            scenario_state=recovery_state,
        )
        state = self._require_recovery_receipt(recovery_receipt, injection)
        migration_receipt: Mapping[str, Any] = {}
        if FaultKind(injection.kind) in _MIGRATION_FAULTS:
            migration_receipt = self.owner.migrate(
                injection=injection,
                fault_receipt=fault_receipt,
                recovery_receipt=recovery_receipt,
                scenario_state=recovery_state,
            )
            self._require_migration(migration_receipt, injection)
        else:
            current_route = str(
                scenario_state.get("route_id")
                or (scenario_state.get("route") or {}).get("route_id")
                or ""
            )
            migration_receipt = {
                "receipt_id": new_identity("migration_not_required"),
                "required": False,
                "route_before": current_route,
                "route_after": current_route,
                "reason": "fault kind does not require placement migration",
            }
        verification = self.owner.reverify(
            injection=injection,
            checkpoint=checkpoint,
            fault_receipt=fault_receipt,
            recovery_receipt=recovery_receipt,
            migration_receipt=migration_receipt,
            scenario_state=recovery_state,
        )
        self._require_verification(verification, injection)
        execution = FaultExecution.create(
            injection=injection,
            state=state,
            checkpoint_receipt=checkpoint,
            fault_receipt=fault_receipt,
            recovery_receipt={
                **dict(recovery_receipt),
                "requirement_change": dict(requirement_receipt),
            },
            migration_receipt=migration_receipt,
            verification_receipt=verification,
            observed_step=effective_step,
            started_at=started_at,
            completed_at=utc_now(),
        )
        observation = execution.to_observation()
        observation.validate()
        self._executions[injection.injection_id] = execution
        return execution

    @staticmethod
    def _require_components(injection: FaultInjection) -> None:
        disabled = []
        if _disabled("ZYRA_WORKER_POOL_INTEGRATION_DISABLED"):
            disabled.append("scheduler")
        if _disabled("ZYRA_DISABLE_RECOVERY_RUNTIME"):
            disabled.append("recovery")
        if _disabled("ZYRA_MEMORY_RETRIEVAL_DISABLED"):
            disabled.append("memory")
        if _disabled("ZYRA_TARGETED_COMMUNICATION_DISABLED"):
            disabled.append("low_entropy_communication")
        if disabled:
            raise unavailable(
                "live_required_component_disabled",
                "Formal live fault campaign has no fallback for a disabled owner.",
                phase="fault-campaign",
                detail={
                    "injection_id": injection.injection_id,
                    "disabled": disabled,
                },
            )

    @staticmethod
    def _require_checkpoint(
        receipt: Mapping[str, Any],
        injection: FaultInjection,
    ) -> None:
        required = {
            "checkpoint_id": receipt.get("checkpoint_id"),
            "run_id": receipt.get("run_id"),
            "task_id": receipt.get("task_id"),
            "state_digest": receipt.get("state_digest"),
            "owner": receipt.get("owner"),
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing or receipt.get("committed") is not True:
            raise conflict(
                "fault_checkpoint_invalid",
                "Fault campaign checkpoint was not committed by its owner.",
                phase="fault-campaign",
                detail={
                    "injection_id": injection.injection_id,
                    "missing": missing,
                    "receipt": canonicalize(receipt),
                },
            )

    @staticmethod
    def _require_fault_receipt(
        receipt: Mapping[str, Any],
        injection: FaultInjection,
    ) -> None:
        observed_kind = str(
            receipt.get("kind")
            or receipt.get("fault_kind")
            or receipt.get("signal_kind")
            or ""
        )
        if observed_kind != injection.kind:
            raise conflict(
                "fault_owner_kind_mismatch",
                "Fault owner observed another fault kind.",
                phase="fault-campaign",
                detail={"expected": injection.kind, "observed": observed_kind},
            )
        if not (
            receipt.get("event_id")
            or receipt.get("signal_event_id")
            or receipt.get("receipt_id")
        ):
            raise conflict(
                "fault_owner_event_missing",
                "Fault owner receipt lacks canonical identity.",
                phase="fault-campaign",
            )
        if receipt.get("observed") is not True:
            raise conflict(
                "fault_not_observed",
                "Fault label was not observed by the canonical fault runtime.",
                phase="fault-campaign",
                detail={"injection_id": injection.injection_id},
            )

    @staticmethod
    def _require_recovery_receipt(
        receipt: Mapping[str, Any],
        injection: FaultInjection,
    ) -> FaultState:
        state = str(
            receipt.get("state")
            or receipt.get("status")
            or receipt.get("recovery_state")
            or ""
        ).casefold()
        if state in {"recovered", "resumed", "rerouted", "replanned", "completed"}:
            selected = FaultState.RECOVERED
        elif state in {"rejected", "denied", "safely_failed"}:
            selected = FaultState.REJECTED
        else:
            raise conflict(
                "fault_recovery_incomplete",
                "Canonical recovery owner did not reach a terminal safe state.",
                phase="fault-campaign",
                detail={
                    "injection_id": injection.injection_id,
                    "state": state,
                },
            )
        event_ids = tuple(
            str(item)
            for item in receipt.get("event_ids")
            or receipt.get("recovery_event_ids")
            or (
                receipt.get("event_id")
                or receipt.get("receipt_id")
                or "",
            )
            if str(item)
        )
        if not event_ids:
            raise conflict(
                "fault_recovery_events_missing",
                "Recovery owner receipt lacks canonical events.",
                phase="fault-campaign",
            )
        if receipt.get("checkpoint_restored") is not True:
            raise conflict(
                "fault_checkpoint_not_restored",
                "Recovery owner did not prove checkpoint restore.",
                phase="fault-campaign",
            )
        return selected

    @staticmethod
    def _require_migration(
        receipt: Mapping[str, Any],
        injection: FaultInjection,
    ) -> None:
        before = str(
            receipt.get("route_before")
            or (receipt.get("before") or {}).get("route_id")
            or (receipt.get("before") or {}).get("routeId")
            or ""
        )
        after = str(
            receipt.get("route_after")
            or (receipt.get("after") or {}).get("route_id")
            or (receipt.get("after") or {}).get("routeId")
            or ""
        )
        if not before or not after or before == after:
            raise conflict(
                "fault_placement_migration_invalid",
                "Placement-affecting fault did not migrate to another owner route.",
                phase="fault-campaign",
                detail={
                    "injection_id": injection.injection_id,
                    "before": before,
                    "after": after,
                },
            )
        if not (
            receipt.get("lease_id")
            or receipt.get("receipt_id")
            or (receipt.get("after") or {}).get("lease_id")
        ):
            raise conflict(
                "fault_migration_lease_missing",
                "Placement migration lacks a successor lease receipt.",
                phase="fault-campaign",
            )

    @staticmethod
    def _require_verification(
        receipt: Mapping[str, Any],
        injection: FaultInjection,
    ) -> None:
        if receipt.get("valid") is not True:
            raise conflict(
                "fault_reverification_failed",
                "Post-recovery verifier did not accept the restored state.",
                phase="fault-campaign",
                detail={"injection_id": injection.injection_id},
            )
        if not (receipt.get("event_id") or receipt.get("receipt_id")):
            raise conflict(
                "fault_reverification_identity_missing",
                "Post-recovery verifier lacks canonical identity.",
                phase="fault-campaign",
            )
        if not receipt.get("input_digest") or not receipt.get("state_digest"):
            raise conflict(
                "fault_reverification_binding_missing",
                "Post-recovery verifier is not bound to input and state digests.",
                phase="fault-campaign",
            )


def campaign_events(
    executions: Sequence[FaultExecution],
    *,
    run_id: str,
    task_id: str,
    starting_sequence: int,
    previous_event_id: str,
) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    sequence = starting_sequence
    parent = previous_event_id

    def emit(
        event_type: str,
        effect: str,
        stage: str,
        receipt: Mapping[str, Any],
        *,
        injection_id: str,
    ) -> str:
        nonlocal sequence, parent
        sequence += 1
        receipt_event_id = str(
            receipt.get("event_id")
            or receipt.get("receipt_id")
            or ""
        )
        event_id = (
            f"fault-event-{sequence:06d}-"
            f"{digest((run_id, task_id, event_type, injection_id, receipt, sequence))[:12]}"
        )
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "run_id": run_id,
            "task_id": task_id,
            "sequence": sequence,
            "causation_id": parent,
            "parent_event_id": parent,
            "created_at": str(receipt.get("created_at") or utc_now()),
            "payload": {
                "semantic_effect": effect,
                "mutation": {
                    "injection_id": injection_id,
                    "receipt_digest": digest(receipt),
                    "state": receipt.get("state") or receipt.get("status"),
                },
                "receipt": canonicalize(receipt),
                "receipt_event_id": receipt_event_id,
                "causation_id": parent,
            },
            "metadata": {
                "stage": stage,
                "semantic_effect": effect,
                "worker_id": str(receipt.get("worker_id") or ""),
                "provider_id": str(receipt.get("provider_id") or ""),
            },
        }
        output.append(event)
        parent = event_id
        return event_id

    for execution in executions:
        injection_id = execution.injection.injection_id
        emit(
            "compact_committed",
            "compact_restore",
            "checkpoint",
            execution.checkpoint_receipt,
            injection_id=injection_id,
        )
        emit(
            "fault_observed",
            "fault",
            "fault",
            execution.fault_receipt,
            injection_id=injection_id,
        )
        event_type = (
            "requirement_change"
            if execution.injection.kind == FaultKind.REQUIREMENT_CHANGE.value
            else "recovery_planned"
        )
        emit(
            event_type,
            "recovery",
            "recovery",
            execution.recovery_receipt,
            injection_id=injection_id,
        )
        if execution.injection.kind in {
            item.value for item in _MIGRATION_FAULTS
        }:
            emit(
                "resource_decision",
                "placement",
                "scheduler",
                execution.migration_receipt,
                injection_id=injection_id,
            )
            emit(
                "topology_route",
                "route",
                "scheduler",
                execution.migration_receipt,
                injection_id=injection_id,
            )
        emit(
            "checkpoint_restored",
            "compact_restore",
            "restore",
            execution.recovery_receipt,
            injection_id=injection_id,
        )
        emit(
            "verification",
            "verification",
            "verification",
            execution.verification_receipt,
            injection_id=injection_id,
        )
    return tuple(output)


def campaign_summary(executions: Sequence[FaultExecution]) -> dict[str, Any]:
    kinds = Counter(item.injection.kind for item in executions)
    states = Counter(item.state.value for item in executions)
    migrations = sum(
        1
        for item in executions
        if str(item.migration_receipt.get("route_before") or "")
        and str(item.migration_receipt.get("route_after") or "")
        and item.migration_receipt.get("route_before")
        != item.migration_receipt.get("route_after")
    )
    result = {
        "schema": "zyra.live-fault-campaign-summary/v1",
        "execution_count": len(executions),
        "kind_counts": dict(kinds),
        "state_counts": dict(states),
        "checkpoint_count": sum(bool(item.checkpoint_receipt) for item in executions),
        "migration_count": migrations,
        "reverification_count": sum(
            item.verification_receipt.get("valid") is True for item in executions
        ),
        "execution_digests": [item.execution_digest for item in executions],
    }
    result["summary_digest"] = digest(result)
    return result


def _software_defaults() -> tuple[FaultInjection, ...]:
    return (
        FaultInjection(
            injection_id="software-requirement-change",
            stage="patch",
            kind=FaultKind.REQUIREMENT_CHANGE.value,
            after_effective_step=240,
            target="software-plan",
            payload={
                "requirement": "Retain an input-bound failure-path test and causal receipt.",
            },
        ),
        FaultInjection(
            injection_id="software-tool-timeout",
            stage="test",
            kind=FaultKind.TOOL_TIMEOUT.value,
            after_effective_step=480,
            target="terminal-test",
            payload={"deadline_ms": 10, "retry_deadline_ms": 120_000},
        ),
        FaultInjection(
            injection_id="software-worker-loss",
            stage="test",
            kind=FaultKind.WORKER_LOSS.value,
            after_effective_step=720,
            target="software-worker-primary",
            payload={"successor_required": True},
        ),
        FaultInjection(
            injection_id="software-provider-failure",
            stage="verification",
            kind=FaultKind.PROVIDER_FAILURE.value,
            after_effective_step=900,
            target="provider-primary",
            payload={"failover_required": True},
        ),
        FaultInjection(
            injection_id="software-edge-network-loss",
            stage="delivery",
            kind=FaultKind.EDGE_NETWORK_LOSS.value,
            after_effective_step=1_020,
            target="edge-primary",
            payload={"migrate_to": "device"},
        ),
    )


def _research_defaults() -> tuple[FaultInjection, ...]:
    return (
        FaultInjection(
            injection_id="research-requirement-change",
            stage="source-index",
            kind=FaultKind.REQUIREMENT_CHANGE.value,
            after_effective_step=220,
            target="research-plan",
            payload={
                "requirement": "Expose contradictions and uncertainty separately from deterministic claims.",
            },
        ),
        FaultInjection(
            injection_id="research-network-timeout",
            stage="source-acquisition",
            kind=FaultKind.TOOL_TIMEOUT.value,
            after_effective_step=440,
            target="http-source-worker",
            payload={"deadline_ms": 50, "retry_deadline_ms": 45_000},
        ),
        FaultInjection(
            injection_id="research-node-loss",
            stage="claim-extraction",
            kind=FaultKind.NODE_LOSS.value,
            after_effective_step=660,
            target="researcher-primary",
            payload={"successor_required": True},
        ),
        FaultInjection(
            injection_id="research-provider-rate-limit",
            stage="claim-extraction",
            kind=FaultKind.PROVIDER_RATE_LIMIT.value,
            after_effective_step=880,
            target="provider-primary",
            payload={"retry_after_ms": 250, "failover_required": True},
        ),
        FaultInjection(
            injection_id="research-network-loss",
            stage="citation-verification",
            kind=FaultKind.NETWORK_LOSS.value,
            after_effective_step=1_020,
            target="edge-source-route",
            payload={"continue_from_acquired_checksums": True},
        ),
    )


def _disabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


__all__ = [
    "CallbackFaultOwnerPort",
    "FaultCampaignRuntime",
    "FaultExecution",
    "FaultOwnerPort",
    "FaultSchedule",
    "RequirementChangeRuntime",
    "UnboundFaultOwnerPort",
    "campaign_events",
    "campaign_summary",
]
