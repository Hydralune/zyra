from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zyra_core import TaskState

from .contracts import (
    ContinuationMode,
    CorrelationRefs,
    FaultInjectionReceipt,
    FaultInjectionRequest,
    FaultKind,
    FaultSignal,
    InjectionKind,
    InjectionPhase,
    ObservationCategory,
    ObservationProvenance,
    ObserverLifecycle,
    ObserverMaturity,
    ProjectionReceipt,
    RecoveryHandoff,
    SignalOrigin,
    StructuredObservation,
    runtime_id,
)
from .event_writer import FaultSignalEventWriter
from .observer_registry import CallbackObserver, WatchdogObserverRegistry
from .observers import injection_observer_descriptor
from .recovery_bridge import WatchdogRecoveryBridge
from .state_store import FaultStateStore


@dataclass(frozen=True, slots=True)
class InjectionSpec:
    category: ObservationCategory
    code: str
    status: str
    error_type: str
    required_ref: str
    retryable: bool
    terminal: bool
    summary: str


INJECTION_SPECS: dict[InjectionKind, InjectionSpec] = {
    InjectionKind.WORKER_LOST: InjectionSpec(
        ObservationCategory.WORKER,
        "worker_lost",
        "lost",
        "InjectedWorkerLoss",
        "worker_id",
        True,
        True,
        "The requested worker-loss boundary was injected into the current run.",
    ),
    InjectionKind.TOOL_TIMEOUT: InjectionSpec(
        ObservationCategory.TOOL,
        "tool_timeout",
        "timed_out",
        "InjectedToolTimeout",
        "tool_call_id",
        True,
        True,
        "The requested tool deadline was injected into the current run.",
    ),
    InjectionKind.BROWSER_CRASH: InjectionSpec(
        ObservationCategory.BROWSER,
        "process_exited",
        "crashed",
        "InjectedBrowserCrash",
        "browser_session_id",
        True,
        True,
        "The requested browser crash was injected into the current run.",
    ),
    InjectionKind.MODEL_FAILURE: InjectionSpec(
        ObservationCategory.PROVIDER,
        "provider_error",
        "failed",
        "InjectedProviderFailure",
        "provider_id",
        True,
        True,
        "The requested provider failure was injected into the current run.",
    ),
    InjectionKind.WORKSPACE_CORRUPT: InjectionSpec(
        ObservationCategory.WORKSPACE,
        "workspace_corrupt",
        "corrupt",
        "InjectedWorkspaceCorruption",
        "workspace_id",
        False,
        True,
        "The requested workspace integrity failure was injected into the current run.",
    ),
}


class SameRunFaultInjector:
    """Builds injected observations without touching real external resources."""

    def __init__(self, registry: WatchdogObserverRegistry) -> None:
        self.registry = registry
        self.descriptor = injection_observer_descriptor()
        self.callback = CallbackObserver(self.descriptor)
        self._lock = threading.RLock()
        self._revision = 0
        self.registry.register(self.callback)
        # The injection observer is INJECTION_ONLY, so the lifecycle supervisor
        # deliberately leaves its ownership here.  A previous process can leave
        # the durable lifecycle at RUNNING while its process-local callback is
        # gone; a plain start() then returns early without attaching, and every
        # trigger fails with "injection observer is not running".  Recover the
        # stale RUNNING state so this process's callback is attached and running.
        current = self.registry.store.require_observer(self.descriptor.observer_id)
        if current.lifecycle is ObserverLifecycle.RUNNING and not (
            bool(self.callback.snapshot().get("attached"))
            and bool(self.callback.snapshot().get("running"))
        ):
            self.registry.recover_stale_running(
                self.descriptor.observer_id,
                process_epoch=runtime_id("injection-epoch"),
            )
        else:
            self.registry.start(self.descriptor.observer_id)

    def build(self, request: FaultInjectionRequest) -> StructuredObservation:
        spec = INJECTION_SPECS[request.kind]
        selected = str(getattr(request.target, spec.required_ref) or "")
        if not selected:
            raise ValueError(f"{request.kind.value} injection requires explicit {spec.required_ref}")
        with self._lock:
            self._revision += 1
            revision = self._revision
        refs = request.target.with_observation(
            runtime_id("injected-observation"),
            revision=max(request.target.source_state_revision, revision),
        )
        details = {
            "injection_id": request.injection_id,
            "injection_kind": request.kind.value,
            "requested_by": request.requested_by,
            "idempotency_key": request.idempotency_key,
            "continuation": request.continuation.value,
            "same_run": True,
            "simulates_boundary_only": True,
            "destructive_external_mutation": False,
            "required_ref": spec.required_ref,
            **dict(request.parameters),
        }
        return StructuredObservation(
            category=spec.category,
            code=spec.code,
            refs=refs,
            provenance=ObservationProvenance(
                observer_id=self.descriptor.observer_id,
                source_repo=self.descriptor.source_repo,
                source_revision=self.descriptor.source_revision,
                observation_point=self.descriptor.observation_point,
                maturity=ObserverMaturity.INJECTION_ONLY,
                injection_id=request.injection_id,
            ),
            summary=spec.summary,
            status=spec.status,
            error_type=spec.error_type,
            retryable_hint=spec.retryable,
            terminal_hint=spec.terminal,
            elapsed_ms=(
                int(request.parameters.get("elapsed_ms", request.parameters.get("deadline_ms", 1_000)))
                if request.kind is InjectionKind.TOOL_TIMEOUT
                else None
            ),
            deadline_ms=(
                int(request.parameters.get("deadline_ms", 1_000))
                if request.kind is InjectionKind.TOOL_TIMEOUT
                else None
            ),
            details=details,
        )

    def trigger(self, observation: StructuredObservation) -> FaultSignal:
        before = set(signal.signal_id for signal in self.registry.store.signals(injection_id=observation.provenance.injection_id))
        if not self.callback.observe(observation):
            raise RuntimeError("injection observer is not running")
        candidates = self.registry.store.signals(injection_id=observation.provenance.injection_id)
        signal = next((item for item in candidates if item.signal_id not in before), None)
        if signal is None:
            signal = next((item for item in candidates if item.refs.observation_id == observation.observation_id), None)
        if signal is None:
            raise RuntimeError("injected observation did not classify into a fault signal")
        if signal.origin is not SignalOrigin.INJECTION:
            raise RuntimeError("same-run injector produced a non-injection signal")
        return signal


class FaultInjectionRuntime:
    """Durable same-run injection state machine and API/command implementation."""

    def __init__(
        self,
        store: FaultStateStore,
        registry: WatchdogObserverRegistry,
        writer: FaultSignalEventWriter,
        recovery: WatchdogRecoveryBridge,
    ) -> None:
        self.store = store
        self.registry = registry
        self.writer = writer
        self.recovery = recovery
        self.injector = SameRunFaultInjector(registry)
        self._task_locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()

    def inject(
        self,
        request: FaultInjectionRequest,
        *,
        task_state: TaskState,
    ) -> FaultInjectionReceipt:
        self._validate_same_run(request, task_state)
        with self._task_lock(request.task_id):
            stored_request, latest, duplicate = self.store.begin_injection(request)
            if duplicate:
                return self._duplicate_receipt(stored_request)
            try:
                latest = self.store.transition_injection(
                    request.injection_id,
                    InjectionPhase.ARMED,
                    expected_revision=latest.revision,
                    reason="validated explicit target refs and fenced same-run injection",
                    details={"required_ref": INJECTION_SPECS[request.kind].required_ref},
                )
                observation = self.injector.build(request)
                latest = self.store.transition_injection(
                    request.injection_id,
                    InjectionPhase.TRIGGERED,
                    expected_revision=latest.revision,
                    reason="injection boundary constructed without mutating an external resource",
                    details={"observation_id": observation.observation_id},
                )
                signal = self.injector.trigger(observation)
                latest = self.store.transition_injection(
                    request.injection_id,
                    InjectionPhase.OBSERVED,
                    expected_revision=latest.revision,
                    reason="injection-only observer classified a structured same-run observation",
                    signal_id=signal.signal_id,
                )
                projection = self.writer.write(signal, task_state=task_state)
                if not projection.canonical_event_written:
                    failed = self.store.transition_injection(
                        request.injection_id,
                        InjectionPhase.FAILED,
                        expected_revision=latest.revision,
                        reason="canonical fault event projection failed closed",
                        signal_id=signal.signal_id,
                        event_id=projection.event_id,
                        details={"errors": list(projection.errors)},
                    )
                    return self._receipt(request, failed.phase, signal, projection, None)
                self._apply_same_run_projection(task_state, request, signal, projection)
                latest = self.store.transition_injection(
                    request.injection_id,
                    InjectionPhase.PROJECTED,
                    expected_revision=latest.revision,
                    reason="canonical event and same-run task projection committed",
                    signal_id=signal.signal_id,
                    event_id=projection.event_id,
                    details={"projection_receipt_id": projection.receipt_id},
                )
                return self._finish(request, latest.revision, signal, projection)
            except Exception as error:
                current = self.store.injection(request.injection_id)
                if current is not None and not current[1][-1].phase.terminal:
                    latest_state = current[1][-1]
                    failed = self.store.transition_injection(
                        request.injection_id,
                        InjectionPhase.FAILED,
                        expected_revision=latest_state.revision,
                        reason="same-run fault injection failed closed",
                        details={"error": f"{type(error).__name__}: {error}"},
                    )
                    return self._receipt(request, failed.phase, None, None, None)
                raise

    def reconcile_incomplete(
        self,
        task_state_resolver: Any,
    ) -> tuple[Mapping[str, Any], ...]:
        """Close crash-interrupted injections without replaying external effects.

        A pre-observation crash is failed closed because there is no durable
        signal proving that the boundary occurred. Once a signal exists, its
        stable projection receipt is used to resume only the remaining journal
        transitions and 07C handoff. This runs under the same task lock as a
        normal injection and never rebuilds an injected observation.
        """
        reports: list[Mapping[str, Any]] = []
        for request, transitions in self.store.injections(limit=5_000):
            latest = transitions[-1]
            if latest.phase.terminal:
                continue
            with self._task_lock(request.task_id):
                reports.append(self._reconcile_one(request, latest, task_state_resolver))
        return tuple(reports)

    def _reconcile_one(
        self,
        request: FaultInjectionRequest,
        latest: Any,
        task_state_resolver: Any,
    ) -> Mapping[str, Any]:
        current = self.store.injection(request.injection_id)
        if current is None:
            return {
                "injection_id": request.injection_id,
                "status": "missing",
                "phase": latest.phase.value,
            }
        request, transitions = current
        latest = transitions[-1]
        if latest.phase.terminal:
            return {
                "injection_id": request.injection_id,
                "status": "already_terminal",
                "phase": latest.phase.value,
            }
        task_state = task_state_resolver(request.task_id) if callable(task_state_resolver) else None
        if task_state is None:
            return {
                "injection_id": request.injection_id,
                "status": "deferred",
                "phase": latest.phase.value,
                "reason": "canonical task state is unavailable",
            }
        if task_state.run_id != request.run_id or task_state.task_id != request.task_id:
            failed = self.store.transition_injection(
                request.injection_id,
                InjectionPhase.FAILED,
                expected_revision=latest.revision,
                reason="restart reconciliation rejected mismatched run/task state",
                details={
                    "resolved_run_id": task_state.run_id,
                    "resolved_task_id": task_state.task_id,
                },
            )
            return self._reconciliation_report(request, failed, "failed_closed")

        signals = self.store.signals(injection_id=request.injection_id, limit=20)
        signal = next(
            (item for item in signals if not latest.signal_id or item.signal_id == latest.signal_id),
            signals[0] if signals else None,
        )
        if latest.phase in {InjectionPhase.REQUESTED, InjectionPhase.ARMED}:
            failed = self.store.transition_injection(
                request.injection_id,
                InjectionPhase.FAILED,
                expected_revision=latest.revision,
                reason="restart occurred before a durable injected observation",
                details={
                    "replay_performed": False,
                    "external_resource_mutated": False,
                    "safe_to_retry_with_new_idempotency_key": True,
                },
            )
            return self._reconciliation_report(request, failed, "failed_closed")
        if latest.phase is InjectionPhase.TRIGGERED:
            if signal is None:
                failed = self.store.transition_injection(
                    request.injection_id,
                    InjectionPhase.FAILED,
                    expected_revision=latest.revision,
                    reason="triggered injection lacked a durable classified signal after restart",
                    details={"replay_performed": False, "ambiguous_boundary": True},
                )
                return self._reconciliation_report(request, failed, "failed_closed")
            latest = self.store.transition_injection(
                request.injection_id,
                InjectionPhase.OBSERVED,
                expected_revision=latest.revision,
                reason="restart reconciliation found the durable classified signal",
                signal_id=signal.signal_id,
                details={"replayed_observation": False},
            )
        if latest.phase is InjectionPhase.OBSERVED:
            if signal is None:
                failed = self.store.transition_injection(
                    request.injection_id,
                    InjectionPhase.FAILED,
                    expected_revision=latest.revision,
                    reason="observed injection signal disappeared before projection",
                )
                return self._reconciliation_report(request, failed, "failed_closed")
            projection = self.store.projection_receipt(signal.signal_id)
            if projection is None:
                projection = self.writer.write(signal, task_state=task_state)
            if not projection.canonical_event_written:
                failed = self.store.transition_injection(
                    request.injection_id,
                    InjectionPhase.FAILED,
                    expected_revision=latest.revision,
                    reason="restart reconciliation could not prove canonical event projection",
                    signal_id=signal.signal_id,
                    event_id=projection.event_id,
                    details={"errors": list(projection.errors)},
                )
                return self._reconciliation_report(request, failed, "failed_closed")
            self._apply_same_run_projection(task_state, request, signal, projection)
            latest = self.store.transition_injection(
                request.injection_id,
                InjectionPhase.PROJECTED,
                expected_revision=latest.revision,
                reason="restart reconciliation committed the stable projection receipt",
                signal_id=signal.signal_id,
                event_id=projection.event_id,
                details={
                    "projection_receipt_id": projection.receipt_id,
                    "event_redelivered": False,
                },
            )
        if latest.phase is not InjectionPhase.PROJECTED or signal is None:
            return {
                "injection_id": request.injection_id,
                "status": "deferred",
                "phase": latest.phase.value,
                "reason": "reconciliation reached an unsupported durable phase",
            }
        projection = self.store.projection_receipt(signal.signal_id)
        if projection is None or not projection.canonical_event_written:
            failed = self.store.transition_injection(
                request.injection_id,
                InjectionPhase.FAILED,
                expected_revision=latest.revision,
                reason="projected injection lacked a successful durable projection receipt",
                signal_id=signal.signal_id,
            )
            return self._reconciliation_report(request, failed, "failed_closed")
        self._apply_same_run_projection(task_state, request, signal, projection)
        receipt = self._finish(request, latest.revision, signal, projection)
        terminal = receipt.transitions[-1]
        return self._reconciliation_report(
            request,
            terminal,
            "resumed",
            signal_id=signal.signal_id,
            event_id=projection.event_id,
        )

    @staticmethod
    def _reconciliation_report(
        request: FaultInjectionRequest,
        transition: Any,
        status: str,
        *,
        signal_id: str = "",
        event_id: str = "",
    ) -> Mapping[str, Any]:
        return {
            "schema": "zyra.fault-injection-reconciliation/v1",
            "injection_id": request.injection_id,
            "run_id": request.run_id,
            "task_id": request.task_id,
            "status": status,
            "phase": transition.phase.value,
            "revision": transition.revision,
            "signal_id": signal_id or transition.signal_id,
            "event_id": event_id or transition.event_id,
            "external_resource_replayed": False,
        }

    def _finish(
        self,
        request: FaultInjectionRequest,
        expected_revision: int,
        signal: FaultSignal,
        projection: ProjectionReceipt,
    ) -> FaultInjectionReceipt:
        use_handoff = request.continuation is ContinuationMode.RECOVERY_HANDOFF
        if request.continuation is ContinuationMode.AUTO:
            use_handoff = signal.terminal or not signal.retryable
        handoff: RecoveryHandoff | None = None
        if use_handoff:
            handoff = self.recovery.handoff(
                signal,
                injection_id=request.injection_id,
                metadata={"continuation_mode": request.continuation.value},
            )
            terminal = self.store.transition_injection(
                request.injection_id,
                InjectionPhase.HANDED_OFF,
                expected_revision=expected_revision,
                reason="fault boundary handed to the 07C recovery planner contract",
                signal_id=signal.signal_id,
                event_id=projection.event_id,
                handoff_id=handoff.handoff_id,
            )
        else:
            terminal = self.store.transition_injection(
                request.injection_id,
                InjectionPhase.CONTINUED,
                expected_revision=expected_revision,
                reason="current run retained control after observable fault projection",
                signal_id=signal.signal_id,
                event_id=projection.event_id,
            )
        return self._receipt(request, terminal.phase, signal, projection, handoff)

    def _apply_same_run_projection(
        self,
        state: TaskState,
        request: FaultInjectionRequest,
        signal: FaultSignal,
        projection: ProjectionReceipt,
    ) -> None:
        current = copy.deepcopy(dict(state.metadata.get("fault_injection") or {}))
        current[request.injection_id] = {
            "kind": request.kind.value,
            "signal_id": signal.signal_id,
            "event_id": projection.event_id,
            "target": request.target.to_dict(),
            "status": "projected",
            "same_run": True,
            "external_resource_mutated": False,
        }
        state.metadata["fault_injection"] = current

    def _duplicate_receipt(self, request: FaultInjectionRequest) -> FaultInjectionReceipt:
        stored = self.store.injection(request.injection_id)
        if stored is None:
            raise RuntimeError("duplicate injection disappeared from durable state")
        _, transitions = stored
        phase = transitions[-1].phase
        signals = self.store.signals(injection_id=request.injection_id, limit=10)
        signal = signals[0] if signals else None
        projection = None if signal is None else self.store.projection_receipt(signal.signal_id)
        handoff = None
        if signal is not None:
            handoff = next(
                (item for item in self.store.handoffs(task_id=request.task_id, limit=5_000) if item.signal_id == signal.signal_id),
                None,
            )
        if not phase.terminal:
            raise RuntimeError("concurrent injection is not terminal yet")
        return FaultInjectionReceipt(
            request=request,
            phase=phase,
            signal=signal,
            projection=projection,
            handoff=handoff,
            transitions=transitions,
            duplicate=True,
            same_run=True,
        )

    def _receipt(
        self,
        request: FaultInjectionRequest,
        phase: InjectionPhase,
        signal: FaultSignal | None,
        projection: ProjectionReceipt | None,
        handoff: RecoveryHandoff | None,
    ) -> FaultInjectionReceipt:
        stored = self.store.injection(request.injection_id)
        transitions = () if stored is None else stored[1]
        return FaultInjectionReceipt(
            request=request,
            phase=phase,
            signal=signal,
            projection=projection,
            handoff=handoff,
            transitions=transitions,
            same_run=True,
        )

    @staticmethod
    def _validate_same_run(request: FaultInjectionRequest, state: TaskState) -> None:
        if request.run_id != state.run_id or request.task_id != state.task_id:
            raise ValueError("fault injection may only target the supplied current run/task state")
        spec = INJECTION_SPECS[request.kind]
        if not str(getattr(request.target, spec.required_ref) or ""):
            raise ValueError(f"missing explicit injection target ref: {spec.required_ref}")
        if request.target.run_id != state.run_id or request.target.task_id != state.task_id:
            raise ValueError("injection target does not belong to the active run")

    def _task_lock(self, task_id: str) -> threading.RLock:
        with self._guard:
            return self._task_locks.setdefault(task_id, threading.RLock())

    @staticmethod
    def request_from_mapping(
        value: Mapping[str, Any],
        *,
        state: TaskState,
        requested_by: str,
        default_idempotency_key: str,
    ) -> FaultInjectionRequest:
        target_value = dict(value.get("target") or {})
        target = CorrelationRefs(
            run_id=state.run_id,
            task_id=state.task_id,
            observation_id=str(target_value.get("observation_id") or runtime_id("injection-target")),
            session_id=str(target_value.get("session_id", "")),
            node_id=str(target_value.get("node_id", "")),
            attempt_id=str(target_value.get("attempt_id", "")),
            tool_call_id=str(target_value.get("tool_call_id", "")),
            tool_name=str(target_value.get("tool_name", "")),
            worker_id=str(target_value.get("worker_id", "")),
            backend_id=str(target_value.get("backend_id", "")),
            provider_id=str(target_value.get("provider_id", "")),
            workspace_id=str(target_value.get("workspace_id", "")),
            browser_session_id=str(target_value.get("browser_session_id", "")),
            mcp_server_id=str(target_value.get("mcp_server_id", "")),
            subagent_task_id=str(target_value.get("subagent_task_id", "")),
            source_state_revision=int(target_value.get("source_state_revision", 0) or 0),
        )
        return FaultInjectionRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            kind=InjectionKind(str(value.get("kind", ""))),
            target=target,
            requested_by=requested_by,
            idempotency_key=str(value.get("idempotency_key") or default_idempotency_key),
            continuation=ContinuationMode(str(value.get("continuation", ContinuationMode.AUTO.value))),
            parameters=dict(value.get("parameters") or {}),
        )

    @staticmethod
    def contract() -> dict[str, Any]:
        return {
            "schema": "zyra.fault-injection-runtime/v1",
            "kinds": {
                kind.value: {
                    "category": spec.category.value,
                    "code": spec.code,
                    "required_ref": spec.required_ref,
                }
                for kind, spec in INJECTION_SPECS.items()
            },
            "phases": [item.value for item in InjectionPhase],
            "same_run": True,
            "external_resource_mutation": False,
            "observer_maturity": ObserverMaturity.INJECTION_ONLY.value,
            "can_mask_disabled_real_observer": False,
            "restart_reconciliation": {
                "pre_observation": "failed_closed_without_replay",
                "post_observation": "resume_from_durable_signal_and_projection",
                "external_resource_replayed": False,
            },
        }


__all__ = ["FaultInjectionRuntime", "INJECTION_SPECS", "InjectionSpec", "SameRunFaultInjector"]
