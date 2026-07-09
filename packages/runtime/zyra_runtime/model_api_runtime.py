from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .runtime_budget_state import (
    CODEWORKER_API_FOUNDATION_RUNTIME_ID,
    M1_02D_OWNER_UNIT,
    RuntimeBudgetState,
)


class ModelStreamFrameKind(StrEnum):
    REQUEST_START = "request_start"
    MESSAGE_START = "message_start"
    CONTENT_DELTA = "content_delta"
    MESSAGE_DELTA_PATCH = "message_delta_patch"
    USAGE_PATCH = "usage_patch"
    MESSAGE_STOP = "message_stop"
    STREAM_ERROR = "stream_error"
    STREAM_STALL = "stream_stall"
    NON_STREAMING_FALLBACK = "non_streaming_fallback"


class ModelStreamStatus(StrEnum):
    READY = "ready"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FALLBACK_COMPLETED = "fallback_completed"
    ERRORED = "errored"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class ApiErrorKind(StrEnum):
    NONE = "none"
    PROMPT_TOO_LONG = "prompt_too_long"
    RATE_LIMIT = "rate_limit"
    MODEL_UNAVAILABLE = "model_unavailable"
    TOOL_USE_RESULT_MISMATCH = "tool_use_result_mismatch"
    STREAM_STALL = "stream_stall"
    AUTH = "auth"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class ApiRetryStatus(StrEnum):
    READY = "ready"
    NOT_NEEDED = "not_needed"
    RETRIED = "retried"
    FALLBACK_SELECTED = "fallback_selected"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class ApiRetryDecisionKind(StrEnum):
    NO_RETRY = "no_retry"
    RETRY_SAME_MODEL = "retry_same_model"
    RETRY_FALLBACK_MODEL = "retry_fallback_model"
    REDUCE_PROMPT_AND_RETRY = "reduce_prompt_and_retry"
    FAIL_FAST = "fail_fast"
    BLOCKED = "blocked"


class ModelApiSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ModelApiSurface(StrEnum):
    REQUEST = "request"
    STREAM = "stream"
    PATCH = "patch"
    USAGE = "usage"
    RETRY = "retry"
    FALLBACK = "fallback"
    BUDGET_STATE = "budget_state"


@dataclass(frozen=True, slots=True)
class ModelApiFinding:
    code: str
    severity: ModelApiSeverity
    surface: ModelApiSurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ModelApiSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelRequestEnvelope:
    request_id: str
    session_id: str
    worker_request_id: str
    turn_id: str
    turn_index: int
    model: str
    messages: tuple[Mapping[str, Any], ...]
    context_chars: int
    context_limit_chars: int
    tool_call_count: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def input_tokens_estimate(self) -> int:
        message_chars = sum(len(str(message.get("content") or "")) for message in self.messages)
        return max(1, (message_chars + max(0, self.context_chars)) // 4)

    @property
    def prompt_too_long(self) -> bool:
        return self.context_limit_chars > 0 and self.context_chars > self.context_limit_chars

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "model": self.model,
            "messages": [to_jsonable(message) for message in self.messages],
            "context_chars": self.context_chars,
            "context_limit_chars": self.context_limit_chars,
            "tool_call_count": self.tool_call_count,
            "input_tokens_estimate": self.input_tokens_estimate,
            "prompt_too_long": self.prompt_too_long,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelStreamFrame:
    frame_id: str
    kind: ModelStreamFrameKind
    sequence: int
    request_id: str
    turn_index: int
    delta: str = ""
    usage_patch: dict[str, int] = field(default_factory=dict)
    error_kind: ApiErrorKind = ApiErrorKind.NONE
    stop_reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def error(self) -> bool:
        return self.error_kind != ApiErrorKind.NONE or self.kind in {
            ModelStreamFrameKind.STREAM_ERROR,
            ModelStreamFrameKind.STREAM_STALL,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "kind": str(self.kind),
            "sequence": self.sequence,
            "request_id": self.request_id,
            "turn_index": self.turn_index,
            "delta": self.delta,
            "usage_patch": dict(self.usage_patch),
            "error_kind": str(self.error_kind),
            "error": self.error,
            "stop_reason": self.stop_reason,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ModelUsagePatch:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelStreamReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    envelope: ModelRequestEnvelope
    status: ModelStreamStatus
    frames: tuple[ModelStreamFrame, ...]
    usage: ModelUsagePatch
    findings: tuple[ModelApiFinding, ...]
    assistant_message: str = ""
    stop_reason: str = ""
    error_kind: ApiErrorKind = ApiErrorKind.NONE
    disabled: bool = False
    fallback_used: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return (
            not self.disabled
            and self.status not in {ModelStreamStatus.BLOCKED, ModelStreamStatus.DISABLED, ModelStreamStatus.ERRORED}
            and not any(finding.blocking for finding in self.findings)
        )

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def error_frame_count(self) -> int:
        return sum(1 for frame in self.frames if frame.error)

    @property
    def retryable_error(self) -> bool:
        return self.error_kind in {
            ApiErrorKind.RATE_LIMIT,
            ApiErrorKind.MODEL_UNAVAILABLE,
            ApiErrorKind.STREAM_STALL,
            ApiErrorKind.TIMEOUT,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.model_stream_runtime.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "fallback_used": self.fallback_used,
            "envelope": self.envelope.to_dict(),
            "frames": [frame.to_dict() for frame in self.frames],
            "frame_count": self.frame_count,
            "error_frame_count": self.error_frame_count,
            "usage": self.usage.to_dict(),
            "assistant_message": self.assistant_message,
            "stop_reason": self.stop_reason,
            "error_kind": str(self.error_kind),
            "retryable_error": self.retryable_error,
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "model_stream_report_id": self.report_id,
            "model_stream_owner_unit": self.owner_unit,
            "model_stream_runtime_id": self.runtime_id,
            "model_stream_ok": str(self.ok).lower(),
            "model_stream_status": str(self.status),
            "model_stream_disabled": str(self.disabled).lower(),
            "model_stream_model": self.envelope.model,
            "model_stream_turn_index": str(self.envelope.turn_index),
            "model_stream_frames": str(self.frame_count),
            "model_stream_error_frames": str(self.error_frame_count),
            "model_stream_error_kind": str(self.error_kind),
            "model_stream_retryable_error": str(self.retryable_error).lower(),
            "model_stream_input_tokens": str(self.usage.input_tokens),
            "model_stream_output_tokens": str(self.usage.output_tokens),
            "model_stream_total_tokens": str(self.usage.total_tokens),
            "model_stream_cost_usd": f"{self.usage.estimated_cost_usd:.8f}",
            "model_stream_findings": str(len(self.findings)),
            "model_stream_fallback_used": str(self.fallback_used).lower(),
        }


@dataclass(frozen=True, slots=True)
class ApiRetryPolicy:
    max_attempts: int = 3
    fallback_models: tuple[str, ...] = ("zyra-local-fallback",)
    retry_rate_limit: bool = True
    retry_model_unavailable: bool = True
    retry_stream_stall: bool = True
    retry_timeout: bool = True
    reduce_prompt_on_prompt_too_long: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "fallback_models": list(self.fallback_models),
            "retry_rate_limit": self.retry_rate_limit,
            "retry_model_unavailable": self.retry_model_unavailable,
            "retry_stream_stall": self.retry_stream_stall,
            "retry_timeout": self.retry_timeout,
            "reduce_prompt_on_prompt_too_long": self.reduce_prompt_on_prompt_too_long,
        }


@dataclass(frozen=True, slots=True)
class ApiRetryAttempt:
    attempt_id: str
    attempt_index: int
    model: str
    decision: ApiRetryDecisionKind
    error_kind: ApiErrorKind
    retryable: bool
    fallback_model: str = ""
    delay_ms: int = 0
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def fallback_selected(self) -> bool:
        return bool(self.fallback_model)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_index": self.attempt_index,
            "model": self.model,
            "decision": str(self.decision),
            "error_kind": str(self.error_kind),
            "retryable": self.retryable,
            "fallback_model": self.fallback_model,
            "fallback_selected": self.fallback_selected,
            "delay_ms": self.delay_ms,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ApiRetryReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    status: ApiRetryStatus
    policy: ApiRetryPolicy
    attempts: tuple[ApiRetryAttempt, ...]
    findings: tuple[ModelApiFinding, ...]
    final_model: str = ""
    recovered: bool = False
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return (
            not self.disabled
            and self.status not in {ApiRetryStatus.BLOCKED, ApiRetryStatus.DISABLED, ApiRetryStatus.EXHAUSTED}
            and not any(finding.blocking for finding in self.findings)
        )

    @property
    def retry_count(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.decision != ApiRetryDecisionKind.NO_RETRY)

    @property
    def fallback_used(self) -> bool:
        return any(attempt.fallback_selected for attempt in self.attempts)

    @property
    def error_kinds(self) -> tuple[str, ...]:
        return tuple(sorted({str(attempt.error_kind) for attempt in self.attempts if attempt.error_kind != ApiErrorKind.NONE}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.api_retry_runtime.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "policy": self.policy.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "retry_count": self.retry_count,
            "fallback_used": self.fallback_used,
            "final_model": self.final_model,
            "recovered": self.recovered,
            "error_kinds": list(self.error_kinds),
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "api_retry_report_id": self.report_id,
            "api_retry_owner_unit": self.owner_unit,
            "api_retry_runtime_id": self.runtime_id,
            "api_retry_ok": str(self.ok).lower(),
            "api_retry_status": str(self.status),
            "api_retry_disabled": str(self.disabled).lower(),
            "api_retry_attempts": str(len(self.attempts)),
            "api_retry_retry_count": str(self.retry_count),
            "api_retry_fallback_used": str(self.fallback_used).lower(),
            "api_retry_recovered": str(self.recovered).lower(),
            "api_retry_final_model": self.final_model,
            "api_retry_error_kinds": ",".join(self.error_kinds),
            "api_retry_findings": str(len(self.findings)),
        }


class ModelStreamRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled

    def build_envelope(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        turn_id: str,
        turn_index: int,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        context_chars: int,
        context_limit_chars: int,
        tool_call_count: int,
        metadata: Mapping[str, str] | None = None,
    ) -> ModelRequestEnvelope:
        return ModelRequestEnvelope(
            request_id=new_id("model_req"),
            session_id=session_id,
            worker_request_id=worker_request_id,
            turn_id=turn_id,
            turn_index=turn_index,
            model=model or "zyra-local-code-model",
            messages=tuple(messages),
            context_chars=max(0, int(context_chars or 0)),
            context_limit_chars=max(1, int(context_limit_chars or 1)),
            tool_call_count=max(0, int(tool_call_count or 0)),
            metadata={str(k): str(v) for k, v in dict(metadata or {}).items()},
        )

    def stream(
        self,
        *,
        envelope: ModelRequestEnvelope,
        budget_state: RuntimeBudgetState,
        constraints: Mapping[str, Any] | None = None,
    ) -> ModelStreamReport:
        constraints = dict(constraints or {})
        findings: list[ModelApiFinding] = []
        frames: list[ModelStreamFrame] = []
        if self.disabled:
            findings.append(
                ModelApiFinding(
                    code="MODEL_STREAM_RUNTIME_DISABLED",
                    severity=ModelApiSeverity.BLOCKER,
                    surface=ModelApiSurface.STREAM,
                    message="ModelStreamRuntime is disabled; CodeWorker cannot produce model usage or stream patches.",
                )
            )
            return ModelStreamReport(
                report_id=new_id("model_stream"),
                owner_unit=self.owner_unit,
                runtime_id=self.runtime_id,
                envelope=envelope,
                status=ModelStreamStatus.DISABLED,
                frames=tuple(frames),
                usage=ModelUsagePatch(0, 0),
                findings=tuple(findings),
                error_kind=ApiErrorKind.UNKNOWN,
                disabled=True,
            )
        if not budget_state.ok:
            findings.append(
                ModelApiFinding(
                    code="RUNTIME_BUDGET_STATE_NOT_READY_FOR_MODEL_STREAM",
                    severity=ModelApiSeverity.BLOCKER,
                    surface=ModelApiSurface.BUDGET_STATE,
                    message="ModelStreamRuntime requires RuntimeBudgetState before emitting usage patches.",
                )
            )
        requested_error = _error_from_constraints(constraints, envelope)
        seq = 1
        frames.append(_frame(ModelStreamFrameKind.REQUEST_START, seq, envelope, metadata={"model": envelope.model}))
        seq += 1
        frames.append(_frame(ModelStreamFrameKind.MESSAGE_START, seq, envelope, metadata={"role": "assistant"}))
        seq += 1
        if requested_error == ApiErrorKind.STREAM_STALL:
            frames.append(
                _frame(
                    ModelStreamFrameKind.STREAM_STALL,
                    seq,
                    envelope,
                    error_kind=ApiErrorKind.STREAM_STALL,
                    metadata={"watchdog": "stall_timeout"},
                )
            )
            usage = ModelUsagePatch(
                input_tokens=envelope.input_tokens_estimate,
                output_tokens=0,
                estimated_cost_usd=_estimate_cost(envelope.input_tokens_estimate, 0),
                metadata={"partial": "true"},
            )
            budget_state.record_model_usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.estimated_cost_usd,
                turn_index=envelope.turn_index,
                model=envelope.model,
                request_id=envelope.request_id,
            )
            return ModelStreamReport(
                report_id=new_id("model_stream"),
                owner_unit=self.owner_unit,
                runtime_id=self.runtime_id,
                envelope=envelope,
                status=ModelStreamStatus.ERRORED,
                frames=tuple(frames),
                usage=usage,
                findings=tuple(findings),
                stop_reason="stream_stall",
                error_kind=ApiErrorKind.STREAM_STALL,
            )
        if requested_error != ApiErrorKind.NONE:
            frames.append(
                _frame(
                    ModelStreamFrameKind.STREAM_ERROR,
                    seq,
                    envelope,
                    error_kind=requested_error,
                    metadata={"semantic_error": str(requested_error)},
                )
            )
            usage = ModelUsagePatch(
                input_tokens=envelope.input_tokens_estimate,
                output_tokens=0,
                estimated_cost_usd=_estimate_cost(envelope.input_tokens_estimate, 0),
                metadata={"partial": "true"},
            )
            budget_state.record_model_usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.estimated_cost_usd,
                turn_index=envelope.turn_index,
                model=envelope.model,
                request_id=envelope.request_id,
            )
            return ModelStreamReport(
                report_id=new_id("model_stream"),
                owner_unit=self.owner_unit,
                runtime_id=self.runtime_id,
                envelope=envelope,
                status=ModelStreamStatus.ERRORED,
                frames=tuple(frames),
                usage=usage,
                findings=tuple(findings),
                stop_reason=str(requested_error),
                error_kind=requested_error,
            )
        assistant = _assistant_message(envelope)
        for chunk in _chunks(assistant, 80):
            frames.append(_frame(ModelStreamFrameKind.CONTENT_DELTA, seq, envelope, delta=chunk))
            seq += 1
        output_tokens = max(1, len(assistant) // 4)
        usage = ModelUsagePatch(
            input_tokens=envelope.input_tokens_estimate,
            output_tokens=output_tokens,
            cache_read_tokens=max(0, envelope.input_tokens_estimate // 10),
            cache_write_tokens=max(0, envelope.input_tokens_estimate // 20),
            estimated_cost_usd=_estimate_cost(envelope.input_tokens_estimate, output_tokens),
            metadata={"prompt_cache": "deterministic"},
        )
        frames.append(
            _frame(
                ModelStreamFrameKind.MESSAGE_DELTA_PATCH,
                seq,
                envelope,
                metadata={"patch": "assistant_message_content", "chars": str(len(assistant))},
            )
        )
        seq += 1
        frames.append(
            _frame(
                ModelStreamFrameKind.USAGE_PATCH,
                seq,
                envelope,
                usage_patch={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                },
            )
        )
        seq += 1
        frames.append(_frame(ModelStreamFrameKind.MESSAGE_STOP, seq, envelope, stop_reason="end_turn"))
        budget_state.record_model_usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.estimated_cost_usd,
            turn_index=envelope.turn_index,
            model=envelope.model,
            request_id=envelope.request_id,
            metadata={"frame_count": str(len(frames))},
        )
        return ModelStreamReport(
            report_id=new_id("model_stream"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            envelope=envelope,
            status=ModelStreamStatus.COMPLETED,
            frames=tuple(frames),
            usage=usage,
            findings=tuple(findings),
            assistant_message=assistant,
            stop_reason="end_turn",
        )

    def events_for_report(
        self,
        report: ModelStreamReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> list[EventRecord]:
        events: list[EventRecord] = []
        for frame in report.frames:
            events.append(
                EventRecord(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    event_type=EventType.AGENT_MESSAGE,
                    payload={
                        "query_session": {
                            "session_id": report.envelope.session_id,
                            "worker_request_id": report.envelope.worker_request_id,
                            "phase": "model_stream_frame",
                            "model_stream_report_id": report.report_id,
                            "model_stream_frame": frame.to_dict(),
                        }
                    },
                )
            )
        events.append(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "session_id": report.envelope.session_id,
                        "worker_request_id": report.envelope.worker_request_id,
                        "phase": "model_stream_report",
                        "model_stream": report.to_dict(),
                    }
                },
            )
        )
        return events


class ApiRetryRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
        disabled: bool = False,
        policy: ApiRetryPolicy | None = None,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled
        self.policy = policy or ApiRetryPolicy()

    def build_report(
        self,
        *,
        stream_report: ModelStreamReport,
        budget_state: RuntimeBudgetState,
        constraints: Mapping[str, Any] | None = None,
    ) -> ApiRetryReport:
        constraints = dict(constraints or {})
        policy = self._policy_from_constraints(constraints)
        findings: list[ModelApiFinding] = []
        attempts: list[ApiRetryAttempt] = []
        if self.disabled:
            findings.append(
                ModelApiFinding(
                    code="API_RETRY_RUNTIME_DISABLED",
                    severity=ModelApiSeverity.BLOCKER,
                    surface=ModelApiSurface.RETRY,
                    message="ApiRetryRuntime is disabled; stream errors cannot be retried or routed to fallback.",
                )
            )
            return ApiRetryReport(
                report_id=new_id("api_retry"),
                owner_unit=self.owner_unit,
                runtime_id=self.runtime_id,
                session_id=stream_report.envelope.session_id,
                worker_request_id=stream_report.envelope.worker_request_id,
                status=ApiRetryStatus.DISABLED,
                policy=policy,
                attempts=(),
                findings=tuple(findings),
                disabled=True,
            )
        if not budget_state.ok:
            findings.append(
                ModelApiFinding(
                    code="RUNTIME_BUDGET_STATE_NOT_READY_FOR_RETRY",
                    severity=ModelApiSeverity.BLOCKER,
                    surface=ModelApiSurface.BUDGET_STATE,
                    message="ApiRetryRuntime requires RuntimeBudgetState before consuming retry budget.",
                )
            )
        if stream_report.ok and stream_report.error_kind == ApiErrorKind.NONE:
            attempts.append(
                ApiRetryAttempt(
                    attempt_id=new_id("api_attempt"),
                    attempt_index=1,
                    model=stream_report.envelope.model,
                    decision=ApiRetryDecisionKind.NO_RETRY,
                    error_kind=ApiErrorKind.NONE,
                    retryable=False,
                    metadata={"stream_report_id": stream_report.report_id},
                )
            )
            return ApiRetryReport(
                report_id=new_id("api_retry"),
                owner_unit=self.owner_unit,
                runtime_id=self.runtime_id,
                session_id=stream_report.envelope.session_id,
                worker_request_id=stream_report.envelope.worker_request_id,
                status=ApiRetryStatus.NOT_NEEDED,
                policy=policy,
                attempts=tuple(attempts),
                findings=tuple(findings),
                final_model=stream_report.envelope.model,
                recovered=True,
            )
        decision = self._decision_for_error(stream_report.error_kind, policy)
        retryable = decision in {
            ApiRetryDecisionKind.RETRY_SAME_MODEL,
            ApiRetryDecisionKind.RETRY_FALLBACK_MODEL,
            ApiRetryDecisionKind.REDUCE_PROMPT_AND_RETRY,
        }
        fallback_model = ""
        if decision == ApiRetryDecisionKind.RETRY_FALLBACK_MODEL and policy.fallback_models:
            fallback_model = policy.fallback_models[0]
        attempts.append(
            ApiRetryAttempt(
                attempt_id=new_id("api_attempt"),
                attempt_index=1,
                model=stream_report.envelope.model,
                decision=decision,
                error_kind=stream_report.error_kind,
                retryable=retryable,
                fallback_model=fallback_model,
                delay_ms=_retry_delay_ms(stream_report.error_kind),
                metadata={"stream_report_id": stream_report.report_id},
            )
        )
        if retryable:
            budget_state.record_retry(
                reason=str(stream_report.error_kind),
                turn_index=stream_report.envelope.turn_index,
                attempt_id=attempts[-1].attempt_id,
                retryable=True,
                metadata={"decision": str(decision), "fallback_model": fallback_model},
            )
        recovered = retryable and (decision != ApiRetryDecisionKind.FAIL_FAST)
        if not recovered:
            findings.append(
                ModelApiFinding(
                    code="API_ERROR_NOT_RECOVERED",
                    severity=ModelApiSeverity.BLOCKER,
                    surface=ModelApiSurface.RETRY,
                    message=f"API error {stream_report.error_kind} was not recoverable by retry policy.",
                    metadata={"decision": str(decision)},
                )
            )
        status = ApiRetryStatus.RETRIED if retryable else ApiRetryStatus.EXHAUSTED
        if fallback_model:
            status = ApiRetryStatus.FALLBACK_SELECTED
        return ApiRetryReport(
            report_id=new_id("api_retry"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=stream_report.envelope.session_id,
            worker_request_id=stream_report.envelope.worker_request_id,
            status=status,
            policy=policy,
            attempts=tuple(attempts),
            findings=tuple(findings),
            final_model=fallback_model or stream_report.envelope.model,
            recovered=recovered,
        )

    def event_for_report(
        self,
        report: ApiRetryReport,
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
                    "phase": "api_retry_report",
                    "api_retry": report.to_dict(),
                }
            },
        )

    def _policy_from_constraints(self, constraints: Mapping[str, Any]) -> ApiRetryPolicy:
        fallback_models = tuple(_string_list(constraints.get("api_retry_fallback_models"))) or self.policy.fallback_models
        return ApiRetryPolicy(
            max_attempts=max(1, _safe_int(constraints.get("api_retry_max_attempts"), self.policy.max_attempts)),
            fallback_models=fallback_models,
            retry_rate_limit=_truthy(constraints.get("api_retry_rate_limit"), default=self.policy.retry_rate_limit),
            retry_model_unavailable=_truthy(
                constraints.get("api_retry_model_unavailable"),
                default=self.policy.retry_model_unavailable,
            ),
            retry_stream_stall=_truthy(constraints.get("api_retry_stream_stall"), default=self.policy.retry_stream_stall),
            retry_timeout=_truthy(constraints.get("api_retry_timeout"), default=self.policy.retry_timeout),
            reduce_prompt_on_prompt_too_long=_truthy(
                constraints.get("api_retry_reduce_prompt"),
                default=self.policy.reduce_prompt_on_prompt_too_long,
            ),
        )

    def _decision_for_error(self, error_kind: ApiErrorKind, policy: ApiRetryPolicy) -> ApiRetryDecisionKind:
        if error_kind == ApiErrorKind.NONE:
            return ApiRetryDecisionKind.NO_RETRY
        if error_kind == ApiErrorKind.PROMPT_TOO_LONG:
            return ApiRetryDecisionKind.REDUCE_PROMPT_AND_RETRY if policy.reduce_prompt_on_prompt_too_long else ApiRetryDecisionKind.FAIL_FAST
        if error_kind == ApiErrorKind.RATE_LIMIT:
            return ApiRetryDecisionKind.RETRY_SAME_MODEL if policy.retry_rate_limit else ApiRetryDecisionKind.FAIL_FAST
        if error_kind == ApiErrorKind.MODEL_UNAVAILABLE:
            return ApiRetryDecisionKind.RETRY_FALLBACK_MODEL if policy.retry_model_unavailable and policy.fallback_models else ApiRetryDecisionKind.FAIL_FAST
        if error_kind == ApiErrorKind.STREAM_STALL:
            return ApiRetryDecisionKind.RETRY_SAME_MODEL if policy.retry_stream_stall else ApiRetryDecisionKind.FAIL_FAST
        if error_kind == ApiErrorKind.TIMEOUT:
            return ApiRetryDecisionKind.RETRY_SAME_MODEL if policy.retry_timeout else ApiRetryDecisionKind.FAIL_FAST
        if error_kind in {ApiErrorKind.AUTH, ApiErrorKind.TOOL_USE_RESULT_MISMATCH}:
            return ApiRetryDecisionKind.FAIL_FAST
        return ApiRetryDecisionKind.FAIL_FAST


def model_stream_metadata(report: ModelStreamReport | None) -> dict[str, str]:
    if report is None:
        return {"model_stream_ok": "false", "model_stream_status": "missing", "model_stream_report_id": ""}
    return report.metadata()


def api_retry_metadata(report: ApiRetryReport | None) -> dict[str, str]:
    if report is None:
        return {"api_retry_ok": "false", "api_retry_status": "missing", "api_retry_report_id": ""}
    return report.metadata()


def render_model_stream_markdown(report: ModelStreamReport) -> str:
    lines = [
        "# Model Stream Runtime",
        "",
        f"- report_id: {report.report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- model: {report.envelope.model}",
        f"- error_kind: {report.error_kind}",
        f"- frames: {report.frame_count}",
        f"- usage: input={report.usage.input_tokens} output={report.usage.output_tokens}",
        "",
        "## Frames",
    ]
    for frame in report.frames:
        lines.append(f"- {frame.sequence} {frame.kind} error={str(frame.error).lower()} stop={frame.stop_reason}")
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def render_api_retry_markdown(report: ApiRetryReport) -> str:
    lines = [
        "# API Retry Runtime",
        "",
        f"- report_id: {report.report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- retry_count: {report.retry_count}",
        f"- fallback_used: {str(report.fallback_used).lower()}",
        f"- final_model: {report.final_model}",
        "",
        "## Attempts",
    ]
    for attempt in report.attempts:
        lines.append(
            f"- {attempt.attempt_index} {attempt.decision} error={attempt.error_kind} retryable={str(attempt.retryable).lower()} fallback={attempt.fallback_model}"
        )
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def _frame(
    kind: ModelStreamFrameKind,
    sequence: int,
    envelope: ModelRequestEnvelope,
    *,
    delta: str = "",
    usage_patch: Mapping[str, int] | None = None,
    error_kind: ApiErrorKind = ApiErrorKind.NONE,
    stop_reason: str = "",
    metadata: Mapping[str, str] | None = None,
) -> ModelStreamFrame:
    return ModelStreamFrame(
        frame_id=new_id("model_frame"),
        kind=kind,
        sequence=sequence,
        request_id=envelope.request_id,
        turn_index=envelope.turn_index,
        delta=delta,
        usage_patch={str(key): int(value) for key, value in dict(usage_patch or {}).items()},
        error_kind=error_kind,
        stop_reason=stop_reason,
        metadata={str(k): str(v) for k, v in dict(metadata or {}).items()},
    )


def _error_from_constraints(constraints: Mapping[str, Any], envelope: ModelRequestEnvelope) -> ApiErrorKind:
    raw = (
        constraints.get("model_stream_error_kind")
        or constraints.get("simulate_model_error")
        or constraints.get("api_error_kind")
        or ""
    )
    if _truthy(constraints.get("simulate_stream_stall")):
        return ApiErrorKind.STREAM_STALL
    if envelope.prompt_too_long:
        return ApiErrorKind.PROMPT_TOO_LONG
    value = str(raw).strip().lower()
    aliases = {
        "rate-limit": ApiErrorKind.RATE_LIMIT,
        "rate_limit": ApiErrorKind.RATE_LIMIT,
        "model-unavailable": ApiErrorKind.MODEL_UNAVAILABLE,
        "model_unavailable": ApiErrorKind.MODEL_UNAVAILABLE,
        "prompt-too-long": ApiErrorKind.PROMPT_TOO_LONG,
        "prompt_too_long": ApiErrorKind.PROMPT_TOO_LONG,
        "tool-use-result-mismatch": ApiErrorKind.TOOL_USE_RESULT_MISMATCH,
        "tool_use_result_mismatch": ApiErrorKind.TOOL_USE_RESULT_MISMATCH,
        "stream-stall": ApiErrorKind.STREAM_STALL,
        "stream_stall": ApiErrorKind.STREAM_STALL,
        "timeout": ApiErrorKind.TIMEOUT,
        "auth": ApiErrorKind.AUTH,
        "unknown": ApiErrorKind.UNKNOWN,
    }
    return aliases.get(value, ApiErrorKind.NONE)


def _assistant_message(envelope: ModelRequestEnvelope) -> str:
    return (
        f"CodeWorker model stream turn {envelope.turn_index} accepted "
        f"{envelope.tool_call_count} planned tool call(s) with {envelope.context_chars} context char(s)."
    )


def _chunks(text: str, size: int) -> list[str]:
    if not text:
        return []
    return [text[index : index + size] for index in range(0, len(text), max(1, size))]


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (max(0, input_tokens) * 0.0000003) + (max(0, output_tokens) * 0.0000012)


def _retry_delay_ms(error_kind: ApiErrorKind) -> int:
    if error_kind == ApiErrorKind.RATE_LIMIT:
        return 750
    if error_kind == ApiErrorKind.MODEL_UNAVAILABLE:
        return 250
    if error_kind in {ApiErrorKind.STREAM_STALL, ApiErrorKind.TIMEOUT}:
        return 500
    return 0


def _string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "force"}
