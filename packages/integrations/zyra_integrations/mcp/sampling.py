from __future__ import annotations

"""Fail-closed MCP sampling runtime with session-owned budgets.

An MCP server can ask its client to call an LLM.  That reverses the usual
trust direction: untrusted server content may consume model tokens or request
tool-use loops.  Zyra therefore advertises sampling only when an explicit
callback is registered and enforces deterministic request, token, tool-round,
concurrency, timeout, and cancellation limits before and after the callback.

Audit records intentionally contain counts and content digests, never prompts,
messages, model output, tool arguments, or provider exception strings.
"""

import hashlib
import inspect
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, TypeAlias, runtime_checkable

from .credentials import PublicValue, redact_public_value


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class SamplingError(RuntimeError):
    code = "sampling_error"

    def __init__(self, message: str = "MCP sampling failed", *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = _safe_code(code)


class SamplingDenied(SamplingError):
    code = "sampling_denied"


class SamplingCancelled(SamplingError):
    code = "sampling_cancelled"


class SamplingTimedOut(SamplingError):
    code = "sampling_timeout"


class SamplingDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    COMPLETED = "completed"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class SamplingReason(StrEnum):
    DEFAULT_DENY = "default_deny"
    CALLBACK_MISSING = "callback_missing"
    SESSION_CLOSED = "session_closed"
    REQUEST_CAP = "request_cap"
    REQUEST_RATE_CAP = "request_rate_cap"
    REQUEST_TOO_LARGE = "request_too_large"
    TOKEN_CAP = "token_cap"
    SESSION_TOKEN_CAP = "session_token_cap"
    TOOL_ROUND_CAP = "tool_round_cap"
    SESSION_TOOL_ROUND_CAP = "session_tool_round_cap"
    CONCURRENCY_CAP = "concurrency_cap"
    MODEL_NOT_ALLOWED = "model_not_allowed"
    CALLBACK_ERROR = "callback_error"
    CALLBACK_TIMEOUT = "callback_timeout"
    CANCELLED = "cancelled"
    PROVIDER_TOKEN_OVERRUN = "provider_token_overrun"
    COMPLETED = "completed"


def _safe_code(value: Any, default: str = "unknown") -> str:
    text = str(value or default).strip().casefold().replace("-", "_")
    selected = "".join(character for character in text if character.isalnum() or character in "_./")
    return selected[:128] or default


def _timestamp(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise SamplingError("sampling values cannot contain non-finite numbers", code="invalid_request")
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_json_value(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    raise SamplingError(f"unsupported sampling value type: {type(value).__name__}", code="invalid_request")


def _estimate_tokens(value: Any) -> int:
    """Conservative provider-independent estimate used when usage is absent."""

    try:
        encoded = _canonical_json(_json_value(value)).encode("utf-8")
    except SamplingError:
        encoded = str(type(value).__name__).encode("utf-8")
    # Four UTF-8 bytes per token is common for English, but CJK and JSON
    # punctuation can be denser.  Three bytes keeps the budget conservative.
    return max(1, (len(encoded) + 2) // 3)


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    enabled: bool = False
    max_requests_per_session: int = 25
    max_requests_per_minute: int = 10
    max_tokens_per_request: int = 4096
    max_tokens_per_session: int = 32768
    max_tool_rounds_per_request: int = 5
    max_tool_rounds_per_session: int = 25
    max_concurrent_per_session: int = 1
    max_request_bytes: int = 2 * 1024 * 1024
    timeout_seconds: float = 30.0
    cancellation_poll_seconds: float = 0.02
    allowed_models: tuple[str, ...] = ()
    allow_unhinted_model: bool = True

    def __post_init__(self) -> None:
        positive = {
            "max_requests_per_session": self.max_requests_per_session,
            "max_requests_per_minute": self.max_requests_per_minute,
            "max_tokens_per_request": self.max_tokens_per_request,
            "max_tokens_per_session": self.max_tokens_per_session,
            "max_concurrent_per_session": self.max_concurrent_per_session,
            "max_request_bytes": self.max_request_bytes,
        }
        for name, value in positive.items():
            if not isinstance(value, int) or value <= 0:
                raise SamplingError(f"{name} must be a positive integer", code="invalid_policy")
        for name, value in {
            "max_tool_rounds_per_request": self.max_tool_rounds_per_request,
            "max_tool_rounds_per_session": self.max_tool_rounds_per_session,
        }.items():
            if not isinstance(value, int) or value < 0:
                raise SamplingError(f"{name} must be a non-negative integer", code="invalid_policy")
        if self.timeout_seconds <= 0 or self.cancellation_poll_seconds <= 0:
            raise SamplingError("sampling timeouts must be positive", code="invalid_policy")
        if self.max_tokens_per_request > self.max_tokens_per_session:
            raise SamplingError("per-request token cap cannot exceed session token cap", code="invalid_policy")
        object.__setattr__(
            self,
            "allowed_models",
            tuple(sorted({str(model).strip() for model in self.allowed_models if str(model).strip()})),
        )

    def safe_dict(self) -> dict[str, PublicValue]:
        return {
            "enabled": self.enabled,
            "max_requests_per_session": self.max_requests_per_session,
            "max_requests_per_minute": self.max_requests_per_minute,
            "max_tokens_per_request": self.max_tokens_per_request,
            "max_tokens_per_session": self.max_tokens_per_session,
            "max_tool_rounds_per_request": self.max_tool_rounds_per_request,
            "max_tool_rounds_per_session": self.max_tool_rounds_per_session,
            "max_concurrent_per_session": self.max_concurrent_per_session,
            "max_request_bytes": self.max_request_bytes,
            "timeout_seconds": self.timeout_seconds,
            "allowed_models": list(self.allowed_models),
            "allow_unhinted_model": self.allow_unhinted_model,
        }


@dataclass(frozen=True, slots=True, repr=False)
class SamplingRequest:
    server_id: str
    session_id: str
    request_id: str
    messages: tuple[Mapping[str, JsonValue], ...]
    max_tokens: int
    system_prompt: str = ""
    model_hint: str = ""
    tools: tuple[Mapping[str, JsonValue], ...] = ()
    temperature: float | None = None
    tool_round: int = 0
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("server_id", self.server_id, 512),
            ("session_id", self.session_id, 512),
            ("request_id", self.request_id, 1024),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise SamplingError(f"{name} is invalid", code="invalid_request")
        if not self.messages:
            raise SamplingError("sampling requires at least one message", code="invalid_request")
        normalized_messages: list[Mapping[str, JsonValue]] = []
        for message in self.messages:
            selected = _json_value(message)
            if not isinstance(selected, dict):
                raise SamplingError("sampling messages must be objects", code="invalid_request")
            normalized_messages.append(selected)
        object.__setattr__(self, "messages", tuple(normalized_messages))
        if not isinstance(self.max_tokens, int) or self.max_tokens <= 0:
            raise SamplingError("max_tokens must be positive", code="invalid_request")
        if len(self.system_prompt) > 1024 * 1024:
            raise SamplingError("system prompt is too large", code="invalid_request")
        if len(self.model_hint) > 1024:
            raise SamplingError("model hint is too long", code="invalid_request")
        if self.temperature is not None and (
            self.temperature != self.temperature
            or self.temperature in (float("inf"), float("-inf"))
            or self.temperature < 0
        ):
            raise SamplingError("temperature is invalid", code="invalid_request")
        if not isinstance(self.tool_round, int) or self.tool_round < 0:
            raise SamplingError("tool_round must be non-negative", code="invalid_request")
        normalized_tools: list[Mapping[str, JsonValue]] = []
        for tool in self.tools:
            selected_tool = _json_value(tool)
            if not isinstance(selected_tool, dict):
                raise SamplingError("sampling tools must be objects", code="invalid_request")
            normalized_tools.append(selected_tool)
        object.__setattr__(self, "tools", tuple(normalized_tools))
        selected_metadata = _json_value(self.metadata)
        if not isinstance(selected_metadata, dict):
            raise SamplingError("sampling metadata must be an object", code="invalid_request")
        object.__setattr__(self, "metadata", selected_metadata)

    def __repr__(self) -> str:
        return (
            f"SamplingRequest(server_id={self.server_id!r}, session_id={self.session_id!r}, "
            f"request_id={self.request_id!r}, messages=<redacted:{len(self.messages)}>, "
            f"system_prompt={'<redacted>' if self.system_prompt else '<absent>'}, "
            f"max_tokens={self.max_tokens}, model_hint={self.model_hint!r}, tools=<redacted:{len(self.tools)}>)"
        )

    @property
    def content_digest(self) -> str:
        return _digest(
            {
                "messages": self.messages,
                "system_prompt": self.system_prompt,
                "tools": self.tools,
                "metadata": self.metadata,
            }
        )

    @property
    def encoded_size(self) -> int:
        return len(
            _canonical_json(
                {
                    "messages": self.messages,
                    "system_prompt": self.system_prompt,
                    "model_hint": self.model_hint,
                    "tools": self.tools,
                    "metadata": self.metadata,
                }
            ).encode("utf-8")
        )

    def capped(self, maximum: int) -> "SamplingRequest":
        return replace(self, max_tokens=min(self.max_tokens, maximum))

    def safe_dict(self) -> dict[str, PublicValue]:
        return {
            "server_id": self.server_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "message_count": len(self.messages),
            "has_system_prompt": bool(self.system_prompt),
            "model_hint": self.model_hint,
            "tool_count": len(self.tools),
            "tool_round": self.tool_round,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "encoded_size": self.encoded_size,
            "content_digest": self.content_digest,
            "metadata_keys": sorted(str(key) for key in self.metadata),
            "raw_content_included": False,
        }

    @classmethod
    def from_value(cls, value: Any, *, session_id: str = "") -> "SamplingRequest":
        if isinstance(value, cls):
            return value
        if hasattr(value, "server_id") and hasattr(value, "request_id"):
            model_preferences = getattr(value, "model_preferences", {})
            hints = model_preferences.get("hints") if isinstance(model_preferences, Mapping) else None
            model_hint = ""
            if isinstance(hints, Sequence) and hints:
                first = hints[0]
                if isinstance(first, Mapping):
                    model_hint = str(first.get("name") or "")
            return cls(
                server_id=str(value.server_id),
                session_id=session_id or str(getattr(value, "session_id", "default")),
                request_id=str(value.request_id),
                messages=tuple(value.messages),
                max_tokens=int(value.max_tokens),
                system_prompt=str(getattr(value, "system_prompt", "")),
                model_hint=model_hint,
                tools=tuple(getattr(value, "tools", ())),
                temperature=getattr(value, "temperature", None),
                tool_round=int(getattr(value, "tool_round", 0)),
                metadata=getattr(value, "metadata", {}),
            )
        if isinstance(value, Mapping):
            preferences = value.get("model_preferences") or value.get("modelPreferences") or {}
            model_hint = str(value.get("model_hint") or "")
            if not model_hint and isinstance(preferences, Mapping):
                hints = preferences.get("hints")
                if isinstance(hints, Sequence) and hints and isinstance(hints[0], Mapping):
                    model_hint = str(hints[0].get("name") or "")
            return cls(
                server_id=str(value.get("server_id") or value.get("serverId") or ""),
                session_id=session_id or str(value.get("session_id") or value.get("sessionId") or "default"),
                request_id=str(value.get("request_id") or value.get("requestId") or ""),
                messages=tuple(value.get("messages") or ()),
                max_tokens=int(value.get("max_tokens") or value.get("maxTokens") or 0),
                system_prompt=str(value.get("system_prompt") or value.get("systemPrompt") or ""),
                model_hint=model_hint,
                tools=tuple(value.get("tools") or ()),
                temperature=float(value["temperature"]) if value.get("temperature") is not None else None,
                tool_round=int(value.get("tool_round") or value.get("toolRound") or 0),
                metadata=value.get("metadata") or value.get("_meta") or {},
            )
        raise SamplingError("unsupported sampling request", code="invalid_request")


@dataclass(frozen=True, slots=True, repr=False)
class SamplingResponse:
    content: JsonValue
    model: str
    token_count: int = 0
    stop_reason: str = "endTurn"
    tool_calls: tuple[Mapping[str, JsonValue], ...] = ()
    tool_rounds: int = 0
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", _json_value(self.content))
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 1024:
            raise SamplingError("sampling response model is invalid", code="invalid_response")
        if not isinstance(self.token_count, int) or self.token_count < 0:
            raise SamplingError("sampling response token_count is invalid", code="invalid_response")
        if not isinstance(self.tool_rounds, int) or self.tool_rounds < 0:
            raise SamplingError("sampling response tool_rounds is invalid", code="invalid_response")
        calls: list[Mapping[str, JsonValue]] = []
        for call in self.tool_calls:
            selected = _json_value(call)
            if not isinstance(selected, dict):
                raise SamplingError("sampling tool calls must be objects", code="invalid_response")
            calls.append(selected)
        object.__setattr__(self, "tool_calls", tuple(calls))
        selected_metadata = _json_value(self.metadata)
        if not isinstance(selected_metadata, dict):
            raise SamplingError("sampling response metadata must be an object", code="invalid_response")
        object.__setattr__(self, "metadata", selected_metadata)

    def __repr__(self) -> str:
        return (
            f"SamplingResponse(content=<redacted>, model={self.model!r}, token_count={self.token_count}, "
            f"stop_reason={self.stop_reason!r}, tool_calls=<redacted:{len(self.tool_calls)}>, "
            f"tool_rounds={self.tool_rounds})"
        )

    @property
    def effective_token_count(self) -> int:
        return self.token_count or _estimate_tokens({"content": self.content, "tool_calls": self.tool_calls})

    @property
    def content_digest(self) -> str:
        return _digest({"content": self.content, "tool_calls": self.tool_calls, "metadata": self.metadata})

    def safe_dict(self) -> dict[str, PublicValue]:
        return {
            "model": self.model,
            "token_count": self.effective_token_count,
            "stop_reason": self.stop_reason,
            "tool_call_count": len(self.tool_calls),
            "tool_rounds": self.tool_rounds,
            "content_digest": self.content_digest,
            "metadata_keys": sorted(str(key) for key in self.metadata),
            "raw_content_included": False,
        }

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "role": "assistant",
            "content": self.content,
            "model": self.model,
            "stopReason": self.stop_reason,
            **({"toolCalls": [dict(call) for call in self.tool_calls]} if self.tool_calls else {}),
            **({"_meta": dict(self.metadata)} if self.metadata else {}),
        }

    @classmethod
    def from_value(cls, value: Any, *, fallback_model: str = "zyra") -> "SamplingResponse":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(content={"type": "text", "text": value}, model=fallback_model)
        if isinstance(value, Mapping):
            if "content" not in value and "text" in value:
                content: Any = {"type": "text", "text": value.get("text")}
            else:
                content = value.get("content")
            return cls(
                content=_json_value(content),
                model=str(value.get("model") or fallback_model),
                token_count=int(value.get("token_count") or value.get("tokens") or 0),
                stop_reason=str(value.get("stop_reason") or value.get("stopReason") or "endTurn"),
                tool_calls=tuple(value.get("tool_calls") or value.get("toolCalls") or ()),
                tool_rounds=int(value.get("tool_rounds") or value.get("toolRounds") or 0),
                metadata=value.get("metadata") or value.get("_meta") or {},
            )
        raise SamplingError("sampling callback returned an unsupported response", code="invalid_response")


class SamplingContext:
    """Cancellation and budget view passed to the explicit callback."""

    __slots__ = (
        "server_id",
        "session_id",
        "request_id",
        "effective_max_tokens",
        "deadline_monotonic",
        "_cancel_event",
    )

    def __init__(
        self,
        request: SamplingRequest,
        *,
        effective_max_tokens: int,
        deadline_monotonic: float,
    ) -> None:
        self.server_id = request.server_id
        self.session_id = request.session_id
        self.request_id = request.request_id
        self.effective_max_tokens = effective_max_tokens
        self.deadline_monotonic = deadline_monotonic
        self._cancel_event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self) -> None:
        self._cancel_event.set()

    def remaining_seconds(self, monotonic: Callable[[], float] = time.monotonic) -> float:
        return max(0.0, self.deadline_monotonic - monotonic())

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise SamplingCancelled()


@runtime_checkable
class SamplingCallback(Protocol):
    def __call__(self, request: SamplingRequest, context: SamplingContext) -> SamplingResponse | Mapping[str, Any] | str: ...


@dataclass(frozen=True, slots=True)
class SessionBudgetSnapshot:
    server_id: str
    session_id: str
    requests_started: int
    requests_completed: int
    requests_denied: int
    tokens_consumed: int
    tokens_reserved: int
    tool_rounds: int
    active_requests: int
    closed: bool

    def safe_dict(self) -> dict[str, PublicValue]:
        return {
            "server_id": self.server_id,
            "session_id": self.session_id,
            "requests_started": self.requests_started,
            "requests_completed": self.requests_completed,
            "requests_denied": self.requests_denied,
            "tokens_consumed": self.tokens_consumed,
            "tokens_reserved": self.tokens_reserved,
            "tool_rounds": self.tool_rounds,
            "active_requests": self.active_requests,
            "closed": self.closed,
        }


@dataclass(slots=True)
class _SessionBudget:
    server_id: str
    session_id: str
    requests_started: int = 0
    requests_completed: int = 0
    requests_denied: int = 0
    tokens_consumed: int = 0
    tokens_reserved: int = 0
    tool_rounds: int = 0
    active_requests: int = 0
    closed: bool = False
    request_times: deque[float] = field(default_factory=deque)

    def snapshot(self) -> SessionBudgetSnapshot:
        return SessionBudgetSnapshot(
            server_id=self.server_id,
            session_id=self.session_id,
            requests_started=self.requests_started,
            requests_completed=self.requests_completed,
            requests_denied=self.requests_denied,
            tokens_consumed=self.tokens_consumed,
            tokens_reserved=self.tokens_reserved,
            tool_rounds=self.tool_rounds,
            active_requests=self.active_requests,
            closed=self.closed,
        )


@dataclass(frozen=True, slots=True)
class SamplingAuditRecord:
    server_id: str
    session_id: str
    request_id: str
    decision: SamplingDecision
    reason: SamplingReason
    occurred_at: str
    request_digest: str
    message_count: int
    tool_count: int
    requested_tokens: int
    effective_max_tokens: int
    response_tokens: int = 0
    response_digest: str = ""
    model: str = ""
    duration_ms: int = 0
    callback_error_code: str = ""
    budget: SessionBudgetSnapshot | None = None

    def safe_dict(self) -> dict[str, PublicValue]:
        return {
            "server_id": self.server_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "decision": str(self.decision),
            "reason": str(self.reason),
            "occurred_at": self.occurred_at,
            "request_digest": self.request_digest,
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "requested_tokens": self.requested_tokens,
            "effective_max_tokens": self.effective_max_tokens,
            "response_tokens": self.response_tokens,
            "response_digest": self.response_digest,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "callback_error_code": self.callback_error_code,
            "budget": self.budget.safe_dict() if self.budget else None,
            "raw_request_included": False,
            "raw_response_included": False,
            "exception_message_included": False,
        }


@dataclass(frozen=True, slots=True)
class SamplingResolution:
    decision: SamplingDecision
    reason: SamplingReason
    request_id: str
    effective_max_tokens: int = 0
    response: SamplingResponse | None = None
    audit: SamplingAuditRecord | None = None

    @property
    def allowed(self) -> bool:
        return self.decision in {SamplingDecision.ALLOWED, SamplingDecision.COMPLETED}

    def safe_dict(self) -> dict[str, PublicValue]:
        return {
            "decision": str(self.decision),
            "reason": str(self.reason),
            "request_id": self.request_id,
            "effective_max_tokens": self.effective_max_tokens,
            "response": self.response.safe_dict() if self.response else None,
            "audit": self.audit.safe_dict() if self.audit else None,
            "raw_content_included": False,
        }

    def to_wire(self) -> dict[str, JsonValue]:
        if self.decision is SamplingDecision.COMPLETED and self.response is not None:
            return self.response.to_wire()
        return {
            "error": {
                "code": str(self.reason),
                "message": "MCP sampling request was not completed",
            }
        }

    def to_mcp_resolution(self, *, include_response: bool = False) -> Any:
        from .models import McpSamplingDecision, McpSamplingResolution

        allowed = self.decision in {SamplingDecision.ALLOWED, SamplingDecision.COMPLETED}
        response: Mapping[str, JsonValue] = {}
        if include_response and self.response is not None:
            response = self.response.to_wire()
        return McpSamplingResolution(
            request_id=self.request_id,
            decision=McpSamplingDecision.ALLOW if allowed else McpSamplingDecision.DENY,
            reason=str(self.reason),
            effective_max_tokens=self.effective_max_tokens if allowed else 0,
            response=response,
        )


@dataclass(slots=True)
class _CallbackResult:
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None


class McpSamplingRuntime:
    """Enforce sampling policy around an explicitly registered callback."""

    def __init__(
        self,
        policy: SamplingPolicy | None = None,
        *,
        callback: SamplingCallback | None = None,
        event_sink: Any = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy or SamplingPolicy()
        self.event_sink = event_sink
        self.clock = clock
        self.monotonic = monotonic
        self._lock = threading.RLock()
        self._callbacks: dict[str, SamplingCallback] = {}
        self._default_callback: SamplingCallback | None = None
        self._sessions: dict[tuple[str, str], _SessionBudget] = {}
        self._active_contexts: dict[tuple[str, str, str], SamplingContext] = {}
        if callback is not None:
            self.register_callback("*", callback)

    def register_callback(self, server_id: str, callback: SamplingCallback) -> None:
        if not callable(callback):
            raise TypeError("sampling callback must be callable")
        # Validate the public callback contract once.  Variadic callables are
        # accepted; fixed arity must be able to consume request + context.
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            # Some C-extension callables do not expose signatures. Runtime
            # invocation remains authoritative.
            pass
        else:
            try:
                signature.bind(None, None)
            except TypeError as exc:
                raise TypeError("sampling callback must accept request and context") from exc
        with self._lock:
            if server_id == "*":
                self._default_callback = callback
            else:
                if not server_id or len(server_id) > 512:
                    raise SamplingError("server_id is invalid", code="invalid_callback_registration")
                self._callbacks[server_id] = callback

    def unregister_callback(self, server_id: str) -> bool:
        with self._lock:
            if server_id == "*":
                existed = self._default_callback is not None
                self._default_callback = None
                return existed
            return self._callbacks.pop(server_id, None) is not None

    def _callback_for(self, server_id: str) -> SamplingCallback | None:
        with self._lock:
            return self._callbacks.get(server_id) or self._default_callback

    def advertised(self, server_id: str) -> bool:
        return self.policy.enabled and self._callback_for(server_id) is not None

    def _budget(self, request: SamplingRequest) -> _SessionBudget:
        key = (request.server_id, request.session_id)
        return self._sessions.setdefault(key, _SessionBudget(*key))

    def session_snapshot(self, server_id: str, session_id: str) -> SessionBudgetSnapshot:
        with self._lock:
            budget = self._sessions.get((server_id, session_id))
            if budget is None:
                return _SessionBudget(server_id, session_id).snapshot()
            return budget.snapshot()

    def close_session(self, server_id: str, session_id: str, *, cancel_active: bool = True) -> SessionBudgetSnapshot:
        with self._lock:
            budget = self._sessions.setdefault((server_id, session_id), _SessionBudget(server_id, session_id))
            budget.closed = True
            contexts = [
                context
                for (selected_server, selected_session, _), context in self._active_contexts.items()
                if selected_server == server_id and selected_session == session_id
            ]
            snapshot = budget.snapshot()
        if cancel_active:
            for context in contexts:
                context.cancel()
        self._emit("mcp.sampling.session_closed", {"budget": snapshot.safe_dict()})
        return snapshot

    def reset_session(self, server_id: str, session_id: str, *, require_idle: bool = True) -> bool:
        with self._lock:
            key = (server_id, session_id)
            budget = self._sessions.get(key)
            if budget is None:
                return False
            if require_idle and budget.active_requests:
                raise SamplingError("cannot reset an active sampling session", code="session_active")
            self._sessions.pop(key, None)
        self._emit("mcp.sampling.session_reset", {"server_id": server_id, "session_id": session_id})
        return True

    def cancel_request(self, server_id: str, session_id: str, request_id: str) -> bool:
        with self._lock:
            context = self._active_contexts.get((server_id, session_id, request_id))
        if context is None:
            return False
        context.cancel()
        return True

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        selected = redact_public_value(payload)
        if not isinstance(selected, dict):
            selected = {"value": selected}
        event = {
            "event_type": event_type,
            "occurred_at": _timestamp(self.clock),
            "payload": selected,
        }
        # Defensive schema assertion. Audit payloads never use these keys;
        # rejecting them catches accidental future prompt/result projection.
        serialized = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).casefold()
        forbidden = ('"messages":', '"system_prompt":', '"content":', '"tool_calls":', '"arguments":')
        if any(marker in serialized for marker in forbidden):
            raise SamplingError("unsafe sampling audit payload", code="audit_content_blocked")
        sink = self.event_sink
        if sink is None:
            return
        try:
            if callable(sink):
                sink(event)
            elif hasattr(sink, "append_event"):
                try:
                    sink.append_event(event_type, event)
                except TypeError:
                    sink.append_event(event)
            elif hasattr(sink, "publish"):
                sink.publish(event)
        except Exception:
            return

    def _audit(
        self,
        request: SamplingRequest,
        decision: SamplingDecision,
        reason: SamplingReason,
        *,
        effective_max_tokens: int,
        started: float,
        response: SamplingResponse | None = None,
        callback_error_code: str = "",
    ) -> SamplingAuditRecord:
        budget = self.session_snapshot(request.server_id, request.session_id)
        record = SamplingAuditRecord(
            server_id=request.server_id,
            session_id=request.session_id,
            request_id=request.request_id,
            decision=decision,
            reason=reason,
            occurred_at=_timestamp(self.clock),
            request_digest=request.content_digest,
            message_count=len(request.messages),
            tool_count=len(request.tools),
            requested_tokens=request.max_tokens,
            effective_max_tokens=effective_max_tokens,
            response_tokens=response.effective_token_count if response else 0,
            response_digest=response.content_digest if response else "",
            model=response.model if response else request.model_hint,
            duration_ms=max(0, int((self.monotonic() - started) * 1000)),
            callback_error_code=_safe_code(callback_error_code, "") if callback_error_code else "",
            budget=budget,
        )
        self._emit("mcp.sampling.resolved", record.safe_dict())
        return record

    def _deny(
        self,
        request: SamplingRequest,
        reason: SamplingReason,
        *,
        started: float,
        effective_max_tokens: int = 0,
    ) -> SamplingResolution:
        with self._lock:
            self._budget(request).requests_denied += 1
        audit = self._audit(
            request,
            SamplingDecision.DENIED,
            reason,
            effective_max_tokens=effective_max_tokens,
            started=started,
        )
        return SamplingResolution(
            SamplingDecision.DENIED,
            reason,
            request.request_id,
            effective_max_tokens,
            audit=audit,
        )

    def _preflight(
        self,
        request: SamplingRequest,
        callback: SamplingCallback | None,
        *,
        started: float,
    ) -> tuple[int, SamplingResolution | None]:
        if not self.policy.enabled:
            return 0, self._deny(request, SamplingReason.DEFAULT_DENY, started=started)
        if callback is None:
            return 0, self._deny(request, SamplingReason.CALLBACK_MISSING, started=started)
        if request.encoded_size > self.policy.max_request_bytes:
            return 0, self._deny(request, SamplingReason.REQUEST_TOO_LARGE, started=started)
        if self.policy.allowed_models:
            if request.model_hint and request.model_hint not in self.policy.allowed_models:
                return 0, self._deny(request, SamplingReason.MODEL_NOT_ALLOWED, started=started)
            if not request.model_hint and not self.policy.allow_unhinted_model:
                return 0, self._deny(request, SamplingReason.MODEL_NOT_ALLOWED, started=started)
        with self._lock:
            budget = self._budget(request)
            if budget.closed:
                reason = SamplingReason.SESSION_CLOSED
            elif budget.requests_started >= self.policy.max_requests_per_session:
                reason = SamplingReason.REQUEST_CAP
            else:
                now = self.clock()
                window = now - 60.0
                while budget.request_times and budget.request_times[0] <= window:
                    budget.request_times.popleft()
                if len(budget.request_times) >= self.policy.max_requests_per_minute:
                    reason = SamplingReason.REQUEST_RATE_CAP
                elif budget.active_requests >= self.policy.max_concurrent_per_session:
                    reason = SamplingReason.CONCURRENCY_CAP
                elif request.tool_round > self.policy.max_tool_rounds_per_request:
                    reason = SamplingReason.TOOL_ROUND_CAP
                elif budget.tool_rounds + request.tool_round > self.policy.max_tool_rounds_per_session:
                    reason = SamplingReason.SESSION_TOOL_ROUND_CAP
                else:
                    remaining = self.policy.max_tokens_per_session - budget.tokens_consumed - budget.tokens_reserved
                    if remaining <= 0:
                        reason = SamplingReason.SESSION_TOKEN_CAP
                    else:
                        effective = min(request.max_tokens, self.policy.max_tokens_per_request, remaining)
                        if effective <= 0:
                            reason = SamplingReason.TOKEN_CAP
                        else:
                            budget.requests_started += 1
                            budget.request_times.append(now)
                            budget.active_requests += 1
                            budget.tokens_reserved += effective
                            budget.tool_rounds += request.tool_round
                            return effective, None
        return 0, self._deny(request, reason, started=started)

    def _release_reservation(
        self,
        request: SamplingRequest,
        effective_max_tokens: int,
        *,
        consumed_tokens: int = 0,
        completed: bool = False,
        response_tool_rounds: int = 0,
    ) -> None:
        with self._lock:
            budget = self._budget(request)
            budget.tokens_reserved = max(0, budget.tokens_reserved - effective_max_tokens)
            budget.active_requests = max(0, budget.active_requests - 1)
            # Provider work has already happened once a response exists. Count
            # that usage even when the response violates a post-flight cap;
            # otherwise a misbehaving callback could repeatedly exceed the
            # limit without consuming the session budget.
            budget.tokens_consumed += max(0, consumed_tokens)
            budget.tool_rounds += max(0, response_tool_rounds)
            if completed:
                budget.requests_completed += 1
            self._active_contexts.pop((request.server_id, request.session_id, request.request_id), None)

    @staticmethod
    def _invoke_worker(
        callback: SamplingCallback,
        request: SamplingRequest,
        context: SamplingContext,
        result: _CallbackResult,
    ) -> None:
        try:
            result.value = callback(request, context)
        except BaseException as exc:
            result.error = exc
        finally:
            result.done.set()

    def sample(
        self,
        request: SamplingRequest | Mapping[str, Any] | Any,
        *,
        session_id: str = "",
        cancel_event: threading.Event | None = None,
    ) -> SamplingResolution:
        selected = SamplingRequest.from_value(request, session_id=session_id)
        started = self.monotonic()
        callback = self._callback_for(selected.server_id)
        effective, denied = self._preflight(selected, callback, started=started)
        if denied is not None:
            return denied
        assert callback is not None
        capped_request = selected.capped(effective)
        context = SamplingContext(
            capped_request,
            effective_max_tokens=effective,
            deadline_monotonic=started + self.policy.timeout_seconds,
        )
        key = (selected.server_id, selected.session_id, selected.request_id)
        with self._lock:
            if key in self._active_contexts:
                self._release_reservation(selected, effective)
                return self._deny(selected, SamplingReason.CONCURRENCY_CAP, started=started)
            self._active_contexts[key] = context
        holder = _CallbackResult()
        worker = threading.Thread(
            target=self._invoke_worker,
            args=(callback, capped_request, context, holder),
            name=f"mcp-sampling-{hashlib.sha256(selected.request_id.encode()).hexdigest()[:10]}",
            daemon=True,
        )
        worker.start()
        terminal_reason: SamplingReason | None = None
        while not holder.done.wait(self.policy.cancellation_poll_seconds):
            if cancel_event is not None and cancel_event.is_set():
                terminal_reason = SamplingReason.CANCELLED
                context.cancel()
                break
            if context.cancelled:
                terminal_reason = SamplingReason.CANCELLED
                break
            if self.monotonic() >= context.deadline_monotonic:
                terminal_reason = SamplingReason.CALLBACK_TIMEOUT
                context.cancel()
                break
        if terminal_reason is not None:
            self._release_reservation(selected, effective)
            decision = (
                SamplingDecision.CANCELLED
                if terminal_reason is SamplingReason.CANCELLED
                else SamplingDecision.TIMED_OUT
            )
            audit = self._audit(
                selected,
                decision,
                terminal_reason,
                effective_max_tokens=effective,
                started=started,
            )
            return SamplingResolution(decision, terminal_reason, selected.request_id, effective, audit=audit)
        # The callback may cooperate with cancellation and return between the
        # final wait tick and this branch.  Cancellation still wins over that
        # late value; accepting it would let close_session race into a normal
        # completion and charge tokens after the session was closed.
        if context.cancelled or (cancel_event is not None and cancel_event.is_set()):
            context.cancel()
            self._release_reservation(selected, effective)
            audit = self._audit(
                selected,
                SamplingDecision.CANCELLED,
                SamplingReason.CANCELLED,
                effective_max_tokens=effective,
                started=started,
            )
            return SamplingResolution(
                SamplingDecision.CANCELLED,
                SamplingReason.CANCELLED,
                selected.request_id,
                effective,
                audit=audit,
            )
        if holder.error is not None:
            self._release_reservation(selected, effective)
            if isinstance(holder.error, SamplingCancelled):
                decision = SamplingDecision.CANCELLED
                reason = SamplingReason.CANCELLED
            elif isinstance(holder.error, (SamplingTimedOut, TimeoutError)):
                decision = SamplingDecision.TIMED_OUT
                reason = SamplingReason.CALLBACK_TIMEOUT
            else:
                decision = SamplingDecision.ERROR
                reason = SamplingReason.CALLBACK_ERROR
            error_code = _safe_code(getattr(holder.error, "code", type(holder.error).__name__), "callback_error")
            audit = self._audit(
                selected,
                decision,
                reason,
                effective_max_tokens=effective,
                started=started,
                callback_error_code=error_code,
            )
            return SamplingResolution(decision, reason, selected.request_id, effective, audit=audit)
        try:
            response = SamplingResponse.from_value(holder.value, fallback_model=selected.model_hint or "zyra")
        except Exception as exc:
            self._release_reservation(selected, effective)
            audit = self._audit(
                selected,
                SamplingDecision.ERROR,
                SamplingReason.CALLBACK_ERROR,
                effective_max_tokens=effective,
                started=started,
                callback_error_code=_safe_code(getattr(exc, "code", "invalid_response")),
            )
            return SamplingResolution(
                SamplingDecision.ERROR,
                SamplingReason.CALLBACK_ERROR,
                selected.request_id,
                effective,
                audit=audit,
            )
        if self.policy.allowed_models and response.model not in self.policy.allowed_models:
            actual_tokens = response.effective_token_count
            self._release_reservation(
                selected,
                effective,
                consumed_tokens=actual_tokens,
                response_tool_rounds=response.tool_rounds,
            )
            audit = self._audit(
                selected,
                SamplingDecision.DENIED,
                SamplingReason.MODEL_NOT_ALLOWED,
                effective_max_tokens=effective,
                started=started,
                response=response,
            )
            return SamplingResolution(
                SamplingDecision.DENIED,
                SamplingReason.MODEL_NOT_ALLOWED,
                selected.request_id,
                effective,
                audit=audit,
            )
        actual_tokens = response.effective_token_count
        if actual_tokens > effective:
            self._release_reservation(selected, effective, consumed_tokens=actual_tokens)
            audit = self._audit(
                selected,
                SamplingDecision.ERROR,
                SamplingReason.PROVIDER_TOKEN_OVERRUN,
                effective_max_tokens=effective,
                started=started,
                response=response,
            )
            return SamplingResolution(
                SamplingDecision.ERROR,
                SamplingReason.PROVIDER_TOKEN_OVERRUN,
                selected.request_id,
                effective,
                audit=audit,
            )
        if response.tool_rounds > self.policy.max_tool_rounds_per_request:
            self._release_reservation(
                selected,
                effective,
                consumed_tokens=actual_tokens,
                response_tool_rounds=response.tool_rounds,
            )
            audit = self._audit(
                selected,
                SamplingDecision.DENIED,
                SamplingReason.TOOL_ROUND_CAP,
                effective_max_tokens=effective,
                started=started,
                response=response,
            )
            return SamplingResolution(
                SamplingDecision.DENIED,
                SamplingReason.TOOL_ROUND_CAP,
                selected.request_id,
                effective,
                audit=audit,
            )
        with self._lock:
            budget = self._budget(selected)
            if budget.tool_rounds + response.tool_rounds > self.policy.max_tool_rounds_per_session:
                session_round_overrun = True
            else:
                session_round_overrun = False
        if session_round_overrun:
            self._release_reservation(
                selected,
                effective,
                consumed_tokens=actual_tokens,
                response_tool_rounds=response.tool_rounds,
            )
            audit = self._audit(
                selected,
                SamplingDecision.DENIED,
                SamplingReason.SESSION_TOOL_ROUND_CAP,
                effective_max_tokens=effective,
                started=started,
                response=response,
            )
            return SamplingResolution(
                SamplingDecision.DENIED,
                SamplingReason.SESSION_TOOL_ROUND_CAP,
                selected.request_id,
                effective,
                audit=audit,
            )
        self._release_reservation(
            selected,
            effective,
            consumed_tokens=actual_tokens,
            completed=True,
            response_tool_rounds=response.tool_rounds,
        )
        audit = self._audit(
            selected,
            SamplingDecision.COMPLETED,
            SamplingReason.COMPLETED,
            effective_max_tokens=effective,
            started=started,
            response=response,
        )
        return SamplingResolution(
            SamplingDecision.COMPLETED,
            SamplingReason.COMPLETED,
            selected.request_id,
            effective,
            response=response,
            audit=audit,
        )

    def safe_diagnostics(self) -> dict[str, PublicValue]:
        with self._lock:
            sessions = [budget.snapshot().safe_dict() for budget in self._sessions.values()]
            callback_servers = sorted(self._callbacks)
            active_count = len(self._active_contexts)
        return {
            "policy": self.policy.safe_dict(),
            "advertised_servers": callback_servers,
            "has_default_callback": self._default_callback is not None,
            "sessions": sessions,
            "active_count": active_count,
            "raw_content_included": False,
        }


__all__ = [
    "McpSamplingRuntime",
    "SamplingAuditRecord",
    "SamplingCallback",
    "SamplingCancelled",
    "SamplingContext",
    "SamplingDecision",
    "SamplingDenied",
    "SamplingError",
    "SamplingPolicy",
    "SamplingReason",
    "SamplingRequest",
    "SamplingResolution",
    "SamplingResponse",
    "SamplingTimedOut",
    "SessionBudgetSnapshot",
]
