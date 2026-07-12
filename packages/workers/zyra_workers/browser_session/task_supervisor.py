from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Generic, TypeVar

from .errors import BrowserRuntimeError, BrowserSessionBusy
from .models import browser_id, browser_now, stable_digest


T = TypeVar("T")


class BrowserTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class BrowserTaskKey:
    session_id: str
    name: str
    generation: int
    task_id: str = field(default_factory=lambda: browser_id("brtask"))

    @property
    def fingerprint(self) -> str:
        return stable_digest({
            "session_id": self.session_id,
            "name": self.name,
            "generation": self.generation,
            "task_id": self.task_id,
        })


@dataclass(frozen=True, slots=True)
class BrowserTaskRecord:
    key: BrowserTaskKey
    status: BrowserTaskStatus
    submitted_at: str
    started_at: str = ""
    completed_at: str = ""
    error: str = ""
    result_digest: str = ""
    cancellation_reason: str = ""
    late: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.key.task_id,
            "session_id": self.key.session_id,
            "name": self.key.name,
            "generation": self.key.generation,
            "fingerprint": self.key.fingerprint,
            "status": str(self.status),
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "result_digest": self.result_digest,
            "cancellation_reason": self.cancellation_reason,
            "late": self.late,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BrowserTaskResult(Generic[T]):
    record: BrowserTaskRecord
    value: T | None = None

    @property
    def accepted(self) -> bool:
        return self.record.status == BrowserTaskStatus.COMPLETED and not self.record.late


@dataclass(frozen=True, slots=True)
class BrowserTaskSupervisorSnapshot:
    sessions: int
    tasks: int
    pending: int
    running: int
    completed: int
    failed: int
    cancelled: int
    quarantined: int
    late_results: int
    generations: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "tasks": self.tasks,
            "pending": self.pending,
            "running": self.running,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "quarantined": self.quarantined,
            "late_results": self.late_results,
            "generations": dict(self.generations),
        }


class BrowserTaskSupervisor:
    def __init__(
        self,
        *,
        max_workers: int = 16,
        history_limit: int = 4096,
        disabled: bool = False,
    ) -> None:
        if max_workers < 1:
            raise ValueError("browser task supervisor requires workers")
        self.max_workers = max_workers
        self.history_limit = max(128, history_limit)
        self.disabled = disabled
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="zyra-browser")
        self._generations: dict[str, int] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._records: dict[str, BrowserTaskRecord] = {}
        self._results: dict[str, Any] = {}
        self._session_tasks: dict[str, set[str]] = defaultdict(set)
        self._history: deque[str] = deque(maxlen=self.history_limit)
        self._quarantine: deque[BrowserTaskRecord] = deque(maxlen=self.history_limit)
        self._condition = threading.Condition(threading.RLock())
        self._closed = False
        self._late_results = 0

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserRuntimeError("browser task supervisor is disabled", code="browser_task_supervisor_disabled")
        if self._closed:
            raise BrowserRuntimeError("browser task supervisor is closed", code="browser_task_supervisor_closed")

    def begin_session(self, session_id: str) -> int:
        self._ensure_available()
        if not session_id:
            raise ValueError("browser task session id is required")
        with self._condition:
            generation = self._generations.get(session_id, 0) + 1
            self._generations[session_id] = generation
            self._condition.notify_all()
            return generation

    def current_generation(self, session_id: str) -> int:
        with self._condition:
            return self._generations.get(session_id, 0)

    def ensure_generation(self, session_id: str, generation: int) -> None:
        current = self.current_generation(session_id)
        if generation != current:
            raise BrowserSessionBusy(
                f"browser task generation is stale: expected {current}, received {generation}",
                session_id=session_id,
            )

    def submit(
        self,
        session_id: str,
        name: str,
        operation: Callable[..., T],
        *args: Any,
        generation: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> BrowserTaskKey:
        self._ensure_available()
        with self._condition:
            current = self._generations.get(session_id, 0)
            if current == 0:
                current = self.begin_session(session_id)
            chosen = current if generation is None else generation
            if chosen != current:
                raise BrowserSessionBusy("cannot submit task to stale browser generation", session_id=session_id)
            key = BrowserTaskKey(session_id=session_id, name=name, generation=chosen)
            record = BrowserTaskRecord(
                key=key,
                status=BrowserTaskStatus.PENDING,
                submitted_at=browser_now(),
                metadata=dict(metadata or {}),
            )
            self._records[key.task_id] = record
            self._session_tasks[session_id].add(key.task_id)
            self._history.append(key.task_id)
            future = self._executor.submit(self._execute, key, operation, args, kwargs)
            self._futures[key.task_id] = future
            future.add_done_callback(lambda completed, task_id=key.task_id: self._settle(task_id, completed))
            return key

    def _execute(
        self,
        key: BrowserTaskKey,
        operation: Callable[..., T],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> T:
        with self._condition:
            record = self._records[key.task_id]
            current = self._generations.get(key.session_id, 0)
            if current != key.generation:
                self._records[key.task_id] = replace(
                    record,
                    status=BrowserTaskStatus.QUARANTINED,
                    completed_at=browser_now(),
                    error="task generation became stale before execution",
                    late=True,
                )
                raise BrowserSessionBusy("browser task generation became stale", session_id=key.session_id)
            self._records[key.task_id] = replace(
                record,
                status=BrowserTaskStatus.RUNNING,
                started_at=browser_now(),
            )
            self._condition.notify_all()
        return operation(*args, **kwargs)

    def _settle(self, task_id: str, future: Future[Any]) -> None:
        with self._condition:
            record = self._records.get(task_id)
            if record is None:
                return
            current = self._generations.get(record.key.session_id, 0)
            late = current != record.key.generation
            try:
                value = future.result()
            except CancelledError:
                settled = replace(
                    record,
                    status=BrowserTaskStatus.CANCELLED,
                    completed_at=browser_now(),
                    cancellation_reason=record.cancellation_reason or "future_cancelled",
                    late=late,
                )
            except Exception as error:
                status = BrowserTaskStatus.QUARANTINED if late else BrowserTaskStatus.FAILED
                settled = replace(
                    record,
                    status=status,
                    completed_at=browser_now(),
                    error=f"{type(error).__name__}: {error}",
                    late=late,
                )
            else:
                status = BrowserTaskStatus.QUARANTINED if late else BrowserTaskStatus.COMPLETED
                settled = replace(
                    record,
                    status=status,
                    completed_at=browser_now(),
                    result_digest=stable_digest(value if isinstance(value, (dict, list, tuple, str)) else str(value)),
                    late=late,
                )
                if not late:
                    self._results[task_id] = value
            self._records[task_id] = settled
            self._futures.pop(task_id, None)
            if settled.status == BrowserTaskStatus.QUARANTINED:
                self._late_results += 1
                self._quarantine.append(settled)
            self._condition.notify_all()

    def result(self, key: BrowserTaskKey, *, timeout: float | None = None) -> BrowserTaskResult[Any]:
        self._ensure_available()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                record = self._records.get(key.task_id)
                if record is None:
                    raise KeyError(key.task_id)
                if record.status not in {BrowserTaskStatus.PENDING, BrowserTaskStatus.RUNNING}:
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(f"browser task {key.name} did not settle")
                self._condition.wait(remaining)
            value = self._results.get(key.task_id)
            result = BrowserTaskResult(record=record, value=value)
        if record.status == BrowserTaskStatus.FAILED:
            raise BrowserRuntimeError(
                f"browser task {key.name} failed: {record.error}",
                session_id=key.session_id,
                operation=key.name,
                code="browser_supervised_task_failed",
            )
        if record.status == BrowserTaskStatus.QUARANTINED:
            raise BrowserSessionBusy("browser task result was quarantined as late", session_id=key.session_id)
        if record.status == BrowserTaskStatus.CANCELLED:
            raise BrowserRuntimeError(
                f"browser task {key.name} was cancelled",
                session_id=key.session_id,
                code="browser_supervised_task_cancelled",
            )
        return result

    def run_guarded(
        self,
        session_id: str,
        name: str,
        operation: Callable[..., T],
        *args: Any,
        generation: int | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> T:
        key = self.submit(
            session_id,
            name,
            operation,
            *args,
            generation=generation,
            metadata=metadata,
            **kwargs,
        )
        result = self.result(key, timeout=timeout)
        return result.value  # type: ignore[return-value]

    def cancel(self, task_id: str, *, reason: str = "cancelled") -> bool:
        with self._condition:
            record = self._records.get(task_id)
            if record is None or record.status not in {BrowserTaskStatus.PENDING, BrowserTaskStatus.RUNNING}:
                return False
            self._records[task_id] = replace(record, cancellation_reason=reason)
            future = self._futures.get(task_id)
            cancelled = future.cancel() if future is not None else False
            if cancelled:
                self._condition.notify_all()
            return cancelled

    def cancel_session(
        self,
        session_id: str,
        *,
        reason: str = "session_generation_advanced",
        advance_generation: bool = True,
    ) -> tuple[str, ...]:
        with self._condition:
            if advance_generation:
                self._generations[session_id] = self._generations.get(session_id, 0) + 1
            task_ids = tuple(self._session_tasks.get(session_id, ()))
        cancelled: list[str] = []
        for task_id in task_ids:
            if self.cancel(task_id, reason=reason):
                cancelled.append(task_id)
        return tuple(cancelled)

    def drain(self, session_id: str, *, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                task_ids = self._session_tasks.get(session_id, set())
                unsettled = [
                    task_id for task_id in task_ids
                    if self._records.get(task_id) is not None
                    and self._records[task_id].status in {BrowserTaskStatus.PENDING, BrowserTaskStatus.RUNNING}
                ]
                if not unsettled:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    def records(self, *, session_id: str = "", status: BrowserTaskStatus | None = None) -> tuple[BrowserTaskRecord, ...]:
        with self._condition:
            values = tuple(self._records.values())
        if session_id:
            values = tuple(item for item in values if item.key.session_id == session_id)
        if status is not None:
            values = tuple(item for item in values if item.status == status)
        return tuple(sorted(values, key=lambda item: (item.submitted_at, item.key.task_id)))

    def quarantined(self, *, session_id: str = "") -> tuple[BrowserTaskRecord, ...]:
        with self._condition:
            values = tuple(self._quarantine)
        if session_id:
            values = tuple(item for item in values if item.key.session_id == session_id)
        return values

    def prune(self, *, keep_per_session: int = 256) -> int:
        removed = 0
        with self._condition:
            for session_id, task_ids in tuple(self._session_tasks.items()):
                ordered = sorted(
                    task_ids,
                    key=lambda task_id: self._records[task_id].submitted_at,
                    reverse=True,
                )
                for task_id in ordered[keep_per_session:]:
                    record = self._records.get(task_id)
                    if record and record.status not in {BrowserTaskStatus.PENDING, BrowserTaskStatus.RUNNING}:
                        self._records.pop(task_id, None)
                        self._results.pop(task_id, None)
                        task_ids.discard(task_id)
                        removed += 1
                if not task_ids:
                    self._session_tasks.pop(session_id, None)
        return removed

    def snapshot(self) -> BrowserTaskSupervisorSnapshot:
        with self._condition:
            records = tuple(self._records.values())
            counts = {status: sum(1 for item in records if item.status == status) for status in BrowserTaskStatus}
            return BrowserTaskSupervisorSnapshot(
                sessions=len(self._generations),
                tasks=len(records),
                pending=counts[BrowserTaskStatus.PENDING],
                running=counts[BrowserTaskStatus.RUNNING],
                completed=counts[BrowserTaskStatus.COMPLETED],
                failed=counts[BrowserTaskStatus.FAILED],
                cancelled=counts[BrowserTaskStatus.CANCELLED],
                quarantined=counts[BrowserTaskStatus.QUARANTINED],
                late_results=self._late_results,
                generations=dict(self._generations),
            )

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = True) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._generations)
        if cancel_futures:
            for session_id in sessions:
                self.cancel_session(session_id, reason="supervisor_shutdown", advance_generation=True)
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
