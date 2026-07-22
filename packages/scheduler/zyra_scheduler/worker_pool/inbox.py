from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable, Mapping, Sequence

from .errors import WorkerPoolError, WorkerPoolErrorCode
from .lifecycle import WorkerLifecycleRuntime
from .models import (
    InboxEnvelope,
    InboxMessageState,
    WakeupRecord,
    WakeupState,
    parse_utc,
    stable_digest,
    utc_iso,
)
from .store import WorkerPoolStore


class WorkerInboxRuntime:
    """Durable inbox and wakeup queue based on AgentScope's proven lifecycle.

    A message is committed before its wakeup.  Wakeups are advisory: if a worker is
    already running, it will drain the inbox; if a resume races with a live session,
    the wakeup is requeued rather than dropped.  Claim leases make process restart
    recovery explicit.
    """

    def __init__(
        self,
        store: WorkerPoolStore,
        lifecycle: WorkerLifecycleRuntime,
        *,
        claim_ttl_seconds: float = 30.0,
    ) -> None:
        if claim_ttl_seconds <= 0:
            raise ValueError("inbox claim TTL must be positive")
        self.store = store
        self.lifecycle = lifecycle
        self.claim_ttl_seconds = float(claim_ttl_seconds)

    def enqueue(
        self,
        *,
        worker_id: str,
        task_id: str,
        run_id: str,
        message_kind: str,
        payload: Mapping[str, Any],
        priority: int = 100,
        available_at: str = "",
        idempotency_key: str = "",
        causation_id: str = "",
        correlation_id: str = "",
        wake: bool = True,
    ) -> tuple[InboxEnvelope, WakeupRecord | None]:
        worker = self.store.require_worker(worker_id)
        envelope = InboxEnvelope(
            worker_id=worker_id,
            task_id=task_id,
            run_id=run_id,
            message_kind=message_kind,
            payload=dict(payload),
            priority=priority,
            available_at=available_at or utc_iso(),
            idempotency_key=idempotency_key,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        wakeup = None
        if wake:
            wakeup = WakeupRecord(
                worker_id=worker_id,
                task_id=task_id,
                envelope_id=envelope.envelope_id,
                reason=f"inbox:{message_kind}",
                available_at=envelope.available_at,
                idempotency_key=stable_digest((envelope.idempotency_key, "wakeup")),
                metadata={
                    "worker_state_at_enqueue": worker.state.value,
                    "inbox_owner": "WorkerInboxRuntime",
                },
            )
        return self.store.enqueue_inbox(envelope, wakeup=wakeup)

    def claim(
        self,
        worker_id: str,
        *,
        claim_owner: str,
        limit: int = 32,
    ) -> tuple[InboxEnvelope, ...]:
        self.store.require_worker(worker_id)
        deadline = utc_iso(parse_utc(utc_iso()) + timedelta(seconds=self.claim_ttl_seconds))
        return self.store.claim_inbox(
            worker_id,
            claim_owner=claim_owner,
            claim_deadline_at=deadline,
            limit=limit,
        )

    def acknowledge(
        self,
        envelope_id: str,
        *,
        claim_owner: str,
    ) -> InboxEnvelope:
        envelope = self._require_envelope(envelope_id)
        if envelope.state is InboxMessageState.ACKNOWLEDGED:
            return envelope
        self._assert_claim(envelope, claim_owner)
        updated = InboxEnvelope.from_dict(
            {
                **envelope.to_dict(),
                "state": InboxMessageState.ACKNOWLEDGED.value,
                "acknowledged_at": utc_iso(),
                "claim_deadline_at": "",
                "version": envelope.version + 1,
            }
        )
        return self.store.update_inbox(
            updated,
            expected_version=envelope.version,
            operation="inbox_acknowledged",
        )

    def requeue(
        self,
        envelope_id: str,
        *,
        claim_owner: str,
        reason: str,
        delay_seconds: float = 0.0,
    ) -> InboxEnvelope:
        envelope = self._require_envelope(envelope_id)
        self._assert_claim(envelope, claim_owner)
        if envelope.delivery_count >= envelope.max_deliveries:
            state = InboxMessageState.DEAD_LETTERED
        else:
            state = InboxMessageState.REQUEUED
        available_at = utc_iso(parse_utc(utc_iso()) + timedelta(seconds=max(0.0, delay_seconds)))
        updated = InboxEnvelope.from_dict(
            {
                **envelope.to_dict(),
                "state": state.value,
                "available_at": available_at,
                "claim_owner": "",
                "claim_deadline_at": "",
                "failure_reason": reason,
                "version": envelope.version + 1,
            }
        )
        result = self.store.update_inbox(
            updated,
            expected_version=envelope.version,
            operation="inbox_dead_lettered" if state is InboxMessageState.DEAD_LETTERED else "inbox_requeued",
        )
        if state is InboxMessageState.REQUEUED:
            wakeup = WakeupRecord(
                worker_id=envelope.worker_id,
                task_id=envelope.task_id,
                envelope_id=envelope.envelope_id,
                reason="inbox_requeued",
                available_at=available_at,
                idempotency_key=stable_digest((envelope.idempotency_key, envelope.delivery_count, reason)),
            )
            self.store.enqueue_wakeup(wakeup)
        return result

    def cancel_task_messages(self, task_id: str, *, reason: str) -> tuple[InboxEnvelope, ...]:
        changed: list[InboxEnvelope] = []
        active_states = (
            InboxMessageState.PENDING,
            InboxMessageState.REQUEUED,
            InboxMessageState.CLAIMED,
        )
        for envelope in self.store.list_inbox(task_id=task_id, states=active_states):
            updated = InboxEnvelope.from_dict(
                {
                    **envelope.to_dict(),
                    "state": InboxMessageState.CANCELLED.value,
                    "failure_reason": reason,
                    "claim_deadline_at": "",
                    "version": envelope.version + 1,
                }
            )
            changed.append(
                self.store.update_inbox(
                    updated,
                    expected_version=envelope.version,
                    operation="inbox_cancelled",
                )
            )
        return tuple(changed)

    def dispatch_wakeups(
        self,
        *,
        dispatcher_id: str,
        is_session_active: Callable[[str], bool],
        on_wakeup: Callable[[WakeupRecord], Any],
        worker_id: str = "",
        limit: int = 32,
        requeue_delay_seconds: float = 0.25,
    ) -> tuple[WakeupRecord, ...]:
        dispatched: list[WakeupRecord] = []
        for wakeup in self.store.claim_wakeups(
            claim_owner=dispatcher_id,
            worker_id=worker_id,
            limit=limit,
        ):
            if is_session_active(wakeup.worker_id):
                self._requeue_wakeup(
                    wakeup,
                    reason="worker session is already active and will drain inbox",
                    delay_seconds=requeue_delay_seconds,
                    operation="wakeup_requeued_active_session",
                )
                continue
            try:
                self.lifecycle.wake(wakeup.worker_id)
            except WorkerPoolError as error:
                if error.code is not WorkerPoolErrorCode.INVALID_WORKER_TRANSITION:
                    raise
            try:
                result = on_wakeup(wakeup)
                accepted = result is not False
                if isinstance(result, Mapping):
                    accepted = bool(result.get("accepted", True))
                if not accepted:
                    raise WorkerPoolError(
                        WorkerPoolErrorCode.EXECUTION_REJECTED,
                        "wakeup execution port rejected the dispatch",
                        operation="dispatch_worker_wakeup",
                        worker_id=wakeup.worker_id,
                        task_id=wakeup.task_id,
                    )
            except Exception as error:
                self._requeue_wakeup(
                    wakeup,
                    reason=f"{type(error).__name__}: {error}",
                    delay_seconds=requeue_delay_seconds,
                    operation="wakeup_requeued_dispatch_failure",
                )
                raise
            updated = WakeupRecord.from_dict(
                {
                    **wakeup.to_dict(),
                    "state": WakeupState.DISPATCHED.value,
                    "dispatched_at": utc_iso(),
                    "version": wakeup.version + 1,
                }
            )
            dispatched.append(
                self.store.update_wakeup(
                    updated,
                    expected_version=wakeup.version,
                    operation="wakeup_dispatched",
                )
            )
        return tuple(dispatched)

    def recover_wakeup_claims(self, *, now: str | None = None) -> tuple[WakeupRecord, ...]:
        current = parse_utc(now or utc_iso())
        recovered: list[WakeupRecord] = []
        for wakeup in self.store.list_wakeups(states=(WakeupState.CLAIMED,)):
            if not wakeup.claimed_at:
                expired = True
            else:
                expired = (
                    parse_utc(wakeup.claimed_at) + timedelta(seconds=self.claim_ttl_seconds)
                    <= current
                )
            if not expired:
                continue
            recovered.append(
                self._requeue_wakeup(
                    wakeup,
                    reason="wakeup claim owner did not acknowledge before deadline",
                    delay_seconds=0,
                    operation="wakeup_claim_recovered",
                )
            )
        return tuple(recovered)

    def recover_expired_claims(self, *, now: str | None = None) -> tuple[InboxEnvelope, ...]:
        current = parse_utc(now or utc_iso())
        recovered: list[InboxEnvelope] = []
        for envelope in self.store.list_inbox(states=(InboxMessageState.CLAIMED,)):
            if not envelope.claim_deadline_at or parse_utc(envelope.claim_deadline_at) > current:
                continue
            state = (
                InboxMessageState.DEAD_LETTERED
                if envelope.delivery_count >= envelope.max_deliveries
                else InboxMessageState.REQUEUED
            )
            updated = InboxEnvelope.from_dict(
                {
                    **envelope.to_dict(),
                    "state": state.value,
                    "claim_owner": "",
                    "claim_deadline_at": "",
                    "failure_reason": "claim owner did not acknowledge before deadline",
                    "version": envelope.version + 1,
                }
            )
            recovered.append(
                self.store.update_inbox(
                    updated,
                    expected_version=envelope.version,
                    operation="inbox_claim_recovered",
                )
            )
        return tuple(recovered)

    def _require_envelope(self, envelope_id: str) -> InboxEnvelope:
        envelope = self.store.get_inbox(envelope_id)
        if envelope is None:
            raise WorkerPoolError(
                WorkerPoolErrorCode.INBOX_NOT_FOUND,
                "worker inbox envelope was not found",
                operation="require_inbox_envelope",
            )
        return envelope

    def _requeue_wakeup(
        self,
        wakeup: WakeupRecord,
        *,
        reason: str,
        delay_seconds: float,
        operation: str,
    ) -> WakeupRecord:
        updated = WakeupRecord.from_dict(
            {
                **wakeup.to_dict(),
                "state": WakeupState.REQUEUED.value,
                "available_at": utc_iso(
                    parse_utc(utc_iso()) + timedelta(seconds=max(0.0, delay_seconds))
                ),
                "claim_owner": "",
                "claimed_at": "",
                "version": wakeup.version + 1,
                "metadata": {
                    **dict(wakeup.metadata),
                    "requeue_reason": reason,
                },
            }
        )
        return self.store.update_wakeup(
            updated,
            expected_version=wakeup.version,
            operation=operation,
        )

    @staticmethod
    def _assert_claim(envelope: InboxEnvelope, claim_owner: str) -> None:
        if envelope.state is not InboxMessageState.CLAIMED or envelope.claim_owner != claim_owner:
            raise WorkerPoolError(
                WorkerPoolErrorCode.INBOX_CLAIM_CONFLICT,
                "inbox envelope is not claimed by the caller",
                operation="assert_inbox_claim",
                worker_id=envelope.worker_id,
                task_id=envelope.task_id,
            )
