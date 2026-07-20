from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .models import (
    BackendDefinition,
    BackendDispatchAttempt,
    BackendDispatchError,
    BackendDispatchPhase,
    BackendDispatchSession,
    BackendFailureKind,
    BackendHealthStatus,
    BackendLease,
    BackendRecoveryInput,
    BackendRecoveryIntent,
    BackendSelectionRequest,
    RecoveryInputKind,
    new_backend_id,
    now_timestamp,
)
from .registry import BackendRegistry
from .store import BackendRegistryStore
from .transport import DispatchCancellation


class BackendFailoverDecisionKind(StrEnum):
    RETRY_SAME_BACKEND = "retry_same_backend"
    CHANGE_BACKEND = "change_backend"
    REBUILD_WORKSPACE = "rebuild_workspace"
    CHANGE_PROVIDER_ROUTE = "change_provider_route"
    RECONCILE = "reconcile"
    CANCEL = "cancel"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class BackendFailoverDecision:
    kind: BackendFailoverDecisionKind
    reason: str
    retryable: bool
    replay_safe: bool
    previous_backend_id: str
    next_backend_id: str | None
    provider_route_changed: bool
    backend_changed: bool
    workspace_rebuild_required: bool
    delay_seconds: float
    attempt_number: int
    maximum_attempts: int
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BackendFailoverPolicy:
    maximum_attempts: int = 3
    maximum_same_backend_attempts: int = 1
    base_delay_seconds: float = 0.05
    maximum_delay_seconds: float = 2.0
    retry_capacity_on_same_backend: bool = False
    failover_on_turn_timeout: bool = True
    failover_on_protocol_error: bool = False
    quarantine_workspace_corruption: bool = True
    allow_combined_provider_backend_escalation: bool = False

    def __post_init__(self) -> None:
        if self.maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive")
        if self.maximum_same_backend_attempts <= 0:
            raise ValueError("maximum_same_backend_attempts must be positive")
        if self.base_delay_seconds < 0 or self.maximum_delay_seconds < 0:
            raise ValueError("failover delay values cannot be negative")
        if self.maximum_delay_seconds < self.base_delay_seconds:
            raise ValueError("maximum delay cannot be less than base delay")


class BackendFailoverRuntime:
    """Deterministic execution-backend recovery; it never owns provider fallback."""

    def __init__(
        self,
        registry: BackendRegistry,
        *,
        policy: BackendFailoverPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.store = registry.store
        self.policy = policy or BackendFailoverPolicy(maximum_attempts=registry.attempt_limit)

    def decide(
        self,
        *,
        error: BackendDispatchError,
        lease: BackendLease,
        request: BackendSelectionRequest,
        attempts: Sequence[BackendDispatchAttempt],
        session: BackendDispatchSession,
    ) -> BackendFailoverDecision:
        attempt_number = len(attempts)
        evidence = {
            "failure_kind": error.kind.value,
            "recovery_intent": error.recovery_intent.value,
            "output_observed": error.output_observed,
            "attempt_count": attempt_number,
            "session_revision": session.revision,
            "provider_route_id": lease.provider_route_id,
            "m0_execution_ref": lease.m0_execution_ref,
        }
        if session.cancel_requested or error.kind is BackendFailureKind.DISPATCH_ABORTED:
            return self._decision(
                BackendFailoverDecisionKind.CANCEL,
                error,
                lease,
                attempt_number,
                reason=session.cancel_reason or str(error),
                replay_safe=False,
                evidence=evidence,
            )
        if error.kind is BackendFailureKind.PROVIDER_FAILURE:
            # ProviderControlPlane is the only owner allowed to change this
            # route. The backend stays pinned and exposes recovery input only.
            return self._decision(
                BackendFailoverDecisionKind.CHANGE_PROVIDER_ROUTE,
                error,
                lease,
                attempt_number,
                reason=str(error),
                replay_safe=not error.output_observed,
                provider_route_changed=True,
                evidence=evidence,
            )
        if error.output_observed:
            return self._decision(
                BackendFailoverDecisionKind.RECONCILE,
                error,
                lease,
                attempt_number,
                reason="observable output forbids automatic replay",
                replay_safe=False,
                evidence=evidence,
            )
        if not error.retryable:
            return self._decision(
                BackendFailoverDecisionKind.STOP,
                error,
                lease,
                attempt_number,
                reason=str(error),
                replay_safe=False,
                evidence=evidence,
            )
        if attempt_number >= min(lease.attempt_limit, self.policy.maximum_attempts):
            return self._decision(
                BackendFailoverDecisionKind.STOP,
                error,
                lease,
                attempt_number,
                reason="backend dispatch exhausted bounded attempts",
                replay_safe=True,
                evidence=evidence,
            )
        if error.kind is BackendFailureKind.WORKSPACE_CORRUPT:
            return self._decision(
                BackendFailoverDecisionKind.REBUILD_WORKSPACE,
                error,
                lease,
                attempt_number,
                reason=str(error),
                replay_safe=True,
                backend_changed=True,
                workspace_rebuild_required=True,
                delay_seconds=self._delay(attempt_number),
                evidence=evidence,
            )
        if error.kind in {
            BackendFailureKind.BACKEND_UNAVAILABLE,
            BackendFailureKind.BACKEND_TIMEOUT,
            BackendFailureKind.LEASE_EXPIRED,
            BackendFailureKind.TURN_TIMEOUT,
        }:
            if error.kind is BackendFailureKind.TURN_TIMEOUT and not self.policy.failover_on_turn_timeout:
                return self._decision(
                    BackendFailoverDecisionKind.STOP,
                    error,
                    lease,
                    attempt_number,
                    reason="turn-timeout failover disabled by policy",
                    replay_safe=True,
                    evidence=evidence,
                )
            return self._decision(
                BackendFailoverDecisionKind.CHANGE_BACKEND,
                error,
                lease,
                attempt_number,
                reason=str(error),
                replay_safe=True,
                backend_changed=True,
                delay_seconds=self._delay(attempt_number),
                evidence=evidence,
            )
        if error.kind is BackendFailureKind.BACKEND_CAPACITY:
            if self.policy.retry_capacity_on_same_backend:
                same_backend_attempts = sum(
                    1 for attempt in attempts if attempt.backend_id == lease.backend_id
                )
                if same_backend_attempts < self.policy.maximum_same_backend_attempts:
                    return self._decision(
                        BackendFailoverDecisionKind.RETRY_SAME_BACKEND,
                        error,
                        lease,
                        attempt_number,
                        reason=str(error),
                        replay_safe=True,
                        delay_seconds=self._delay(attempt_number),
                        evidence=evidence,
                    )
            return self._decision(
                BackendFailoverDecisionKind.CHANGE_BACKEND,
                error,
                lease,
                attempt_number,
                reason=str(error),
                replay_safe=True,
                backend_changed=True,
                delay_seconds=self._delay(attempt_number),
                evidence=evidence,
            )
        if error.kind is BackendFailureKind.BACKEND_PROTOCOL and self.policy.failover_on_protocol_error:
            return self._decision(
                BackendFailoverDecisionKind.CHANGE_BACKEND,
                error,
                lease,
                attempt_number,
                reason=str(error),
                replay_safe=True,
                backend_changed=True,
                evidence=evidence,
            )
        return self._decision(
            BackendFailoverDecisionKind.STOP,
            error,
            lease,
            attempt_number,
            reason=str(error),
            replay_safe=False,
            evidence=evidence,
        )

    def acquire_next(
        self,
        *,
        decision: BackendFailoverDecision,
        request: BackendSelectionRequest,
        previous_lease: BackendLease,
    ) -> BackendLease:
        if decision.kind is BackendFailoverDecisionKind.RETRY_SAME_BACKEND:
            return previous_lease
        if decision.kind not in {
            BackendFailoverDecisionKind.CHANGE_BACKEND,
            BackendFailoverDecisionKind.REBUILD_WORKSPACE,
        }:
            raise RuntimeError(f"decision does not acquire a backend: {decision.kind.value}")
        next_request = replace(
            request,
            excluded_backend_ids=tuple(
                sorted({*request.excluded_backend_ids, previous_lease.backend_id})
            ),
        )
        return self.registry.acquire(next_request, previous_lease_id=previous_lease.lease_id)

    def recovery_input(
        self,
        *,
        decision: BackendFailoverDecision,
        request: BackendSelectionRequest,
        session: BackendDispatchSession,
        attempt: BackendDispatchAttempt,
        previous_lease: BackendLease,
        next_lease: BackendLease | None,
    ) -> BackendRecoveryInput:
        kind_by_decision = {
            BackendFailoverDecisionKind.CHANGE_BACKEND: RecoveryInputKind.BACKEND_FAILOVER,
            BackendFailoverDecisionKind.REBUILD_WORKSPACE: RecoveryInputKind.WORKSPACE_REBUILD,
            BackendFailoverDecisionKind.CHANGE_PROVIDER_ROUTE: RecoveryInputKind.PROVIDER_ROUTE_CHANGE,
            BackendFailoverDecisionKind.RECONCILE: RecoveryInputKind.RECONCILE_PARTIAL_OUTPUT,
            BackendFailoverDecisionKind.CANCEL: RecoveryInputKind.CONTROL_CANCEL,
            BackendFailoverDecisionKind.STOP: RecoveryInputKind.BACKEND_FAILOVER,
            BackendFailoverDecisionKind.RETRY_SAME_BACKEND: RecoveryInputKind.BACKEND_FAILOVER,
        }
        return BackendRecoveryInput(
            recovery_input_id=new_backend_id("backend_recovery"),
            kind=kind_by_decision[decision.kind],
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            turn_id=request.turn_id,
            dispatch_session_id=session.session_id,
            attempt_id=attempt.attempt_id,
            previous_backend_id=previous_lease.backend_id,
            next_backend_id=(next_lease.backend_id if next_lease is not None else None),
            previous_provider_route_id=previous_lease.provider_route_id or "",
            next_provider_route_id=(
                (next_lease.provider_route_id or "")
                if next_lease is not None
                else previous_lease.provider_route_id or ""
            ),
            m0_execution_ref=previous_lease.m0_execution_ref,
            workspace_root=previous_lease.workspace_root,
            reason_code=decision.kind.value,
            reason=decision.reason,
            replay_safe=decision.replay_safe,
            requires_reconcile=decision.kind is BackendFailoverDecisionKind.RECONCILE,
            consumed=False,
            created_at=now_timestamp(),
            metadata={
                "attempt_number": decision.attempt_number,
                "maximum_attempts": decision.maximum_attempts,
                "provider_route_changed": decision.provider_route_changed,
                "backend_changed": decision.backend_changed,
                "workspace_rebuild_required": decision.workspace_rebuild_required,
                "provider_route_checksum": previous_lease.provider_route_checksum,
                "provider_catalog_revision": previous_lease.provider_catalog_revision,
                "provider_credential_version": previous_lease.provider_credential_version,
                "provider_credential_fingerprint": previous_lease.provider_credential_fingerprint,
                "provider_transport_id": previous_lease.provider_transport_id,
                "physical_worker_lease_ref": previous_lease.physical_worker_lease_ref,
                "evidence": dict(decision.evidence),
            },
        )

    def quarantine_workspace(
        self,
        *,
        lease: BackendLease,
        worker_id: str | None,
        reason: str,
        seconds: float = 3600.0,
    ) -> str:
        quarantine_id = new_backend_id("workspace_quarantine")
        created_at = now_timestamp()
        self.store.quarantine_workspace(
            quarantine_id=quarantine_id,
            workspace_root=lease.workspace_root,
            backend_id=lease.backend_id,
            worker_id=worker_id,
            reason=reason,
            created_at=created_at,
            expires_at=created_at + max(0.0, seconds),
            metadata={
                "lease_id": lease.lease_id,
                "m0_execution_ref": lease.m0_execution_ref,
                "provider_route_id": lease.provider_route_id,
            },
        )
        return quarantine_id

    def _delay(self, attempt_number: int) -> float:
        return min(
            self.policy.maximum_delay_seconds,
            self.policy.base_delay_seconds * (2 ** max(0, attempt_number - 1)),
        )

    def _decision(
        self,
        kind: BackendFailoverDecisionKind,
        error: BackendDispatchError,
        lease: BackendLease,
        attempt_number: int,
        *,
        reason: str,
        replay_safe: bool,
        provider_route_changed: bool = False,
        backend_changed: bool = False,
        workspace_rebuild_required: bool = False,
        delay_seconds: float = 0.0,
        evidence: Mapping[str, Any],
    ) -> BackendFailoverDecision:
        return BackendFailoverDecision(
            kind=kind,
            reason=reason,
            retryable=error.retryable,
            replay_safe=replay_safe,
            previous_backend_id=lease.backend_id,
            next_backend_id=None,
            provider_route_changed=provider_route_changed,
            backend_changed=backend_changed,
            workspace_rebuild_required=workspace_rebuild_required,
            delay_seconds=delay_seconds,
            attempt_number=attempt_number,
            maximum_attempts=min(lease.attempt_limit, self.policy.maximum_attempts),
            evidence=dict(evidence),
        )


class TurnTimeoutRuntime:
    """Owns monotonic deadline/cancel state; transports own physical interruption."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._timers: dict[str, threading.Timer] = {}

    def create(
        self,
        *,
        dispatch_id: str,
        deadline_at: float,
    ) -> DispatchCancellation:
        if deadline_at <= time.time():
            raise BackendDispatchError(
                BackendFailureKind.TURN_TIMEOUT,
                "turn deadline already elapsed",
                retryable=True,
                recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
            )
        cancellation = DispatchCancellation(dispatch_id=dispatch_id, deadline_at=deadline_at)
        timer = threading.Timer(
            max(0.0, deadline_at - time.time()),
            lambda: cancellation.cancel("turn deadline elapsed"),
        )
        timer.daemon = True
        with self._lock:
            previous = self._timers.pop(dispatch_id, None)
            if previous is not None:
                previous.cancel()
            self._timers[dispatch_id] = timer
        timer.start()
        return cancellation

    def complete(self, dispatch_id: str) -> None:
        with self._lock:
            timer = self._timers.pop(dispatch_id, None)
        if timer is not None:
            timer.cancel()

    def cancel(self, dispatch_id: str) -> bool:
        with self._lock:
            timer = self._timers.pop(dispatch_id, None)
        if timer is None:
            return False
        timer.cancel()
        return True


def transition_session(
    store: BackendRegistryStore,
    session: BackendDispatchSession,
    *,
    phase: BackendDispatchPhase,
    backend_id: str | None = None,
    lease_id: str | None = None,
    envelope_id: str | None = None,
    attempt_count: int | None = None,
    failover_count: int | None = None,
    output_observed: bool | None = None,
    terminal_reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> BackendDispatchSession:
    current = store.get_dispatch_session(session.session_id)
    if current is None:
        raise KeyError(f"backend dispatch session not found: {session.session_id}")
    merged_metadata = dict(current.metadata)
    merged_metadata.update(dict(metadata or {}))
    updated = replace(
        current,
        phase=phase,
        current_backend_id=(current.current_backend_id if backend_id is None else backend_id),
        current_lease_id=(current.current_lease_id if lease_id is None else lease_id),
        current_envelope_id=(current.current_envelope_id if envelope_id is None else envelope_id),
        attempt_count=(current.attempt_count if attempt_count is None else attempt_count),
        failover_count=(current.failover_count if failover_count is None else failover_count),
        output_observed=(current.output_observed if output_observed is None else output_observed),
        terminal_reason=(current.terminal_reason if terminal_reason is None else terminal_reason),
        updated_at=now_timestamp(),
        revision=current.revision + 1,
        metadata=merged_metadata,
    )
    return store.put_dispatch_session(updated, expected_revision=current.revision)


__all__ = [
    "BackendFailoverDecision",
    "BackendFailoverDecisionKind",
    "BackendFailoverPolicy",
    "BackendFailoverRuntime",
    "TurnTimeoutRuntime",
    "transition_session",
]
