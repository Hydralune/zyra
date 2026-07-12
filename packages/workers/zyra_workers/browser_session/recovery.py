from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, TypeVar

from .errors import (
    BrowserConnectionLost,
    BrowserFocusError,
    BrowserProfileCorrupt,
    BrowserRequestTimeout,
    BrowserRuntimeError,
    BrowserTargetDetached,
    BrowserTransportError,
    classify_browser_error,
)
from .models import browser_id, browser_now, stable_digest


T = TypeVar("T")


class DisconnectKind(StrEnum):
    INTENTIONAL = "intentional"
    SILENT_REQUEST_TIMEOUT = "silent_request_timeout"
    WEBSOCKET_DROP = "websocket_drop"
    PROCESS_EXIT = "process_exit"
    ENDPOINT_UNREACHABLE = "endpoint_unreachable"
    TARGET_DETACHED = "target_detached"
    FOCUS_LOST = "focus_lost"
    PROFILE_CORRUPT = "profile_corrupt"
    POLICY_DENIED = "policy_denied"
    UNKNOWN = "unknown"


class RecoveryAction(StrEnum):
    NOOP = "noop"
    RETRY_REQUEST = "retry_request"
    RECONNECT_TRANSPORT = "reconnect_transport"
    REDISCOVER_TARGETS = "rediscover_targets"
    RECOVER_FOCUS = "recover_focus"
    CREATE_BLANK_TARGET = "create_blank_target"
    RESTART_PROCESS = "restart_process"
    QUARANTINE_PROFILE = "quarantine_profile"
    FAIL_SESSION = "fail_session"


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class DisconnectObservation:
    session_id: str
    kind: DisconnectKind
    message: str
    operation: str = ""
    target_id: str = ""
    generation: int = 0
    intentional: bool = False
    retryable: bool = True
    process_alive: bool | None = None
    endpoint_reachable: bool | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    observation_id: str = field(default_factory=lambda: browser_id("brobs"))
    observed_at: str = field(default_factory=browser_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "kind": str(self.kind),
            "message": self.message,
            "operation": self.operation,
            "target_id": self.target_id,
            "generation": self.generation,
            "intentional": self.intentional,
            "retryable": self.retryable,
            "process_alive": self.process_alive,
            "endpoint_reachable": self.endpoint_reachable,
            "metadata": dict(self.metadata),
            "observation_id": self.observation_id,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class RecoveryStep:
    action: RecoveryAction
    attempt: int
    delay_seconds: float
    timeout_seconds: float
    required: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": str(self.action),
            "attempt": self.attempt,
            "delay_seconds": self.delay_seconds,
            "timeout_seconds": self.timeout_seconds,
            "required": self.required,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    observation: DisconnectObservation
    steps: tuple[RecoveryStep, ...]
    plan_id: str = field(default_factory=lambda: browser_id("brrecovery"))
    created_at: str = field(default_factory=browser_now)

    @property
    def fingerprint(self) -> str:
        return stable_digest({
            "observation": self.observation.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "observation": self.observation.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RecoveryStepReceipt:
    plan_id: str
    action: RecoveryAction
    attempt: int
    ok: bool
    started_at: str
    completed_at: str
    error: str = ""
    result: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "action": str(self.action),
            "attempt": self.attempt,
            "ok": self.ok,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "result": dict(self.result),
        }


@dataclass(frozen=True, slots=True)
class RecoveryReceipt:
    plan: RecoveryPlan
    ok: bool
    steps: tuple[RecoveryStepReceipt, ...]
    final_action: RecoveryAction
    error: str = ""
    completed_at: str = field(default_factory=browser_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "ok": self.ok,
            "steps": [step.to_dict() for step in self.steps],
            "final_action": str(self.final_action),
            "error": self.error,
            "completed_at": self.completed_at,
        }


@dataclass(slots=True)
class _Circuit:
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    successes: int = 0
    opened_monotonic: float = 0.0
    last_failure: str = ""
    probe_in_flight: bool = False


class BrowserRecoveryCoordinator:
    def __init__(
        self,
        *,
        reconnect_delays: Sequence[float] = (1.0, 2.0, 4.0),
        reconnect_timeout_seconds: float = 15.0,
        failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 30.0,
        receipt_limit: int = 1024,
        disabled: bool = False,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("recovery circuit failure threshold must be positive")
        if reconnect_timeout_seconds <= 0 or circuit_cooldown_seconds <= 0:
            raise ValueError("recovery timeouts must be positive")
        if any(delay < 0 for delay in reconnect_delays):
            raise ValueError("recovery delays cannot be negative")
        self.reconnect_delays = tuple(float(delay) for delay in reconnect_delays)
        self.reconnect_timeout_seconds = reconnect_timeout_seconds
        self.failure_threshold = failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds
        self.disabled = disabled
        self._circuits: dict[str, _Circuit] = {}
        self._receipts: deque[RecoveryReceipt] = deque(maxlen=max(64, receipt_limit))
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()

    def classify(
        self,
        error: BaseException | None,
        *,
        session_id: str,
        operation: str = "",
        target_id: str = "",
        generation: int = 0,
        intentional: bool = False,
        process_alive: bool | None = None,
        endpoint_reachable: bool | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DisconnectObservation:
        if intentional:
            kind = DisconnectKind.INTENTIONAL
            retryable = False
        elif isinstance(error, BrowserRequestTimeout):
            kind = DisconnectKind.SILENT_REQUEST_TIMEOUT
            retryable = True
        elif isinstance(error, (BrowserConnectionLost, BrowserTransportError)):
            kind = DisconnectKind.WEBSOCKET_DROP
            retryable = True
        elif isinstance(error, BrowserTargetDetached):
            kind = DisconnectKind.TARGET_DETACHED
            retryable = True
        elif isinstance(error, BrowserFocusError):
            kind = DisconnectKind.FOCUS_LOST
            retryable = True
        elif isinstance(error, BrowserProfileCorrupt):
            kind = DisconnectKind.PROFILE_CORRUPT
            retryable = False
        elif process_alive is False:
            kind = DisconnectKind.PROCESS_EXIT
            retryable = True
        elif endpoint_reachable is False:
            kind = DisconnectKind.ENDPOINT_UNREACHABLE
            retryable = True
        else:
            failure = classify_browser_error(error or RuntimeError("unknown disconnect"))
            kind = DisconnectKind.UNKNOWN
            retryable = failure.retryable
        return DisconnectObservation(
            session_id=session_id,
            kind=kind,
            message=str(error or kind),
            operation=operation,
            target_id=target_id,
            generation=generation,
            intentional=intentional,
            retryable=retryable,
            process_alive=process_alive,
            endpoint_reachable=endpoint_reachable,
            metadata=dict(metadata or {}),
        )

    def plan(self, observation: DisconnectObservation) -> RecoveryPlan:
        if self.disabled:
            raise BrowserRuntimeError("browser recovery coordinator is disabled", code="browser_recovery_disabled")
        steps: list[RecoveryStep] = []
        if observation.kind == DisconnectKind.INTENTIONAL:
            steps.append(RecoveryStep(RecoveryAction.NOOP, 0, 0.0, 0.1, reason="intentional disconnect"))
        elif observation.kind == DisconnectKind.SILENT_REQUEST_TIMEOUT:
            steps.append(RecoveryStep(RecoveryAction.RETRY_REQUEST, 1, 0.0, self.reconnect_timeout_seconds, reason="request timeout"))
            steps.extend(self._reconnect_steps(start_attempt=2))
        elif observation.kind in {DisconnectKind.WEBSOCKET_DROP, DisconnectKind.ENDPOINT_UNREACHABLE}:
            steps.extend(self._reconnect_steps())
            steps.append(RecoveryStep(RecoveryAction.REDISCOVER_TARGETS, len(steps) + 1, 0.0, self.reconnect_timeout_seconds))
            steps.append(RecoveryStep(RecoveryAction.RECOVER_FOCUS, len(steps) + 1, 0.0, 5.0))
        elif observation.kind == DisconnectKind.PROCESS_EXIT:
            steps.append(RecoveryStep(RecoveryAction.RESTART_PROCESS, 1, 0.0, self.reconnect_timeout_seconds))
            steps.extend(self._reconnect_steps(start_attempt=2))
            steps.append(RecoveryStep(RecoveryAction.REDISCOVER_TARGETS, len(steps) + 1, 0.0, 5.0))
            steps.append(RecoveryStep(RecoveryAction.RECOVER_FOCUS, len(steps) + 1, 0.0, 5.0))
        elif observation.kind == DisconnectKind.TARGET_DETACHED:
            steps.append(RecoveryStep(RecoveryAction.REDISCOVER_TARGETS, 1, 0.0, 5.0))
            steps.append(RecoveryStep(RecoveryAction.RECOVER_FOCUS, 2, 0.0, 5.0))
            steps.append(RecoveryStep(RecoveryAction.CREATE_BLANK_TARGET, 3, 0.0, 5.0, required=False))
        elif observation.kind == DisconnectKind.FOCUS_LOST:
            steps.append(RecoveryStep(RecoveryAction.RECOVER_FOCUS, 1, 0.0, 5.0))
            steps.append(RecoveryStep(RecoveryAction.CREATE_BLANK_TARGET, 2, 0.0, 5.0, required=False))
        elif observation.kind == DisconnectKind.PROFILE_CORRUPT:
            steps.append(RecoveryStep(RecoveryAction.QUARANTINE_PROFILE, 1, 0.0, 10.0))
            steps.append(RecoveryStep(RecoveryAction.FAIL_SESSION, 2, 0.0, 1.0))
        elif observation.retryable:
            steps.extend(self._reconnect_steps())
        else:
            steps.append(RecoveryStep(RecoveryAction.FAIL_SESSION, 1, 0.0, 1.0))
        return RecoveryPlan(observation=observation, steps=tuple(steps))

    def _reconnect_steps(self, *, start_attempt: int = 1) -> list[RecoveryStep]:
        steps: list[RecoveryStep] = []
        for offset, delay in enumerate(self.reconnect_delays):
            steps.append(
                RecoveryStep(
                    action=RecoveryAction.RECONNECT_TRANSPORT,
                    attempt=start_attempt + offset,
                    delay_seconds=delay,
                    timeout_seconds=self.reconnect_timeout_seconds,
                    reason="bounded reconnect backoff",
                )
            )
        return steps

    def execute(
        self,
        plan: RecoveryPlan,
        handlers: Mapping[RecoveryAction, Callable[[RecoveryStep], Mapping[str, Any] | None]],
    ) -> RecoveryReceipt:
        if self.disabled:
            raise BrowserRuntimeError("browser recovery coordinator is disabled", code="browser_recovery_disabled")
        session_id = plan.observation.session_id
        lock = self._session_lock(session_id)
        if not lock.acquire(blocking=False):
            raise BrowserRuntimeError("browser recovery is already running", session_id=session_id, code="browser_recovery_busy")
        receipts: list[RecoveryStepReceipt] = []
        final_action = RecoveryAction.NOOP
        error_text = ""
        try:
            if not self.allow(session_id):
                raise BrowserRuntimeError("browser recovery circuit is open", session_id=session_id, code="browser_recovery_circuit_open")
            for step in plan.steps:
                started = browser_now()
                if step.delay_seconds:
                    time.sleep(step.delay_seconds)
                handler = handlers.get(step.action)
                if handler is None:
                    if step.required:
                        receipt = RecoveryStepReceipt(
                            plan_id=plan.plan_id,
                            action=step.action,
                            attempt=step.attempt,
                            ok=False,
                            started_at=started,
                            completed_at=browser_now(),
                            error="recovery handler is missing",
                        )
                        receipts.append(receipt)
                        error_text = receipt.error
                        self.record_failure(session_id, receipt.error)
                        break
                    continue
                try:
                    result = handler(step) or {}
                except Exception as error:
                    receipt = RecoveryStepReceipt(
                        plan_id=plan.plan_id,
                        action=step.action,
                        attempt=step.attempt,
                        ok=False,
                        started_at=started,
                        completed_at=browser_now(),
                        error=f"{type(error).__name__}: {error}",
                    )
                    receipts.append(receipt)
                    error_text = receipt.error
                    self.record_failure(session_id, receipt.error)
                    if step.required:
                        continue
                else:
                    receipt = RecoveryStepReceipt(
                        plan_id=plan.plan_id,
                        action=step.action,
                        attempt=step.attempt,
                        ok=True,
                        started_at=started,
                        completed_at=browser_now(),
                        result=dict(result),
                    )
                    receipts.append(receipt)
                    final_action = step.action
                    error_text = ""
                    if step.action in {
                        RecoveryAction.RETRY_REQUEST,
                        RecoveryAction.RECONNECT_TRANSPORT,
                        RecoveryAction.RECOVER_FOCUS,
                        RecoveryAction.CREATE_BLANK_TARGET,
                        RecoveryAction.RESTART_PROCESS,
                    }:
                        self.record_success(session_id)
                        if bool(result.get("recovered", True)):
                            break
            ok = bool(receipts) and receipts[-1].ok and final_action != RecoveryAction.FAIL_SESSION
            recovery = RecoveryReceipt(
                plan=plan,
                ok=ok,
                steps=tuple(receipts),
                final_action=final_action,
                error="" if ok else error_text or "recovery exhausted",
            )
            with self._lock:
                self._receipts.append(recovery)
            return recovery
        finally:
            lock.release()

    def allow(self, session_id: str) -> bool:
        with self._lock:
            circuit = self._circuits.setdefault(session_id, _Circuit())
            if circuit.state == CircuitState.CLOSED:
                return True
            if circuit.state == CircuitState.OPEN:
                elapsed = time.monotonic() - circuit.opened_monotonic
                if elapsed < self.circuit_cooldown_seconds:
                    return False
                circuit.state = CircuitState.HALF_OPEN
                circuit.probe_in_flight = False
            if circuit.state == CircuitState.HALF_OPEN:
                if circuit.probe_in_flight:
                    return False
                circuit.probe_in_flight = True
                return True
            return False

    def record_failure(self, session_id: str, error: str) -> CircuitState:
        with self._lock:
            circuit = self._circuits.setdefault(session_id, _Circuit())
            circuit.failures += 1
            circuit.last_failure = error
            circuit.probe_in_flight = False
            if circuit.failures >= self.failure_threshold:
                circuit.state = CircuitState.OPEN
                circuit.opened_monotonic = time.monotonic()
            return circuit.state

    def record_success(self, session_id: str) -> CircuitState:
        with self._lock:
            circuit = self._circuits.setdefault(session_id, _Circuit())
            circuit.successes += 1
            circuit.failures = 0
            circuit.last_failure = ""
            circuit.probe_in_flight = False
            circuit.state = CircuitState.CLOSED
            return circuit.state

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._circuits.pop(session_id, None)

    def circuit(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            circuit = self._circuits.setdefault(session_id, _Circuit())
            return {
                "state": str(circuit.state),
                "failures": circuit.failures,
                "successes": circuit.successes,
                "last_failure": circuit.last_failure,
                "probe_in_flight": circuit.probe_in_flight,
            }

    def receipts(self, *, session_id: str = "") -> tuple[RecoveryReceipt, ...]:
        with self._lock:
            values = tuple(self._receipts)
        if session_id:
            values = tuple(item for item in values if item.plan.observation.session_id == session_id)
        return values

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runtime_id": "zyra-browser-recovery-coordinator",
                "disabled": self.disabled,
                "reconnect_delays": list(self.reconnect_delays),
                "reconnect_timeout_seconds": self.reconnect_timeout_seconds,
                "failure_threshold": self.failure_threshold,
                "circuits": {session_id: self.circuit(session_id) for session_id in self._circuits},
                "receipts": len(self._receipts),
            }

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(session_id, threading.Lock())
