from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_streaming import ToolStreamFrameKind, ToolStreamingBatchTrace


class ToolConcurrencyMode(StrEnum):
    CONCURRENT_READ_ONLY = "concurrent_read_only"
    SERIAL_READ_ONLY = "serial_read_only"
    SERIAL_NON_READ_ONLY = "serial_non_read_only"
    UNKNOWN = "unknown"


class ToolConcurrencyStatus(StrEnum):
    PASS = "pass"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ToolConcurrencyFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ToolConcurrencyFinding:
    code: str
    severity: ToolConcurrencyFindingSeverity
    message: str
    trace_id: str = ""
    batch_index: int = 0
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolConcurrencyFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "blocking": self.blocking,
            "message": self.message,
            "trace_id": self.trace_id,
            "batch_index": self.batch_index,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolConcurrencyBatchObservation:
    observation_id: str
    trace_id: str
    batch_index: int
    mode: ToolConcurrencyMode
    tool_count: int
    read_only_count: int
    write_count: int
    concurrency_safe_count: int
    conflict_keys: tuple[str, ...]
    conflict_protected: bool
    max_parallel_width: int
    dispatch_count: int
    completion_count: int
    source_path: str = "packages/runtime/zyra_runtime/tool_runtime_concurrency.py"
    upstream_signal: str = "runTools partitionToolCalls runToolsConcurrently runToolsSerially"
    created_at: str = field(default_factory=now_iso)

    @property
    def read_only_batch(self) -> bool:
        return self.tool_count > 0 and self.read_only_count == self.tool_count

    @property
    def has_write(self) -> bool:
        return self.write_count > 0

    @property
    def dispatch_complete(self) -> bool:
        return self.dispatch_count == self.tool_count and self.completion_count == self.tool_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "trace_id": self.trace_id,
            "batch_index": self.batch_index,
            "mode": str(self.mode),
            "tool_count": self.tool_count,
            "read_only_count": self.read_only_count,
            "write_count": self.write_count,
            "concurrency_safe_count": self.concurrency_safe_count,
            "conflict_keys": list(self.conflict_keys),
            "conflict_protected": self.conflict_protected,
            "max_parallel_width": self.max_parallel_width,
            "dispatch_count": self.dispatch_count,
            "completion_count": self.completion_count,
            "read_only_batch": self.read_only_batch,
            "has_write": self.has_write,
            "dispatch_complete": self.dispatch_complete,
            "source_path": self.source_path,
            "upstream_signal": self.upstream_signal,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolConcurrencyReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    observations: tuple[ToolConcurrencyBatchObservation, ...]
    findings: tuple[ToolConcurrencyFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolConcurrencyStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolConcurrencyStatus.BLOCKED
        if self.findings:
            return ToolConcurrencyStatus.DEGRADED
        return ToolConcurrencyStatus.PASS

    @property
    def batch_count(self) -> int:
        return len(self.observations)

    @property
    def concurrent_batch_count(self) -> int:
        return sum(1 for item in self.observations if item.mode == ToolConcurrencyMode.CONCURRENT_READ_ONLY)

    @property
    def serial_write_batch_count(self) -> int:
        return sum(1 for item in self.observations if item.mode == ToolConcurrencyMode.SERIAL_NON_READ_ONLY)

    @property
    def conflict_protected_count(self) -> int:
        return sum(1 for item in self.observations if item.conflict_protected)

    @property
    def max_parallel_width(self) -> int:
        return max((item.max_parallel_width for item in self.observations), default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "batch_count": self.batch_count,
            "concurrent_batch_count": self.concurrent_batch_count,
            "serial_write_batch_count": self.serial_write_batch_count,
            "conflict_protected_count": self.conflict_protected_count,
            "max_parallel_width": self.max_parallel_width,
            "observations": [item.to_dict() for item in self.observations],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_concurrency_report_id": self.report_id,
            "tool_concurrency_owner_unit": self.owner_unit,
            "tool_concurrency_ok": str(self.ok).lower(),
            "tool_concurrency_status": str(self.status),
            "tool_concurrency_batches": str(self.batch_count),
            "tool_concurrency_concurrent_batches": str(self.concurrent_batch_count),
            "tool_concurrency_serial_write_batches": str(self.serial_write_batch_count),
            "tool_concurrency_conflict_protected": str(self.conflict_protected_count),
            "tool_concurrency_max_parallel_width": str(self.max_parallel_width),
            "tool_concurrency_findings": str(len(self.findings)),
        }


class ToolConcurrencyRuntime:
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
        traces: Sequence[ToolStreamingBatchTrace],
    ) -> ToolConcurrencyReport:
        observations = tuple(self._observe(trace) for trace in traces)
        findings = tuple(self._findings(observations))
        return ToolConcurrencyReport(
            report_id=new_id("toolconcurrency"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            observations=observations,
            findings=findings,
        )

    def event_for_report(
        self,
        report: ToolConcurrencyReport,
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
                    "phase": "tool_concurrency_report",
                    "tool_concurrency_report": report.to_dict(),
                }
            },
        )

    def _observe(self, trace: ToolStreamingBatchTrace) -> ToolConcurrencyBatchObservation:
        dispatch_frames = [frame for frame in trace.frames if frame.kind == ToolStreamFrameKind.TOOL_DISPATCHED]
        completion_frames = [frame for frame in trace.frames if frame.kind == ToolStreamFrameKind.TOOL_COMPLETED]
        read_only_count = 0
        write_count = 0
        concurrency_safe_count = 0
        conflict_keys: list[str] = []
        for frame in dispatch_frames:
            payload = dict(frame.payload)
            if payload.get("read_only") is True:
                read_only_count += 1
            else:
                write_count += 1
            if payload.get("concurrency_safe") is True:
                concurrency_safe_count += 1
            conflict_key = str(payload.get("conflict_key") or "")
            if conflict_key:
                conflict_keys.append(conflict_key)
        mode = _mode_from_string(trace.execution_mode)
        batch_payload = _first_payload(trace, ToolStreamFrameKind.BATCH_QUEUED)
        conflict_protected = batch_payload.get("conflict_protected") is True
        max_parallel_width = trace.tool_count if mode == ToolConcurrencyMode.CONCURRENT_READ_ONLY else 1 if trace.tool_count else 0
        return ToolConcurrencyBatchObservation(
            observation_id=new_id("toolconcurrencybatch"),
            trace_id=trace.trace_id,
            batch_index=trace.batch_index,
            mode=mode,
            tool_count=trace.tool_count,
            read_only_count=read_only_count,
            write_count=write_count,
            concurrency_safe_count=concurrency_safe_count,
            conflict_keys=tuple(dict.fromkeys(conflict_keys)),
            conflict_protected=conflict_protected,
            max_parallel_width=max_parallel_width,
            dispatch_count=len(dispatch_frames),
            completion_count=len(completion_frames),
        )

    def _findings(
        self,
        observations: Sequence[ToolConcurrencyBatchObservation],
    ) -> list[ToolConcurrencyFinding]:
        findings: list[ToolConcurrencyFinding] = []
        for item in observations:
            if not item.dispatch_complete:
                findings.append(
                    ToolConcurrencyFinding(
                        code="TOOL_CONCURRENCY_DISPATCH_COMPLETION_MISMATCH",
                        severity=ToolConcurrencyFindingSeverity.BLOCKER,
                        message="Batch dispatch/completion frames do not match planned tool count.",
                        trace_id=item.trace_id,
                        batch_index=item.batch_index,
                        metadata={
                            "tool_count": str(item.tool_count),
                            "dispatch_count": str(item.dispatch_count),
                            "completion_count": str(item.completion_count),
                        },
                    )
                )
            if item.mode == ToolConcurrencyMode.CONCURRENT_READ_ONLY and not item.read_only_batch:
                findings.append(
                    ToolConcurrencyFinding(
                        code="TOOL_CONCURRENCY_WRITE_IN_CONCURRENT_BATCH",
                        severity=ToolConcurrencyFindingSeverity.BLOCKER,
                        message="A concurrent batch contains a non-read-only tool.",
                        trace_id=item.trace_id,
                        batch_index=item.batch_index,
                    )
                )
            if item.mode == ToolConcurrencyMode.SERIAL_NON_READ_ONLY and item.tool_count > 1:
                findings.append(
                    ToolConcurrencyFinding(
                        code="TOOL_CONCURRENCY_WRITE_BATCH_NOT_SERIALIZED",
                        severity=ToolConcurrencyFindingSeverity.BLOCKER,
                        message="A non-read-only batch contains more than one tool.",
                        trace_id=item.trace_id,
                        batch_index=item.batch_index,
                        metadata={"tool_count": str(item.tool_count)},
                    )
                )
            if item.mode == ToolConcurrencyMode.UNKNOWN:
                findings.append(
                    ToolConcurrencyFinding(
                        code="TOOL_CONCURRENCY_UNKNOWN_BATCH_MODE",
                        severity=ToolConcurrencyFindingSeverity.WARNING,
                        message="A batch has an unknown execution mode.",
                        trace_id=item.trace_id,
                        batch_index=item.batch_index,
                    )
                )
            duplicate_conflicts = item.tool_count > 1 and len(item.conflict_keys) < item.write_count
            if duplicate_conflicts and not item.conflict_protected:
                findings.append(
                    ToolConcurrencyFinding(
                        code="TOOL_CONCURRENCY_UNPROTECTED_CONFLICT_KEY",
                        severity=ToolConcurrencyFindingSeverity.BLOCKER,
                        message="Potential write conflict was not marked as conflict-protected.",
                        trace_id=item.trace_id,
                        batch_index=item.batch_index,
                    )
                )
        return findings


def tool_concurrency_metadata(report: ToolConcurrencyReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_concurrency_ok": "true",
            "tool_concurrency_batches": "0",
        }
    return report.metadata()


def render_tool_concurrency_markdown(report: ToolConcurrencyReport) -> str:
    lines = [
        "## Tool Concurrency Runtime",
        "",
        f"- owner_unit: `{report.owner_unit}`",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- batches: `{report.batch_count}`",
        f"- concurrent_batches: `{report.concurrent_batch_count}`",
        f"- serial_write_batches: `{report.serial_write_batch_count}`",
        f"- max_parallel_width: `{report.max_parallel_width}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    lines.extend(["", "### Observations", ""])
    for item in report.observations:
        lines.append(
            f"- batch `{item.batch_index}` `{item.mode}`: tools `{item.tool_count}`, "
            f"read `{item.read_only_count}`, write `{item.write_count}`"
        )
    return "\n".join(lines)


def _mode_from_string(value: str) -> ToolConcurrencyMode:
    try:
        return ToolConcurrencyMode(value)
    except ValueError:
        return ToolConcurrencyMode.UNKNOWN


def _first_payload(trace: ToolStreamingBatchTrace, kind: ToolStreamFrameKind) -> dict[str, Any]:
    for frame in trace.frames:
        if frame.kind == kind:
            return dict(to_jsonable(frame.payload))
    return {}
