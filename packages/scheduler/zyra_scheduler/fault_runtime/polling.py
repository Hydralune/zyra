from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import utc_now


class PollSourceStatus(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    BACKOFF = "backoff"
    DISABLED = "disabled"
    FAILED = "failed"


@dataclass(slots=True)
class PollSource:
    source_id: str
    callback: Callable[[], Mapping[str, Any]]
    interval_ms: int
    max_failures: int
    status: PollSourceStatus = PollSourceStatus.REGISTERED
    revision: int = 1
    next_due_ms: int = 0
    run_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_started_at: str = ""
    last_completed_at: str = ""
    last_duration_ms: int = 0
    last_error: str = ""
    last_result: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("poll source id must not be empty")
        if self.interval_ms < 1 or self.max_failures < 1:
            raise ValueError("poll source interval and max failures must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "interval_ms": self.interval_ms,
            "max_failures": self.max_failures,
            "status": self.status.value,
            "revision": self.revision,
            "next_due_ms": self.next_due_ms,
            "run_count": self.run_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "consecutive_failures": self.consecutive_failures,
            "last_started_at": self.last_started_at,
            "last_completed_at": self.last_completed_at,
            "last_duration_ms": self.last_duration_ms,
            "last_error": self.last_error,
            "last_result": dict(self.last_result),
        }


class WatchdogPollingCoordinator:
    """Cooperative polling loop for deadline, heartbeat, and process sources.

    No thread is started implicitly. API or worker composition must explicitly
    call ``start``; tests and deterministic runtimes may instead call
    ``run_due`` with a controlled monotonic clock.
    """

    def __init__(
        self,
        *,
        monotonic_ms: Callable[[], int] | None = None,
        idle_wait_ms: int = 25,
    ) -> None:
        if idle_wait_ms < 1 or idle_wait_ms > 5_000:
            raise ValueError("polling idle wait must be in range 1..5000ms")
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self.idle_wait_ms = idle_wait_ms
        self._guard = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sources: dict[str, PollSource] = {}
        self._cycles = 0
        self._started_at = ""
        self._stopped_at = ""

    def register(
        self,
        source_id: str,
        callback: Callable[[], Mapping[str, Any]],
        *,
        interval_ms: int,
        max_failures: int = 3,
    ) -> Mapping[str, Any]:
        if not callable(callback):
            raise TypeError("poll source callback must be callable")
        with self._guard:
            existing = self._sources.get(source_id)
            if existing is not None:
                if existing.callback is not callback:
                    raise RuntimeError(f"poll source already registered: {source_id}")
                return existing.to_dict()
            source = PollSource(
                source_id=source_id,
                callback=callback,
                interval_ms=int(interval_ms),
                max_failures=int(max_failures),
                next_due_ms=self.monotonic_ms(),
            )
            self._sources[source_id] = source
            self._wake.set()
            return source.to_dict()

    def enable(self, source_id: str, *, immediate: bool = True) -> Mapping[str, Any]:
        with self._guard:
            source = self._require(source_id)
            source.status = PollSourceStatus.RUNNING
            source.revision += 1
            source.consecutive_failures = 0
            source.last_error = ""
            if immediate:
                source.next_due_ms = self.monotonic_ms()
            self._wake.set()
            return source.to_dict()

    def disable(self, source_id: str) -> Mapping[str, Any]:
        with self._guard:
            source = self._require(source_id)
            source.status = PollSourceStatus.DISABLED
            source.revision += 1
            self._wake.set()
            return source.to_dict()

    def update_interval(self, source_id: str, interval_ms: int) -> Mapping[str, Any]:
        if interval_ms < 1:
            raise ValueError("poll interval must be positive")
        with self._guard:
            source = self._require(source_id)
            source.interval_ms = int(interval_ms)
            source.revision += 1
            source.next_due_ms = min(source.next_due_ms, self.monotonic_ms() + source.interval_ms)
            self._wake.set()
            return source.to_dict()

    def run_due(self, *, at_ms: int | None = None) -> tuple[Mapping[str, Any], ...]:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        with self._guard:
            due = tuple(
                item
                for item in sorted(self._sources.values(), key=lambda value: value.source_id)
                if item.status in {PollSourceStatus.REGISTERED, PollSourceStatus.RUNNING, PollSourceStatus.BACKOFF}
                and item.next_due_ms <= now
            )
        receipts: list[Mapping[str, Any]] = []
        for source in due:
            receipts.append(self._run_source(source, at_ms=now))
        with self._guard:
            self._cycles += 1
        return tuple(receipts)

    def start(self, *, thread_name: str = "zyra-runtime-watchdog") -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._started_at = utc_now()
            self._stopped_at = ""
            self._thread = threading.Thread(target=self._loop, name=thread_name, daemon=True)
            self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
        with self._guard:
            thread = self._thread
        if thread is None:
            return True
        self._stop.set()
        self._wake.set()
        thread.join(timeout=max(0.0, timeout_seconds))
        stopped = not thread.is_alive()
        if stopped:
            with self._guard:
                self._stopped_at = utc_now()
                self._thread = None
        return stopped

    def snapshot(self) -> Mapping[str, Any]:
        with self._guard:
            return {
                "schema": "zyra.watchdog-polling-coordinator/v1",
                "thread_running": self._thread is not None and self._thread.is_alive(),
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "cycles": self._cycles,
                "idle_wait_ms": self.idle_wait_ms,
                "sources": {
                    key: value.to_dict()
                    for key, value in sorted(self._sources.items())
                },
                "implicit_thread_start": False,
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self.monotonic_ms()
            self.run_due(at_ms=now)
            delay = self._next_delay(now)
            self._wake.wait(delay / 1_000)
            self._wake.clear()

    def _next_delay(self, now: int) -> int:
        with self._guard:
            due = [
                max(0, item.next_due_ms - now)
                for item in self._sources.values()
                if item.status in {PollSourceStatus.REGISTERED, PollSourceStatus.RUNNING, PollSourceStatus.BACKOFF}
            ]
        return self.idle_wait_ms if not due else max(1, min(self.idle_wait_ms, min(due)))

    def _run_source(self, source: PollSource, *, at_ms: int) -> Mapping[str, Any]:
        started = self.monotonic_ms()
        with self._guard:
            if source.status is PollSourceStatus.REGISTERED:
                source.status = PollSourceStatus.RUNNING
            source.run_count += 1
            source.last_started_at = utc_now()
        try:
            result = dict(source.callback())
        except Exception as error:
            completed = self.monotonic_ms()
            with self._guard:
                source.failure_count += 1
                source.consecutive_failures += 1
                source.last_error = f"{type(error).__name__}: {error}"
                source.last_duration_ms = max(0, completed - started)
                source.last_completed_at = utc_now()
                source.revision += 1
                if source.consecutive_failures >= source.max_failures:
                    source.status = PollSourceStatus.FAILED
                    source.next_due_ms = 2**63 - 1
                else:
                    source.status = PollSourceStatus.BACKOFF
                    delay = min(source.interval_ms * (2**source.consecutive_failures), 60_000)
                    source.next_due_ms = at_ms + delay
                return {
                    "source_id": source.source_id,
                    "ok": False,
                    "error": source.last_error,
                    "status": source.status.value,
                    "next_due_ms": source.next_due_ms,
                }
        completed = self.monotonic_ms()
        with self._guard:
            source.success_count += 1
            source.consecutive_failures = 0
            source.status = PollSourceStatus.RUNNING
            source.last_error = ""
            source.last_result = result
            source.last_duration_ms = max(0, completed - started)
            source.last_completed_at = utc_now()
            source.next_due_ms = at_ms + source.interval_ms
            source.revision += 1
            return {
                "source_id": source.source_id,
                "ok": True,
                "result": dict(result),
                "status": source.status.value,
                "next_due_ms": source.next_due_ms,
            }

    def _require(self, source_id: str) -> PollSource:
        source = self._sources.get(source_id)
        if source is None:
            raise KeyError(source_id)
        return source


__all__ = ["PollSource", "PollSourceStatus", "WatchdogPollingCoordinator"]
