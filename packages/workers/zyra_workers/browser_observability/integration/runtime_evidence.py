from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import EventRecord, EventType

from ..models import (
    HealthStatus,
    ObservationScope,
    Severity,
    SignalKind,
    TraceSpan,
    TraceSpanKind,
    TraceStatus,
    WatchdogName,
    WatchdogSignal,
    digest_value,
    new_observation_id,
    utc_now,
)
from .contracts import (
    BrowserIntegrationOutput,
    EvidenceSource,
    EvidenceTerminalState,
    RuntimeEvidenceEnvelope,
)


class RuntimeEvidenceMappingError(RuntimeError):
    code = "browser_runtime_evidence_mapping_error"


class StreamPhase(StrEnum):
    NEW = "new"
    STREAMING = "streaming"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class RuntimeEvidenceMapperPolicy:
    enabled: bool = True
    strict_partial_sequence: bool = True
    maximum_partials_per_correlation: int = 10_000
    maximum_correlations: int = 2_000
    fail_on_terminal_reopen: bool = True

    def __post_init__(self) -> None:
        if self.maximum_partials_per_correlation < 1:
            raise ValueError("partial stream limit must be positive")
        if self.maximum_correlations < 1:
            raise ValueError("correlation limit must be positive")


@dataclass(slots=True)
class _CorrelationState:
    scope: ObservationScope
    source: EvidenceSource
    correlation_key: str
    phase: StreamPhase = StreamPhase.NEW
    evidence_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    last_sequence: int = 0
    partial_digests: list[str] = field(default_factory=list)
    terminal_state: EvidenceTerminalState = EvidenceTerminalState.NONE
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    tool_call_id: str = ""
    started_at: str = field(default_factory=utc_now)
    finished_at: str = ""
    error: str = ""
    retryable: bool = False
    outcome_unknown: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)

    def projection(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(),
            "source": str(self.source),
            "correlation_key": self.correlation_key,
            "phase": str(self.phase),
            "evidence_ids": list(self.evidence_ids),
            "event_ids": list(self.event_ids),
            "artifact_ids": list(self.artifact_ids),
            "last_sequence": self.last_sequence,
            "partial_count": len(self.partial_digests),
            "terminal_state": str(self.terminal_state),
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "tool_call_id": self.tool_call_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "retryable": self.retryable,
            "outcome_unknown": self.outcome_unknown,
            "attributes": dict(self.attributes),
        }


class RuntimeEvidenceMapper:
    """Map supplementary OMP-shaped evidence into Zyra trace/health contracts.

    The mapper owns no upstream lifecycle or JSONL store. Disabling it removes
    only supplementary spans/signals; browser action/history/artifact owners
    continue unchanged.
    """

    def __init__(
        self,
        *,
        policy: RuntimeEvidenceMapperPolicy | None = None,
    ) -> None:
        self.policy = policy or RuntimeEvidenceMapperPolicy()
        self._states: dict[str, _CorrelationState] = {}
        self._completed: list[_CorrelationState] = []
        self._mapped = 0
        self._failures = 0

    def map(
        self,
        scope: ObservationScope,
        values: Sequence[RuntimeEvidenceEnvelope | Mapping[str, Any]],
    ) -> BrowserIntegrationOutput:
        if not self.policy.enabled:
            return BrowserIntegrationOutput(
                projection={
                    "runtime_evidence_mapper": {
                        "enabled": False,
                        "mapped": 0,
                        "canonical_browser_path_affected": False,
                    }
                }
            )
        evidence = tuple(
            item
            if isinstance(item, RuntimeEvidenceEnvelope)
            else RuntimeEvidenceEnvelope.from_mapping(scope, item)
            for item in values
        )
        if any(item.scope != scope for item in evidence):
            raise RuntimeEvidenceMappingError("runtime evidence crosses browser scope")
        spans: list[TraceSpan] = []
        signals: list[WatchdogSignal] = []
        events: list[EventRecord] = []
        try:
            for item in evidence:
                terminal_span, terminal_signal = self._consume(item)
                if terminal_span is not None:
                    spans.append(terminal_span)
                if terminal_signal is not None:
                    signals.append(terminal_signal)
                events.append(self._event(item, terminal_span, terminal_signal))
                self._mapped += 1
        except Exception:
            self._failures += 1
            raise
        return BrowserIntegrationOutput(
            evidence=evidence,
            spans=tuple(spans),
            signals=tuple(signals),
            events=tuple(events),
            projection={"runtime_evidence_mapper": self.projection(scope=scope)},
        )

    def projection(self, *, scope: ObservationScope | None = None) -> dict[str, Any]:
        active = tuple(
            item
            for item in self._states.values()
            if scope is None or item.scope == scope
        )
        completed = tuple(
            item
            for item in self._completed
            if scope is None or item.scope == scope
        )
        sources: dict[str, int] = defaultdict(int)
        for item in (*active, *completed):
            sources[str(item.source)] += 1
        return {
            "schema": "zyra.browser-observability.runtime-evidence-mapper.v1",
            "enabled": self.policy.enabled,
            "mapped": self._mapped,
            "failures": self._failures,
            "active_correlations": len(active),
            "completed_correlations": len(completed),
            "sources": dict(sorted(sources.items())),
            "active": [item.projection() for item in active[-100:]],
            "completed": [item.projection() for item in completed[-100:]],
            "canonical_history_owner": "BrowserHistoryStore",
            "canonical_lifecycle_owners": {
                "mcp": "M1-03B",
                "subagent_background": "M1-03C",
                "browser": "M1-04A/04C",
            },
            "recovery_planner_owner": "M1-07C",
        }

    def _consume(
        self,
        evidence: RuntimeEvidenceEnvelope,
    ) -> tuple[TraceSpan | None, WatchdogSignal | None]:
        state = self._states.get(evidence.correlation_key)
        if state is None:
            if len(self._states) >= self.policy.maximum_correlations:
                raise RuntimeEvidenceMappingError("runtime evidence correlation limit exceeded")
            state = _CorrelationState(
                scope=evidence.scope,
                source=evidence.source,
                correlation_key=evidence.correlation_key,
                trace_id=evidence.trace_id or evidence.scope.key,
                span_id=evidence.span_id or new_observation_id("browser-runtime-span"),
                parent_span_id=evidence.parent_span_id,
                tool_call_id=evidence.tool_call_id or evidence.parent_tool_call_id,
            )
            self._states[evidence.correlation_key] = state
        elif state.phase == StreamPhase.TERMINAL and self.policy.fail_on_terminal_reopen:
            raise RuntimeEvidenceMappingError(
                f"terminal evidence correlation {evidence.correlation_key!r} reopened"
            )
        self._validate_source_identity(evidence)
        if evidence.partial_sequence:
            expected = state.last_sequence + 1
            if self.policy.strict_partial_sequence and evidence.partial_sequence != expected:
                raise RuntimeEvidenceMappingError(
                    f"partial sequence must be {expected}, got {evidence.partial_sequence}"
                )
            if len(state.partial_digests) >= self.policy.maximum_partials_per_correlation:
                raise RuntimeEvidenceMappingError("partial stream record limit exceeded")
            state.last_sequence = evidence.partial_sequence
            state.partial_digests.append(evidence.digest)
            state.phase = StreamPhase.STREAMING
        state.evidence_ids.append(evidence.evidence_id)
        if evidence.source_event_id:
            state.event_ids.append(evidence.source_event_id)
        state.event_ids.extend(evidence.correlation_event_ids)
        state.artifact_ids.extend(evidence.artifact_ids)
        state.retryable = state.retryable or evidence.retryable
        state.outcome_unknown = state.outcome_unknown or evidence.outcome_unknown
        state.attributes.update(self._attributes(evidence))
        if not evidence.terminal:
            return None, None
        state.phase = StreamPhase.TERMINAL
        state.terminal_state = evidence.terminal_state
        state.finished_at = evidence.created_at
        state.error = str(
            evidence.payload.get("error")
            or evidence.payload.get("reason")
            or evidence.metadata.get("error")
            or ""
        )
        span = self._span(state)
        signal = self._failure_signal(state, evidence)
        self._states.pop(evidence.correlation_key, None)
        self._completed.append(state)
        self._completed = self._completed[-self.policy.maximum_correlations :]
        return span, signal

    @staticmethod
    def _validate_source_identity(evidence: RuntimeEvidenceEnvelope) -> None:
        if evidence.source == EvidenceSource.PROVIDER_STREAM:
            if evidence.event_type in {"message_update", "content_delta"} and not evidence.partial_sequence:
                raise RuntimeEvidenceMappingError("provider partial lacks sequence")
        if evidence.source == EvidenceSource.MCP:
            if evidence.event_type.startswith("reconnect") and not evidence.mcp_connection_id:
                raise RuntimeEvidenceMappingError("MCP reconnect lacks connection identity")
        if evidence.source == EvidenceSource.SUBAGENT:
            if not evidence.parent_tool_call_id:
                raise RuntimeEvidenceMappingError("subagent evidence lacks parent tool call")
        if evidence.source == EvidenceSource.HASHLINE:
            for key in ("path", "expected_hash", "actual_hash"):
                if not evidence.payload.get(key):
                    raise RuntimeEvidenceMappingError(f"Hashline evidence lacks {key}")
        if evidence.source == EvidenceSource.WORKTREE:
            if not evidence.branch or not evidence.base_sha:
                raise RuntimeEvidenceMappingError("worktree evidence lacks branch/base SHA")

    @staticmethod
    def _attributes(evidence: RuntimeEvidenceEnvelope) -> dict[str, Any]:
        common = {
            "source": str(evidence.source),
            "event_type": evidence.event_type,
            "provider_request_id": evidence.provider_request_id,
            "mcp_server_id": evidence.mcp_server_id,
            "mcp_connection_id": evidence.mcp_connection_id,
            "mcp_request_id": evidence.mcp_request_id,
            "subagent_task_id": evidence.subagent_task_id,
            "parent_tool_call_id": evidence.parent_tool_call_id,
            "background_job_id": evidence.background_job_id,
            "worktree_id": evidence.worktree_id,
            "branch": evidence.branch,
            "base_sha": evidence.base_sha,
            "hashline_receipt_id": evidence.hashline_receipt_id,
            "outcome_unknown": evidence.outcome_unknown,
            "retryable": evidence.retryable,
        }
        if evidence.source == EvidenceSource.HASHLINE:
            common.update(
                {
                    "path": evidence.payload.get("path"),
                    "expected_hash": evidence.payload.get("expected_hash"),
                    "actual_hash": evidence.payload.get("actual_hash"),
                    "hash_recognized": evidence.payload.get("hash_recognized"),
                }
            )
        if evidence.source == EvidenceSource.WORKTREE:
            common.update(
                {
                    "conflict_paths": evidence.payload.get("conflict_paths") or [],
                    "patch_artifact_id": evidence.payload.get("patch_artifact_id") or "",
                }
            )
        return {
            key: value
            for key, value in common.items()
            if value is not None and value != ""
        }

    @staticmethod
    def _span(state: _CorrelationState) -> TraceSpan:
        kind = {
            EvidenceSource.PROVIDER_STREAM: TraceSpanKind.PROVIDER,
            EvidenceSource.MCP: TraceSpanKind.MCP,
            EvidenceSource.SUBAGENT: TraceSpanKind.SUBAGENT,
            EvidenceSource.BACKGROUND_TASK: TraceSpanKind.SUBAGENT,
            EvidenceSource.HASHLINE: TraceSpanKind.TOOL_RESULT,
            EvidenceSource.WORKTREE: TraceSpanKind.TOOL_RESULT,
        }.get(state.source, TraceSpanKind.BROWSER_ACTION)
        status = {
            EvidenceTerminalState.COMPLETED: TraceStatus.OK,
            EvidenceTerminalState.CANCELLED: TraceStatus.CANCELLED,
            EvidenceTerminalState.INTERRUPTED: TraceStatus.CANCELLED,
            EvidenceTerminalState.FAILED: TraceStatus.ERROR,
            EvidenceTerminalState.EXHAUSTED: TraceStatus.ERROR,
            EvidenceTerminalState.CONFLICT: TraceStatus.ERROR,
        }.get(state.terminal_state, TraceStatus.UNKNOWN)
        return TraceSpan(
            scope=state.scope,
            kind=kind,
            name=f"runtime_evidence:{state.source}",
            status=status,
            span_id=state.span_id,
            trace_id=state.trace_id,
            parent_span_id=state.parent_span_id,
            started_at=state.started_at,
            finished_at=state.finished_at,
            tool_call_id=state.tool_call_id,
            input_digest=digest_value(state.partial_digests),
            output_digest=digest_value(
                {
                    "terminal_state": str(state.terminal_state),
                    "artifact_ids": state.artifact_ids,
                }
            ),
            error_code=(
                str(state.terminal_state)
                if state.terminal_state
                not in {EvidenceTerminalState.NONE, EvidenceTerminalState.COMPLETED}
                else ""
            ),
            event_ids=tuple(dict.fromkeys(state.event_ids)),
            artifact_ids=tuple(dict.fromkeys(state.artifact_ids)),
            attributes={
                **state.attributes,
                "partial_count": len(state.partial_digests),
                "last_partial_sequence": state.last_sequence,
                "terminal_state": str(state.terminal_state),
                "error": state.error,
            },
        )

    @staticmethod
    def _failure_signal(
        state: _CorrelationState,
        terminal: RuntimeEvidenceEnvelope,
    ) -> WatchdogSignal | None:
        if state.terminal_state == EvidenceTerminalState.COMPLETED:
            return None
        summary_by_source = {
            EvidenceSource.PROVIDER_STREAM: "Provider stream ended before a clean terminal message.",
            EvidenceSource.MCP: "MCP connection or request exhausted its lifecycle.",
            EvidenceSource.SUBAGENT: "Subagent execution ended without successful completion.",
            EvidenceSource.BACKGROUND_TASK: "Background task ended without successful completion.",
            EvidenceSource.HASHLINE: "Hashline receipt detected stale or conflicting file state.",
            EvidenceSource.WORKTREE: "Worktree operation reported an unresolved conflict.",
        }
        return WatchdogSignal(
            scope=state.scope,
            watchdog=WatchdogName.LOCAL_BROWSER,
            kind=SignalKind.TOOL_FAILED,
            status=HealthStatus.UNHEALTHY,
            severity=Severity.ERROR,
            summary=state.error or summary_by_source.get(state.source, "Runtime evidence reported a failure."),
            sequence=max(1, state.last_sequence),
            retryable=state.retryable,
            terminal=True,
            evidence_event_ids=tuple(dict.fromkeys(state.event_ids)),
            artifact_ids=tuple(dict.fromkeys(state.artifact_ids)),
            metadata={
                "runtime_evidence_source": str(state.source),
                "correlation_key": state.correlation_key,
                "terminal_state": str(state.terminal_state),
                "outcome_unknown": state.outcome_unknown,
                "evidence_id": terminal.evidence_id,
                "supplementary_mapper": True,
            },
        )

    @staticmethod
    def _event(
        evidence: RuntimeEvidenceEnvelope,
        span: TraceSpan | None,
        signal: WatchdogSignal | None,
    ) -> EventRecord:
        return EventRecord(
            run_id=evidence.scope.run_id,
            task_id=evidence.scope.task_id,
            node_id=evidence.scope.node_id or None,
            event_type=(
                EventType.WORKER_HEALTH
                if signal is not None
                else EventType.BROWSER_RUNTIME_DIAGNOSTIC
            ),
            payload={
                "browser_runtime_evidence": evidence.to_dict(),
                "trace_span_id": span.span_id if span else "",
                "worker_health_signal_id": signal.signal_id if signal else "",
                "recovery_planner_owner": "M1-07C",
                "is_recovery_plan": False,
            },
        )
