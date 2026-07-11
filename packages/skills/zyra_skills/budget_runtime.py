from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import RLock
from typing import Any, Iterable, Mapping

from .errors import SkillBudgetExceeded, SkillInvocationConflict
from .models import SkillContextBudget, new_id, utc_now


class SkillBudgetStage(StrEnum):
    LISTING = "listing"
    BODY = "body"
    RESOURCE = "resource"
    ATTACHMENT = "attachment"
    RESTORE = "restore"


@dataclass(frozen=True, slots=True)
class SkillBudgetAllocation:
    allocation_id: str
    invocation_id: str
    stage: SkillBudgetStage
    tokens: int
    source_ref: str
    reason: str
    active: bool = True
    created_at: str = field(default_factory=utc_now)
    released_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocation_id": self.allocation_id,
            "invocation_id": self.invocation_id,
            "stage": str(self.stage),
            "tokens": self.tokens,
            "source_ref": self.source_ref,
            "reason": self.reason,
            "active": self.active,
            "created_at": self.created_at,
            "released_at": self.released_at,
        }


@dataclass(frozen=True, slots=True)
class SkillInvocationBudgetState:
    invocation_id: str
    session_id: str
    total_limit: int
    body_limit: int
    resource_limit: int
    restore_limit: int
    allocation_ids: tuple[str, ...] = ()
    consumed_tokens: int = 0
    peak_tokens: int = 0
    closed: bool = False
    close_reason: str = ""
    revision: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.total_limit - self.consumed_tokens)

    def stage_limit(self, stage: SkillBudgetStage) -> int:
        if stage is SkillBudgetStage.BODY:
            return self.body_limit
        if stage is SkillBudgetStage.RESOURCE:
            return self.resource_limit
        if stage is SkillBudgetStage.RESTORE:
            return self.restore_limit
        return self.total_limit

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "session_id": self.session_id,
            "total_limit": self.total_limit,
            "body_limit": self.body_limit,
            "resource_limit": self.resource_limit,
            "restore_limit": self.restore_limit,
            "allocation_ids": list(self.allocation_ids),
            "consumed_tokens": self.consumed_tokens,
            "peak_tokens": self.peak_tokens,
            "remaining_tokens": self.remaining_tokens,
            "closed": self.closed,
            "close_reason": self.close_reason,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SkillContextBudgetRuntime:
    """Deterministic context budget custody for progressive disclosure.

    Token estimates are conservative accounting units, not provider billing.
    The state ensures body, resources, attachments, and restore cannot each
    independently spend the full invocation budget.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._states: dict[str, SkillInvocationBudgetState] = {}
        self._allocations: dict[str, SkillBudgetAllocation] = {}
        self._stage_consumption: dict[tuple[str, SkillBudgetStage], int] = {}

    def open(
        self,
        *,
        invocation_id: str,
        session_id: str,
        budget: SkillContextBudget,
    ) -> SkillInvocationBudgetState:
        with self._lock:
            if invocation_id in self._states:
                existing = self._states[invocation_id]
                if existing.session_id != session_id:
                    raise SkillInvocationConflict("skill budget invocation/session identity mismatch")
                return existing
            state = SkillInvocationBudgetState(
                invocation_id=invocation_id,
                session_id=session_id,
                total_limit=budget.invocation_total_tokens,
                body_limit=budget.body_tokens,
                resource_limit=budget.resource_read_tokens,
                restore_limit=budget.restore_tokens,
            )
            self._states[invocation_id] = state
            return state

    def reserve(
        self,
        *,
        invocation_id: str,
        stage: SkillBudgetStage,
        tokens: int,
        source_ref: str,
        reason: str,
    ) -> SkillBudgetAllocation:
        if tokens < 0:
            raise ValueError("skill token reservation cannot be negative")
        with self._lock:
            state = self._require_state(invocation_id)
            if state.closed:
                raise SkillBudgetExceeded(
                    "skill context budget is closed",
                    detail={"invocation_id": invocation_id, "reason": state.close_reason},
                )
            stage_used = self._stage_consumption.get((invocation_id, stage), 0)
            stage_limit = state.stage_limit(stage)
            if stage in {SkillBudgetStage.BODY, SkillBudgetStage.RESOURCE, SkillBudgetStage.RESTORE}:
                if stage_used + tokens > stage_limit:
                    raise SkillBudgetExceeded(
                        "skill stage token budget exceeded",
                        detail={
                            "stage": str(stage),
                            "requested": tokens,
                            "consumed": stage_used,
                            "limit": stage_limit,
                            "source_ref": source_ref,
                        },
                    )
            if state.consumed_tokens + tokens > state.total_limit:
                raise SkillBudgetExceeded(
                    "skill invocation total token budget exceeded",
                    detail={
                        "requested": tokens,
                        "consumed": state.consumed_tokens,
                        "limit": state.total_limit,
                        "stage": str(stage),
                    },
                )
            allocation = SkillBudgetAllocation(
                allocation_id=new_id("skillbudget"),
                invocation_id=invocation_id,
                stage=stage,
                tokens=tokens,
                source_ref=source_ref,
                reason=reason,
            )
            self._allocations[allocation.allocation_id] = allocation
            self._stage_consumption[(invocation_id, stage)] = stage_used + tokens
            consumed = state.consumed_tokens + tokens
            self._states[invocation_id] = replace(
                state,
                allocation_ids=(*state.allocation_ids, allocation.allocation_id),
                consumed_tokens=consumed,
                peak_tokens=max(state.peak_tokens, consumed),
                revision=state.revision + 1,
                updated_at=utc_now(),
            )
            return allocation

    def release(self, allocation_id: str, *, reason: str = "released") -> SkillBudgetAllocation:
        with self._lock:
            allocation = self._allocations.get(allocation_id)
            if allocation is None:
                raise SkillBudgetExceeded("skill budget allocation was not found")
            if not allocation.active:
                return allocation
            state = self._require_state(allocation.invocation_id)
            released = replace(allocation, active=False, reason=reason, released_at=utc_now())
            self._allocations[allocation_id] = released
            key = (allocation.invocation_id, allocation.stage)
            self._stage_consumption[key] = max(0, self._stage_consumption.get(key, 0) - allocation.tokens)
            self._states[state.invocation_id] = replace(
                state,
                consumed_tokens=max(0, state.consumed_tokens - allocation.tokens),
                revision=state.revision + 1,
                updated_at=utc_now(),
            )
            return released

    def close(self, invocation_id: str, *, reason: str) -> SkillInvocationBudgetState:
        with self._lock:
            state = self._require_state(invocation_id)
            if state.closed:
                return state
            for allocation_id in state.allocation_ids:
                allocation = self._allocations.get(allocation_id)
                if allocation is not None and allocation.active:
                    self.release(allocation_id, reason=reason)
            current = self._require_state(invocation_id)
            closed = replace(
                current,
                closed=True,
                close_reason=reason,
                revision=current.revision + 1,
                updated_at=utc_now(),
            )
            self._states[invocation_id] = closed
            return closed

    def state(self, invocation_id: str) -> SkillInvocationBudgetState:
        with self._lock:
            return self._require_state(invocation_id)

    def allocations(self, invocation_id: str, *, active_only: bool = False) -> tuple[SkillBudgetAllocation, ...]:
        with self._lock:
            state = self._require_state(invocation_id)
            values = tuple(
                self._allocations[allocation_id]
                for allocation_id in state.allocation_ids
                if allocation_id in self._allocations
            )
            return tuple(value for value in values if value.active) if active_only else values

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "states": {key: state.to_dict() for key, state in self._states.items()},
                "allocations": {key: value.to_dict() for key, value in self._allocations.items()},
                "active_payload_included": False,
            }

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        if int(snapshot.get("version") or 0) != 1:
            raise SkillBudgetExceeded("unsupported skill budget snapshot version")
        raw_states = snapshot.get("states")
        raw_allocations = snapshot.get("allocations")
        if not isinstance(raw_states, Mapping) or not isinstance(raw_allocations, Mapping):
            raise SkillBudgetExceeded("skill budget snapshot is malformed")
        allocations: dict[str, SkillBudgetAllocation] = {}
        for allocation_id, raw in raw_allocations.items():
            if not isinstance(raw, Mapping):
                raise SkillBudgetExceeded("skill budget allocation snapshot is malformed")
            allocation = SkillBudgetAllocation(
                allocation_id=str(raw.get("allocation_id") or allocation_id),
                invocation_id=str(raw.get("invocation_id") or ""),
                stage=SkillBudgetStage(str(raw.get("stage") or "body")),
                tokens=int(raw.get("tokens") or 0),
                source_ref=str(raw.get("source_ref") or ""),
                reason=str(raw.get("reason") or ""),
                active=bool(raw.get("active", False)),
                created_at=str(raw.get("created_at") or utc_now()),
                released_at=str(raw.get("released_at") or ""),
            )
            if allocation.allocation_id != str(allocation_id):
                raise SkillBudgetExceeded("skill budget allocation id mismatch")
            allocations[allocation.allocation_id] = allocation
        states: dict[str, SkillInvocationBudgetState] = {}
        for invocation_id, raw in raw_states.items():
            if not isinstance(raw, Mapping):
                raise SkillBudgetExceeded("skill budget state snapshot is malformed")
            state = SkillInvocationBudgetState(
                invocation_id=str(raw.get("invocation_id") or invocation_id),
                session_id=str(raw.get("session_id") or ""),
                total_limit=int(raw.get("total_limit") or 0),
                body_limit=int(raw.get("body_limit") or 0),
                resource_limit=int(raw.get("resource_limit") or 0),
                restore_limit=int(raw.get("restore_limit") or 0),
                allocation_ids=tuple(str(value) for value in raw.get("allocation_ids") or ()),
                consumed_tokens=int(raw.get("consumed_tokens") or 0),
                peak_tokens=int(raw.get("peak_tokens") or 0),
                closed=bool(raw.get("closed", False)),
                close_reason=str(raw.get("close_reason") or ""),
                revision=int(raw.get("revision") or 0),
                created_at=str(raw.get("created_at") or utc_now()),
                updated_at=str(raw.get("updated_at") or utc_now()),
            )
            if state.invocation_id != str(invocation_id):
                raise SkillBudgetExceeded("skill budget state id mismatch")
            if any(allocation_id not in allocations for allocation_id in state.allocation_ids):
                raise SkillBudgetExceeded("skill budget state references a missing allocation")
            active_sum = sum(
                allocations[allocation_id].tokens
                for allocation_id in state.allocation_ids
                if allocations[allocation_id].active
            )
            if active_sum != state.consumed_tokens:
                raise SkillBudgetExceeded("skill budget snapshot consumed-token invariant failed")
            states[state.invocation_id] = state
        stage_consumption: dict[tuple[str, SkillBudgetStage], int] = {}
        for allocation in allocations.values():
            if allocation.active:
                key = (allocation.invocation_id, allocation.stage)
                stage_consumption[key] = stage_consumption.get(key, 0) + allocation.tokens
        with self._lock:
            self._states = states
            self._allocations = allocations
            self._stage_consumption = stage_consumption

    def _require_state(self, invocation_id: str) -> SkillInvocationBudgetState:
        state = self._states.get(invocation_id)
        if state is None:
            raise SkillBudgetExceeded(
                "skill context budget state was not opened",
                detail={"invocation_id": invocation_id},
            )
        return state
