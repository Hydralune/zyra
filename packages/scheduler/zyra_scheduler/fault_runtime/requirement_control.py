from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from zyra_core import EventRecord, TaskState
from zyra_symbolic import ConstraintKeeper, TopologyRouter, apply_requirement_change

from .contracts import runtime_id, stable_digest, utc_now
from .event_writer import FaultSignalEventWriter
from .state_store import FaultStateStore


@dataclass(frozen=True, slots=True)
class RequirementChangeReceipt:
    run_id: str
    task_id: str
    source_event_id: str
    emitted_event_ids: tuple[str, ...]
    replan_node_id: str
    decision_id: str
    fault_state_digest_before: str
    fault_state_digest_after: str
    unchanged_fault_state: bool
    receipt_id: str = field(default_factory=lambda: runtime_id("requirement-control"))
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.requirement-change-fault-isolation-receipt/v1",
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "source_event_id": self.source_event_id,
            "emitted_event_ids": list(self.emitted_event_ids),
            "replan_node_id": self.replan_node_id,
            "decision_id": self.decision_id,
            "fault_state_digest_before": self.fault_state_digest_before,
            "fault_state_digest_after": self.fault_state_digest_after,
            "unchanged_fault_state": self.unchanged_fault_state,
            "route": "ConstraintKeeper/TopologyRouter -> replan",
            "classified_as_fault": False,
            "fault_counter_changed": False,
            "fault_backoff_changed": False,
            "scheduler_health_changed": False,
            "failure_memory_changed": False,
            "created_at": self.created_at,
        }


class RequirementChangeFaultIsolation:
    """Routes requirement changes while proving fault state is untouched.

    The established symbolic control path remains the sole behavior owner.
    This guard snapshots only fault-domain state before and after invoking
    ``apply_requirement_change``. It intentionally ignores task graph changes,
    because those are the expected output of ``TopologyRouter`` and
    ``ConstraintKeeper``.
    """

    _METADATA_KEY = "M1-S07B-02.requirement-control"

    def __init__(
        self,
        store: FaultStateStore,
        writer: FaultSignalEventWriter,
        *,
        event_sink: Callable[[EventRecord], None],
        router: TopologyRouter | None = None,
        keeper: ConstraintKeeper | None = None,
    ) -> None:
        if not callable(event_sink):
            raise TypeError("requirement control requires canonical event sink")
        self.store = store
        self.writer = writer
        self.event_sink = event_sink
        self.router = router or TopologyRouter()
        self.keeper = keeper or ConstraintKeeper()
        self._guard = threading.RLock()
        self._receipts: list[RequirementChangeReceipt] = []
        self._metadata_revision = 0
        self._restore()

    def apply(self, state: TaskState, event: EventRecord) -> RequirementChangeReceipt:
        self._validate_event(state, event)
        before = self._fault_snapshot(state.task_id)
        before_digest = stable_digest(before)
        before_task_fault = copy.deepcopy(dict(state.metadata.get("fault_runtime") or {}))
        before_projection = copy.deepcopy(dict(self.writer.task_projection(state.task_id)))
        events = apply_requirement_change(
            state,
            event,
            router=self.router,
            keeper=self.keeper,
        )
        for emitted in events:
            self.event_sink(emitted)
        after = self._fault_snapshot(state.task_id)
        after_digest = stable_digest(after)
        after_task_fault = copy.deepcopy(dict(state.metadata.get("fault_runtime") or {}))
        after_projection = copy.deepcopy(dict(self.writer.task_projection(state.task_id)))
        unchanged = (
            before_digest == after_digest
            and before_task_fault == after_task_fault
            and before_projection == after_projection
        )
        if not unchanged:
            raise RuntimeError(
                "RequirementChanged mutated fault counters, backoff, scheduler health, memory projection, or fault task metadata"
            )
        latest = dict((state.metadata.get("requirement_changes") or [{}])[-1])
        receipt = RequirementChangeReceipt(
            run_id=state.run_id,
            task_id=state.task_id,
            source_event_id=event.event_id,
            emitted_event_ids=tuple(item.event_id for item in events),
            replan_node_id=str(latest.get("replan_node_id") or ""),
            decision_id=str(latest.get("decision_id") or ""),
            fault_state_digest_before=before_digest,
            fault_state_digest_after=after_digest,
            unchanged_fault_state=True,
        )
        with self._guard:
            self._receipts.append(receipt)
            del self._receipts[:-200]
            self._persist_locked()
        return receipt

    def assert_isolated(
        self,
        state: TaskState,
        operation: Callable[[], Sequence[EventRecord] | None],
        *,
        source_event_id: str,
    ) -> Mapping[str, Any]:
        """Guard an existing requirement-change handler without rerouting it."""

        before = self._fault_snapshot(state.task_id)
        before_task_fault = copy.deepcopy(dict(state.metadata.get("fault_runtime") or {}))
        before_projection = copy.deepcopy(dict(self.writer.task_projection(state.task_id)))
        result = operation()
        after = self._fault_snapshot(state.task_id)
        after_task_fault = copy.deepcopy(dict(state.metadata.get("fault_runtime") or {}))
        after_projection = copy.deepcopy(dict(self.writer.task_projection(state.task_id)))
        unchanged = (
            before == after
            and before_task_fault == after_task_fault
            and before_projection == after_projection
        )
        if not unchanged:
            raise RuntimeError("requirement-change handler crossed the watchdog fault boundary")
        emitted = tuple(result or ())
        return {
            "schema": "zyra.requirement-change-fault-isolation/v1",
            "source_event_id": source_event_id,
            "run_id": state.run_id,
            "task_id": state.task_id,
            "emitted_event_ids": [item.event_id for item in emitted],
            "fault_state_digest": stable_digest(after),
            "unchanged_fault_state": True,
            "route_owner": "ConstraintKeeper/TopologyRouter",
            "fault_owner": "WatchdogSignalClassifier",
            "classified_as_fault": False,
        }

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        with self._guard:
            receipts = [
                item.to_dict()
                for item in self._receipts
                if not task_id or item.task_id == task_id
            ]
        return {
            "schema": "zyra.requirement-change-fault-isolation-runtime/v1",
            "task_id": task_id,
            "receipts": receipts[-100:],
            "receipt_count": len(receipts),
            "metadata_revision": self._metadata_revision,
            "route": "ConstraintKeeper/TopologyRouter -> replan",
            "fault_classifier_invoked": False,
            "requirement_changed_is_fault": False,
        }

    def _fault_snapshot(self, task_id: str) -> Mapping[str, Any]:
        signals = self.store.signals(task_id=task_id, limit=10_000)
        observations = self.store.observations(task_id=task_id, limit=10_000)
        injections = self.store.injections(task_id=task_id, limit=10_000)
        handoffs = self.store.handoffs(task_id=task_id, limit=10_000)
        deliveries = self.store.handoff_deliveries(task_id=task_id, limit=10_000)
        projection_receipts = []
        for signal in signals:
            receipt = self.store.projection_receipt(signal.signal_id)
            projection_receipts.append(None if receipt is None else receipt.to_dict())
        return {
            "signal_ids": [item.signal_id for item in signals],
            "signal_fingerprints": [item.fingerprint for item in signals],
            "observation_ids": [item.observation_id for item in observations],
            "observation_fingerprints": [item.fingerprint for item in observations],
            "injections": [
                {
                    "injection_id": request.injection_id,
                    "phases": [step.phase.value for step in transitions],
                    "revisions": [step.revision for step in transitions],
                }
                for request, transitions in injections
            ],
            "handoff_ids": [item.handoff_id for item in handoffs],
            "handoff_signal_ids": [item.signal_id for item in handoffs],
            "deliveries": [
                {
                    "handoff_id": str(item.get("handoff_id") or ""),
                    "status": str(item.get("status") or ""),
                    "attempt_count": int(item.get("attempt_count") or 0),
                    "lease_owner": str(item.get("lease_owner") or ""),
                }
                for item in deliveries
            ],
            "projection_receipts": projection_receipts,
        }

    @staticmethod
    def _validate_event(state: TaskState, event: EventRecord) -> None:
        if event.run_id != state.run_id or event.task_id != state.task_id:
            raise ValueError("requirement change event is outside task scope")
        event_type = str(event.event_type).split(".")[-1].lower()
        payload_intent = str(event.payload.get("intent") or "").lower()
        command = str(event.payload.get("command") or "").lower()
        if not (
            event_type in {"requirement_change", "requirement_changed", "control_command"}
            or payload_intent in {"requirement_change", "requirement_changed"}
            or command in {"/change", "change"}
        ):
            raise ValueError("event is not a RequirementChanged control fact")

    def _persist_locked(self) -> None:
        payload = {
            "schema": "zyra.requirement-change-fault-isolation-runtime/v1",
            "receipts": [item.to_dict() for item in self._receipts[-100:]],
            "updated_at": utc_now(),
            "requirement_changed_is_fault": False,
        }
        self._metadata_revision = self.store.put_metadata(
            self._METADATA_KEY,
            payload,
            expected_revision=self._metadata_revision,
        )

    def _restore(self) -> None:
        saved = self.store.metadata(self._METADATA_KEY)
        if saved is not None:
            _payload, self._metadata_revision = saved


def requirement_control_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.requirement-change-fault-isolation-contract/v1",
        "route": "ConstraintKeeper/TopologyRouter -> replan",
        "classifier": "not invoked",
        "fault_count_mutation": False,
        "fault_pressure_mutation": False,
        "fault_backoff_mutation": False,
        "scheduler_health_mutation": False,
        "failure_memory_mutation": False,
        "control_reason_can_be_failure_signal": False,
    }


__all__ = [
    "RequirementChangeFaultIsolation",
    "RequirementChangeReceipt",
    "requirement_control_contract",
]
