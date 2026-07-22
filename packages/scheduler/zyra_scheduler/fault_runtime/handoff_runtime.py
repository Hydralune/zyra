from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from zyra_core import TaskState

from .contracts import FaultSignal, ProjectionReceipt, RecoveryHandoff, runtime_id, utc_now
from .event_writer import FaultSignalEventWriter
from .recovery_bridge import WatchdogRecoveryBridge
from .state_store import FaultStateStore


class RecoveryConsumer(Protocol):
    """Port implemented by M1-07C; 07B never selects a recovery plan."""

    consumer_id: str

    def accept_fault_handoff(
        self,
        handoff: RecoveryHandoff,
        *,
        task_state: TaskState | None,
        causal_context: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class HandoffDispatchStatus(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    RELEASED = "released"
    DEAD_LETTERED = "dead_lettered"
    SKIPPED = "skipped"
    BLOCKED_PROJECTION = "blocked_projection"


@dataclass(frozen=True, slots=True)
class HandoffDispatchReceipt:
    handoff_id: str
    signal_id: str
    task_id: str
    consumer_id: str
    status: HandoffDispatchStatus
    attempt_count: int
    lease_token: str
    projection_event_id: str
    consumer_receipt: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    receipt_id: str = field(default_factory=lambda: runtime_id("handoff-dispatch"))
    created_at: str = field(default_factory=utc_now)

    @property
    def acknowledged(self) -> bool:
        return self.status is HandoffDispatchStatus.ACKNOWLEDGED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.watchdog-handoff-dispatch-receipt/v1",
            "receipt_id": self.receipt_id,
            "handoff_id": self.handoff_id,
            "signal_id": self.signal_id,
            "task_id": self.task_id,
            "consumer_id": self.consumer_id,
            "status": self.status.value,
            "attempt_count": self.attempt_count,
            "lease_token": self.lease_token,
            "projection_event_id": self.projection_event_id,
            "consumer_receipt": dict(self.consumer_receipt),
            "error": self.error,
            "created_at": self.created_at,
            "acknowledged": self.acknowledged,
            "recovery_plan_selected_by_07b": False,
        }


class HandoffCausalityGuard:
    """Verifies ``FaultSignal -> projection -> handoff`` before dispatch.

    The guard is deliberately effect-oriented. A handoff cannot reach 07C if
    the canonical event write failed, the handoff points at a different
    observation scope, or the producer claims to have executed the planner.
    Memory and scheduler delivery are reported independently because some
    fault kinds do not have a scheduler target and deployments may omit a
    memory writer during maintenance. Their delivery failures remain visible
    and cause the projection retry path to run.
    """

    def __init__(self, store: FaultStateStore, writer: FaultSignalEventWriter) -> None:
        self.store = store
        self.writer = writer

    def verify(
        self,
        handoff: RecoveryHandoff,
        *,
        task_state: TaskState | None,
        repair_projection: bool = True,
    ) -> Mapping[str, Any]:
        signal = self.store.signal(handoff.signal_id)
        if signal is None:
            return self._failure(handoff, "signal_missing", "canonical fault signal does not exist")
        mismatch = self._scope_mismatch(signal, handoff)
        if mismatch:
            return self._failure(handoff, "scope_mismatch", mismatch)
        if bool(handoff.metadata.get("planner_executed")):
            return self._failure(
                handoff,
                "planner_owner_violation",
                "07B handoff claims recovery planner execution",
            )
        projection = self.store.projection_receipt(signal.signal_id)
        repaired = False
        if (projection is None or not projection.ok) and repair_projection:
            projection = self.writer.retry_incomplete(signal, task_state=task_state)
            repaired = True
        if projection is None:
            return self._failure(handoff, "projection_missing", "fault projection receipt is missing")
        if not projection.canonical_event_written:
            return self._failure(
                handoff,
                "canonical_event_missing",
                "; ".join(projection.errors) or "canonical event write not confirmed",
                projection=projection,
            )
        if projection.errors:
            return self._failure(
                handoff,
                "projection_incomplete",
                "; ".join(projection.errors),
                projection=projection,
            )
        return {
            "schema": "zyra.watchdog-handoff-causality/v1",
            "ok": True,
            "handoff_id": handoff.handoff_id,
            "signal_id": signal.signal_id,
            "observation_id": signal.refs.observation_id,
            "event_id": projection.event_id,
            "event_written": projection.canonical_event_written,
            "memory_record_ids": list(projection.memory_record_ids),
            "scheduler_changed": projection.scheduler_changed,
            "scheduler_receipt": dict(projection.scheduler_receipt),
            "task_projection_changed": projection.task_projection_changed,
            "projection_repaired": repaired,
            "fault_kind": signal.kind.value,
            "run_id": signal.refs.run_id,
            "task_id": signal.refs.task_id,
            "recovery_plan_owner": "M1-S07C",
            "producer_selected_plan": False,
        }

    @staticmethod
    def _scope_mismatch(signal: FaultSignal, handoff: RecoveryHandoff) -> str:
        if handoff.run_id != signal.refs.run_id:
            return "handoff run_id differs from fault signal"
        if handoff.task_id != signal.refs.task_id:
            return "handoff task_id differs from fault signal"
        if handoff.refs.observation_id != signal.refs.observation_id:
            return "handoff observation_id differs from fault signal"
        for name in (
            "session_id",
            "node_id",
            "attempt_id",
            "tool_call_id",
            "worker_id",
            "backend_id",
            "provider_id",
            "workspace_id",
            "browser_session_id",
            "mcp_server_id",
        ):
            if getattr(handoff.refs, name) != getattr(signal.refs, name):
                return f"handoff {name} differs from fault signal"
        return ""

    @staticmethod
    def _failure(
        handoff: RecoveryHandoff,
        code: str,
        reason: str,
        *,
        projection: ProjectionReceipt | None = None,
    ) -> Mapping[str, Any]:
        return {
            "schema": "zyra.watchdog-handoff-causality/v1",
            "ok": False,
            "handoff_id": handoff.handoff_id,
            "signal_id": handoff.signal_id,
            "run_id": handoff.run_id,
            "task_id": handoff.task_id,
            "error_code": code,
            "error": reason,
            "event_id": projection.event_id if projection else "",
            "event_written": bool(projection and projection.canonical_event_written),
            "projection_errors": list(projection.errors) if projection else [],
            "recovery_plan_owner": "M1-S07C",
            "producer_selected_plan": False,
        }


class SameRunHandoffDispatcher:
    """Lease-fenced outbox dispatcher for the future 07C consumer.

    A consumer is injected as a narrow port. The dispatcher verifies causal
    projections, calls the consumer once per valid lease, and writes the
    consumer receipt into the existing handoff delivery journal. Exceptions
    release the lease with bounded backoff. No action in
    ``handoff.requested_actions`` is interpreted here.
    """

    _METADATA_KEY = "M1-S07B-02.handoff-dispatch"

    def __init__(
        self,
        store: FaultStateStore,
        bridge: WatchdogRecoveryBridge,
        writer: FaultSignalEventWriter,
        *,
        task_state_resolver: Callable[[str], TaskState | None] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
        lease_ms: int = 30_000,
        retry_base_ms: int = 500,
        max_attempts: int = 5,
    ) -> None:
        if lease_ms < 1 or retry_base_ms < 1 or max_attempts < 1:
            raise ValueError("handoff dispatch timing policy must be positive")
        self.store = store
        self.bridge = bridge
        self.writer = writer
        self.task_state_resolver = task_state_resolver
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self.lease_ms = lease_ms
        self.retry_base_ms = retry_base_ms
        self.max_attempts = max_attempts
        self.causality = HandoffCausalityGuard(store, writer)
        self._guard = threading.RLock()
        self._consumers: dict[str, RecoveryConsumer] = {}
        self._receipts: list[HandoffDispatchReceipt] = []
        self._metadata_revision = 0
        self._restore()

    def register(self, consumer: RecoveryConsumer) -> Mapping[str, Any]:
        consumer_id = str(getattr(consumer, "consumer_id", "") or "").strip()
        if not consumer_id:
            raise ValueError("recovery consumer requires consumer_id")
        if not callable(getattr(consumer, "accept_fault_handoff", None)):
            raise TypeError("recovery consumer lacks accept_fault_handoff")
        with self._guard:
            prior = self._consumers.get(consumer_id)
            if prior is not None and prior is not consumer:
                raise RuntimeError(f"recovery consumer already registered: {consumer_id}")
            self._consumers[consumer_id] = consumer
            self._persist_locked()
        return {
            "schema": "zyra.watchdog-recovery-consumer-registration/v1",
            "consumer_id": consumer_id,
            "registered": True,
            "owns_recovery_plan": True,
            "registered_by": "M1-S07B-02.SameRunHandoffDispatcher",
        }

    def unregister(self, consumer_id: str) -> bool:
        with self._guard:
            removed = self._consumers.pop(consumer_id, None) is not None
            if removed:
                self._persist_locked()
            return removed

    def dispatch(
        self,
        consumer_id: str,
        *,
        task_id: str = "",
        limit: int = 20,
        now_ms: int | None = None,
    ) -> tuple[HandoffDispatchReceipt, ...]:
        consumer = self._require_consumer(consumer_id)
        now = self._now(now_ms)
        claims = self.bridge.claim(
            consumer=consumer_id,
            now_ms=now,
            lease_ms=self.lease_ms,
            task_id=task_id,
            limit=limit,
        )
        receipts: list[HandoffDispatchReceipt] = []
        for claim in claims:
            receipts.append(self._dispatch_claim(consumer, claim, now_ms=now))
        with self._guard:
            self._receipts.extend(receipts)
            del self._receipts[:-500]
            if receipts:
                self._persist_locked()
        return tuple(receipts)

    def dispatch_one(
        self,
        consumer_id: str,
        handoff_id: str,
        *,
        now_ms: int | None = None,
    ) -> HandoffDispatchReceipt:
        handoff = self.store.handoff(handoff_id)
        if handoff is None:
            raise KeyError(f"recovery handoff not found: {handoff_id}")
        receipts = self.dispatch(
            consumer_id,
            task_id=handoff.task_id,
            limit=100,
            now_ms=now_ms,
        )
        selected = next((item for item in receipts if item.handoff_id == handoff_id), None)
        if selected is not None:
            return selected
        return HandoffDispatchReceipt(
            handoff_id=handoff_id,
            signal_id=handoff.signal_id,
            task_id=handoff.task_id,
            consumer_id=consumer_id,
            status=HandoffDispatchStatus.SKIPPED,
            attempt_count=0,
            lease_token="",
            projection_event_id="",
            error="handoff was not claimable by this consumer",
        )

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        deliveries = self.bridge.delivery_snapshot(task_id=task_id)
        with self._guard:
            receipts = [
                item.to_dict()
                for item in self._receipts[-200:]
                if not task_id or item.task_id == task_id
            ]
            consumers = sorted(self._consumers)
        counts: dict[str, int] = {}
        for delivery in deliveries:
            status = str(delivery.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return {
            "schema": "zyra.watchdog-handoff-dispatcher/v1",
            "consumer_ids": consumers,
            "delivery_counts": dict(sorted(counts.items())),
            "deliveries": [dict(item) for item in deliveries],
            "recent_receipts": receipts,
            "lease_ms": self.lease_ms,
            "retry_base_ms": self.retry_base_ms,
            "max_attempts": self.max_attempts,
            "metadata_revision": self._metadata_revision,
            "owns_recovery_plan": False,
            "recovery_plan_owner": "registered M1-S07C consumer",
        }

    def _dispatch_claim(
        self,
        consumer: RecoveryConsumer,
        claim: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> HandoffDispatchReceipt:
        claimed_handoff = claim.get("handoff")
        claimed_handoff_value = dict(claimed_handoff) if isinstance(claimed_handoff, Mapping) else {}
        handoff_id = str(claim.get("handoff_id") or claimed_handoff_value.get("handoff_id") or "")
        lease_token = str(claim.get("lease_token") or "")
        attempt_count = int(claim.get("attempt_count") or claim.get("attempt") or 0)
        handoff = self.store.handoff(handoff_id)
        if handoff is None:
            return HandoffDispatchReceipt(
                handoff_id=handoff_id or "missing-handoff",
                signal_id="missing-signal",
                task_id="missing-task",
                consumer_id=consumer.consumer_id,
                status=HandoffDispatchStatus.SKIPPED,
                attempt_count=attempt_count,
                lease_token=lease_token,
                projection_event_id="",
                error="claimed handoff record is missing",
            )
        task_state = self.task_state_resolver(handoff.task_id) if self.task_state_resolver else None
        causal = self.causality.verify(
            handoff,
            task_state=task_state,
            repair_projection=True,
        )
        if not bool(causal.get("ok")):
            release = self.bridge.release(
                handoff.handoff_id,
                consumer=consumer.consumer_id,
                lease_token=lease_token,
                now_ms=now_ms,
                retry_after_ms=self._retry_after(attempt_count),
                error=str(causal.get("error") or "handoff causality failed"),
                max_attempts=self.max_attempts,
            )
            status = (
                HandoffDispatchStatus.DEAD_LETTERED
                if str(release.get("status")) == "dead_letter"
                else HandoffDispatchStatus.BLOCKED_PROJECTION
            )
            return HandoffDispatchReceipt(
                handoff_id=handoff.handoff_id,
                signal_id=handoff.signal_id,
                task_id=handoff.task_id,
                consumer_id=consumer.consumer_id,
                status=status,
                attempt_count=attempt_count,
                lease_token=lease_token,
                projection_event_id=str(causal.get("event_id") or ""),
                consumer_receipt={"causality": dict(causal), "release": dict(release)},
                error=str(causal.get("error") or "handoff causality failed"),
            )
        try:
            raw_receipt = consumer.accept_fault_handoff(
                handoff,
                task_state=task_state,
                causal_context=causal,
            )
            consumer_receipt = dict(raw_receipt)
            if str(consumer_receipt.get("handoff_id") or handoff.handoff_id) != handoff.handoff_id:
                raise ValueError("recovery consumer receipt handoff_id mismatch")
            if str(consumer_receipt.get("signal_id") or handoff.signal_id) != handoff.signal_id:
                raise ValueError("recovery consumer receipt signal_id mismatch")
            if bool(consumer_receipt.get("retry")):
                raise RetryableHandoffError(
                    str(consumer_receipt.get("error") or "recovery consumer requested retry")
                )
            acknowledged = self.bridge.acknowledge(
                handoff.handoff_id,
                consumer=consumer.consumer_id,
                lease_token=lease_token,
                receipt={
                    "schema": "zyra.watchdog-to-recovery-consumer-receipt/v1",
                    "handoff_id": handoff.handoff_id,
                    "signal_id": handoff.signal_id,
                    "event_id": str(causal.get("event_id") or ""),
                    "consumer_receipt": consumer_receipt,
                    "recovery_plan_selected_by_consumer": True,
                    "recovery_plan_selected_by_07b": False,
                },
            )
            return HandoffDispatchReceipt(
                handoff_id=handoff.handoff_id,
                signal_id=handoff.signal_id,
                task_id=handoff.task_id,
                consumer_id=consumer.consumer_id,
                status=HandoffDispatchStatus.ACKNOWLEDGED,
                attempt_count=attempt_count,
                lease_token=lease_token,
                projection_event_id=str(causal.get("event_id") or ""),
                consumer_receipt={**consumer_receipt, "delivery": dict(acknowledged)},
            )
        except Exception as error:
            release = self.bridge.release(
                handoff.handoff_id,
                consumer=consumer.consumer_id,
                lease_token=lease_token,
                now_ms=now_ms,
                retry_after_ms=self._retry_after(attempt_count),
                error=f"{type(error).__name__}: {error}",
                max_attempts=self.max_attempts,
            )
            status = (
                HandoffDispatchStatus.DEAD_LETTERED
                if str(release.get("status")) == "dead_letter"
                else HandoffDispatchStatus.RELEASED
            )
            return HandoffDispatchReceipt(
                handoff_id=handoff.handoff_id,
                signal_id=handoff.signal_id,
                task_id=handoff.task_id,
                consumer_id=consumer.consumer_id,
                status=status,
                attempt_count=attempt_count,
                lease_token=lease_token,
                projection_event_id=str(causal.get("event_id") or ""),
                consumer_receipt={"release": dict(release)},
                error=f"{type(error).__name__}: {error}",
            )

    def _retry_after(self, attempt_count: int) -> int:
        exponent = max(0, attempt_count - 1)
        return min(self.retry_base_ms * (2**exponent), 60_000)

    def _require_consumer(self, consumer_id: str) -> RecoveryConsumer:
        with self._guard:
            consumer = self._consumers.get(consumer_id)
        if consumer is None:
            raise KeyError(f"recovery consumer not registered: {consumer_id}")
        return consumer

    def _persist_locked(self) -> None:
        payload = {
            "schema": "zyra.watchdog-handoff-dispatcher/v1",
            "consumer_ids": sorted(self._consumers),
            "recent_receipts": [item.to_dict() for item in self._receipts[-100:]],
            "updated_at": utc_now(),
            "owns_recovery_plan": False,
        }
        self._metadata_revision = self.store.put_metadata(
            self._METADATA_KEY,
            payload,
            expected_revision=self._metadata_revision,
        )

    def _restore(self) -> None:
        saved = self.store.metadata(self._METADATA_KEY)
        if saved is None:
            return
        _payload, revision = saved
        self._metadata_revision = revision

    def _now(self, value: int | None) -> int:
        selected = self.monotonic_ms() if value is None else int(value)
        if selected < 0:
            raise ValueError("monotonic time must not be negative")
        return selected


class RetryableHandoffError(RuntimeError):
    pass


def handoff_dispatch_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.watchdog-handoff-dispatch-contract/v1",
        "producer": "M1-S07B-02.SameRunHandoffDispatcher",
        "consumer": "M1-S07C.RecoveryPlanner port",
        "causal_chain": [
            "StructuredObservation",
            "FaultSignal",
            "ProjectionReceipt.event_id",
            "RecoveryHandoff",
            "lease",
            "consumer receipt",
        ],
        "lease_fenced": True,
        "bounded_retry": True,
        "projection_repaired_before_dispatch": True,
        "owns_recovery_plan": False,
        "interprets_requested_actions": False,
    }


__all__ = [
    "HandoffCausalityGuard",
    "HandoffDispatchReceipt",
    "HandoffDispatchStatus",
    "RecoveryConsumer",
    "RetryableHandoffError",
    "SameRunHandoffDispatcher",
    "handoff_dispatch_contract",
]
