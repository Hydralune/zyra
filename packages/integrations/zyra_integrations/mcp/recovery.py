from __future__ import annotations

"""Deterministic MCP recovery planning over existing domain owners."""

import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, new_id, now_iso

from .events import sanitize_mcp_value
from .models import JsonValue, McpConnectionState, to_json_value


class McpRecoveryError(RuntimeError):
    pass


class McpRecoveryDisabled(McpRecoveryError):
    pass


class McpRecoveryConflict(McpRecoveryError):
    pass


class McpRecoveryReason(StrEnum):
    STARTUP_RESTORE = "startup_restore"
    CONNECTION_FAILED = "connection_failed"
    CONNECTION_CLOSED = "connection_closed"
    CONNECTION_REPLACED = "connection_replaced"
    NEEDS_AUTH = "needs_auth"
    SERVER_DISABLED = "server_disabled"
    CONFIG_REMOVED = "config_removed"
    CATALOG_MISSING = "catalog_missing"
    CATALOG_STALE = "catalog_stale"
    CAPABILITY_CHANGED = "capability_changed"
    TASK_INTERRUPTED = "task_interrupted"
    TASK_CANCEL_REQUESTED = "task_cancel_requested"
    TASK_OUTCOME_UNKNOWN = "task_outcome_unknown"
    INSTRUCTIONS_STALE = "instructions_stale"
    EVENT_COMMIT_FAILED = "event_commit_failed"


class McpRecoveryAction(StrEnum):
    NOOP = "noop"
    REQUIRE_AUTH = "require_auth"
    RECONNECT = "reconnect"
    REFRESH = "refresh"
    WITHDRAW_CATALOG = "withdraw_catalog"
    WITHDRAW_INSTRUCTIONS = "withdraw_instructions"
    REMATERIALIZE = "rematerialize"
    POLL_TASK = "poll_task"
    CANCEL_TASK = "cancel_task"
    MARK_TASK_UNKNOWN = "mark_task_unknown"
    COMMIT_EVENTS = "commit_events"
    FAIL_REQUIRED_SERVER = "fail_required_server"


@dataclass(frozen=True, slots=True)
class McpRecoveryIdentity:
    run_id: str
    task_id: str
    node_id: str = ""
    session_id: str = ""
    worker_request_id: str = ""
    cause_event_id: str = ""
    recovery_id: str = field(default_factory=lambda: new_id("mcprecovery"))

    def __post_init__(self) -> None:
        if not self.run_id or not self.task_id:
            raise ValueError("MCP recovery requires run_id and task_id")

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "cause_event_id": self.cause_event_id,
            "recovery_id": self.recovery_id,
        }


@dataclass(frozen=True, slots=True)
class McpRecoverySignal:
    reason: McpRecoveryReason | str
    server_id: str
    connection_state: str = ""
    connection_generation: int = 0
    capability_generation: int = 0
    expected_capability_generation: int = 0
    config_present: bool = True
    connectable: bool = True
    required: bool = False
    disabled: bool = False
    retryable: bool = True
    task_handle: str = ""
    task_status: str = ""
    cancel_requested: bool = False
    instructions_generation: int = 0
    event_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.reason, McpRecoveryReason):
            object.__setattr__(self, "reason", McpRecoveryReason(str(self.reason)))
        if not self.server_id:
            raise ValueError("MCP recovery signal requires server_id")
        generations = (
            self.connection_generation,
            self.capability_generation,
            self.expected_capability_generation,
            self.instructions_generation,
        )
        if any(value < 0 for value in generations):
            raise ValueError("MCP recovery generations cannot be negative")

    @property
    def fingerprint(self) -> str:
        value = (
            str(self.reason),
            self.server_id,
            self.connection_state,
            self.connection_generation,
            self.capability_generation,
            self.expected_capability_generation,
            self.config_present,
            self.connectable,
            self.required,
            self.disabled,
            self.retryable,
            self.task_handle,
            self.task_status,
            self.cancel_requested,
            self.instructions_generation,
            self.event_ids,
        )
        return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "reason": str(self.reason),
            "server_id": self.server_id,
            "connection_state": self.connection_state,
            "connection_generation": self.connection_generation,
            "capability_generation": self.capability_generation,
            "expected_capability_generation": self.expected_capability_generation,
            "config_present": self.config_present,
            "connectable": self.connectable,
            "required": self.required,
            "disabled": self.disabled,
            "retryable": self.retryable,
            "task_handle": self.task_handle,
            "task_status": self.task_status,
            "cancel_requested": self.cancel_requested,
            "instructions_generation": self.instructions_generation,
            "event_ids": list(self.event_ids),
            "metadata": to_json_value(sanitize_mcp_value(self.metadata)),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class McpRecoveryStep:
    sequence: int
    action: McpRecoveryAction | str
    server_id: str
    reason: McpRecoveryReason | str
    task_handle: str = ""
    refresh_kinds: tuple[str, ...] = ()
    expected_connection_generation: int = 0
    expected_capability_generation: int = 0
    delay_seconds: float = 0.0
    required: bool = False
    terminal_on_failure: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    step_id: str = field(default_factory=lambda: new_id("mcprecoverystep"))

    def __post_init__(self) -> None:
        if not isinstance(self.action, McpRecoveryAction):
            object.__setattr__(self, "action", McpRecoveryAction(str(self.action)))
        if not isinstance(self.reason, McpRecoveryReason):
            object.__setattr__(self, "reason", McpRecoveryReason(str(self.reason)))
        if self.sequence < 0 or self.delay_seconds < 0:
            raise ValueError("invalid MCP recovery step ordering or delay")

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "step_id": self.step_id,
            "sequence": self.sequence,
            "action": str(self.action),
            "server_id": self.server_id,
            "reason": str(self.reason),
            "task_handle": self.task_handle,
            "refresh_kinds": list(self.refresh_kinds),
            "expected_connection_generation": self.expected_connection_generation,
            "expected_capability_generation": self.expected_capability_generation,
            "delay_seconds": self.delay_seconds,
            "required": self.required,
            "terminal_on_failure": self.terminal_on_failure,
            "metadata": to_json_value(sanitize_mcp_value(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class McpRecoveryPlan:
    identity: McpRecoveryIdentity
    signals: tuple[McpRecoverySignal, ...]
    steps: tuple[McpRecoveryStep, ...]
    plan_id: str = field(default_factory=lambda: new_id("mcprecoveryplan"))
    created_at: str = field(default_factory=now_iso)

    @property
    def actionable(self) -> bool:
        return any(step.action is not McpRecoveryAction.NOOP for step in self.steps)

    @property
    def terminal(self) -> bool:
        return any(step.action is McpRecoveryAction.FAIL_REQUIRED_SERVER for step in self.steps)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-recovery-plan.v1",
            "plan_id": self.plan_id,
            "identity": self.identity.safe_dict(),
            "signals": [signal.safe_dict() for signal in self.signals],
            "steps": [step.safe_dict() for step in self.steps],
            "actionable": self.actionable,
            "terminal": self.terminal,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class McpRecoveryPolicy:
    max_reconnect_attempts: int = 5
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0
    refresh_kinds: tuple[str, ...] = (
        "tools",
        "resources",
        "resource_templates",
        "prompts",
    )
    cancel_tasks_on_disable: bool = True
    fail_required_server: bool = True
    poll_interrupted_tasks: bool = True

    def __post_init__(self) -> None:
        if self.max_reconnect_attempts < 0:
            raise ValueError("max_reconnect_attempts cannot be negative")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("invalid MCP recovery backoff")

    def delay(self, attempt: int) -> float:
        if attempt <= 0:
            return 0.0
        return min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** min(attempt - 1, 20)),
        )


class McpRecoveryPlanner:
    def __init__(
        self,
        policy: McpRecoveryPolicy | None = None,
        *,
        disabled: bool = False,
    ) -> None:
        self.policy = policy or McpRecoveryPolicy()
        self.disabled = disabled

    def plan(
        self,
        identity: McpRecoveryIdentity,
        signals: Sequence[McpRecoverySignal],
    ) -> McpRecoveryPlan:
        if self.disabled:
            raise McpRecoveryDisabled("MCP recovery planner is disabled")
        ordered = tuple(sorted(signals, key=lambda item: (item.server_id, item.fingerprint)))
        steps: list[McpRecoveryStep] = []
        seen: set[tuple[str, str, str]] = set()

        def add(
            action: McpRecoveryAction,
            signal: McpRecoverySignal,
            *,
            task_handle: str = "",
            refresh_kinds: Sequence[str] = (),
            delay_seconds: float = 0.0,
            terminal: bool = False,
            metadata: Mapping[str, Any] | None = None,
        ) -> None:
            key = (str(action), signal.server_id, task_handle)
            if key in seen:
                return
            seen.add(key)
            steps.append(
                McpRecoveryStep(
                    sequence=len(steps),
                    action=action,
                    server_id=signal.server_id,
                    reason=signal.reason,
                    task_handle=task_handle,
                    refresh_kinds=tuple(refresh_kinds),
                    expected_connection_generation=signal.connection_generation,
                    expected_capability_generation=signal.capability_generation,
                    delay_seconds=delay_seconds,
                    required=signal.required,
                    terminal_on_failure=terminal,
                    metadata=dict(metadata or {}),
                )
            )

        for signal in ordered:
            if signal.disabled or not signal.config_present:
                add(McpRecoveryAction.WITHDRAW_CATALOG, signal)
                add(McpRecoveryAction.WITHDRAW_INSTRUCTIONS, signal)
                if signal.task_handle and self.policy.cancel_tasks_on_disable:
                    add(
                        McpRecoveryAction.CANCEL_TASK,
                        signal,
                        task_handle=signal.task_handle,
                    )
                continue
            if signal.reason is McpRecoveryReason.NEEDS_AUTH:
                add(McpRecoveryAction.REQUIRE_AUTH, signal, terminal=signal.required)
                continue
            if signal.reason is McpRecoveryReason.EVENT_COMMIT_FAILED:
                add(McpRecoveryAction.COMMIT_EVENTS, signal, terminal=True)
                continue
            if signal.cancel_requested and signal.task_handle:
                add(
                    McpRecoveryAction.CANCEL_TASK,
                    signal,
                    task_handle=signal.task_handle,
                )
                continue
            if not signal.connectable:
                if signal.required and self.policy.fail_required_server:
                    add(McpRecoveryAction.FAIL_REQUIRED_SERVER, signal, terminal=True)
                else:
                    add(McpRecoveryAction.WITHDRAW_CATALOG, signal)
                continue
            state = signal.connection_state.casefold()
            if state in {
                str(McpConnectionState.FAILED),
                str(McpConnectionState.CLOSED),
                str(McpConnectionState.RECONNECTING),
                "",
            }:
                if signal.retryable:
                    attempt = int(signal.metadata.get("attempt") or 0)
                    if attempt < self.policy.max_reconnect_attempts:
                        add(
                            McpRecoveryAction.RECONNECT,
                            signal,
                            delay_seconds=self.policy.delay(attempt),
                            terminal=signal.required,
                            metadata={"attempt": attempt + 1},
                        )
                    elif signal.required and self.policy.fail_required_server:
                        add(McpRecoveryAction.FAIL_REQUIRED_SERVER, signal, terminal=True)
                    else:
                        add(McpRecoveryAction.WITHDRAW_CATALOG, signal)
                elif signal.required and self.policy.fail_required_server:
                    add(McpRecoveryAction.FAIL_REQUIRED_SERVER, signal, terminal=True)
                continue
            if signal.reason in {
                McpRecoveryReason.CATALOG_MISSING,
                McpRecoveryReason.CATALOG_STALE,
                McpRecoveryReason.CAPABILITY_CHANGED,
            } or (
                signal.expected_capability_generation
                and signal.capability_generation
                != signal.expected_capability_generation
            ):
                add(
                    McpRecoveryAction.REFRESH,
                    signal,
                    refresh_kinds=self.policy.refresh_kinds,
                    terminal=signal.required,
                )
                add(McpRecoveryAction.REMATERIALIZE, signal)
            if signal.task_handle:
                if signal.task_status.casefold() in {"working", "pending"}:
                    if self.policy.poll_interrupted_tasks:
                        add(
                            McpRecoveryAction.POLL_TASK,
                            signal,
                            task_handle=signal.task_handle,
                        )
                elif signal.reason is McpRecoveryReason.TASK_OUTCOME_UNKNOWN:
                    add(
                        McpRecoveryAction.MARK_TASK_UNKNOWN,
                        signal,
                        task_handle=signal.task_handle,
                    )
            if (
                signal.instructions_generation
                and signal.instructions_generation < signal.connection_generation
            ):
                add(McpRecoveryAction.WITHDRAW_INSTRUCTIONS, signal)
        return McpRecoveryPlan(identity=identity, signals=ordered, steps=tuple(steps))


class McpRecoveryRuntimePort(Protocol):
    def reconnect_server(self, name: str, **context: Any) -> Any: ...
    def refresh_server(
        self,
        name: str,
        *,
        refresh_kinds: Sequence[str],
        **context: Any,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class McpRecoveryStepResult:
    step: McpRecoveryStep
    ok: bool
    changed: bool
    output: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    completed_at: str = field(default_factory=now_iso)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "step": self.step.safe_dict(),
            "ok": self.ok,
            "changed": self.changed,
            "output": to_json_value(sanitize_mcp_value(self.output)),
            "error": self.error,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class McpRecoveryExecution:
    plan: McpRecoveryPlan
    results: tuple[McpRecoveryStepResult, ...]
    stopped_early: bool
    execution_id: str = field(default_factory=lambda: new_id("mcprecoveryexec"))

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results) and not (
            self.stopped_early and len(self.results) < len(self.plan.steps)
        )

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-recovery-execution.v1",
            "execution_id": self.execution_id,
            "plan": self.plan.safe_dict(),
            "results": [result.safe_dict() for result in self.results],
            "stopped_early": self.stopped_early,
            "ok": self.ok,
        }


class McpRecoveryExecutor:
    def __init__(
        self,
        runtime: McpRecoveryRuntimePort,
        *,
        withdraw_catalog: Callable[[str], Any] | None = None,
        withdraw_instructions: Callable[[str, McpRecoveryIdentity], Any] | None = None,
        rematerialize: Callable[[str, McpRecoveryIdentity], Any] | None = None,
        task_poll: Callable[[str, str], Any] | None = None,
        task_cancel: Callable[[str, str], Any] | None = None,
        task_mark_unknown: Callable[[str, str], Any] | None = None,
        event_commit: Callable[[Sequence[str], McpRecoveryIdentity], Any] | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        disabled: bool = False,
    ) -> None:
        self.runtime = runtime
        self.withdraw_catalog = withdraw_catalog
        self.withdraw_instructions = withdraw_instructions
        self.rematerialize = rematerialize
        self.task_poll = task_poll
        self.task_cancel = task_cancel
        self.task_mark_unknown = task_mark_unknown
        self.event_commit = event_commit
        self.event_sink = event_sink
        self.disabled = disabled
        self._executed: dict[str, McpRecoveryStepResult] = {}
        self._lock = threading.RLock()

    def execute(
        self,
        plan: McpRecoveryPlan,
        *,
        stop_on_terminal_failure: bool = True,
    ) -> McpRecoveryExecution:
        if self.disabled:
            raise McpRecoveryDisabled("MCP recovery executor is disabled")
        results: list[McpRecoveryStepResult] = []
        stopped = False
        for step in plan.steps:
            with self._lock:
                cached = self._executed.get(step.step_id)
            if cached is not None:
                results.append(cached)
                continue
            try:
                output = self.execute_step(step, plan.identity, plan.signals)
                result = McpRecoveryStepResult(
                    step=step,
                    ok=True,
                    changed=step.action is not McpRecoveryAction.NOOP,
                    output=safe_mapping(output),
                )
            except Exception as error:
                result = McpRecoveryStepResult(
                    step=step,
                    ok=False,
                    changed=False,
                    error=type(error).__name__,
                    output={"message": str(error)[:1000]},
                )
            with self._lock:
                self._executed[step.step_id] = result
            results.append(result)
            self.emit(plan, result)
            if not result.ok and step.terminal_on_failure and stop_on_terminal_failure:
                stopped = True
                break
        return McpRecoveryExecution(plan, tuple(results), stopped)

    def execute_step(
        self,
        step: McpRecoveryStep,
        identity: McpRecoveryIdentity,
        signals: Sequence[McpRecoverySignal] = (),
    ) -> Any:
        context = {
            "run_id": identity.run_id,
            "task_id": identity.task_id,
            "node_id": identity.node_id,
            "session_id": identity.session_id,
            "worker_request_id": identity.worker_request_id,
            "cause_event_id": identity.cause_event_id,
        }
        if step.action is McpRecoveryAction.NOOP:
            return {"noop": True}
        if step.action is McpRecoveryAction.REQUIRE_AUTH:
            raise McpRecoveryConflict("MCP recovery requires authentication")
        if step.action is McpRecoveryAction.RECONNECT:
            return self.runtime.reconnect_server(step.server_id, **context)
        if step.action is McpRecoveryAction.REFRESH:
            return self.runtime.refresh_server(
                step.server_id,
                refresh_kinds=step.refresh_kinds,
                **context,
            )
        if step.action is McpRecoveryAction.WITHDRAW_CATALOG:
            return self.require_callback(self.withdraw_catalog, "withdraw_catalog")(
                step.server_id
            )
        if step.action is McpRecoveryAction.WITHDRAW_INSTRUCTIONS:
            return self.require_callback(
                self.withdraw_instructions,
                "withdraw_instructions",
            )(step.server_id, identity)
        if step.action is McpRecoveryAction.REMATERIALIZE:
            return self.require_callback(self.rematerialize, "rematerialize")(
                step.server_id,
                identity,
            )
        if step.action is McpRecoveryAction.POLL_TASK:
            return self.require_callback(self.task_poll, "task_poll")(
                step.server_id,
                step.task_handle,
            )
        if step.action is McpRecoveryAction.CANCEL_TASK:
            return self.require_callback(self.task_cancel, "task_cancel")(
                step.server_id,
                step.task_handle,
            )
        if step.action is McpRecoveryAction.MARK_TASK_UNKNOWN:
            return self.require_callback(
                self.task_mark_unknown,
                "task_mark_unknown",
            )(step.server_id, step.task_handle)
        if step.action is McpRecoveryAction.COMMIT_EVENTS:
            event_ids = tuple(
                event_id
                for signal in plan_signal_match(step, signals)
                for event_id in signal.event_ids
            )
            return self.require_callback(self.event_commit, "event_commit")(
                event_ids,
                identity,
            )
        if step.action is McpRecoveryAction.FAIL_REQUIRED_SERVER:
            raise McpRecoveryError(f"required MCP server failed: {step.server_id}")
        raise McpRecoveryError(f"unsupported MCP recovery action: {step.action}")

    @staticmethod
    def require_callback(callback: Any, name: str) -> Any:
        if not callable(callback):
            raise McpRecoveryError(f"MCP recovery callback is unavailable: {name}")
        return callback

    def emit(
        self,
        plan: McpRecoveryPlan,
        result: McpRecoveryStepResult,
    ) -> None:
        if self.event_sink is None:
            return
        event_type = getattr(EventType, "MCP_TASK_UPDATED", EventType.SYSTEM_NOTICE)
        self.event_sink(
            EventRecord(
                run_id=plan.identity.run_id,
                task_id=plan.identity.task_id,
                node_id=plan.identity.node_id or None,
                event_type=event_type,
                payload={
                    "mcp_runtime": {
                        "schema": "zyra.mcp-recovery-event.v1",
                        "plan_id": plan.plan_id,
                        "recovery_id": plan.identity.recovery_id,
                        "cause_event_id": plan.identity.cause_event_id,
                        "result": result.safe_dict(),
                    }
                },
            )
        )


def plan_signal_match(
    step: McpRecoveryStep,
    signals: Sequence[McpRecoverySignal],
) -> tuple[McpRecoverySignal, ...]:
    return tuple(
        signal
        for signal in signals
        if signal.server_id == step.server_id and signal.reason is step.reason
    )


def safe_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    for name in ("safe_dict", "to_dict"):
        method = getattr(value, name, None)
        if callable(method):
            selected = method()
            if isinstance(selected, Mapping):
                return dict(selected)
    return {"type": type(value).__name__}


__all__ = [
    "McpRecoveryAction",
    "McpRecoveryConflict",
    "McpRecoveryDisabled",
    "McpRecoveryError",
    "McpRecoveryExecution",
    "McpRecoveryExecutor",
    "McpRecoveryIdentity",
    "McpRecoveryPlan",
    "McpRecoveryPlanner",
    "McpRecoveryPolicy",
    "McpRecoveryReason",
    "McpRecoverySignal",
    "McpRecoveryStep",
    "McpRecoveryStepResult",
]
