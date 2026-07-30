from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable, Mapping, Sequence

from .errors import LeaseFenced, WorkerPoolError, WorkerPoolErrorCode
from .lifecycle import WorkerLifecycleRuntime
from .models import (
    AttemptState,
    CapabilityRequirement,
    ExecutionOutcome,
    ExecutionReceipt,
    LeaseAcquisition,
    LeaseState,
    ResourceVector,
    TaskAttempt,
    WorkerCapabilityManifest,
    WorkerInstance,
    WorkerLease,
    WorkerLifecycleState,
    WorkerSelection,
    parse_utc,
    stable_digest,
    utc_iso,
    utc_now,
)
from .store import WorkerPoolStore


LeaseCommitHook = Callable[
    [Any, TaskAttempt, WorkerLease, WorkerInstance, WorkerCapabilityManifest],
    None,
]


class WorkerLeaseManager:
    ACTIVE_LEASE_STATES = (LeaseState.ACTIVE, LeaseState.DRAINING)

    def __init__(
        self,
        store: WorkerPoolStore,
        lifecycle: WorkerLifecycleRuntime,
        *,
        default_ttl_seconds: float = 30.0,
        max_ttl_seconds: float = 3600.0,
    ) -> None:
        if default_ttl_seconds <= 0 or max_ttl_seconds < default_ttl_seconds:
            raise ValueError("invalid worker lease TTL configuration")
        self.store = store
        self.lifecycle = lifecycle
        self.default_ttl_seconds = float(default_ttl_seconds)
        self.max_ttl_seconds = float(max_ttl_seconds)

    def acquire(
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
        parent_attempt_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        admission_guard: Callable[[Any, WorkerInstance], None] | None = None,
        commit_hook: LeaseCommitHook | None = None,
    ) -> LeaseAcquisition:
        duration = self._ttl(ttl_seconds)
        latest = self.store.latest_attempt(task_id)
        number = int(attempt_number or (1 if latest is None else latest.attempt_number + 1))
        if latest is not None and number <= latest.attempt_number:
            existing = self._existing_acquisition(task_id, number, idempotency_key)
            if existing is not None:
                return existing
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                "new physical attempt number must exceed the latest task attempt",
                operation="acquire_worker_lease",
                task_id=task_id,
                metadata={"latest_attempt": latest.attempt_number, "requested_attempt": number},
            )
        if latest is not None and not latest.terminal and latest.state is not AttemptState.LOST:
            raise WorkerPoolError(
                WorkerPoolErrorCode.LOGICAL_TASK_CONFLICT,
                "logical task already has a non-terminal physical attempt",
                operation="acquire_worker_lease",
                task_id=task_id,
                attempt_id=latest.attempt_id,
            )
        candidates = self.rank_candidates(
            requirement,
            preferred_worker_ids=preferred_worker_ids,
            excluded_worker_ids=excluded_worker_ids,
        )
        accepted = next((item for item in candidates if item.accepted), None)
        if accepted is None:
            raise WorkerPoolError(
                WorkerPoolErrorCode.RESOURCE_EXHAUSTED,
                "no registered worker can satisfy the lease requirement",
                operation="acquire_worker_lease",
                task_id=task_id,
                retryable=True,
                metadata={"candidates": [item.to_dict() for item in candidates]},
            )
        worker = self.store.require_worker(accepted.worker_id)
        manifest = self.store.latest_manifest(worker.worker_id)
        if manifest is None or manifest.digest != worker.manifest_digest:
            raise WorkerPoolError(
                WorkerPoolErrorCode.CAPABILITY_MISMATCH,
                "selected worker has no matching current capability manifest",
                operation="acquire_worker_lease",
                worker_id=worker.worker_id,
                task_id=task_id,
            )
        attempt = TaskAttempt(
            task_id=task_id,
            run_id=run_id,
            attempt_number=number,
            parent_attempt_id=parent_attempt_id or (latest.attempt_id if latest else ""),
            recovery_reason=recovery_reason,
            metadata={
                **dict(metadata or {}),
                "logical_task_owner": "typescript.AgentTaskRuntime",
                "physical_attempt_owner": "WorkerLeaseManager",
            },
        )
        key = idempotency_key or stable_digest((task_id, number, owner_session_id, requirement.to_dict()))
        acquired_at = utc_iso()
        lease = WorkerLease(
            task_id=task_id,
            run_id=run_id,
            attempt_id=attempt.attempt_id,
            worker_id=worker.worker_id,
            owner_session_id=owner_session_id,
            backend_id=worker.backend_id,
            deadline_at=utc_iso(parse_utc(acquired_at) + timedelta(seconds=duration)),
            resources=requirement.resources,
            acquired_at=acquired_at,
            idempotency_key=key,
            metadata={
                **dict(metadata or {}),
                "manifest_digest": manifest.digest,
                "worker_generation": worker.generation,
                "requirement": requirement.to_dict(),
                "lease_owner": "WorkerLeaseManager",
            },
        )
        with self.store.transaction() as connection:
            current_worker = self.store.require_worker(worker.worker_id, connection=connection)
            if not current_worker.accepting_leases:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.DRAINING,
                    "selected worker stopped accepting leases before acquisition committed",
                    operation="acquire_worker_lease",
                    worker_id=worker.worker_id,
                    task_id=task_id,
                    retryable=True,
                )
            if admission_guard is not None:
                admission_guard(connection, current_worker)
            self._assert_capacity(worker.worker_id, manifest, requirement.resources)
            persisted_attempt = self.store.insert_attempt(attempt, connection=connection)
            persisted_lease = self.store.insert_lease(lease, connection=connection)
            leased_attempt = persisted_attempt.advance(
                AttemptState.LEASED,
                worker_id=worker.worker_id,
                lease_id=persisted_lease.lease_id,
            )
            leased_attempt = self.store.update_attempt(
                leased_attempt,
                expected_version=persisted_attempt.version,
                operation="attempt_leased",
                journal_payload={
                    "worker_id": worker.worker_id,
                    "lease_id": persisted_lease.lease_id,
                    "attempt_number": leased_attempt.attempt_number,
                },
                connection=connection,
            )
            if commit_hook is not None:
                commit_hook(
                    connection,
                    leased_attempt,
                    persisted_lease,
                    current_worker,
                    manifest,
                )
        self.lifecycle.mark_busy(worker.worker_id)
        return LeaseAcquisition(
            attempt=leased_attempt,
            lease=persisted_lease,
            worker=self.store.require_worker(worker.worker_id),
            manifest=manifest,
            candidates=candidates,
        )

    def rank_candidates(
        self,
        requirement: CapabilityRequirement,
        *,
        preferred_worker_ids: Sequence[str] = (),
        excluded_worker_ids: Sequence[str] = (),
    ) -> tuple[WorkerSelection, ...]:
        preferred = set(preferred_worker_ids)
        excluded = set(excluded_worker_ids)
        selections: list[WorkerSelection] = []
        for worker in self.store.list_workers():
            reasons: list[str] = []
            manifest = self.store.latest_manifest(worker.worker_id)
            allocated = self.allocated_resources(worker.worker_id)
            available = ResourceVector()
            accepted = True
            if worker.worker_id in excluded:
                accepted = False
                reasons.append("worker excluded by dispatch request")
            if not worker.accepting_leases:
                accepted = False
                reasons.append(f"worker state {worker.state.value} does not accept leases")
            if manifest is None:
                accepted = False
                reasons.append("worker has no capability manifest")
                digest = ""
            else:
                digest = manifest.digest
                if manifest.digest != worker.manifest_digest:
                    accepted = False
                    reasons.append("worker manifest binding is stale")
                supported, failures = manifest.supports(requirement)
                if not supported:
                    accepted = False
                    reasons.extend(failures)
                if manifest.resource_capacity.fits(allocated):
                    available = manifest.resource_capacity.minus(allocated)
                else:
                    accepted = False
                    reasons.append("worker is already overcommitted")
                if not available.fits(requirement.resources):
                    accepted = False
                    reasons.append("remaining worker resources are insufficient")
            active = len(self.store.list_leases(worker_id=worker.worker_id, states=self.ACTIVE_LEASE_STATES))
            score = 1000.0 if worker.worker_id in preferred else 0.0
            score += 100.0 if worker.state is WorkerLifecycleState.IDLE else 60.0
            score -= active * 15.0
            if manifest is not None:
                utilization = allocated.utilization_against(manifest.resource_capacity)
                score -= sum(utilization.values()) * 10.0
                score += len(set(requirement.required).intersection(manifest.capabilities)) * 2.0
            if not accepted:
                score -= 10000.0
            selections.append(
                WorkerSelection(
                    worker_id=worker.worker_id,
                    accepted=accepted,
                    score=round(score, 6),
                    reasons=tuple(reasons or ("eligible",)),
                    available=available,
                    manifest_digest=digest,
                    active_lease_count=active,
                )
            )
        selections.sort(key=lambda item: (-int(item.accepted), -item.score, item.worker_id))
        return tuple(selections)

    def start_attempt(
        self,
        lease_id: str,
        *,
        worker_id: str,
        fence_token: str,
        fence_epoch: int,
        backend_dispatch_id: str = "",
    ) -> TaskAttempt:
        lease = self.assert_fence(
            lease_id,
            worker_id=worker_id,
            fence_token=fence_token,
            fence_epoch=fence_epoch,
            operation="start_attempt",
        )
        attempt = self.store.require_attempt(lease.attempt_id)
        if attempt.state is AttemptState.RUNNING:
            return attempt
        if attempt.state is not AttemptState.LEASED:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                f"attempt in {attempt.state.value} state cannot start",
                operation="start_attempt",
                attempt_id=attempt.attempt_id,
                lease_id=lease_id,
            )
        updated = attempt.advance(
            AttemptState.RUNNING,
            started_at=utc_iso(),
            backend_dispatch_id=backend_dispatch_id,
        )
        return self.store.update_attempt(
            updated,
            expected_version=attempt.version,
            operation="attempt_started",
            journal_payload={
                "lease_id": lease_id,
                "worker_id": worker_id,
                "backend_dispatch_id": backend_dispatch_id,
            },
        )

    def renew(
        self,
        lease_id: str,
        *,
        worker_id: str,
        fence_token: str,
        fence_epoch: int,
        ttl_seconds: float | None = None,
    ) -> WorkerLease:
        lease = self.assert_fence(
            lease_id,
            worker_id=worker_id,
            fence_token=fence_token,
            fence_epoch=fence_epoch,
            operation="renew_worker_lease",
        )
        renewed_at = utc_iso()
        updated = lease.advance(
            renewed_at=renewed_at,
            deadline_at=utc_iso(parse_utc(renewed_at) + timedelta(seconds=self._ttl(ttl_seconds))),
        )
        return self.store.update_lease(
            updated,
            expected_version=lease.version,
            operation="lease_renewed",
            journal_payload={
                "deadline_at": updated.deadline_at,
                "fence_epoch": updated.fence_epoch,
                "worker_id": worker_id,
            },
        )

    def begin_drain(self, lease_id: str, *, reason: str) -> WorkerLease:
        lease = self.store.require_lease(lease_id)
        if lease.state is LeaseState.DRAINING:
            return lease
        if lease.state is not LeaseState.ACTIVE:
            raise WorkerPoolError(
                WorkerPoolErrorCode.LEASE_CONFLICT,
                "only an active lease can enter draining",
                operation="drain_worker_lease",
                lease_id=lease_id,
            )
        updated = lease.advance(
            LeaseState.DRAINING,
            drain_requested_at=utc_iso(),
            metadata={**dict(lease.metadata), "drain_reason": reason},
        )
        return self.store.update_lease(
            updated,
            expected_version=lease.version,
            operation="lease_draining",
            journal_payload={"reason": reason, "worker_id": lease.worker_id},
        )

    def complete(
        self,
        lease_id: str,
        *,
        worker_id: str,
        fence_token: str,
        fence_epoch: int,
        outcome: ExecutionOutcome,
        summary: str,
        artifact_refs: Sequence[str] = (),
        event_refs: Sequence[str] = (),
        backend_receipt_ref: str = "",
        gateway_receipt_ref: str = "",
        error_code: str = "",
        error_message: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionReceipt:
        lease = self.assert_fence(
            lease_id,
            worker_id=worker_id,
            fence_token=fence_token,
            fence_epoch=fence_epoch,
            operation="complete_attempt",
        )
        attempt = self.store.require_attempt(lease.attempt_id)
        if attempt.state not in {AttemptState.LEASED, AttemptState.RUNNING}:
            receipts = self.store.receipts_for_task(attempt.task_id)
            existing = next((item for item in receipts if item.attempt_id == attempt.attempt_id), None)
            if existing is not None:
                return existing
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                "attempt is not executable",
                operation="complete_attempt",
                attempt_id=attempt.attempt_id,
                lease_id=lease_id,
            )
        attempt_state = {
            ExecutionOutcome.SUCCEEDED: AttemptState.SUCCEEDED,
            ExecutionOutcome.CANCELLED: AttemptState.CANCELLED,
            ExecutionOutcome.FENCED: AttemptState.SUPERSEDED,
        }.get(outcome, AttemptState.FAILED)
        finished_at = utc_iso()
        receipt = ExecutionReceipt(
            task_id=lease.task_id,
            run_id=lease.run_id,
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
            worker_id=worker_id,
            fence_epoch=fence_epoch,
            outcome=outcome,
            started_at=attempt.started_at or lease.acquired_at,
            finished_at=finished_at,
            summary=summary,
            artifact_refs=tuple(artifact_refs),
            event_refs=tuple(event_refs),
            backend_receipt_ref=backend_receipt_ref,
            gateway_receipt_ref=gateway_receipt_ref,
            error_code=error_code,
            error_message=error_message,
            metadata=dict(metadata or {}),
        )
        with self.store.transaction() as connection:
            current_lease = self.store.require_lease(lease_id, connection=connection)
            if not current_lease.assert_fence(
                worker_id=worker_id,
                fence_token=fence_token,
                fence_epoch=fence_epoch,
            ):
                raise LeaseFenced(lease_id, operation="complete_attempt", worker_id=worker_id)
            current_attempt = self.store.require_attempt(lease.attempt_id, connection=connection)
            completed_attempt = current_attempt.advance(attempt_state, finished_at=finished_at)
            self.store.update_attempt(
                completed_attempt,
                expected_version=current_attempt.version,
                operation=f"attempt_{attempt_state.value}",
                journal_payload={"outcome": outcome.value, "receipt_id": receipt.receipt_id},
                connection=connection,
            )
            released = current_lease.advance(LeaseState.RELEASED, released_at=finished_at)
            self.store.update_lease(
                released,
                expected_version=current_lease.version,
                operation="lease_released",
                journal_payload={"outcome": outcome.value, "receipt_id": receipt.receipt_id},
                connection=connection,
            )
            self.store.append_execution_receipt(receipt, connection=connection)
        self.lifecycle.mark_idle_if_unleased(worker_id)
        return receipt

    def cancel(self, lease_id: str, *, reason: str) -> tuple[WorkerLease, TaskAttempt]:
        lease = self.store.require_lease(lease_id)
        attempt = self.store.require_attempt(lease.attempt_id)
        if lease.state is LeaseState.CANCELLED and attempt.state is AttemptState.CANCELLED:
            return lease, attempt
        now = utc_iso()
        with self.store.transaction() as connection:
            current_lease = self.store.require_lease(lease_id, connection=connection)
            current_attempt = self.store.require_attempt(lease.attempt_id, connection=connection)
            if current_lease.terminal:
                return current_lease, current_attempt
            cancelled_lease = current_lease.advance(
                LeaseState.CANCELLED,
                cancel_requested_at=now,
                released_at=now,
                fence_epoch=current_lease.fence_epoch + 1,
                metadata={**dict(current_lease.metadata), "cancel_reason": reason},
            )
            cancelled_attempt = current_attempt.advance(
                AttemptState.CANCELLED,
                finished_at=now,
                metadata={**dict(current_attempt.metadata), "cancel_reason": reason},
            )
            self.store.update_lease(
                cancelled_lease,
                expected_version=current_lease.version,
                operation="lease_cancelled",
                journal_payload={"reason": reason, "fence_epoch": cancelled_lease.fence_epoch},
                connection=connection,
            )
            self.store.update_attempt(
                cancelled_attempt,
                expected_version=current_attempt.version,
                operation="attempt_cancelled",
                journal_payload={"reason": reason, "lease_id": lease_id},
                connection=connection,
            )
        self.lifecycle.mark_idle_if_unleased(lease.worker_id)
        return cancelled_lease, cancelled_attempt

    def expire(self, lease_id: str, *, reason: str) -> tuple[WorkerLease, TaskAttempt]:
        lease = self.store.require_lease(lease_id)
        attempt = self.store.require_attempt(lease.attempt_id)
        if lease.state is LeaseState.EXPIRED:
            return lease, attempt
        if lease.terminal:
            return lease, attempt
        now = utc_iso()
        with self.store.transaction() as connection:
            current_lease = self.store.require_lease(lease_id, connection=connection)
            current_attempt = self.store.require_attempt(lease.attempt_id, connection=connection)
            expired = current_lease.advance(
                LeaseState.EXPIRED,
                released_at=now,
                fence_epoch=current_lease.fence_epoch + 1,
                metadata={**dict(current_lease.metadata), "expiry_reason": reason},
            )
            lost = current_attempt.advance(
                AttemptState.LOST,
                finished_at=now,
                recovery_reason=reason,
            )
            self.store.update_lease(
                expired,
                expected_version=current_lease.version,
                operation="lease_expired",
                journal_payload={"reason": reason, "fence_epoch": expired.fence_epoch},
                connection=connection,
            )
            self.store.update_attempt(
                lost,
                expected_version=current_attempt.version,
                operation="attempt_lost",
                journal_payload={"reason": reason, "lease_id": lease_id},
                connection=connection,
            )
        self.lifecycle.mark_idle_if_unleased(lease.worker_id)
        return expired, lost

    def fence(self, lease_id: str, *, reason: str) -> WorkerLease:
        lease = self.store.require_lease(lease_id)
        if lease.state is LeaseState.FENCED:
            return lease
        updated = lease.advance(
            LeaseState.FENCED,
            fence_epoch=lease.fence_epoch + 1,
            released_at=utc_iso(),
            metadata={**dict(lease.metadata), "fence_reason": reason},
        )
        result = self.store.update_lease(
            updated,
            expected_version=lease.version,
            operation="lease_fenced",
            journal_payload={"reason": reason, "fence_epoch": updated.fence_epoch},
        )
        self.lifecycle.mark_idle_if_unleased(lease.worker_id)
        return result

    def assert_fence(
        self,
        lease_id: str,
        *,
        worker_id: str,
        fence_token: str,
        fence_epoch: int,
        operation: str,
    ) -> WorkerLease:
        lease = self.store.require_lease(lease_id)
        if lease.expired_at():
            self.expire(lease_id, reason="lease deadline elapsed before fenced operation")
            raise LeaseFenced(
                lease_id,
                operation=operation,
                worker_id=worker_id,
                attempt_id=lease.attempt_id,
                reason="lease deadline elapsed",
            )
        if not lease.assert_fence(
            worker_id=worker_id,
            fence_token=fence_token,
            fence_epoch=fence_epoch,
        ):
            raise LeaseFenced(
                lease_id,
                operation=operation,
                worker_id=worker_id,
                attempt_id=lease.attempt_id,
            )
        return lease

    def sweep_expired(self, *, now: str | None = None) -> tuple[WorkerLease, ...]:
        current = parse_utc(now) if now else utc_now()
        expired: list[WorkerLease] = []
        for lease in self.store.list_leases(states=self.ACTIVE_LEASE_STATES):
            if lease.expired_at(current):
                current_lease, _ = self.expire(lease.lease_id, reason="lease deadline elapsed")
                expired.append(current_lease)
        return tuple(expired)

    def allocated_resources(self, worker_id: str) -> ResourceVector:
        result = ResourceVector()
        for lease in self.store.list_leases(worker_id=worker_id, states=self.ACTIVE_LEASE_STATES):
            result = result.plus(lease.resources)
        return result

    def _assert_capacity(
        self,
        worker_id: str,
        manifest: WorkerCapabilityManifest,
        requested: ResourceVector,
    ) -> None:
        allocated = self.allocated_resources(worker_id)
        if not manifest.resource_capacity.fits(allocated):
            raise WorkerPoolError(
                WorkerPoolErrorCode.RESOURCE_EXHAUSTED,
                "worker resource ledger is already overcommitted",
                operation="assert_worker_capacity",
                worker_id=worker_id,
                retryable=True,
            )
        available = manifest.resource_capacity.minus(allocated)
        if not available.fits(requested):
            raise WorkerPoolError(
                WorkerPoolErrorCode.RESOURCE_EXHAUSTED,
                "worker resources changed before lease acquisition committed",
                operation="assert_worker_capacity",
                worker_id=worker_id,
                retryable=True,
                metadata={"available": available.to_dict(), "requested": requested.to_dict()},
            )

    def _existing_acquisition(
        self,
        task_id: str,
        attempt_number: int,
        idempotency_key: str,
    ) -> LeaseAcquisition | None:
        attempt = next(
            (item for item in self.store.list_attempts(task_id=task_id) if item.attempt_number == attempt_number),
            None,
        )
        if attempt is None or not attempt.lease_id:
            return None
        lease = self.store.get_lease(attempt.lease_id)
        if lease is None or (idempotency_key and lease.idempotency_key != idempotency_key):
            return None
        worker = self.store.require_worker(lease.worker_id)
        manifest = self.store.latest_manifest(worker.worker_id)
        if manifest is None:
            return None
        return LeaseAcquisition(
            attempt=attempt,
            lease=lease,
            worker=worker,
            manifest=manifest,
            reused=True,
        )

    def _ttl(self, value: float | None) -> float:
        duration = self.default_ttl_seconds if value is None else float(value)
        if duration <= 0:
            raise ValueError("worker lease TTL must be positive")
        return min(duration, self.max_ttl_seconds)
