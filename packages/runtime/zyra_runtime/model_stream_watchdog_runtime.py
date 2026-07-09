from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .model_api_runtime import ApiErrorKind, ApiRetryReport, ModelStreamFrameKind, ModelStreamReport
from .runtime_budget_state import CODEWORKER_API_FOUNDATION_RUNTIME_ID, M1_02D_OWNER_UNIT


class ModelStreamWatchdogStatus(StrEnum):
    READY = "ready"
    RECOVERED = "recovered"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ModelStreamWatchdogSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ModelStreamWatchdogSurface(StrEnum):
    FRAME_SEQUENCE = "frame_sequence"
    STREAM_START = "stream_start"
    MESSAGE_PATCH = "message_patch"
    USAGE_PATCH = "usage_patch"
    STREAM_STOP = "stream_stop"
    STREAM_ERROR = "stream_error"
    RETRY_RECOVERY = "retry_recovery"
    BUDGET_MUTATION = "budget_mutation"


class ModelStreamWatchdogSignalKind(StrEnum):
    COMPLETE = "complete"
    STALL = "stall"
    ERROR = "error"
    LATE_USAGE = "late_usage"
    MISSING_USAGE = "missing_usage"
    MISSING_STOP = "missing_stop"
    RETRY_EXPECTED = "retry_expected"
    RETRY_RECOVERED = "retry_recovered"
    UNRECOVERED = "unrecovered"


@dataclass(frozen=True, slots=True)
class ModelStreamFrameObservation:
    observation_id: str
    report_id: str
    request_id: str
    turn_index: int
    sequence: int
    kind: str
    error_kind: str
    has_delta: bool
    has_usage_patch: bool
    stop_reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.kind in {
            str(ModelStreamFrameKind.MESSAGE_STOP),
            str(ModelStreamFrameKind.STREAM_ERROR),
            str(ModelStreamFrameKind.STREAM_STALL),
        }

    @property
    def errored(self) -> bool:
        return self.error_kind not in {"", str(ApiErrorKind.NONE)} or self.kind in {
            str(ModelStreamFrameKind.STREAM_ERROR),
            str(ModelStreamFrameKind.STREAM_STALL),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "report_id": self.report_id,
            "request_id": self.request_id,
            "turn_index": self.turn_index,
            "sequence": self.sequence,
            "kind": self.kind,
            "error_kind": self.error_kind,
            "has_delta": self.has_delta,
            "has_usage_patch": self.has_usage_patch,
            "stop_reason": self.stop_reason,
            "terminal": self.terminal,
            "errored": self.errored,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelStreamWatchdogSignal:
    signal_id: str
    kind: ModelStreamWatchdogSignalKind
    report_id: str
    turn_index: int
    severity: ModelStreamWatchdogSeverity
    route: str
    recovered: bool = False
    retry_report_id: str = ""
    message: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def blocking(self) -> bool:
        return self.severity == ModelStreamWatchdogSeverity.BLOCKER and not self.recovered

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "kind": str(self.kind),
            "report_id": self.report_id,
            "turn_index": self.turn_index,
            "severity": str(self.severity),
            "route": self.route,
            "recovered": self.recovered,
            "retry_report_id": self.retry_report_id,
            "blocking": self.blocking,
            "message": self.message,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ModelStreamWatchdogFinding:
    code: str
    severity: ModelStreamWatchdogSeverity
    surface: ModelStreamWatchdogSurface
    message: str
    report_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ModelStreamWatchdogSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "report_id": self.report_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelStreamWatchdogReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    observations: tuple[ModelStreamFrameObservation, ...]
    signals: tuple[ModelStreamWatchdogSignal, ...]
    findings: tuple[ModelStreamWatchdogFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(signal.blocking for signal in self.signals) and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ModelStreamWatchdogStatus:
        if not self.ok:
            return ModelStreamWatchdogStatus.BLOCKED
        if any(signal.recovered for signal in self.signals):
            return ModelStreamWatchdogStatus.RECOVERED
        if self.findings:
            return ModelStreamWatchdogStatus.DEGRADED
        return ModelStreamWatchdogStatus.READY

    @property
    def stream_count(self) -> int:
        return len({item.report_id for item in self.observations})

    @property
    def error_signal_count(self) -> int:
        return sum(1 for signal in self.signals if signal.kind in {ModelStreamWatchdogSignalKind.ERROR, ModelStreamWatchdogSignalKind.STALL})

    @property
    def recovered_signal_count(self) -> int:
        return sum(1 for signal in self.signals if signal.recovered)

    @property
    def usage_patch_count(self) -> int:
        return sum(1 for item in self.observations if item.has_usage_patch)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.model_stream_watchdog.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "stream_count": self.stream_count,
            "observation_count": len(self.observations),
            "signal_count": len(self.signals),
            "error_signal_count": self.error_signal_count,
            "recovered_signal_count": self.recovered_signal_count,
            "usage_patch_count": self.usage_patch_count,
            "observations": [item.to_dict() for item in self.observations],
            "signals": [signal.to_dict() for signal in self.signals],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "model_stream_watchdog_report_id": self.report_id,
            "model_stream_watchdog_owner_unit": self.owner_unit,
            "model_stream_watchdog_runtime_id": self.runtime_id,
            "model_stream_watchdog_ok": str(self.ok).lower(),
            "model_stream_watchdog_status": str(self.status),
            "model_stream_watchdog_streams": str(self.stream_count),
            "model_stream_watchdog_observations": str(len(self.observations)),
            "model_stream_watchdog_signals": str(len(self.signals)),
            "model_stream_watchdog_error_signals": str(self.error_signal_count),
            "model_stream_watchdog_recovered_signals": str(self.recovered_signal_count),
            "model_stream_watchdog_usage_patches": str(self.usage_patch_count),
            "model_stream_watchdog_findings": str(len(self.findings)),
        }


class ModelStreamWatchdogRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        stream_reports: Sequence[ModelStreamReport],
        retry_reports: Sequence[ApiRetryReport],
    ) -> ModelStreamWatchdogReport:
        observations: list[ModelStreamFrameObservation] = []
        signals: list[ModelStreamWatchdogSignal] = []
        findings: list[ModelStreamWatchdogFinding] = []
        retry_by_stream = _retry_by_stream_report(retry_reports)
        for stream_report in stream_reports:
            report_observations = self._observe_stream(stream_report)
            observations.extend(report_observations)
            stream_signals = self._signals_for_stream(stream_report, retry_by_stream.get(stream_report.report_id))
            signals.extend(stream_signals)
            findings.extend(self._findings_for_stream(stream_report, report_observations, stream_signals))
        if not stream_reports:
            findings.append(
                ModelStreamWatchdogFinding(
                    code="NO_MODEL_STREAM_REPORTS",
                    severity=ModelStreamWatchdogSeverity.BLOCKER,
                    surface=ModelStreamWatchdogSurface.STREAM_START,
                    message="No ModelStreamReport was available for stream watchdog validation.",
                )
            )
        return ModelStreamWatchdogReport(
            report_id=new_id("stream_watchdog"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            observations=tuple(observations),
            signals=tuple(signals),
            findings=tuple(findings),
            source_decisions=default_model_stream_watchdog_source_decisions(),
        )

    def event_for_report(
        self,
        report: ModelStreamWatchdogReport,
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
                    "phase": "model_stream_watchdog",
                    "model_stream_watchdog": report.to_dict(),
                }
            },
        )

    def signal_events(
        self,
        report: ModelStreamWatchdogReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> list[EventRecord]:
        events: list[EventRecord] = []
        for signal in report.signals:
            if signal.kind not in {
                ModelStreamWatchdogSignalKind.STALL,
                ModelStreamWatchdogSignalKind.ERROR,
                ModelStreamWatchdogSignalKind.RETRY_EXPECTED,
                ModelStreamWatchdogSignalKind.RETRY_RECOVERED,
                ModelStreamWatchdogSignalKind.UNRECOVERED,
            }:
                continue
            events.append(
                EventRecord(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    event_type=EventType.AGENT_MESSAGE,
                    payload={
                        "query_session": {
                            "session_id": report.session_id,
                            "worker_request_id": report.worker_request_id,
                            "phase": "model_stream_watchdog_signal",
                            "model_stream_watchdog_signal": signal.to_dict(),
                            "model_stream_watchdog_report_id": report.report_id,
                        }
                    },
                )
            )
        return events

    def _observe_stream(self, stream_report: ModelStreamReport) -> list[ModelStreamFrameObservation]:
        observations: list[ModelStreamFrameObservation] = []
        for frame in stream_report.frames:
            observations.append(
                ModelStreamFrameObservation(
                    observation_id=new_id("stream_obs"),
                    report_id=stream_report.report_id,
                    request_id=stream_report.envelope.request_id,
                    turn_index=stream_report.envelope.turn_index,
                    sequence=frame.sequence,
                    kind=str(frame.kind),
                    error_kind=str(frame.error_kind),
                    has_delta=bool(frame.delta),
                    has_usage_patch=bool(frame.usage_patch),
                    stop_reason=frame.stop_reason,
                    metadata={"model": stream_report.envelope.model},
                )
            )
        return observations

    def _signals_for_stream(
        self,
        stream_report: ModelStreamReport,
        retry_report: ApiRetryReport | None,
    ) -> list[ModelStreamWatchdogSignal]:
        signals: list[ModelStreamWatchdogSignal] = []
        if stream_report.ok:
            signals.append(
                ModelStreamWatchdogSignal(
                    signal_id=new_id("stream_signal"),
                    kind=ModelStreamWatchdogSignalKind.COMPLETE,
                    report_id=stream_report.report_id,
                    turn_index=stream_report.envelope.turn_index,
                    severity=ModelStreamWatchdogSeverity.INFO,
                    route="complete",
                    message="Model stream completed with a stop frame and usage patch.",
                )
            )
            return signals
        error_kind = stream_report.error_kind
        if error_kind == ApiErrorKind.STREAM_STALL:
            kind = ModelStreamWatchdogSignalKind.STALL
            route = "retry_stream_stall"
        elif error_kind != ApiErrorKind.NONE:
            kind = ModelStreamWatchdogSignalKind.ERROR
            route = "api_retry_runtime"
        else:
            kind = ModelStreamWatchdogSignalKind.UNRECOVERED
            route = "stream_unknown"
        recovered = bool(retry_report and retry_report.recovered)
        signals.append(
            ModelStreamWatchdogSignal(
                signal_id=new_id("stream_signal"),
                kind=kind,
                report_id=stream_report.report_id,
                turn_index=stream_report.envelope.turn_index,
                severity=ModelStreamWatchdogSeverity.WARNING if recovered else ModelStreamWatchdogSeverity.BLOCKER,
                route=route,
                recovered=recovered,
                retry_report_id=retry_report.report_id if retry_report else "",
                message=f"Model stream ended with {error_kind}; recovered={str(recovered).lower()}.",
                metadata={"error_kind": str(error_kind)},
            )
        )
        if recovered:
            signals.append(
                ModelStreamWatchdogSignal(
                    signal_id=new_id("stream_signal"),
                    kind=ModelStreamWatchdogSignalKind.RETRY_RECOVERED,
                    report_id=stream_report.report_id,
                    turn_index=stream_report.envelope.turn_index,
                    severity=ModelStreamWatchdogSeverity.INFO,
                    route=str(retry_report.status) if retry_report else "api_retry",
                    recovered=True,
                    retry_report_id=retry_report.report_id if retry_report else "",
                    message="ApiRetryRuntime recovered the model stream error.",
                )
            )
        else:
            signals.append(
                ModelStreamWatchdogSignal(
                    signal_id=new_id("stream_signal"),
                    kind=ModelStreamWatchdogSignalKind.UNRECOVERED,
                    report_id=stream_report.report_id,
                    turn_index=stream_report.envelope.turn_index,
                    severity=ModelStreamWatchdogSeverity.BLOCKER,
                    route="block_codeworker_api_foundation",
                    recovered=False,
                    retry_report_id=retry_report.report_id if retry_report else "",
                    message="Model stream error was not recovered by ApiRetryRuntime.",
                )
            )
        return signals

    def _findings_for_stream(
        self,
        stream_report: ModelStreamReport,
        observations: Sequence[ModelStreamFrameObservation],
        signals: Sequence[ModelStreamWatchdogSignal],
    ) -> list[ModelStreamWatchdogFinding]:
        findings: list[ModelStreamWatchdogFinding] = []
        kinds = [item.kind for item in observations]
        sequences = [item.sequence for item in observations]
        if sequences != sorted(sequences):
            findings.append(
                ModelStreamWatchdogFinding(
                    code="MODEL_STREAM_FRAME_SEQUENCE_OUT_OF_ORDER",
                    severity=ModelStreamWatchdogSeverity.BLOCKER,
                    surface=ModelStreamWatchdogSurface.FRAME_SEQUENCE,
                    message="Model stream frames are not monotonically ordered.",
                    report_id=stream_report.report_id,
                )
            )
        if str(ModelStreamFrameKind.REQUEST_START) not in kinds:
            findings.append(
                ModelStreamWatchdogFinding(
                    code="MODEL_STREAM_REQUEST_START_MISSING",
                    severity=ModelStreamWatchdogSeverity.BLOCKER,
                    surface=ModelStreamWatchdogSurface.STREAM_START,
                    message="Model stream did not emit a request_start frame.",
                    report_id=stream_report.report_id,
                )
            )
        if stream_report.ok and str(ModelStreamFrameKind.USAGE_PATCH) not in kinds:
            findings.append(
                ModelStreamWatchdogFinding(
                    code="MODEL_STREAM_USAGE_PATCH_MISSING",
                    severity=ModelStreamWatchdogSeverity.BLOCKER,
                    surface=ModelStreamWatchdogSurface.USAGE_PATCH,
                    message="Completed model stream did not emit usage_patch.",
                    report_id=stream_report.report_id,
                )
            )
        if stream_report.ok and str(ModelStreamFrameKind.MESSAGE_STOP) not in kinds:
            findings.append(
                ModelStreamWatchdogFinding(
                    code="MODEL_STREAM_STOP_MISSING",
                    severity=ModelStreamWatchdogSeverity.BLOCKER,
                    surface=ModelStreamWatchdogSurface.STREAM_STOP,
                    message="Completed model stream did not emit message_stop.",
                    report_id=stream_report.report_id,
                )
            )
        if any(signal.blocking for signal in signals):
            findings.append(
                ModelStreamWatchdogFinding(
                    code="MODEL_STREAM_UNRECOVERED_ERROR",
                    severity=ModelStreamWatchdogSeverity.BLOCKER,
                    surface=ModelStreamWatchdogSurface.RETRY_RECOVERY,
                    message="Model stream has an unrecovered error signal.",
                    report_id=stream_report.report_id,
                )
            )
        return findings


def model_stream_watchdog_metadata(report: ModelStreamWatchdogReport | None) -> dict[str, str]:
    if report is None:
        return {
            "model_stream_watchdog_ok": "false",
            "model_stream_watchdog_status": "missing",
            "model_stream_watchdog_report_id": "",
        }
    return report.metadata()


def render_model_stream_watchdog_markdown(report: ModelStreamWatchdogReport) -> str:
    lines = [
        "# Model Stream Watchdog",
        "",
        f"- report_id: {report.report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- streams: {report.stream_count}",
        f"- signals: {len(report.signals)}",
        f"- recovered_signals: {report.recovered_signal_count}",
        "",
        "## Signals",
    ]
    for signal in report.signals:
        lines.append(
            f"- {signal.kind} report={signal.report_id} severity={signal.severity} recovered={str(signal.recovered).lower()} route={signal.route}"
        )
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def default_model_stream_watchdog_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/claude.ts",
            "target_path": "packages/runtime/zyra_runtime/model_stream_watchdog_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "stream frame ordering, terminal frame, usage patch and semantic error watchdog",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/retry-and-errors",
            "target_path": "packages/runtime/zyra_runtime/model_stream_watchdog_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "retry/fallback recovery signal validation",
        },
    )


def _retry_by_stream_report(retry_reports: Sequence[ApiRetryReport]) -> dict[str, ApiRetryReport]:
    mapping: dict[str, ApiRetryReport] = {}
    for report in retry_reports:
        for attempt in report.attempts:
            stream_report_id = attempt.metadata.get("stream_report_id", "")
            if stream_report_id:
                mapping[stream_report_id] = report
    return mapping
