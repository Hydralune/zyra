from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from zyra_core import EventRecord, EventType

from .cancellation import CancellationPorts, WorkerCancellationRuntime
from .capabilities import (
    AgentToolConstraints,
    BackendCapability,
    CapabilityAttestor,
    NativeWorkerCapabilities,
    WorkerCapabilityComposer,
    requirement_from_subagent_dispatch,
)
from .heartbeat import HeartbeatPolicy, HeartbeatSweepResult, WorkerHeartbeatRuntime
from .inbox import WorkerInboxRuntime
from .leases import WorkerLeaseManager
from .lifecycle import WorkerLifecycleRuntime
from .models import (
    AttemptState,
    CancellationRequest,
    CapabilityRequirement,
    HealthAssessment,
    HealthDisposition,
    LeaseAcquisition,
    LeaseState,
    PoolJournalRecord,
    PoolStateSnapshot,
    ResourceVector,
    TaskAttempt,
    WorkerCapabilityManifest,
    WorkerHealthStatus,
    WorkerInstance,
    WorkerLocation,
    WorkerTelemetry,
    stable_digest,
    utc_iso,
)
from .store import WorkerPoolStore
from .recovery import WorkerTakeoverRuntime


@dataclass(frozen=True, slots=True)
class LocalWorkerRegistration:
    worker: WorkerInstance
    manifest: WorkerCapabilityManifest
    process_identity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker.to_dict(),
            "manifest": self.manifest.to_dict(),
            "process_identity": self.process_identity,
        }


@dataclass(frozen=True, slots=True)
class StartupRecoveryReport:
    expired_lease_ids: tuple[str, ...]
    recovered_inbox_ids: tuple[str, ...]
    health: tuple[HealthAssessment, ...]
    store_integrity: Mapping[str, Any]
    captured_at: str = field(default_factory=utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "expired_lease_ids": list(self.expired_lease_ids),
            "recovered_inbox_ids": list(self.recovered_inbox_ids),
            "health": [item.to_dict() for item in self.health],
            "store_integrity": dict(self.store_integrity),
            "captured_at": self.captured_at,
        }


class WorkerPoolEventProjector:
    """Projects canonical physical journal records onto the existing event spine."""

    EVENT_BY_OPERATION: Mapping[str, EventType] = {
        "worker_registered": EventType.WORKER_HEALTH,
        "worker_starting": EventType.WORKER_HEALTH,
        "worker_idle": EventType.WORKER_HEALTH,
        "worker_busy": EventType.WORKER_HEALTH,
        "worker_draining": EventType.WORKER_HEALTH,
        "worker_stopped": EventType.WORKER_HEALTH,
        "worker_lost": EventType.WORKER_HEALTH,
        "heartbeat_observed": EventType.WORKER_HEALTH,
        "lease_acquired": EventType.RESOURCE_DECISION,
        "lease_renewed": EventType.RESOURCE_DECISION,
        "lease_expired": EventType.NODE_FAILED,
        "lease_cancelled": EventType.CONTROL_COMMAND,
        "lease_fenced": EventType.CONSTRAINT_CHECK,
        "execution_receipt_committed": EventType.ARTIFACT_WRITTEN,
        "inbox_enqueued": EventType.AGENT_MESSAGE,
        "inbox_claimed": EventType.AGENT_MESSAGE,
        "wakeup_dispatched": EventType.CONTROL_COMMAND,
        "cancellation_completed": EventType.CONTROL_COMMAND,
    }

    def project(self, record: PoolJournalRecord) -> EventRecord | None:
        if not record.run_id or not record.task_id:
            if record.aggregate_type not in {"worker", "worker_lease"}:
                return None
        run_id = record.run_id or str(record.payload.get("run_id") or "worker-pool")
        task_id = record.task_id or str(record.payload.get("task_id") or record.aggregate_id)
        event_type = self.EVENT_BY_OPERATION.get(record.operation, EventType.SYSTEM_NOTICE)
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            event_type=event_type,
            payload={
                "worker_pool_operation": record.operation,
                "aggregate_type": record.aggregate_type,
                "aggregate_id": record.aggregate_id,
                "journal_sequence": record.sequence,
                "physical_state_owner": "WorkerPoolStore",
                "causation_id": record.causation_id,
                "correlation_id": record.correlation_id,
                **dict(record.payload),
            },
        )

    def project_after(self, store: WorkerPoolStore, sequence: int = 0) -> tuple[EventRecord, ...]:
        events = [self.project(item) for item in store.journal(after_sequence=sequence)]
        return tuple(item for item in events if item is not None)


class WorkerPoolFoundationRuntime:
    """Composition root for 07A physical worker and resource-pool ownership."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        attestation_secret: bytes,
        cancellation_ports: CancellationPorts | None = None,
        heartbeat_policy: HeartbeatPolicy | None = None,
        default_lease_ttl_seconds: float = 30.0,
        recovery_sink: Callable[[HealthAssessment], Any] | None = None,
    ) -> None:
        self.store = WorkerPoolStore(store_path)
        self.store.initialize()
        self.attestor = CapabilityAttestor(attestation_secret)
        self.composer = WorkerCapabilityComposer()
        self.lifecycle = WorkerLifecycleRuntime(self.store)
        self.leases = WorkerLeaseManager(
            self.store,
            self.lifecycle,
            default_ttl_seconds=default_lease_ttl_seconds,
        )
        self.heartbeats = WorkerHeartbeatRuntime(
            self.store,
            self.lifecycle,
            self.leases,
            policy=heartbeat_policy,
            recovery_sink=recovery_sink,
        )
        self.inbox = WorkerInboxRuntime(self.store, self.lifecycle)
        self.takeover = WorkerTakeoverRuntime(self.store, self.leases)
        self.cancellation = WorkerCancellationRuntime(
            self.store,
            self.leases,
            self.inbox,
            ports=cancellation_ports,
        )
        self.events = WorkerPoolEventProjector()

    def register_local_worker(
        self,
        *,
        worker_id: str,
        worker_kind: str,
        backend: BackendCapability,
        capabilities: Sequence[str],
        tool_ids: Sequence[str],
        resources: ResourceVector,
        agent_constraints: AgentToolConstraints | None = None,
        owner_session_id: str = "",
        replace_generation: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> LocalWorkerRegistration:
        composition = self.composer.compose(
            NativeWorkerCapabilities(
                worker_id=worker_id,
                worker_kind=worker_kind,
                location=WorkerLocation.LOCAL,
                capabilities=tuple(capabilities),
                tool_ids=tuple(tool_ids),
                resources=resources,
                labels={"transport": "in-process-port", "host_pid": str(os.getpid())},
            ),
            backends=(backend,),
            agent_constraints=agent_constraints,
        )
        manifest = composition.manifest
        process_identity = f"local-pid-{os.getpid()}"
        challenge = self.attestor.challenge()
        attestation = self.attestor.issue(
            worker_id=worker_id,
            process_identity=process_identity,
            manifest_digest=manifest.digest,
            challenge_nonce=challenge,
            endpoint=f"local://pid/{os.getpid()}/{worker_id}",
            protocol_version=manifest.protocol_version,
            metadata={"location": "local", "pid": os.getpid()},
        )
        worker = self.lifecycle.register(
            manifest,
            attestation,
            attestor=self.attestor,
            expected_challenge=challenge,
            backend_id=backend.backend_id,
            owner_session_id=owner_session_id,
            metadata={**dict(metadata or {}), "local_worker": True},
            replace_generation=replace_generation,
        )
        worker = self.lifecycle.start(worker.worker_id)
        return LocalWorkerRegistration(
            worker=worker,
            manifest=manifest,
            process_identity=process_identity,
        )

    def heartbeat_local_worker(
        self,
        worker_id: str,
        *,
        sequence: int,
        observed: ResourceVector | None = None,
        queue_depth: int = 0,
        process_uptime_ms: int = 0,
        load_average: float = 0.0,
    ) -> None:
        from .models import WorkerHeartbeat

        worker = self.store.require_worker(worker_id)
        manifest = self.store.latest_manifest(worker_id)
        if manifest is None:
            raise RuntimeError("local worker has no manifest")
        leases = self.store.list_leases(worker_id=worker_id, states=(LeaseState.ACTIVE, LeaseState.DRAINING))
        allocated = self.leases.allocated_resources(worker_id)
        telemetry = WorkerTelemetry(
            worker_id=worker_id,
            sequence=sequence,
            capacity=manifest.resource_capacity,
            allocated=allocated,
            observed=observed or allocated,
            active_attempt_ids=tuple(lease.attempt_id for lease in leases),
            queue_depth=queue_depth,
            process_uptime_ms=process_uptime_ms,
            load_average=load_average,
            metadata={"location": "local"},
        )
        heartbeat = WorkerHeartbeat(
            worker_id=worker_id,
            worker_generation=worker.generation,
            sequence=sequence,
            manifest_digest=manifest.digest,
            active_lease_ids=tuple(lease.lease_id for lease in leases),
            telemetry=telemetry,
            process_identity=worker.process_identity,
        )
        self.heartbeats.observe(heartbeat)

    def acquire_task(
        self,
        *,
        task_id: str,
        run_id: str,
        owner_session_id: str,
        requirement: CapabilityRequirement,
        attempt_number: int | None = None,
        preferred_worker_ids: Sequence[str] = (),
        excluded_worker_ids: Sequence[str] = (),
        ttl_seconds: float | None = None,
        idempotency_key: str = "",
        recovery_reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> LeaseAcquisition:
        return self.leases.acquire(
            task_id=task_id,
            run_id=run_id,
            owner_session_id=owner_session_id,
            requirement=requirement,
            attempt_number=attempt_number,
            preferred_worker_ids=preferred_worker_ids,
            excluded_worker_ids=excluded_worker_ids,
            ttl_seconds=ttl_seconds,
            idempotency_key=idempotency_key,
            recovery_reason=recovery_reason,
            metadata=metadata,
        )

    def acquire_subagent(
        self,
        request: Any,
        *,
        owner_session_id: str,
        backend_kinds: Sequence[str] = (),
        locations: Sequence[WorkerLocation] = (),
        resources: ResourceVector | None = None,
        preferred_worker_ids: Sequence[str] = (),
    ) -> LeaseAcquisition:
        requirement = requirement_from_subagent_dispatch(
            request,
            backend_kinds=backend_kinds,
            locations=locations,
            resources=resources,
        )
        return self.acquire_task(
            task_id=str(getattr(request, "task_id")),
            run_id=str(getattr(request, "run_id")),
            owner_session_id=owner_session_id,
            requirement=requirement,
            attempt_number=int(getattr(request, "attempt", 1)),
            preferred_worker_ids=preferred_worker_ids,
            idempotency_key=str(getattr(request, "idempotency_key", "")),
            metadata={
                "dispatch_id": str(getattr(request, "dispatch_id", "")),
                "parent_task_id": str(getattr(request, "parent_task_id", "")),
                "agent_definition_id": str(getattr(request, "agent_definition_id", "")),
                "logical_task_not_duplicated": True,
            },
        )

    def recover_startup(self) -> StartupRecoveryReport:
        expired = self.leases.sweep_expired()
        recovered = self.inbox.recover_expired_claims()
        assessments: list[HealthAssessment] = []
        for worker in self.store.list_workers():
            try:
                assessments.append(self.heartbeats.assess(worker.worker_id))
            except Exception:
                continue
        return StartupRecoveryReport(
            expired_lease_ids=tuple(item.lease_id for item in expired),
            recovered_inbox_ids=tuple(item.envelope_id for item in recovered),
            health=tuple(assessments),
            store_integrity=self.store.integrity_report(),
        )

    def snapshot(self) -> PoolStateSnapshot:
        workers = self.store.list_workers()
        health: list[HealthAssessment] = []
        for worker in workers:
            try:
                health.append(self.heartbeats.assess(worker.worker_id))
            except Exception:
                health.append(
                    HealthAssessment(
                        worker_id=worker.worker_id,
                        status=WorkerHealthStatus.UNRECOVERABLE,
                        disposition=HealthDisposition.REPLAN,
                        reason="worker health projection failed",
                        recoverable=False,
                    )
                )
        return PoolStateSnapshot(
            workers=workers,
            manifests=self.store.list_manifests(),
            active_leases=self.store.list_leases(states=(LeaseState.ACTIVE, LeaseState.DRAINING)),
            active_attempts=self.store.list_attempts(
                states=(
                    AttemptState.PENDING,
                    AttemptState.LEASED,
                    AttemptState.RUNNING,
                    AttemptState.LOST,
                )
            ),
            health=tuple(health),
            revision=self.store.revision,
        )

    def api_projection(self) -> Mapping[str, Any]:
        snapshot = self.snapshot()
        health_by_worker = {item.worker_id: item for item in snapshot.health}
        active_by_worker: dict[str, list[Mapping[str, Any]]] = {}
        for lease in snapshot.active_leases:
            active_by_worker.setdefault(lease.worker_id, []).append(lease.to_dict())
        return {
            "revision": snapshot.revision,
            "captured_at": snapshot.captured_at,
            "workers": [
                {
                    **worker.to_dict(),
                    "manifest": next(
                        (item.to_dict() for item in snapshot.manifests if item.worker_id == worker.worker_id),
                        None,
                    ),
                    "health": health_by_worker.get(worker.worker_id).to_dict()
                    if worker.worker_id in health_by_worker
                    else None,
                    "active_leases": active_by_worker.get(worker.worker_id, []),
                    "telemetry": (
                        self.store.latest_telemetry(worker.worker_id).to_dict()
                        if self.store.latest_telemetry(worker.worker_id)
                        else None
                    ),
                }
                for worker in snapshot.workers
            ],
            "active_leases": [item.to_dict() for item in snapshot.active_leases],
            "active_attempts": [item.to_dict() for item in snapshot.active_attempts],
            "custody": {
                "logical_task": "typescript.AgentTaskRuntime",
                "physical_attempt": "WorkerLeaseManager",
                "worker_lease": "WorkerPoolStore",
                "heartbeat_telemetry": "WorkerHeartbeatRuntime",
                "execution_receipt": "WorkerPoolStore",
            },
            "integrity": self.store.integrity_report(),
        }


class SubagentWorkerLeaseAdapter:
    """Narrow 03D-to-07A adapter; it never persists the logical AgentTask body."""

    def __init__(self, runtime: WorkerPoolFoundationRuntime) -> None:
        self.runtime = runtime

    def acquire(
        self,
        request: Any,
        *,
        owner_session_id: str,
        preferred_worker_ids: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        acquisition = self.runtime.acquire_subagent(
            request,
            owner_session_id=owner_session_id,
            preferred_worker_ids=preferred_worker_ids,
        )
        return {
            "task_id": acquisition.attempt.task_id,
            "attempt": acquisition.attempt.attempt_number,
            "attempt_id": acquisition.attempt.attempt_id,
            "lease_id": acquisition.lease.lease_id,
            "worker_id": acquisition.worker.worker_id,
            "backend_id": acquisition.lease.backend_id,
            "fence_epoch": acquisition.lease.fence_epoch,
            "manifest_digest": acquisition.manifest.digest,
            "logical_task_not_duplicated": True,
        }

    def cancel(self, *, task_id: str, run_id: str, reason: str, actor_id: str) -> Mapping[str, Any]:
        receipt = self.runtime.cancellation.cancel(
            CancellationRequest(
                task_id=task_id,
                run_id=run_id,
                reason=reason,
                actor_id=actor_id,
            )
        )
        return receipt.to_dict()
