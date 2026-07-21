from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .capabilities import CapabilityAttestor
from .errors import WorkerPoolError, WorkerPoolErrorCode
from .models import (
    CapabilityAttestation,
    WorkerCapabilityManifest,
    WorkerInstance,
    WorkerLifecycleState,
    WorkerLocation,
    utc_iso,
)
from .store import WorkerPoolStore


ALLOWED_TRANSITIONS: Mapping[WorkerLifecycleState, frozenset[WorkerLifecycleState]] = {
    WorkerLifecycleState.REGISTERED: frozenset(
        {WorkerLifecycleState.STARTING, WorkerLifecycleState.STOPPING, WorkerLifecycleState.FAILED}
    ),
    WorkerLifecycleState.STARTING: frozenset(
        {WorkerLifecycleState.IDLE, WorkerLifecycleState.STOPPING, WorkerLifecycleState.LOST, WorkerLifecycleState.FAILED}
    ),
    WorkerLifecycleState.IDLE: frozenset(
        {
            WorkerLifecycleState.BUSY,
            WorkerLifecycleState.PARKED,
            WorkerLifecycleState.DRAINING,
            WorkerLifecycleState.STOPPING,
            WorkerLifecycleState.LOST,
            WorkerLifecycleState.FAILED,
        }
    ),
    WorkerLifecycleState.BUSY: frozenset(
        {
            WorkerLifecycleState.IDLE,
            WorkerLifecycleState.DRAINING,
            WorkerLifecycleState.STOPPING,
            WorkerLifecycleState.LOST,
            WorkerLifecycleState.FAILED,
        }
    ),
    WorkerLifecycleState.PARKED: frozenset(
        {
            WorkerLifecycleState.IDLE,
            WorkerLifecycleState.DRAINING,
            WorkerLifecycleState.STOPPING,
            WorkerLifecycleState.LOST,
            WorkerLifecycleState.FAILED,
        }
    ),
    WorkerLifecycleState.DRAINING: frozenset(
        {
            WorkerLifecycleState.IDLE,
            WorkerLifecycleState.STOPPING,
            WorkerLifecycleState.LOST,
            WorkerLifecycleState.FAILED,
        }
    ),
    WorkerLifecycleState.STOPPING: frozenset(
        {WorkerLifecycleState.STOPPED, WorkerLifecycleState.LOST, WorkerLifecycleState.FAILED}
    ),
    WorkerLifecycleState.STOPPED: frozenset({WorkerLifecycleState.STARTING}),
    WorkerLifecycleState.LOST: frozenset(
        {WorkerLifecycleState.STARTING, WorkerLifecycleState.STOPPING, WorkerLifecycleState.FAILED}
    ),
    WorkerLifecycleState.FAILED: frozenset(
        {WorkerLifecycleState.STARTING, WorkerLifecycleState.STOPPING}
    ),
}


class WorkerLifecycleRuntime:
    def __init__(self, store: WorkerPoolStore) -> None:
        self.store = store

    def register(
        self,
        manifest: WorkerCapabilityManifest,
        attestation: CapabilityAttestation,
        *,
        attestor: CapabilityAttestor,
        expected_challenge: str,
        backend_id: str,
        owner_session_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        replace_generation: bool = False,
    ) -> WorkerInstance:
        attestor.verify(
            attestation,
            manifest,
            expected_challenge=expected_challenge,
            expected_endpoint=attestation.endpoint,
        )
        existing = self.store.get_worker(manifest.worker_id)
        generation = 1 if existing is None else existing.generation + 1
        worker = WorkerInstance(
            worker_id=manifest.worker_id,
            worker_kind=manifest.worker_kind,
            location=manifest.location,
            backend_id=backend_id,
            manifest_digest=manifest.digest,
            attestation_id=attestation.attestation_id,
            endpoint=attestation.endpoint,
            process_identity=attestation.process_identity,
            owner_session_id=owner_session_id,
            generation=generation,
            version=1 if existing is None else existing.version + 1,
            metadata={
                **dict(metadata or {}),
                "physical_state_owner": "WorkerLifecycleRuntime",
                "manifest_owner": "WorkerCapabilityComposer",
            },
        )
        with self.store.transaction() as connection:
            persisted_manifest = self.store.register_manifest(manifest, connection=connection)
            if persisted_manifest.digest != manifest.digest:
                worker = replace(worker, manifest_digest=persisted_manifest.digest)
            self.store.save_attestation(attestation, connection=connection)
            if existing is None:
                return self.store.insert_worker(worker, connection=connection)
            if not replace_generation:
                if existing.process_identity == worker.process_identity:
                    return existing
                raise WorkerPoolError(
                    WorkerPoolErrorCode.WORKER_ALREADY_EXISTS,
                    "worker id is already registered; replacement requires a new generation",
                    operation="register_worker",
                    worker_id=manifest.worker_id,
                )
            return self.store.upsert_worker_generation(worker, connection=connection)

    def start(self, worker_id: str) -> WorkerInstance:
        current = self.store.require_worker(worker_id)
        if current.state is WorkerLifecycleState.IDLE:
            return current
        if current.state is not WorkerLifecycleState.STARTING:
            current = self.transition(worker_id, WorkerLifecycleState.STARTING, reason="worker start requested")
        return self.transition(
            worker_id,
            WorkerLifecycleState.IDLE,
            reason="worker startup completed",
            changes={
                "started_at": utc_iso(),
                "stopped_at": "",
                "failure_code": "",
                "failure_reason": "",
            },
        )

    def park(self, worker_id: str, *, reason: str = "worker parked") -> WorkerInstance:
        worker = self.store.require_worker(worker_id)
        if self._active_lease_count(worker_id):
            raise WorkerPoolError(
                WorkerPoolErrorCode.INVALID_WORKER_TRANSITION,
                "worker with active leases cannot be parked",
                operation="park_worker",
                worker_id=worker_id,
            )
        return self.transition(worker_id, WorkerLifecycleState.PARKED, reason=reason)

    def wake(self, worker_id: str, *, reason: str = "worker wakeup dispatched") -> WorkerInstance:
        worker = self.store.require_worker(worker_id)
        if worker.state in {WorkerLifecycleState.IDLE, WorkerLifecycleState.BUSY}:
            return worker
        if worker.state is not WorkerLifecycleState.PARKED:
            raise WorkerPoolError(
                WorkerPoolErrorCode.INVALID_WORKER_TRANSITION,
                "only a parked worker can be woken",
                operation="wake_worker",
                worker_id=worker_id,
            )
        return self.transition(worker_id, WorkerLifecycleState.IDLE, reason=reason)

    def begin_drain(self, worker_id: str, *, reason: str = "worker drain requested") -> WorkerInstance:
        worker = self.store.require_worker(worker_id)
        if worker.state is WorkerLifecycleState.DRAINING:
            return worker
        return self.transition(
            worker_id,
            WorkerLifecycleState.DRAINING,
            reason=reason,
            changes={"drain_requested_at": utc_iso()},
        )

    def finish_drain(self, worker_id: str, *, stop: bool = False) -> WorkerInstance:
        if self._active_lease_count(worker_id):
            raise WorkerPoolError(
                WorkerPoolErrorCode.INVALID_WORKER_TRANSITION,
                "worker drain cannot finish while leases remain active",
                operation="finish_worker_drain",
                worker_id=worker_id,
            )
        target = WorkerLifecycleState.STOPPING if stop else WorkerLifecycleState.IDLE
        return self.transition(worker_id, target, reason="worker drain completed")

    def stop(self, worker_id: str, *, force: bool = False, reason: str = "worker stop requested") -> WorkerInstance:
        worker = self.store.require_worker(worker_id)
        if worker.state is WorkerLifecycleState.STOPPED:
            return worker
        active = self._active_lease_count(worker_id)
        if active and not force:
            return self.begin_drain(worker_id, reason=reason)
        if worker.state is not WorkerLifecycleState.STOPPING:
            worker = self.transition(worker_id, WorkerLifecycleState.STOPPING, reason=reason)
        return self.transition(
            worker_id,
            WorkerLifecycleState.STOPPED,
            reason="worker stopped",
            changes={"stopped_at": utc_iso()},
        )

    def mark_lost(self, worker_id: str, *, reason: str, failure_code: str = "heartbeat_timeout") -> WorkerInstance:
        worker = self.store.require_worker(worker_id)
        if worker.state is WorkerLifecycleState.LOST:
            return worker
        return self.transition(
            worker_id,
            WorkerLifecycleState.LOST,
            reason=reason,
            changes={"failure_code": failure_code, "failure_reason": reason},
        )

    def mark_failed(self, worker_id: str, *, reason: str, failure_code: str) -> WorkerInstance:
        worker = self.store.require_worker(worker_id)
        if worker.state is WorkerLifecycleState.FAILED:
            return worker
        return self.transition(
            worker_id,
            WorkerLifecycleState.FAILED,
            reason=reason,
            changes={"failure_code": failure_code, "failure_reason": reason},
        )

    def mark_busy(self, worker_id: str) -> WorkerInstance:
        worker = self.store.require_worker(worker_id)
        if worker.state is WorkerLifecycleState.BUSY:
            return worker
        return self.transition(worker_id, WorkerLifecycleState.BUSY, reason="worker accepted a lease")

    def mark_idle_if_unleased(self, worker_id: str) -> WorkerInstance:
        worker = self.store.require_worker(worker_id)
        if self._active_lease_count(worker_id) or worker.state is not WorkerLifecycleState.BUSY:
            return worker
        return self.transition(worker_id, WorkerLifecycleState.IDLE, reason="worker has no active leases")

    def transition(
        self,
        worker_id: str,
        target: WorkerLifecycleState,
        *,
        reason: str,
        changes: Mapping[str, Any] | None = None,
    ) -> WorkerInstance:
        current = self.store.require_worker(worker_id)
        if current.state is target:
            return current
        if target not in ALLOWED_TRANSITIONS[current.state]:
            raise WorkerPoolError(
                WorkerPoolErrorCode.INVALID_WORKER_TRANSITION,
                f"worker transition {current.state.value} -> {target.value} is not allowed",
                operation="transition_worker",
                worker_id=worker_id,
                metadata={"source": current.state.value, "target": target.value},
            )
        updated = current.advance(target, **dict(changes or {}))
        return self.store.update_worker(
            updated,
            expected_version=current.version,
            operation=f"worker_{target.value}",
            journal_payload={
                "source": current.state.value,
                "target": target.value,
                "reason": reason,
                "generation": updated.generation,
                "version": updated.version,
            },
        )

    def _active_lease_count(self, worker_id: str) -> int:
        from .models import LeaseState

        return len(
            self.store.list_leases(
                worker_id=worker_id,
                states=(LeaseState.ACTIVE, LeaseState.DRAINING),
            )
        )
