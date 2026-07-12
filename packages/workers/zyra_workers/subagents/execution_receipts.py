from __future__ import annotations

"""Durable execution fencing for logical subagent attempts.

The upstream runtimes keep most of this state in an in-process job registry.
Zyra cannot do that: a process restart must not turn an already committed child
effect into another execution.  This module therefore owns only logical
execution receipts.  It deliberately has no worker id, lease, capacity or
heartbeat fields; those remain M1-07A state.
"""

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import Condition, RLock
from typing import Any, Callable, Iterable, Mapping

from zyra_core import new_id, now_iso


class ExecutionReceiptError(RuntimeError):
    pass


class ExecutionReceiptDisabled(ExecutionReceiptError):
    pass


class ExecutionReceiptConflict(ExecutionReceiptError):
    pass


class ExecutionReceiptNotFound(ExecutionReceiptError):
    pass


class ExecutionAttemptFenced(ExecutionReceiptError):
    pass


class ExecutionEffectUnknown(ExecutionReceiptError):
    """Raised when a crash occurred after effect start but before commit."""


class ExecutionPhase(StrEnum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    STARTED = "started"
    EFFECT_STARTED = "effect_started"
    EFFECT_COMMITTED = "effect_committed"
    YIELD_COMMITTED = "yield_committed"
    HANDOFF_COMMITTED = "handoff_committed"
    CLEANUP_COMMITTED = "cleanup_committed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    PARKED = "parked"

    @property
    def terminal(self) -> bool:
        return self in {
            ExecutionPhase.CLEANUP_COMMITTED,
            ExecutionPhase.CANCELLED,
            ExecutionPhase.FAILED,
            ExecutionPhase.PARKED,
        }


_PHASE_ORDER: dict[ExecutionPhase, int] = {
    ExecutionPhase.RESERVED: 10,
    ExecutionPhase.DISPATCHED: 20,
    ExecutionPhase.STARTED: 30,
    ExecutionPhase.EFFECT_STARTED: 40,
    ExecutionPhase.EFFECT_COMMITTED: 50,
    ExecutionPhase.YIELD_COMMITTED: 60,
    ExecutionPhase.HANDOFF_COMMITTED: 70,
    ExecutionPhase.CLEANUP_COMMITTED: 80,
    ExecutionPhase.CANCELLED: 90,
    ExecutionPhase.FAILED: 90,
    ExecutionPhase.PARKED: 90,
}


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    run_id: str
    parent_task_id: str
    task_id: str
    parent_session_id: str
    request_digest: str
    idempotency_key: str

    def __post_init__(self) -> None:
        values = {
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "task_id": self.task_id,
            "parent_session_id": self.parent_session_id,
            "request_digest": self.request_digest,
            "idempotency_key": self.idempotency_key,
        }
        for name, value in values.items():
            if not str(value).strip():
                raise ValueError(f"{name} is required")
            if len(str(value)) > 4096:
                raise ValueError(f"{name} exceeds maximum length")

    @property
    def partition_key(self) -> str:
        return f"{self.run_id}:{self.parent_task_id}:{self.parent_session_id}"

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "task_id": self.task_id,
            "parent_session_id": self.parent_session_id,
            "request_digest": self.request_digest,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionIdentity":
        return cls(
            run_id=str(value.get("run_id") or ""),
            parent_task_id=str(value.get("parent_task_id") or ""),
            task_id=str(value.get("task_id") or ""),
            parent_session_id=str(value.get("parent_session_id") or ""),
            request_digest=str(value.get("request_digest") or ""),
            idempotency_key=str(value.get("idempotency_key") or ""),
        )


@dataclass(frozen=True, slots=True)
class ExecutionTransition:
    transition_id: str
    phase_before: ExecutionPhase
    phase_after: ExecutionPhase
    revision_before: int
    revision_after: int
    attempt: int
    token_digest: str
    reason: str = ""
    effect_digest: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "phase_before": self.phase_before.value,
            "phase_after": self.phase_after.value,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "attempt": self.attempt,
            "token_digest": self.token_digest,
            "reason": self.reason,
            "effect_digest": self.effect_digest,
            "metadata": _safe_mapping(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionTransition":
        return cls(
            transition_id=str(value.get("transition_id") or new_id("exectrans")),
            phase_before=ExecutionPhase(str(value.get("phase_before") or ExecutionPhase.RESERVED.value)),
            phase_after=ExecutionPhase(str(value.get("phase_after") or ExecutionPhase.RESERVED.value)),
            revision_before=int(value.get("revision_before") or 0),
            revision_after=int(value.get("revision_after") or 0),
            attempt=max(1, int(value.get("attempt") or 1)),
            token_digest=str(value.get("token_digest") or ""),
            reason=str(value.get("reason") or ""),
            effect_digest=str(value.get("effect_digest") or ""),
            metadata=_safe_mapping(value.get("metadata")),
            created_at=str(value.get("created_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class ExecutionAttemptReceipt:
    receipt_id: str
    identity: ExecutionIdentity
    execution_ref: str
    attempt: int
    attempt_token_digest: str
    phase: ExecutionPhase
    revision: int = 0
    dispatch_digest: str = ""
    effect_digest: str = ""
    yield_digest: str = ""
    handoff_digest: str = ""
    cleanup_digest: str = ""
    terminal_reason: str = ""
    late_result_count: int = 0
    replay_count: int = 0
    transitions: tuple[ExecutionTransition, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def terminal(self) -> bool:
        return self.phase.terminal

    @property
    def effect_may_have_happened(self) -> bool:
        return _PHASE_ORDER[self.phase] >= _PHASE_ORDER[ExecutionPhase.EFFECT_STARTED]

    @property
    def effect_committed(self) -> bool:
        return bool(self.effect_digest) or _PHASE_ORDER[self.phase] >= _PHASE_ORDER[ExecutionPhase.EFFECT_COMMITTED]

    @property
    def replay_safe(self) -> bool:
        return not self.effect_may_have_happened

    def to_dict(self, *, include_history: bool = True) -> dict[str, Any]:
        value = {
            "receipt_id": self.receipt_id,
            "identity": self.identity.to_dict(),
            "execution_ref": self.execution_ref,
            "attempt": self.attempt,
            "attempt_token_digest": self.attempt_token_digest,
            "phase": self.phase.value,
            "revision": self.revision,
            "dispatch_digest": self.dispatch_digest,
            "effect_digest": self.effect_digest,
            "yield_digest": self.yield_digest,
            "handoff_digest": self.handoff_digest,
            "cleanup_digest": self.cleanup_digest,
            "terminal_reason": self.terminal_reason,
            "late_result_count": self.late_result_count,
            "replay_count": self.replay_count,
            "metadata": _safe_mapping(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_history:
            value["transitions"] = [item.to_dict() for item in self.transitions]
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionAttemptReceipt":
        transitions = value.get("transitions")
        return cls(
            receipt_id=str(value.get("receipt_id") or new_id("execreceipt")),
            identity=ExecutionIdentity.from_dict(_safe_mapping(value.get("identity"))),
            execution_ref=str(value.get("execution_ref") or ""),
            attempt=max(1, int(value.get("attempt") or 1)),
            attempt_token_digest=str(value.get("attempt_token_digest") or ""),
            phase=ExecutionPhase(str(value.get("phase") or ExecutionPhase.RESERVED.value)),
            revision=max(0, int(value.get("revision") or 0)),
            dispatch_digest=str(value.get("dispatch_digest") or ""),
            effect_digest=str(value.get("effect_digest") or ""),
            yield_digest=str(value.get("yield_digest") or ""),
            handoff_digest=str(value.get("handoff_digest") or ""),
            cleanup_digest=str(value.get("cleanup_digest") or ""),
            terminal_reason=str(value.get("terminal_reason") or ""),
            late_result_count=max(0, int(value.get("late_result_count") or 0)),
            replay_count=max(0, int(value.get("replay_count") or 0)),
            transitions=tuple(
                ExecutionTransition.from_dict(item)
                for item in transitions or ()
                if isinstance(item, Mapping)
            ),
            metadata=_safe_mapping(value.get("metadata")),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    receipt: ExecutionAttemptReceipt
    attempt_token: str
    created: bool
    replay: bool


@dataclass(frozen=True, slots=True)
class LateExecutionResult:
    late_result_id: str
    task_id: str
    receipt_id: str
    attempt: int
    execution_ref: str
    current_phase: ExecutionPhase
    result_digest: str
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "late_result_id": self.late_result_id,
            "task_id": self.task_id,
            "receipt_id": self.receipt_id,
            "attempt": self.attempt,
            "execution_ref": self.execution_ref,
            "current_phase": self.current_phase.value,
            "result_digest": self.result_digest,
            "reason": self.reason,
            "metadata": _safe_mapping(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LateExecutionResult":
        return cls(
            late_result_id=str(value.get("late_result_id") or new_id("lateresult")),
            task_id=str(value.get("task_id") or ""),
            receipt_id=str(value.get("receipt_id") or ""),
            attempt=max(1, int(value.get("attempt") or 1)),
            execution_ref=str(value.get("execution_ref") or ""),
            current_phase=ExecutionPhase(str(value.get("current_phase") or ExecutionPhase.FAILED.value)),
            result_digest=str(value.get("result_digest") or ""),
            reason=str(value.get("reason") or ""),
            metadata=_safe_mapping(value.get("metadata")),
            created_at=str(value.get("created_at") or now_iso()),
        )


class ExecutionReceiptStore:
    """Atomic logical receipt store with idempotency and terminal fencing."""

    schema = "zyra.subagent-execution-receipts/v2"

    def __init__(self, path: str | Path, *, disabled: bool = False, maximum_history: int = 128) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.disabled = bool(disabled)
        self.maximum_history = max(8, int(maximum_history))
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._receipts: dict[str, ExecutionAttemptReceipt] = {}
        self._by_idempotency: dict[str, str] = {}
        self._by_task: dict[str, str] = {}
        self._late_results: list[LateExecutionResult] = []
        self._load()

    def claim(self, identity: ExecutionIdentity, *, execution_ref: str = "") -> ExecutionClaim:
        self._require_enabled()
        with self._lock:
            existing_id = self._by_idempotency.get(identity.idempotency_key)
            if existing_id:
                receipt = self._receipts[existing_id]
                self._assert_same_identity(receipt.identity, identity)
                replayed = replace(
                    receipt,
                    replay_count=receipt.replay_count + 1,
                    revision=receipt.revision + 1,
                    updated_at=now_iso(),
                )
                self._receipts[existing_id] = replayed
                self._persist()
                return ExecutionClaim(replayed, "", created=False, replay=True)
            task_receipt_id = self._by_task.get(identity.task_id)
            if task_receipt_id:
                receipt = self._receipts[task_receipt_id]
                self._assert_same_identity(receipt.identity, identity)
                return ExecutionClaim(receipt, "", created=False, replay=True)
            token = new_id("attempttoken") + new_id("nonce")
            token_digest = _digest_text(token)
            receipt = ExecutionAttemptReceipt(
                receipt_id=new_id("execreceipt"),
                identity=copy.deepcopy(identity),
                execution_ref=str(execution_ref or new_id("logicalexec")),
                attempt=1,
                attempt_token_digest=token_digest,
                phase=ExecutionPhase.RESERVED,
                metadata={
                    "state_owner": "M1-03D ExecutionReceiptStore",
                    "physical_worker_state_owned": False,
                    "workspace_lifecycle_owned": False,
                },
            )
            self._receipts[receipt.receipt_id] = receipt
            self._by_idempotency[identity.idempotency_key] = receipt.receipt_id
            self._by_task[identity.task_id] = receipt.receipt_id
            self._persist()
            return ExecutionClaim(copy.deepcopy(receipt), token, created=True, replay=False)

    def get(self, receipt_id: str) -> ExecutionAttemptReceipt:
        self._require_enabled()
        with self._lock:
            value = self._receipts.get(receipt_id)
            if value is None:
                raise ExecutionReceiptNotFound(receipt_id)
            return copy.deepcopy(value)

    def for_task(self, task_id: str) -> ExecutionAttemptReceipt | None:
        self._require_enabled()
        with self._lock:
            receipt_id = self._by_task.get(task_id)
            return copy.deepcopy(self._receipts[receipt_id]) if receipt_id else None

    def for_idempotency(self, idempotency_key: str) -> ExecutionAttemptReceipt | None:
        self._require_enabled()
        with self._lock:
            receipt_id = self._by_idempotency.get(idempotency_key)
            return copy.deepcopy(self._receipts[receipt_id]) if receipt_id else None

    def list(
        self,
        *,
        parent_task_id: str | None = None,
        run_id: str | None = None,
        include_terminal: bool = True,
    ) -> tuple[ExecutionAttemptReceipt, ...]:
        self._require_enabled()
        with self._lock:
            values = []
            for receipt in self._receipts.values():
                if parent_task_id is not None and receipt.identity.parent_task_id != parent_task_id:
                    continue
                if run_id is not None and receipt.identity.run_id != run_id:
                    continue
                if not include_terminal and receipt.terminal:
                    continue
                values.append(copy.deepcopy(receipt))
            return tuple(sorted(values, key=lambda item: (item.created_at, item.receipt_id)))

    def transition(
        self,
        receipt_id: str,
        phase: ExecutionPhase,
        *,
        attempt_token: str,
        expected_revision: int | None = None,
        reason: str = "",
        effect_digest: str = "",
        metadata: Mapping[str, Any] | None = None,
        updater: Callable[[ExecutionAttemptReceipt], ExecutionAttemptReceipt] | None = None,
    ) -> ExecutionAttemptReceipt:
        self._require_enabled()
        with self._lock:
            current = self._receipts.get(receipt_id)
            if current is None:
                raise ExecutionReceiptNotFound(receipt_id)
            self._assert_token(current, attempt_token)
            if expected_revision is not None and current.revision != expected_revision:
                raise ExecutionReceiptConflict(
                    f"receipt revision conflict: expected {expected_revision}, actual {current.revision}"
                )
            self._validate_transition(current, phase)
            next_revision = current.revision + 1
            transition = ExecutionTransition(
                transition_id=new_id("exectrans"),
                phase_before=current.phase,
                phase_after=phase,
                revision_before=current.revision,
                revision_after=next_revision,
                attempt=current.attempt,
                token_digest=current.attempt_token_digest,
                reason=str(reason),
                effect_digest=str(effect_digest),
                metadata=_safe_mapping(metadata),
            )
            history = (*current.transitions, transition)[-self.maximum_history :]
            changed = replace(
                current,
                phase=phase,
                revision=next_revision,
                transitions=history,
                updated_at=now_iso(),
            )
            if phase is ExecutionPhase.DISPATCHED and effect_digest:
                changed = replace(changed, dispatch_digest=effect_digest)
            elif phase is ExecutionPhase.EFFECT_COMMITTED:
                changed = replace(changed, effect_digest=effect_digest or current.effect_digest)
            elif phase is ExecutionPhase.YIELD_COMMITTED:
                changed = replace(changed, yield_digest=effect_digest or current.yield_digest)
            elif phase is ExecutionPhase.HANDOFF_COMMITTED:
                changed = replace(changed, handoff_digest=effect_digest or current.handoff_digest)
            elif phase is ExecutionPhase.CLEANUP_COMMITTED:
                changed = replace(changed, cleanup_digest=effect_digest or current.cleanup_digest)
            if phase in {ExecutionPhase.CANCELLED, ExecutionPhase.FAILED, ExecutionPhase.PARKED}:
                changed = replace(changed, terminal_reason=str(reason or phase.value))
            if updater is not None:
                updated = updater(copy.deepcopy(changed))
                if not isinstance(updated, ExecutionAttemptReceipt):
                    raise TypeError("execution receipt updater must return ExecutionAttemptReceipt")
                changed = updated
            self._receipts[receipt_id] = changed
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    def start_effect(self, receipt_id: str, *, attempt_token: str, effect_identity: Mapping[str, Any]) -> ExecutionAttemptReceipt:
        return self.transition(
            receipt_id,
            ExecutionPhase.EFFECT_STARTED,
            attempt_token=attempt_token,
            effect_digest=_digest_object(effect_identity),
            metadata={"effect_identity": _safe_mapping(effect_identity)},
        )

    def commit_effect(self, receipt_id: str, *, attempt_token: str, outcome: Mapping[str, Any]) -> ExecutionAttemptReceipt:
        return self.transition(
            receipt_id,
            ExecutionPhase.EFFECT_COMMITTED,
            attempt_token=attempt_token,
            effect_digest=_digest_object(outcome),
            metadata={"outcome_keys": sorted(str(key) for key in outcome)},
        )

    def commit_yield(self, receipt_id: str, *, attempt_token: str, yield_value: Mapping[str, Any]) -> ExecutionAttemptReceipt:
        return self.transition(
            receipt_id,
            ExecutionPhase.YIELD_COMMITTED,
            attempt_token=attempt_token,
            effect_digest=_digest_object(yield_value),
        )

    def commit_handoff(self, receipt_id: str, *, attempt_token: str, handoff: Mapping[str, Any]) -> ExecutionAttemptReceipt:
        return self.transition(
            receipt_id,
            ExecutionPhase.HANDOFF_COMMITTED,
            attempt_token=attempt_token,
            effect_digest=_digest_object(handoff),
        )

    def commit_cleanup(self, receipt_id: str, *, attempt_token: str, cleanup: Mapping[str, Any]) -> ExecutionAttemptReceipt:
        current = self.get(receipt_id)
        if current.phase is ExecutionPhase.CLEANUP_COMMITTED:
            digest = _digest_object(cleanup)
            if digest != current.cleanup_digest:
                raise ExecutionReceiptConflict("cleanup receipt already committed with different digest")
            return current
        return self.transition(
            receipt_id,
            ExecutionPhase.CLEANUP_COMMITTED,
            attempt_token=attempt_token,
            effect_digest=_digest_object(cleanup),
        )

    def cancel(self, receipt_id: str, *, reason: str, attempt_token: str = "") -> ExecutionAttemptReceipt:
        current = self.get(receipt_id)
        if current.terminal:
            return current
        token = attempt_token
        if not token:
            raise ExecutionAttemptFenced("cancellation requires the durable attempt token")
        return self.transition(
            receipt_id,
            ExecutionPhase.CANCELLED,
            attempt_token=token,
            reason=reason,
        )

    def park_unsafe_restart(self, receipt_id: str, *, reason: str) -> ExecutionAttemptReceipt:
        """Park a receipt after restart without replaying an uncertain effect.

        This operation intentionally does not require the plaintext attempt
        token because the previous process is gone.  It is only legal for a
        non-terminal receipt and always moves to a terminal PARKED fence.
        """

        self._require_enabled()
        with self._lock:
            current = self._receipts.get(receipt_id)
            if current is None:
                raise ExecutionReceiptNotFound(receipt_id)
            if current.terminal:
                return copy.deepcopy(current)
            transition = ExecutionTransition(
                transition_id=new_id("exectrans"),
                phase_before=current.phase,
                phase_after=ExecutionPhase.PARKED,
                revision_before=current.revision,
                revision_after=current.revision + 1,
                attempt=current.attempt,
                token_digest=current.attempt_token_digest,
                reason=reason,
                metadata={"restart_reconciled": True, "replayed": False},
            )
            changed = replace(
                current,
                phase=ExecutionPhase.PARKED,
                revision=current.revision + 1,
                terminal_reason=reason,
                transitions=(*current.transitions, transition)[-self.maximum_history :],
                updated_at=now_iso(),
            )
            self._receipts[receipt_id] = changed
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    def quarantine_late_result(
        self,
        receipt_id: str,
        *,
        attempt: int,
        execution_ref: str,
        result: Mapping[str, Any],
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> LateExecutionResult:
        self._require_enabled()
        with self._lock:
            current = self._receipts.get(receipt_id)
            if current is None:
                raise ExecutionReceiptNotFound(receipt_id)
            late = LateExecutionResult(
                late_result_id=new_id("lateresult"),
                task_id=current.identity.task_id,
                receipt_id=receipt_id,
                attempt=max(1, int(attempt)),
                execution_ref=str(execution_ref),
                current_phase=current.phase,
                result_digest=_digest_object(result),
                reason=str(reason),
                metadata=_safe_mapping(metadata),
            )
            self._late_results.append(late)
            self._late_results = self._late_results[-1024:]
            self._receipts[receipt_id] = replace(
                current,
                late_result_count=current.late_result_count + 1,
                revision=current.revision + 1,
                updated_at=now_iso(),
            )
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(late)

    def late_results(self, *, task_id: str | None = None) -> tuple[LateExecutionResult, ...]:
        self._require_enabled()
        with self._lock:
            return tuple(
                copy.deepcopy(item)
                for item in self._late_results
                if task_id is None or item.task_id == task_id
            )

    def reconcile_after_restart(self) -> tuple[ExecutionAttemptReceipt, ...]:
        """Fail closed for every receipt that cannot be replayed safely."""

        reconciled: list[ExecutionAttemptReceipt] = []
        for receipt in self.list(include_terminal=False):
            if receipt.phase in {ExecutionPhase.RESERVED, ExecutionPhase.DISPATCHED}:
                # No side-effect boundary was crossed.  The caller may create a
                # new explicit attempt after checking task state, so park the
                # old token rather than pretending it is still live.
                reason = "restart_before_effect; explicit resume required"
            elif receipt.phase is ExecutionPhase.STARTED:
                reason = "restart_during_execution; execution outcome unknown"
            elif receipt.phase is ExecutionPhase.EFFECT_STARTED:
                reason = "restart_after_effect_started; duplicate side effect prohibited"
            else:
                reason = "restart_after_committed_effect; delivery recovery required"
            reconciled.append(self.park_unsafe_restart(receipt.receipt_id, reason=reason))
        return tuple(reconciled)

    def wait_for_terminal(self, receipt_id: str, timeout: float | None = None) -> ExecutionAttemptReceipt:
        self._require_enabled()
        with self._changed:
            if receipt_id not in self._receipts:
                raise ExecutionReceiptNotFound(receipt_id)
            self._changed.wait_for(lambda: self._receipts[receipt_id].terminal, timeout=timeout)
            return copy.deepcopy(self._receipts[receipt_id])

    def snapshot(self) -> dict[str, Any]:
        self._require_enabled()
        with self._lock:
            receipts = [item.to_dict() for item in sorted(self._receipts.values(), key=lambda value: value.receipt_id)]
            late = [item.to_dict() for item in self._late_results]
            body = {
                "schema": self.schema,
                "owner": "M1-03D ExecutionReceiptStore",
                "forbidden_physical_fields": ["worker_id", "lease_id", "capacity", "heartbeat"],
                "receipts": receipts,
                "late_results": late,
            }
            return {**body, "checksum": _digest_object(body)}

    def _validate_transition(self, current: ExecutionAttemptReceipt, target: ExecutionPhase) -> None:
        if current.terminal:
            if current.phase is target:
                return
            raise ExecutionAttemptFenced(
                f"receipt {current.receipt_id} is terminal at {current.phase.value}"
            )
        if target in {ExecutionPhase.CANCELLED, ExecutionPhase.FAILED, ExecutionPhase.PARKED}:
            return
        if _PHASE_ORDER[target] < _PHASE_ORDER[current.phase]:
            raise ExecutionReceiptConflict(
                f"execution phase regression {current.phase.value}->{target.value}"
            )
        allowed_next: dict[ExecutionPhase, set[ExecutionPhase]] = {
            ExecutionPhase.RESERVED: {ExecutionPhase.DISPATCHED},
            ExecutionPhase.DISPATCHED: {ExecutionPhase.STARTED},
            ExecutionPhase.STARTED: {ExecutionPhase.EFFECT_STARTED, ExecutionPhase.EFFECT_COMMITTED},
            ExecutionPhase.EFFECT_STARTED: {ExecutionPhase.EFFECT_COMMITTED},
            ExecutionPhase.EFFECT_COMMITTED: {ExecutionPhase.YIELD_COMMITTED},
            ExecutionPhase.YIELD_COMMITTED: {ExecutionPhase.HANDOFF_COMMITTED},
            ExecutionPhase.HANDOFF_COMMITTED: {ExecutionPhase.CLEANUP_COMMITTED},
        }
        if target is current.phase:
            return
        if target not in allowed_next.get(current.phase, set()):
            raise ExecutionReceiptConflict(
                f"invalid execution phase transition {current.phase.value}->{target.value}"
            )

    @staticmethod
    def _assert_same_identity(existing: ExecutionIdentity, requested: ExecutionIdentity) -> None:
        mismatches = []
        for name in (
            "run_id",
            "parent_task_id",
            "parent_session_id",
            "request_digest",
            "idempotency_key",
        ):
            if getattr(existing, name) != getattr(requested, name):
                mismatches.append(name)
        if mismatches:
            raise ExecutionReceiptConflict(
                "idempotency identity mismatch: " + ", ".join(mismatches)
            )

    @staticmethod
    def _assert_token(receipt: ExecutionAttemptReceipt, token: str) -> None:
        if not token or not receipt.attempt_token_digest:
            raise ExecutionAttemptFenced("execution attempt token is required")
        if not _constant_time_equal(receipt.attempt_token_digest, _digest_text(token)):
            raise ExecutionAttemptFenced("execution attempt token mismatch")

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExecutionReceiptError(f"cannot load execution receipts: {error}") from error
        if not isinstance(value, Mapping):
            raise ExecutionReceiptError("execution receipt store root must be an object")
        expected = str(value.get("checksum") or "")
        body = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
        if expected and not _constant_time_equal(expected, _digest_object(body)):
            raise ExecutionReceiptError("execution receipt store checksum mismatch")
        receipts = value.get("receipts")
        if isinstance(receipts, Iterable) and not isinstance(receipts, (str, bytes, Mapping)):
            for item in receipts:
                if not isinstance(item, Mapping):
                    continue
                receipt = ExecutionAttemptReceipt.from_dict(item)
                self._receipts[receipt.receipt_id] = receipt
                self._by_idempotency[receipt.identity.idempotency_key] = receipt.receipt_id
                self._by_task[receipt.identity.task_id] = receipt.receipt_id
        late = value.get("late_results")
        if isinstance(late, Iterable) and not isinstance(late, (str, bytes, Mapping)):
            self._late_results = [
                LateExecutionResult.from_dict(item)
                for item in late
                if isinstance(item, Mapping)
            ][-1024:]

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = self.snapshot()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise ExecutionReceiptDisabled("ExecutionReceiptStore is disabled")


def request_digest(value: Mapping[str, Any]) -> str:
    """Return the canonical request digest used before any side effect."""

    return _digest_object(_safe_mapping(value))


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        lowered = name.casefold()
        if any(token in lowered for token in ("secret", "token", "password", "authorization", "credential")):
            result[name] = "<redacted>"
        elif isinstance(item, Mapping):
            result[name] = _safe_mapping(item)
        elif isinstance(item, (list, tuple, set, frozenset)):
            result[name] = [
                _safe_mapping(entry) if isinstance(entry, Mapping) else _json_scalar(entry)
                for entry in item
            ]
        else:
            result[name] = _json_scalar(item)
    return result


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    method = getattr(value, "safe_dict", None) or getattr(value, "to_dict", None)
    if callable(method):
        selected = method()
        return _safe_mapping(selected) if isinstance(selected, Mapping) else str(selected)
    return str(value)


def _digest_object(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _digest_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(str(value).encode('utf-8')).hexdigest()}"


def _constant_time_equal(left: str, right: str) -> bool:
    return hashlib.sha256(left.encode("utf-8")).digest() == hashlib.sha256(right.encode("utf-8")).digest()


__all__ = [
    "ExecutionAttemptFenced",
    "ExecutionAttemptReceipt",
    "ExecutionClaim",
    "ExecutionEffectUnknown",
    "ExecutionIdentity",
    "ExecutionPhase",
    "ExecutionReceiptConflict",
    "ExecutionReceiptDisabled",
    "ExecutionReceiptError",
    "ExecutionReceiptNotFound",
    "ExecutionReceiptStore",
    "ExecutionTransition",
    "LateExecutionResult",
    "request_digest",
]
