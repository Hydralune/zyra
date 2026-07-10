from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


M1_02D_OWNER_UNIT = "M1-02D"
CODEWORKER_API_FOUNDATION_RUNTIME_ID = "zyra-codeworker-api-foundation-runtime"


class RuntimeBudgetScope(StrEnum):
    CONTEXT_WINDOW = "context_window"
    TOOL_RESULTS = "tool_results"
    MODEL_INPUT = "model_input"
    MODEL_OUTPUT = "model_output"
    RETRY = "retry"
    COMPACT_RESTORE = "compact_restore"
    SESSION = "session"


class RuntimeBudgetPressure(StrEnum):
    OK = "ok"
    WATCH = "watch"
    COMPACT_NEEDED = "compact_needed"
    OVER_LIMIT = "over_limit"
    BLOCKED = "blocked"


class RuntimeBudgetEventKind(StrEnum):
    CONTEXT_USAGE = "context_usage"
    TOOL_RESULT_USAGE = "tool_result_usage"
    MODEL_USAGE = "model_usage"
    RETRY_USAGE = "retry_usage"
    COMPACT_BOUNDARY = "compact_boundary"
    NEXT_TURN_RESTORE = "next_turn_restore"
    VALIDATION = "validation"


class RuntimeBudgetStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class RuntimeBudgetSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class RuntimeBudgetSurface(StrEnum):
    STATE = "state"
    CONTEXT = "context"
    TOOL_RESULT = "tool_result"
    MODEL_STREAM = "model_stream"
    API_RETRY = "api_retry"
    COMPACT_RESTORE = "compact_restore"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class RuntimeBudgetLimit:
    scope: RuntimeBudgetScope
    limit: int
    used: int
    reserve: int = 0
    label: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def effective_limit(self) -> int:
        return max(0, self.limit - max(0, self.reserve))

    @property
    def remaining(self) -> int:
        return max(0, self.effective_limit - max(0, self.used))

    @property
    def ratio(self) -> float:
        if self.effective_limit <= 0:
            return 1.0 if self.used > 0 else 0.0
        return max(0.0, self.used / self.effective_limit)

    @property
    def pressure(self) -> RuntimeBudgetPressure:
        if self.effective_limit <= 0:
            return RuntimeBudgetPressure.BLOCKED
        if self.used > self.effective_limit:
            return RuntimeBudgetPressure.OVER_LIMIT
        if self.ratio >= 0.92:
            return RuntimeBudgetPressure.COMPACT_NEEDED
        if self.ratio >= 0.76:
            return RuntimeBudgetPressure.WATCH
        return RuntimeBudgetPressure.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": str(self.scope),
            "limit": self.limit,
            "reserve": self.reserve,
            "effective_limit": self.effective_limit,
            "used": self.used,
            "remaining": self.remaining,
            "ratio": round(self.ratio, 6),
            "pressure": str(self.pressure),
            "label": self.label,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuntimeBudgetMutation:
    mutation_id: str
    kind: RuntimeBudgetEventKind
    scope: RuntimeBudgetScope
    used_delta: int = 0
    input_tokens_delta: int = 0
    output_tokens_delta: int = 0
    cost_delta_usd: float = 0.0
    retry_delta: int = 0
    compact_delta: int = 0
    reason: str = ""
    turn_index: int = 0
    tool_call_id: str = ""
    artifact_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "kind": str(self.kind),
            "scope": str(self.scope),
            "used_delta": self.used_delta,
            "input_tokens_delta": self.input_tokens_delta,
            "output_tokens_delta": self.output_tokens_delta,
            "cost_delta_usd": round(self.cost_delta_usd, 8),
            "retry_delta": self.retry_delta,
            "compact_delta": self.compact_delta,
            "reason": self.reason,
            "turn_index": self.turn_index,
            "tool_call_id": self.tool_call_id,
            "artifact_id": self.artifact_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RuntimeBudgetFinding:
    code: str
    severity: RuntimeBudgetSeverity
    surface: RuntimeBudgetSurface
    message: str
    scope: RuntimeBudgetScope | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == RuntimeBudgetSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "scope": str(self.scope) if self.scope else "",
            "message": self.message,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuntimeBudgetSnapshot:
    snapshot_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    status: RuntimeBudgetStatus
    limits: tuple[RuntimeBudgetLimit, ...]
    mutations: tuple[RuntimeBudgetMutation, ...]
    findings: tuple[RuntimeBudgetFinding, ...]
    input_tokens: int
    output_tokens: int
    retry_count: int
    compact_count: int
    estimated_cost_usd: float
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status not in {RuntimeBudgetStatus.BLOCKED, RuntimeBudgetStatus.DISABLED} and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def highest_pressure(self) -> RuntimeBudgetPressure:
        priority = {
            RuntimeBudgetPressure.OK: 0,
            RuntimeBudgetPressure.WATCH: 1,
            RuntimeBudgetPressure.COMPACT_NEEDED: 2,
            RuntimeBudgetPressure.OVER_LIMIT: 3,
            RuntimeBudgetPressure.BLOCKED: 4,
        }
        current = RuntimeBudgetPressure.OK
        for limit in self.limits:
            if priority[limit.pressure] > priority[current]:
                current = limit.pressure
        return current

    @property
    def context_limit(self) -> RuntimeBudgetLimit | None:
        return self.limit_for(RuntimeBudgetScope.CONTEXT_WINDOW)

    @property
    def tool_result_limit(self) -> RuntimeBudgetLimit | None:
        return self.limit_for(RuntimeBudgetScope.TOOL_RESULTS)

    def limit_for(self, scope: RuntimeBudgetScope) -> RuntimeBudgetLimit | None:
        for limit in self.limits:
            if limit.scope == scope:
                return limit
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.runtime_budget_state.v1",
            "snapshot_id": self.snapshot_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "highest_pressure": str(self.highest_pressure),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "retry_count": self.retry_count,
            "compact_count": self.compact_count,
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "limits": [limit.to_dict() for limit in self.limits],
            "mutations": [mutation.to_dict() for mutation in self.mutations],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        context = self.context_limit
        tool_results = self.tool_result_limit
        return {
            "runtime_budget_state_snapshot_id": self.snapshot_id,
            "runtime_budget_state_owner_unit": self.owner_unit,
            "runtime_budget_state_runtime_id": self.runtime_id,
            "runtime_budget_state_ok": str(self.ok).lower(),
            "runtime_budget_state_status": str(self.status),
            "runtime_budget_state_disabled": str(self.disabled).lower(),
            "runtime_budget_state_highest_pressure": str(self.highest_pressure),
            "runtime_budget_state_context_used_chars": str(context.used if context else 0),
            "runtime_budget_state_context_limit_chars": str(context.limit if context else 0),
            "runtime_budget_state_context_remaining_chars": str(context.remaining if context else 0),
            "runtime_budget_state_context_pressure": str(context.pressure if context else ""),
            "runtime_budget_state_tool_result_chars": str(tool_results.used if tool_results else 0),
            "runtime_budget_state_tool_result_limit_chars": str(tool_results.limit if tool_results else 0),
            "runtime_budget_state_input_tokens": str(self.input_tokens),
            "runtime_budget_state_output_tokens": str(self.output_tokens),
            "runtime_budget_state_retry_count": str(self.retry_count),
            "runtime_budget_state_compact_count": str(self.compact_count),
            "runtime_budget_state_mutations": str(len(self.mutations)),
            "runtime_budget_state_findings": str(len(self.findings)),
            "runtime_budget_state_blockers": str(sum(1 for finding in self.findings if finding.blocking)),
            "runtime_budget_state_cost_usd": f"{self.estimated_cost_usd:.8f}",
        }


class RuntimeBudgetState:
    """Shared budget custody for context, compact restore, model stream and API retry.

    M1-02D needs a state object that survives the handoff between the tool
    result context chain, compact boundary creation, model streaming, retry
    handling, and the next-turn restore contract. This class is deliberately
    mutable inside the QueryEngine run, but every external handoff uses an
    immutable RuntimeBudgetSnapshot so downstream audits can prove exactly what
    changed.
    """

    def __init__(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        context_limit_chars: int,
        tool_result_limit_chars: int,
        model_input_token_limit: int = 200000,
        model_output_token_limit: int = 8192,
        retry_limit: int = 3,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
        disabled: bool = False,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.session_id = session_id
        self.worker_request_id = worker_request_id
        self.context_limit_chars = max(1, int(context_limit_chars or 1))
        self.tool_result_limit_chars = max(1, int(tool_result_limit_chars or 1))
        self.model_input_token_limit = max(1, int(model_input_token_limit or 1))
        self.model_output_token_limit = max(1, int(model_output_token_limit or 1))
        self.retry_limit = max(0, int(retry_limit or 0))
        self.disabled = bool(disabled)
        self.metadata = {str(k): str(v) for k, v in dict(metadata or {}).items()}
        self.context_used_chars = 0
        self.tool_result_chars = 0
        self.model_input_tokens = 0
        self.model_output_tokens = 0
        self.retry_count = 0
        self.compact_count = 0
        self.estimated_cost_usd = 0.0
        self._mutations: list[RuntimeBudgetMutation] = []
        self._findings: list[RuntimeBudgetFinding] = []
        if self.disabled:
            self._findings.append(
                RuntimeBudgetFinding(
                    code="RUNTIME_BUDGET_STATE_DISABLED",
                    severity=RuntimeBudgetSeverity.BLOCKER,
                    surface=RuntimeBudgetSurface.STATE,
                    scope=RuntimeBudgetScope.SESSION,
                    message="RuntimeBudgetState is disabled; CodeWorker API foundation cannot retain cross-turn budget custody.",
                )
            )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: RuntimeBudgetSnapshot | Mapping[str, Any],
        *,
        session_id: str,
        worker_request_id: str,
        context_limit_chars: int | None = None,
        tool_result_limit_chars: int | None = None,
        model_input_token_limit: int | None = None,
        model_output_token_limit: int | None = None,
        retry_limit: int | None = None,
        owner_unit: str | None = None,
        runtime_id: str | None = None,
        disabled: bool | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "RuntimeBudgetState":
        raw = snapshot.to_dict() if isinstance(snapshot, RuntimeBudgetSnapshot) else dict(_as_mapping(snapshot))
        nested = _as_mapping(raw.get("runtime_budget_state"))
        if nested and not raw.get("limits"):
            raw = dict(nested)
        parsed_limits: dict[RuntimeBudgetScope, Mapping[str, Any]] = {}
        for item in _mapping_items(raw.get("limits")):
            scope = _enum_or_default(RuntimeBudgetScope, item.get("scope"), None)
            if scope is not None:
                parsed_limits[scope] = item

        def current_limit(
            explicit: int | None,
            scope: RuntimeBudgetScope,
            fallback: int,
            *,
            allow_zero: bool = False,
        ) -> int:
            if explicit is not None:
                value = _safe_int(explicit, fallback)
            else:
                value = _safe_int(_as_mapping(parsed_limits.get(scope)).get("limit"), fallback)
            return max(0 if allow_zero else 1, value)

        source_session_id = str(raw.get("session_id") or "")
        source_worker_request_id = str(raw.get("worker_request_id") or "")
        source_snapshot_id = str(raw.get("snapshot_id") or "")
        restored_metadata = {
            **_str_map(raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else None),
            **_str_map(metadata),
            "runtime_budget_restored": "true",
            "runtime_budget_restored_source_snapshot_id": source_snapshot_id,
            "runtime_budget_restored_source_session_id": source_session_id,
            "runtime_budget_restored_source_worker_request_id": source_worker_request_id,
            "runtime_budget_restored_current_session_id": str(session_id),
            "runtime_budget_restored_current_worker_request_id": str(worker_request_id),
        }
        state = cls(
            session_id=str(session_id),
            worker_request_id=str(worker_request_id),
            context_limit_chars=current_limit(context_limit_chars, RuntimeBudgetScope.CONTEXT_WINDOW, 1),
            tool_result_limit_chars=current_limit(tool_result_limit_chars, RuntimeBudgetScope.TOOL_RESULTS, 1),
            model_input_token_limit=current_limit(model_input_token_limit, RuntimeBudgetScope.MODEL_INPUT, 200000),
            model_output_token_limit=current_limit(model_output_token_limit, RuntimeBudgetScope.MODEL_OUTPUT, 8192),
            retry_limit=current_limit(retry_limit, RuntimeBudgetScope.RETRY, 3, allow_zero=True),
            owner_unit=str(owner_unit or raw.get("owner_unit") or M1_02D_OWNER_UNIT),
            runtime_id=str(runtime_id or raw.get("runtime_id") or CODEWORKER_API_FOUNDATION_RUNTIME_ID),
            disabled=_safe_bool(raw.get("disabled"), default=False) if disabled is None else bool(disabled),
            metadata=restored_metadata,
        )
        state.context_used_chars = max(
            0,
            _safe_int(_as_mapping(parsed_limits.get(RuntimeBudgetScope.CONTEXT_WINDOW)).get("used"), 0),
        )
        state.tool_result_chars = max(
            0,
            _safe_int(_as_mapping(parsed_limits.get(RuntimeBudgetScope.TOOL_RESULTS)).get("used"), 0),
        )
        state.model_input_tokens = max(
            0,
            _safe_int(
                raw.get("input_tokens"),
                _safe_int(_as_mapping(parsed_limits.get(RuntimeBudgetScope.MODEL_INPUT)).get("used"), 0),
            ),
        )
        state.model_output_tokens = max(
            0,
            _safe_int(
                raw.get("output_tokens"),
                _safe_int(_as_mapping(parsed_limits.get(RuntimeBudgetScope.MODEL_OUTPUT)).get("used"), 0),
            ),
        )
        state.retry_count = max(
            0,
            _safe_int(
                raw.get("retry_count"),
                _safe_int(_as_mapping(parsed_limits.get(RuntimeBudgetScope.RETRY)).get("used"), 0),
            ),
        )
        state.compact_count = max(0, _safe_int(raw.get("compact_count"), 0))
        state.estimated_cost_usd = max(0.0, _safe_float(raw.get("estimated_cost_usd"), 0.0))
        state._mutations = []
        for item in _mapping_items(raw.get("mutations")):
            kind = _enum_or_default(RuntimeBudgetEventKind, item.get("kind"), RuntimeBudgetEventKind.VALIDATION)
            scope = _enum_or_default(RuntimeBudgetScope, item.get("scope"), RuntimeBudgetScope.SESSION)
            state._mutations.append(
                RuntimeBudgetMutation(
                    mutation_id=str(item.get("mutation_id") or new_id("budget_mut")),
                    kind=kind,
                    scope=scope,
                    used_delta=_safe_int(item.get("used_delta"), 0),
                    input_tokens_delta=_safe_int(item.get("input_tokens_delta"), 0),
                    output_tokens_delta=_safe_int(item.get("output_tokens_delta"), 0),
                    cost_delta_usd=_safe_float(item.get("cost_delta_usd"), 0.0),
                    retry_delta=_safe_int(item.get("retry_delta"), 0),
                    compact_delta=_safe_int(item.get("compact_delta"), 0),
                    reason=str(item.get("reason") or "restored_runtime_budget_mutation"),
                    turn_index=max(0, _safe_int(item.get("turn_index"), 0)),
                    tool_call_id=str(item.get("tool_call_id") or ""),
                    artifact_id=str(item.get("artifact_id") or ""),
                    metadata={**_str_map(item.get("metadata") if isinstance(item.get("metadata"), Mapping) else None), "restored": "true"},
                    created_at=str(item.get("created_at") or now_iso()),
                )
            )
        state._findings = []
        for item in _mapping_items(raw.get("findings")):
            scope_value = item.get("scope")
            state._findings.append(
                RuntimeBudgetFinding(
                    code=str(item.get("code") or "RESTORED_RUNTIME_BUDGET_FINDING"),
                    severity=_enum_or_default(
                        RuntimeBudgetSeverity,
                        item.get("severity"),
                        RuntimeBudgetSeverity.WARNING,
                    ),
                    surface=_enum_or_default(
                        RuntimeBudgetSurface,
                        item.get("surface"),
                        RuntimeBudgetSurface.STATE,
                    ),
                    message=str(item.get("message") or "Restored runtime budget finding."),
                    scope=(
                        _enum_or_default(RuntimeBudgetScope, scope_value, None)
                        if scope_value not in {None, "", "None"}
                        else None
                    ),
                    metadata={**_str_map(item.get("metadata") if isinstance(item.get("metadata"), Mapping) else None), "restored": "true"},
                )
            )
        if state.disabled and not any(finding.code == "RUNTIME_BUDGET_STATE_DISABLED" for finding in state._findings):
            state._findings.append(
                RuntimeBudgetFinding(
                    code="RUNTIME_BUDGET_STATE_DISABLED",
                    severity=RuntimeBudgetSeverity.BLOCKER,
                    surface=RuntimeBudgetSurface.STATE,
                    scope=RuntimeBudgetScope.SESSION,
                    message="Restored RuntimeBudgetState remains disabled in the current scope.",
                    metadata={"restored": "true"},
                )
            )
        return state

    @property
    def mutations(self) -> tuple[RuntimeBudgetMutation, ...]:
        return tuple(self._mutations)

    @property
    def findings(self) -> tuple[RuntimeBudgetFinding, ...]:
        return tuple(self._findings)

    @property
    def ok(self) -> bool:
        return not self.disabled and not any(finding.blocking for finding in self._findings)

    @property
    def status(self) -> RuntimeBudgetStatus:
        if self.disabled:
            return RuntimeBudgetStatus.DISABLED
        if any(finding.blocking for finding in self._findings):
            return RuntimeBudgetStatus.BLOCKED
        if self._findings:
            return RuntimeBudgetStatus.DEGRADED
        return RuntimeBudgetStatus.READY

    def record_context_usage(
        self,
        *,
        active_chars: int,
        reserve_chars: int = 0,
        source: str = "",
        turn_index: int = 0,
        metadata: Mapping[str, str] | None = None,
    ) -> RuntimeBudgetMutation:
        previous = self.context_used_chars
        self.context_used_chars = max(0, int(active_chars or 0))
        mutation = self._append_mutation(
            RuntimeBudgetEventKind.CONTEXT_USAGE,
            RuntimeBudgetScope.CONTEXT_WINDOW,
            used_delta=self.context_used_chars - previous,
            reason=source or "context_window_snapshot",
            turn_index=turn_index,
            metadata={"reserve_chars": str(max(0, int(reserve_chars or 0))), **_str_map(metadata)},
        )
        self._refresh_pressure_findings(RuntimeBudgetScope.CONTEXT_WINDOW)
        return mutation

    def record_tool_result_projection(
        self,
        *,
        inline_chars: int,
        original_chars: int = 0,
        tool_call_id: str = "",
        tool_name: str = "",
        turn_index: int = 0,
        artifact_ids: Sequence[str] = (),
        metadata: Mapping[str, str] | None = None,
    ) -> RuntimeBudgetMutation:
        used = max(0, int(inline_chars or 0))
        self.tool_result_chars += used
        mutation = self._append_mutation(
            RuntimeBudgetEventKind.TOOL_RESULT_USAGE,
            RuntimeBudgetScope.TOOL_RESULTS,
            used_delta=used,
            reason="tool_result_context_projection",
            turn_index=turn_index,
            tool_call_id=tool_call_id,
            artifact_id=",".join(str(item) for item in artifact_ids if item),
            metadata={
                "tool_name": str(tool_name),
                "original_chars": str(max(0, int(original_chars or 0))),
                **_str_map(metadata),
            },
        )
        self._refresh_pressure_findings(RuntimeBudgetScope.TOOL_RESULTS)
        return mutation

    def record_model_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float = 0.0,
        turn_index: int = 0,
        model: str = "",
        request_id: str = "",
        metadata: Mapping[str, str] | None = None,
    ) -> RuntimeBudgetMutation:
        input_delta = max(0, int(input_tokens or 0))
        output_delta = max(0, int(output_tokens or 0))
        self.model_input_tokens += input_delta
        self.model_output_tokens += output_delta
        self.estimated_cost_usd += max(0.0, float(cost_usd or 0.0))
        mutation = self._append_mutation(
            RuntimeBudgetEventKind.MODEL_USAGE,
            RuntimeBudgetScope.MODEL_INPUT,
            input_tokens_delta=input_delta,
            output_tokens_delta=output_delta,
            cost_delta_usd=max(0.0, float(cost_usd or 0.0)),
            reason="model_stream_usage_patch",
            turn_index=turn_index,
            metadata={"model": model, "model_request_id": request_id, **_str_map(metadata)},
        )
        self._refresh_pressure_findings(RuntimeBudgetScope.MODEL_INPUT)
        self._refresh_pressure_findings(RuntimeBudgetScope.MODEL_OUTPUT)
        return mutation

    def record_retry(
        self,
        *,
        reason: str,
        turn_index: int = 0,
        attempt_id: str = "",
        retryable: bool = True,
        metadata: Mapping[str, str] | None = None,
    ) -> RuntimeBudgetMutation:
        self.retry_count += 1
        mutation = self._append_mutation(
            RuntimeBudgetEventKind.RETRY_USAGE,
            RuntimeBudgetScope.RETRY,
            retry_delta=1,
            reason=reason,
            turn_index=turn_index,
            metadata={"attempt_id": attempt_id, "retryable": str(retryable).lower(), **_str_map(metadata)},
        )
        self._refresh_pressure_findings(RuntimeBudgetScope.RETRY)
        return mutation

    def record_compact_boundary(
        self,
        *,
        boundary_id: str,
        reason: str,
        before_chars: int,
        after_chars: int,
        artifact_id: str = "",
        turn_index: int = 0,
        metadata: Mapping[str, str] | None = None,
    ) -> RuntimeBudgetMutation:
        self.compact_count += 1
        self.context_used_chars = max(0, int(after_chars or 0))
        mutation = self._append_mutation(
            RuntimeBudgetEventKind.COMPACT_BOUNDARY,
            RuntimeBudgetScope.COMPACT_RESTORE,
            used_delta=max(0, int(after_chars or 0)) - max(0, int(before_chars or 0)),
            compact_delta=1,
            reason=reason,
            turn_index=turn_index,
            artifact_id=artifact_id,
            metadata={
                "boundary_id": boundary_id,
                "before_chars": str(max(0, int(before_chars or 0))),
                "after_chars": str(max(0, int(after_chars or 0))),
                **_str_map(metadata),
            },
        )
        self._refresh_pressure_findings(RuntimeBudgetScope.CONTEXT_WINDOW)
        return mutation

    def record_next_turn_restore(
        self,
        *,
        restore_contract_id: str,
        segment_count: int,
        restored_chars: int,
        turn_index: int = 0,
        metadata: Mapping[str, str] | None = None,
    ) -> RuntimeBudgetMutation:
        return self._append_mutation(
            RuntimeBudgetEventKind.NEXT_TURN_RESTORE,
            RuntimeBudgetScope.COMPACT_RESTORE,
            used_delta=max(0, int(restored_chars or 0)),
            reason="next_turn_restore_contract",
            turn_index=turn_index,
            metadata={
                "restore_contract_id": restore_contract_id,
                "segment_count": str(max(0, int(segment_count or 0))),
                **_str_map(metadata),
            },
        )

    def ingest_tool_result_context_report(self, report: Any) -> int:
        projections = getattr(report, "projections", ())
        count = 0
        for projection in projections:
            self.record_tool_result_projection(
                inline_chars=_safe_int(getattr(projection, "inline_chars", 0)),
                original_chars=_safe_int(getattr(projection, "original_chars", 0)),
                tool_call_id=str(getattr(projection, "tool_call_id", "") or ""),
                tool_name=str(getattr(projection, "tool_name", "") or ""),
                turn_index=_safe_int(getattr(projection, "turn_index", 0)),
                artifact_ids=tuple(str(item) for item in getattr(projection, "artifact_ids", ()) or ()),
                metadata={
                    "projection_id": str(getattr(projection, "projection_id", "") or ""),
                    "budget_applied": str(bool(getattr(projection, "budget_applied", False))).lower(),
                    "externalized_artifact_id": str(getattr(projection, "externalized_artifact_id", "") or ""),
                },
            )
            count += 1
        if count == 0:
            self._findings.append(
                RuntimeBudgetFinding(
                    code="NO_TOOL_RESULT_CONTEXT_PROJECTIONS",
                    severity=RuntimeBudgetSeverity.INFO,
                    surface=RuntimeBudgetSurface.TOOL_RESULT,
                    scope=RuntimeBudgetScope.TOOL_RESULTS,
                    message="No tool result projections were available to add to the runtime budget state.",
                )
            )
        return count

    def require_ready(self) -> RuntimeBudgetSnapshot:
        snapshot = self.snapshot()
        if not snapshot.ok:
            self._append_mutation(
                RuntimeBudgetEventKind.VALIDATION,
                RuntimeBudgetScope.SESSION,
                reason="runtime_budget_state_validation_failed",
                metadata={"status": str(snapshot.status), "blockers": str(sum(1 for finding in snapshot.findings if finding.blocking))},
            )
            return self.snapshot()
        self._append_mutation(
            RuntimeBudgetEventKind.VALIDATION,
            RuntimeBudgetScope.SESSION,
            reason="runtime_budget_state_validation_passed",
            metadata={"status": str(snapshot.status)},
        )
        return self.snapshot()

    def snapshot(self) -> RuntimeBudgetSnapshot:
        limits = (
            RuntimeBudgetLimit(
                RuntimeBudgetScope.CONTEXT_WINDOW,
                self.context_limit_chars,
                self.context_used_chars,
                label="QueryEngine active context characters",
                metadata={"source": "ClaudeContextWindowManager"},
            ),
            RuntimeBudgetLimit(
                RuntimeBudgetScope.TOOL_RESULTS,
                self.tool_result_limit_chars,
                self.tool_result_chars,
                label="Tool result context characters visible to next turn",
                metadata={"source": "ToolResultContextRuntime"},
            ),
            RuntimeBudgetLimit(
                RuntimeBudgetScope.MODEL_INPUT,
                self.model_input_token_limit,
                self.model_input_tokens,
                label="Model input token budget",
                metadata={"source": "ModelStreamRuntime"},
            ),
            RuntimeBudgetLimit(
                RuntimeBudgetScope.MODEL_OUTPUT,
                self.model_output_token_limit,
                self.model_output_tokens,
                label="Model output token budget",
                metadata={"source": "ModelStreamRuntime"},
            ),
            RuntimeBudgetLimit(
                RuntimeBudgetScope.RETRY,
                self.retry_limit,
                self.retry_count,
                label="API retry attempt budget",
                metadata={"source": "ApiRetryRuntime"},
            ),
        )
        return RuntimeBudgetSnapshot(
            snapshot_id=new_id("budget"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=self.session_id,
            worker_request_id=self.worker_request_id,
            status=self.status,
            limits=limits,
            mutations=tuple(self._mutations),
            findings=tuple(self._findings),
            input_tokens=self.model_input_tokens,
            output_tokens=self.model_output_tokens,
            retry_count=self.retry_count,
            compact_count=self.compact_count,
            estimated_cost_usd=self.estimated_cost_usd,
            disabled=self.disabled,
        )

    def metadata_values(self) -> dict[str, str]:
        return self.snapshot().metadata()

    def event_for_snapshot(
        self,
        snapshot: RuntimeBudgetSnapshot,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "runtime_budget_state",
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.BUDGET_UPDATED,
            payload={
                "query_session": {
                    "session_id": snapshot.session_id,
                    "worker_request_id": snapshot.worker_request_id,
                    "phase": phase,
                    "runtime_budget_state": snapshot.to_dict(),
                }
            },
        )

    def _append_mutation(
        self,
        kind: RuntimeBudgetEventKind,
        scope: RuntimeBudgetScope,
        *,
        used_delta: int = 0,
        input_tokens_delta: int = 0,
        output_tokens_delta: int = 0,
        cost_delta_usd: float = 0.0,
        retry_delta: int = 0,
        compact_delta: int = 0,
        reason: str = "",
        turn_index: int = 0,
        tool_call_id: str = "",
        artifact_id: str = "",
        metadata: Mapping[str, str] | None = None,
    ) -> RuntimeBudgetMutation:
        mutation = RuntimeBudgetMutation(
            mutation_id=new_id("budget_mut"),
            kind=kind,
            scope=scope,
            used_delta=int(used_delta or 0),
            input_tokens_delta=int(input_tokens_delta or 0),
            output_tokens_delta=int(output_tokens_delta or 0),
            cost_delta_usd=float(cost_delta_usd or 0.0),
            retry_delta=int(retry_delta or 0),
            compact_delta=int(compact_delta or 0),
            reason=reason,
            turn_index=int(turn_index or 0),
            tool_call_id=tool_call_id,
            artifact_id=artifact_id,
            metadata=_str_map(metadata),
        )
        self._mutations.append(mutation)
        return mutation

    def _refresh_pressure_findings(self, scope: RuntimeBudgetScope) -> None:
        if self.disabled:
            return
        limits = self.snapshot().limits
        target = next((limit for limit in limits if limit.scope == scope), None)
        if target is None:
            return
        existing_codes = {finding.code for finding in self._findings}
        if target.pressure == RuntimeBudgetPressure.OVER_LIMIT:
            code = f"{str(scope).upper()}_OVER_LIMIT"
            if code not in existing_codes:
                self._findings.append(
                    RuntimeBudgetFinding(
                        code=code,
                        severity=RuntimeBudgetSeverity.ERROR,
                        surface=_surface_for_scope(scope),
                        scope=scope,
                        message=f"{scope} used {target.used} over effective limit {target.effective_limit}.",
                        metadata={"ratio": f"{target.ratio:.4f}", "remaining": str(target.remaining)},
                    )
                )
        elif target.pressure == RuntimeBudgetPressure.COMPACT_NEEDED:
            code = f"{str(scope).upper()}_COMPACT_NEEDED"
            if code not in existing_codes:
                self._findings.append(
                    RuntimeBudgetFinding(
                        code=code,
                        severity=RuntimeBudgetSeverity.WARNING,
                        surface=_surface_for_scope(scope),
                        scope=scope,
                        message=f"{scope} is near limit and needs compact/reduce action.",
                        metadata={"ratio": f"{target.ratio:.4f}", "remaining": str(target.remaining)},
                    )
                )
        elif target.pressure == RuntimeBudgetPressure.BLOCKED:
            code = f"{str(scope).upper()}_BLOCKED"
            if code not in existing_codes:
                self._findings.append(
                    RuntimeBudgetFinding(
                        code=code,
                        severity=RuntimeBudgetSeverity.BLOCKER,
                        surface=_surface_for_scope(scope),
                        scope=scope,
                        message=f"{scope} has no usable budget limit.",
                    )
                )


def runtime_budget_metadata(snapshot: RuntimeBudgetSnapshot | RuntimeBudgetState | None) -> dict[str, str]:
    if snapshot is None:
        return {
            "runtime_budget_state_ok": "false",
            "runtime_budget_state_status": "missing",
            "runtime_budget_state_snapshot_id": "",
        }
    if isinstance(snapshot, RuntimeBudgetState):
        return snapshot.metadata_values()
    return snapshot.metadata()


def render_runtime_budget_state_markdown(snapshot: RuntimeBudgetSnapshot) -> str:
    lines = [
        "# Runtime Budget State",
        "",
        f"- owner_unit: {snapshot.owner_unit}",
        f"- runtime_id: {snapshot.runtime_id}",
        f"- status: {snapshot.status}",
        f"- ok: {str(snapshot.ok).lower()}",
        f"- highest_pressure: {snapshot.highest_pressure}",
        f"- input_tokens: {snapshot.input_tokens}",
        f"- output_tokens: {snapshot.output_tokens}",
        f"- retry_count: {snapshot.retry_count}",
        f"- compact_count: {snapshot.compact_count}",
        "",
        "## Limits",
    ]
    for limit in snapshot.limits:
        lines.append(
            f"- {limit.scope}: used={limit.used} limit={limit.limit} remaining={limit.remaining} pressure={limit.pressure}"
        )
    lines.extend(["", "## Findings"])
    if snapshot.findings:
        for finding in snapshot.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    lines.extend(["", "## Mutations"])
    for mutation in snapshot.mutations[-30:]:
        lines.append(
            f"- {mutation.kind} {mutation.scope} delta={mutation.used_delta} input={mutation.input_tokens_delta} output={mutation.output_tokens_delta} reason={mutation.reason}"
        )
    return "\n".join(lines)


def merge_runtime_budget_snapshots(snapshots: Iterable[RuntimeBudgetSnapshot]) -> RuntimeBudgetSnapshot:
    items = list(snapshots)
    if not items:
        return RuntimeBudgetSnapshot(
            snapshot_id=new_id("budget"),
            owner_unit=M1_02D_OWNER_UNIT,
            runtime_id=CODEWORKER_API_FOUNDATION_RUNTIME_ID,
            session_id="",
            worker_request_id="",
            status=RuntimeBudgetStatus.BLOCKED,
            limits=(),
            mutations=(),
            findings=(
                RuntimeBudgetFinding(
                    code="NO_RUNTIME_BUDGET_SNAPSHOTS",
                    severity=RuntimeBudgetSeverity.BLOCKER,
                    surface=RuntimeBudgetSurface.STATE,
                    message="No RuntimeBudgetSnapshot values were available to merge.",
                ),
            ),
            input_tokens=0,
            output_tokens=0,
            retry_count=0,
            compact_count=0,
            estimated_cost_usd=0.0,
        )
    first = items[0]
    limits_by_scope: dict[RuntimeBudgetScope, RuntimeBudgetLimit] = {}
    for item in items:
        for limit in item.limits:
            current = limits_by_scope.get(limit.scope)
            if current is None:
                limits_by_scope[limit.scope] = limit
            else:
                limits_by_scope[limit.scope] = RuntimeBudgetLimit(
                    scope=limit.scope,
                    limit=max(current.limit, limit.limit),
                    reserve=max(current.reserve, limit.reserve),
                    used=current.used + limit.used,
                    label=current.label or limit.label,
                    metadata={**current.metadata, **limit.metadata},
                )
    findings = tuple(finding for item in items for finding in item.findings)
    status = RuntimeBudgetStatus.READY
    if any(item.status in {RuntimeBudgetStatus.BLOCKED, RuntimeBudgetStatus.DISABLED} for item in items):
        status = RuntimeBudgetStatus.BLOCKED
    elif findings:
        status = RuntimeBudgetStatus.DEGRADED
    return RuntimeBudgetSnapshot(
        snapshot_id=new_id("budget"),
        owner_unit=first.owner_unit,
        runtime_id=first.runtime_id,
        session_id=first.session_id,
        worker_request_id=first.worker_request_id,
        status=status,
        limits=tuple(limits_by_scope.values()),
        mutations=tuple(mutation for item in items for mutation in item.mutations),
        findings=findings,
        input_tokens=sum(item.input_tokens for item in items),
        output_tokens=sum(item.output_tokens for item in items),
        retry_count=sum(item.retry_count for item in items),
        compact_count=sum(item.compact_count for item in items),
        estimated_cost_usd=sum(item.estimated_cost_usd for item in items),
        disabled=any(item.disabled for item in items),
    )


def _surface_for_scope(scope: RuntimeBudgetScope) -> RuntimeBudgetSurface:
    if scope == RuntimeBudgetScope.CONTEXT_WINDOW:
        return RuntimeBudgetSurface.CONTEXT
    if scope == RuntimeBudgetScope.TOOL_RESULTS:
        return RuntimeBudgetSurface.TOOL_RESULT
    if scope in {RuntimeBudgetScope.MODEL_INPUT, RuntimeBudgetScope.MODEL_OUTPUT}:
        return RuntimeBudgetSurface.MODEL_STREAM
    if scope == RuntimeBudgetScope.RETRY:
        return RuntimeBudgetSurface.API_RETRY
    if scope == RuntimeBudgetScope.COMPACT_RESTORE:
        return RuntimeBudgetSurface.COMPACT_RESTORE
    return RuntimeBudgetSurface.STATE


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else default
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on", "enabled"}:
        return True
    if normalized in {"false", "0", "no", "off", "disabled"}:
        return False
    return default


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        return default


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _str_map(values: Mapping[str, Any] | None) -> dict[str, str]:
    return {str(key): str(value) for key, value in dict(values or {}).items()}
