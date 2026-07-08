from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_session_bridge import ToolSessionBridgeReport


class ToolTimelineEventKind(StrEnum):
    SESSION_TOOL_USE = "session_tool_use"
    BATCH_QUEUED = "batch_queued"
    BATCH_STARTED = "batch_started"
    TOOL_DISPATCHED = "tool_dispatched"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_RESULT = "tool_result"
    RESULT_CONTEXT = "result_context"
    PERMISSION_QUESTION = "permission_question"
    BUDGET_SIGNAL = "budget_signal"
    WATCHDOG_SIGNAL = "watchdog_signal"
    BATCH_COMPLETED = "batch_completed"


class ToolTimelineEdgeKind(StrEnum):
    SESSION_TO_DISPATCH = "session_to_dispatch"
    BATCH_ORDER = "batch_order"
    DISPATCH_TO_START = "dispatch_to_start"
    START_TO_COMPLETE = "start_to_complete"
    COMPLETE_TO_RESULT = "complete_to_result"
    RESULT_TO_CONTEXT = "result_to_context"
    BUDGET_TO_WATCHDOG = "budget_to_watchdog"
    PERMISSION_TO_SESSION = "permission_to_session"


class ToolTimelineStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolTimelineFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolTimelineSurface(StrEnum):
    STREAM = "stream"
    RECEIPT = "receipt"
    EVENT_LOG = "event_log"
    SESSION_BRIDGE = "session_bridge"
    BATCH = "batch"
    RESULT_CONTEXT = "result_context"
    PERMISSION = "permission"


@dataclass(frozen=True, slots=True)
class ToolTimelineEvent:
    event_id: str
    kind: ToolTimelineEventKind
    sequence: int
    tool_call_id: str = ""
    tool_name: str = ""
    turn_index: int = 0
    batch_index: int = 0
    step_index: int = 0
    source: str = ""
    timestamp: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> tuple[int, int, int, str, str]:
        return (self.turn_index, self.batch_index, self.step_index, self.tool_call_id, str(self.kind))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": str(self.kind),
            "sequence": self.sequence,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "turn_index": self.turn_index,
            "batch_index": self.batch_index,
            "step_index": self.step_index,
            "source": self.source,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolTimelineEdge:
    edge_id: str
    kind: ToolTimelineEdgeKind
    before_event_id: str
    after_event_id: str
    tool_call_id: str = ""
    batch_index: int = 0
    required: bool = True
    satisfied: bool = True
    reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.required and not self.satisfied

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "kind": str(self.kind),
            "before_event_id": self.before_event_id,
            "after_event_id": self.after_event_id,
            "tool_call_id": self.tool_call_id,
            "batch_index": self.batch_index,
            "required": self.required,
            "satisfied": self.satisfied,
            "blocking": self.blocking,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolTimelineBatchWindow:
    window_id: str
    turn_index: int
    batch_index: int
    execution_mode: str
    tool_call_ids: tuple[str, ...]
    tool_names: tuple[str, ...]
    start_sequence: int
    end_sequence: int
    dispatch_count: int
    completion_count: int
    read_only_count: int
    write_count: int
    conflict_protected: bool
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool(self.tool_call_ids) and self.dispatch_count == len(self.tool_call_ids) and self.completion_count == len(self.tool_call_ids)

    @property
    def concurrent_read_only(self) -> bool:
        return self.execution_mode.endswith("concurrent_read_only") or self.execution_mode == "concurrent_read_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "turn_index": self.turn_index,
            "batch_index": self.batch_index,
            "execution_mode": self.execution_mode,
            "tool_call_ids": list(self.tool_call_ids),
            "tool_names": list(self.tool_names),
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "dispatch_count": self.dispatch_count,
            "completion_count": self.completion_count,
            "read_only_count": self.read_only_count,
            "write_count": self.write_count,
            "conflict_protected": self.conflict_protected,
            "complete": self.complete,
            "concurrent_read_only": self.concurrent_read_only,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolTimelineFinding:
    code: str
    severity: ToolTimelineFindingSeverity
    surface: ToolTimelineSurface
    message: str
    tool_call_id: str = ""
    batch_index: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolTimelineFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "tool_call_id": self.tool_call_id,
            "batch_index": self.batch_index,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionTimelineReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    events: tuple[ToolTimelineEvent, ...]
    edges: tuple[ToolTimelineEdge, ...]
    batch_windows: tuple[ToolTimelineBatchWindow, ...]
    findings: tuple[ToolTimelineFinding, ...] = ()
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings) and not any(edge.blocking for edge in self.edges)

    @property
    def status(self) -> ToolTimelineStatus:
        if any(finding.blocking for finding in self.findings) or any(edge.blocking for edge in self.edges):
            return ToolTimelineStatus.BLOCKED
        if not self.events:
            return ToolTimelineStatus.EMPTY
        if self.findings:
            return ToolTimelineStatus.DEGRADED
        return ToolTimelineStatus.READY

    @property
    def tool_call_count(self) -> int:
        return len({event.tool_call_id for event in self.events if event.tool_call_id})

    @property
    def complete_batch_count(self) -> int:
        return sum(1 for window in self.batch_windows if window.complete)

    @property
    def concurrent_batch_count(self) -> int:
        return sum(1 for window in self.batch_windows if window.concurrent_read_only)

    @property
    def serial_batch_count(self) -> int:
        return len(self.batch_windows) - self.concurrent_batch_count

    @property
    def blocking_edge_count(self) -> int:
        return sum(1 for edge in self.edges if edge.blocking)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_execution_timeline.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "event_count": len(self.events),
            "edge_count": len(self.edges),
            "batch_window_count": len(self.batch_windows),
            "tool_call_count": self.tool_call_count,
            "complete_batch_count": self.complete_batch_count,
            "concurrent_batch_count": self.concurrent_batch_count,
            "serial_batch_count": self.serial_batch_count,
            "blocking_edge_count": self.blocking_edge_count,
            "events": [event.to_dict() for event in self.events],
            "edges": [edge.to_dict() for edge in self.edges],
            "batch_windows": [window.to_dict() for window in self.batch_windows],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_execution_timeline_report_id": self.report_id,
            "tool_execution_timeline_owner_unit": self.owner_unit,
            "tool_execution_timeline_runtime_id": self.runtime_id,
            "tool_execution_timeline_ok": str(self.ok).lower(),
            "tool_execution_timeline_status": str(self.status),
            "tool_execution_timeline_events": str(len(self.events)),
            "tool_execution_timeline_edges": str(len(self.edges)),
            "tool_execution_timeline_batches": str(len(self.batch_windows)),
            "tool_execution_timeline_complete_batches": str(self.complete_batch_count),
            "tool_execution_timeline_concurrent_batches": str(self.concurrent_batch_count),
            "tool_execution_timeline_serial_batches": str(self.serial_batch_count),
            "tool_execution_timeline_tool_calls": str(self.tool_call_count),
            "tool_execution_timeline_blocking_edges": str(self.blocking_edge_count),
            "tool_execution_timeline_findings": str(len(self.findings)),
        }


class ToolExecutionTimelineRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        traces: Sequence[Any],
        receipts: Sequence[Mapping[str, Any]],
        event_records: Sequence[EventRecord],
        session_bridge_report: ToolSessionBridgeReport | None = None,
    ) -> ToolExecutionTimelineReport:
        events = self._events_from_bridge(session_bridge_report)
        events.extend(self._events_from_traces(traces))
        events.extend(self._events_from_receipts(receipts, start_sequence=len(events) + 1))
        events.extend(self._events_from_event_records(event_records, start_sequence=len(events) + 1))
        events = _stable_events(events)
        edges = self._edges(events)
        windows = self._batch_windows(events)
        findings = self._findings(events, edges, windows, receipts)
        return ToolExecutionTimelineReport(
            report_id=new_id("tooltimeline"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            events=tuple(events),
            edges=tuple(edges),
            batch_windows=tuple(windows),
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: ToolExecutionTimelineReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "tool_execution_timeline",
                    "tool_execution_timeline": report.to_dict(),
                }
            },
        )

    def _events_from_bridge(self, report: ToolSessionBridgeReport | None) -> list[ToolTimelineEvent]:
        if report is None:
            return []
        events: list[ToolTimelineEvent] = []
        sequence = 0
        for turn in report.turns:
            for tool_use in turn.tool_uses:
                sequence += 1
                events.append(
                    ToolTimelineEvent(
                        event_id=new_id("tooltimeev"),
                        kind=ToolTimelineEventKind.SESSION_TOOL_USE,
                        sequence=sequence,
                        tool_call_id=tool_use.upstream_tool_use_id,
                        tool_name=tool_use.tool_name,
                        turn_index=turn.turn_index,
                        step_index=tool_use.block_index,
                        source="ToolSessionBridgeRuntime",
                        metadata={
                            "bridge_report_id": report.report_id,
                            "origin": str(tool_use.origin),
                            "source_format": str(tool_use.source_format),
                        },
                    )
                )
        return events

    def _events_from_traces(self, traces: Sequence[Any]) -> list[ToolTimelineEvent]:
        events: list[ToolTimelineEvent] = []
        sequence = 0
        for trace in traces:
            payload = trace.to_dict() if hasattr(trace, "to_dict") else trace
            if not isinstance(payload, Mapping):
                continue
            trace_sequence = _int(payload.get("batch_index")) * 1000
            frames = payload.get("frames") if isinstance(payload.get("frames"), Sequence) else ()
            for frame in frames:
                if not isinstance(frame, Mapping):
                    continue
                sequence += 1
                frame_kind = _frame_kind(frame)
                if frame_kind is None:
                    continue
                events.append(
                    ToolTimelineEvent(
                        event_id=str(frame.get("frame_id") or new_id("tooltimeev")),
                        kind=frame_kind,
                        sequence=trace_sequence + _int(frame.get("sequence") or sequence),
                        tool_call_id=str(frame.get("tool_call_id") or ""),
                        tool_name=str(frame.get("tool_name") or ""),
                        turn_index=_int(frame.get("turn_index") or payload.get("turn_index")),
                        batch_index=_int(frame.get("batch_index") or payload.get("batch_index")),
                        step_index=_int(frame.get("step_index")),
                        source="ToolStreamingRuntime",
                        timestamp=str(frame.get("created_at") or ""),
                        metadata={
                            "trace_id": str(frame.get("trace_id") or payload.get("trace_id") or ""),
                            "status": str(frame.get("status") or ""),
                            "execution_mode": str(payload.get("execution_mode") or ""),
                        },
                    )
                )
        return events

    def _events_from_receipts(self, receipts: Sequence[Mapping[str, Any]], *, start_sequence: int) -> list[ToolTimelineEvent]:
        events: list[ToolTimelineEvent] = []
        sequence = start_sequence
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
            result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
            decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
            tool_call_id = str(request.get("tool_call_id") or result.get("tool_call_id") or "")
            tool_name = str(request.get("tool_name") or "")
            turn_index = _int(request.get("turn_index"))
            step_index = _int(request.get("step_index"))
            metadata = request.get("metadata") if isinstance(request.get("metadata"), Mapping) else {}
            batch_index = _int(metadata.get("batch_index"))
            sequence += 1
            events.append(
                ToolTimelineEvent(
                    event_id=new_id("tooltimeev"),
                    kind=ToolTimelineEventKind.TOOL_STARTED,
                    sequence=sequence,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    turn_index=turn_index,
                    batch_index=batch_index,
                    step_index=step_index,
                    source="ToolExecutionReceipt",
                    timestamp=str(receipt.get("started_at") or ""),
                    metadata={"executor": str(receipt.get("executor_name") or "")},
                )
            )
            sequence += 1
            events.append(
                ToolTimelineEvent(
                    event_id=new_id("tooltimeev"),
                    kind=ToolTimelineEventKind.TOOL_COMPLETED,
                    sequence=sequence,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    turn_index=turn_index,
                    batch_index=batch_index,
                    step_index=step_index,
                    source="ToolExecutionReceipt",
                    timestamp=str(receipt.get("completed_at") or ""),
                    metadata={"ok": str(result.get("ok") is True).lower(), "error": str(result.get("error") or "")},
                )
            )
            if decision.get("applied") is True:
                sequence += 1
                events.append(
                    ToolTimelineEvent(
                        event_id=new_id("tooltimeev"),
                        kind=ToolTimelineEventKind.BUDGET_SIGNAL,
                        sequence=sequence,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        turn_index=turn_index,
                        batch_index=batch_index,
                        step_index=step_index,
                        source="ToolResultBudgetRuntime",
                        metadata={"artifact_id": str(decision.get("artifact_id") or ""), "reason": str(decision.get("reason") or "")},
                    )
                )
        return events

    def _events_from_event_records(self, event_records: Sequence[EventRecord], *, start_sequence: int) -> list[ToolTimelineEvent]:
        events: list[ToolTimelineEvent] = []
        sequence = start_sequence
        for event in event_records:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            query = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
            phase = str(query.get("phase") or "")
            kind = _phase_kind(phase, payload)
            if kind is None:
                continue
            sequence += 1
            tool_result = payload.get("tool_result") if isinstance(payload.get("tool_result"), Mapping) else {}
            tool_call = payload.get("tool_call") if isinstance(payload.get("tool_call"), Mapping) else {}
            tool_call_metadata = tool_call.get("metadata") if isinstance(tool_call.get("metadata"), Mapping) else {}
            events.append(
                ToolTimelineEvent(
                    event_id=str(event.event_id),
                    kind=kind,
                    sequence=sequence,
                    tool_call_id=str(query.get("tool_call_id") or tool_result.get("tool_call_id") or tool_call.get("tool_call_id") or ""),
                    tool_name=str(query.get("tool_name") or tool_result.get("tool_name") or tool_call.get("tool_name") or ""),
                    turn_index=_int(query.get("turn_index") or tool_call_metadata.get("turn_index")),
                    batch_index=_int(query.get("batch_index")),
                    step_index=_int(query.get("step_index") or tool_call_metadata.get("step_index")),
                    source="EventRecord",
                    timestamp=str(event.created_at),
                    metadata={"phase": phase, "event_type": str(event.event_type)},
                )
            )
        return events

    def _edges(self, events: Sequence[ToolTimelineEvent]) -> list[ToolTimelineEdge]:
        by_tool: dict[str, list[ToolTimelineEvent]] = {}
        for event in events:
            if event.tool_call_id:
                by_tool.setdefault(event.tool_call_id, []).append(event)
        edges: list[ToolTimelineEdge] = []
        for tool_call_id, tool_events in by_tool.items():
            tool_events = sorted(tool_events, key=lambda item: item.sequence)
            edges.extend(_required_edge(tool_events, ToolTimelineEventKind.SESSION_TOOL_USE, ToolTimelineEventKind.TOOL_DISPATCHED, ToolTimelineEdgeKind.SESSION_TO_DISPATCH, tool_call_id))
            edges.extend(_required_edge(tool_events, ToolTimelineEventKind.TOOL_DISPATCHED, ToolTimelineEventKind.TOOL_STARTED, ToolTimelineEdgeKind.DISPATCH_TO_START, tool_call_id))
            edges.extend(_required_edge(tool_events, ToolTimelineEventKind.TOOL_STARTED, ToolTimelineEventKind.TOOL_COMPLETED, ToolTimelineEdgeKind.START_TO_COMPLETE, tool_call_id))
            edges.extend(_required_edge(tool_events, ToolTimelineEventKind.TOOL_COMPLETED, ToolTimelineEventKind.TOOL_RESULT, ToolTimelineEdgeKind.COMPLETE_TO_RESULT, tool_call_id))
            edges.extend(_optional_edge(tool_events, ToolTimelineEventKind.TOOL_RESULT, ToolTimelineEventKind.RESULT_CONTEXT, ToolTimelineEdgeKind.RESULT_TO_CONTEXT, tool_call_id))
            edges.extend(_optional_edge(tool_events, ToolTimelineEventKind.BUDGET_SIGNAL, ToolTimelineEventKind.WATCHDOG_SIGNAL, ToolTimelineEdgeKind.BUDGET_TO_WATCHDOG, tool_call_id))
            edges.extend(_optional_edge(tool_events, ToolTimelineEventKind.PERMISSION_QUESTION, ToolTimelineEventKind.RESULT_CONTEXT, ToolTimelineEdgeKind.PERMISSION_TO_SESSION, tool_call_id))
        batch_events: dict[tuple[int, int], list[ToolTimelineEvent]] = {}
        for event in events:
            if event.batch_index:
                batch_events.setdefault((event.turn_index, event.batch_index), []).append(event)
        sorted_batches = sorted(batch_events)
        for before_key, after_key in zip(sorted_batches, sorted_batches[1:], strict=False):
            before = max(batch_events[before_key], key=lambda item: item.sequence)
            after = min(batch_events[after_key], key=lambda item: item.sequence)
            edges.append(
                ToolTimelineEdge(
                    edge_id=new_id("tooltimeedge"),
                    kind=ToolTimelineEdgeKind.BATCH_ORDER,
                    before_event_id=before.event_id,
                    after_event_id=after.event_id,
                    batch_index=after_key[1],
                    required=True,
                    satisfied=before.sequence <= after.sequence,
                    reason="batch index order must be observable in event sequence",
                )
            )
        return edges

    def _batch_windows(self, events: Sequence[ToolTimelineEvent]) -> list[ToolTimelineBatchWindow]:
        grouped: dict[tuple[int, int], list[ToolTimelineEvent]] = {}
        for event in events:
            if event.batch_index:
                grouped.setdefault((event.turn_index, event.batch_index), []).append(event)
        windows: list[ToolTimelineBatchWindow] = []
        for (turn_index, batch_index), batch_events in sorted(grouped.items()):
            batch_events = sorted(batch_events, key=lambda item: item.sequence)
            dispatches = [event for event in batch_events if event.kind == ToolTimelineEventKind.TOOL_DISPATCHED]
            completions = [event for event in batch_events if event.kind in {ToolTimelineEventKind.TOOL_COMPLETED, ToolTimelineEventKind.TOOL_RESULT}]
            execution_mode = next((event.metadata.get("execution_mode", "") for event in batch_events if event.metadata.get("execution_mode")), "")
            tool_call_ids = tuple(dict.fromkeys(event.tool_call_id for event in dispatches if event.tool_call_id))
            tool_names = tuple(dict.fromkeys(event.tool_name for event in dispatches if event.tool_name))
            read_only_count = sum(1 for event in dispatches if str(event.metadata.get("read_only") or "").lower() == "true")
            write_count = len(dispatches) - read_only_count
            conflict_protected = any(str(event.metadata.get("conflict_protected") or "").lower() == "true" for event in batch_events)
            windows.append(
                ToolTimelineBatchWindow(
                    window_id=new_id("tooltimewin"),
                    turn_index=turn_index,
                    batch_index=batch_index,
                    execution_mode=execution_mode,
                    tool_call_ids=tool_call_ids,
                    tool_names=tool_names,
                    start_sequence=batch_events[0].sequence,
                    end_sequence=batch_events[-1].sequence,
                    dispatch_count=len(dispatches),
                    completion_count=len({event.tool_call_id for event in completions if event.tool_call_id}),
                    read_only_count=read_only_count,
                    write_count=write_count,
                    conflict_protected=conflict_protected,
                    metadata={"event_count": str(len(batch_events))},
                )
            )
        return windows

    def _findings(
        self,
        events: Sequence[ToolTimelineEvent],
        edges: Sequence[ToolTimelineEdge],
        windows: Sequence[ToolTimelineBatchWindow],
        receipts: Sequence[Mapping[str, Any]],
    ) -> list[ToolTimelineFinding]:
        findings: list[ToolTimelineFinding] = []
        if receipts and not events:
            findings.append(ToolTimelineFinding("TIMELINE_EMPTY_WITH_RECEIPTS", ToolTimelineFindingSeverity.BLOCKER, ToolTimelineSurface.EVENT_LOG, "Receipts exist but no timeline events were built."))
        for edge in edges:
            if edge.blocking:
                findings.append(
                    ToolTimelineFinding(
                        code="TIMELINE_REQUIRED_EDGE_MISSING",
                        severity=ToolTimelineFindingSeverity.BLOCKER,
                        surface=ToolTimelineSurface.EVENT_LOG,
                        message=f"Required timeline edge is not satisfied: {edge.kind}.",
                        tool_call_id=edge.tool_call_id,
                        batch_index=edge.batch_index,
                        metadata={"edge_id": edge.edge_id, "reason": edge.reason},
                    )
                )
        for window in windows:
            if not window.complete:
                findings.append(
                    ToolTimelineFinding(
                        code="TIMELINE_BATCH_INCOMPLETE",
                        severity=ToolTimelineFindingSeverity.ERROR,
                        surface=ToolTimelineSurface.BATCH,
                        message="A tool batch does not have matching dispatch and completion observations.",
                        batch_index=window.batch_index,
                        metadata={"dispatch_count": str(window.dispatch_count), "completion_count": str(window.completion_count)},
                    )
                )
            if window.concurrent_read_only and window.write_count:
                findings.append(
                    ToolTimelineFinding(
                        code="TIMELINE_CONCURRENT_BATCH_HAS_WRITE",
                        severity=ToolTimelineFindingSeverity.BLOCKER,
                        surface=ToolTimelineSurface.BATCH,
                        message="A concurrent read-only batch contains a mutating tool.",
                        batch_index=window.batch_index,
                    )
                )
        return findings


def tool_execution_timeline_metadata(report: ToolExecutionTimelineReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_execution_timeline_ok": "false", "tool_execution_timeline_events": "0"}
    return report.metadata()


def assert_tool_execution_timeline_ready(report: ToolExecutionTimelineReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.findings if finding.blocking) or "blocking edge"
    raise AssertionError(f"tool execution timeline blocked: {blockers}")


def render_tool_execution_timeline_markdown(report: ToolExecutionTimelineReport) -> str:
    lines = [
        "## Tool Execution Timeline",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- events: `{len(report.events)}`",
        f"- edges: `{len(report.edges)}`",
        f"- batches: `{len(report.batch_windows)}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    lines.extend(["", "### Batches", ""])
    for window in report.batch_windows:
        lines.append(f"- batch `{window.batch_index}` mode `{window.execution_mode}` tools `{', '.join(window.tool_names)}` complete `{str(window.complete).lower()}`")
    return "\n".join(lines)


def _stable_events(events: Sequence[ToolTimelineEvent]) -> list[ToolTimelineEvent]:
    ordered = sorted(
        events,
        key=lambda item: (
            item.turn_index,
            _batch_sort(item.batch_index),
            item.step_index,
            item.tool_call_id,
            _phase_order(item.kind),
            item.sequence,
            item.timestamp,
            item.source,
            item.event_id,
        ),
    )
    return [replace(event, sequence=index + 1) for index, event in enumerate(ordered)]


def _batch_sort(batch_index: int) -> int:
    return batch_index if batch_index > 0 else 999999


def _phase_order(kind: ToolTimelineEventKind) -> int:
    order = {
        ToolTimelineEventKind.SESSION_TOOL_USE: 10,
        ToolTimelineEventKind.BATCH_QUEUED: 20,
        ToolTimelineEventKind.BATCH_STARTED: 30,
        ToolTimelineEventKind.TOOL_DISPATCHED: 40,
        ToolTimelineEventKind.TOOL_STARTED: 50,
        ToolTimelineEventKind.PERMISSION_QUESTION: 55,
        ToolTimelineEventKind.TOOL_COMPLETED: 60,
        ToolTimelineEventKind.BUDGET_SIGNAL: 70,
        ToolTimelineEventKind.WATCHDOG_SIGNAL: 80,
        ToolTimelineEventKind.TOOL_RESULT: 90,
        ToolTimelineEventKind.RESULT_CONTEXT: 100,
        ToolTimelineEventKind.BATCH_COMPLETED: 110,
    }
    return order.get(kind, 999)


def _frame_kind(frame: Mapping[str, Any]) -> ToolTimelineEventKind | None:
    kind = str(frame.get("kind") or "").split(".")[-1].lower()
    mapping = {
        "batch_queued": ToolTimelineEventKind.BATCH_QUEUED,
        "batch_started": ToolTimelineEventKind.BATCH_STARTED,
        "tool_dispatched": ToolTimelineEventKind.TOOL_DISPATCHED,
        "tool_completed": ToolTimelineEventKind.TOOL_COMPLETED,
        "tool_result_budgeted": ToolTimelineEventKind.BUDGET_SIGNAL,
        "tool_failure_observed": ToolTimelineEventKind.WATCHDOG_SIGNAL,
        "batch_completed": ToolTimelineEventKind.BATCH_COMPLETED,
    }
    return mapping.get(kind)


def _phase_kind(phase: str, payload: Mapping[str, Any]) -> ToolTimelineEventKind | None:
    if payload.get("tool_result") is not None:
        return ToolTimelineEventKind.TOOL_RESULT
    mapping = {
        "assistant_tool_use_accepted": ToolTimelineEventKind.SESSION_TOOL_USE,
        "tool_call_started": ToolTimelineEventKind.TOOL_STARTED,
        "tool_call_completed": ToolTimelineEventKind.TOOL_COMPLETED,
        "tool_result_session_appended": ToolTimelineEventKind.RESULT_CONTEXT,
        "tool_result_context_projected": ToolTimelineEventKind.RESULT_CONTEXT,
        "tool_permission_question_appended": ToolTimelineEventKind.PERMISSION_QUESTION,
        "tool_result_budget_exceeded": ToolTimelineEventKind.BUDGET_SIGNAL,
        "watchdog_signal": ToolTimelineEventKind.WATCHDOG_SIGNAL,
        "tool_batch_started": ToolTimelineEventKind.BATCH_STARTED,
        "tool_batch_completed": ToolTimelineEventKind.BATCH_COMPLETED,
    }
    return mapping.get(phase)


def _required_edge(
    events: Sequence[ToolTimelineEvent],
    before: ToolTimelineEventKind,
    after: ToolTimelineEventKind,
    kind: ToolTimelineEdgeKind,
    tool_call_id: str,
) -> list[ToolTimelineEdge]:
    before_event = next((event for event in events if event.kind == before), None)
    after_candidates = [event for event in events if event.kind == after]
    after_event = None
    if before_event is not None:
        after_event = next((event for event in after_candidates if event.sequence >= before_event.sequence), None)
    if after_event is None and after_candidates:
        after_event = after_candidates[0]
    if before_event is None and before == ToolTimelineEventKind.SESSION_TOOL_USE:
        return []
    if before_event is None or after_event is None:
        return [
            ToolTimelineEdge(
                edge_id=new_id("tooltimeedge"),
                kind=kind,
                before_event_id=before_event.event_id if before_event else "",
                after_event_id=after_event.event_id if after_event else "",
                tool_call_id=tool_call_id,
                required=True,
                satisfied=False,
                reason=f"{before} must be observed before {after}",
            )
        ]
    return [
        ToolTimelineEdge(
            edge_id=new_id("tooltimeedge"),
            kind=kind,
            before_event_id=before_event.event_id,
            after_event_id=after_event.event_id,
            tool_call_id=tool_call_id,
            required=True,
            satisfied=before_event.sequence <= after_event.sequence,
            reason=f"{before} precedes {after}",
        )
    ]


def _optional_edge(
    events: Sequence[ToolTimelineEvent],
    before: ToolTimelineEventKind,
    after: ToolTimelineEventKind,
    kind: ToolTimelineEdgeKind,
    tool_call_id: str,
) -> list[ToolTimelineEdge]:
    before_event = next((event for event in events if event.kind == before), None)
    after_event = next((event for event in events if event.kind == after), None)
    if before_event is None:
        return []
    return [
        ToolTimelineEdge(
            edge_id=new_id("tooltimeedge"),
            kind=kind,
            before_event_id=before_event.event_id,
            after_event_id=after_event.event_id if after_event else "",
            tool_call_id=tool_call_id,
            required=False,
            satisfied=after_event is not None and before_event.sequence <= after_event.sequence,
            reason=f"optional {before} to {after}",
        )
    ]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
