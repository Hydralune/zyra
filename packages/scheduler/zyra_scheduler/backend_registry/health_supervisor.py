from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import (
    BackendControlEvent,
    BackendDefinition,
    BackendFailureKind,
    BackendHealthRecord,
    BackendHealthStatus,
    BackendKind,
    BackendRecoveryIntent,
    checksum,
    new_backend_id,
    now_timestamp,
)
from .registry import BackendRegistry
from .transport import BackendHealthProbe, BackendTransportRegistry


class CircuitPhase(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class BackendProbeSample:
    sample_id: str
    backend_id: str
    generation: str
    ok: bool
    status: str
    latency_milliseconds: float
    checked_at: float
    transport: str
    failure_kind: BackendFailureKind | None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_probe(
        cls,
        probe: BackendHealthProbe,
        *,
        generation: str,
    ) -> BackendProbeSample:
        kind: BackendFailureKind | None
        if probe.ok:
            kind = None
        elif "timeout" in probe.status.lower() or "timed out" in str(probe.detail).lower():
            kind = BackendFailureKind.BACKEND_TIMEOUT
        elif "capacity" in probe.status.lower():
            kind = BackendFailureKind.BACKEND_CAPACITY
        elif "protocol" in probe.status.lower():
            kind = BackendFailureKind.BACKEND_PROTOCOL
        else:
            kind = BackendFailureKind.BACKEND_UNAVAILABLE
        return cls(
            sample_id=new_backend_id("backend_probe"),
            backend_id=probe.backend_id,
            generation=generation,
            ok=probe.ok,
            status=probe.status,
            latency_milliseconds=max(0.0, probe.latency_milliseconds),
            checked_at=probe.checked_at,
            transport=probe.transport,
            failure_kind=kind,
            detail=dict(probe.detail),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "backend_id": self.backend_id,
            "generation": self.generation,
            "ok": self.ok,
            "status": self.status,
            "latency_milliseconds": self.latency_milliseconds,
            "checked_at": self.checked_at,
            "transport": self.transport,
            "failure_kind": None if self.failure_kind is None else self.failure_kind.value,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class BackendCircuitPolicy:
    window_size: int = 20
    minimum_samples: int = 3
    open_failure_ratio: float = 0.60
    degraded_failure_ratio: float = 0.20
    slow_latency_milliseconds: float = 5_000.0
    open_seconds: float = 30.0
    half_open_successes: int = 2
    probe_parallelism: int = 8

    def __post_init__(self) -> None:
        if self.window_size < 1:
            raise ValueError("backend circuit window must be positive")
        if self.minimum_samples < 1 or self.minimum_samples > self.window_size:
            raise ValueError("backend circuit minimum samples is invalid")
        if not 0.0 <= self.open_failure_ratio <= 1.0:
            raise ValueError("backend circuit open ratio is invalid")
        if not 0.0 <= self.degraded_failure_ratio <= self.open_failure_ratio:
            raise ValueError("backend circuit degraded ratio is invalid")
        if self.open_seconds <= 0:
            raise ValueError("backend circuit open duration must be positive")
        if self.half_open_successes < 1:
            raise ValueError("backend half-open success count must be positive")
        if self.probe_parallelism < 1:
            raise ValueError("backend probe parallelism must be positive")


@dataclass(frozen=True, slots=True)
class BackendCircuitSnapshot:
    backend_id: str
    phase: CircuitPhase
    sample_count: int
    success_count: int
    failure_count: int
    failure_ratio: float
    mean_latency_milliseconds: float
    percentile_95_latency_milliseconds: float
    consecutive_successes: int
    consecutive_failures: int
    opened_at: float | None
    retry_at: float | None
    generation: str
    reason: str
    sample_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "phase": self.phase.value,
            "sample_count": self.sample_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "failure_ratio": self.failure_ratio,
            "mean_latency_milliseconds": self.mean_latency_milliseconds,
            "percentile_95_latency_milliseconds": self.percentile_95_latency_milliseconds,
            "consecutive_successes": self.consecutive_successes,
            "consecutive_failures": self.consecutive_failures,
            "opened_at": self.opened_at,
            "retry_at": self.retry_at,
            "generation": self.generation,
            "reason": self.reason,
            "sample_ids": list(self.sample_ids),
        }


@dataclass(frozen=True, slots=True)
class BackendProbeBatch:
    batch_id: str
    started_at: float
    completed_at: float
    samples: tuple[BackendProbeSample, ...]
    circuits: tuple[BackendCircuitSnapshot, ...]
    events: tuple[BackendControlEvent, ...]

    @property
    def ok(self) -> bool:
        return all(sample.ok for sample in self.samples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "ok": self.ok,
            "samples": [sample.to_dict() for sample in self.samples],
            "circuits": [circuit.to_dict() for circuit in self.circuits],
            "events": [event.to_dict() for event in self.events],
        }


class BackendHealthSupervisor:
    def __init__(
        self,
        registry: BackendRegistry,
        *,
        transports: BackendTransportRegistry | None = None,
        policy: BackendCircuitPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.store = registry.store
        self.transports = transports or BackendTransportRegistry()
        self.policy = policy or BackendCircuitPolicy()
        self._lock = threading.RLock()
        self._samples: dict[str, deque[BackendProbeSample]] = defaultdict(
            lambda: deque(maxlen=self.policy.window_size)
        )
        self._opened_at: dict[str, float] = {}
        self._half_open_successes: dict[str, int] = defaultdict(int)
        self._generation: dict[str, str] = {}

    def probe(
        self,
        definition: BackendDefinition,
        *,
        in_process_operation: Callable[..., Any] | None = None,
    ) -> tuple[BackendProbeSample, BackendCircuitSnapshot, BackendControlEvent | None]:
        before = self.registry.health(definition.backend_id)
        if not definition.enabled:
            sample = BackendProbeSample(
                sample_id=new_backend_id("backend_probe"),
                backend_id=definition.backend_id,
                generation=str(definition.metadata.get("generation") or "disabled"),
                ok=False,
                status="disabled",
                latency_milliseconds=0.0,
                checked_at=now_timestamp(),
                transport=definition.kind.value,
                failure_kind=BackendFailureKind.BACKEND_UNAVAILABLE,
                detail={"definition_enabled": False},
            )
        else:
            try:
                transport = self.transports.create(
                    definition,
                    in_process_operation=in_process_operation,
                )
                raw = transport.probe(definition)
            except BaseException as error:
                sample = BackendProbeSample(
                    sample_id=new_backend_id("backend_probe"),
                    backend_id=definition.backend_id,
                    generation=str(definition.metadata.get("generation") or ""),
                    ok=False,
                    status="transport_unavailable",
                    latency_milliseconds=0.0,
                    checked_at=now_timestamp(),
                    transport=definition.kind.value,
                    failure_kind=BackendFailureKind.BACKEND_UNAVAILABLE,
                    detail={
                        "exception_type": type(error).__name__,
                        "reason": str(error)[:2_048],
                    },
                )
            else:
                generation = str(
                    raw.detail.get("generation")
                    or definition.metadata.get("generation")
                    or ""
                )
                sample = BackendProbeSample.from_probe(raw, generation=generation)
        with self._lock:
            previous_generation = self._generation.get(definition.backend_id)
            if sample.generation and previous_generation and sample.generation != previous_generation:
                self._samples[definition.backend_id].clear()
                self._opened_at.pop(definition.backend_id, None)
                self._half_open_successes[definition.backend_id] = 0
            if sample.generation:
                self._generation[definition.backend_id] = sample.generation
            self._samples[definition.backend_id].append(sample)
            snapshot = self._evaluate(definition, at=sample.checked_at)
        after = self._persist(definition, before, sample, snapshot)
        event = None
        if before.status != after.status:
            event = self._event(definition, before, after, sample, snapshot)
            self.store.append_event(event)
        return sample, snapshot, event

    def probe_all(
        self,
        *,
        runtime_worker: str = "",
        in_process_operations: Mapping[str, Callable[..., Any]] | None = None,
    ) -> BackendProbeBatch:
        started = now_timestamp()
        definitions = self.registry.definitions(runtime_worker=runtime_worker)
        samples: list[BackendProbeSample] = []
        circuits: list[BackendCircuitSnapshot] = []
        events: list[BackendControlEvent] = []
        operations = dict(in_process_operations or {})
        semaphore = threading.BoundedSemaphore(self.policy.probe_parallelism)
        results: dict[str, tuple[BackendProbeSample, BackendCircuitSnapshot, BackendControlEvent | None]] = {}
        errors: dict[str, BaseException] = {}
        result_lock = threading.RLock()

        def run(definition: BackendDefinition) -> None:
            with semaphore:
                try:
                    result = self.probe(
                        definition,
                        in_process_operation=operations.get(definition.backend_id),
                    )
                except BaseException as error:
                    with result_lock:
                        errors[definition.backend_id] = error
                else:
                    with result_lock:
                        results[definition.backend_id] = result

        threads = [
            threading.Thread(target=run, args=(definition,), daemon=True)
            for definition in definitions
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            backend_id = sorted(errors)[0]
            raise RuntimeError(f"backend probe failed for {backend_id}: {errors[backend_id]}") from errors[backend_id]
        for backend_id in sorted(results):
            sample, circuit, event = results[backend_id]
            samples.append(sample)
            circuits.append(circuit)
            if event is not None:
                events.append(event)
        return BackendProbeBatch(
            batch_id=new_backend_id("backend_probe_batch"),
            started_at=started,
            completed_at=now_timestamp(),
            samples=tuple(samples),
            circuits=tuple(circuits),
            events=tuple(events),
        )

    def snapshot(self, backend_id: str, *, at: float | None = None) -> BackendCircuitSnapshot:
        definition = self.registry.definition(backend_id)
        with self._lock:
            return self._evaluate(definition, at=now_timestamp() if at is None else at)

    def snapshots(self) -> tuple[BackendCircuitSnapshot, ...]:
        return tuple(self.snapshot(item.backend_id) for item in self.registry.definitions())

    def permits_dispatch(self, backend_id: str, *, at: float | None = None) -> bool:
        snapshot = self.snapshot(backend_id, at=at)
        return snapshot.phase in {CircuitPhase.CLOSED, CircuitPhase.HALF_OPEN}

    def _evaluate(self, definition: BackendDefinition, *, at: float) -> BackendCircuitSnapshot:
        samples = tuple(self._samples[definition.backend_id])
        successes = sum(1 for sample in samples if sample.ok)
        failures = len(samples) - successes
        failure_ratio = failures / len(samples) if samples else 0.0
        latencies = sorted(sample.latency_milliseconds for sample in samples)
        mean_latency = sum(latencies) / len(latencies) if latencies else 0.0
        p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1)
        p95_latency = latencies[p95_index] if latencies else 0.0
        consecutive_successes = count_tail(samples, expected=True)
        consecutive_failures = count_tail(samples, expected=False)
        opened_at = self._opened_at.get(definition.backend_id)
        retry_at = None if opened_at is None else opened_at + self.policy.open_seconds
        if not definition.enabled:
            phase = CircuitPhase.DISABLED
            reason = "backend definition is disabled"
        elif opened_at is not None and retry_at is not None and at < retry_at:
            phase = CircuitPhase.OPEN
            reason = "backend circuit cooling down"
        elif opened_at is not None:
            phase = CircuitPhase.HALF_OPEN
            reason = "backend circuit is testing recovery"
            if consecutive_successes >= self.policy.half_open_successes:
                self._opened_at.pop(definition.backend_id, None)
                self._half_open_successes[definition.backend_id] = 0
                phase = CircuitPhase.CLOSED
                opened_at = None
                retry_at = None
                reason = "backend half-open probes recovered"
        elif len(samples) >= self.policy.minimum_samples and failure_ratio >= self.policy.open_failure_ratio:
            opened_at = at
            retry_at = at + self.policy.open_seconds
            self._opened_at[definition.backend_id] = at
            phase = CircuitPhase.OPEN
            reason = "backend probe failure ratio opened circuit"
        else:
            phase = CircuitPhase.CLOSED
            reason = "backend circuit is healthy"
            if len(samples) >= self.policy.minimum_samples and failure_ratio >= self.policy.degraded_failure_ratio:
                reason = "backend circuit is closed with degraded probe ratio"
            elif p95_latency >= self.policy.slow_latency_milliseconds:
                reason = "backend circuit is closed with slow probe latency"
        return BackendCircuitSnapshot(
            backend_id=definition.backend_id,
            phase=phase,
            sample_count=len(samples),
            success_count=successes,
            failure_count=failures,
            failure_ratio=failure_ratio,
            mean_latency_milliseconds=mean_latency,
            percentile_95_latency_milliseconds=p95_latency,
            consecutive_successes=consecutive_successes,
            consecutive_failures=consecutive_failures,
            opened_at=opened_at,
            retry_at=retry_at,
            generation=self._generation.get(definition.backend_id, ""),
            reason=reason,
            sample_ids=tuple(sample.sample_id for sample in samples),
        )

    def _persist(
        self,
        definition: BackendDefinition,
        current: BackendHealthRecord,
        sample: BackendProbeSample,
        circuit: BackendCircuitSnapshot,
    ) -> BackendHealthRecord:
        if circuit.phase is CircuitPhase.DISABLED:
            status = BackendHealthStatus.DISABLED
        elif circuit.phase is CircuitPhase.OPEN:
            status = BackendHealthStatus.QUARANTINED
        elif not sample.ok:
            status = BackendHealthStatus.UNAVAILABLE
        elif circuit.failure_ratio >= self.policy.degraded_failure_ratio:
            status = BackendHealthStatus.DEGRADED
        elif circuit.percentile_95_latency_milliseconds >= self.policy.slow_latency_milliseconds:
            status = BackendHealthStatus.DEGRADED
        else:
            status = BackendHealthStatus.HEALTHY
        value = replace(
            current,
            status=status,
            revision=current.revision + 1,
            consecutive_failures=circuit.consecutive_failures,
            success_count=current.success_count + int(sample.ok),
            failure_count=current.failure_count + int(not sample.ok),
            latency_milliseconds=circuit.mean_latency_milliseconds,
            reason=circuit.reason,
            last_checked_at=sample.checked_at,
            last_success_at=(sample.checked_at if sample.ok else current.last_success_at),
            last_failure_at=(sample.checked_at if not sample.ok else current.last_failure_at),
            quarantine_until=circuit.retry_at if circuit.phase is CircuitPhase.OPEN else None,
            metadata={
                **dict(current.metadata),
                "circuit": circuit.to_dict(),
                "last_probe": sample.to_dict(),
            },
        )
        self.store.put_health(value)
        return value

    @staticmethod
    def _event(
        definition: BackendDefinition,
        before: BackendHealthRecord,
        after: BackendHealthRecord,
        sample: BackendProbeSample,
        circuit: BackendCircuitSnapshot,
    ) -> BackendControlEvent:
        payload = {
            "backend_id": definition.backend_id,
            "before_status": before.status.value,
            "after_status": after.status.value,
            "sample": sample.to_dict(),
            "circuit": circuit.to_dict(),
            "provider_route_changed": False,
            "backend_route_eligible": after.status in {
                BackendHealthStatus.HEALTHY,
                BackendHealthStatus.DEGRADED,
            },
        }
        return BackendControlEvent(
            event_id=new_backend_id("backend_event"),
            event_type="backend.health.transitioned",
            run_id="backend-supervisor",
            task_id=definition.backend_id,
            node_id=None,
            lease_id=None,
            envelope_id=None,
            backend_id=definition.backend_id,
            provider_route_id=None,
            causation_id=sample.sample_id,
            correlation_id=f"backend-health:{definition.backend_id}",
            created_at=sample.checked_at,
            payload=payload,
            payload_digest=checksum(payload),
        )


def count_tail(samples: Sequence[BackendProbeSample], *, expected: bool) -> int:
    count = 0
    for sample in reversed(samples):
        if sample.ok is not expected:
            break
        count += 1
    return count


__all__ = [
    "BackendCircuitPolicy",
    "BackendCircuitSnapshot",
    "BackendHealthSupervisor",
    "BackendProbeBatch",
    "BackendProbeSample",
    "CircuitPhase",
]
