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
    ReplayIssue,
    ReplayProjection,
    Severity,
    SignalKind,
    TraceSpan,
    TraceSpanKind,
    TraceStatus,
    WatchdogName,
    WatchdogSignal,
    scope_from_mapping,
)
from .trace_runtime import BrowserTraceRuntime, TracePairingError


@dataclass(frozen=True, slots=True)
class ReplayPolicy:
    require_session_start: bool = True
    require_terminal_tool_results: bool = True
    require_artifact_lineage: bool = True
    fail_on_history_corruption: bool = True
    max_records: int = 100_000


class BrowserHistoryReplay:
    """Deterministic replay over durable facts; no browser side effects."""

    def __init__(
        self,
        store: BrowserHistoryStore,
        *,
        trace_runtime: BrowserTraceRuntime | None = None,
        policy: ReplayPolicy | None = None,
    ) -> None:
        self.store = store
        self.trace_runtime = trace_runtime or BrowserTraceRuntime()
        self.policy = policy or ReplayPolicy()

    def replay(
        self,
        scope: ObservationScope,
    ) -> ReplayProjection:
        issues: list[ReplayIssue] = []
        audit = self.store.audit(scope)
        issues.extend(audit.issues)
        if not audit.ok and self.policy.fail_on_history_corruption:
            return ReplayProjection(
                scope=scope,
                records=(),
                tool_pairs=(),
                trace_spans=(),
                artifacts=(),
                signals=(),
                issues=tuple(issues),
                complete=False,
                head_digest=audit.head_digest,
            )
        records = self.store.records(scope)
        if len(records) > self.policy.max_records:
            issues.append(
                ReplayIssue(
                    code="replay_record_limit",
                    summary="history exceeds replay record limit",
                    fatal=True,
                    details={
                        "records": len(records),
                        "limit": self.policy.max_records,
                    },
                )
            )
            records = records[: self.policy.max_records]
        if (
            self.policy.require_session_start
            and not any(item.kind == HistoryKind.SESSION_STARTED for item in records)
        ):
            issues.append(
                ReplayIssue(
                    code="session_start_missing",
                    summary="durable history has no session start",
                    fatal=True,
                )
            )
        try:
            tool_pairs = self.trace_runtime.tool_pairs(scope, records)
        except TracePairingError as error:
            tool_pairs = ()
            issues.append(
                ReplayIssue(
                    code=error.code,
                    summary=str(error),
                    fatal=self.policy.require_terminal_tool_results,
                )
            )
        try:
            spans = self.trace_runtime.spans_from_records(scope, records)
        except TracePairingError as error:
            spans = ()
            issues.append(
                ReplayIssue(
                    code=error.code,
                    summary=str(error),
                    fatal=self.policy.require_terminal_tool_results,
                )
            )
        artifacts = self._artifacts(scope, records, issues)
        signals = self._signals(scope, records, issues)
        referenced_artifacts = {
            artifact_id
            for item in records
            for artifact_id in item.artifact_ids
        }
        lineage_ids = {item.artifact_id for item in artifacts}
        missing_lineage = sorted(referenced_artifacts - lineage_ids)
        if missing_lineage and self.policy.require_artifact_lineage:
            issues.append(
                ReplayIssue(
                    code="artifact_lineage_missing",
                    summary="history references artifacts without 04D lineage",
                    fatal=True,
                    details={"artifact_ids": missing_lineage},
                )
            )
        complete = not any(item.fatal for item in issues)
        return ReplayProjection(
            scope=scope,
            records=records,
            tool_pairs=tool_pairs,
            trace_spans=spans,
            artifacts=artifacts,
            signals=signals,
            issues=tuple(issues),
            complete=complete,
            head_digest=audit.head_digest,
        )

    def _artifacts(
        self,
        scope: ObservationScope,
        records: Sequence[Any],
        issues: list[ReplayIssue],
    ) -> tuple[ArtifactLineage, ...]:
        values: list[ArtifactLineage] = []
        for record in records:
            if record.kind != HistoryKind.ARTIFACT_PUBLISHED:
                continue
            raw = record.payload.get("lineage")
            if not isinstance(raw, Mapping):
                issues.append(
                    ReplayIssue(
                        code="artifact_lineage_invalid",
                        summary="artifact record has no lineage mapping",
                        sequence=record.sequence,
                        record_id=record.record_id,
                        fatal=True,
                    )
                )
                continue
            try:
                item_scope = scope_from_mapping(dict(raw.get("scope") or {}))
                item = ArtifactLineage(
                    scope=item_scope,
                    artifact_id=str(raw["artifact_id"]),
                    role=ArtifactRole(str(raw["role"])),
                    uri=str(raw.get("uri") or ""),
                    sha256=str(raw["sha256"]),
                    size_bytes=int(raw.get("size_bytes") or 0),
                    receipt_id=str(raw.get("receipt_id") or ""),
                    created_at=str(raw.get("created_at") or ""),
                    source_event_ids=tuple(raw.get("source_event_ids") or ()),
                    source_record_ids=tuple(raw.get("source_record_ids") or ()),
                    parent_artifact_ids=tuple(raw.get("parent_artifact_ids") or ()),
                    media_type=str(raw.get("media_type") or "application/octet-stream"),
                    quarantined=bool(raw.get("quarantined")),
                    metadata=dict(raw.get("metadata") or {}),
                )
            except (KeyError, TypeError, ValueError) as error:
                issues.append(
                    ReplayIssue(
                        code="artifact_lineage_invalid",
                        summary=str(error),
                        sequence=record.sequence,
                        record_id=record.record_id,
                        fatal=True,
                    )
                )
                continue
            if item.scope != scope:
                issues.append(
                    ReplayIssue(
                        code="artifact_scope_mismatch",
                        summary="artifact lineage crosses replay scope",
                        sequence=record.sequence,
                        record_id=record.record_id,
                        fatal=True,
                    )
                )
                continue
            values.append(item)
        return tuple(values)

    def _signals(
        self,
        scope: ObservationScope,
        records: Sequence[Any],
        issues: list[ReplayIssue],
    ) -> tuple[WatchdogSignal, ...]:
        values: list[WatchdogSignal] = []
        for record in records:
            if record.kind != HistoryKind.WATCHDOG_SIGNAL:
                continue
            raw = record.payload.get("signal")
            if not isinstance(raw, Mapping):
                continue
            try:
                item = WatchdogSignal(
                    scope=scope_from_mapping(dict(raw.get("scope") or {})),
                    watchdog=WatchdogName(str(raw["watchdog"])),
                    kind=SignalKind(str(raw["kind"])),
                    status=HealthStatus(str(raw["status"])),
                    severity=Severity(str(raw["severity"])),
                    summary=str(raw["summary"]),
                    signal_id=str(raw["signal_id"]),
                    detected_at=str(raw.get("detected_at") or ""),
                    sequence=int(raw.get("sequence") or 0),
                    retryable=bool(raw.get("retryable")),
                    terminal=bool(raw.get("terminal")),
                    evidence_event_ids=tuple(raw.get("evidence_event_ids") or ()),
                    artifact_ids=tuple(raw.get("artifact_ids") or ()),
                    metadata=dict(raw.get("metadata") or {}),
                )
            except (KeyError, TypeError, ValueError) as error:
                issues.append(
                    ReplayIssue(
                        code="watchdog_signal_invalid",
                        summary=str(error),
                        sequence=record.sequence,
                        record_id=record.record_id,
                        fatal=False,
                    )
                )
                continue
            if item.scope == scope:
                values.append(item)
        return tuple(values)
