from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import EventRecord, EventType

from ...browser_session.errors import BrowserConnectionLost, BrowserRuntimeError
from ..models import ObservationScope, digest_value, new_observation_id, utc_now
from .contracts import EvidenceSource, RuntimeEvidenceEnvelope


class BrowserObservabilityBusError(BrowserRuntimeError):
    code = "browser_observability_bus_error"


class BrowserReconnectExhausted(BrowserConnectionLost):
    code = "browser_reconnection_failed"

    def __init__(self, message: str, *, receipt: "ReconnectObservationReceipt") -> None:
        super().__init__(
            message,
            session_id=receipt.scope.browser_session_id,
            operation="browser_session_reconnect",
            details=receipt.to_dict(),
            code=self.code,
            retryable=False,
            terminal=True,
        )
        self.receipt = receipt


class AttachmentPhase(StrEnum):
    NEW = "new"
    ATTACHED = "attached"
    DRAINING = "draining"
    DETACHED = "detached"
    FAILED = "failed"


class AttachedEventKind(StrEnum):
    CDP_EVENT = "cdp_event"
    CDP_CONNECTION_LOST = "cdp_connection_lost"
    CDP_REQUEST_TIMEOUT = "cdp_request_timeout"
    CDP_PROTOCOL_ERROR = "cdp_protocol_error"
    CDP_HANDLER_ERROR = "cdp_handler_error"
    SESSION_EVENT = "session_event"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EventAttachmentPolicy:
    event_capacity: int = 4_096
    max_callback_failures: int = 3
    teardown_timeout_seconds: float = 5.0
    reject_stale_generation: bool = True
    reconnect_attempts: int = 3
    reconnect_backoff_seconds: tuple[float, ...] = (0.0, 0.05, 0.1)

    def __post_init__(self) -> None:
        if self.event_capacity < 1:
            raise ValueError("event attachment capacity must be positive")
        if self.max_callback_failures < 1:
            raise ValueError("event callback failure limit must be positive")
        if self.teardown_timeout_seconds <= 0:
            raise ValueError("event teardown timeout must be positive")
        if self.reconnect_attempts < 1:
            raise ValueError("reconnect attempts must be positive")
        if any(item < 0 for item in self.reconnect_backoff_seconds):
            raise ValueError("reconnect delays must be non-negative")


@dataclass(frozen=True, slots=True)
class AttachedBrowserEvent:
    scope: ObservationScope
    topic: str
    payload: Mapping[str, Any]
    bus_event_id: str
    bus_generation: int
    bus_sequence: int
    source: str
    received_at: str = field(default_factory=utc_now)
    receipt_id: str = field(
        default_factory=lambda: new_observation_id("browser-attached-event")
    )
    stale: bool = False

    @property
    def kind(self) -> AttachedEventKind:
        return {
            "browser.cdp.event": AttachedEventKind.CDP_EVENT,
            "browser.cdp.connection_lost": AttachedEventKind.CDP_CONNECTION_LOST,
            "browser.cdp.request_timeout": AttachedEventKind.CDP_REQUEST_TIMEOUT,
            "browser.cdp.protocol_error": AttachedEventKind.CDP_PROTOCOL_ERROR,
            "browser.cdp.handler_error": AttachedEventKind.CDP_HANDLER_ERROR,
        }.get(self.topic, AttachedEventKind.SESSION_EVENT if self.topic.startswith("browser.") else AttachedEventKind.UNKNOWN)

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.browser-observability.attached-event.v1",
            "receipt_id": self.receipt_id,
            "scope": self.scope.to_dict(),
            "topic": self.topic,
            "kind": str(self.kind),
            "payload": dict(self.payload),
            "bus_event_id": self.bus_event_id,
            "bus_generation": self.bus_generation,
            "bus_sequence": self.bus_sequence,
            "source": self.source,
            "received_at": self.received_at,
            "stale": self.stale,
        }
        if include_digest:
            value["digest"] = digest_value(value)
        return value

    def evidence(self) -> RuntimeEvidenceEnvelope:
        source_event_type = str(self.payload.get("method") or self.topic)
        return RuntimeEvidenceEnvelope(
            scope=self.scope,
            source=EvidenceSource.BROWSER_CDP,
            event_type=source_event_type,
            payload={
                **dict(self.payload),
                "bus_generation": self.bus_generation,
                "bus_sequence": self.bus_sequence,
                "attached_receipt_id": self.receipt_id,
            },
            evidence_id=self.receipt_id,
            source_event_id=self.bus_event_id,
            correlation_event_ids=(self.bus_event_id,),
            retryable=self.kind in {
                AttachedEventKind.CDP_CONNECTION_LOST,
                AttachedEventKind.CDP_REQUEST_TIMEOUT,
            },
            outcome_unknown=self.kind in {
                AttachedEventKind.CDP_CONNECTION_LOST,
                AttachedEventKind.CDP_REQUEST_TIMEOUT,
            },
            metadata={"stale": self.stale, "source": self.source},
        )


@dataclass(frozen=True, slots=True)
class EventAttachmentReceipt:
    scope: ObservationScope
    attachment_id: str
    subscription_id: str
    phase: AttachmentPhase
    generation: int
    attached_at: str
    detached_at: str = ""
    accepted_events: int = 0
    stale_events: int = 0
    callback_failures: int = 0
    drained: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.event-attachment.v1",
            "scope": self.scope.to_dict(),
            "attachment_id": self.attachment_id,
            "subscription_id": self.subscription_id,
            "phase": str(self.phase),
            "generation": self.generation,
            "attached_at": self.attached_at,
            "detached_at": self.detached_at,
            "accepted_events": self.accepted_events,
            "stale_events": self.stale_events,
            "callback_failures": self.callback_failures,
            "drained": self.drained,
            "error": self.error,
            "canonical_event_bus_owner": "M1-04A",
        }


@dataclass(frozen=True, slots=True)
class ReconnectAttemptObservation:
    attempt: int
    delay_seconds: float
    started_at: str
    duration_ms: int
    ok: bool
    error: str = ""
    session_generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "delay_seconds": self.delay_seconds,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "error": self.error,
            "session_generation": self.session_generation,
        }


@dataclass(frozen=True, slots=True)
class ReconnectObservationReceipt:
    scope: ObservationScope
    reason: str
    attempts: tuple[ReconnectAttemptObservation, ...]
    exhausted: bool
    receipt_id: str = field(
        default_factory=lambda: new_observation_id("browser-reconnect-observation")
    )
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.reconnect-observation.v1",
            "receipt_id": self.receipt_id,
            "scope": self.scope.to_dict(),
            "reason": self.reason,
            "attempts": [item.to_dict() for item in self.attempts],
            "exhausted": self.exhausted,
            "created_at": self.created_at,
            "reconnect_owner": "M1-04A",
            "recovery_planner_owner": "M1-07C",
            "is_recovery_plan": False,
        }


class BrowserEventAttachment:
    """Observe the existing 04A event bus without becoming a second bus owner."""

    def __init__(
        self,
        scope: ObservationScope,
        event_bus: Any,
        *,
        policy: EventAttachmentPolicy | None = None,
        event_sink: Callable[[AttachedBrowserEvent], None] | None = None,
    ) -> None:
        self.scope = scope
        self.event_bus = event_bus
        self.policy = policy or EventAttachmentPolicy()
        self.event_sink = event_sink
        self.attachment_id = new_observation_id("browser-bus-attachment")
        self._subscription_id = ""
        self._phase = AttachmentPhase.NEW
        self._generation = 0
        self._events: deque[AttachedBrowserEvent] = deque(maxlen=self.policy.event_capacity)
        self._accepted = 0
        self._stale = 0
        self._callback_failures = 0
        self._inflight = 0
        self._attached_at = ""
        self._detached_at = ""
        self._error = ""
        self._guard = threading.RLock()
        self._idle = threading.Condition(self._guard)

    @property
    def phase(self) -> AttachmentPhase:
        with self._guard:
            return self._phase

    def attach(self) -> EventAttachmentReceipt:
        with self._guard:
            if self._phase == AttachmentPhase.ATTACHED:
                return self.receipt()
            snapshot = self.event_bus.snapshot()
            if str(snapshot.state) != "running":
                self.event_bus.start()
                snapshot = self.event_bus.snapshot()
            self._generation = int(snapshot.generation)
            subscription = self.event_bus.subscribe(
                self._on_event,
                topic_prefix="browser.",
                max_failures=self.policy.max_callback_failures,
                subscription_id=f"obs-{self.attachment_id}",
            )
            self._subscription_id = str(subscription.subscription_id)
            self._phase = AttachmentPhase.ATTACHED
            self._attached_at = utc_now()
            self._detached_at = ""
            self._error = ""
            return self.receipt()

    def detach(self, *, drain: bool = True) -> EventAttachmentReceipt:
        with self._idle:
            if self._phase == AttachmentPhase.DETACHED:
                return self.receipt(drained=True)
            self._phase = AttachmentPhase.DRAINING
            deadline = time.monotonic() + self.policy.teardown_timeout_seconds
            while drain and self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._phase = AttachmentPhase.FAILED
                    self._error = "event attachment teardown timed out"
                    return self.receipt(drained=False)
                self._idle.wait(timeout=remaining)
            if self._subscription_id:
                self.event_bus.unsubscribe(self._subscription_id)
            self._phase = AttachmentPhase.DETACHED
            self._detached_at = utc_now()
            return self.receipt(drained=not self._inflight)

    def rebind(self, event_bus: Any) -> EventAttachmentReceipt:
        self.detach(drain=True)
        with self._guard:
            self.event_bus = event_bus
            self._phase = AttachmentPhase.NEW
            self._subscription_id = ""
        return self.attach()

    def events(self, *, after_sequence: int = 0) -> tuple[AttachedBrowserEvent, ...]:
        with self._guard:
            return tuple(
                item for item in self._events if item.bus_sequence > after_sequence
            )

    def evidence(self) -> tuple[RuntimeEvidenceEnvelope, ...]:
        return tuple(item.evidence() for item in self.events() if not item.stale)

    def canonical_events(self) -> tuple[EventRecord, ...]:
        return tuple(
            EventRecord(
                run_id=self.scope.run_id,
                task_id=self.scope.task_id,
                node_id=self.scope.node_id or None,
                event_type=(
                    EventType.WORKER_HEALTH
                    if item.kind
                    in {
                        AttachedEventKind.CDP_CONNECTION_LOST,
                        AttachedEventKind.CDP_REQUEST_TIMEOUT,
                        AttachedEventKind.CDP_PROTOCOL_ERROR,
                        AttachedEventKind.CDP_HANDLER_ERROR,
                    }
                    else EventType.BROWSER_RUNTIME_DIAGNOSTIC
                ),
                payload={
                    "browser_attached_event": item.to_dict(),
                    "recovery_planner_owner": "M1-07C",
                    "is_recovery_plan": False,
                },
            )
            for item in self.events()
            if not item.stale
        )

    def receipt(self, *, drained: bool = False) -> EventAttachmentReceipt:
        with self._guard:
            return EventAttachmentReceipt(
                scope=self.scope,
                attachment_id=self.attachment_id,
                subscription_id=self._subscription_id,
                phase=self._phase,
                generation=self._generation,
                attached_at=self._attached_at,
                detached_at=self._detached_at,
                accepted_events=self._accepted,
                stale_events=self._stale,
                callback_failures=self._callback_failures,
                drained=drained,
                error=self._error,
            )

    def _on_event(self, raw: Any) -> None:
        with self._idle:
            self._inflight += 1
        try:
            generation = int(getattr(raw, "generation", 0) or 0)
            stale = generation != self._generation
            item = AttachedBrowserEvent(
                scope=self.scope,
                topic=str(getattr(raw, "topic", "") or ""),
                payload=dict(getattr(raw, "payload", {}) or {}),
                bus_event_id=str(getattr(raw, "event_id", "") or ""),
                bus_generation=generation,
                bus_sequence=int(getattr(raw, "sequence", 0) or 0),
                source=str(getattr(raw, "source", "browser-runtime") or "browser-runtime"),
                stale=stale,
            )
            with self._guard:
                if stale:
                    self._stale += 1
                    if self.policy.reject_stale_generation:
                        self._events.append(item)
                        return
                self._events.append(item)
                self._accepted += 1
            if self.event_sink is not None:
                self.event_sink(item)
        except Exception as exc:
            with self._guard:
                self._callback_failures += 1
                self._error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            with self._idle:
                self._inflight = max(0, self._inflight - 1)
                self._idle.notify_all()


class BrowserReconnectObserver:
    """Project 04A reconnect attempts; never owns reconnect state or policy."""

    def __init__(
        self,
        *,
        policy: EventAttachmentPolicy | None = None,
        sleeper: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.policy = policy or EventAttachmentPolicy()
        self.sleeper = sleeper or time.sleep
        self.monotonic = monotonic or time.monotonic

    def observe(
        self,
        scope: ObservationScope,
        reconnect_owner: Callable[[], Any],
        *,
        reason: str,
    ) -> ReconnectObservationReceipt:
        attempts: list[ReconnectAttemptObservation] = []
        for index in range(1, self.policy.reconnect_attempts + 1):
            delay = self._delay(index)
            if delay:
                self.sleeper(delay)
            started_at = utc_now()
            started = self.monotonic()
            try:
                result = reconnect_owner()
                ok = bool(getattr(result, "ok", result))
                generation = int(
                    getattr(getattr(result, "diagnostic", None), "event_bus_generation", 0)
                    or 0
                )
                error = str(getattr(result, "error", "") or "")
            except Exception as exc:  # noqa: BLE001 - typed owner failure is evidence.
                ok = False
                generation = 0
                error = f"{type(exc).__name__}: {exc}"
            attempts.append(
                ReconnectAttemptObservation(
                    attempt=index,
                    delay_seconds=delay,
                    started_at=started_at,
                    duration_ms=max(0, int((self.monotonic() - started) * 1000)),
                    ok=ok,
                    error=error,
                    session_generation=generation,
                )
            )
            if ok:
                return ReconnectObservationReceipt(
                    scope=scope,
                    reason=reason,
                    attempts=tuple(attempts),
                    exhausted=False,
                )
        receipt = ReconnectObservationReceipt(
            scope=scope,
            reason=reason,
            attempts=tuple(attempts),
            exhausted=True,
        )
        raise BrowserReconnectExhausted(
            f"browser reconnect exhausted after {len(attempts)} attempts",
            receipt=receipt,
        )

    def _delay(self, attempt: int) -> float:
        values = self.policy.reconnect_backoff_seconds
        if not values:
            return 0.0
        return values[min(attempt - 1, len(values) - 1)]


def reconnect_event(receipt: ReconnectObservationReceipt) -> EventRecord:
    return EventRecord(
        run_id=receipt.scope.run_id,
        task_id=receipt.scope.task_id,
        node_id=receipt.scope.node_id or None,
        event_type=EventType.WORKER_HEALTH,
        payload={
            "browser_reconnect": receipt.to_dict(),
            "recovery_planner_owner": "M1-07C",
            "is_recovery_plan": False,
        },
    )
