from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from .contracts import CorrelationRefs, runtime_id, utc_now
from .deadline_runtime import TerminalResultReceipt, ToolExecutionLease
from .provider_supervision import (
    ProviderAttemptPermit,
    ProviderFailureOutcome,
    ProviderRetryPolicy,
)
from .state_store import FaultStateStore

if TYPE_CHECKING:
    from .runtime import RuntimeWatchdog


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...


class SourceSessionPhase(StrEnum):
    ATTACHING = "attaching"
    RUNNING = "running"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    STOPPING = "stopping"
    STOPPED = "stopped"
    DISABLED = "disabled"


class SourceKind(StrEnum):
    TOOL = "tool"
    WORKER = "worker"
    BROWSER = "browser"
    PROVIDER = "provider"
    MCP = "mcp"
    WORKSPACE = "workspace"


@dataclass(slots=True)
class SessionSourceBinding:
    source_id: str
    source_kind: SourceKind
    refs: CorrelationRefs
    generation: int
    phase: SourceSessionPhase
    attached_at_ms: int
    last_heartbeat_ms: int
    revision: int = 1
    process_key: str = ""
    process_pid: int | None = None
    heartbeat_sequence: int = 0
    stop_reason: str = ""
    failure_count: int = 0
    restart_count: int = 0
    last_error_code: str = ""
    last_observation_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def scope_key(self) -> str:
        return f"{self.refs.run_id}:{self.refs.task_id}:{self.source_kind.value}:{self.source_id}"

    @property
    def accepts_events(self) -> bool:
        return self.phase in {SourceSessionPhase.RUNNING, SourceSessionPhase.DEGRADED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "refs": self.refs.to_dict(),
            "generation": self.generation,
            "phase": self.phase.value,
            "attached_at_ms": self.attached_at_ms,
            "last_heartbeat_ms": self.last_heartbeat_ms,
            "revision": self.revision,
            "process_key": self.process_key,
            "process_pid": self.process_pid,
            "heartbeat_sequence": self.heartbeat_sequence,
            "stop_reason": self.stop_reason,
            "failure_count": self.failure_count,
            "restart_count": self.restart_count,
            "last_error_code": self.last_error_code,
            "last_observation_ids": list(self.last_observation_ids[-50:]),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SourceLifecycleReceipt:
    source_id: str
    source_kind: SourceKind
    generation: int
    action: str
    phase: SourceSessionPhase
    changed: bool
    revision: int
    receipt_id: str = field(default_factory=lambda: runtime_id("source-lifecycle"))
    created_at: str = field(default_factory=utc_now)
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.watchdog-source-lifecycle-receipt/v1",
            "receipt_id": self.receipt_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "generation": self.generation,
            "action": self.action,
            "phase": self.phase.value,
            "changed": self.changed,
            "revision": self.revision,
            "created_at": self.created_at,
            "details": dict(self.details),
        }


class RuntimeSourceSessionManager:
    """Binds real observation sources to one run/task/session scope.

    The manager crops Browser Use's explicit attach/start/stop lifecycle into a
    generation-fenced Zyra boundary. It does not own any source's canonical
    state: worker leases, browser process/CDP state, provider attempts and MCP
    transport state remain with their existing runtime owners. This class owns
    only integration binding and restart cursors, persisted through the
    existing ``FaultStateStore.runtime_metadata`` table.
    """

    _METADATA_KEY = "M1-S07B-02.source-sessions"

    def __init__(
        self,
        watchdog: RuntimeWatchdog,
        store: FaultStateStore,
        *,
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        self.watchdog = watchdog
        self.store = store
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._guard = threading.RLock()
        self._bindings: dict[str, SessionSourceBinding] = {}
        self._metadata_revision = 0
        self._lifecycle_receipts: list[SourceLifecycleReceipt] = []
        self._tool_leases: dict[str, ToolExecutionLease] = {}
        self._provider_permits: dict[str, ProviderAttemptPermit] = {}
        self._process_handles: dict[str, ProcessHandle] = {}
        self._restore()

    def attach_worker(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        interval_ms: int,
        grace_intervals: int = 3,
        process_handle: ProcessHandle | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceLifecycleReceipt:
        if not refs.worker_id:
            raise ValueError("worker source requires worker_id")
        binding, changed = self._attach_binding(
            SourceKind.WORKER,
            refs.worker_id,
            refs,
            generation=generation,
            metadata=metadata,
        )
        if changed:
            self.watchdog.worker_heartbeats.bind(
                refs,
                generation=generation,
                interval_ms=interval_ms,
                grace_intervals=grace_intervals,
                metadata={
                    **dict(metadata or {}),
                    "integration_scope_key": binding.scope_key,
                    "session_binding_revision": binding.revision,
                },
            )
        if process_handle is not None:
            process_key = f"worker:{refs.worker_id}"
            self.watchdog.processes.bind(
                process_key,
                refs,
                process_handle,
                generation=generation,
                metadata={
                    **dict(metadata or {}),
                    "source_kind": SourceKind.WORKER.value,
                    "integration_scope_key": binding.scope_key,
                },
            )
            with self._guard:
                binding.process_key = process_key
                binding.process_pid = int(process_handle.pid)
                self._process_handles[process_key] = process_handle
                binding.revision += 1
                self._persist_locked()
        return self._receipt(binding, "attach", changed, {"heartbeat_interval_ms": interval_ms})

    def worker_heartbeat(
        self,
        worker_id: str,
        *,
        generation: int,
        sequence: int,
        at_ms: int | None = None,
    ) -> SourceLifecycleReceipt:
        binding = self._require(SourceKind.WORKER, worker_id, generation)
        accepted = self.watchdog.worker_heartbeats.heartbeat(
            worker_id,
            generation=generation,
            sequence=sequence,
            at_ms=at_ms,
        )
        with self._guard:
            if accepted:
                binding.last_heartbeat_ms = self._now(at_ms)
                binding.heartbeat_sequence = sequence
                binding.phase = SourceSessionPhase.RUNNING
                binding.revision += 1
                self._persist_locked()
        return self._receipt(
            binding,
            "heartbeat",
            accepted,
            {"sequence": sequence, "stale_or_duplicate": not accepted},
        )

    def attach_browser(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        process_id: int | None = None,
        process_handle: ProcessHandle | None = None,
        cdp_connected: bool | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceLifecycleReceipt:
        if not refs.browser_session_id:
            raise ValueError("browser source requires browser_session_id")
        binding, changed = self._attach_binding(
            SourceKind.BROWSER,
            refs.browser_session_id,
            refs,
            generation=generation,
            metadata=metadata,
        )
        if changed or process_handle is not None:
            source = self.watchdog.browser_source.attach(
                refs,
                generation=generation,
                process_id=process_id,
                process_handle=process_handle,
                cdp_connected=cdp_connected,
            )
            with self._guard:
                binding.process_pid = source.attached_process_id
                if process_handle is not None:
                    binding.process_key = f"browser:{refs.browser_session_id}"
                    self._process_handles[binding.process_key] = process_handle
                binding.revision += 1
                self._persist_locked()
        return self._receipt(
            binding,
            "attach",
            changed,
            {"cdp_connected": cdp_connected, "process_id": binding.process_pid},
        )

    def browser_heartbeat(
        self,
        browser_session_id: str,
        *,
        generation: int,
        at_ms: int | None = None,
    ) -> SourceLifecycleReceipt:
        binding = self._require(SourceKind.BROWSER, browser_session_id, generation)
        signals = self.watchdog.browser_source.heartbeat(
            browser_session_id,
            generation=generation,
            at_ms=at_ms,
        )
        with self._guard:
            binding.last_heartbeat_ms = self._now(at_ms)
            binding.heartbeat_sequence += 1
            binding.phase = SourceSessionPhase.RUNNING
            binding.last_observation_ids.extend(str(item.signal_id) for item in signals)
            binding.revision += 1
            self._persist_locked()
        return self._receipt(
            binding,
            "heartbeat",
            True,
            {"source_signal_ids": [str(item.signal_id) for item in signals]},
        )

    def browser_cdp_connected(
        self,
        browser_session_id: str,
        *,
        generation: int,
        at_ms: int | None = None,
    ) -> SourceLifecycleReceipt:
        binding = self._require(SourceKind.BROWSER, browser_session_id, generation)
        signals = self.watchdog.browser_source.cdp_connected(
            browser_session_id,
            generation=generation,
            at_ms=at_ms,
        )
        with self._guard:
            binding.phase = SourceSessionPhase.RUNNING
            binding.last_heartbeat_ms = self._now(at_ms)
            binding.last_error_code = ""
            binding.last_observation_ids.extend(str(item.signal_id) for item in signals)
            binding.revision += 1
            self._persist_locked()
        return self._receipt(
            binding,
            "cdp_connected",
            True,
            {"source_signal_ids": [str(item.signal_id) for item in signals]},
        )

    def browser_cdp_disconnected(
        self,
        browser_session_id: str,
        *,
        generation: int,
        reason_code: str,
        at_ms: int | None = None,
    ) -> SourceLifecycleReceipt:
        binding = self._require(SourceKind.BROWSER, browser_session_id, generation)
        if not reason_code.strip():
            raise ValueError("browser disconnect requires structured reason_code")
        signals = self.watchdog.browser_source.cdp_disconnected(
            browser_session_id,
            generation=generation,
            reason=reason_code,
            at_ms=at_ms,
        )
        with self._guard:
            binding.phase = SourceSessionPhase.DEGRADED
            binding.failure_count += 1
            binding.last_error_code = reason_code
            binding.last_observation_ids.extend(str(item.signal_id) for item in signals)
            binding.revision += 1
            self._persist_locked()
        return self._receipt(
            binding,
            "cdp_disconnected",
            bool(signals),
            {"reason_code": reason_code, "source_signal_ids": [str(item.signal_id) for item in signals]},
        )

    def attach_provider(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        policy: ProviderRetryPolicy | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceLifecycleReceipt:
        if not refs.provider_id:
            raise ValueError("provider source requires provider_id")
        binding, changed = self._attach_binding(
            SourceKind.PROVIDER,
            refs.provider_id,
            refs,
            generation=generation,
            metadata=metadata,
        )
        if changed:
            self.watchdog.provider_attempts.bind(
                refs,
                generation=generation,
                policy=policy,
                metadata={
                    **dict(metadata or {}),
                    "integration_scope_key": binding.scope_key,
                },
            )
        return self._receipt(binding, "attach", changed, {})

    def begin_provider_attempt(
        self,
        provider_id: str,
        *,
        generation: int,
        attempt_id: str,
        at_ms: int | None = None,
    ) -> ProviderAttemptPermit:
        binding = self._require(SourceKind.PROVIDER, provider_id, generation)
        permit = self.watchdog.provider_attempts.begin_attempt(
            provider_id,
            generation=generation,
            attempt_id=attempt_id,
            at_ms=at_ms,
        )
        with self._guard:
            self._provider_permits[attempt_id] = permit
            binding.last_heartbeat_ms = self._now(at_ms)
            binding.revision += 1
            self._persist_locked()
        return permit

    def provider_succeeded(
        self,
        attempt_id: str,
        *,
        at_ms: int | None = None,
    ) -> SourceLifecycleReceipt:
        permit = self._take_provider_permit(attempt_id)
        binding = self._require(SourceKind.PROVIDER, permit.provider_id, permit.generation)
        self.watchdog.provider_attempts.succeeded(permit, at_ms=at_ms)
        with self._guard:
            binding.phase = SourceSessionPhase.RUNNING
            binding.last_heartbeat_ms = self._now(at_ms)
            binding.last_error_code = ""
            binding.revision += 1
            self._persist_locked()
        return self._receipt(binding, "provider_succeeded", True, {"attempt_id": attempt_id})

    def provider_failed(
        self,
        attempt_id: str,
        *,
        status_code: int | None = None,
        error_type: str = "",
        error_code: str = "provider_error",
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
        at_ms: int | None = None,
    ) -> ProviderFailureOutcome:
        permit = self._take_provider_permit(attempt_id)
        binding = self._require(SourceKind.PROVIDER, permit.provider_id, permit.generation)
        outcome = self.watchdog.provider_attempts.failed(
            permit,
            status_code=status_code,
            error_type=error_type,
            error_code=error_code,
            retryable=retryable,
            details=details,
            at_ms=at_ms,
        )
        with self._guard:
            binding.phase = SourceSessionPhase.DEGRADED
            binding.failure_count += 1
            binding.last_error_code = error_code
            if outcome.observation is not None:
                binding.last_observation_ids.append(outcome.observation.observation_id)
            binding.revision += 1
            self._persist_locked()
        return outcome

    def attach_mcp(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        max_reconnect_attempts: int = 4,
        base_backoff_ms: int = 250,
        process_handle: ProcessHandle | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceLifecycleReceipt:
        if not refs.mcp_server_id:
            raise ValueError("MCP source requires mcp_server_id")
        binding, changed = self._attach_binding(
            SourceKind.MCP,
            refs.mcp_server_id,
            refs,
            generation=generation,
            metadata=metadata,
        )
        if changed:
            self.watchdog.mcp_transports.connected(
                refs,
                generation=generation,
                max_reconnect_attempts=max_reconnect_attempts,
                base_backoff_ms=base_backoff_ms,
                metadata={
                    **dict(metadata or {}),
                    "integration_scope_key": binding.scope_key,
                },
            )
        if process_handle is not None:
            process_key = f"mcp:{refs.mcp_server_id}"
            self.watchdog.processes.bind(
                process_key,
                refs,
                process_handle,
                generation=generation,
                metadata={
                    **dict(metadata or {}),
                    "source_kind": SourceKind.MCP.value,
                    "integration_scope_key": binding.scope_key,
                },
            )
            with self._guard:
                binding.process_key = process_key
                binding.process_pid = int(process_handle.pid)
                self._process_handles[process_key] = process_handle
                binding.revision += 1
                self._persist_locked()
        return self._receipt(binding, "attach", changed, {})

    def mcp_disconnected(
        self,
        server_id: str,
        *,
        generation: int,
        reason_code: str,
        at_ms: int | None = None,
    ) -> SourceLifecycleReceipt:
        binding = self._require(SourceKind.MCP, server_id, generation)
        if not reason_code.strip():
            raise ValueError("MCP disconnect requires structured reason_code")
        observation = self.watchdog.mcp_transports.disconnected(
            server_id,
            generation=generation,
            reason_code=reason_code,
            at_ms=at_ms,
        )
        with self._guard:
            binding.phase = SourceSessionPhase.DEGRADED
            binding.failure_count += 1
            binding.last_error_code = reason_code
            if observation is not None:
                binding.last_observation_ids.append(observation.observation_id)
            binding.revision += 1
            self._persist_locked()
        return self._receipt(
            binding,
            "mcp_disconnected",
            observation is not None,
            {
                "reason_code": reason_code,
                "observation_id": observation.observation_id if observation else "",
                "transport_phase": "disconnected",
                "breaker_evaluated_on_reconnect": True,
            },
        )

    def mcp_reconnect_failed(
        self,
        server_id: str,
        *,
        generation: int,
        at_ms: int | None = None,
    ) -> SourceLifecycleReceipt:
        binding = self._require(SourceKind.MCP, server_id, generation)
        transport = self.watchdog.mcp_transports.reconnect_failed(
            server_id,
            generation=generation,
            at_ms=at_ms,
        )
        with self._guard:
            binding.phase = (
                SourceSessionPhase.DISABLED
                if transport.phase.value == "breaker_open"
                else SourceSessionPhase.RESTARTING
            )
            binding.failure_count += 1
            binding.revision += 1
            self._persist_locked()
        return self._receipt(
            binding,
            "mcp_reconnect_failed",
            True,
            {
                "transport_phase": transport.phase.value,
                "attempt": transport.reconnect_attempt,
                "next_retry_at_ms": transport.next_retry_at_ms,
            },
        )

    def mcp_reconnect_succeeded(
        self,
        server_id: str,
        *,
        generation: int,
    ) -> SourceLifecycleReceipt:
        binding = self._require(SourceKind.MCP, server_id, generation)
        transport = self.watchdog.mcp_transports.reconnect_succeeded(
            server_id,
            generation=generation,
        )
        with self._guard:
            binding.phase = SourceSessionPhase.RUNNING
            binding.restart_count += 1
            binding.last_error_code = ""
            binding.revision += 1
            self._persist_locked()
        return self._receipt(
            binding,
            "mcp_reconnect_succeeded",
            True,
            {"transport_phase": transport.phase.value},
        )

    def arm_tool(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        deadline_ms: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolExecutionLease:
        if not refs.tool_call_id:
            raise ValueError("tool source requires tool_call_id")
        binding, _ = self._attach_binding(
            SourceKind.TOOL,
            refs.tool_call_id,
            refs,
            generation=generation,
            metadata=metadata,
        )
        lease = self.watchdog.tool_execution.arm(
            refs,
            generation=generation,
            deadline_ms=deadline_ms,
            metadata={
                **dict(metadata or {}),
                "integration_scope_key": binding.scope_key,
            },
        )
        with self._guard:
            self._tool_leases[refs.tool_call_id] = lease
            binding.revision += 1
            self._persist_locked()
        return lease

    def settle_tool(
        self,
        tool_call_id: str,
        *,
        generation: int,
        token: str,
        result_id: str,
        ok: bool,
        cancelled: bool = False,
    ) -> TerminalResultReceipt:
        binding = self._require(SourceKind.TOOL, tool_call_id, generation)
        receipt = self.watchdog.tool_execution.accept_result(
            tool_call_id,
            generation=generation,
            token=token,
            result_id=result_id,
            ok=ok,
            cancelled=cancelled,
        )
        with self._guard:
            if receipt.accepted:
                binding.phase = SourceSessionPhase.STOPPED
                binding.stop_reason = "terminal tool result accepted"
            elif receipt.disposition.value == "late_after_timeout":
                binding.phase = SourceSessionPhase.DEGRADED
                binding.last_error_code = "tool_timeout"
            binding.revision += 1
            self._persist_locked()
        return receipt

    def restart(
        self,
        source_kind: SourceKind | str,
        source_id: str,
        *,
        from_generation: int,
        refs: CorrelationRefs,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceLifecycleReceipt:
        kind = SourceKind(source_kind)
        current = self._require(kind, source_id, from_generation)
        next_generation = from_generation + 1
        with self._guard:
            current.phase = SourceSessionPhase.RESTARTING
            current.revision += 1
            current.stop_reason = "generation restart"
            self._persist_locked()
        replacement, changed = self._attach_binding(
            kind,
            source_id,
            refs,
            generation=next_generation,
            metadata={**current.metadata, **dict(metadata or {})},
        )
        with self._guard:
            replacement.restart_count = current.restart_count + 1
            replacement.revision += 1
            self._persist_locked()
        return self._receipt(
            replacement,
            "restart",
            changed,
            {"from_generation": from_generation, "to_generation": next_generation},
        )

    def stop(
        self,
        source_kind: SourceKind | str,
        source_id: str,
        *,
        generation: int,
        reason: str,
        disable: bool = False,
    ) -> SourceLifecycleReceipt:
        kind = SourceKind(source_kind)
        binding = self._require(kind, source_id, generation)
        if not reason.strip():
            raise ValueError("source stop requires a reason")
        with self._guard:
            if binding.phase in {SourceSessionPhase.STOPPED, SourceSessionPhase.DISABLED}:
                return self._receipt(binding, "disable" if disable else "stop", False, {"reason": reason})
            binding.phase = SourceSessionPhase.STOPPING
            binding.revision += 1
            self._persist_locked()
        if kind is SourceKind.WORKER:
            self.watchdog.worker_heartbeats.stop_binding(source_id, generation=generation)
        elif kind is SourceKind.BROWSER:
            try:
                self.watchdog.browser_source.intentional_stop(source_id, generation=generation)
            finally:
                self.watchdog.browser_source.close(source_id, generation=generation)
        elif kind is SourceKind.PROVIDER:
            self.watchdog.provider_attempts.stop(source_id, generation=generation)
        elif kind is SourceKind.MCP:
            self.watchdog.mcp_transports.stop_transport(source_id, generation=generation)
        if binding.process_key:
            try:
                self.watchdog.processes.expected_stop(binding.process_key)
            except KeyError:
                pass
        with self._guard:
            binding.phase = SourceSessionPhase.DISABLED if disable else SourceSessionPhase.STOPPED
            binding.stop_reason = reason
            binding.revision += 1
            self._persist_locked()
        return self._receipt(
            binding,
            "disable" if disable else "stop",
            True,
            {"reason": reason, "capture_path_removed": True},
        )

    def tick(self, *, at_ms: int | None = None) -> Mapping[str, Any]:
        now = self._now(at_ms)
        before = {item.signal_id for item in self.store.signals(limit=500)}
        tick = dict(self.watchdog.tick(at_ms=now))
        after = self.store.signals(limit=500)
        new_signals = [item for item in after if item.signal_id not in before]
        with self._guard:
            for signal in new_signals:
                binding = self._binding_for_signal(signal.refs)
                if binding is None:
                    continue
                binding.last_observation_ids.append(signal.refs.observation_id)
                binding.last_error_code = signal.observed_code
                binding.failure_count += 1
                binding.phase = SourceSessionPhase.DEGRADED
                binding.revision += 1
            if new_signals:
                self._persist_locked()
        return {
            "schema": "zyra.watchdog-source-session-tick/v1",
            "at_ms": now,
            "watchdog": tick,
            "new_signal_ids": [item.signal_id for item in new_signals],
            "new_fault_kinds": [item.kind.value for item in new_signals],
            "binding_count": len(self._bindings),
        }

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        with self._guard:
            bindings = {
                key: value.to_dict()
                for key, value in sorted(self._bindings.items())
                if not task_id or value.refs.task_id == task_id
            }
            receipts = [
                item.to_dict()
                for item in self._lifecycle_receipts[-100:]
                if not task_id
                or (binding := self._bindings.get(self._key(item.source_kind, item.source_id))) is None
                or binding.refs.task_id == task_id
            ]
        return {
            "schema": "zyra.watchdog-source-sessions/v1",
            "bindings": bindings,
            "lifecycle_receipts": receipts,
            "metadata_revision": self._metadata_revision,
            "source_roles": {
                "browser_observer_primary": "browser-use-cropped-python",
                "cross_runtime_supplementary": "oh-my-pi-cropped-typescript",
                "canonical_state_owner": "FaultStateStore",
                "recovery_plan_owner": "M1-S07C",
            },
            "injection_fallback_for_disabled_capture": False,
            "free_text_identity_inference": False,
        }

    def _attach_binding(
        self,
        kind: SourceKind,
        source_id: str,
        refs: CorrelationRefs,
        *,
        generation: int,
        metadata: Mapping[str, Any] | None,
    ) -> tuple[SessionSourceBinding, bool]:
        if not source_id.strip():
            raise ValueError("source_id must not be empty")
        if generation < 0:
            raise ValueError("source generation must be non-negative")
        self._validate_ref(kind, source_id, refs)
        now = self._now(None)
        key = self._key(kind, source_id)
        with self._guard:
            current = self._bindings.get(key)
            if current is not None:
                if generation < current.generation:
                    raise RuntimeError("stale source generation cannot replace current binding")
                if generation == current.generation:
                    if current.refs.run_id != refs.run_id or current.refs.task_id != refs.task_id:
                        raise RuntimeError("source generation cannot move across run/task scope")
                    if current.phase is SourceSessionPhase.DISABLED:
                        raise RuntimeError("disabled source requires a newer generation to reattach")
                    return current, False
                current.phase = SourceSessionPhase.STOPPED
                current.stop_reason = "replaced by newer generation"
                current.revision += 1
            binding = SessionSourceBinding(
                source_id=source_id,
                source_kind=kind,
                refs=refs,
                generation=generation,
                phase=SourceSessionPhase.RUNNING,
                attached_at_ms=now,
                last_heartbeat_ms=now,
                metadata={
                    **dict(metadata or {}),
                    "critical_ref_source": "structured_refs_only",
                    "recovery_plan_owner": "M1-S07C",
                },
            )
            self._bindings[key] = binding
            self._persist_locked()
            return binding, True

    def _require(self, kind: SourceKind, source_id: str, generation: int) -> SessionSourceBinding:
        with self._guard:
            binding = self._bindings.get(self._key(kind, source_id))
            if binding is None:
                raise RuntimeError(f"{kind.value} source is not attached: {source_id}")
            if binding.generation != generation:
                raise RuntimeError("stale source generation")
            if not binding.accepts_events:
                raise RuntimeError(f"source does not accept events in phase {binding.phase.value}")
            return binding

    def _take_provider_permit(self, attempt_id: str) -> ProviderAttemptPermit:
        with self._guard:
            permit = self._provider_permits.pop(attempt_id, None)
        if permit is None:
            raise RuntimeError("provider attempt is unknown or already settled")
        return permit

    def _binding_for_signal(self, refs: CorrelationRefs) -> SessionSourceBinding | None:
        candidates = (
            (SourceKind.TOOL, refs.tool_call_id),
            (SourceKind.WORKER, refs.worker_id),
            (SourceKind.BROWSER, refs.browser_session_id),
            (SourceKind.PROVIDER, refs.provider_id),
            (SourceKind.MCP, refs.mcp_server_id),
            (SourceKind.WORKSPACE, refs.workspace_id),
        )
        for kind, source_id in candidates:
            if source_id and (binding := self._bindings.get(self._key(kind, source_id))) is not None:
                return binding
        return None

    def _receipt(
        self,
        binding: SessionSourceBinding,
        action: str,
        changed: bool,
        details: Mapping[str, Any],
    ) -> SourceLifecycleReceipt:
        receipt = SourceLifecycleReceipt(
            source_id=binding.source_id,
            source_kind=binding.source_kind,
            generation=binding.generation,
            action=action,
            phase=binding.phase,
            changed=changed,
            revision=binding.revision,
            details={
                "run_id": binding.refs.run_id,
                "task_id": binding.refs.task_id,
                "session_id": binding.refs.session_id,
                **dict(details),
            },
        )
        with self._guard:
            self._lifecycle_receipts.append(receipt)
            del self._lifecycle_receipts[:-200]
        return receipt

    def _persist_locked(self) -> None:
        payload = {
            "schema": "zyra.watchdog-source-sessions/v1",
            "bindings": {
                key: value.to_dict()
                for key, value in sorted(self._bindings.items())
            },
            "updated_at": utc_now(),
            "owner": "RuntimeSourceSessionManager",
            "canonical_fault_state_owner": "FaultStateStore",
        }
        self._metadata_revision = self.store.put_metadata(
            self._METADATA_KEY,
            payload,
            expected_revision=self._metadata_revision,
        )

    def _restore(self) -> None:
        saved = self.store.metadata(self._METADATA_KEY)
        if saved is None:
            return
        payload, revision = saved
        restored: dict[str, SessionSourceBinding] = {}
        for key, raw in dict(payload.get("bindings") or {}).items():
            value = dict(raw or {})
            refs_value = dict(value.get("refs") or {})
            try:
                refs = CorrelationRefs(
                    run_id=str(refs_value.get("run_id") or ""),
                    task_id=str(refs_value.get("task_id") or ""),
                    observation_id=str(refs_value.get("observation_id") or runtime_id("restored-source")),
                    session_id=str(refs_value.get("session_id") or ""),
                    node_id=str(refs_value.get("node_id") or ""),
                    attempt_id=str(refs_value.get("attempt_id") or ""),
                    tool_call_id=str(refs_value.get("tool_call_id") or ""),
                    tool_name=str(refs_value.get("tool_name") or ""),
                    worker_id=str(refs_value.get("worker_id") or ""),
                    backend_id=str(refs_value.get("backend_id") or ""),
                    provider_id=str(refs_value.get("provider_id") or ""),
                    workspace_id=str(refs_value.get("workspace_id") or ""),
                    browser_session_id=str(refs_value.get("browser_session_id") or ""),
                    mcp_server_id=str(refs_value.get("mcp_server_id") or ""),
                    subagent_task_id=str(refs_value.get("subagent_task_id") or ""),
                    source_state_revision=int(refs_value.get("source_state_revision") or 0),
                )
                prior_phase = SourceSessionPhase(str(value.get("phase") or SourceSessionPhase.STOPPED.value))
                phase = (
                    SourceSessionPhase.RESTARTING
                    if prior_phase in {SourceSessionPhase.RUNNING, SourceSessionPhase.DEGRADED, SourceSessionPhase.ATTACHING}
                    else prior_phase
                )
                restored[str(key)] = SessionSourceBinding(
                    source_id=str(value.get("source_id") or ""),
                    source_kind=SourceKind(str(value.get("source_kind") or "")),
                    refs=refs,
                    generation=int(value.get("generation") or 0),
                    phase=phase,
                    attached_at_ms=int(value.get("attached_at_ms") or 0),
                    last_heartbeat_ms=int(value.get("last_heartbeat_ms") or 0),
                    revision=int(value.get("revision") or 0) + 1,
                    process_key=str(value.get("process_key") or ""),
                    process_pid=(int(value["process_pid"]) if value.get("process_pid") is not None else None),
                    heartbeat_sequence=int(value.get("heartbeat_sequence") or 0),
                    stop_reason=(
                        "runtime restarted; native source must reattach"
                        if phase is SourceSessionPhase.RESTARTING
                        else str(value.get("stop_reason") or "")
                    ),
                    failure_count=int(value.get("failure_count") or 0),
                    restart_count=int(value.get("restart_count") or 0),
                    last_error_code=str(value.get("last_error_code") or ""),
                    last_observation_ids=[str(item) for item in value.get("last_observation_ids") or ()],
                    metadata=dict(value.get("metadata") or {}),
                )
            except (TypeError, ValueError):
                continue
        self._bindings = restored
        self._metadata_revision = revision
        if restored:
            with self._guard:
                self._persist_locked()

    @staticmethod
    def _validate_ref(kind: SourceKind, source_id: str, refs: CorrelationRefs) -> None:
        selected = {
            SourceKind.TOOL: refs.tool_call_id,
            SourceKind.WORKER: refs.worker_id,
            SourceKind.BROWSER: refs.browser_session_id,
            SourceKind.PROVIDER: refs.provider_id,
            SourceKind.MCP: refs.mcp_server_id,
            SourceKind.WORKSPACE: refs.workspace_id,
        }[kind]
        if source_id != selected:
            raise ValueError(f"{kind.value} source_id does not match structured correlation refs")

    @staticmethod
    def _key(kind: SourceKind, source_id: str) -> str:
        return f"{kind.value}:{source_id}"

    def _now(self, value: int | None) -> int:
        selected = self.monotonic_ms() if value is None else int(value)
        if selected < 0:
            raise ValueError("monotonic time must not be negative")
        return selected


def source_session_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.watchdog-source-session-contract/v1",
        "owner": "RuntimeSourceSessionManager",
        "lifecycle": [item.value for item in SourceSessionPhase],
        "source_kinds": [item.value for item in SourceKind],
        "generation_fenced": True,
        "native_capture_removed_on_disable": True,
        "injection_fallback_for_disabled_capture": False,
        "browser_source": "browser-use cropped same-language lifecycle",
        "cross_runtime_source": "oh-my-pi cropped TypeScript supervision",
        "canonical_state_owner": "FaultStateStore",
        "recovery_plan_owner": "M1-S07C",
    }


__all__ = [
    "ProcessHandle",
    "RuntimeSourceSessionManager",
    "SessionSourceBinding",
    "SourceKind",
    "SourceLifecycleReceipt",
    "SourceSessionPhase",
    "source_session_contract",
]
