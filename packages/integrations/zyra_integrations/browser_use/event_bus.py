from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class EventBusState(StrEnum):
    NEW = "new"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class BrowserEvent:
    topic: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: f"bevt_{uuid4().hex[:16]}")
    source: str = "browser-runtime"
    generation: int = 0
    sequence: int = 0
    created_monotonic: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not self.topic:
            raise ValueError("browser event topic is required")
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class BrowserEventDelivery:
    event_id: str
    subscription_id: str
    accepted: bool
    attempt: int
    error: str = ""


@dataclass(frozen=True, slots=True)
class BrowserSubscription:
    subscription_id: str
    topic_prefix: str
    callback: Callable[[BrowserEvent], None]
    max_failures: int = 3


@dataclass(frozen=True, slots=True)
class BrowserEventBusSnapshot:
    state: EventBusState
    generation: int
    sequence: int
    queued: int
    published: int
    delivered: int
    dropped: int
    callback_failures: int
    subscriptions: int
    recent_event_ids: tuple[str, ...]


class BrowserEventBus:
    """Restartable bounded event bus with subscriber failure isolation."""

    _STOP = object()

    def __init__(self, *, capacity: int = 2048, recent_capacity: int = 128) -> None:
        if capacity < 1 or recent_capacity < 1:
            raise ValueError("event bus capacities must be positive")
        self._capacity = capacity
        self._queue: queue.Queue[BrowserEvent | object] = queue.Queue(maxsize=capacity)
        self._recent: deque[str] = deque(maxlen=recent_capacity)
        self._subscriptions: dict[str, BrowserSubscription] = {}
        self._subscription_failures: dict[str, int] = {}
        self._state = EventBusState.NEW
        self._generation = 0
        self._sequence = 0
        self._published = 0
        self._delivered = 0
        self._dropped = 0
        self._callback_failures = 0
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def start(self) -> int:
        with self._lock:
            if self._state == EventBusState.RUNNING:
                return self._generation
            self._generation += 1
            self._state = EventBusState.RUNNING
            self._queue = queue.Queue(maxsize=self._capacity)
            self._thread = threading.Thread(
                target=self._dispatch_loop,
                name=f"zyra-browser-event-bus-{self._generation}",
                daemon=True,
            )
            self._thread.start()
            return self._generation

    def stop(self, *, drain: bool = True, timeout: float = 5.0) -> None:
        with self._lock:
            if self._state in {EventBusState.NEW, EventBusState.STOPPED}:
                self._state = EventBusState.STOPPED
                return
            self._state = EventBusState.STOPPING
            thread = self._thread
        if drain:
            deadline = time.monotonic() + max(timeout, 0.0)
            while self._queue.unfinished_tasks and time.monotonic() < deadline:
                time.sleep(0.005)
        try:
            self._queue.put_nowait(self._STOP)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
            self._queue.put_nowait(self._STOP)
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(timeout, 0.0))
        with self._lock:
            self._state = EventBusState.STOPPED
            self._thread = None

    def restart(self, *, timeout: float = 5.0) -> int:
        self.stop(drain=True, timeout=timeout)
        return self.start()

    def subscribe(
        self,
        callback: Callable[[BrowserEvent], None],
        *,
        topic_prefix: str = "",
        max_failures: int = 3,
        subscription_id: str = "",
    ) -> BrowserSubscription:
        if max_failures < 1:
            raise ValueError("max_failures must be positive")
        subscription = BrowserSubscription(
            subscription_id=subscription_id or f"bsub_{uuid4().hex[:16]}",
            topic_prefix=topic_prefix,
            callback=callback,
            max_failures=max_failures,
        )
        with self._lock:
            self._subscriptions[subscription.subscription_id] = subscription
            self._subscription_failures[subscription.subscription_id] = 0
        return subscription

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            removed = self._subscriptions.pop(subscription_id, None)
            self._subscription_failures.pop(subscription_id, None)
            return removed is not None

    def publish(
        self,
        topic: str,
        payload: Mapping[str, Any] | None = None,
        *,
        source: str = "browser-runtime",
        block: bool = False,
        timeout: float | None = None,
    ) -> BrowserEvent:
        with self._lock:
            if self._state != EventBusState.RUNNING:
                raise RuntimeError("browser event bus is not running")
            self._sequence += 1
            event = BrowserEvent(
                topic=topic,
                payload=dict(payload or {}),
                source=source,
                generation=self._generation,
                sequence=self._sequence,
            )
        try:
            self._queue.put(event, block=block, timeout=timeout if block else None)
        except queue.Full as error:
            with self._lock:
                self._dropped += 1
            raise RuntimeError("browser event bus capacity exceeded") from error
        with self._lock:
            self._published += 1
            self._recent.append(event.event_id)
        return event

    def snapshot(self) -> BrowserEventBusSnapshot:
        with self._lock:
            return BrowserEventBusSnapshot(
                state=self._state,
                generation=self._generation,
                sequence=self._sequence,
                queued=self._queue.qsize(),
                published=self._published,
                delivered=self._delivered,
                dropped=self._dropped,
                callback_failures=self._callback_failures,
                subscriptions=len(self._subscriptions),
                recent_event_ids=tuple(self._recent),
            )

    def _dispatch_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                assert isinstance(item, BrowserEvent)
                self._deliver(item)
            finally:
                self._queue.task_done()

    def _deliver(self, event: BrowserEvent) -> list[BrowserEventDelivery]:
        with self._lock:
            subscriptions = tuple(self._subscriptions.values())
        deliveries: list[BrowserEventDelivery] = []
        for subscription in subscriptions:
            if subscription.topic_prefix and not event.topic.startswith(subscription.topic_prefix):
                continue
            with self._lock:
                attempt = self._subscription_failures.get(subscription.subscription_id, 0) + 1
            try:
                subscription.callback(event)
            except Exception as error:  # subscriber failures must not stop CDP ingestion
                with self._lock:
                    failures = self._subscription_failures.get(subscription.subscription_id, 0) + 1
                    self._subscription_failures[subscription.subscription_id] = failures
                    self._callback_failures += 1
                    if failures >= subscription.max_failures:
                        self._subscriptions.pop(subscription.subscription_id, None)
                deliveries.append(
                    BrowserEventDelivery(
                        event_id=event.event_id,
                        subscription_id=subscription.subscription_id,
                        accepted=False,
                        attempt=attempt,
                        error=type(error).__name__,
                    )
                )
                continue
            with self._lock:
                self._subscription_failures[subscription.subscription_id] = 0
                self._delivered += 1
            deliveries.append(
                BrowserEventDelivery(
                    event_id=event.event_id,
                    subscription_id=subscription.subscription_id,
                    accepted=True,
                    attempt=attempt,
                )
            )
        return deliveries
