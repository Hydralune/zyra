from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .classifier import ClassificationResult, WatchdogSignalClassifier
from .contracts import (
    FaultKind,
    FaultSignal,
    ObserverDescriptor,
    ObserverLifecycle,
    ObserverMaturity,
    ObserverState,
    StructuredObservation,
)
from .emission_guard import EmissionDecision, ObservationEmissionGuard
from .errors import FaultRuntimeError, FaultRuntimeErrorCode
from .state_store import FaultStateStore


class RuntimeObserver(Protocol):
    descriptor: ObserverDescriptor

    def attach(self, emit: Callable[[StructuredObservation], None]) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def snapshot(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ObserverSubmission:
    observation: StructuredObservation
    emission: EmissionDecision
    classification: ClassificationResult
    signal: FaultSignal | None
    observation_duplicate: bool
    signal_duplicate: bool
    observer_state: ObserverState

    @property
    def accepted(self) -> bool:
        return self.emission.accepted and not self.observation_duplicate

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "observation": self.observation.to_dict(),
            "emission": self.emission.to_dict(),
            "classification": self.classification.to_dict(),
            "signal": None if self.signal is None else self.signal.to_dict(),
            "observation_duplicate": self.observation_duplicate,
            "signal_duplicate": self.signal_duplicate,
            "observer_state": self.observer_state.to_dict(),
        }


class WatchdogObserverRegistry:
    """Owns explicit observer attachment/start/stop and signal intake.

    A descriptor is not proof of an active source.  Only a registered runtime
    instance that reached RUNNING may submit observations.  Disabling an
    observer first commits DISABLED and then invokes its stop callback; a late
    callback therefore fails closed instead of leaking a final observation.
    """

    def __init__(
        self,
        store: FaultStateStore,
        *,
        classifier: WatchdogSignalClassifier | None = None,
        emission_guard: ObservationEmissionGuard | None = None,
        signal_sink: Callable[[FaultSignal], None] | None = None,
    ) -> None:
        self.store = store
        self.classifier = classifier or WatchdogSignalClassifier()
        self.emission_guard = emission_guard or ObservationEmissionGuard()
        self.signal_sink = signal_sink
        self._guard = threading.RLock()
        self._observers: dict[str, RuntimeObserver] = {}
        self._last_submissions: dict[str, ObserverSubmission] = {}

    def register(self, observer: RuntimeObserver) -> ObserverState:
        descriptor = observer.descriptor
        with self._guard:
            existing = self._observers.get(descriptor.observer_id)
            if existing is not None and existing is not observer:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                    f"observer runtime already registered: {descriptor.observer_id}",
                )
            state = self.store.register_observer(descriptor)
            self._observers[descriptor.observer_id] = observer
            return state

    def attach(self, observer_id: str) -> ObserverState:
        with self._guard:
            observer = self._runtime(observer_id)
            current = self.store.require_observer(observer_id)
            if current.descriptor.maturity is ObserverMaturity.SOURCE_INACTIVE:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.OBSERVER_SOURCE_INACTIVE,
                    f"source-inactive observer cannot attach: {observer_id}",
                )
            if current.lifecycle is ObserverLifecycle.ATTACHED:
                return current
            if current.lifecycle is ObserverLifecycle.RUNNING:
                return current
            attached = self.store.transition_observer(
                observer_id,
                ObserverLifecycle.ATTACHED,
                expected_revision=current.revision,
                reason="runtime observer attached to its declared observation point",
            )
            try:
                observer.attach(lambda observation: self.submit(observation))
            except Exception as error:
                self.store.transition_observer(
                    observer_id,
                    ObserverLifecycle.FAILED,
                    expected_revision=attached.revision,
                    reason="observer attach callback failed",
                    error=f"{type(error).__name__}: {error}",
                )
                raise
            return attached

    def start(self, observer_id: str) -> ObserverState:
        with self._guard:
            observer = self._runtime(observer_id)
            current = self.store.require_observer(observer_id)
            if current.lifecycle is ObserverLifecycle.RUNNING:
                return current
            if current.lifecycle in {
                ObserverLifecycle.REGISTERED,
                ObserverLifecycle.STOPPED,
                ObserverLifecycle.DISABLED,
                ObserverLifecycle.FAILED,
            }:
                current = self.attach(observer_id)
                current = self.store.require_observer(observer_id)
            if current.lifecycle is not ObserverLifecycle.ATTACHED and current.lifecycle is not ObserverLifecycle.STOPPED:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.STATE_STORE_CONFLICT,
                    f"observer cannot start from {current.lifecycle.value}",
                )
            started = self.store.transition_observer(
                observer_id,
                ObserverLifecycle.RUNNING,
                expected_revision=current.revision,
                reason="observer started at its declared observation point",
            )
            try:
                observer.start()
            except Exception as error:
                self.store.transition_observer(
                    observer_id,
                    ObserverLifecycle.FAILED,
                    expected_revision=started.revision,
                    reason="observer start callback failed",
                    error=f"{type(error).__name__}: {error}",
                )
                raise
            return self.store.require_observer(observer_id)

    def start_defaults(self) -> tuple[ObserverState, ...]:
        states: list[ObserverState] = []
        with self._guard:
            ids = sorted(
                observer_id
                for observer_id, observer in self._observers.items()
                if observer.descriptor.enabled_by_default
                and observer.descriptor.maturity is ObserverMaturity.ACTIVE_REAL
            )
        for observer_id in ids:
            states.append(self.start(observer_id))
        return tuple(states)

    def runtime_snapshot(self, observer_id: str) -> Mapping[str, Any]:
        with self._guard:
            return dict(self._runtime(observer_id).snapshot())

    def recover_stale_running(
        self,
        observer_id: str,
        *,
        process_epoch: str,
    ) -> ObserverState:
        """Replace process-local callbacks lost while durable state said RUNNING."""
        with self._guard:
            observer = self._runtime(observer_id)
            current = self.store.require_observer(observer_id)
            runtime = dict(observer.snapshot())
            if current.lifecycle is not ObserverLifecycle.RUNNING:
                return self.start(observer_id)
            if bool(runtime.get("attached")) and bool(runtime.get("running")):
                return current
            stopped = self.store.transition_observer(
                observer_id,
                ObserverLifecycle.STOPPED,
                expected_revision=current.revision,
                reason=f"process epoch {process_epoch} replaced stale runtime callbacks",
            )
            try:
                observer.stop()
            except Exception as error:
                failed = self.store.transition_observer(
                    observer_id,
                    ObserverLifecycle.FAILED,
                    expected_revision=stopped.revision,
                    reason="stale observer callback cleanup failed",
                    error=f"{type(error).__name__}: {error}",
                )
                if failed.lifecycle is ObserverLifecycle.FAILED:
                    return self.start(observer_id)
            return self.start(observer_id)

    def fail(
        self,
        observer_id: str,
        *,
        error: str,
        reason: str,
    ) -> ObserverState:
        with self._guard:
            observer = self._runtime(observer_id)
            current = self.store.require_observer(observer_id)
            if current.lifecycle is ObserverLifecycle.FAILED:
                return current
            failed = self.store.transition_observer(
                observer_id,
                ObserverLifecycle.FAILED,
                expected_revision=current.revision,
                reason=reason,
                error=error,
            )
            try:
                observer.stop()
            finally:
                self.emission_guard.reset(observer_id)
            return failed

    def stop(self, observer_id: str, *, reason: str = "observer stopped") -> ObserverState:
        with self._guard:
            observer = self._runtime(observer_id)
            current = self.store.require_observer(observer_id)
            if current.lifecycle is ObserverLifecycle.STOPPED:
                return current
            stopped = self.store.transition_observer(
                observer_id,
                ObserverLifecycle.STOPPED,
                expected_revision=current.revision,
                reason=reason,
            )
            try:
                observer.stop()
            except Exception as error:
                return self.store.transition_observer(
                    observer_id,
                    ObserverLifecycle.FAILED,
                    expected_revision=stopped.revision,
                    reason="observer stop callback failed",
                    error=f"{type(error).__name__}: {error}",
                )
            return stopped

    def disable(self, observer_id: str, *, reason: str = "observer disabled by runtime control") -> ObserverState:
        with self._guard:
            observer = self._runtime(observer_id)
            current = self.store.require_observer(observer_id)
            if current.lifecycle is ObserverLifecycle.DISABLED:
                return current
            disabled = self.store.transition_observer(
                observer_id,
                ObserverLifecycle.DISABLED,
                expected_revision=current.revision,
                reason=reason,
            )
            try:
                observer.stop()
            finally:
                self.emission_guard.reset(observer_id)
            return disabled

    def submit(self, observation: StructuredObservation) -> ObserverSubmission:
        observer_id = observation.provenance.observer_id
        with self._guard:
            runtime = self._runtime(observer_id)
            current = self.store.require_observer(observer_id)
            if not current.lifecycle.accepts_observations:
                code = (
                    FaultRuntimeErrorCode.OBSERVER_DISABLED
                    if current.lifecycle is ObserverLifecycle.DISABLED
                    else FaultRuntimeErrorCode.OBSERVER_NOT_RUNNING
                )
                raise FaultRuntimeError(
                    code,
                    f"observer cannot emit while {current.lifecycle.value}: {observer_id}",
                )
            descriptor = current.descriptor
            if observation.category not in descriptor.categories:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.INVALID_OBSERVATION,
                    "observer emitted an undeclared observation category",
                    details={
                        "observer_id": observer_id,
                        "category": observation.category.value,
                        "declared": [item.value for item in descriptor.categories],
                    },
                )
            if observation.provenance.observation_point != descriptor.observation_point:
                raise FaultRuntimeError(
                    FaultRuntimeErrorCode.INVALID_OBSERVATION,
                    "observation point differs from registered descriptor",
                )
            emission = self.emission_guard.accept(observation)
            if not emission.accepted:
                submission = ObserverSubmission(
                    observation=observation,
                    emission=emission,
                    classification=ClassificationResult(None, True, emission.reason),
                    signal=None,
                    observation_duplicate=False,
                    signal_duplicate=False,
                    observer_state=current,
                )
                self._last_submissions[observer_id] = submission
                return submission
            classification = self.classifier.classify(observation)
            observation_value, observation_duplicate, updated = self.store.record_observation(
                observation,
                emitted=int(classification.signal is not None),
            )
            signal: FaultSignal | None = None
            signal_duplicate = False
            if classification.signal is not None:
                if classification.signal.kind not in descriptor.emitted_kinds:
                    raise FaultRuntimeError(
                        FaultRuntimeErrorCode.INVALID_OBSERVATION,
                        "classified fault kind is not declared by observer",
                        details={
                            "observer_id": observer_id,
                            "kind": classification.signal.kind.value,
                            "declared": [item.value for item in descriptor.emitted_kinds],
                        },
                    )
                signal, signal_duplicate = self.store.append_signal(classification.signal)
                if not signal_duplicate and self.signal_sink is not None:
                    self.signal_sink(signal)
            submission = ObserverSubmission(
                observation=observation_value,
                emission=emission,
                classification=classification,
                signal=signal,
                observation_duplicate=observation_duplicate,
                signal_duplicate=signal_duplicate,
                observer_state=updated,
            )
            self._last_submissions[observer_id] = submission
            return submission

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            states = {item.descriptor.observer_id: item for item in self.store.observers()}
            runtimes = {
                observer_id: dict(observer.snapshot())
                for observer_id, observer in sorted(self._observers.items())
            }
            return {
                "schema": "zyra.watchdog-observer-registry/v1",
                "state_owner": "FaultStateStore",
                "observers": {
                    observer_id: {
                        "state": state.to_dict(),
                        "runtime": runtimes.get(observer_id, {}),
                        "last_submission": (
                            self._last_submissions[observer_id].to_dict()
                            if observer_id in self._last_submissions
                            else None
                        ),
                    }
                    for observer_id, state in sorted(states.items())
                },
                "emission_guard": self.emission_guard.snapshot(),
                "maturity_values": [item.value for item in ObserverMaturity],
                "active_real": sorted(
                    observer_id
                    for observer_id, state in states.items()
                    if state.descriptor.maturity is ObserverMaturity.ACTIVE_REAL
                    and state.lifecycle is ObserverLifecycle.RUNNING
                ),
            }

    def _runtime(self, observer_id: str) -> RuntimeObserver:
        observer = self._observers.get(observer_id)
        if observer is None:
            raise FaultRuntimeError(
                FaultRuntimeErrorCode.OBSERVER_NOT_REGISTERED,
                f"observer runtime is not registered: {observer_id}",
            )
        return observer


class CallbackObserver:
    """Lifecycle-complete observer for event/callback boundaries.

    It is used where another canonical owner (permission, workspace, provider)
    already emits structured receipts.  ``observe`` is deliberately inactive
    until the registry starts the observer and becomes inactive immediately on
    stop, making disconnect tests observable without mocking registry state.
    """

    def __init__(
        self,
        descriptor: ObserverDescriptor,
    ) -> None:
        self.descriptor = descriptor
        self._emit: Callable[[StructuredObservation], None] | None = None
        self._attached = False
        self._running = False
        self._received = 0
        self._delivered = 0
        self._stopped = 0
        self._guard = threading.RLock()

    def attach(self, emit: Callable[[StructuredObservation], None]) -> None:
        with self._guard:
            if self._running and self._emit is not emit:
                raise RuntimeError("cannot replace observer callback while it is running")
            self._emit = emit
            self._attached = True

    def start(self) -> None:
        with self._guard:
            if not self._attached or self._emit is None:
                raise RuntimeError("observer cannot start before attach")
            self._running = True

    def stop(self) -> None:
        with self._guard:
            self._running = False
            self._stopped += 1

    def observe(self, observation: StructuredObservation) -> bool:
        with self._guard:
            self._received += 1
            emit = self._emit if self._running else None
        if emit is None:
            return False
        emit(observation)
        with self._guard:
            self._delivered += 1
        return True

    def snapshot(self) -> Mapping[str, Any]:
        with self._guard:
            return {
                "attached": self._attached,
                "running": self._running,
                "received": self._received,
                "delivered": self._delivered,
                "stopped": self._stopped,
                "observation_point": self.descriptor.observation_point,
            }


def descriptor_contract(descriptors: Sequence[ObserverDescriptor]) -> dict[str, Any]:
    return {
        "schema": "zyra.watchdog-observer-descriptors/v1",
        "observers": [item.to_dict() for item in descriptors],
        "counts": {
            maturity.value: sum(item.maturity is maturity for item in descriptors)
            for maturity in ObserverMaturity
        },
        "required_maturity_values": [
            ObserverMaturity.ACTIVE_REAL.value,
            ObserverMaturity.EXPERIMENTAL.value,
            ObserverMaturity.SOURCE_INACTIVE.value,
            ObserverMaturity.INJECTION_ONLY.value,
        ],
        "descriptor_is_activity_proof": False,
    }
