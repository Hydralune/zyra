from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .history_store import BrowserHistoryStore
from .models import (
    ArtifactLineage,
    ArtifactRole,
    HealthStatus,
    HistoryKind,
    ObservationScope,
    RecoveryInput,
    RecoveryReason,
    Severity,
    SignalKind,
    WatchdogName,
    WatchdogSignal,
)


class DurableProjectionError(RuntimeError):
    code = "browser_durable_projection_error"


@dataclass(frozen=True, slots=True)
class DurableBrowserProjection:
    scope: ObservationScope
    head_digest: str
    record_count: int
    artifact_lineage: tuple[ArtifactLineage, ...]
    signals: tuple[WatchdogSignal, ...]
    recovery_inputs: tuple[RecoveryInput, ...]

    @property
    def health_status(self) -> HealthStatus:
        rank = {
            HealthStatus.UNKNOWN: 0,
            HealthStatus.HEALTHY: 1,
            HealthStatus.DEGRADED: 2,
            HealthStatus.UNHEALTHY: 3,
            HealthStatus.TERMINATED: 4,
        }
        return max(
            (item.status for item in self.signals),
            key=lambda item: rank[item],
            default=HealthStatus.UNKNOWN,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.restart-projection.v1",
            "scope": self.scope.to_dict(),
            "head_digest": self.head_digest,
            "record_count": self.record_count,
            "health_status": str(self.health_status),
            "artifact_lineage": [item.to_dict() for item in self.artifact_lineage],
            "signals": [item.to_dict() for item in self.signals],
            "recovery_inputs": [item.to_dict() for item in self.recovery_inputs],
            "durable_source": "BrowserHistoryStore",
            "canonical": False,
            "recovery_planner_owner": "M1-07C",
        }


class BrowserRestartProjectionRuntime:
    """Rebuild process-live health/artifact views solely from durable history."""

    def __init__(self, history_store: BrowserHistoryStore) -> None:
        self.history_store = history_store

    def rebuild(self, scope: ObservationScope) -> DurableBrowserProjection:
        records = self.history_store.records(scope)
        head = self.history_store.head(scope)
        lineage: list[ArtifactLineage] = []
        signals: list[WatchdogSignal] = []
        recovery_inputs: list[RecoveryInput] = []
        seen_lineage: set[str] = set()
        seen_signals: set[str] = set()
        seen_inputs: set[str] = set()
        for record in records:
            if record.kind == HistoryKind.ARTIFACT_PUBLISHED:
                value = record.payload.get("lineage")
                if isinstance(value, Mapping):
                    item = artifact_lineage_from_mapping(scope, value)
                    if item.receipt_id not in seen_lineage:
                        seen_lineage.add(item.receipt_id)
                        lineage.append(item)
            elif record.kind == HistoryKind.WATCHDOG_SIGNAL:
                value = record.payload.get("signal")
                if isinstance(value, Mapping):
                    item = watchdog_signal_from_mapping(scope, value)
                    if item.signal_id not in seen_signals:
                        seen_signals.add(item.signal_id)
                        signals.append(item)
            elif record.kind == HistoryKind.RECOVERY_INPUT:
                value = record.payload.get("recovery_input")
                if isinstance(value, Mapping):
                    item = recovery_input_from_mapping(scope, value)
                    if item.input_id not in seen_inputs:
                        seen_inputs.add(item.input_id)
                        recovery_inputs.append(item)
        return DurableBrowserProjection(
            scope=scope,
            head_digest=head.content_digest if head else "",
            record_count=head.sequence if head else 0,
            artifact_lineage=tuple(lineage),
            signals=tuple(signals),
            recovery_inputs=tuple(recovery_inputs),
        )

    def rebuild_task(self, task_id: str) -> tuple[DurableBrowserProjection, ...]:
        output: list[DurableBrowserProjection] = []
        for item in self.history_store.list_scopes(task_id=task_id):
            scope_value = item.get("scope")
            if not isinstance(scope_value, Mapping):
                continue
            output.append(self.rebuild(scope_from_mapping(scope_value)))
        return tuple(output)


def artifact_lineage_from_mapping(
    scope: ObservationScope,
    value: Mapping[str, Any],
) -> ArtifactLineage:
    _assert_scope(scope, value.get("scope"))
    return ArtifactLineage(
        scope=scope,
        artifact_id=str(value["artifact_id"]),
        role=ArtifactRole(str(value["role"])),
        uri=str(value.get("uri") or ""),
        sha256=str(value["sha256"]),
        size_bytes=int(value.get("size_bytes") or 0),
        receipt_id=str(value["receipt_id"]),
        created_at=str(value.get("created_at") or ""),
        source_event_ids=_strings(value.get("source_event_ids")),
        source_record_ids=_strings(value.get("source_record_ids")),
        parent_artifact_ids=_strings(value.get("parent_artifact_ids")),
        media_type=str(value.get("media_type") or "application/octet-stream"),
        quarantined=bool(value.get("quarantined")),
        metadata=_mapping(value.get("metadata")),
    )


def watchdog_signal_from_mapping(
    scope: ObservationScope,
    value: Mapping[str, Any],
) -> WatchdogSignal:
    _assert_scope(scope, value.get("scope"))
    return WatchdogSignal(
        scope=scope,
        watchdog=WatchdogName(str(value["watchdog"])),
        kind=SignalKind(str(value["kind"])),
        status=HealthStatus(str(value["status"])),
        severity=Severity(str(value["severity"])),
        summary=str(value["summary"]),
        signal_id=str(value["signal_id"]),
        detected_at=str(value.get("detected_at") or ""),
        sequence=int(value.get("sequence") or 0),
        retryable=bool(value.get("retryable")),
        terminal=bool(value.get("terminal")),
        evidence_event_ids=_strings(value.get("evidence_event_ids")),
        artifact_ids=_strings(value.get("artifact_ids")),
        metadata=_mapping(value.get("metadata")),
    )


def recovery_input_from_mapping(
    scope: ObservationScope,
    value: Mapping[str, Any],
) -> RecoveryInput:
    _assert_scope(scope, value.get("scope"))
    return RecoveryInput(
        scope=scope,
        reason=RecoveryReason(str(value["reason"])),
        summary=str(value["summary"]),
        signal_ids=_strings(value.get("signal_ids")),
        input_id=str(value["input_id"]),
        created_at=str(value.get("created_at") or ""),
        failed_tool_call_ids=_strings(value.get("failed_tool_call_ids")),
        failed_receipt_ids=_strings(value.get("failed_receipt_ids")),
        evidence_event_ids=_strings(value.get("evidence_event_ids")),
        artifact_ids=_strings(value.get("artifact_ids")),
        retryable=bool(value.get("retryable")),
        outcome_unknown=bool(value.get("outcome_unknown")),
        metadata=_mapping(value.get("metadata")),
    )


def scope_from_mapping(value: Mapping[str, Any]) -> ObservationScope:
    return ObservationScope(
        run_id=str(value["run_id"]),
        task_id=str(value["task_id"]),
        node_id=str(value.get("node_id") or ""),
        browser_session_id=str(value["browser_session_id"]),
        canonical_session_id=str(value.get("canonical_session_id") or ""),
        worker_request_id=str(value["worker_request_id"]),
    )


def _assert_scope(scope: ObservationScope, raw: Any) -> None:
    if isinstance(raw, Mapping) and scope_from_mapping(raw) != scope:
        raise DurableProjectionError("durable browser record crosses observation scope")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(str(item) for item in value if str(item))
    return ()
