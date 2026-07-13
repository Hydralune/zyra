from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zyra_core import EventRecord, EventType

from .models import (
    HealthStatus,
    ObservationScope,
    RecoveryInput,
    RecoveryReason,
    SignalKind,
    WatchdogSignal,
)


_RECOVERY_REASON_BY_SIGNAL: dict[SignalKind, RecoveryReason] = {
    SignalKind.PROCESS_EXITED: RecoveryReason.BROWSER_PROCESS_EXIT,
    SignalKind.CDP_DISCONNECTED: RecoveryReason.CDP_DISCONNECT,
    SignalKind.HEARTBEAT_LATE: RecoveryReason.HEARTBEAT_TIMEOUT,
    SignalKind.REQUEST_TIMEOUT: RecoveryReason.REQUEST_TIMEOUT,
    SignalKind.REQUEST_STALLED: RecoveryReason.REQUEST_TIMEOUT,
    SignalKind.SECURITY_BLOCK: RecoveryReason.SECURITY_DENIAL,
    SignalKind.DOWNLOAD_FAILED: RecoveryReason.DOWNLOAD_FAILURE,
    SignalKind.DOWNLOAD_QUARANTINED: RecoveryReason.DOWNLOAD_FAILURE,
    SignalKind.STORAGE_FAILED: RecoveryReason.STORAGE_FAILURE,
    SignalKind.PERMISSION_DRIFT: RecoveryReason.PERMISSION_DRIFT,
    SignalKind.TOOL_FAILED: RecoveryReason.ACTION_FAILURE,
    SignalKind.ARTIFACT_MISSING: RecoveryReason.ARTIFACT_FAILURE,
    SignalKind.ARTIFACT_TAMPERED: RecoveryReason.ARTIFACT_FAILURE,
    SignalKind.HISTORY_CORRUPTION: RecoveryReason.HISTORY_FAILURE,
}


@dataclass(frozen=True, slots=True)
class FailureProjection:
    health_events: tuple[EventRecord, ...]
    tool_failed_events: tuple[EventRecord, ...]
    recovery_input_events: tuple[EventRecord, ...]
    recovery_inputs: tuple[RecoveryInput, ...]

    @property
    def events(self) -> tuple[EventRecord, ...]:
        return (
            *self.health_events,
            *self.tool_failed_events,
            *self.recovery_input_events,
        )


class BrowserFailureProjector:
    """Projects observability facts without taking the 07C planning owner."""

    def project(
        self,
        scope: ObservationScope,
        signals: Sequence[WatchdogSignal],
        *,
        action_error: str = "",
        failed_tool_call_ids: Sequence[str] = (),
        failed_receipt_ids: Sequence[str] = (),
        evidence_event_ids: Sequence[str] = (),
        artifact_ids: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> FailureProjection:
        self._assert_scope(scope, signals)
        health_events = tuple(self.health_event(item) for item in signals)
        failed_events: list[EventRecord] = []
        recovery_inputs: list[RecoveryInput] = []
        if action_error:
            failed_events.append(
                EventRecord(
                    run_id=scope.run_id,
                    task_id=scope.task_id,
                    node_id=scope.node_id or None,
                    event_type=EventType.AGENT_MESSAGE,
                    payload={
                        "browser_tool_failed": {
                            "schema": "zyra.browser-observability.tool-failed.v1",
                            "scope": scope.to_dict(),
                            "error": action_error,
                            "failed_tool_call_ids": list(failed_tool_call_ids),
                            "failed_receipt_ids": list(failed_receipt_ids),
                            "evidence_event_ids": list(evidence_event_ids),
                            "artifact_ids": list(artifact_ids),
                            "fallback_allowed": False,
                            "planner_owner": "M1-07C",
                        }
                    },
                )
            )
        grouped: dict[RecoveryReason, list[WatchdogSignal]] = {}
        for signal in signals:
            reason = _RECOVERY_REASON_BY_SIGNAL.get(signal.kind)
            if reason is None:
                continue
            if signal.status not in {
                HealthStatus.UNHEALTHY,
                HealthStatus.TERMINATED,
            } and not signal.terminal:
                continue
            grouped.setdefault(reason, []).append(signal)
        if action_error and not grouped:
            grouped[RecoveryReason.ACTION_FAILURE] = []
        for reason, reason_signals in grouped.items():
            summary = (
                reason_signals[0].summary
                if reason_signals
                else f"Browser action failed: {action_error}"
            )
            recovery_inputs.append(
                RecoveryInput(
                    scope=scope,
                    reason=reason,
                    summary=summary,
                    signal_ids=tuple(item.signal_id for item in reason_signals),
                    failed_tool_call_ids=tuple(failed_tool_call_ids),
                    failed_receipt_ids=tuple(failed_receipt_ids),
                    evidence_event_ids=tuple(
                        dict.fromkeys(
                            [
                                *evidence_event_ids,
                                *(
                                    event_id
                                    for item in reason_signals
                                    for event_id in item.evidence_event_ids
                                ),
                            ]
                        )
                    ),
                    artifact_ids=tuple(
                        dict.fromkeys(
                            [
                                *artifact_ids,
                                *(
                                    artifact_id
                                    for item in reason_signals
                                    for artifact_id in item.artifact_ids
                                ),
                            ]
                        )
                    ),
                    retryable=any(item.retryable for item in reason_signals),
                    outcome_unknown=any(
                        bool(item.metadata.get("outcome_unknown"))
                        for item in reason_signals
                    ),
                    metadata={
                        **dict(metadata or {}),
                        "source_watchdogs": sorted(
                            {str(item.watchdog) for item in reason_signals}
                        ),
                        "source_signal_kinds": sorted(
                            {str(item.kind) for item in reason_signals}
                        ),
                        "action_error": action_error,
                    },
                )
            )
        recovery_events = tuple(
            EventRecord(
                run_id=scope.run_id,
                task_id=scope.task_id,
                node_id=scope.node_id or None,
                event_type=EventType.AGENT_MESSAGE,
                payload={"browser_recovery_input": item.to_dict()},
            )
            for item in recovery_inputs
        )
        if any(event.event_type == EventType.RECOVERY_PLANNED for event in recovery_events):
            raise RuntimeError("04D failure projector must not emit recovery_planned")
        return FailureProjection(
            health_events=health_events,
            tool_failed_events=tuple(failed_events),
            recovery_input_events=recovery_events,
            recovery_inputs=tuple(recovery_inputs),
        )

    @staticmethod
    def health_event(
        signal: WatchdogSignal,
    ) -> EventRecord:
        return EventRecord(
            run_id=signal.scope.run_id,
            task_id=signal.scope.task_id,
            node_id=signal.scope.node_id or None,
            event_type=EventType.WORKER_HEALTH,
            payload={
                "worker_health_signal": signal.to_dict(),
                "worker": "BrowserWorker",
                "source_module": "zyra_workers.browser_observability",
            },
        )

    @staticmethod
    def _assert_scope(
        scope: ObservationScope,
        signals: Sequence[WatchdogSignal],
    ) -> None:
        foreign = [item.signal_id for item in signals if item.scope != scope]
        if foreign:
            raise ValueError(
                f"failure projection received signals from another scope: {foreign}"
            )
