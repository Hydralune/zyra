from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import TaskState

from .contracts import FaultDisposition, FaultSignal, ProjectionReceipt, runtime_id, utc_now
from .event_writer import FaultSignalEventWriter
from .recovery_bridge import WatchdogRecoveryBridge
from .state_store import FaultStateStore


class SignalEffectPhase(StrEnum):
    DISCOVERED = "discovered"
    PROJECTING = "projecting"
    PROJECTED = "projected"
    HANDOFF_READY = "handoff_ready"
    CONTINUABLE = "continuable"
    BLOCKED = "blocked"
    RESOLVED = "resolved"


@dataclass(slots=True)
class SignalEffectState:
    signal_id: str
    task_id: str
    run_id: str
    phase: SignalEffectPhase
    revision: int = 1
    event_id: str = ""
    memory_record_ids: list[str] = field(default_factory=list)
    scheduler_changed: bool = False
    scheduler_receipt: dict[str, Any] = field(default_factory=dict)
    task_projection_changed: bool = False
    handoff_id: str = ""
    projection_attempts: int = 0
    last_error: str = ""
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "phase": self.phase.value,
            "revision": self.revision,
            "event_id": self.event_id,
            "memory_record_ids": list(self.memory_record_ids),
            "scheduler_changed": self.scheduler_changed,
            "scheduler_receipt": dict(self.scheduler_receipt),
            "task_projection_changed": self.task_projection_changed,
            "handoff_id": self.handoff_id,
            "projection_attempts": self.projection_attempts,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class SignalEffectReceipt:
    signal_id: str
    task_id: str
    phase: SignalEffectPhase
    event_id: str
    memory_record_ids: tuple[str, ...]
    scheduler_changed: bool
    handoff_id: str
    changed: bool
    errors: tuple[str, ...] = ()
    receipt_id: str = field(default_factory=lambda: runtime_id("signal-effect"))
    created_at: str = field(default_factory=utc_now)

    @property
    def ready(self) -> bool:
        return self.phase in {SignalEffectPhase.HANDOFF_READY, SignalEffectPhase.CONTINUABLE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.watchdog-signal-effect-receipt/v1",
            "receipt_id": self.receipt_id,
            "signal_id": self.signal_id,
            "task_id": self.task_id,
            "phase": self.phase.value,
            "event_id": self.event_id,
            "memory_record_ids": list(self.memory_record_ids),
            "scheduler_changed": self.scheduler_changed,
            "handoff_id": self.handoff_id,
            "changed": self.changed,
            "errors": list(self.errors),
            "ready": self.ready,
            "created_at": self.created_at,
        }


class SignalEffectCoordinator:
    """Reconciles every durable signal into its real downstream effects.

    ``FaultSignalEventWriter`` remains the projection owner. This coordinator
    supplies an operational cursor that prevents API/worker restarts from
    leaving a signal without its canonical event, failure memory, scheduler
    health receipt, task projection or required 07C handoff. It never writes a
    second copy of those domain states.
    """

    _METADATA_KEY = "M1-S07B-02.signal-effects"

    def __init__(
        self,
        store: FaultStateStore,
        writer: FaultSignalEventWriter,
        bridge: WatchdogRecoveryBridge,
        *,
        task_state_resolver: Callable[[str], TaskState | None] | None = None,
    ) -> None:
        self.store = store
        self.writer = writer
        self.bridge = bridge
        self.task_state_resolver = task_state_resolver
        self._guard = threading.RLock()
        self._states: dict[str, SignalEffectState] = {}
        self._metadata_revision = 0
        self._receipts: list[SignalEffectReceipt] = []
        self._restore()

    def reconcile_signal(
        self,
        signal_id: str,
        *,
        task_state: TaskState | None = None,
        require_handoff: bool | None = None,
    ) -> SignalEffectReceipt:
        signal = self.store.signal(signal_id)
        if signal is None:
            raise KeyError(f"fault signal not found: {signal_id}")
        if task_state is None and self.task_state_resolver is not None:
            task_state = self.task_state_resolver(signal.refs.task_id)
        if task_state is not None and (
            task_state.task_id != signal.refs.task_id or task_state.run_id != signal.refs.run_id
        ):
            raise ValueError("task state does not match fault signal scope")
        with self._guard:
            existing = self._states.get(signal.signal_id)
            if existing is not None and existing.phase in {
                SignalEffectPhase.HANDOFF_READY,
                SignalEffectPhase.CONTINUABLE,
                SignalEffectPhase.RESOLVED,
            }:
                receipt = self._receipt(existing, False, ())
                self._receipts.append(receipt)
                del self._receipts[:-500]
                return receipt
            if existing is not None and existing.phase is SignalEffectPhase.PROJECTING:
                # Projection writes canonical events, memory and scheduler
                # health outside this coordinator lock.  Return the in-flight
                # cursor so concurrent listener/reconcile calls cannot run the
                # same external projection twice.
                receipt = self._receipt(existing, False, ())
                self._receipts.append(receipt)
                del self._receipts[:-500]
                return receipt
            state = self._states.setdefault(
                signal.signal_id,
                SignalEffectState(
                    signal_id=signal.signal_id,
                    task_id=signal.refs.task_id,
                    run_id=signal.refs.run_id,
                    phase=SignalEffectPhase.DISCOVERED,
                ),
            )
            prior = state.to_dict()
            state.phase = SignalEffectPhase.PROJECTING
            state.projection_attempts += 1
            state.revision += 1
            state.updated_at = utc_now()
        projection = self._projection(signal, task_state)
        with self._guard:
            self._apply_projection(state, projection)
            if not projection.ok:
                state.phase = SignalEffectPhase.BLOCKED
                state.last_error = "; ".join(projection.errors) or "fault projection incomplete"
            else:
                should_handoff = self._needs_handoff(signal) if require_handoff is None else require_handoff
                if should_handoff:
                    handoff = self.bridge.handoff(
                        signal,
                        metadata={
                            "signal_effect_coordinator": True,
                            "projection_event_id": projection.event_id,
                            "memory_record_ids": list(projection.memory_record_ids),
                            "scheduler_changed": projection.scheduler_changed,
                            "planner_executed": False,
                        },
                    )
                    state.handoff_id = handoff.handoff_id
                    state.phase = SignalEffectPhase.HANDOFF_READY
                else:
                    state.phase = SignalEffectPhase.CONTINUABLE
                state.last_error = ""
            state.revision += 1
            state.updated_at = utc_now()
            changed = state.to_dict() != prior
            receipt = self._receipt(state, changed, projection.errors)
            self._receipts.append(receipt)
            del self._receipts[:-500]
            self._persist_locked()
            return receipt

    def reconcile_task(
        self,
        task_id: str,
        *,
        task_state: TaskState | None = None,
        limit: int = 500,
    ) -> tuple[SignalEffectReceipt, ...]:
        signals = self.store.signals(task_id=task_id, limit=limit)
        receipts: list[SignalEffectReceipt] = []
        for signal in reversed(signals):
            receipts.append(
                self.reconcile_signal(
                    signal.signal_id,
                    task_state=task_state,
                )
            )
        return tuple(receipts)

    def reconcile_new_for_task(
        self,
        task_id: str,
        *,
        task_state: TaskState | None = None,
        limit: int = 500,
    ) -> tuple[SignalEffectReceipt, ...]:
        signals = self.store.signals(task_id=task_id, limit=limit)
        receipts: list[SignalEffectReceipt] = []
        with self._guard:
            known = set(self._states)
        for signal in reversed(signals):
            if signal.signal_id in known:
                continue
            receipts.append(self.reconcile_signal(signal.signal_id, task_state=task_state))
        return tuple(receipts)

    def reconcile_all(self, *, limit: int = 5_000) -> tuple[SignalEffectReceipt, ...]:
        receipts: list[SignalEffectReceipt] = []
        for signal in reversed(self.store.signals(limit=limit)):
            receipts.append(self.reconcile_signal(signal.signal_id))
        return tuple(receipts)

    def resolve(
        self,
        signal_id: str,
        *,
        recovery_receipt: Mapping[str, Any],
    ) -> SignalEffectReceipt:
        with self._guard:
            state = self._states.get(signal_id)
            if state is None:
                raise KeyError(f"signal effect state not found: {signal_id}")
            if state.phase not in {SignalEffectPhase.HANDOFF_READY, SignalEffectPhase.CONTINUABLE}:
                raise RuntimeError(f"signal effect cannot resolve from {state.phase.value}")
            receipt_signal_id = str(recovery_receipt.get("signal_id") or signal_id)
            if receipt_signal_id != signal_id:
                raise ValueError("recovery receipt signal_id mismatch")
            if state.handoff_id:
                receipt_handoff_id = str(recovery_receipt.get("handoff_id") or state.handoff_id)
                if receipt_handoff_id != state.handoff_id:
                    raise ValueError("recovery receipt handoff_id mismatch")
            projection_changed = self.writer.resolve(state.task_id, signal_id)
            state.phase = SignalEffectPhase.RESOLVED
            state.revision += 1
            state.updated_at = utc_now()
            state.last_error = ""
            receipt = self._receipt(
                state,
                True,
                () if projection_changed or not state.task_projection_changed else ("task projection did not resolve",),
            )
            self._receipts.append(receipt)
            del self._receipts[:-500]
            self._persist_locked()
            return receipt

    def readiness(self, signal_id: str) -> Mapping[str, Any]:
        signal = self.store.signal(signal_id)
        if signal is None:
            return {
                "schema": "zyra.watchdog-signal-effect-readiness/v1",
                "signal_id": signal_id,
                "ready": False,
                "error": "signal_missing",
            }
        with self._guard:
            state = self._states.get(signal_id)
        projection = self.store.projection_receipt(signal_id)
        handoffs = self.store.handoffs(task_id=signal.refs.task_id, limit=5_000)
        handoff = next((item for item in handoffs if item.signal_id == signal_id), None)
        needs_handoff = self._needs_handoff(signal)
        return {
            "schema": "zyra.watchdog-signal-effect-readiness/v1",
            "signal_id": signal_id,
            "task_id": signal.refs.task_id,
            "fault_kind": signal.kind.value,
            "phase": state.phase.value if state else SignalEffectPhase.DISCOVERED.value,
            "event_written": bool(projection and projection.canonical_event_written),
            "event_id": projection.event_id if projection else "",
            "memory_record_ids": list(projection.memory_record_ids) if projection else [],
            "scheduler_changed": bool(projection and projection.scheduler_changed),
            "scheduler_receipt": dict(projection.scheduler_receipt) if projection else {},
            "task_projection_changed": bool(projection and projection.task_projection_changed),
            "needs_handoff": needs_handoff,
            "handoff_id": handoff.handoff_id if handoff else "",
            "ready": bool(
                projection
                and projection.ok
                and (not needs_handoff or handoff is not None)
            ),
            "recovery_plan_selected": False,
        }

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        with self._guard:
            states = {
                key: value.to_dict()
                for key, value in sorted(self._states.items())
                if not task_id or value.task_id == task_id
            }
            receipts = [
                item.to_dict()
                for item in self._receipts[-200:]
                if not task_id or item.task_id == task_id
            ]
        counts: dict[str, int] = {}
        for state in states.values():
            phase = str(state["phase"])
            counts[phase] = counts.get(phase, 0) + 1
        return {
            "schema": "zyra.watchdog-signal-effect-coordinator/v1",
            "task_id": task_id,
            "states": states,
            "counts": dict(sorted(counts.items())),
            "recent_receipts": receipts,
            "metadata_revision": self._metadata_revision,
            "projection_owner": "FaultSignalEventWriter",
            "event_owner": "canonical task event store",
            "memory_owner": "MemoryFabric",
            "scheduler_health_owner": "BackendRegistryStore",
            "recovery_plan_owner": "M1-S07C",
        }

    def _projection(
        self,
        signal: FaultSignal,
        task_state: TaskState | None,
    ) -> ProjectionReceipt:
        prior = self.store.projection_receipt(signal.signal_id)
        if prior is None:
            return self.writer.write(signal, task_state=task_state)
        if not prior.ok:
            return self.writer.retry_incomplete(signal, task_state=task_state)
        return prior

    @staticmethod
    def _needs_handoff(signal: FaultSignal) -> bool:
        return (
            signal.terminal
            or signal.disposition in {FaultDisposition.BLOCK, FaultDisposition.RECOVER}
        )

    @staticmethod
    def _apply_projection(state: SignalEffectState, projection: ProjectionReceipt) -> None:
        state.event_id = projection.event_id
        state.memory_record_ids = list(projection.memory_record_ids)
        state.scheduler_changed = projection.scheduler_changed
        state.scheduler_receipt = dict(projection.scheduler_receipt)
        state.task_projection_changed = projection.task_projection_changed
        state.phase = SignalEffectPhase.PROJECTED

    @staticmethod
    def _receipt(
        state: SignalEffectState,
        changed: bool,
        errors: tuple[str, ...],
    ) -> SignalEffectReceipt:
        return SignalEffectReceipt(
            signal_id=state.signal_id,
            task_id=state.task_id,
            phase=state.phase,
            event_id=state.event_id,
            memory_record_ids=tuple(state.memory_record_ids),
            scheduler_changed=state.scheduler_changed,
            handoff_id=state.handoff_id,
            changed=changed,
            errors=tuple(errors),
        )

    def _persist_locked(self) -> None:
        payload = {
            "schema": "zyra.watchdog-signal-effect-coordinator/v1",
            "states": {
                key: value.to_dict()
                for key, value in sorted(self._states.items())
            },
            "updated_at": utc_now(),
            "owns_projection_state": False,
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
        payload, revision = saved
        restored: dict[str, SignalEffectState] = {}
        for signal_id, raw in dict(payload.get("states") or {}).items():
            value = dict(raw or {})
            try:
                restored[str(signal_id)] = SignalEffectState(
                    signal_id=str(value.get("signal_id") or signal_id),
                    task_id=str(value.get("task_id") or ""),
                    run_id=str(value.get("run_id") or ""),
                    phase=SignalEffectPhase(str(value.get("phase") or "discovered")),
                    revision=int(value.get("revision") or 0),
                    event_id=str(value.get("event_id") or ""),
                    memory_record_ids=[str(item) for item in value.get("memory_record_ids") or ()],
                    scheduler_changed=bool(value.get("scheduler_changed")),
                    scheduler_receipt=dict(value.get("scheduler_receipt") or {}),
                    task_projection_changed=bool(value.get("task_projection_changed")),
                    handoff_id=str(value.get("handoff_id") or ""),
                    projection_attempts=int(value.get("projection_attempts") or 0),
                    last_error=str(value.get("last_error") or ""),
                    updated_at=str(value.get("updated_at") or utc_now()),
                )
            except (TypeError, ValueError):
                continue
        self._states = restored
        self._metadata_revision = revision


def signal_effect_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.watchdog-signal-effect-contract/v1",
        "owner": "SignalEffectCoordinator integration cursor",
        "projection_owner": "FaultSignalEventWriter",
        "event_owner": "canonical task event store",
        "memory_owner": "MemoryFabric",
        "scheduler_health_owner": "BackendRegistryStore",
        "handoff_owner": "WatchdogRecoveryBridge",
        "recovery_plan_owner": "M1-S07C",
        "reconciles_after_restart": True,
        "duplicates_domain_state": False,
    }


__all__ = [
    "SignalEffectCoordinator",
    "SignalEffectPhase",
    "SignalEffectReceipt",
    "SignalEffectState",
    "signal_effect_contract",
]
