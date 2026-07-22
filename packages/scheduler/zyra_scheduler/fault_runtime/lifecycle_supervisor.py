from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import (
    ObserverLifecycle,
    ObserverMaturity,
    runtime_id,
    utc_now,
)
from .observer_registry import WatchdogObserverRegistry
from .state_store import FaultStateStore


@dataclass(frozen=True, slots=True)
class ObserverRestartPolicy:
    max_failures: int = 4
    base_backoff_ms: int = 250
    max_backoff_ms: int = 30_000
    stale_after_ms: int = 60_000

    def __post_init__(self) -> None:
        if self.max_failures < 1:
            raise ValueError("observer max_failures must be positive")
        if self.base_backoff_ms < 1 or self.max_backoff_ms < self.base_backoff_ms:
            raise ValueError("observer restart backoff bounds are invalid")
        if self.stale_after_ms < 1:
            raise ValueError("observer stale_after_ms must be positive")

    def backoff_ms(self, failure_count: int) -> int:
        return min(
            self.base_backoff_ms * (2 ** max(0, failure_count - 1)),
            self.max_backoff_ms,
        )


class ObserverRuntimeSupervisor:
    """Reconciles durable observer state with process-local callback reality.

    ``FaultStateStore`` survives a crash, but Python callback objects do not.
    Therefore a persisted RUNNING row cannot by itself prove that a new
    process has attached the source. The supervisor owns a process epoch,
    restart backoff and heartbeat cursor while the registry continues to own
    lifecycle transitions and the store remains the only durable owner.
    """

    def __init__(
        self,
        store: FaultStateStore,
        registry: WatchdogObserverRegistry,
        *,
        monotonic_ms: Callable[[], int] | None = None,
        policy: ObserverRestartPolicy | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self.policy = policy or ObserverRestartPolicy()
        self.process_epoch = runtime_id("watchdog-epoch")
        self._guard = threading.RLock()
        self._runtime: dict[str, dict[str, Any]] = {}
        self._startup_report: list[dict[str, Any]] = []

    def reconcile_startup(self) -> tuple[Mapping[str, Any], ...]:
        """Attach defaults and repair stale RUNNING rows from prior processes."""
        report: list[dict[str, Any]] = []
        for state in self.store.observers():
            descriptor = state.descriptor
            observer_id = descriptor.observer_id
            if descriptor.maturity is ObserverMaturity.SOURCE_INACTIVE:
                if state.lifecycle.accepts_observations:
                    repaired = self.registry.fail(
                        observer_id,
                        error="source_inactive observer was persisted as running",
                        reason="startup maturity fence failed closed",
                    )
                    report.append(self._entry(observer_id, "failed_closed", repaired.lifecycle.value))
                else:
                    report.append(self._entry(observer_id, "source_inactive", state.lifecycle.value))
                continue
            if descriptor.maturity is ObserverMaturity.INJECTION_ONLY:
                report.append(self._entry(observer_id, "injection_owned", state.lifecycle.value))
                continue
            runtime = self.registry.runtime_snapshot(observer_id)
            if state.lifecycle is ObserverLifecycle.DISABLED:
                report.append(self._entry(observer_id, "preserved_disabled", state.lifecycle.value))
                self._initialize_runtime(observer_id, state.lifecycle, now=self._now(None))
                continue
            stale_running = state.lifecycle is ObserverLifecycle.RUNNING and not (
                bool(runtime.get("attached")) and bool(runtime.get("running"))
            )
            if stale_running:
                state = self.registry.recover_stale_running(
                    observer_id,
                    process_epoch=self.process_epoch,
                )
                report.append(self._entry(observer_id, "reattached_stale_running", state.lifecycle.value))
            elif descriptor.enabled_by_default and not state.lifecycle.accepts_observations:
                state = self.registry.start(observer_id)
                report.append(self._entry(observer_id, "started_default", state.lifecycle.value))
            else:
                report.append(self._entry(observer_id, "preserved", state.lifecycle.value))
            self._initialize_runtime(observer_id, state.lifecycle, now=self._now(None))
        with self._guard:
            self._startup_report = report
        self._persist_epoch(report)
        return tuple(report)

    def source_heartbeat(
        self,
        observer_id: str,
        *,
        generation: int,
        sequence: int,
        at_ms: int | None = None,
    ) -> bool:
        now = self._now(at_ms)
        if generation < 0 or sequence < 0:
            raise ValueError("observer heartbeat generation and sequence must be non-negative")
        with self._guard:
            current = self._require_runtime(observer_id)
            if generation < int(current["generation"]):
                current["stale_heartbeats"] += 1
                return False
            if generation > int(current["generation"]):
                current["generation"] = generation
                current["sequence"] = -1
            if sequence <= int(current["sequence"]):
                current["stale_heartbeats"] += 1
                return False
            current["sequence"] = sequence
            current["last_heartbeat_ms"] = now
            current["last_heartbeat_at"] = utc_now()
            current["heartbeat_count"] += 1
            current["heartbeat_required"] = True
            current["failure_count"] = 0
            current["next_restart_ms"] = None
            current["stale"] = False
            return True

    def report_failure(
        self,
        observer_id: str,
        *,
        error: str,
        at_ms: int | None = None,
    ) -> Mapping[str, Any]:
        now = self._now(at_ms)
        with self._guard:
            runtime = self._require_runtime(observer_id)
            state = self.store.require_observer(observer_id)
            if state.descriptor.maturity is not ObserverMaturity.ACTIVE_REAL:
                raise ValueError("only active_real observers may enter restart supervision")
            if state.lifecycle is ObserverLifecycle.DISABLED:
                return self._failure_receipt(observer_id, runtime, state.lifecycle, "disabled")
            if state.lifecycle is not ObserverLifecycle.FAILED:
                state = self.registry.fail(
                    observer_id,
                    error=error,
                    reason="runtime supervisor received a real source failure",
                )
            runtime["failure_count"] += 1
            runtime["last_error"] = error
            runtime["last_failed_ms"] = now
            runtime["last_failed_at"] = utc_now()
            runtime["stale"] = False
            if runtime["failure_count"] >= self.policy.max_failures:
                state = self.registry.disable(
                    observer_id,
                    reason="observer exhausted bounded restart attempts",
                )
                runtime["next_restart_ms"] = None
                outcome = "disabled_after_failures"
            else:
                delay = self.policy.backoff_ms(int(runtime["failure_count"]))
                runtime["next_restart_ms"] = now + delay
                outcome = "restart_scheduled"
            self._persist_observer(observer_id, runtime)
            return self._failure_receipt(observer_id, runtime, state.lifecycle, outcome)

    def restart_due(self, *, at_ms: int | None = None) -> tuple[Mapping[str, Any], ...]:
        now = self._now(at_ms)
        with self._guard:
            candidates = tuple(
                observer_id
                for observer_id, runtime in sorted(self._runtime.items())
                if runtime.get("next_restart_ms") is not None
                and int(runtime["next_restart_ms"]) <= now
            )
        results: list[Mapping[str, Any]] = []
        for observer_id in candidates:
            results.append(self._restart_one(observer_id, now))
        return tuple(results)

    def sweep_stale(self, *, at_ms: int | None = None) -> tuple[Mapping[str, Any], ...]:
        now = self._now(at_ms)
        stale: list[tuple[str, int]] = []
        with self._guard:
            for observer_id, runtime in self._runtime.items():
                last = int(runtime["last_heartbeat_ms"])
                state = self.store.require_observer(observer_id)
                if not state.lifecycle.accepts_observations:
                    continue
                if not runtime["heartbeat_required"]:
                    continue
                if now - last < self.policy.stale_after_ms:
                    continue
                if runtime["stale"]:
                    continue
                runtime["stale"] = True
                stale.append((observer_id, now - last))
        return tuple(
            self.report_failure(
                observer_id,
                error=f"observer heartbeat stale for {age_ms}ms",
                at_ms=now,
            )
            for observer_id, age_ms in stale
        )

    def mark_stopped(self, observer_id: str) -> None:
        with self._guard:
            runtime = self._runtime.get(observer_id)
            if runtime is None:
                return
            runtime["next_restart_ms"] = None
            runtime["stale"] = False
            runtime["stopped_at"] = utc_now()

    def snapshot(self) -> Mapping[str, Any]:
        with self._guard:
            runtime = {
                observer_id: dict(value)
                for observer_id, value in sorted(self._runtime.items())
            }
            report = [dict(item) for item in self._startup_report]
        return {
            "schema": "zyra.observer-runtime-supervisor/v1",
            "process_epoch": self.process_epoch,
            "startup_report": report,
            "observers": runtime,
            "policy": {
                "max_failures": self.policy.max_failures,
                "base_backoff_ms": self.policy.base_backoff_ms,
                "max_backoff_ms": self.policy.max_backoff_ms,
                "stale_after_ms": self.policy.stale_after_ms,
            },
            "state_owner": "FaultStateStore.runtime_metadata",
            "recovery_plan_owner": False,
        }

    def _restart_one(self, observer_id: str, now: int) -> Mapping[str, Any]:
        with self._guard:
            runtime = self._require_runtime(observer_id)
            scheduled = runtime.get("next_restart_ms")
            if scheduled is None or int(scheduled) > now:
                return {
                    "observer_id": observer_id,
                    "outcome": "not_due",
                    "process_epoch": self.process_epoch,
                }
            runtime["next_restart_ms"] = None
        try:
            state = self.registry.start(observer_id)
        except Exception as error:
            return self.report_failure(
                observer_id,
                error=f"restart failed: {type(error).__name__}: {error}",
                at_ms=now,
            )
        with self._guard:
            runtime = self._require_runtime(observer_id)
            runtime["restart_count"] += 1
            runtime["generation"] += 1
            runtime["sequence"] = -1
            runtime["last_heartbeat_ms"] = now
            runtime["last_heartbeat_at"] = utc_now()
            runtime["last_error"] = ""
            runtime["stale"] = False
            self._persist_observer(observer_id, runtime)
            return {
                "observer_id": observer_id,
                "outcome": "restarted",
                "lifecycle": state.lifecycle.value,
                "generation": runtime["generation"],
                "restart_count": runtime["restart_count"],
                "process_epoch": self.process_epoch,
            }

    def _initialize_runtime(
        self,
        observer_id: str,
        lifecycle: ObserverLifecycle,
        *,
        now: int,
    ) -> dict[str, Any]:
        with self._guard:
            prior = self.store.metadata(f"observer-supervision:{observer_id}")
            prior_value = {} if prior is None else dict(prior[0])
            runtime = {
                "observer_id": observer_id,
                "process_epoch": self.process_epoch,
                "generation": int(prior_value.get("generation", 0)) + 1,
                "sequence": -1,
                "lifecycle": lifecycle.value,
                "failure_count": int(prior_value.get("failure_count", 0)),
                "restart_count": int(prior_value.get("restart_count", 0)),
                "heartbeat_count": 0,
                "heartbeat_required": False,
                "stale_heartbeats": 0,
                "last_heartbeat_ms": now,
                "last_heartbeat_at": utc_now(),
                "last_failed_ms": None,
                "last_failed_at": "",
                "last_error": "",
                "next_restart_ms": None,
                "stale": False,
            }
            self._runtime[observer_id] = runtime
            self._persist_observer(observer_id, runtime)
            return runtime

    def _persist_epoch(self, report: list[dict[str, Any]]) -> None:
        key = "observer-supervision:active-epoch"
        prior = self.store.metadata(key)
        expected = 0 if prior is None else prior[1]
        self.store.put_metadata(
            key,
            {
                "process_epoch": self.process_epoch,
                "started_at": utc_now(),
                "observer_count": len(report),
                "stale_running_recovered": sum(
                    item["action"] == "reattached_stale_running"
                    for item in report
                ),
            },
            expected_revision=expected,
        )

    def _persist_observer(self, observer_id: str, runtime: Mapping[str, Any]) -> None:
        key = f"observer-supervision:{observer_id}"
        prior = self.store.metadata(key)
        expected = 0 if prior is None else prior[1]
        self.store.put_metadata(key, runtime, expected_revision=expected)

    def _require_runtime(self, observer_id: str) -> dict[str, Any]:
        runtime = self._runtime.get(observer_id)
        if runtime is None:
            raise KeyError(f"observer is not supervised: {observer_id}")
        return runtime

    def _now(self, value: int | None) -> int:
        now = self.monotonic_ms() if value is None else int(value)
        if now < 0:
            raise ValueError("monotonic time must not be negative")
        return now

    def _entry(self, observer_id: str, action: str, lifecycle: str) -> dict[str, Any]:
        return {
            "observer_id": observer_id,
            "action": action,
            "lifecycle": lifecycle,
            "process_epoch": self.process_epoch,
        }

    def _failure_receipt(
        self,
        observer_id: str,
        runtime: Mapping[str, Any],
        lifecycle: ObserverLifecycle,
        outcome: str,
    ) -> Mapping[str, Any]:
        return {
            "schema": "zyra.observer-runtime-failure/v1",
            "observer_id": observer_id,
            "outcome": outcome,
            "lifecycle": lifecycle.value,
            "failure_count": runtime["failure_count"],
            "next_restart_ms": runtime["next_restart_ms"],
            "process_epoch": self.process_epoch,
        }


def lifecycle_supervision_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.observer-runtime-supervision-contract/v1",
        "durable_owner": "FaultStateStore.runtime_metadata",
        "process_epoch_fenced": True,
        "persisted_running_is_not_attachment_proof": True,
        "bounded_restart_backoff": True,
        "generation_and_sequence_heartbeat_fenced": True,
        "source_inactive_fail_closed": True,
        "recovery_plan_owner": "M1-S07C",
    }


__all__ = [
    "ObserverRestartPolicy",
    "ObserverRuntimeSupervisor",
    "lifecycle_supervision_contract",
]
