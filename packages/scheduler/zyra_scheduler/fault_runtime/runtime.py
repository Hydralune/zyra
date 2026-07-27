from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from zyra_core import TaskState

from .classifier import WatchdogSignalClassifier, default_classifier_contract
from .browser_source import BrowserCrashSourceRuntime, browser_source_contract
from .contracts import (
    FaultInjectionReceipt,
    FaultInjectionRequest,
    FaultSignal,
    ObserverMaturity,
)
from .emission_guard import ObservationEmissionGuard
from .event_writer import FaultSignalEventWriter, event_writer_contract
from .injection import FaultInjectionRuntime
from .observer_registry import CallbackObserver, WatchdogObserverRegistry, descriptor_contract
from .observers import (
    BrowserCrashObserver,
    PermissionReceiptObserver,
    ProcessLifecycleObserver,
    ProviderFailureObserver,
    SchemaValidationObserver,
    SubagentLifecycleObserver,
    ToolDeadlineObserver,
    WorkspaceIntegrityObserver,
    injection_observer_descriptor,
    observer_contract,
    source_inactive_browser_descriptor,
    advisor_candidate_descriptor,
)
from .recovery_bridge import WatchdogRecoveryBridge
from .state_store import FaultStateStore
from .supervision import McpTransportObserver, WorkerHeartbeatObserver
from .runtime_event_adapter import RuntimeEventObservationAdapter
from .diagnostics import FaultRuntimeDiagnostics
from .deadline_runtime import ToolDeadlineRuntime, deadline_runtime_contract
from .lifecycle_supervisor import (
    ObserverRuntimeSupervisor,
    lifecycle_supervision_contract,
)
from .polling import WatchdogPollingCoordinator
from .query import FaultRuntimeQueryService
from .pressure import FaultPressureMonitor
from .provider_supervision import ProviderAttemptSupervisor, provider_supervision_contract
from .integration import WatchdogFaultIntegrationRuntime, integration_contract


class WatchdogRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeWatchdog:
    """Composes active observation sources around one durable signal owner."""

    def __init__(
        self,
        store: FaultStateStore,
        writer: FaultSignalEventWriter,
        *,
        workspace_roots: Mapping[str, str | Path] | None = None,
        task_state_resolver: Callable[[str], TaskState | None] | None = None,
        auto_handoff: bool = True,
    ) -> None:
        self.store = store
        self.writer = writer
        self.task_state_resolver = task_state_resolver
        self.auto_handoff = auto_handoff
        self.recovery = WatchdogRecoveryBridge(store)
        self.classifier = WatchdogSignalClassifier()
        self.emission_guard = ObservationEmissionGuard()
        self._guard = threading.RLock()
        self._last_signal_ids: list[str] = []
        self._last_projection_errors: dict[str, tuple[str, ...]] = {}
        self._signal_listeners: list[Callable[[FaultSignal], None]] = []
        self._signal_listener_errors: list[dict[str, str]] = []
        self.registry = WatchdogObserverRegistry(
            store,
            classifier=self.classifier,
            emission_guard=self.emission_guard,
            signal_sink=self._on_signal,
        )
        self.tool_deadlines = ToolDeadlineObserver()
        self.tool_execution = ToolDeadlineRuntime(self.tool_deadlines)
        self.processes = ProcessLifecycleObserver()
        self.permissions = PermissionReceiptObserver()
        self.providers = ProviderFailureObserver()
        self.schemas = SchemaValidationObserver()
        self.subagents = SubagentLifecycleObserver()
        self.workspaces = WorkspaceIntegrityObserver(workspace_roots)
        self.browser = BrowserCrashObserver()
        self.browser_source = BrowserCrashSourceRuntime(self.browser)
        self.worker_heartbeats = WorkerHeartbeatObserver()
        self.mcp_transports = McpTransportObserver()
        self.provider_attempts = ProviderAttemptSupervisor(self.providers)
        self.source_inactive_browser = CallbackObserver(source_inactive_browser_descriptor())
        self.advisor_candidate = CallbackObserver(advisor_candidate_descriptor())
        self._sources = (
            self.tool_deadlines,
            self.processes,
            self.permissions,
            self.providers,
            self.schemas,
            self.subagents,
            self.workspaces,
            self.browser,
            self.worker_heartbeats,
            self.mcp_transports,
            self.source_inactive_browser,
            self.advisor_candidate,
        )
        for source in self._sources:
            self.registry.register(source)
        self.runtime_events = RuntimeEventObservationAdapter(self.registry)
        self.polling = WatchdogPollingCoordinator()
        self.polling.register("tool-deadlines", lambda: dict(self.tick()), interval_ms=250)
        self.lifecycle = ObserverRuntimeSupervisor(self.store, self.registry)
        self.queries = FaultRuntimeQueryService(self.store)
        self.diagnostics = FaultRuntimeDiagnostics(self.store)
        self.pressure = FaultPressureMonitor()

    def start(self) -> tuple[Mapping[str, Any], ...]:
        return self.lifecycle.reconcile_startup()

    def ingest_runtime_event(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        if os.environ.get("ZYRA_WATCHDOG_RUNTIME_DISABLED", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise WatchdogRuntimeError(
                "watchdog_runtime_disabled",
                "runtime watchdog owner is disabled by the Zyra owner-disconnect gate",
            )
        self.runtime_events.ingest(event)
        source_signal_id = str(event.get("signal_id") or "")
        return {
            "accepted": True,
            "source_signal_id": source_signal_id,
            "canonical_signal_id": self.runtime_events.canonical_signal_id(source_signal_id),
            "phase": str(event.get("phase") or ""),
        }

    def stop(
        self,
        *,
        persist_observer_state: bool = True,
    ) -> tuple[Mapping[str, Any], ...]:
        self.polling.stop()
        self.browser_source.detach_all(intentional=True)
        stopped = []
        states = self.store.observers() if persist_observer_state else ()
        for state in states:
            if state.lifecycle.accepts_observations and state.descriptor.maturity is not ObserverMaturity.INJECTION_ONLY:
                stopped.append(self.registry.stop(state.descriptor.observer_id, reason="runtime watchdog shutdown"))
                self.lifecycle.mark_stopped(state.descriptor.observer_id)
        return tuple(item.to_dict() for item in stopped)

    def tick(self, *, at_ms: int | None = None) -> Mapping[str, Any]:
        tool_observations = self.tool_execution.poll(at_ms=at_ms)
        process_observations = self.processes.poll()
        heartbeat_observations = self.worker_heartbeats.sweep(at_ms=at_ms)
        browser_source_signals = self.browser_source.poll_all(at_ms=at_ms)
        observer_restarts = self.lifecycle.restart_due(at_ms=at_ms)
        stale_observers = self.lifecycle.sweep_stale(at_ms=at_ms)
        return {
            "schema": "zyra.runtime-watchdog-tick/v1",
            "tool_observation_ids": [item.observation_id for item in tool_observations],
            "process_observation_ids": [item.observation_id for item in process_observations],
            "heartbeat_observation_ids": [item.observation_id for item in heartbeat_observations],
            "browser_source_signal_ids": {
                key: list(value)
                for key, value in browser_source_signals.items()
            },
            "mcp_reconnect_due": list(self.mcp_transports.due_reconnects()),
            "observer_restarts": list(observer_restarts),
            "stale_observers": list(stale_observers),
            "signal_ids": list(self._last_signal_ids[-100:]),
        }

    def disable_observer(self, observer_id: str, *, reason: str) -> Mapping[str, Any]:
        if observer_id == injection_observer_descriptor().observer_id:
            raise ValueError("injection-only observer is controlled by FaultInjectionRuntime")
        return self.registry.disable(observer_id, reason=reason).to_dict()

    def enable_observer(self, observer_id: str) -> Mapping[str, Any]:
        state = self.store.require_observer(observer_id)
        if state.descriptor.maturity is ObserverMaturity.SOURCE_INACTIVE:
            raise ValueError("source_inactive observer cannot be enabled")
        return self.registry.start(observer_id).to_dict()

    def add_signal_listener(self, listener: Callable[[FaultSignal], None]) -> None:
        if not callable(listener):
            raise TypeError("watchdog signal listener must be callable")
        with self._guard:
            if listener not in self._signal_listeners:
                self._signal_listeners.append(listener)

    def remove_signal_listener(self, listener: Callable[[FaultSignal], None]) -> None:
        with self._guard:
            if listener in self._signal_listeners:
                self._signal_listeners.remove(listener)

    def snapshot(self, *, task_id: str = "") -> dict[str, Any]:
        with self._guard:
            return {
                "schema": "zyra.runtime-watchdog/v1",
                "registry": self.registry.snapshot(),
                "fault_state": self.store.snapshot(task_id=task_id),
                "task_projection": self.writer.task_projection(task_id) if task_id else {},
                "last_signal_ids": list(self._last_signal_ids[-100:]),
                "projection_errors": {
                    key: list(value)
                    for key, value in self._last_projection_errors.items()
                },
                "signal_listener_count": len(self._signal_listeners),
                "signal_listener_errors": list(self._signal_listener_errors[-100:]),
                "typescript_runtime_ingress": self.runtime_events.snapshot(),
                "browser_crash_source": self.browser_source.snapshot(),
                "provider_attempts": self.provider_attempts.snapshot(),
                "tool_deadline_runtime": self.tool_execution.snapshot(),
                "observer_runtime_supervision": self.lifecycle.snapshot(),
                "polling": self.polling.snapshot(),
                "task_summary": self.queries.task_summary(task_id) if task_id else {},
                "diagnostics": self.diagnostics.inspect(task_id=task_id).to_dict(),
                "pressure": self.pressure.snapshot(task_id=task_id),
                "state_custody": {
                    "observations_signals_injections_handoffs": "python.FaultStateStore",
                    "event_history": "canonical Zyra task store",
                    "memory": "python.MemoryFabric",
                    "scheduler_health": "python.BackendRegistryStore",
                    "recovery_planning": "M1-S07C",
                    "second_scheduler_health_store": False,
                    "second_memory_store": False,
                },
            }

    def health(self, *, task_id: str = "") -> Mapping[str, Any]:
        """Return an operational health verdict without mutating source state."""
        report = self.diagnostics.inspect(task_id=task_id)
        states = self.store.observers()
        active_real = [
            item
            for item in states
            if item.descriptor.maturity is ObserverMaturity.ACTIVE_REAL
            and item.lifecycle.accepts_observations
        ]
        failed = [item for item in states if item.lifecycle.value == "failed"]
        disabled = [item for item in states if item.lifecycle.value == "disabled"]
        source_inactive_running = [
            item
            for item in states
            if item.descriptor.maturity is ObserverMaturity.SOURCE_INACTIVE
            and item.lifecycle.accepts_observations
        ]
        if source_inactive_running or not active_real:
            status = "unavailable"
        elif failed or not report.ok:
            status = "degraded"
        else:
            status = "healthy"
        return {
            "schema": "zyra.runtime-watchdog-health/v1",
            "status": status,
            "task_id": task_id,
            "active_real_observer_ids": sorted(item.descriptor.observer_id for item in active_real),
            "failed_observer_ids": sorted(item.descriptor.observer_id for item in failed),
            "disabled_observer_ids": sorted(item.descriptor.observer_id for item in disabled),
            "source_inactive_running_ids": sorted(
                item.descriptor.observer_id for item in source_inactive_running
            ),
            "diagnostic_ok": report.ok,
            "diagnostic_counts": dict(report.counts),
            "pressure_level": (
                self.pressure.level(task_id).value if task_id else "not_task_scoped"
            ),
            "polling_thread_running": bool(self.polling.snapshot().get("thread_running")),
            "canonical_event_sink_required": True,
            "state_owner": "python.FaultStateStore",
            "injection_is_real_health_source": False,
            "requirement_changed_is_fault": False,
        }

    def _on_signal(self, signal: FaultSignal) -> None:
        pressure = self.pressure.record(signal)
        task_state = self.task_state_resolver(signal.refs.task_id) if self.task_state_resolver else None
        receipt = self.writer.write(signal, task_state=task_state)
        with self._guard:
            self._last_signal_ids.append(signal.signal_id)
            if receipt.errors:
                self._last_projection_errors[signal.signal_id] = receipt.errors
        if (
            self.auto_handoff
            and receipt.canonical_event_written
            and signal.origin.value == "observer"
            and (
                pressure.force_recovery_handoff
                or signal.terminal
                or signal.disposition.value in {"block", "recover"}
            )
        ):
            self.recovery.handoff(
                signal,
                metadata={
                    "automatic_observer_handoff": True,
                    "pressure_level": pressure.level.value,
                    "pressure_forced": pressure.force_recovery_handoff,
                },
            )
        with self._guard:
            listeners = tuple(self._signal_listeners)
        for listener in listeners:
            try:
                listener(signal)
            except Exception as error:
                with self._guard:
                    self._signal_listener_errors.append(
                        {
                            "signal_id": signal.signal_id,
                            "listener": getattr(listener, "__qualname__", type(listener).__name__),
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                    del self._signal_listener_errors[:-100]


class FaultRuntimeApplication:
    """Single application boundary shared by API, commands, and workers."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        event_sink: Callable[[Any], None],
        memory: Any | None = None,
        scheduler_health: Any | None = None,
        task_state_resolver: Callable[[str], TaskState | None] | None = None,
        workspace_roots: Mapping[str, str | Path] | None = None,
    ) -> None:
        self.store = FaultStateStore(store_path)
        self.writer = FaultSignalEventWriter(
            self.store,
            event_sink=event_sink,
            memory=memory,
            scheduler_health=scheduler_health,
            task_state_resolver=task_state_resolver,
        )
        self.watchdog = RuntimeWatchdog(
            self.store,
            self.writer,
            workspace_roots=workspace_roots,
            task_state_resolver=task_state_resolver,
        )
        self.watchdog.start()
        self.recovery = self.watchdog.recovery
        self.injections = FaultInjectionRuntime(
            self.store,
            self.watchdog.registry,
            self.writer,
            self.recovery,
        )
        self.reconciliation_report = self.injections.reconcile_incomplete(task_state_resolver)
        self.integration = WatchdogFaultIntegrationRuntime(
            self,
            event_sink=event_sink,
            task_state_resolver=task_state_resolver,
        )

    def inject(self, request: FaultInjectionRequest, *, task_state: TaskState) -> FaultInjectionReceipt:
        return self.injections.inject(request, task_state=task_state)

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        return {
            **self.watchdog.snapshot(task_id=task_id),
            "injection_reconciliation": list(self.reconciliation_report),
            "integration": self.integration.snapshot(task_id=task_id),
        }

    def close(self) -> None:
        self.integration.close()
        durable_state_available = self.store.path.is_file()
        self.watchdog.stop(
            persist_observer_state=durable_state_available,
        )
        if durable_state_available:
            injection_state = self.store.observer("same-run-fault-injector")
            if (
                injection_state is not None
                and injection_state.lifecycle.accepts_observations
            ):
                self.watchdog.registry.stop(
                    "same-run-fault-injector",
                    reason="fault injection runtime shutdown",
                )
        self.store.close()

    @staticmethod
    def contract() -> dict[str, Any]:
        descriptors = observer_contract()["observers"]
        return {
            "schema": "zyra.fault-runtime-application/v1",
            "classifier": default_classifier_contract(),
            "observers": descriptors,
            "event_writer": event_writer_contract(),
            "browser_source": browser_source_contract(),
            "provider_supervision": provider_supervision_contract(),
            "deadline_runtime": deadline_runtime_contract(),
            "observer_runtime_supervision": lifecycle_supervision_contract(),
            "injection": FaultInjectionRuntime.contract(),
            "recovery_bridge": WatchdogRecoveryBridge.contract(),
            "integration": integration_contract(),
            "roles": {
                "browser_observer_primary": "browser-use lifecycle + Zyra 04D detector",
                "classifier_primary": "Zyra structured deterministic rules",
                "process_provider_supplement": "oh-my-pi",
                "taxonomy_conformance": "existing Zyra FailureKind and EventType",
            },
            "excluded": {
                "requirement_changed_as_fault": True,
                "free_text_critical_ref_inference": True,
                "browser_use_inactive_crash_watchdog_as_real_source": True,
                "fault_injection_masking_disabled_observer": True,
            },
        }


__all__ = ["FaultRuntimeApplication", "RuntimeWatchdog", "WatchdogRuntimeError"]
