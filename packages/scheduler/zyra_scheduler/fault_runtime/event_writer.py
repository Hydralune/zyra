from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, TaskState, to_jsonable

from ..worker_pool.health_bridge import WorkerRouteHealthSignal
from ..worker_pool.integration_models import RouteHealth
from .contracts import (
    FaultDisposition,
    FaultKind,
    FaultSeverity,
    FaultSignal,
    ProjectionReceipt,
    SignalOrigin,
    runtime_id,
    utc_now,
)
from .state_store import FaultStateStore


class MemoryWriter(Protocol):
    def refresh_task_memory(
        self,
        state: TaskState,
        events: Sequence[Mapping[str, Any]],
        *,
        persist: bool = True,
    ) -> Any: ...


class SchedulerHealthWriter(Protocol):
    def record_worker_health(self, signal: WorkerRouteHealthSignal) -> Any: ...


EventSink = Callable[[EventRecord], None]


@dataclass(slots=True)
class TaskProjection:
    task_id: str
    run_id: str
    revision: int = 0
    active_signal_ids: list[str] = field(default_factory=list)
    blocked_signal_ids: list[str] = field(default_factory=list)
    retryable_signal_ids: list[str] = field(default_factory=list)
    last_signal_id: str = ""
    last_fault_kind: str = ""
    last_event_id: str = ""
    updated_at: str = field(default_factory=utc_now)

    def apply(self, signal: FaultSignal, event_id: str) -> bool:
        if signal.signal_id in self.active_signal_ids:
            return False
        self.revision += 1
        self.active_signal_ids.append(signal.signal_id)
        if signal.disposition in {FaultDisposition.BLOCK, FaultDisposition.RECOVER}:
            self.blocked_signal_ids.append(signal.signal_id)
        if signal.retryable:
            self.retryable_signal_ids.append(signal.signal_id)
        self.last_signal_id = signal.signal_id
        self.last_fault_kind = signal.kind.value
        self.last_event_id = event_id
        self.updated_at = utc_now()
        return True

    def resolve(self, signal_id: str) -> bool:
        if signal_id not in self.active_signal_ids:
            return False
        self.revision += 1
        self.active_signal_ids.remove(signal_id)
        if signal_id in self.blocked_signal_ids:
            self.blocked_signal_ids.remove(signal_id)
        if signal_id in self.retryable_signal_ids:
            self.retryable_signal_ids.remove(signal_id)
        self.updated_at = utc_now()
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.fault-task-projection/v1",
            "task_id": self.task_id,
            "run_id": self.run_id,
            "revision": self.revision,
            "active_signal_ids": list(self.active_signal_ids),
            "blocked_signal_ids": list(self.blocked_signal_ids),
            "retryable_signal_ids": list(self.retryable_signal_ids),
            "last_signal_id": self.last_signal_id,
            "last_fault_kind": self.last_fault_kind,
            "last_event_id": self.last_event_id,
            "updated_at": self.updated_at,
        }


def _event_type(signal: FaultSignal) -> EventType:
    if signal.origin is SignalOrigin.INJECTION:
        return EventType.FAILURE_INJECTED
    return EventType.WORKER_HEALTH


def _route_health(signal: FaultSignal) -> RouteHealth:
    if signal.severity is FaultSeverity.WARNING:
        return RouteHealth.DEGRADED
    if signal.severity in {FaultSeverity.ERROR, FaultSeverity.CRITICAL}:
        return RouteHealth.UNAVAILABLE
    return RouteHealth.HEALTHY


def _route_disposition(signal: FaultSignal) -> str:
    if signal.disposition is FaultDisposition.RECOVER:
        return "replan"
    if signal.disposition is FaultDisposition.BLOCK:
        return "expire_lease"
    if signal.disposition is FaultDisposition.DEGRADE:
        return "observe"
    return "none"


class FaultSignalEventWriter:
    """Projects a classified fault into existing canonical owners.

    The writer owns only projection receipts and a small task-facing index.
    Event history remains in the canonical runtime store, memory remains in
    MemoryFabric, and route health remains in M1-S05D BackendRegistryStore.
    """

    def __init__(
        self,
        store: FaultStateStore,
        *,
        event_sink: EventSink,
        memory: MemoryWriter | None = None,
        scheduler_health: SchedulerHealthWriter | None = None,
        task_state_resolver: Callable[[str], TaskState | None] | None = None,
    ) -> None:
        if not callable(event_sink):
            raise TypeError("FaultSignalEventWriter requires a canonical event sink")
        self.store = store
        self.event_sink = event_sink
        self.memory = memory
        self.scheduler_health = scheduler_health
        self.task_state_resolver = task_state_resolver
        self._lock = threading.RLock()
        self._projections: dict[str, TaskProjection] = {}

    def write(self, signal: FaultSignal, *, task_state: TaskState | None = None) -> ProjectionReceipt:
        prior = self.store.projection_receipt(signal.signal_id)
        if prior is not None:
            return prior
        if task_state is None and self.task_state_resolver is not None:
            task_state = self.task_state_resolver(signal.refs.task_id)
        event = self._event(signal)
        event_errors: list[str] = []
        self._deliver_event(signal, event, event_errors)
        if event_errors:
            receipt = ProjectionReceipt(
                signal_id=signal.signal_id,
                event_id=event.event_id,
                canonical_event_written=False,
                memory_record_ids=(),
                scheduler_changed=False,
                scheduler_receipt={},
                task_projection_changed=False,
                errors=tuple(event_errors),
            )
            self.store.append_projection_receipt(receipt)
            return receipt

        memory_ids, memory_errors = self._deliver_memory(signal, event, task_state)
        scheduler_changed, scheduler_receipt, scheduler_errors = self._deliver_scheduler(signal)
        projection_changed = self._project_task(signal, event, task_state)
        receipt = ProjectionReceipt(
            signal_id=signal.signal_id,
            event_id=event.event_id,
            canonical_event_written=True,
            memory_record_ids=tuple(memory_ids),
            scheduler_changed=scheduler_changed,
            scheduler_receipt=scheduler_receipt,
            task_projection_changed=projection_changed,
            errors=tuple(memory_errors + scheduler_errors),
        )
        self.store.append_projection_receipt(receipt)
        return receipt

    def retry_incomplete(self, signal: FaultSignal, *, task_state: TaskState | None = None) -> ProjectionReceipt:
        prior = self.store.projection_receipt(signal.signal_id)
        if prior is None or prior.ok:
            return self.write(signal, task_state=task_state) if prior is None else prior
        # A failed canonical event write is retried with the stable prior event id.
        event = self._event(signal, event_id=prior.event_id)
        errors: list[str] = []
        self._deliver_event(signal, event, errors)
        if errors:
            return prior
        memory_ids, memory_errors = self._deliver_memory(signal, event, task_state)
        scheduler_changed, scheduler_receipt, scheduler_errors = self._deliver_scheduler(signal)
        projection_changed = self._project_task(signal, event, task_state)
        replacement = ProjectionReceipt(
            signal_id=signal.signal_id,
            event_id=event.event_id,
            canonical_event_written=True,
            memory_record_ids=tuple(memory_ids),
            scheduler_changed=scheduler_changed,
            scheduler_receipt=scheduler_receipt,
            task_projection_changed=projection_changed,
            errors=tuple(memory_errors + scheduler_errors),
        )
        # The state store preserves first-write idempotency. Record the retry as
        # delivery evidence and return the successful in-memory receipt.
        self.store.record_delivery_attempt(
            signal_id=signal.signal_id,
            sink="projection-retry",
            ok=replacement.ok,
            receipt=replacement.to_dict(),
            error="; ".join(replacement.errors),
        )
        return replacement

    def task_projection(self, task_id: str) -> Mapping[str, Any]:
        with self._lock:
            projection = self._projections.get(task_id)
            if projection is None:
                return {
                    "schema": "zyra.fault-task-projection/v1",
                    "task_id": task_id,
                    "revision": 0,
                    "active_signal_ids": [],
                    "blocked_signal_ids": [],
                    "retryable_signal_ids": [],
                }
            return projection.to_dict()

    def resolve(self, task_id: str, signal_id: str) -> bool:
        with self._lock:
            projection = self._projections.get(task_id)
            changed = False if projection is None else projection.resolve(signal_id)
            if changed:
                self.store.put_metadata(
                    f"task-projection:{task_id}",
                    projection.to_dict(),
                    expected_revision=self._metadata_revision(f"task-projection:{task_id}"),
                )
            return changed

    def restore_projections(self) -> int:
        restored = 0
        for signal in self.store.signals(limit=10_000):
            receipt = self.store.projection_receipt(signal.signal_id)
            if receipt is None or not receipt.canonical_event_written:
                continue
            with self._lock:
                projection = self._projections.setdefault(
                    signal.refs.task_id,
                    TaskProjection(task_id=signal.refs.task_id, run_id=signal.refs.run_id),
                )
                if projection.apply(signal, receipt.event_id):
                    restored += 1
        return restored

    def _event(self, signal: FaultSignal, *, event_id: str = "") -> EventRecord:
        payload = {
            "schema": "zyra.watchdog-fault-event/v1",
            "signal": signal.to_dict(),
            "fault_kind": signal.kind.value,
            "fault_severity": signal.severity.value,
            "fault_disposition": signal.disposition.value,
            "signal_origin": signal.origin.value,
            "observer_id": signal.provenance.observer_id,
            "injection_id": signal.provenance.injection_id,
            "explicit_refs": signal.refs.to_dict(),
            "recoverable": signal.retryable,
            "terminal": signal.terminal,
            "requirement_changed": False,
            "canonical_fault_owner": "python.FaultStateStore",
        }
        return EventRecord(
            run_id=signal.refs.run_id,
            task_id=signal.refs.task_id,
            node_id=signal.refs.node_id or None,
            event_type=_event_type(signal),
            event_id=event_id or runtime_id("faultevent"),
            payload=payload,
        )

    def _deliver_event(self, signal: FaultSignal, event: EventRecord, errors: list[str]) -> None:
        try:
            self.event_sink(event)
            self.store.record_delivery_attempt(
                signal_id=signal.signal_id,
                sink="canonical-event",
                ok=True,
                receipt={"event_id": event.event_id, "event_type": event.event_type.value},
            )
        except Exception as error:
            message = f"canonical-event:{type(error).__name__}:{error}"
            errors.append(message)
            self.store.record_delivery_attempt(
                signal_id=signal.signal_id,
                sink="canonical-event",
                ok=False,
                error=message,
            )

    def _deliver_memory(
        self,
        signal: FaultSignal,
        event: EventRecord,
        task_state: TaskState | None,
    ) -> tuple[list[str], list[str]]:
        if self.memory is None or task_state is None:
            return [], []
        try:
            snapshot = self.memory.refresh_task_memory(task_state, [to_jsonable(event)], persist=True)
            ids = [str(record.memory_id) for record in getattr(snapshot, "records", ())]
            self.store.record_delivery_attempt(
                signal_id=signal.signal_id,
                sink="memory-fabric",
                ok=True,
                receipt={"memory_record_ids": ids},
            )
            return ids, []
        except Exception as error:
            message = f"memory-fabric:{type(error).__name__}:{error}"
            self.store.record_delivery_attempt(
                signal_id=signal.signal_id,
                sink="memory-fabric",
                ok=False,
                error=message,
            )
            return [], [message]

    def _deliver_scheduler(self, signal: FaultSignal) -> tuple[bool, Mapping[str, Any], list[str]]:
        if self.scheduler_health is None or not signal.refs.backend_id:
            return False, {}, []
        worker_ids = (signal.refs.worker_id,) if signal.refs.worker_id else ()
        route_signal = WorkerRouteHealthSignal(
            signal_id=signal.signal_id,
            route_id=signal.refs.backend_id,
            status=_route_health(signal),
            disposition=_route_disposition(signal),
            reason=signal.summary,
            worker_ids=worker_ids,
            active_lease_ids=(),
            task_ids=(signal.refs.task_id,),
            run_ids=(signal.refs.run_id,),
            worker_generations={signal.refs.worker_id: signal.refs.source_state_revision} if signal.refs.worker_id else {},
            source_statuses={signal.refs.worker_id: signal.severity.value} if signal.refs.worker_id else {},
            observed_at=signal.created_at,
            metadata={
                "source": "M1-S07B-01.FaultSignalEventWriter",
                "fault_kind": signal.kind.value,
                "origin": signal.origin.value,
                "injection_id": signal.provenance.injection_id,
                "state_owner": "python.BackendRegistryStore",
            },
        )
        try:
            receipt = self.scheduler_health.record_worker_health(route_signal)
            value = receipt.to_dict() if hasattr(receipt, "to_dict") else to_jsonable(receipt)
            changed = bool(value.get("changed", False)) if isinstance(value, Mapping) else False
            accepted = bool(value.get("accepted", True)) if isinstance(value, Mapping) else True
            self.store.record_delivery_attempt(
                signal_id=signal.signal_id,
                sink="scheduler-health",
                ok=accepted,
                receipt=dict(value) if isinstance(value, Mapping) else {"value": value},
                error="" if accepted else str(value.get("reason", "scheduler rejected health signal")),
            )
            errors = [] if accepted else [str(value.get("reason", "scheduler rejected health signal"))]
            return changed, dict(value) if isinstance(value, Mapping) else {"value": value}, errors
        except Exception as error:
            message = f"scheduler-health:{type(error).__name__}:{error}"
            self.store.record_delivery_attempt(
                signal_id=signal.signal_id,
                sink="scheduler-health",
                ok=False,
                error=message,
            )
            return False, {}, [message]

    def _project_task(self, signal: FaultSignal, event: EventRecord, task_state: TaskState | None) -> bool:
        with self._lock:
            projection = self._projections.setdefault(
                signal.refs.task_id,
                TaskProjection(task_id=signal.refs.task_id, run_id=signal.refs.run_id),
            )
            changed = projection.apply(signal, event.event_id)
            if not changed:
                return False
            key = f"task-projection:{signal.refs.task_id}"
            self.store.put_metadata(
                key,
                projection.to_dict(),
                expected_revision=self._metadata_revision(key),
            )
            if task_state is not None:
                fault_state = copy.deepcopy(dict(task_state.metadata.get("fault_runtime") or {}))
                fault_state.update(projection.to_dict())
                task_state.metadata["fault_runtime"] = fault_state
            return True

    def _metadata_revision(self, key: str) -> int:
        current = self.store.metadata(key)
        return 0 if current is None else current[1]


def event_writer_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.fault-event-writer-contract/v1",
        "input": "FaultSignal",
        "canonical_event": {
            "injected": EventType.FAILURE_INJECTED.value,
            "observed": EventType.WORKER_HEALTH.value,
        },
        "projections": {
            "memory": "python.MemoryFabric",
            "scheduler": "python.BackendRegistryStore via BackendRegistryHealthAdapter",
            "task": "TaskState.metadata[fault_runtime]",
            "receipts": "python.FaultStateStore",
        },
        "free_text_identity_inference": False,
        "requirement_changed_is_fault": False,
    }


__all__ = [
    "EventSink",
    "FaultSignalEventWriter",
    "MemoryWriter",
    "SchedulerHealthWriter",
    "TaskProjection",
    "event_writer_contract",
]
