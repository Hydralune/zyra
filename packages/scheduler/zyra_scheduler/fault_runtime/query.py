from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import FaultKind, FaultSeverity, FaultSignal, InjectionPhase, SignalOrigin
from .state_store import FaultStateStore


@dataclass(frozen=True, slots=True)
class FaultQuery:
    task_id: str = ""
    run_id: str = ""
    kinds: tuple[FaultKind, ...] = ()
    severities: tuple[FaultSeverity, ...] = ()
    origins: tuple[SignalOrigin, ...] = ()
    observer_ids: tuple[str, ...] = ()
    worker_id: str = ""
    provider_id: str = ""
    browser_session_id: str = ""
    workspace_id: str = ""
    tool_call_id: str = ""
    injection_id: str = ""
    terminal: bool | None = None
    retryable: bool | None = None
    limit: int = 200

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 5_000:
            raise ValueError("fault query limit must be in range 1..5000")


@dataclass(frozen=True, slots=True)
class FaultTimelineEntry:
    created_at: str
    item_type: str
    item_id: str
    task_id: str
    run_id: str
    fault_kind: str = ""
    signal_id: str = ""
    observation_id: str = ""
    injection_id: str = ""
    event_id: str = ""
    phase: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "item_type": self.item_type,
            "item_id": self.item_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "fault_kind": self.fault_kind,
            "signal_id": self.signal_id,
            "observation_id": self.observation_id,
            "injection_id": self.injection_id,
            "event_id": self.event_id,
            "phase": self.phase,
            "payload": dict(self.payload),
        }


class FaultRuntimeQueryService:
    """Read model over durable observations, signals, projections and handoffs."""

    def __init__(self, store: FaultStateStore) -> None:
        self.store = store

    def signals(self, query: FaultQuery | None = None) -> tuple[FaultSignal, ...]:
        selected = query or FaultQuery()
        candidates = self.store.signals(
            task_id=selected.task_id,
            injection_id=selected.injection_id,
            limit=max(selected.limit * 4, selected.limit),
        )
        result: list[FaultSignal] = []
        for signal in candidates:
            if selected.run_id and signal.refs.run_id != selected.run_id:
                continue
            if selected.kinds and signal.kind not in selected.kinds:
                continue
            if selected.severities and signal.severity not in selected.severities:
                continue
            if selected.origins and signal.origin not in selected.origins:
                continue
            if selected.observer_ids and signal.provenance.observer_id not in selected.observer_ids:
                continue
            if selected.worker_id and signal.refs.worker_id != selected.worker_id:
                continue
            if selected.provider_id and signal.refs.provider_id != selected.provider_id:
                continue
            if selected.browser_session_id and signal.refs.browser_session_id != selected.browser_session_id:
                continue
            if selected.workspace_id and signal.refs.workspace_id != selected.workspace_id:
                continue
            if selected.tool_call_id and signal.refs.tool_call_id != selected.tool_call_id:
                continue
            if selected.terminal is not None and signal.terminal is not selected.terminal:
                continue
            if selected.retryable is not None and signal.retryable is not selected.retryable:
                continue
            result.append(signal)
            if len(result) >= selected.limit:
                break
        return tuple(result)

    def signal_view(self, signal_id: str) -> Mapping[str, Any] | None:
        signal = self.store.signal(signal_id)
        if signal is None:
            return None
        observation = self.store.observation(signal.refs.observation_id)
        projection = self.store.projection_receipt(signal.signal_id)
        handoff = next(
            (item for item in self.store.handoffs(task_id=signal.refs.task_id, limit=5_000) if item.signal_id == signal.signal_id),
            None,
        )
        injection = None
        if signal.provenance.injection_id:
            injection = self.store.injection(signal.provenance.injection_id)
        return {
            "schema": "zyra.fault-signal-view/v1",
            "signal": signal.to_dict(),
            "observation": None if observation is None else observation.to_dict(),
            "projection": None if projection is None else projection.to_dict(),
            "recovery_handoff": None if handoff is None else handoff.to_dict(),
            "injection": (
                None
                if injection is None
                else {
                    "request": injection[0].to_dict(),
                    "transitions": [item.to_dict() for item in injection[1]],
                }
            ),
            "delivery_attempts": list(self.store.delivery_attempts(signal.signal_id)),
            "causal_complete": observation is not None and projection is not None,
        }

    def timeline(self, *, task_id: str, limit: int = 1_000) -> tuple[FaultTimelineEntry, ...]:
        if not task_id:
            raise ValueError("fault timeline requires task_id")
        entries: list[FaultTimelineEntry] = []
        observations = self.store.observations(task_id=task_id, limit=limit)
        for item in observations:
            entries.append(
                FaultTimelineEntry(
                    created_at=item.observed_at,
                    item_type="observation",
                    item_id=item.observation_id,
                    task_id=item.refs.task_id,
                    run_id=item.refs.run_id,
                    observation_id=item.observation_id,
                    injection_id=item.provenance.injection_id,
                    payload=item.to_dict(),
                )
            )
        signals = self.store.signals(task_id=task_id, limit=limit)
        for item in signals:
            entries.append(
                FaultTimelineEntry(
                    created_at=item.created_at,
                    item_type="signal",
                    item_id=item.signal_id,
                    task_id=item.refs.task_id,
                    run_id=item.refs.run_id,
                    fault_kind=item.kind.value,
                    signal_id=item.signal_id,
                    observation_id=item.refs.observation_id,
                    injection_id=item.provenance.injection_id,
                    payload=item.to_dict(),
                )
            )
            projection = self.store.projection_receipt(item.signal_id)
            if projection is not None:
                entries.append(
                    FaultTimelineEntry(
                        created_at=projection.created_at,
                        item_type="projection",
                        item_id=projection.receipt_id,
                        task_id=item.refs.task_id,
                        run_id=item.refs.run_id,
                        fault_kind=item.kind.value,
                        signal_id=item.signal_id,
                        observation_id=item.refs.observation_id,
                        injection_id=item.provenance.injection_id,
                        event_id=projection.event_id,
                        payload=projection.to_dict(),
                    )
                )
        for request, transitions in self.store.injections(task_id=task_id, limit=limit):
            for transition in transitions:
                entries.append(
                    FaultTimelineEntry(
                        created_at=transition.created_at,
                        item_type="injection_transition",
                        item_id=transition.transition_id,
                        task_id=request.task_id,
                        run_id=request.run_id,
                        signal_id=transition.signal_id,
                        injection_id=request.injection_id,
                        event_id=transition.event_id,
                        phase=transition.phase.value,
                        payload=transition.to_dict(),
                    )
                )
        for handoff in self.store.handoffs(task_id=task_id, limit=limit):
            entries.append(
                FaultTimelineEntry(
                    created_at=handoff.created_at,
                    item_type="recovery_handoff",
                    item_id=handoff.handoff_id,
                    task_id=handoff.task_id,
                    run_id=handoff.run_id,
                    fault_kind=handoff.fault_kind.value,
                    signal_id=handoff.signal_id,
                    observation_id=handoff.refs.observation_id,
                    injection_id=handoff.injection_id,
                    payload=handoff.to_dict(),
                )
            )
        entries.sort(key=lambda item: (item.created_at, item.item_type, item.item_id))
        return tuple(entries[-limit:])

    def task_summary(self, task_id: str) -> Mapping[str, Any]:
        signals = self.store.signals(task_id=task_id, limit=5_000)
        observations = self.store.observations(task_id=task_id, limit=5_000)
        injections = self.store.injections(task_id=task_id, limit=5_000)
        handoffs = self.store.handoffs(task_id=task_id, limit=5_000)
        by_kind = Counter(item.kind.value for item in signals)
        by_severity = Counter(item.severity.value for item in signals)
        by_origin = Counter(item.origin.value for item in signals)
        by_observer = Counter(item.provenance.observer_id for item in signals)
        phases = Counter(transitions[-1].phase.value for _, transitions in injections if transitions)
        projected = [item for item in signals if self.store.projection_receipt(item.signal_id) is not None]
        failed_projection = [
            item for item in projected
            if not self.store.projection_receipt(item.signal_id).ok  # type: ignore[union-attr]
        ]
        return {
            "schema": "zyra.fault-task-summary/v1",
            "task_id": task_id,
            "counts": {
                "observations": len(observations),
                "signals": len(signals),
                "injections": len(injections),
                "recovery_handoffs": len(handoffs),
                "projected": len(projected),
                "projection_errors": len(failed_projection),
                "terminal": sum(item.terminal for item in signals),
                "retryable": sum(item.retryable for item in signals),
            },
            "by_kind": dict(sorted(by_kind.items())),
            "by_severity": dict(sorted(by_severity.items())),
            "by_origin": dict(sorted(by_origin.items())),
            "by_observer": dict(sorted(by_observer.items())),
            "injection_terminal_phases": dict(sorted(phases.items())),
            "real_source_count": len({item.provenance.observer_id for item in signals if item.origin is SignalOrigin.OBSERVER}),
            "injection_masks_real_source": False,
        }

    def coverage(self) -> Mapping[str, Any]:
        states = self.store.observers()
        descriptors = [item.descriptor for item in states]
        declared: dict[str, set[str]] = defaultdict(set)
        observed: dict[str, set[str]] = defaultdict(set)
        for descriptor in descriptors:
            for kind in descriptor.emitted_kinds:
                declared[kind.value].add(descriptor.observer_id)
        for signal in self.store.signals(limit=5_000):
            observed[signal.kind.value].add(signal.provenance.observer_id)
        return {
            "schema": "zyra.fault-observer-coverage/v1",
            "fault_kinds": {
                kind.value: {
                    "declared_observers": sorted(declared.get(kind.value, set())),
                    "observed_observers": sorted(observed.get(kind.value, set())),
                    "has_declared_source": bool(declared.get(kind.value)),
                }
                for kind in FaultKind
                if kind is not FaultKind.UNKNOWN
            },
            "maturity_counts": dict(Counter(item.descriptor.maturity.value for item in states)),
            "running_observers": sorted(
                item.descriptor.observer_id for item in states if item.lifecycle.accepts_observations
            ),
        }

    def recovery_queue(self, *, task_id: str = "") -> tuple[Mapping[str, Any], ...]:
        """Return handoffs with the evidence 07C needs to claim work safely."""
        delivery_rows = self.store.handoff_deliveries(
            task_id=task_id,
            statuses=("pending", "leased", "dead"),
            limit=5_000,
        )
        delivery_by_handoff = {
            str(item["handoff_id"]): item
            for item in delivery_rows
        }
        handoffs = tuple(
            item
            for item in self.store.handoffs(task_id=task_id, limit=5_000)
            if item.handoff_id in delivery_by_handoff
        )
        output: list[Mapping[str, Any]] = []
        for handoff in handoffs:
            signal = self.store.signal(handoff.signal_id)
            handoff_delivery = delivery_by_handoff[handoff.handoff_id]
            if signal is None:
                output.append({
                    "handoff": handoff.to_dict(),
                    "handoff_delivery": handoff_delivery,
                    "ready": False,
                    "blocker": "canonical fault signal is missing",
                })
                continue
            projection = self.store.projection_receipt(signal.signal_id)
            observation = self.store.observation(signal.refs.observation_id)
            delivery = self.store.delivery_attempts(signal.signal_id)
            ready = (
                projection is not None
                and projection.canonical_event_written
                and observation is not None
                and handoff_delivery["status"] != "dead"
            )
            output.append({
                "handoff": handoff.to_dict(),
                "signal": signal.to_dict(),
                "projection": None if projection is None else projection.to_dict(),
                "observation": None if observation is None else observation.to_dict(),
                "delivery_attempts": list(delivery),
                "handoff_delivery": handoff_delivery,
                "ready": ready,
                "blocker": (
                    ""
                    if ready
                    else (
                        "handoff delivery exhausted its bounded attempts"
                        if handoff_delivery["status"] == "dead"
                        else "fault causality is incomplete"
                    )
                ),
                "claim_owner": "M1-S07C.RecoveryPlanner",
                "07b_planner_executed": False,
            })
        return tuple(output)

    def run_matrix(self, *, task_id: str = "") -> Mapping[str, Any]:
        """Summarize real/injected fault reachability per run and target class."""
        signals = self.store.signals(task_id=task_id, limit=5_000)
        matrix: dict[str, dict[str, Any]] = {}
        for signal in signals:
            row = matrix.setdefault(signal.refs.run_id, {
                "run_id": signal.refs.run_id,
                "task_ids": set(),
                "real_signal_ids": [],
                "injected_signal_ids": [],
                "fault_kinds": set(),
                "observer_ids": set(),
                "target_classes": set(),
                "projected_count": 0,
                "handoff_count": 0,
            })
            row["task_ids"].add(signal.refs.task_id)
            row["fault_kinds"].add(signal.kind.value)
            row["observer_ids"].add(signal.provenance.observer_id)
            if signal.origin is SignalOrigin.INJECTION:
                row["injected_signal_ids"].append(signal.signal_id)
            else:
                row["real_signal_ids"].append(signal.signal_id)
            refs = signal.refs
            target = next((
                name
                for name, value in (
                    ("tool", refs.tool_call_id),
                    ("worker", refs.worker_id),
                    ("browser", refs.browser_session_id),
                    ("provider", refs.provider_id),
                    ("workspace", refs.workspace_id),
                    ("mcp", refs.mcp_server_id),
                    ("subagent", refs.subagent_task_id),
                )
                if value
            ), "task")
            row["target_classes"].add(target)
            row["projected_count"] += int(self.store.projection_receipt(signal.signal_id) is not None)
        for handoff in self.store.handoffs(task_id=task_id, limit=5_000):
            row = matrix.get(handoff.run_id)
            if row is not None:
                row["handoff_count"] += 1
        normalized = []
        for run_id in sorted(matrix):
            row = matrix[run_id]
            normalized.append({
                **row,
                "task_ids": sorted(row["task_ids"]),
                "fault_kinds": sorted(row["fault_kinds"]),
                "observer_ids": sorted(row["observer_ids"]),
                "target_classes": sorted(row["target_classes"]),
                "real_signal_count": len(row["real_signal_ids"]),
                "injected_signal_count": len(row["injected_signal_ids"]),
                "injection_masks_real_source": False,
            })
        return {
            "schema": "zyra.fault-run-matrix/v1",
            "task_id": task_id,
            "runs": normalized,
            "run_count": len(normalized),
        }

    @staticmethod
    def contract() -> dict[str, Any]:
        return {
            "schema": "zyra.fault-query-service/v1",
            "views": [
                "signals",
                "signal causal view",
                "task timeline",
                "task summary",
                "observer coverage",
                "recovery queue",
                "run matrix",
            ],
            "state_owner": "python.FaultStateStore",
            "read_only": True,
            "recovery_plan_owner": False,
        }


__all__ = ["FaultQuery", "FaultRuntimeQueryService", "FaultTimelineEntry"]
