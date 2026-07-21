from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .errors import WorkerPoolError, WorkerPoolErrorCode
from .leases import WorkerLeaseManager
from .models import (
    AttemptState,
    CapabilityRequirement,
    LeaseAcquisition,
    LeaseState,
    ResourceVector,
    TaskAttempt,
    WorkerLease,
    WorkerLocation,
    stable_digest,
    utc_iso,
)
from .store import WorkerPoolStore


class TakeoverDisposition(StrEnum):
    RETRY_ALTERNATE_WORKER = "retry_alternate_worker"
    RETRY_SAME_WORKER_NEW_GENERATION = "retry_same_worker_new_generation"
    REPLAN_REQUIRED = "replan_required"
    TERMINAL = "terminal"


class TakeoverReason(StrEnum):
    LEASE_EXPIRED = "lease_expired"
    WORKER_LOST = "worker_lost"
    WORKER_DRAINED = "worker_drained"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    PROCESS_RESTART = "process_restart"
    MANUAL_RECOVERY = "manual_recovery"


@dataclass(frozen=True, slots=True)
class WorkerFailureSignal:
    task_id: str
    run_id: str
    attempt_id: str
    lease_id: str
    worker_id: str
    reason: TakeoverReason
    message: str
    recoverable: bool
    signal_id: str
    observed_at: str = field(default_factory=utc_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "reason": self.reason.value,
            "message": self.message,
            "recoverable": self.recoverable,
            "signal_id": self.signal_id,
            "observed_at": self.observed_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class WorkerTakeoverPlan:
    signal: WorkerFailureSignal
    disposition: TakeoverDisposition
    next_attempt_number: int
    requirement: CapabilityRequirement
    excluded_worker_ids: tuple[str, ...]
    preferred_worker_ids: tuple[str, ...]
    owner_session_id: str
    idempotency_key: str
    rationale: tuple[str, ...]
    candidate_worker_ids: tuple[str, ...] = ()
    plan_id: str = ""
    created_at: str = field(default_factory=utc_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.plan_id:
            object.__setattr__(
                self,
                "plan_id",
                "takeover_plan_"
                + stable_digest(
                    (
                        self.signal.signal_id,
                        self.next_attempt_number,
                        self.requirement.to_dict(),
                        self.excluded_worker_ids,
                    )
                )[:24],
            )

    @property
    def executable(self) -> bool:
        return self.disposition in {
            TakeoverDisposition.RETRY_ALTERNATE_WORKER,
            TakeoverDisposition.RETRY_SAME_WORKER_NEW_GENERATION,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal.to_dict(),
            "disposition": self.disposition.value,
            "next_attempt_number": self.next_attempt_number,
            "requirement": self.requirement.to_dict(),
            "excluded_worker_ids": list(self.excluded_worker_ids),
            "preferred_worker_ids": list(self.preferred_worker_ids),
            "owner_session_id": self.owner_session_id,
            "idempotency_key": self.idempotency_key,
            "rationale": list(self.rationale),
            "candidate_worker_ids": list(self.candidate_worker_ids),
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class WorkerTakeoverReceipt:
    plan: WorkerTakeoverPlan
    acquisition: LeaseAcquisition | None
    executed: bool
    changed: bool
    failure_code: str = ""
    failure_message: str = ""
    receipt_id: str = ""
    completed_at: str = field(default_factory=utc_iso)

    def __post_init__(self) -> None:
        if not self.receipt_id:
            object.__setattr__(
                self,
                "receipt_id",
                "takeover_receipt_"
                + stable_digest(
                    (
                        self.plan.plan_id,
                        self.acquisition.lease.lease_id if self.acquisition else "",
                        self.executed,
                        self.failure_code,
                    )
                )[:24],
            )

    def to_dict(self, *, include_fence_token: bool = False) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "acquisition": (
                self.acquisition.to_dict(include_fence_token=include_fence_token)
                if self.acquisition
                else None
            ),
            "executed": self.executed,
            "changed": self.changed,
            "failure_code": self.failure_code,
            "failure_message": self.failure_message,
            "receipt_id": self.receipt_id,
            "completed_at": self.completed_at,
        }


class WorkerTakeoverRuntime:
    """Builds deterministic attempt takeover without taking logical-task ownership."""

    def __init__(self, store: WorkerPoolStore, leases: WorkerLeaseManager) -> None:
        self.store = store
        self.leases = leases

    def signal_for_attempt(
        self,
        attempt_id: str,
        *,
        reason: TakeoverReason,
        message: str,
        recoverable: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> WorkerFailureSignal:
        attempt = self.store.require_attempt(attempt_id)
        lease = self.store.lease_for_attempt(attempt_id)
        if lease is None:
            raise WorkerPoolError(
                WorkerPoolErrorCode.LEASE_NOT_FOUND,
                "lost physical attempt has no lease recovery reference",
                operation="build_worker_failure_signal",
                task_id=attempt.task_id,
                attempt_id=attempt_id,
            )
        signal_id = "worker_failure_" + stable_digest(
            (attempt.task_id, attempt.attempt_id, lease.lease_id, reason.value, message)
        )[:24]
        return WorkerFailureSignal(
            task_id=attempt.task_id,
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            lease_id=lease.lease_id,
            worker_id=lease.worker_id,
            reason=reason,
            message=message,
            recoverable=recoverable,
            signal_id=signal_id,
            metadata={
                **dict(metadata or {}),
                "logical_task_owner": "typescript.AgentTaskRuntime",
                "physical_recovery_owner": "WorkerTakeoverRuntime",
            },
        )

    def plan(
        self,
        signal: WorkerFailureSignal,
        *,
        requirement: CapabilityRequirement | None = None,
        owner_session_id: str = "",
        allow_same_worker_generation: bool = False,
    ) -> WorkerTakeoverPlan:
        attempt = self.store.require_attempt(signal.attempt_id)
        lease = self.store.require_lease(signal.lease_id)
        if attempt.task_id != signal.task_id or lease.attempt_id != signal.attempt_id:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                "worker failure signal does not match canonical attempt and lease",
                operation="plan_worker_takeover",
                task_id=signal.task_id,
                attempt_id=signal.attempt_id,
                lease_id=signal.lease_id,
            )
        effective_requirement = requirement or _requirement_from_lease(lease)
        if attempt.terminal and attempt.state is not AttemptState.LOST:
            return WorkerTakeoverPlan(
                signal=signal,
                disposition=TakeoverDisposition.TERMINAL,
                next_attempt_number=attempt.attempt_number + 1,
                requirement=effective_requirement,
                excluded_worker_ids=(signal.worker_id,),
                preferred_worker_ids=(),
                owner_session_id=owner_session_id or lease.owner_session_id,
                idempotency_key=f"takeover:{signal.signal_id}:terminal",
                rationale=(f"attempt is already terminal in {attempt.state.value} state",),
            )
        if not signal.recoverable:
            return WorkerTakeoverPlan(
                signal=signal,
                disposition=TakeoverDisposition.REPLAN_REQUIRED,
                next_attempt_number=attempt.attempt_number + 1,
                requirement=effective_requirement,
                excluded_worker_ids=(signal.worker_id,),
                preferred_worker_ids=(),
                owner_session_id=owner_session_id or lease.owner_session_id,
                idempotency_key=f"takeover:{signal.signal_id}:replan",
                rationale=("failure was classified as unrecoverable",),
            )
        excluded = () if allow_same_worker_generation else (signal.worker_id,)
        candidates = self.leases.rank_candidates(
            effective_requirement,
            excluded_worker_ids=excluded,
        )
        accepted = tuple(item.worker_id for item in candidates if item.accepted)
        if accepted:
            disposition = TakeoverDisposition.RETRY_ALTERNATE_WORKER
            rationale = (
                "canonical old lease is terminal or will be fenced before takeover",
                "an alternate worker satisfies the preserved capability and location requirement",
            )
        elif allow_same_worker_generation:
            same = self.store.get_worker(signal.worker_id)
            original_generation = int(lease.metadata.get("worker_generation") or 0)
            if same is not None and same.generation > original_generation and same.accepting_leases:
                disposition = TakeoverDisposition.RETRY_SAME_WORKER_NEW_GENERATION
                accepted = (same.worker_id,)
                rationale = (
                    "the worker id returned with a strictly newer attested process generation",
                    "the previous fence token and epoch remain invalid",
                )
            else:
                disposition = TakeoverDisposition.REPLAN_REQUIRED
                rationale = ("no compatible worker is available for deterministic takeover",)
        else:
            disposition = TakeoverDisposition.REPLAN_REQUIRED
            rationale = (
                "no alternate worker satisfies the preserved capability/location requirement",
                "edge-only work cannot silently fall back to a local worker",
            )
        return WorkerTakeoverPlan(
            signal=signal,
            disposition=disposition,
            next_attempt_number=attempt.attempt_number + 1,
            requirement=effective_requirement,
            excluded_worker_ids=excluded,
            preferred_worker_ids=accepted,
            owner_session_id=owner_session_id or lease.owner_session_id,
            idempotency_key=f"takeover:{signal.signal_id}:{attempt.attempt_number + 1}",
            rationale=rationale,
            candidate_worker_ids=accepted,
            metadata={
                "old_attempt_id": attempt.attempt_id,
                "old_lease_id": lease.lease_id,
                "old_fence_epoch": lease.fence_epoch,
                "requirement_preserved": True,
            },
        )

    def execute(self, plan: WorkerTakeoverPlan) -> WorkerTakeoverReceipt:
        if not plan.executable:
            return WorkerTakeoverReceipt(
                plan=plan,
                acquisition=None,
                executed=False,
                changed=False,
                failure_code=plan.disposition.value,
                failure_message="; ".join(plan.rationale),
            )
        old_lease = self.store.require_lease(plan.signal.lease_id)
        if old_lease.state in {LeaseState.ACTIVE, LeaseState.DRAINING}:
            self.leases.expire(
                old_lease.lease_id,
                reason=f"takeover plan {plan.plan_id}: {plan.signal.message}",
            )
        try:
            acquisition = self.leases.acquire(
                task_id=plan.signal.task_id,
                run_id=plan.signal.run_id,
                owner_session_id=plan.owner_session_id,
                requirement=plan.requirement,
                attempt_number=plan.next_attempt_number,
                preferred_worker_ids=plan.preferred_worker_ids,
                excluded_worker_ids=plan.excluded_worker_ids,
                idempotency_key=plan.idempotency_key,
                recovery_reason=plan.signal.message,
                parent_attempt_id=plan.signal.attempt_id,
                metadata={
                    "takeover_plan_id": plan.plan_id,
                    "failure_signal_id": plan.signal.signal_id,
                    "logical_task_not_duplicated": True,
                },
            )
        except WorkerPoolError as error:
            return WorkerTakeoverReceipt(
                plan=plan,
                acquisition=None,
                executed=True,
                changed=False,
                failure_code=error.code.value,
                failure_message=str(error),
            )
        return WorkerTakeoverReceipt(
            plan=plan,
            acquisition=acquisition,
            executed=True,
            changed=True,
        )

    def recover_lost_attempts(
        self,
        *,
        owner_session_by_task: Mapping[str, str] | None = None,
    ) -> tuple[WorkerTakeoverReceipt, ...]:
        owners = dict(owner_session_by_task or {})
        receipts: list[WorkerTakeoverReceipt] = []
        for attempt in self.store.list_attempts(states=(AttemptState.LOST,)):
            signal = self.signal_for_attempt(
                attempt.attempt_id,
                reason=TakeoverReason.WORKER_LOST,
                message=attempt.recovery_reason or "worker became unavailable",
            )
            plan = self.plan(
                signal,
                owner_session_id=owners.get(attempt.task_id, ""),
            )
            receipts.append(self.execute(plan))
        return tuple(receipts)


def _requirement_from_lease(lease: WorkerLease) -> CapabilityRequirement:
    raw = lease.metadata.get("requirement")
    data = dict(raw) if isinstance(raw, Mapping) else {}
    locations: list[WorkerLocation] = []
    for value in data.get("locations") or ():
        try:
            locations.append(WorkerLocation(str(value)))
        except ValueError:
            continue
    return CapabilityRequirement(
        required=tuple(str(item) for item in data.get("required") or ()),
        forbidden=tuple(str(item) for item in data.get("forbidden") or ()),
        tool_ids=tuple(str(item) for item in data.get("tool_ids") or ()),
        backend_kinds=tuple(str(item) for item in data.get("backend_kinds") or ()),
        locations=tuple(locations),
        min_protocol_version=int(data.get("min_protocol_version") or 1),
        resources=ResourceVector.from_dict(
            data.get("resources") if isinstance(data.get("resources"), Mapping) else lease.resources.to_dict()
        ),
        labels={str(key): str(value) for key, value in dict(data.get("labels") or {}).items()},
    )
