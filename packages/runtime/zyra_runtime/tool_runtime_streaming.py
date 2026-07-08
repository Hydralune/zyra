from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_loop import ToolLoopBatch
from .tool_runtime_foundation import (
    TOOL_LOOP_FOUNDATION_OWNER_UNIT,
    TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ToolExecutionReceipt,
    ToolExecutionRuntime,
    ToolRuntimeDisabledError,
    ToolUseContext,
)


class ToolStreamFrameKind(StrEnum):
    BATCH_QUEUED = "batch_queued"
    BATCH_STARTED = "batch_started"
    TOOL_DISPATCHED = "tool_dispatched"
    TOOL_COMPLETED = "tool_completed"
    TOOL_RESULT_BUDGETED = "tool_result_budgeted"
    TOOL_FAILURE_OBSERVED = "tool_failure_observed"
    CONTEXT_MODIFIER_APPLIED = "context_modifier_applied"
    BATCH_COMPLETED = "batch_completed"
    BATCH_FAILED = "batch_failed"


class ToolStreamFrameStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    APPLIED = "applied"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolStreamingFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ToolStreamFrame:
    frame_id: str
    trace_id: str
    sequence: int
    kind: ToolStreamFrameKind
    status: ToolStreamFrameStatus
    worker_request_id: str
    session_id: str
    turn_id: str
    turn_index: int
    batch_index: int
    tool_call_id: str = ""
    tool_name: str = ""
    step_index: int = 0
    message: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    source_path: str = "packages/runtime/zyra_runtime/tool_runtime_streaming.py"
    upstream_signal: str = "StreamingToolExecutor"
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "trace_id": self.trace_id,
            "sequence": self.sequence,
            "kind": str(self.kind),
            "status": str(self.status),
            "worker_request_id": self.worker_request_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "batch_index": self.batch_index,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "step_index": self.step_index,
            "message": self.message,
            "payload": to_jsonable(dict(self.payload)),
            "source_path": self.source_path,
            "upstream_signal": self.upstream_signal,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolStreamingFinding:
    code: str
    severity: ToolStreamingFindingSeverity
    message: str
    trace_id: str = ""
    tool_call_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolStreamingFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "blocking": self.blocking,
            "message": self.message,
            "trace_id": self.trace_id,
            "tool_call_id": self.tool_call_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolStreamingBatchTrace:
    trace_id: str
    owner_unit: str
    runtime_id: str
    worker_request_id: str
    session_id: str
    turn_id: str
    turn_index: int
    batch_index: int
    execution_mode: str
    tool_names: tuple[str, ...]
    frames: tuple[ToolStreamFrame, ...]
    receipts: tuple[ToolExecutionReceipt, ...]
    started_at: str
    completed_at: str

    @property
    def ok(self) -> bool:
        return all(receipt.bounded_result.ok for receipt in self.receipts)

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def tool_count(self) -> int:
        return len(self.tool_names)

    @property
    def budgeted_count(self) -> int:
        return sum(1 for receipt in self.receipts if receipt.budget_decision.applied)

    @property
    def failure_count(self) -> int:
        return sum(1 for receipt in self.receipts if not receipt.bounded_result.ok or receipt.failure_signal is not None)

    @property
    def modifier_application_count(self) -> int:
        return sum(len(receipt.modifier_applications) for receipt in self.receipts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "worker_request_id": self.worker_request_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "batch_index": self.batch_index,
            "execution_mode": self.execution_mode,
            "tool_names": list(self.tool_names),
            "ok": self.ok,
            "tool_count": self.tool_count,
            "frame_count": self.frame_count,
            "budgeted_count": self.budgeted_count,
            "failure_count": self.failure_count,
            "modifier_application_count": self.modifier_application_count,
            "frames": [frame.to_dict() for frame in self.frames],
            "receipts": [receipt.to_dict() for receipt in self.receipts],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_streaming_trace_id": self.trace_id,
            "tool_streaming_trace_ok": str(self.ok).lower(),
            "tool_streaming_trace_frames": str(self.frame_count),
            "tool_streaming_trace_tools": str(self.tool_count),
            "tool_streaming_trace_budgeted": str(self.budgeted_count),
            "tool_streaming_trace_failures": str(self.failure_count),
            "tool_streaming_trace_modifiers": str(self.modifier_application_count),
        }


@dataclass(frozen=True, slots=True)
class ToolStreamingReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    worker_request_id: str
    session_id: str
    traces: tuple[ToolStreamingBatchTrace, ...]
    findings: tuple[ToolStreamingFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def trace_count(self) -> int:
        return len(self.traces)

    @property
    def frame_count(self) -> int:
        return sum(trace.frame_count for trace in self.traces)

    @property
    def tool_count(self) -> int:
        return sum(trace.tool_count for trace in self.traces)

    @property
    def budgeted_count(self) -> int:
        return sum(trace.budgeted_count for trace in self.traces)

    @property
    def failure_count(self) -> int:
        return sum(trace.failure_count for trace in self.traces)

    @property
    def modifier_application_count(self) -> int:
        return sum(trace.modifier_application_count for trace in self.traces)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "worker_request_id": self.worker_request_id,
            "session_id": self.session_id,
            "ok": self.ok,
            "trace_count": self.trace_count,
            "frame_count": self.frame_count,
            "tool_count": self.tool_count,
            "budgeted_count": self.budgeted_count,
            "failure_count": self.failure_count,
            "modifier_application_count": self.modifier_application_count,
            "traces": [trace.to_dict() for trace in self.traces],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_streaming_report_id": self.report_id,
            "tool_streaming_owner_unit": self.owner_unit,
            "tool_streaming_ok": str(self.ok).lower(),
            "tool_streaming_traces": str(self.trace_count),
            "tool_streaming_frames": str(self.frame_count),
            "tool_streaming_tools": str(self.tool_count),
            "tool_streaming_budgeted": str(self.budgeted_count),
            "tool_streaming_failures": str(self.failure_count),
            "tool_streaming_modifiers": str(self.modifier_application_count),
            "tool_streaming_findings": str(len(self.findings)),
        }


class ToolStreamingRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def execute_batch(
        self,
        execution_runtime: ToolExecutionRuntime,
        batch: ToolLoopBatch,
        *,
        tool_context: ToolUseContext,
        max_workers: int,
    ) -> ToolStreamingBatchTrace:
        trace_id = new_id("toolstream")
        started_at = now_iso()
        frames: list[ToolStreamFrame] = []
        sequence = 0

        def add(
            kind: ToolStreamFrameKind,
            status: ToolStreamFrameStatus,
            *,
            tool_call_id: str = "",
            tool_name: str = "",
            step_index: int = 0,
            message: str = "",
            payload: Mapping[str, Any] | None = None,
        ) -> None:
            nonlocal sequence
            sequence += 1
            frames.append(
                ToolStreamFrame(
                    frame_id=new_id("toolframe"),
                    trace_id=trace_id,
                    sequence=sequence,
                    kind=kind,
                    status=status,
                    worker_request_id=tool_context.worker_request_id,
                    session_id=tool_context.session_id,
                    turn_id=tool_context.turn_id,
                    turn_index=tool_context.turn_index,
                    batch_index=batch.batch_index,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    step_index=step_index,
                    message=message,
                    payload=dict(payload or {}),
                )
            )

        add(
            ToolStreamFrameKind.BATCH_QUEUED,
            ToolStreamFrameStatus.PENDING,
            message="Tool batch queued for execution.",
            payload={
                "execution_mode": str(batch.execution_mode),
                "tool_count": len(batch.requests),
                "read_only": batch.read_only,
                "conflict_protected": batch.conflict_protected,
                "conflict_keys": list(batch.conflict_keys),
            },
        )
        add(
            ToolStreamFrameKind.BATCH_STARTED,
            ToolStreamFrameStatus.RUNNING,
            message="Tool batch entered the execution runtime.",
            payload={
                "max_workers": max(1, int(max_workers)),
                "tool_names": [request.tool_name for request in batch.requests],
            },
        )
        for request in batch.requests:
            add(
                ToolStreamFrameKind.TOOL_DISPATCHED,
                ToolStreamFrameStatus.RUNNING,
                tool_call_id=request.call.tool_call_id,
                tool_name=request.tool_name,
                step_index=request.step_index,
                message=f"Dispatched tool {request.tool_name}.",
                payload={
                    "read_only": request.read_only,
                    "access_mode": str(request.access_mode),
                    "concurrency_safe": request.concurrency_safe,
                    "schema_valid": request.valid,
                    "conflict_key": request.conflict_key,
                },
            )

        try:
            receipts = execution_runtime.execute_batch(
                batch,
                tool_context=tool_context,
                max_workers=max(1, int(max_workers)),
            )
        except ToolRuntimeDisabledError:
            add(
                ToolStreamFrameKind.BATCH_FAILED,
                ToolStreamFrameStatus.FAILED,
                message="Tool execution runtime was disabled.",
                payload={"error": "tool_runtime_disabled"},
            )
            raise
        except Exception as exc:
            add(
                ToolStreamFrameKind.BATCH_FAILED,
                ToolStreamFrameStatus.FAILED,
                message="Tool batch raised before receipts were settled.",
                payload={"error": type(exc).__name__, "detail": str(exc)},
            )
            raise

        for receipt in receipts:
            request = receipt.request
            result = receipt.bounded_result
            add(
                ToolStreamFrameKind.TOOL_COMPLETED,
                ToolStreamFrameStatus.COMPLETED if result.ok else ToolStreamFrameStatus.FAILED,
                tool_call_id=request.call.tool_call_id,
                tool_name=request.tool_name,
                step_index=request.step_index,
                message=f"Completed tool {request.tool_name}.",
                payload={
                    "ok": result.ok,
                    "error": result.error,
                    "summary": result.summary,
                    "artifact_ids": [artifact.artifact_id for artifact in result.artifacts],
                    "executor_name": receipt.executor_name,
                },
            )
            if receipt.budget_decision.applied:
                add(
                    ToolStreamFrameKind.TOOL_RESULT_BUDGETED,
                    ToolStreamFrameStatus.APPLIED,
                    tool_call_id=request.call.tool_call_id,
                    tool_name=request.tool_name,
                    step_index=request.step_index,
                    message="Tool result was shaped by the result budget runtime.",
                    payload={
                        "budget_decision": receipt.budget_decision.to_dict(),
                        "budget_receipt": receipt.budget_receipt.to_dict(),
                    },
                )
            if receipt.failure_signal is not None:
                add(
                    ToolStreamFrameKind.TOOL_FAILURE_OBSERVED,
                    ToolStreamFrameStatus.APPLIED,
                    tool_call_id=request.call.tool_call_id,
                    tool_name=request.tool_name,
                    step_index=request.step_index,
                    message="Tool failure signal was observed on the streaming path.",
                    payload={"failure_signal": receipt.failure_signal.to_dict()},
                )
            for application in receipt.modifier_applications:
                add(
                    ToolStreamFrameKind.CONTEXT_MODIFIER_APPLIED,
                    ToolStreamFrameStatus.APPLIED,
                    tool_call_id=request.call.tool_call_id,
                    tool_name=request.tool_name,
                    step_index=request.step_index,
                    message="Tool context modifier was applied after batch execution.",
                    payload={"modifier": dict(application)},
                )

        add(
            ToolStreamFrameKind.BATCH_COMPLETED,
            ToolStreamFrameStatus.COMPLETED if all(receipt.bounded_result.ok for receipt in receipts) else ToolStreamFrameStatus.FAILED,
            message="Tool batch completed with settled receipts.",
            payload={
                "receipt_count": len(receipts),
                "budgeted_count": sum(1 for receipt in receipts if receipt.budget_decision.applied),
                "failure_count": sum(1 for receipt in receipts if not receipt.bounded_result.ok or receipt.failure_signal is not None),
                "modifier_application_count": sum(len(receipt.modifier_applications) for receipt in receipts),
            },
        )
        return ToolStreamingBatchTrace(
            trace_id=trace_id,
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            worker_request_id=tool_context.worker_request_id,
            session_id=tool_context.session_id,
            turn_id=tool_context.turn_id,
            turn_index=tool_context.turn_index,
            batch_index=batch.batch_index,
            execution_mode=str(batch.execution_mode),
            tool_names=tuple(request.tool_name for request in batch.requests),
            frames=tuple(frames),
            receipts=tuple(receipts),
            started_at=started_at,
            completed_at=now_iso(),
        )

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        traces: Sequence[ToolStreamingBatchTrace],
    ) -> ToolStreamingReport:
        findings = tuple(self._findings(traces))
        return ToolStreamingReport(
            report_id=new_id("toolstreamreport"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            worker_request_id=worker_request_id,
            session_id=session_id,
            traces=tuple(traces),
            findings=findings,
        )

    def events_for_trace(
        self,
        trace: ToolStreamingBatchTrace,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> list[EventRecord]:
        return [
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "session_id": trace.session_id,
                        "worker_request_id": trace.worker_request_id,
                        "phase": "tool_stream_frame",
                        "tool_stream_frame": frame.to_dict(),
                    }
                },
            )
            for frame in trace.frames
        ]

    def event_for_report(
        self,
        report: ToolStreamingReport,
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
                    "phase": "tool_streaming_report",
                    "tool_streaming_report": report.to_dict(),
                }
            },
        )

    def _findings(self, traces: Sequence[ToolStreamingBatchTrace]) -> Iterable[ToolStreamingFinding]:
        if not traces:
            yield ToolStreamingFinding(
                code="TOOL_STREAMING_NO_TRACES",
                severity=ToolStreamingFindingSeverity.WARNING,
                message="No tool streaming traces were produced for this session.",
            )
            return
        for trace in traces:
            kinds = {frame.kind for frame in trace.frames}
            if ToolStreamFrameKind.BATCH_STARTED not in kinds or ToolStreamFrameKind.BATCH_COMPLETED not in kinds:
                yield ToolStreamingFinding(
                    code="TOOL_STREAMING_BATCH_BOUNDARY_INCOMPLETE",
                    severity=ToolStreamingFindingSeverity.BLOCKER,
                    message="A streaming trace is missing a batch start or completion frame.",
                    trace_id=trace.trace_id,
                )
            dispatched = [frame for frame in trace.frames if frame.kind == ToolStreamFrameKind.TOOL_DISPATCHED]
            completed = [frame for frame in trace.frames if frame.kind == ToolStreamFrameKind.TOOL_COMPLETED]
            if len(dispatched) != trace.tool_count:
                yield ToolStreamingFinding(
                    code="TOOL_STREAMING_DISPATCH_COUNT_MISMATCH",
                    severity=ToolStreamingFindingSeverity.BLOCKER,
                    message="Streaming dispatch frame count does not match the planned tool count.",
                    trace_id=trace.trace_id,
                    metadata={"expected": str(trace.tool_count), "actual": str(len(dispatched))},
                )
            if len(completed) != len(trace.receipts):
                yield ToolStreamingFinding(
                    code="TOOL_STREAMING_COMPLETION_COUNT_MISMATCH",
                    severity=ToolStreamingFindingSeverity.BLOCKER,
                    message="Streaming completion frame count does not match settled receipts.",
                    trace_id=trace.trace_id,
                    metadata={"expected": str(len(trace.receipts)), "actual": str(len(completed))},
                )
            for receipt in trace.receipts:
                tool_call_id = receipt.request.call.tool_call_id
                has_completion = any(
                    frame.kind == ToolStreamFrameKind.TOOL_COMPLETED and frame.tool_call_id == tool_call_id
                    for frame in trace.frames
                )
                if not has_completion:
                    yield ToolStreamingFinding(
                        code="TOOL_STREAMING_RECEIPT_WITHOUT_COMPLETION_FRAME",
                        severity=ToolStreamingFindingSeverity.BLOCKER,
                        message="A settled receipt lacks a matching streaming completion frame.",
                        trace_id=trace.trace_id,
                        tool_call_id=tool_call_id,
                    )


def tool_streaming_metadata(report: ToolStreamingReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_streaming_ok": "true",
            "tool_streaming_traces": "0",
            "tool_streaming_frames": "0",
        }
    return report.metadata()


def render_tool_streaming_markdown(report: ToolStreamingReport) -> str:
    lines = [
        "## Tool Streaming Runtime",
        "",
        f"- owner_unit: `{report.owner_unit}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- traces: `{report.trace_count}`",
        f"- frames: `{report.frame_count}`",
        f"- tools: `{report.tool_count}`",
        f"- budgeted: `{report.budgeted_count}`",
        f"- failures: `{report.failure_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    lines.extend(["", "### Traces", ""])
    for trace in report.traces:
        lines.append(
            f"- `{trace.trace_id}` batch `{trace.batch_index}` mode `{trace.execution_mode}`: "
            f"{trace.tool_count} tools, {trace.frame_count} frames"
        )
    return "\n".join(lines)
