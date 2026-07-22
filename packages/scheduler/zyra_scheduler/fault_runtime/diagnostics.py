from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import InjectionPhase, ObserverLifecycle, ObserverMaturity, SignalOrigin, utc_now
from .state_store import FaultStateStore


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class DiagnosticFinding:
    code: str
    severity: DiagnosticSeverity
    message: str
    subject: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "subject": self.subject,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class FaultDiagnosticReport:
    generated_at: str
    task_id: str
    findings: tuple[DiagnosticFinding, ...]
    counts: Mapping[str, int]
    checks: Mapping[str, bool]

    @property
    def ok(self) -> bool:
        return not any(
            item.severity in {DiagnosticSeverity.ERROR, DiagnosticSeverity.BLOCKER}
            for item in self.findings
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.fault-runtime-diagnostics/v1",
            "generated_at": self.generated_at,
            "task_id": self.task_id,
            "ok": self.ok,
            "counts": dict(self.counts),
            "checks": dict(self.checks),
            "findings": [item.to_dict() for item in self.findings],
        }


class FaultRuntimeDiagnostics:
    """Checks fault-state causality and source maturity at runtime."""

    def __init__(self, store: FaultStateStore) -> None:
        self.store = store

    def inspect(self, *, task_id: str = "") -> FaultDiagnosticReport:
        findings: list[DiagnosticFinding] = []
        observers = self.store.observers()
        observations = self.store.observations(task_id=task_id, limit=5_000)
        signals = self.store.signals(task_id=task_id, limit=5_000)
        injections = self.store.injections(task_id=task_id, limit=5_000)
        handoffs = self.store.handoffs(task_id=task_id, limit=5_000)
        handoff_deliveries = self.store.handoff_deliveries(task_id=task_id, limit=5_000)
        states = {item.descriptor.observer_id: item for item in observers}
        observation_by_id = {item.observation_id: item for item in observations}
        signal_by_id = {item.signal_id: item for item in signals}

        self._observer_maturity(findings, observers)
        self._signal_causality(findings, signals, observation_by_id, states)
        self._projection_causality(findings, signals)
        self._injection_causality(findings, injections, signal_by_id)
        self._handoff_causality(findings, handoffs, signal_by_id)
        self._identity_integrity(findings, signals)
        counts = Counter(item.severity.value for item in findings)
        counts.update(
            {
                "observers": len(observers),
                "observations": len(observations),
                "signals": len(signals),
                "injections": len(injections),
                "handoffs": len(handoffs),
                "handoff_deliveries": len(handoff_deliveries),
                "dead_handoff_deliveries": sum(
                    item["status"] == "dead"
                    for item in handoff_deliveries
                ),
            }
        )
        checks = {
            "single_fault_store": True,
            "requirement_changed_excluded": all(item.kind.value != "requirement_change" for item in signals),
            "source_inactive_not_running": all(
                item.descriptor.maturity is not ObserverMaturity.SOURCE_INACTIVE
                or item.lifecycle is not ObserverLifecycle.RUNNING
                for item in observers
            ),
            "injection_origin_fenced": all(
                (item.origin is SignalOrigin.INJECTION) == bool(item.provenance.injection_id)
                for item in signals
            ),
            "all_signals_have_observations": all(item.refs.observation_id in observation_by_id for item in signals),
            "all_projected_signals_have_event_ids": all(
                (receipt := self.store.projection_receipt(item.signal_id)) is None or bool(receipt.event_id)
                for item in signals
            ),
            "all_handoffs_have_delivery_state": (
                len(handoffs) == len(handoff_deliveries)
            ),
        }
        return FaultDiagnosticReport(
            generated_at=utc_now(),
            task_id=task_id,
            findings=tuple(findings),
            counts=dict(counts),
            checks=checks,
        )

    def _observer_maturity(
        self,
        findings: list[DiagnosticFinding],
        observers: Sequence[Any],
    ) -> None:
        active_real = [item for item in observers if item.descriptor.maturity is ObserverMaturity.ACTIVE_REAL]
        if not active_real:
            findings.append(
                DiagnosticFinding(
                    "no-active-real-observer",
                    DiagnosticSeverity.BLOCKER,
                    "No active_real watchdog source is registered.",
                    "observer-registry",
                )
            )
        for state in observers:
            descriptor = state.descriptor
            if descriptor.maturity is ObserverMaturity.SOURCE_INACTIVE and state.lifecycle is ObserverLifecycle.RUNNING:
                findings.append(
                    DiagnosticFinding(
                        "source-inactive-running",
                        DiagnosticSeverity.BLOCKER,
                        "A source_inactive observer reached running lifecycle.",
                        descriptor.observer_id,
                    )
                )
            if descriptor.maturity is ObserverMaturity.INJECTION_ONLY and descriptor.enabled_by_default:
                findings.append(
                    DiagnosticFinding(
                        "injection-enabled-by-default",
                        DiagnosticSeverity.ERROR,
                        "Injection-only observer cannot be a default real source.",
                        descriptor.observer_id,
                    )
                )
            if state.lifecycle is ObserverLifecycle.FAILED:
                findings.append(
                    DiagnosticFinding(
                        "observer-failed",
                        DiagnosticSeverity.ERROR,
                        "Observer lifecycle is failed.",
                        descriptor.observer_id,
                        {"last_error": state.last_error},
                    )
                )
            elif state.lifecycle is ObserverLifecycle.DISABLED:
                findings.append(
                    DiagnosticFinding(
                        "observer-disabled",
                        DiagnosticSeverity.INFO,
                        "Observer is explicitly disabled; injection remains independently available.",
                        descriptor.observer_id,
                    )
                )

    def _signal_causality(
        self,
        findings: list[DiagnosticFinding],
        signals: Sequence[Any],
        observations: Mapping[str, Any],
        states: Mapping[str, Any],
    ) -> None:
        for signal in signals:
            observation = observations.get(signal.refs.observation_id)
            if observation is None:
                findings.append(
                    DiagnosticFinding(
                        "signal-without-observation",
                        DiagnosticSeverity.BLOCKER,
                        "Fault signal references a missing structured observation.",
                        signal.signal_id,
                        {"observation_id": signal.refs.observation_id},
                    )
                )
                continue
            if observation.provenance.observer_id != signal.provenance.observer_id:
                findings.append(
                    DiagnosticFinding(
                        "signal-observer-mismatch",
                        DiagnosticSeverity.BLOCKER,
                        "Signal provenance differs from its source observation.",
                        signal.signal_id,
                    )
                )
            state = states.get(signal.provenance.observer_id)
            if state is None:
                findings.append(
                    DiagnosticFinding(
                        "signal-observer-unregistered",
                        DiagnosticSeverity.ERROR,
                        "Signal provenance names an unregistered observer.",
                        signal.signal_id,
                    )
                )
            elif signal.kind not in state.descriptor.emitted_kinds:
                findings.append(
                    DiagnosticFinding(
                        "signal-kind-undeclared",
                        DiagnosticSeverity.ERROR,
                        "Signal kind is outside the observer descriptor contract.",
                        signal.signal_id,
                        {"kind": signal.kind.value, "observer_id": signal.provenance.observer_id},
                    )
                )

    def _projection_causality(
        self,
        findings: list[DiagnosticFinding],
        signals: Sequence[Any],
    ) -> None:
        for signal in signals:
            receipt = self.store.projection_receipt(signal.signal_id)
            if receipt is None:
                findings.append(
                    DiagnosticFinding(
                        "signal-not-projected",
                        DiagnosticSeverity.WARNING,
                        "Signal has no canonical event projection receipt yet.",
                        signal.signal_id,
                    )
                )
                continue
            if not receipt.canonical_event_written:
                findings.append(
                    DiagnosticFinding(
                        "canonical-event-write-failed",
                        DiagnosticSeverity.BLOCKER,
                        "Signal could not be written to the canonical event log.",
                        signal.signal_id,
                        {"errors": list(receipt.errors), "event_id": receipt.event_id},
                    )
                )
            elif receipt.errors:
                findings.append(
                    DiagnosticFinding(
                        "secondary-projection-incomplete",
                        DiagnosticSeverity.ERROR,
                        "Canonical event exists but a memory or scheduler projection failed.",
                        signal.signal_id,
                        {"errors": list(receipt.errors)},
                    )
                )

    def _injection_causality(
        self,
        findings: list[DiagnosticFinding],
        injections: Sequence[Any],
        signals: Mapping[str, Any],
    ) -> None:
        for request, transitions in injections:
            if not transitions:
                findings.append(
                    DiagnosticFinding(
                        "injection-without-journal",
                        DiagnosticSeverity.BLOCKER,
                        "Injection has no durable transition journal.",
                        request.injection_id,
                    )
                )
                continue
            revisions = [item.revision for item in transitions]
            if revisions != list(range(1, len(revisions) + 1)):
                findings.append(
                    DiagnosticFinding(
                        "injection-revision-gap",
                        DiagnosticSeverity.BLOCKER,
                        "Injection transition revisions are not contiguous.",
                        request.injection_id,
                        {"revisions": revisions},
                    )
                )
            latest = transitions[-1]
            if not latest.phase.terminal:
                findings.append(
                    DiagnosticFinding(
                        "injection-in-progress",
                        DiagnosticSeverity.INFO,
                        "Injection has not reached a terminal continuation or handoff phase.",
                        request.injection_id,
                        {"phase": latest.phase.value},
                    )
                )
            observed = next((item for item in transitions if item.phase is InjectionPhase.OBSERVED), None)
            if observed is not None:
                signal = signals.get(observed.signal_id)
                if signal is None:
                    findings.append(
                        DiagnosticFinding(
                            "injected-signal-missing",
                            DiagnosticSeverity.BLOCKER,
                            "Observed injection transition references a missing signal.",
                            request.injection_id,
                        )
                    )
                elif signal.origin is not SignalOrigin.INJECTION:
                    findings.append(
                        DiagnosticFinding(
                            "injected-signal-origin-invalid",
                            DiagnosticSeverity.BLOCKER,
                            "Injected observation produced a real-source signal origin.",
                            signal.signal_id,
                        )
                    )

    def _handoff_causality(
        self,
        findings: list[DiagnosticFinding],
        handoffs: Sequence[Any],
        signals: Mapping[str, Any],
    ) -> None:
        for handoff in handoffs:
            signal = signals.get(handoff.signal_id)
            if signal is None:
                findings.append(
                    DiagnosticFinding(
                        "handoff-without-signal",
                        DiagnosticSeverity.BLOCKER,
                        "Recovery handoff references a missing fault signal.",
                        handoff.handoff_id,
                    )
                )
            elif signal.kind is not handoff.fault_kind:
                findings.append(
                    DiagnosticFinding(
                        "handoff-kind-mismatch",
                        DiagnosticSeverity.ERROR,
                        "Recovery handoff kind differs from its fault signal.",
                        handoff.handoff_id,
                    )
                )
            if handoff.metadata.get("planner_executed") is True:
                findings.append(
                    DiagnosticFinding(
                        "07b-executed-recovery-plan",
                        DiagnosticSeverity.BLOCKER,
                        "07B handoff claims recovery planning that belongs to 07C.",
                        handoff.handoff_id,
                    )
                )

    def _identity_integrity(
        self,
        findings: list[DiagnosticFinding],
        signals: Sequence[Any],
    ) -> None:
        for signal in signals:
            details = dict(signal.details)
            if details.get("critical_ref_source") != "structured_refs_only":
                findings.append(
                    DiagnosticFinding(
                        "critical-ref-source-unproven",
                        DiagnosticSeverity.ERROR,
                        "Signal does not prove that critical refs came from structured fields.",
                        signal.signal_id,
                    )
                )


__all__ = [
    "DiagnosticFinding",
    "DiagnosticSeverity",
    "FaultDiagnosticReport",
    "FaultRuntimeDiagnostics",
]
