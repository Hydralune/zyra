from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from zyra_workers.browser_observability import (
    BrowserCrashDetector,
    BrowserObservation,
    CrashDetectorPolicy,
    ObservationScope,
    WatchdogSignal,
)

from .contracts import CorrelationRefs
from .observers import BrowserCrashObserver


class BrowserProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None:
        ...


@dataclass(slots=True)
class BrowserSourceBinding:
    refs: CorrelationRefs
    scope: ObservationScope
    generation: int
    attached_process_id: int | None
    process_handle: BrowserProcessHandle | None
    attached: bool = True
    intentional_stop: bool = False
    poll_count: int = 0
    source_signal_count: int = 0
    canonical_observation_count: int = 0
    last_source_sequence: int = 0
    last_source_signal_ids: list[str] = field(default_factory=list)


class BrowserCrashSourceRuntime:
    """Owns the live 04D crash-detector attachment used by the fault runtime.

    The older 04D detector remains the browser process/CDP state owner. This
    runtime supplies the missing lifecycle connection: explicit scope binding,
    event forwarding, polling and generation fencing into BrowserCrashObserver.
    It never treats the upstream Browser Use CrashWatchdog as attached.
    """

    def __init__(
        self,
        observer: BrowserCrashObserver,
        *,
        detector: BrowserCrashDetector | None = None,
        policy: CrashDetectorPolicy | None = None,
    ) -> None:
        if detector is not None and policy is not None:
            raise ValueError("provide detector or policy, not both")
        self.observer = observer
        self.detector = detector or BrowserCrashDetector(policy=policy)
        self._guard = threading.RLock()
        self._bindings: dict[str, BrowserSourceBinding] = {}
        self._session_index: dict[str, str] = {}
        self._forwarded_signal_ids: set[str] = set()

    @staticmethod
    def scope_from_refs(refs: CorrelationRefs) -> ObservationScope:
        if not refs.browser_session_id:
            raise ValueError("browser crash source requires browser_session_id")
        if not refs.attempt_id:
            raise ValueError("browser crash source requires attempt_id as worker_request_id")
        return ObservationScope(
            run_id=refs.run_id,
            task_id=refs.task_id,
            node_id=refs.node_id,
            browser_session_id=refs.browser_session_id,
            canonical_session_id=refs.session_id,
            worker_request_id=refs.attempt_id,
        )

    def attach(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        process_id: int | None = None,
        process_handle: BrowserProcessHandle | None = None,
        cdp_connected: bool | None = None,
    ) -> BrowserSourceBinding:
        if generation < 0:
            raise ValueError("browser source generation must be non-negative")
        if process_handle is not None:
            handle_pid = int(process_handle.pid)
            if handle_pid <= 0:
                raise ValueError("browser process handle must have a positive pid")
            if process_id is not None and process_id != handle_pid:
                raise ValueError("process_id differs from browser process handle pid")
            process_id = handle_pid
        scope = self.scope_from_refs(refs)
        with self._guard:
            current_key = self._session_index.get(refs.browser_session_id)
            current = self._bindings.get(current_key or "")
            if current is not None:
                if generation < current.generation:
                    raise RuntimeError("stale browser generation cannot replace current source")
                if generation == current.generation and current.scope != scope:
                    raise RuntimeError("browser generation cannot change its observation scope")
                if generation == current.generation and current.attached:
                    if process_handle is not None:
                        current.process_handle = process_handle
                    return current
                if current.attached:
                    self.detector.close(current.scope)
                    current.attached = False
            self.detector.attach(
                scope,
                process_id=process_id,
                cdp_connected=cdp_connected,
            )
            binding = BrowserSourceBinding(
                refs=refs,
                scope=scope,
                generation=generation,
                attached_process_id=process_id,
                process_handle=process_handle,
            )
            self._bindings[scope.key] = binding
            self._session_index[refs.browser_session_id] = scope.key
            return binding

    def heartbeat(
        self,
        browser_session_id: str,
        *,
        generation: int,
        at_ms: int | None = None,
    ) -> tuple[WatchdogSignal, ...]:
        binding = self._require_current(browser_session_id, generation)
        return self._forward(binding, self.detector.heartbeat(binding.scope, at_ms=at_ms))

    def cdp_connected(
        self,
        browser_session_id: str,
        *,
        generation: int,
        at_ms: int | None = None,
    ) -> tuple[WatchdogSignal, ...]:
        binding = self._require_current(browser_session_id, generation)
        return self._forward(binding, self.detector.cdp_connected(binding.scope, at_ms=at_ms))

    def cdp_disconnected(
        self,
        browser_session_id: str,
        *,
        generation: int,
        reason: str,
        at_ms: int | None = None,
    ) -> tuple[WatchdogSignal, ...]:
        binding = self._require_current(browser_session_id, generation)
        signals = self.detector.cdp_disconnected(binding.scope, reason=reason, at_ms=at_ms)
        return self._forward(binding, signals)

    def request_started(
        self,
        browser_session_id: str,
        request_id: str,
        *,
        generation: int,
        at_ms: int | None = None,
    ) -> None:
        if not request_id.strip():
            raise ValueError("browser request identity must not be empty")
        binding = self._require_current(browser_session_id, generation)
        self.detector.request_started(binding.scope, request_id, at_ms=at_ms)

    def request_finished(
        self,
        browser_session_id: str,
        request_id: str,
        *,
        generation: int,
    ) -> None:
        binding = self._require_current(browser_session_id, generation)
        self.detector.request_finished(binding.scope, request_id)

    def observe(
        self,
        observation: BrowserObservation,
        *,
        generation: int,
    ) -> tuple[WatchdogSignal, ...]:
        binding = self._require_current(observation.scope.browser_session_id, generation)
        if observation.scope != binding.scope:
            raise RuntimeError("browser observation does not match attached source scope")
        return self._forward(binding, self.detector.observe(observation))

    def intentional_stop(
        self,
        browser_session_id: str,
        *,
        generation: int,
        at_ms: int | None = None,
    ) -> None:
        binding = self._require_current(browser_session_id, generation)
        self.detector.intentional_stop(binding.scope, at_ms=at_ms)
        binding.intentional_stop = True

    def poll(
        self,
        browser_session_id: str,
        *,
        generation: int,
        at_ms: int | None = None,
    ) -> tuple[WatchdogSignal, ...]:
        binding = self._require_current(browser_session_id, generation)
        signals = self.detector.poll(
            binding.scope,
            at_ms=at_ms,
            process_handle=binding.process_handle,
        )
        binding.poll_count += 1
        return self._forward(binding, signals)

    def poll_all(self, *, at_ms: int | None = None) -> Mapping[str, tuple[str, ...]]:
        with self._guard:
            bindings = tuple(
                item
                for item in self._bindings.values()
                if item.attached and not item.intentional_stop
            )
        output: dict[str, tuple[str, ...]] = {}
        for binding in bindings:
            signals = self.poll(
                binding.refs.browser_session_id,
                generation=binding.generation,
                at_ms=at_ms,
            )
            if signals:
                output[binding.refs.browser_session_id] = tuple(
                    str(signal.signal_id) for signal in signals
                )
        return output

    def close(self, browser_session_id: str, *, generation: int) -> bool:
        with self._guard:
            binding = self._lookup(browser_session_id)
            if binding is None or binding.generation != generation or not binding.attached:
                return False
            self.detector.close(binding.scope)
            binding.attached = False
            return True

    def detach_all(self, *, intentional: bool = True) -> tuple[str, ...]:
        with self._guard:
            bindings = tuple(item for item in self._bindings.values() if item.attached)
        detached: list[str] = []
        for binding in bindings:
            if intentional and not binding.intentional_stop:
                self.detector.intentional_stop(binding.scope)
                binding.intentional_stop = True
            if self.close(binding.refs.browser_session_id, generation=binding.generation):
                detached.append(binding.refs.browser_session_id)
        return tuple(sorted(detached))

    def snapshot(self) -> Mapping[str, Any]:
        with self._guard:
            bindings = {
                item.refs.browser_session_id: {
                    "scope_key": item.scope.key,
                    "run_id": item.refs.run_id,
                    "task_id": item.refs.task_id,
                    "generation": item.generation,
                    "process_id": item.attached_process_id,
                    "attached": item.attached,
                    "intentional_stop": item.intentional_stop,
                    "poll_count": item.poll_count,
                    "source_signal_count": item.source_signal_count,
                    "canonical_observation_count": item.canonical_observation_count,
                    "last_source_sequence": item.last_source_sequence,
                    "last_source_signal_ids": list(item.last_source_signal_ids[-20:]),
                }
                for item in self._bindings.values()
            }
        return {
            "schema": "zyra.browser-crash-source-runtime/v1",
            "implementation": "zyra_workers.browser_observability.BrowserCrashDetector",
            "upstream_browser_use_crash_watchdog_attached": False,
            "bindings": bindings,
            "forwarded_source_signal_count": len(self._forwarded_signal_ids),
        }

    def _forward(
        self,
        binding: BrowserSourceBinding,
        signals: tuple[WatchdogSignal, ...],
    ) -> tuple[WatchdogSignal, ...]:
        for signal in signals:
            signal_id = str(signal.signal_id)
            with self._guard:
                if signal_id in self._forwarded_signal_ids:
                    continue
                self._forwarded_signal_ids.add(signal_id)
            binding.source_signal_count += 1
            binding.last_source_sequence = max(binding.last_source_sequence, int(signal.sequence))
            binding.last_source_signal_ids.append(signal_id)
            observation = self.observer.observe_04d_signal(signal.to_dict())
            if observation is not None:
                binding.canonical_observation_count += 1
        return signals

    def _require_current(self, browser_session_id: str, generation: int) -> BrowserSourceBinding:
        with self._guard:
            binding = self._lookup(browser_session_id)
            if binding is None or not binding.attached:
                raise RuntimeError("browser crash source is not attached")
            if binding.generation != generation:
                raise RuntimeError("stale browser source generation")
            return binding

    def _lookup(self, browser_session_id: str) -> BrowserSourceBinding | None:
        scope_key = self._session_index.get(browser_session_id)
        return self._bindings.get(scope_key or "")


def browser_source_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.browser-crash-source-contract/v1",
        "source_state_owner": "zyra_workers.browser_observability.BrowserCrashDetector",
        "fault_observer": "zyra_scheduler.fault_runtime.BrowserCrashObserver",
        "attach_owner": "zyra_scheduler.fault_runtime.BrowserCrashSourceRuntime",
        "generation_fenced": True,
        "polls_real_process_handle": True,
        "forwards_cdp_disconnect": True,
        "suppresses_intentional_stop": True,
        "upstream_browser_use_crash_watchdog_attached": False,
    }


__all__ = [
    "BrowserCrashSourceRuntime",
    "BrowserProcessHandle",
    "BrowserSourceBinding",
    "browser_source_contract",
]
