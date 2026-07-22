from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import (
    CorrelationRefs,
    FaultKind,
    ObservationCategory,
    StructuredObservation,
    runtime_id,
)
from .observers import ManagedObserver, OH_MY_PI_REVISION, ZYRA_REVISION, _descriptor


class HeartbeatPhase(StrEnum):
    NEW = "new"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    LOST = "lost"
    STOPPED = "stopped"


@dataclass(slots=True)
class HeartbeatBinding:
    refs: CorrelationRefs
    generation: int
    interval_ms: int
    grace_intervals: int
    attached_ms: int
    last_heartbeat_ms: int
    sequence: int = 0
    phase: HeartbeatPhase = HeartbeatPhase.NEW
    emitted_lost_generation: int = -1
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def lost_after_ms(self) -> int:
        return self.interval_ms * self.grace_intervals

    def age(self, at_ms: int) -> int:
        return max(0, at_ms - self.last_heartbeat_ms)


class WorkerHeartbeatObserver(ManagedObserver):
    """Generation-fenced worker heartbeat source.

    A late heartbeat from a prior process generation cannot revive a current
    worker binding. The observer emits only the transition to lost; healthy
    heartbeats remain source state and do not pollute the fault journal.
    """

    def __init__(self, *, monotonic_ms: Callable[[], int] | None = None) -> None:
        super().__init__(
            _descriptor(
                "worker-heartbeat",
                "Generation-fenced worker heartbeat observer",
                ObservationCategory.WORKER,
                (FaultKind.WORKER_UNAVAILABLE,),
                observation_point="worker.heartbeat.sweep",
                source_repo="zyra",
                source_revision=ZYRA_REVISION,
                metadata={"generation_fence": True, "lease_owner": "M1-S05B.WorkerPoolStore"},
            )
        )
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._bindings: dict[str, HeartbeatBinding] = {}
        self._revision = 0

    def bind(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        interval_ms: int,
        grace_intervals: int = 3,
        metadata: Mapping[str, Any] | None = None,
    ) -> HeartbeatBinding:
        if not refs.worker_id:
            raise ValueError("worker heartbeat binding requires worker_id")
        if generation < 0 or interval_ms <= 0 or grace_intervals < 1:
            raise ValueError("invalid heartbeat generation or interval policy")
        now = self.monotonic_ms()
        with self._lock:
            current = self._bindings.get(refs.worker_id)
            if current is not None and generation < current.generation:
                raise RuntimeError("stale worker generation cannot replace heartbeat binding")
            binding = HeartbeatBinding(
                refs=refs,
                generation=generation,
                interval_ms=interval_ms,
                grace_intervals=grace_intervals,
                attached_ms=now,
                last_heartbeat_ms=now,
                metadata=dict(metadata or {}),
            )
            self._bindings[refs.worker_id] = binding
            return binding

    def heartbeat(
        self,
        worker_id: str,
        *,
        generation: int,
        sequence: int,
        at_ms: int | None = None,
    ) -> bool:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        with self._lock:
            binding = self._bindings.get(worker_id)
            if binding is None or generation != binding.generation:
                return False
            if sequence <= binding.sequence:
                return False
            binding.sequence = sequence
            binding.last_heartbeat_ms = now
            binding.phase = HeartbeatPhase.HEALTHY
            return True

    def stop_binding(self, worker_id: str, *, generation: int) -> bool:
        with self._lock:
            binding = self._bindings.get(worker_id)
            if binding is None or binding.generation != generation:
                return False
            binding.phase = HeartbeatPhase.STOPPED
            return True

    def sweep(self, *, at_ms: int | None = None) -> tuple[StructuredObservation, ...]:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        observations: list[StructuredObservation] = []
        with self._lock:
            bindings = tuple(self._bindings.values())
        for binding in bindings:
            if binding.phase is HeartbeatPhase.STOPPED:
                continue
            age = binding.age(now)
            if age < binding.lost_after_ms:
                if age >= binding.interval_ms and binding.phase is HeartbeatPhase.HEALTHY:
                    binding.phase = HeartbeatPhase.DEGRADED
                continue
            if binding.emitted_lost_generation == binding.generation:
                continue
            self._revision += 1
            observation = StructuredObservation(
                category=ObservationCategory.WORKER,
                code="heartbeat_lost",
                refs=binding.refs.with_observation(runtime_id("heartbeat-observation"), revision=self._revision),
                provenance=self.provenance(),
                summary=f"Worker {binding.refs.worker_id} missed its generation-fenced heartbeat deadline.",
                status="lost",
                error_type="WorkerHeartbeatLost",
                retryable_hint=True,
                terminal_hint=True,
                elapsed_ms=age,
                deadline_ms=binding.lost_after_ms,
                details={
                    **binding.metadata,
                    "generation": binding.generation,
                    "heartbeat_sequence": binding.sequence,
                    "interval_ms": binding.interval_ms,
                    "grace_intervals": binding.grace_intervals,
                    "attached_ms": binding.attached_ms,
                    "last_heartbeat_ms": binding.last_heartbeat_ms,
                },
            )
            if self.submit(observation):
                binding.phase = HeartbeatPhase.LOST
                binding.emitted_lost_generation = binding.generation
                observations.append(observation)
        return tuple(observations)

    def snapshot(self) -> Mapping[str, Any]:
        value = dict(super().snapshot())
        with self._lock:
            value["bindings"] = {
                worker_id: {
                    "generation": item.generation,
                    "sequence": item.sequence,
                    "phase": item.phase.value,
                    "interval_ms": item.interval_ms,
                    "grace_intervals": item.grace_intervals,
                    "last_heartbeat_ms": item.last_heartbeat_ms,
                    "emitted_lost_generation": item.emitted_lost_generation,
                }
                for worker_id, item in sorted(self._bindings.items())
            }
        return value


class McpTransportPhase(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    BACKOFF = "backoff"
    BREAKER_OPEN = "breaker_open"
    STOPPED = "stopped"


@dataclass(slots=True)
class McpTransportBinding:
    refs: CorrelationRefs
    generation: int
    max_reconnect_attempts: int
    base_backoff_ms: int
    phase: McpTransportPhase = McpTransportPhase.CONNECTED
    disconnect_count: int = 0
    reconnect_attempt: int = 0
    disconnected_at_ms: int | None = None
    next_retry_at_ms: int | None = None
    emitted_generation: int = -1
    last_reason_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def next_delay_ms(self) -> int:
        exponent = max(0, self.reconnect_attempt - 1)
        return min(self.base_backoff_ms * (2**exponent), 60_000)


class McpTransportObserver(ManagedObserver):
    """MCP disconnect/reconnect breaker adapted from OMP transport lifecycle."""

    def __init__(self, *, monotonic_ms: Callable[[], int] | None = None) -> None:
        super().__init__(
            _descriptor(
                "mcp-transport",
                "MCP reconnect and breaker observer",
                ObservationCategory.MCP,
                (FaultKind.MCP_DISCONNECTED,),
                observation_point="mcp.transport.lifecycle",
                source_repo="oh-my-pi",
                source_revision=OH_MY_PI_REVISION,
                metadata={"backoff": "bounded-exponential", "recovery_owner": "M1-S07C"},
            )
        )
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._bindings: dict[str, McpTransportBinding] = {}
        self._revision = 0

    def connected(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        max_reconnect_attempts: int = 4,
        base_backoff_ms: int = 250,
        metadata: Mapping[str, Any] | None = None,
    ) -> McpTransportBinding:
        if not refs.mcp_server_id:
            raise ValueError("MCP transport binding requires mcp_server_id")
        if generation < 0 or max_reconnect_attempts < 1 or base_backoff_ms < 1:
            raise ValueError("invalid MCP reconnect policy")
        with self._lock:
            current = self._bindings.get(refs.mcp_server_id)
            if current is not None and generation < current.generation:
                raise RuntimeError("stale MCP generation cannot replace active binding")
            binding = McpTransportBinding(
                refs=refs,
                generation=generation,
                max_reconnect_attempts=max_reconnect_attempts,
                base_backoff_ms=base_backoff_ms,
                metadata=dict(metadata or {}),
            )
            self._bindings[refs.mcp_server_id] = binding
            return binding

    def disconnected(
        self,
        server_id: str,
        *,
        generation: int,
        reason_code: str,
        at_ms: int | None = None,
    ) -> StructuredObservation | None:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        with self._lock:
            binding = self._bindings.get(server_id)
            if binding is None or binding.generation != generation:
                return None
            if binding.phase is McpTransportPhase.STOPPED:
                return None
            binding.phase = McpTransportPhase.DISCONNECTED
            binding.disconnect_count += 1
            binding.disconnected_at_ms = now
            binding.last_reason_code = reason_code
            if binding.emitted_generation == generation:
                return None
            self._revision += 1
            observation = StructuredObservation(
                category=ObservationCategory.MCP,
                code="transport_closed",
                refs=binding.refs.with_observation(runtime_id("mcp-observation"), revision=self._revision),
                provenance=self.provenance(),
                summary=f"MCP server {server_id} transport closed unexpectedly.",
                status="disconnected",
                error_type="McpTransportClosed",
                retryable_hint=True,
                terminal_hint=True,
                details={
                    **binding.metadata,
                    "generation": generation,
                    "disconnect_count": binding.disconnect_count,
                    "reason_code": reason_code,
                    "disconnected_at_ms": now,
                },
            )
        if self.submit(observation):
            binding.emitted_generation = generation
            return observation
        return None

    def reconnect_failed(self, server_id: str, *, generation: int, at_ms: int | None = None) -> McpTransportBinding:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        with self._lock:
            binding = self._bindings[server_id]
            if binding.generation != generation:
                raise RuntimeError("stale MCP reconnect result")
            binding.reconnect_attempt += 1
            if binding.reconnect_attempt >= binding.max_reconnect_attempts:
                binding.phase = McpTransportPhase.BREAKER_OPEN
                binding.next_retry_at_ms = None
            else:
                binding.phase = McpTransportPhase.BACKOFF
                binding.next_retry_at_ms = now + binding.next_delay_ms()
            return binding

    def reconnect_succeeded(self, server_id: str, *, generation: int) -> McpTransportBinding:
        with self._lock:
            binding = self._bindings[server_id]
            if binding.generation != generation:
                raise RuntimeError("stale MCP reconnect success")
            binding.phase = McpTransportPhase.CONNECTED
            binding.reconnect_attempt = 0
            binding.next_retry_at_ms = None
            binding.emitted_generation = -1
            return binding

    def due_reconnects(self, *, at_ms: int | None = None) -> tuple[str, ...]:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        with self._lock:
            return tuple(
                server_id
                for server_id, item in sorted(self._bindings.items())
                if item.phase is McpTransportPhase.BACKOFF
                and item.next_retry_at_ms is not None
                and item.next_retry_at_ms <= now
            )

    def stop_transport(self, server_id: str, *, generation: int) -> bool:
        with self._lock:
            binding = self._bindings.get(server_id)
            if binding is None or binding.generation != generation:
                return False
            binding.phase = McpTransportPhase.STOPPED
            return True

    def snapshot(self) -> Mapping[str, Any]:
        value = dict(super().snapshot())
        with self._lock:
            value["transports"] = {
                server_id: {
                    "generation": item.generation,
                    "phase": item.phase.value,
                    "disconnect_count": item.disconnect_count,
                    "reconnect_attempt": item.reconnect_attempt,
                    "max_reconnect_attempts": item.max_reconnect_attempts,
                    "next_retry_at_ms": item.next_retry_at_ms,
                    "last_reason_code": item.last_reason_code,
                }
                for server_id, item in sorted(self._bindings.items())
            }
        return value


def supervision_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.watchdog-supervision/v1",
        "worker_heartbeat": {
            "owner": "WorkerHeartbeatObserver",
            "generation_fenced": True,
            "canonical_lease_owner": "M1-S05B.WorkerPoolStore",
        },
        "mcp_transport": {
            "owner": "McpTransportObserver",
            "source": "oh-my-pi supplementary",
            "bounded_exponential_backoff": True,
            "recovery_plan_owner": "M1-S07C",
        },
        "requirement_changed_is_fault": False,
    }


__all__ = [
    "HeartbeatBinding",
    "HeartbeatPhase",
    "McpTransportBinding",
    "McpTransportObserver",
    "McpTransportPhase",
    "WorkerHeartbeatObserver",
    "supervision_contract",
]
