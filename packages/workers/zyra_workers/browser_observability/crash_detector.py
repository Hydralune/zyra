from __future__ import annotations

import ctypes
import os
import signal
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .models import (
    BrowserObservation,
    HealthStatus,
    ObservationScope,
    Severity,
    SignalKind,
    WatchdogName,
    WatchdogSignal,
    utc_now,
)


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None:
        ...


class CrashPhase(StrEnum):
    NEW = "new"
    ATTACHED = "attached"
    HEALTHY = "healthy"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    INTENTIONAL_STOP = "intentional_stop"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class CrashDetectorPolicy:
    heartbeat_timeout_ms: int = 15_000
    request_timeout_ms: int = 30_000
    disconnect_grace_ms: int = 1_500
    exit_debounce_ms: int = 250
    max_clock_skew_ms: int = 1_000
    suppress_intentional_stop: bool = True
    emit_recovery_for_request_timeout: bool = True

    def __post_init__(self) -> None:
        values = (
            self.heartbeat_timeout_ms,
            self.request_timeout_ms,
            self.disconnect_grace_ms,
            self.exit_debounce_ms,
            self.max_clock_skew_ms,
        )
        if any(value < 0 for value in values):
            raise ValueError("crash detector timeouts must be non-negative")
        if self.heartbeat_timeout_ms == 0:
            raise ValueError("heartbeat timeout must be positive")
        if self.request_timeout_ms == 0:
            raise ValueError("request timeout must be positive")


@dataclass(slots=True)
class CrashDetectorState:
    scope: ObservationScope
    phase: CrashPhase = CrashPhase.NEW
    process_id: int | None = None
    process_exit_code: int | None = None
    process_last_alive_ms: int | None = None
    cdp_connected: bool | None = None
    cdp_last_connected_ms: int | None = None
    cdp_disconnected_at_ms: int | None = None
    last_heartbeat_ms: int | None = None
    active_request_id: str = ""
    active_request_started_ms: int | None = None
    intentional_stop_at_ms: int | None = None
    emitted_fingerprints: set[str] = field(default_factory=set)
    transition_count: int = 0
    updated_at: str = field(default_factory=utc_now)

    def snapshot(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(),
            "phase": str(self.phase),
            "process_id": self.process_id,
            "process_exit_code": self.process_exit_code,
            "process_last_alive_ms": self.process_last_alive_ms,
            "cdp_connected": self.cdp_connected,
            "cdp_last_connected_ms": self.cdp_last_connected_ms,
            "cdp_disconnected_at_ms": self.cdp_disconnected_at_ms,
            "last_heartbeat_ms": self.last_heartbeat_ms,
            "active_request_id": self.active_request_id,
            "active_request_started_ms": self.active_request_started_ms,
            "intentional_stop_at_ms": self.intentional_stop_at_ms,
            "transition_count": self.transition_count,
            "updated_at": self.updated_at,
        }


class SystemProcessProbe:
    """Cross-platform PID liveness probe without a new dependency or subprocess."""

    STILL_ACTIVE = 259
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def running(
        self,
        process_id: int,
    ) -> bool:
        if process_id <= 0:
            return False
        if os.name == "nt":
            return self._running_windows(process_id)
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def exit_code(
        self,
        process_id: int,
    ) -> int | None:
        if process_id <= 0:
            return None
        if os.name != "nt":
            return None
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id,
        )
        if not handle:
            return None
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return None if code.value == self.STILL_ACTIVE else int(code.value)
        finally:
            kernel32.CloseHandle(handle)

    def terminate(
        self,
        process_id: int,
    ) -> None:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0001, False, process_id)
            if not handle:
                raise ProcessLookupError(process_id)
            try:
                if not kernel32.TerminateProcess(handle, 1):
                    raise OSError(f"TerminateProcess failed for pid {process_id}")
            finally:
                kernel32.CloseHandle(handle)
            return
        os.kill(process_id, signal.SIGKILL)

    def _running_windows(
        self,
        process_id: int,
    ) -> bool:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id,
        )
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == self.STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)


class BrowserCrashDetector:
    """Attached Zyra crash detector.

    Browser Use's CrashWatchdog is intentionally not attached upstream. This
    detector therefore owns explicit process-exit, CDP-disconnect and silent
    timeout signals. It does not plan recovery; it only emits typed evidence.
    """

    def __init__(
        self,
        *,
        policy: CrashDetectorPolicy | None = None,
        process_probe: SystemProcessProbe | None = None,
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        self.policy = policy or CrashDetectorPolicy()
        self.process_probe = process_probe or SystemProcessProbe()
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._guard = threading.RLock()
        self._states: dict[str, CrashDetectorState] = {}

    def attach(
        self,
        scope: ObservationScope,
        *,
        process_id: int | None = None,
        cdp_connected: bool | None = None,
    ) -> CrashDetectorState:
        now = self.monotonic_ms()
        with self._guard:
            state = self._states.get(scope.key)
            if state is None:
                state = CrashDetectorState(scope=scope)
                self._states[scope.key] = state
            self._assert_scope(state, scope)
            state.process_id = process_id if process_id is not None else state.process_id
            state.cdp_connected = cdp_connected
            if cdp_connected:
                state.cdp_last_connected_ms = now
                state.cdp_disconnected_at_ms = None
            state.phase = CrashPhase.ATTACHED
            state.transition_count += 1
            state.updated_at = utc_now()
            return state

    def heartbeat(
        self,
        scope: ObservationScope,
        *,
        at_ms: int | None = None,
    ) -> tuple[WatchdogSignal, ...]:
        now = self._normalize_now(at_ms)
        with self._guard:
            state = self._require_state(scope)
            state.last_heartbeat_ms = now
            if state.phase not in {
                CrashPhase.CONFIRMED,
                CrashPhase.INTENTIONAL_STOP,
                CrashPhase.CLOSED,
            }:
                state.phase = CrashPhase.HEALTHY
            state.transition_count += 1
            state.updated_at = utc_now()
            signal = self._signal(
                state,
                SignalKind.CDP_CONNECTED,
                HealthStatus.HEALTHY,
                Severity.INFO,
                "Browser heartbeat observed.",
                sequence=state.transition_count,
                retryable=False,
                terminal=False,
                metadata={"heartbeat_ms": now},
            )
            return self._dedupe(state, (signal,))

    def cdp_connected(
        self,
        scope: ObservationScope,
        *,
        at_ms: int | None = None,
    ) -> tuple[WatchdogSignal, ...]:
        now = self._normalize_now(at_ms)
        with self._guard:
            state = self._require_state(scope)
            state.cdp_connected = True
            state.cdp_last_connected_ms = now
            state.cdp_disconnected_at_ms = None
            if state.phase not in {
                CrashPhase.CONFIRMED,
                CrashPhase.INTENTIONAL_STOP,
                CrashPhase.CLOSED,
            }:
                state.phase = CrashPhase.HEALTHY
            state.transition_count += 1
            state.updated_at = utc_now()
            return self._dedupe(
                state,
                (
                    self._signal(
                        state,
                        SignalKind.CDP_CONNECTED,
                        HealthStatus.HEALTHY,
                        Severity.INFO,
                        "CDP transport connected.",
                        sequence=state.transition_count,
                        metadata={"connected_at_ms": now},
                    ),
                ),
            )

    def cdp_disconnected(
        self,
        scope: ObservationScope,
        *,
        reason: str = "",
        at_ms: int | None = None,
    ) -> tuple[WatchdogSignal, ...]:
        now = self._normalize_now(at_ms)
        with self._guard:
            state = self._require_state(scope)
            state.cdp_connected = False
            state.cdp_disconnected_at_ms = now
            state.transition_count += 1
            state.updated_at = utc_now()
            if self._intentional(state):
                state.phase = CrashPhase.INTENTIONAL_STOP
                return ()
            state.phase = CrashPhase.SUSPECTED
            signal = self._signal(
                state,
                SignalKind.CDP_DISCONNECTED,
                HealthStatus.DEGRADED,
                Severity.WARNING,
                "CDP transport disconnected unexpectedly.",
                sequence=state.transition_count,
                retryable=True,
                terminal=False,
                metadata={
                    "reason": reason,
                    "disconnected_at_ms": now,
                    "confirmation_grace_ms": self.policy.disconnect_grace_ms,
                },
            )
            return self._dedupe(state, (signal,))

    def request_started(
        self,
        scope: ObservationScope,
        request_id: str,
        *,
        at_ms: int | None = None,
    ) -> None:
        now = self._normalize_now(at_ms)
        with self._guard:
            state = self._require_state(scope)
            if state.active_request_id and state.active_request_id != request_id:
                raise RuntimeError("crash detector already tracks another active request")
            state.active_request_id = str(request_id)
            state.active_request_started_ms = now
            state.transition_count += 1
            state.updated_at = utc_now()

    def request_finished(
        self,
        scope: ObservationScope,
        request_id: str,
    ) -> None:
        with self._guard:
            state = self._require_state(scope)
            if state.active_request_id and state.active_request_id != request_id:
                raise RuntimeError("request finish identity differs from active request")
            state.active_request_id = ""
            state.active_request_started_ms = None
            state.transition_count += 1
            state.updated_at = utc_now()

    def intentional_stop(
        self,
        scope: ObservationScope,
        *,
        at_ms: int | None = None,
    ) -> None:
        now = self._normalize_now(at_ms)
        with self._guard:
            state = self._require_state(scope)
            state.intentional_stop_at_ms = now
            state.phase = CrashPhase.INTENTIONAL_STOP
            state.transition_count += 1
            state.updated_at = utc_now()

    def poll(
        self,
        scope: ObservationScope,
        *,
        at_ms: int | None = None,
        process_handle: ProcessHandle | None = None,
    ) -> tuple[WatchdogSignal, ...]:
        now = self._normalize_now(at_ms)
        with self._guard:
            state = self._require_state(scope)
            signals: list[WatchdogSignal] = []
            process_running = self._process_running(state, process_handle)
            exit_code = self._process_exit_code(state, process_handle)
            if process_running:
                state.process_last_alive_ms = now
            elif state.process_id is not None and not self._intentional(state):
                last_alive = state.process_last_alive_ms
                debounce_elapsed = (
                    last_alive is None
                    or now - last_alive >= self.policy.exit_debounce_ms
                )
                if debounce_elapsed:
                    state.process_exit_code = exit_code
                    state.phase = CrashPhase.CONFIRMED
                    signals.append(
                        self._signal(
                            state,
                            SignalKind.PROCESS_EXITED,
                            HealthStatus.TERMINATED,
                            Severity.CRITICAL,
                            "Browser process exited unexpectedly.",
                            sequence=state.transition_count + 1,
                            retryable=True,
                            terminal=True,
                            metadata={
                                "process_id": state.process_id,
                                "exit_code": exit_code,
                                "detected_at_ms": now,
                            },
                        )
                    )
            if (
                state.cdp_connected is False
                and state.cdp_disconnected_at_ms is not None
                and not self._intentional(state)
                and now - state.cdp_disconnected_at_ms >= self.policy.disconnect_grace_ms
            ):
                state.phase = CrashPhase.CONFIRMED
                signals.append(
                    self._signal(
                        state,
                        SignalKind.CDP_DISCONNECTED,
                        HealthStatus.UNHEALTHY,
                        Severity.ERROR,
                        "CDP disconnect exceeded its confirmation grace period.",
                        sequence=state.transition_count + len(signals) + 1,
                        retryable=True,
                        terminal=True,
                        metadata={
                            "disconnected_at_ms": state.cdp_disconnected_at_ms,
                            "detected_at_ms": now,
                            "grace_ms": self.policy.disconnect_grace_ms,
                        },
                    )
                )
            if (
                state.last_heartbeat_ms is not None
                and not self._intentional(state)
                and now - state.last_heartbeat_ms >= self.policy.heartbeat_timeout_ms
            ):
                state.phase = CrashPhase.CONFIRMED
                signals.append(
                    self._signal(
                        state,
                        SignalKind.HEARTBEAT_LATE,
                        HealthStatus.UNHEALTHY,
                        Severity.ERROR,
                        "Browser heartbeat timed out without a terminal event.",
                        sequence=state.transition_count + len(signals) + 1,
                        retryable=True,
                        terminal=True,
                        metadata={
                            "last_heartbeat_ms": state.last_heartbeat_ms,
                            "detected_at_ms": now,
                            "elapsed_ms": now - state.last_heartbeat_ms,
                            "timeout_ms": self.policy.heartbeat_timeout_ms,
                        },
                    )
                )
            if (
                state.active_request_id
                and state.active_request_started_ms is not None
                and now - state.active_request_started_ms >= self.policy.request_timeout_ms
            ):
                state.phase = CrashPhase.CONFIRMED
                signals.append(
                    self._signal(
                        state,
                        SignalKind.REQUEST_TIMEOUT,
                        HealthStatus.UNHEALTHY,
                        Severity.ERROR,
                        "Browser request exceeded its deterministic timeout.",
                        sequence=state.transition_count + len(signals) + 1,
                        retryable=self.policy.emit_recovery_for_request_timeout,
                        terminal=True,
                        metadata={
                            "request_id": state.active_request_id,
                            "started_at_ms": state.active_request_started_ms,
                            "detected_at_ms": now,
                            "elapsed_ms": now - state.active_request_started_ms,
                            "timeout_ms": self.policy.request_timeout_ms,
                            "outcome_unknown": True,
                        },
                    )
                )
            state.transition_count += len(signals) + 1
            state.updated_at = utc_now()
            return self._dedupe(state, tuple(signals))

    def observe(
        self,
        observation: BrowserObservation,
    ) -> tuple[WatchdogSignal, ...]:
        scope = observation.scope
        with self._guard:
            state = self._states.get(scope.key)
            if state is None:
                state = self.attach(
                    scope,
                    process_id=observation.process_id,
                    cdp_connected=observation.cdp_connected,
                )
            if observation.process_id is not None:
                state.process_id = observation.process_id
            if observation.last_heartbeat_ms is not None:
                state.last_heartbeat_ms = observation.last_heartbeat_ms
            if observation.active_request_id:
                state.active_request_id = observation.active_request_id
                state.active_request_started_ms = observation.active_request_started_ms
            if observation.intentional_stop:
                self.intentional_stop(scope, at_ms=observation.monotonic_ms)
            if observation.cdp_connected is True:
                self.cdp_connected(scope, at_ms=observation.monotonic_ms)
            elif observation.cdp_connected is False:
                self.cdp_disconnected(
                    scope,
                    reason=observation.cdp_disconnect_reason,
                    at_ms=observation.monotonic_ms,
                )
        return self.poll(
            scope,
            at_ms=observation.monotonic_ms,
        )

    def state(
        self,
        scope: ObservationScope,
    ) -> dict[str, Any]:
        with self._guard:
            state = self._require_state(scope)
            return state.snapshot()

    def close(
        self,
        scope: ObservationScope,
    ) -> None:
        with self._guard:
            state = self._require_state(scope)
            state.phase = CrashPhase.CLOSED
            state.transition_count += 1
            state.updated_at = utc_now()

    def snapshots(
        self,
        *,
        task_id: str = "",
    ) -> tuple[dict[str, Any], ...]:
        with self._guard:
            return tuple(
                state.snapshot()
                for state in self._states.values()
                if not task_id or state.scope.task_id == task_id
            )

    def _normalize_now(
        self,
        at_ms: int | None,
    ) -> int:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        if now < 0:
            raise ValueError("monotonic time must not be negative")
        return now

    def _require_state(
        self,
        scope: ObservationScope,
    ) -> CrashDetectorState:
        state = self._states.get(scope.key)
        if state is None:
            raise RuntimeError("crash detector is not attached to observation scope")
        self._assert_scope(state, scope)
        return state

    @staticmethod
    def _assert_scope(
        state: CrashDetectorState,
        scope: ObservationScope,
    ) -> None:
        if state.scope != scope:
            raise RuntimeError("crash detector scope key collision")

    def _process_running(
        self,
        state: CrashDetectorState,
        process_handle: ProcessHandle | None,
    ) -> bool:
        if process_handle is not None:
            if state.process_id is None:
                state.process_id = int(process_handle.pid)
            return process_handle.poll() is None
        if state.process_id is None:
            return True
        return self.process_probe.running(state.process_id)

    def _process_exit_code(
        self,
        state: CrashDetectorState,
        process_handle: ProcessHandle | None,
    ) -> int | None:
        if process_handle is not None:
            return process_handle.poll()
        if state.process_id is None:
            return None
        return self.process_probe.exit_code(state.process_id)

    def _intentional(
        self,
        state: CrashDetectorState,
    ) -> bool:
        return (
            self.policy.suppress_intentional_stop
            and state.intentional_stop_at_ms is not None
        )

    @staticmethod
    def _signal(
        state: CrashDetectorState,
        kind: SignalKind,
        status: HealthStatus,
        severity: Severity,
        summary: str,
        *,
        sequence: int,
        retryable: bool = False,
        terminal: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> WatchdogSignal:
        return WatchdogSignal(
            scope=state.scope,
            watchdog=WatchdogName.CRASH_DETECTOR,
            kind=kind,
            status=status,
            severity=severity,
            summary=summary,
            sequence=sequence,
            retryable=retryable,
            terminal=terminal,
            metadata=dict(metadata or {}),
        )

    @staticmethod
    def _dedupe(
        state: CrashDetectorState,
        signals: tuple[WatchdogSignal, ...],
    ) -> tuple[WatchdogSignal, ...]:
        output: list[WatchdogSignal] = []
        for item in signals:
            key = (
                f"{item.kind}:{item.status}:{item.terminal}:"
                f"{item.metadata.get('process_id', '')}:"
                f"{item.metadata.get('request_id', '')}:"
                f"{item.metadata.get('disconnected_at_ms', '')}:"
                f"{item.metadata.get('last_heartbeat_ms', '')}"
            )
            if key in state.emitted_fingerprints:
                continue
            state.emitted_fingerprints.add(key)
            output.append(item)
        return tuple(output)
