from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import now_iso

from .integration_models import BrowserActionIntegrationError, DispatchBoundary, PlanPhase, parse_timestamp
from .models import ActionRequest, digest_value, stable_id


class CancellationState(StrEnum):
    ACTIVE = "active"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class CancellationRecord:
    action_id: str
    state: CancellationState
    revision: int
    reason: str = ""
    actor_id: str = ""
    requested_at: str = ""
    completed_at: str = ""

    def __post_init__(self) -> None:
        if not self.action_id or self.revision < 0:
            raise ValueError("browser cancellation record identity is invalid")

    def public_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "state": str(self.state),
            "revision": self.revision,
            "reason": self.reason,
            "actor_id": self.actor_id,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
        }


class BrowserActionCancellationRegistry:
    """Process-scoped cancellation signal; 04A remains lifecycle owner.

    This registry carries an in-flight signal only.  It does not persist task
    state and cannot authorize execution.  A caller must still use the 03D/04A
    control path to durably cancel a task or browser session.
    """

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._records: dict[str, CancellationRecord] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def register(self, action_id: str) -> CancellationRecord:
        self._ensure_available()
        if not action_id:
            raise ValueError("browser cancellation registration requires action_id")
        with self._lock:
            existing = self._records.get(action_id)
            if existing is not None:
                if existing.state in {CancellationState.CANCELLED, CancellationState.COMPLETED}:
                    raise BrowserActionIntegrationError(
                        "browser_action_terminal_replay",
                        "terminal browser action cannot be registered again",
                        phase=PlanPhase.DISPATCH,
                        action_id=action_id,
                    )
                return existing
            record = CancellationRecord(action_id, CancellationState.ACTIVE, 1)
            self._records[action_id] = record
            self._events[action_id] = threading.Event()
            return record

    def request_cancel(
        self,
        action_id: str,
        *,
        reason: str = "cancelled",
        actor_id: str = "control-plane",
        expected_revision: int | None = None,
    ) -> CancellationRecord:
        self._ensure_available()
        with self._lock:
            current = self._records.get(action_id)
            if current is None:
                current = CancellationRecord(action_id, CancellationState.ACTIVE, 0)
            if expected_revision is not None and current.revision != expected_revision:
                raise BrowserActionIntegrationError(
                    "browser_cancel_revision_conflict",
                    "browser action cancellation revision changed",
                    phase=PlanPhase.CANCELLED,
                    action_id=action_id,
                    details={"expected": expected_revision, "actual": current.revision},
                )
            if current.state == CancellationState.COMPLETED:
                return current
            record = CancellationRecord(
                action_id=action_id,
                state=CancellationState.CANCEL_REQUESTED,
                revision=current.revision + 1,
                reason=str(reason or "cancelled"),
                actor_id=str(actor_id),
                requested_at=now_iso(),
            )
            self._records[action_id] = record
            self._events.setdefault(action_id, threading.Event()).set()
            return record

    def assert_active(
        self,
        action_id: str,
        *,
        phase: PlanPhase,
        boundary: DispatchBoundary = DispatchBoundary.BEFORE_GRANT,
        side_effect_count: int = 0,
    ) -> None:
        self._ensure_available()
        with self._lock:
            record = self._records.get(action_id)
        if record and record.state in {CancellationState.CANCEL_REQUESTED, CancellationState.CANCELLED}:
            raise BrowserActionIntegrationError(
                "browser_action_cancelled",
                record.reason or "browser action was cancelled",
                phase=phase,
                action_id=action_id,
                retryable=False,
                side_effect_count=side_effect_count,
                details={"cancellation": record.public_dict(), "dispatch_boundary": str(boundary)},
            )

    def cancelled(self, action_id: str) -> bool:
        with self._lock:
            record = self._records.get(action_id)
            return bool(record and record.state in {CancellationState.CANCEL_REQUESTED, CancellationState.CANCELLED})

    def event(self, action_id: str) -> threading.Event:
        self._ensure_available()
        with self._lock:
            return self._events.setdefault(action_id, threading.Event())

    def finish(self, action_id: str, *, cancelled: bool = False) -> CancellationRecord:
        self._ensure_available()
        with self._lock:
            current = self._records.get(action_id) or CancellationRecord(action_id, CancellationState.ACTIVE, 0)
            state = CancellationState.CANCELLED if cancelled or current.state == CancellationState.CANCEL_REQUESTED else CancellationState.COMPLETED
            record = CancellationRecord(
                action_id=action_id,
                state=state,
                revision=current.revision + 1,
                reason=current.reason,
                actor_id=current.actor_id,
                requested_at=current.requested_at,
                completed_at=now_iso(),
            )
            self._records[action_id] = record
            self._events.setdefault(action_id, threading.Event()).set()
            return record

    def get(self, action_id: str) -> CancellationRecord | None:
        with self._lock:
            return self._records.get(action_id)

    def prune(self, *, keep_terminal: int = 1024) -> int:
        if keep_terminal < 0:
            raise ValueError("browser cancellation terminal retention cannot be negative")
        with self._lock:
            terminal = [
                record
                for record in self._records.values()
                if record.state in {CancellationState.CANCELLED, CancellationState.COMPLETED}
            ]
            terminal.sort(key=lambda item: (item.completed_at, item.action_id), reverse=True)
            removed = 0
            for record in terminal[keep_terminal:]:
                self._records.pop(record.action_id, None)
                self._events.pop(record.action_id, None)
                removed += 1
            return removed

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            records = tuple(self._records.values())
        return {
            "runtime_id": "zyra-browser-action-cancellation-registry",
            "owner_unit": "M1-S04C-02",
            "canonical_task_control_owner": "M1-03D/M1-04A",
            "persistent": False,
            "disabled": self.disabled,
            "active": sum(record.state == CancellationState.ACTIVE for record in records),
            "cancel_requested": sum(record.state == CancellationState.CANCEL_REQUESTED for record in records),
            "cancelled": sum(record.state == CancellationState.CANCELLED for record in records),
            "completed": sum(record.state == CancellationState.COMPLETED for record in records),
        }

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserActionIntegrationError(
                "browser_cancellation_registry_disabled",
                "browser action cancellation registry is disabled",
                phase=PlanPhase.CANCELLED,
            )


@dataclass(slots=True)
class ActionDeadline:
    request: ActionRequest
    cancellation: BrowserActionCancellationRegistry
    started_monotonic: float = field(default_factory=time.monotonic)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cancellation.register(self.request.identity.action_id)
        if self.request.deadline_at:
            parse_timestamp(self.request.deadline_at)

    @property
    def action_id(self) -> str:
        return self.request.identity.action_id

    @property
    def deadline(self) -> dt.datetime | None:
        return parse_timestamp(self.request.deadline_at) if self.request.deadline_at else None

    @property
    def remaining_seconds(self) -> float | None:
        deadline = self.deadline
        if deadline is None:
            return None
        return max(0.0, (deadline - dt.datetime.now(dt.UTC)).total_seconds())

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    def checkpoint(
        self,
        phase: PlanPhase,
        *,
        boundary: DispatchBoundary = DispatchBoundary.BEFORE_GRANT,
        side_effect_count: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> float | None:
        self.cancellation.assert_active(
            self.action_id,
            phase=phase,
            boundary=boundary,
            side_effect_count=side_effect_count,
        )
        remaining = self.remaining_seconds
        record = {
            "phase": str(phase),
            "boundary": str(boundary),
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_seconds": remaining,
            "side_effect_count": side_effect_count,
            "details_digest": digest_value(details or {}),
            "observed_at": now_iso(),
        }
        self.checkpoints.append(record)
        if remaining is not None and remaining <= 0:
            raise BrowserActionIntegrationError(
                "browser_action_deadline_elapsed",
                "browser action deadline elapsed",
                phase=phase,
                action_id=self.action_id,
                step_index=self.request.identity.step_index,
                retryable=False,
                side_effect_count=side_effect_count,
                details={"deadline_at": self.request.deadline_at, "dispatch_boundary": str(boundary)},
            )
        return remaining

    def timeout_for(self, default_seconds: float, *, floor: float = 0.01) -> float:
        if default_seconds <= 0 or floor <= 0:
            raise ValueError("browser action timeout and floor must be positive")
        remaining = self.checkpoint(PlanPhase.DISPATCH)
        if remaining is None:
            return max(floor, default_seconds)
        return max(floor, min(default_seconds, remaining))

    def finish(self, *, cancelled: bool = False) -> CancellationRecord:
        return self.cancellation.finish(self.action_id, cancelled=cancelled)

    def public_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "deadline_id": stable_id("brdeadline", self.action_id, self.request.deadline_at),
            "deadline_at": self.request.deadline_at,
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_seconds": self.remaining_seconds,
            "checkpoints": list(self.checkpoints),
        }


class BrowserActionDeadlineRuntime:
    def __init__(
        self,
        cancellation: BrowserActionCancellationRegistry | None = None,
        *,
        disabled: bool = False,
    ) -> None:
        self.disabled = disabled
        self.cancellation = cancellation or BrowserActionCancellationRegistry(disabled=disabled)
        self._active: dict[str, ActionDeadline] = {}
        self._lock = threading.RLock()
        self._started = 0
        self._completed = 0
        self._failed = 0

    def begin(self, request: ActionRequest) -> ActionDeadline:
        if self.disabled:
            raise BrowserActionIntegrationError(
                "browser_deadline_runtime_disabled",
                "browser action deadline runtime is disabled",
                phase=PlanPhase.DISPATCH,
            )
        action_id = request.identity.action_id
        with self._lock:
            if action_id in self._active:
                raise BrowserActionIntegrationError(
                    "browser_deadline_replayed",
                    "browser action deadline is already active",
                    phase=PlanPhase.DISPATCH,
                    action_id=action_id,
                )
            deadline = ActionDeadline(request, self.cancellation)
            self._active[action_id] = deadline
            self._started += 1
            return deadline

    def get(self, action_id: str) -> ActionDeadline | None:
        with self._lock:
            return self._active.get(action_id)

    def require(self, action_id: str) -> ActionDeadline:
        deadline = self.get(action_id)
        if deadline is None:
            raise BrowserActionIntegrationError(
                "browser_deadline_missing",
                "browser action has no active deadline",
                phase=PlanPhase.DISPATCH,
                action_id=action_id,
            )
        return deadline

    def active(self) -> tuple[ActionDeadline, ...]:
        """Return a stable view for cancellation-only control matching."""
        with self._lock:
            return tuple(self._active.values())

    def finish(self, action_id: str, *, failed: bool = False, cancelled: bool = False) -> ActionDeadline:
        with self._lock:
            deadline = self._active.pop(action_id, None)
        if deadline is None:
            raise BrowserActionIntegrationError(
                "browser_deadline_missing",
                "browser action deadline cannot be completed twice",
                phase=PlanPhase.DISPATCH,
                action_id=action_id,
            )
        deadline.finish(cancelled=cancelled)
        self._completed += 1
        if failed:
            self._failed += 1
        return deadline

    def cancel(self, action_id: str, *, reason: str, actor_id: str = "control-plane") -> CancellationRecord:
        return self.cancellation.request_cancel(action_id, reason=reason, actor_id=actor_id)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = tuple(self._active.values())
        return {
            "runtime_id": "zyra-browser-action-deadline-runtime",
            "owner_unit": "M1-S04C-02",
            "disabled": self.disabled,
            "started": self._started,
            "completed": self._completed,
            "failed": self._failed,
            "active": len(active),
            "active_action_ids": [item.action_id for item in active],
            "cancellation": self.cancellation.snapshot(),
        }
