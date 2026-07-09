from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


M1_02D_MODEL_RECOVERY_OWNER_UNIT = "M1-02D"
CODEWORKER_MODEL_RECOVERY_MATRIX_RUNTIME_ID = "codeworker_model_recovery_matrix_runtime"


class ModelRecoveryStatus(StrEnum):
    READY = "ready"
    RECOVERED = "recovered"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class ModelRecoverySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ModelRecoverySurface(StrEnum):
    MODEL_STREAM = "model_stream"
    API_RETRY = "api_retry"
    FALLBACK = "fallback"
    PROMPT_REDUCTION = "prompt_reduction"
    RUNTIME_BUDGET = "runtime_budget"
    WATCHDOG = "watchdog"
    RESTORE_CONTEXT = "restore_context"


class ModelRecoveryCaseKind(StrEnum):
    CLEAN_STREAM = "clean_stream"
    PROMPT_TOO_LONG = "prompt_too_long"
    STREAM_STALL = "stream_stall"
    MODEL_UNAVAILABLE = "model_unavailable"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNKNOWN_ERROR = "unknown_error"
    DISABLED_RUNTIME = "disabled_runtime"


class ModelRecoveryActionKind(StrEnum):
    NONE = "none"
    RETRY_SAME_MODEL = "retry_same_model"
    RETRY_FALLBACK_MODEL = "retry_fallback_model"
    REDUCE_PROMPT_AND_RETRY = "reduce_prompt_and_retry"
    FAIL_FAST = "fail_fast"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ModelRecoveryFinding:
    code: str
    severity: ModelRecoverySeverity
    surface: ModelRecoverySurface
    message: str
    case_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ModelRecoverySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "case_id": self.case_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelRecoveryAttemptProjection:
    attempt_id: str
    attempt_index: int
    model: str
    action: ModelRecoveryActionKind
    error_kind: str
    retryable: bool
    fallback_model: str = ""
    delay_ms: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def fallback_selected(self) -> bool:
        return bool(self.fallback_model)

    @property
    def prompt_reduction(self) -> bool:
        return self.action == ModelRecoveryActionKind.REDUCE_PROMPT_AND_RETRY

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_index": self.attempt_index,
            "model": self.model,
            "action": str(self.action),
            "error_kind": self.error_kind,
            "retryable": self.retryable,
            "fallback_model": self.fallback_model,
            "fallback_selected": self.fallback_selected,
            "prompt_reduction": self.prompt_reduction,
            "delay_ms": self.delay_ms,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelRecoveryCase:
    case_id: str
    turn_index: int
    request_id: str
    model: str
    kind: ModelRecoveryCaseKind
    stream_status: str
    stream_ok: bool
    error_kind: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    restore_message_count: int
    attempts: tuple[ModelRecoveryAttemptProjection, ...]
    recovered: bool
    final_model: str = ""
    fallback_used: bool = False
    findings: tuple[ModelRecoveryFinding, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def retry_count(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.action != ModelRecoveryActionKind.NONE)

    @property
    def prompt_reduction_count(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.prompt_reduction)

    @property
    def ok(self) -> bool:
        if self.kind == ModelRecoveryCaseKind.CLEAN_STREAM:
            return self.stream_ok
        return self.recovered and not any(finding.blocking for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "turn_index": self.turn_index,
            "request_id": self.request_id,
            "model": self.model,
            "kind": str(self.kind),
            "stream_status": self.stream_status,
            "stream_ok": self.stream_ok,
            "error_kind": self.error_kind,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "restore_message_count": self.restore_message_count,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "retry_count": self.retry_count,
            "prompt_reduction_count": self.prompt_reduction_count,
            "recovered": self.recovered,
            "final_model": self.final_model,
            "fallback_used": self.fallback_used,
            "ok": self.ok,
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelRecoveryMatrixReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    cases: tuple[ModelRecoveryCase, ...]
    findings: tuple[ModelRecoveryFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not self.disabled and all(case.ok for case in self.cases) and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def status(self) -> ModelRecoveryStatus:
        if self.disabled:
            return ModelRecoveryStatus.DISABLED
        if not self.ok:
            return ModelRecoveryStatus.BLOCKED
        if any(case.recovered and case.kind != ModelRecoveryCaseKind.CLEAN_STREAM for case in self.cases):
            return ModelRecoveryStatus.RECOVERED
        if self.findings or any(case.findings for case in self.cases):
            return ModelRecoveryStatus.DEGRADED
        return ModelRecoveryStatus.READY

    @property
    def error_case_count(self) -> int:
        return sum(1 for case in self.cases if case.kind != ModelRecoveryCaseKind.CLEAN_STREAM)

    @property
    def recovered_case_count(self) -> int:
        return sum(1 for case in self.cases if case.recovered)

    @property
    def fallback_count(self) -> int:
        return sum(1 for case in self.cases if case.fallback_used)

    @property
    def prompt_reduction_count(self) -> int:
        return sum(case.prompt_reduction_count for case in self.cases)

    @property
    def restore_sensitive_count(self) -> int:
        return sum(1 for case in self.cases if case.restore_message_count > 0)

    @property
    def total_input_tokens(self) -> int:
        return sum(case.input_tokens for case in self.cases)

    @property
    def total_output_tokens(self) -> int:
        return sum(case.output_tokens for case in self.cases)

    @property
    def total_cost_usd(self) -> float:
        return sum(case.cost_usd for case in self.cases)

    def metadata(self) -> dict[str, str]:
        return {
            "model_recovery_matrix_report_id": self.report_id,
            "model_recovery_matrix_owner_unit": self.owner_unit,
            "model_recovery_matrix_runtime_id": self.runtime_id,
            "model_recovery_matrix_ok": str(self.ok).lower(),
            "model_recovery_matrix_status": str(self.status),
            "model_recovery_matrix_disabled": str(self.disabled).lower(),
            "model_recovery_matrix_cases": str(len(self.cases)),
            "model_recovery_matrix_error_cases": str(self.error_case_count),
            "model_recovery_matrix_recovered_cases": str(self.recovered_case_count),
            "model_recovery_matrix_fallbacks": str(self.fallback_count),
            "model_recovery_matrix_prompt_reductions": str(self.prompt_reduction_count),
            "model_recovery_matrix_restore_sensitive": str(self.restore_sensitive_count),
            "model_recovery_matrix_input_tokens": str(self.total_input_tokens),
            "model_recovery_matrix_output_tokens": str(self.total_output_tokens),
            "model_recovery_matrix_cost_usd": f"{self.total_cost_usd:.8f}",
            "model_recovery_matrix_findings": str(len(self.findings)),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_model_recovery_matrix.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "cases": [case.to_dict() for case in self.cases],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "error_case_count": self.error_case_count,
            "recovered_case_count": self.recovered_case_count,
            "fallback_count": self.fallback_count,
            "prompt_reduction_count": self.prompt_reduction_count,
            "restore_sensitive_count": self.restore_sensitive_count,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 8),
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


class CodeWorkerModelRecoveryMatrixRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_MODEL_RECOVERY_OWNER_UNIT,
        runtime_id: str = CODEWORKER_MODEL_RECOVERY_MATRIX_RUNTIME_ID,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        model_stream_reports: Sequence[Any],
        api_retry_reports: Sequence[Any],
    ) -> ModelRecoveryMatrixReport:
        streams = [_to_mapping(report) for report in model_stream_reports]
        retries = [_to_mapping(report) for report in api_retry_reports]
        cases: list[ModelRecoveryCase] = []
        findings: list[ModelRecoveryFinding] = []
        for index, stream in enumerate(streams):
            retry = retries[index] if index < len(retries) else {}
            case = self._case_from_reports(stream, retry)
            cases.append(case)
            findings.extend(case.findings)
        if self.disabled:
            findings.append(
                ModelRecoveryFinding(
                    code="MODEL_RECOVERY_MATRIX_DISABLED",
                    severity=ModelRecoverySeverity.BLOCKER,
                    surface=ModelRecoverySurface.MODEL_STREAM,
                    message="Model recovery matrix runtime is disabled.",
                )
            )
        if streams and not retries:
            findings.append(
                ModelRecoveryFinding(
                    code="MODEL_STREAM_WITHOUT_RETRY_REPORT",
                    severity=ModelRecoverySeverity.BLOCKER,
                    surface=ModelRecoverySurface.API_RETRY,
                    message="Model stream reports exist without paired ApiRetryRuntime reports.",
                )
            )
        return ModelRecoveryMatrixReport(
            report_id=new_id("model_recovery_matrix"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            cases=tuple(cases),
            findings=tuple(findings),
            source_decisions=default_model_recovery_matrix_source_decisions(),
            disabled=self.disabled,
        )

    def event_for_report(
        self,
        report: ModelRecoveryMatrixReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "codeworker_model_recovery_matrix",
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
                    "phase": phase,
                    "model_recovery_matrix": report.to_dict(),
                }
            },
        )

    def metadata(self, report: ModelRecoveryMatrixReport | None = None) -> dict[str, str]:
        if report is None:
            return {
                "model_recovery_matrix_ok": str(not self.disabled).lower(),
                "model_recovery_matrix_status": str(
                    ModelRecoveryStatus.DISABLED if self.disabled else ModelRecoveryStatus.READY
                ),
                "model_recovery_matrix_runtime_id": self.runtime_id,
            }
        return report.metadata()

    def _case_from_reports(
        self,
        stream: Mapping[str, Any],
        retry: Mapping[str, Any],
    ) -> ModelRecoveryCase:
        envelope = _as_mapping(stream.get("envelope"))
        envelope_metadata = _as_mapping(envelope.get("metadata"))
        usage = _as_mapping(stream.get("usage"))
        error_kind = str(stream.get("error_kind") or "none")
        kind = _case_kind(error_kind=error_kind, stream=stream)
        attempts = tuple(_attempt_from_payload(item) for item in _as_list(retry.get("attempts")) if isinstance(item, Mapping))
        recovered = _truthy(retry.get("recovered")) or str(retry.get("status") or "") in {
            "not_needed",
            "retried",
            "fallback_selected",
        }
        findings = self._case_findings(stream=stream, retry=retry, kind=kind, attempts=attempts)
        return ModelRecoveryCase(
            case_id=new_id("model_recovery_case"),
            turn_index=_safe_int(envelope.get("turn_index")),
            request_id=str(envelope.get("request_id") or ""),
            model=str(envelope.get("model") or ""),
            kind=kind,
            stream_status=str(stream.get("status") or ""),
            stream_ok=_truthy(stream.get("ok")),
            error_kind=error_kind,
            input_tokens=_safe_int(usage.get("input_tokens")),
            output_tokens=_safe_int(usage.get("output_tokens")),
            cost_usd=_safe_float(usage.get("estimated_cost_usd")),
            restore_message_count=_safe_int(envelope_metadata.get("restore_model_message_count")),
            attempts=attempts,
            recovered=recovered,
            final_model=str(retry.get("final_model") or envelope.get("model") or ""),
            fallback_used=_truthy(retry.get("fallback_used")),
            findings=tuple(findings),
            metadata={
                "model_stream_report_id": str(stream.get("report_id") or ""),
                "api_retry_report_id": str(retry.get("report_id") or ""),
                "restore_application_id": str(envelope_metadata.get("restore_application_id") or ""),
                "restore_contract_id": str(envelope_metadata.get("restore_contract_id") or ""),
            },
        )

    def _case_findings(
        self,
        *,
        stream: Mapping[str, Any],
        retry: Mapping[str, Any],
        kind: ModelRecoveryCaseKind,
        attempts: Sequence[ModelRecoveryAttemptProjection],
    ) -> list[ModelRecoveryFinding]:
        findings: list[ModelRecoveryFinding] = []
        stream_ok = _truthy(stream.get("ok"))
        retry_status = str(retry.get("status") or "")
        if kind == ModelRecoveryCaseKind.CLEAN_STREAM and not stream_ok:
            findings.append(
                ModelRecoveryFinding(
                    code="CLEAN_STREAM_MARKED_NOT_OK",
                    severity=ModelRecoverySeverity.ERROR,
                    surface=ModelRecoverySurface.MODEL_STREAM,
                    message="Model stream has no typed error but is not marked ok.",
                )
            )
        if kind != ModelRecoveryCaseKind.CLEAN_STREAM and retry_status not in {
            "retried",
            "fallback_selected",
            "not_needed",
        }:
            findings.append(
                ModelRecoveryFinding(
                    code="TYPED_MODEL_ERROR_NOT_RECOVERED",
                    severity=ModelRecoverySeverity.BLOCKER,
                    surface=ModelRecoverySurface.API_RETRY,
                    message="Typed model error is not represented by a recovery retry status.",
                    metadata={"kind": str(kind), "retry_status": retry_status},
                )
            )
        if kind == ModelRecoveryCaseKind.MODEL_UNAVAILABLE and not any(attempt.fallback_selected for attempt in attempts):
            findings.append(
                ModelRecoveryFinding(
                    code="MODEL_UNAVAILABLE_WITHOUT_FALLBACK",
                    severity=ModelRecoverySeverity.BLOCKER,
                    surface=ModelRecoverySurface.FALLBACK,
                    message="Model unavailable errors must select a fallback model.",
                )
            )
        if kind == ModelRecoveryCaseKind.PROMPT_TOO_LONG and not any(attempt.prompt_reduction for attempt in attempts):
            findings.append(
                ModelRecoveryFinding(
                    code="PROMPT_TOO_LONG_WITHOUT_PROMPT_REDUCTION",
                    severity=ModelRecoverySeverity.WARNING,
                    surface=ModelRecoverySurface.PROMPT_REDUCTION,
                    message="Prompt-too-long did not produce an explicit reduce-prompt retry decision.",
                )
            )
        return findings


def model_recovery_matrix_metadata(report: ModelRecoveryMatrixReport | None) -> dict[str, str]:
    if report is None:
        return {
            "model_recovery_matrix_ok": "false",
            "model_recovery_matrix_status": "missing",
            "model_recovery_matrix_report_id": "",
        }
    return report.metadata()


def default_model_recovery_matrix_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/api/claude.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_model_recovery_matrix.py",
            "decision": "zyra_module_migrated",
            "capability": "stream frame errors are paired with retry and fallback decisions",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/query.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_model_recovery_matrix.py",
            "decision": "zyra_module_migrated",
            "capability": "restored context message counts are tracked per model request",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/provider.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_model_recovery_matrix.py",
            "decision": "adapter_encapsulated",
            "capability": "provider fallback and cost state are summarized as replayable recovery cases",
        },
    )


def _attempt_from_payload(payload: Mapping[str, Any]) -> ModelRecoveryAttemptProjection:
    return ModelRecoveryAttemptProjection(
        attempt_id=str(payload.get("attempt_id") or ""),
        attempt_index=_safe_int(payload.get("attempt_index")),
        model=str(payload.get("model") or ""),
        action=_action_from_value(payload.get("decision")),
        error_kind=str(payload.get("error_kind") or ""),
        retryable=_truthy(payload.get("retryable")),
        fallback_model=str(payload.get("fallback_model") or ""),
        delay_ms=_safe_int(payload.get("delay_ms")),
        metadata={str(k): str(v) for k, v in dict(_as_mapping(payload.get("metadata"))).items()},
    )


def _case_kind(*, error_kind: str, stream: Mapping[str, Any]) -> ModelRecoveryCaseKind:
    if _truthy(stream.get("disabled")):
        return ModelRecoveryCaseKind.DISABLED_RUNTIME
    aliases = {
        "none": ModelRecoveryCaseKind.CLEAN_STREAM,
        "prompt_too_long": ModelRecoveryCaseKind.PROMPT_TOO_LONG,
        "stream_stall": ModelRecoveryCaseKind.STREAM_STALL,
        "model_unavailable": ModelRecoveryCaseKind.MODEL_UNAVAILABLE,
        "rate_limit": ModelRecoveryCaseKind.RATE_LIMIT,
        "timeout": ModelRecoveryCaseKind.TIMEOUT,
        "unknown": ModelRecoveryCaseKind.UNKNOWN_ERROR,
    }
    return aliases.get(str(error_kind or "none"), ModelRecoveryCaseKind.UNKNOWN_ERROR)


def _action_from_value(value: Any) -> ModelRecoveryActionKind:
    raw = str(value or "none")
    aliases = {
        "no_retry": ModelRecoveryActionKind.NONE,
        "retry_same_model": ModelRecoveryActionKind.RETRY_SAME_MODEL,
        "retry_fallback_model": ModelRecoveryActionKind.RETRY_FALLBACK_MODEL,
        "reduce_prompt_and_retry": ModelRecoveryActionKind.REDUCE_PROMPT_AND_RETRY,
        "fail_fast": ModelRecoveryActionKind.FAIL_FAST,
        "blocked": ModelRecoveryActionKind.BLOCKED,
    }
    return aliases.get(raw, ModelRecoveryActionKind.NONE)


def _to_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict"):
        data = value.to_dict()
        return data if isinstance(data, Mapping) else {}
    data = to_jsonable(value)
    return data if isinstance(data, Mapping) else {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}
