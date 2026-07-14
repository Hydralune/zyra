from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping

from .canonical import stable_id, token_digest
from .constants import DEFAULT_SESSION_LEASE_SECONDS
from .errors import GatewayErrorCode, SandboxGatewayError
from .models import (
    GatewayLease,
    GatewayLifecycleState,
    GatewaySessionRecord,
    LifecycleTransition,
)
from .state_store import GatewayStateStore


_ALLOWED_TRANSITIONS: Mapping[GatewayLifecycleState, frozenset[GatewayLifecycleState]] = {
    GatewayLifecycleState.CREATED: frozenset(
        {
            GatewayLifecycleState.PREPARING,
            GatewayLifecycleState.FAILED,
            GatewayLifecycleState.CLEANING,
        }
    ),
    GatewayLifecycleState.PREPARING: frozenset(
        {
            GatewayLifecycleState.READY,
            GatewayLifecycleState.FAILED,
            GatewayLifecycleState.CLEANING,
        }
    ),
    GatewayLifecycleState.READY: frozenset(
        {
            GatewayLifecycleState.BUSY,
            GatewayLifecycleState.DRAINING,
            GatewayLifecycleState.CLEANING,
            GatewayLifecycleState.FAILED,
            GatewayLifecycleState.ORPHANED,
        }
    ),
    GatewayLifecycleState.BUSY: frozenset(
        {
            GatewayLifecycleState.READY,
            GatewayLifecycleState.DRAINING,
            GatewayLifecycleState.FAILED,
            GatewayLifecycleState.ORPHANED,
        }
    ),
    GatewayLifecycleState.DRAINING: frozenset(
        {
            GatewayLifecycleState.CLEANING,
            GatewayLifecycleState.FAILED,
        }
    ),
    GatewayLifecycleState.CLEANING: frozenset(
        {
            GatewayLifecycleState.CLOSED,
            GatewayLifecycleState.FAILED,
        }
    ),
    GatewayLifecycleState.ORPHANED: frozenset(
        {
            GatewayLifecycleState.PREPARING,
            GatewayLifecycleState.CLEANING,
            GatewayLifecycleState.FAILED,
        }
    ),
    GatewayLifecycleState.FAILED: frozenset(
        {
            GatewayLifecycleState.PREPARING,
            GatewayLifecycleState.CLEANING,
        }
    ),
    GatewayLifecycleState.CLOSED: frozenset(),
}


class SandboxLifecycle:
    """OpenHands-derived explicit lifecycle with Zyra leases and fencing."""

    def __init__(
        self,
        store: GatewayStateStore,
        *,
        lease_seconds: float = DEFAULT_SESSION_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.lease_seconds = float(lease_seconds)
        self.clock = clock

    def create(
        self,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        workspace_id: str,
        worker_id: str,
        backend_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> GatewaySessionRecord:
        record = GatewaySessionRecord.create(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            workspace_id=workspace_id,
            worker_id=worker_id,
            backend_id=backend_id,
            metadata=metadata,
        )
        return self.store.create_session(record)

    def transition(
        self,
        session_id: str,
        to_state: GatewayLifecycleState,
        *,
        reason: str,
        active_command_id: str | None = None,
        failure_code: str | None = None,
        failure_reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[GatewaySessionRecord, LifecycleTransition]:
        existing = self.store.require_session(session_id)
        if to_state is existing.state:
            transition = LifecycleTransition(
                transition_id=stable_id(
                    "gateway-transition",
                    session_id,
                    existing.generation,
                    existing.state.value,
                    "idempotent",
                ),
                session_id=session_id,
                from_state=existing.state,
                to_state=existing.state,
                generation_before=existing.generation,
                generation_after=existing.generation,
                reason=f"idempotent: {reason}",
                metadata=dict(metadata or {}),
            )
            return existing, transition
        allowed = _ALLOWED_TRANSITIONS[existing.state]
        if to_state not in allowed:
            raise SandboxGatewayError(
                GatewayErrorCode.INVALID_STATE,
                f"invalid sandbox lifecycle transition: {existing.state.value} -> {to_state.value}",
                operation="lifecycle_transition",
                metadata={"session_id": session_id, "reason": reason},
            )
        updated = existing.transition(
            to_state,
            active_command_id=active_command_id,
            failure_code=failure_code,
            failure_reason=failure_reason,
            metadata=metadata,
        )
        saved = self.store.save_session(updated, expected_generation=existing.generation)
        transition = LifecycleTransition(
            transition_id=stable_id(
                "gateway-transition",
                session_id,
                existing.generation,
                saved.generation,
                existing.state.value,
                saved.state.value,
                reason,
            ),
            session_id=session_id,
            from_state=existing.state,
            to_state=saved.state,
            generation_before=existing.generation,
            generation_after=saved.generation,
            reason=reason,
            metadata=dict(metadata or {}),
        )
        return saved, transition

    def prepare(self, session_id: str) -> GatewaySessionRecord:
        record, _ = self.transition(
            session_id,
            GatewayLifecycleState.PREPARING,
            reason="backend preparation started",
        )
        return record

    def ready(self, session_id: str) -> GatewaySessionRecord:
        record, _ = self.transition(
            session_id,
            GatewayLifecycleState.READY,
            reason="backend reported ready",
            active_command_id="",
        )
        return record

    def begin_command(
        self,
        session_id: str,
        command_id: str,
        *,
        owner_id: str,
        fence_token: str,
    ) -> tuple[GatewaySessionRecord, GatewayLease]:
        existing = self.store.require_session(session_id)
        if existing.state is not GatewayLifecycleState.READY:
            raise SandboxGatewayError(
                GatewayErrorCode.SESSION_NOT_READY,
                f"sandbox session is not ready: {existing.state.value}",
                operation="begin_command",
                retryable=existing.state in {
                    GatewayLifecycleState.CREATED,
                    GatewayLifecycleState.PREPARING,
                    GatewayLifecycleState.BUSY,
                },
            )
        now = self.clock()
        lease = GatewayLease(
            lease_id=stable_id(
                "gateway-lease",
                session_id,
                command_id,
                owner_id,
                existing.owner_epoch + 1,
                now,
            ),
            session_id=session_id,
            owner_id=owner_id,
            owner_epoch=existing.owner_epoch + 1,
            fence_token_digest=token_digest(fence_token),
            issued_at=now,
            expires_at=now + self.lease_seconds,
        )
        updated = replace(
            existing.transition(
                GatewayLifecycleState.BUSY,
                active_command_id=command_id,
                lease=lease,
                metadata={"last_command_started_at": now},
            ),
            owner_epoch=lease.owner_epoch,
        )
        saved = self.store.save_session(updated, expected_generation=existing.generation)
        self.store.save_lease(lease)
        return saved, lease

    def assert_lease(
        self,
        session_id: str,
        lease_id: str,
        *,
        owner_id: str,
        fence_token: str,
    ) -> GatewayLease:
        record = self.store.require_session(session_id)
        if record.lease is None or record.lease.lease_id != lease_id:
            raise SandboxGatewayError(
                GatewayErrorCode.SESSION_FENCED,
                "gateway lease no longer owns the session",
                operation="assert_lease",
            )
        lease = self.store.require_lease(lease_id)
        if not lease.active:
            raise SandboxGatewayError(
                GatewayErrorCode.LEASE_EXPIRED,
                "gateway lease expired",
                operation="assert_lease",
            )
        if lease.owner_id != owner_id:
            raise SandboxGatewayError(
                GatewayErrorCode.SESSION_FENCED,
                "gateway lease owner mismatch",
                operation="assert_lease",
            )
        if lease.fence_token_digest != token_digest(fence_token):
            raise SandboxGatewayError(
                GatewayErrorCode.SESSION_FENCED,
                "gateway fence token mismatch",
                operation="assert_lease",
            )
        if lease.owner_epoch != record.owner_epoch:
            raise SandboxGatewayError(
                GatewayErrorCode.SESSION_FENCED,
                "gateway lease owner epoch is stale",
                operation="assert_lease",
            )
        return lease

    def finish_command(
        self,
        session_id: str,
        lease_id: str,
        *,
        owner_id: str,
        fence_token: str,
        failed: bool = False,
        failure_code: str = "",
        failure_reason: str = "",
    ) -> GatewaySessionRecord:
        lease = self.assert_lease(
            session_id,
            lease_id,
            owner_id=owner_id,
            fence_token=fence_token,
        )
        existing = self.store.require_session(session_id)
        now = self.clock()
        released = replace(lease, released_at=now)
        self.store.save_lease(released)
        target = GatewayLifecycleState.FAILED if failed else GatewayLifecycleState.READY
        updated = existing.transition(
            target,
            active_command_id="",
            failure_code=failure_code if failed else "",
            failure_reason=failure_reason if failed else "",
            lease=None,
            metadata={"last_command_finished_at": now},
        )
        return self.store.save_session(updated, expected_generation=existing.generation)

    def wait_ready(
        self,
        session_id: str,
        *,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.05,
    ) -> GatewaySessionRecord:
        deadline = time.monotonic() + timeout_seconds
        while True:
            record = self.store.require_session(session_id)
            if record.state is GatewayLifecycleState.READY:
                return record
            if record.state in {GatewayLifecycleState.FAILED, GatewayLifecycleState.CLOSED}:
                raise SandboxGatewayError(
                    GatewayErrorCode.SESSION_NOT_READY,
                    f"sandbox session cannot become ready: {record.state.value}",
                    operation="wait_ready",
                    metadata={"failure_code": record.failure_code},
                )
            if time.monotonic() >= deadline:
                raise SandboxGatewayError(
                    GatewayErrorCode.SESSION_NOT_READY,
                    "timed out waiting for sandbox session readiness",
                    operation="wait_ready",
                    retryable=True,
                )
            time.sleep(poll_seconds)

    def close(self, session_id: str) -> GatewaySessionRecord:
        record = self.store.require_session(session_id)
        if record.state is GatewayLifecycleState.CLOSED:
            return record
        if record.state is GatewayLifecycleState.BUSY:
            record, _ = self.transition(
                session_id,
                GatewayLifecycleState.DRAINING,
                reason="close requested while command active",
            )
        if record.state not in {GatewayLifecycleState.CLEANING, GatewayLifecycleState.DRAINING}:
            record, _ = self.transition(
                session_id,
                GatewayLifecycleState.CLEANING,
                reason="sandbox cleanup started",
                active_command_id="",
            )
        elif record.state is GatewayLifecycleState.DRAINING:
            record, _ = self.transition(
                session_id,
                GatewayLifecycleState.CLEANING,
                reason="sandbox drain completed",
                active_command_id="",
            )
        record, _ = self.transition(
            session_id,
            GatewayLifecycleState.CLOSED,
            reason="sandbox resources cleaned",
        )
        return record

    def recover_interrupted(self) -> tuple[GatewaySessionRecord, ...]:
        recoverable = self.store.list_sessions(
            states={
                GatewayLifecycleState.PREPARING,
                GatewayLifecycleState.BUSY,
                GatewayLifecycleState.DRAINING,
                GatewayLifecycleState.CLEANING,
                GatewayLifecycleState.ORPHANED,
            }
        )
        results: list[GatewaySessionRecord] = []
        for record in recoverable:
            current = self.store.require_session(record.session_id)
            if current.state is GatewayLifecycleState.BUSY:
                expired = current.lease is None or not current.lease.active
                if not expired:
                    continue
                updated = replace(
                    current.transition(
                        GatewayLifecycleState.ORPHANED,
                        active_command_id="",
                        lease=None,
                        metadata={"recovery_reason": "expired_command_lease"},
                    ),
                    recovery_count=current.recovery_count + 1,
                )
                current = self.store.save_session(
                    updated,
                    expected_generation=current.generation,
                )
            if current.state in {
                GatewayLifecycleState.PREPARING,
                GatewayLifecycleState.ORPHANED,
            }:
                updated = replace(
                    current.transition(
                        GatewayLifecycleState.PREPARING,
                        active_command_id="",
                        lease=None,
                        metadata={"recovery_reason": "backend_reprepare_required"},
                    )
                    if current.state is GatewayLifecycleState.ORPHANED
                    else current,
                    recovery_count=current.recovery_count + 1,
                )
                if updated is not current:
                    current = self.store.save_session(
                        updated,
                        expected_generation=current.generation,
                    )
                results.append(current)
                continue
            if current.state in {
                GatewayLifecycleState.DRAINING,
                GatewayLifecycleState.CLEANING,
            }:
                try:
                    current = self.close(current.session_id)
                except SandboxGatewayError:
                    results.append(self.store.require_session(current.session_id))
                else:
                    results.append(current)
                continue
            results.append(current)
        return tuple(results)

    @staticmethod
    def allowed_transitions(
        state: GatewayLifecycleState,
    ) -> frozenset[GatewayLifecycleState]:
        return _ALLOWED_TRANSITIONS[state]
