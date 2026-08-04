from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Generic, Mapping, Sequence, TypeVar

from .control import ACTIVE_DISPATCHES, ActiveDispatchRegistry
from .journal import BackendDispatchJournal, DispatchJournalKind
from .health_supervisor import BackendHealthSupervisor
from .models import (
    BackendControlEvent,
    BackendDispatchAttempt,
    BackendDispatchEnvelope,
    BackendDispatchError,
    BackendDispatchPhase,
    BackendDispatchSession,
    BackendFailureKind,
    BackendLease,
    BackendRecoveryInput,
    BackendRecoveryIntent,
    BackendSelectionRequest,
    checksum,
    new_backend_id,
    now_timestamp,
)
from .policy import (
    BackendFailoverDecision,
    BackendFailoverDecisionKind,
    BackendFailoverRuntime,
    TurnTimeoutRuntime,
    transition_session,
)
from .registry import BackendRegistry
from .transport import (
    BackendTransportRegistry,
    BackendTransportRequest,
    BackendTransportResponse,
)
from .workspace_attestation import WorkspaceAttestationRuntime


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class WorkerDispatchResult(Generic[T]):
    value: T
    session: BackendDispatchSession
    final_lease: BackendLease
    final_envelope: BackendDispatchEnvelope
    attempts: tuple[BackendDispatchAttempt, ...]
    events: tuple[BackendControlEvent, ...]
    recovery_inputs: tuple[BackendRecoveryInput, ...]
    transport_responses: tuple[BackendTransportResponse, ...]
    backend_changed: bool


class WorkerDispatchRouter:
    """Canonical backend dispatch path for callable, process, Docker and HTTP workers."""

    def __init__(
        self,
        registry: BackendRegistry,
        *,
        transports: BackendTransportRegistry | None = None,
        failover: BackendFailoverRuntime | None = None,
        timeouts: TurnTimeoutRuntime | None = None,
        active: ActiveDispatchRegistry | None = None,
        workspace_attestation: WorkspaceAttestationRuntime | None = None,
        health_supervisor: BackendHealthSupervisor | None = None,
    ) -> None:
        self.registry = registry
        self.store = registry.store
        self.journal = BackendDispatchJournal(self.store)
        self.transports = transports or BackendTransportRegistry()
        self.failover = failover or BackendFailoverRuntime(registry)
        self.timeouts = timeouts or TurnTimeoutRuntime()
        self.active = active or ACTIVE_DISPATCHES
        self.workspace_attestation = workspace_attestation or WorkspaceAttestationRuntime(self.store)
        self.health_supervisor = health_supervisor or BackendHealthSupervisor(
            registry,
            transports=self.transports,
        )

    def dispatch_callable(
        self,
        request: BackendSelectionRequest,
        operation: Callable[[BackendDispatchEnvelope], T],
        *,
        idempotency_key: str,
        interruptible: bool = True,
    ) -> WorkerDispatchResult[T]:
        terminal_ids = set(self.registry.terminal_backend_ids())
        if terminal_ids:
            request = replace(
                request,
                excluded_backend_ids=tuple(
                    sorted({*request.excluded_backend_ids, *terminal_ids})
                ),
                metadata={
                    **dict(request.metadata),
                    "terminal_callable_exclusion": True,
                    "terminal_callable_excluded_backend_ids": tuple(sorted(terminal_ids)),
                },
            )
        return self.dispatch(
            request,
            operation_name="worker.run",
            payload={
                "runtime_worker": request.runtime_worker,
                "m0_execution_ref": request.m0_execution_ref,
            },
            idempotency_key=idempotency_key,
            in_process_operation=operation,
            interruptible=interruptible,
        )

    def dispatch_payload(
        self,
        request: BackendSelectionRequest,
        *,
        operation_name: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> WorkerDispatchResult[dict[str, Any]]:
        return self.dispatch(
            request,
            operation_name=operation_name,
            payload=payload,
            idempotency_key=idempotency_key,
            in_process_operation=None,
            interruptible=True,
        )

    def dispatch(
        self,
        request: BackendSelectionRequest,
        *,
        operation_name: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        in_process_operation: Callable[[BackendDispatchEnvelope], T] | None,
        interruptible: bool,
    ) -> WorkerDispatchResult[Any]:
        self._validate_entry(request, operation_name, idempotency_key)
        existing = self.store.dispatch_session_for_idempotency(idempotency_key)
        if existing is not None:
            if not existing.terminal:
                raise BackendDispatchError(
                    BackendFailureKind.LEASE_CONFLICT,
                    f"idempotent dispatch is already active: {existing.session_id}",
                    retryable=False,
                    recovery_intent=BackendRecoveryIntent.NONE,
                    detail={"session_id": existing.session_id, "phase": existing.phase.value},
                )
            raise BackendDispatchError(
                BackendFailureKind.LEASE_CONFLICT,
                "terminal dispatch replay requires a persisted result materializer",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
                detail={"session_id": existing.session_id, "phase": existing.phase.value},
            )
        session = self._create_session(request, idempotency_key=idempotency_key)
        self.journal.append(
            session,
            DispatchJournalKind.SESSION_CREATED,
            {
                "run_id": request.run_id,
                "task_id": request.task_id,
                "turn_id": request.turn_id,
                "runtime_worker": request.runtime_worker,
                "idempotency_key": idempotency_key,
                "provider_route_checksum": request.provider_route_checksum,
                "provider_catalog_revision": request.provider_catalog_revision,
                "provider_credential_version": request.provider_credential_version,
                "provider_credential_fingerprint": request.provider_credential_fingerprint,
                "provider_transport_id": request.provider_transport_id,
                "m0_execution_ref": request.m0_execution_ref,
            },
            provider_route_id=request.provider_route_id,
        )
        attempts: list[BackendDispatchAttempt] = []
        events: list[BackendControlEvent] = []
        recovery_inputs: list[BackendRecoveryInput] = []
        transport_responses: list[BackendTransportResponse] = []
        lease = self.registry.acquire(request)
        original_backend_id = lease.backend_id
        previous_envelope_id: str | None = None
        active_handle = None
        try:
            session = transition_session(
                self.store,
                session,
                phase=BackendDispatchPhase.ROUTED,
                backend_id=lease.backend_id,
                lease_id=lease.lease_id,
                metadata={
                    "workspace_root": lease.workspace_root,
                    "artifact_root": lease.artifact_root,
                    "initial_backend_id": lease.backend_id,
                },
            )
            while True:
                definition = self.registry.definition(lease.backend_id)
                attempt_number = len(attempts) + 1
                envelope = self._envelope(
                    lease,
                    definition=definition,
                    idempotency_key=idempotency_key,
                    attempt=attempt_number,
                    previous_envelope_id=previous_envelope_id,
                )
                input_digest = checksum(
                    {
                        "m0_execution_ref": request.m0_execution_ref,
                        "operation": operation_name,
                        "payload": dict(payload),
                    }
                )
                _, side_effect_fence = self.journal.pin_envelope(
                    session,
                    envelope,
                    operation=operation_name,
                    input_digest=input_digest,
                )
                workspace_before = self.workspace_attestation.attest(
                    lease,
                    definition,
                    envelope,
                )
                started_at = now_timestamp()
                attempt = BackendDispatchAttempt(
                    attempt_id=new_backend_id("backend_attempt"),
                    envelope_id=envelope.envelope_id,
                    lease_id=lease.lease_id,
                    backend_id=lease.backend_id,
                    attempt=attempt_number,
                    started_at=started_at,
                    completed_at=None,
                    outcome="running",
                    failure_kind=None,
                    recovery_intent=None,
                    retryable=False,
                    provider_route_changed=False,
                    backend_changed=lease.backend_id != original_backend_id,
                    output_observed=False,
                    reason="transport dispatch started",
                    metadata={
                        "backend_kind": definition.kind.value,
                        "backend_location": definition.location.value,
                        "registry_revision": lease.registry_revision,
                        "health_revision": lease.health_revision,
                        "provider_route_checksum": lease.provider_route_checksum,
                        "provider_catalog_revision": lease.provider_catalog_revision,
                        "provider_credential_version": lease.provider_credential_version,
                        "provider_credential_fingerprint": lease.provider_credential_fingerprint,
                        "provider_transport_id": lease.provider_transport_id,
                        "m0_execution_ref": lease.m0_execution_ref,
                    },
                )
                self.store.put_attempt(attempt)
                attempts.append(attempt)
                session = transition_session(
                    self.store,
                    session,
                    phase=BackendDispatchPhase.CONNECTING,
                    backend_id=lease.backend_id,
                    lease_id=lease.lease_id,
                    envelope_id=envelope.envelope_id,
                    attempt_count=attempt_number,
                )
                started_event = self._event(
                    "backend.dispatch.started",
                    request,
                    session=session,
                    lease=lease,
                    envelope=envelope,
                    attempt=attempt,
                    causation_id=previous_envelope_id or lease.previous_lease_id,
                    payload={
                        "transport_kind": definition.kind.value,
                        "backend_location": definition.location.value,
                        "attempt": attempt_number,
                        "provider_route_changed": False,
                        "backend_changed": lease.backend_id != original_backend_id,
                    },
                )
                events.append(started_event)
                self.store.append_event(started_event)
                cancellation = self.timeouts.create(
                    dispatch_id=envelope.envelope_id,
                    deadline_at=envelope.deadline_at,
                )
                active_handle = self.active.register(
                    session_id=session.session_id,
                    run_id=session.run_id,
                    task_id=session.task_id,
                    turn_id=session.turn_id,
                    backend_id=lease.backend_id,
                    cancellation=cancellation,
                )
                try:
                    session = transition_session(
                        self.store,
                        session,
                        phase=BackendDispatchPhase.RUNNING,
                    )
                    transport = self.transports.create(
                        definition,
                        in_process_operation=in_process_operation,
                    )
                    probe, circuit, _ = self.health_supervisor.probe(
                        definition,
                        in_process_operation=in_process_operation,
                    )
                    if not probe.ok or not self.health_supervisor.permits_dispatch(definition.backend_id):
                        raise BackendDispatchError(
                            BackendFailureKind.BACKEND_UNAVAILABLE,
                            f"backend health probe failed: {probe.status}",
                            retryable=True,
                            recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                            backend_id=definition.backend_id,
                            lease_id=lease.lease_id,
                            provider_route_id=lease.provider_route_id or "",
                            detail={
                                "health_probe": probe.to_dict(),
                                "circuit": circuit.to_dict(),
                            },
                        )
                    response = transport.dispatch(
                        definition,
                        BackendTransportRequest(
                            envelope=envelope,
                            operation=operation_name,
                            payload=dict(payload),
                            input_digest=input_digest,
                        ),
                        cancellation,
                    )
                    if not self.active.accepts_result(session.session_id, active_handle.fence_epoch):
                        raise BackendDispatchError(
                            BackendFailureKind.DISPATCH_ABORTED,
                            "late backend result rejected by dispatch fence",
                            retryable=False,
                            recovery_intent=BackendRecoveryIntent.RECONCILE,
                            backend_id=lease.backend_id,
                            lease_id=lease.lease_id,
                            output_observed=response.output_observed,
                            detail={"transport_receipt_id": response.transport_receipt_id},
                        )
                    transport_responses.append(response)
                    workspace_after = self.workspace_attestation.attest(
                        lease,
                        definition,
                        envelope,
                    )
                    workspace_assessment = self.workspace_attestation.require_safe_completion(
                        workspace_before,
                        workspace_after,
                        allow_entry_mutation=not bool(payload.get("read_only", False)),
                    )
                    materialization = self.journal.materialize_success(
                        session,
                        envelope,
                        response,
                        fence=side_effect_fence,
                    )
                    elapsed_ms = max(0.0, (now_timestamp() - started_at) * 1_000)
                    attempt = replace(
                        attempt,
                        completed_at=now_timestamp(),
                        outcome="succeeded",
                        output_observed=response.output_observed,
                        reason="worker transport completed",
                        metadata={
                            **dict(attempt.metadata),
                            "elapsed_milliseconds": elapsed_ms,
                            "transport_receipt_id": response.transport_receipt_id,
                            "input_digest": response.input_digest,
                            "result_digest": response.result_digest,
                            "request_bytes": response.request_bytes,
                            "response_bytes": response.response_bytes,
                        },
                    )
                    attempts[-1] = attempt
                    self.store.put_attempt(attempt)
                    self.registry.record_success(lease.backend_id, latency_milliseconds=elapsed_ms)
                    session = transition_session(
                        self.store,
                        session,
                        phase=BackendDispatchPhase.SUCCEEDED,
                        output_observed=response.output_observed,
                        terminal_reason="dispatch completed",
                        metadata={
                            "transport_receipt_id": response.transport_receipt_id,
                            "result_digest": response.result_digest,
                            "materialized_session_id": materialization.session_id,
                            "workspace_attestation_before": workspace_before.attestation_id,
                            "workspace_attestation_after": workspace_after.attestation_id,
                            "workspace_entries_changed": workspace_assessment.entry_digest_changed,
                        },
                    )
                    completed_event = self._event(
                        "backend.dispatch.completed",
                        request,
                        session=session,
                        lease=lease,
                        envelope=envelope,
                        attempt=attempt,
                        causation_id=attempt.attempt_id,
                        payload={
                            "elapsed_milliseconds": elapsed_ms,
                            "backend_changed": lease.backend_id != original_backend_id,
                            "provider_route_changed": False,
                            "transport_receipt_id": response.transport_receipt_id,
                            "result_digest": response.result_digest,
                        },
                    )
                    events.append(completed_event)
                    self.store.append_event(completed_event)
                    self.journal.enqueue(
                        session,
                        topic="backend.dispatch.completed",
                        payload=completed_event.to_dict(),
                    )
                    value = response.native_value if response.native_value is not None else response.result
                    return WorkerDispatchResult(
                        value=value,
                        session=session,
                        final_lease=lease,
                        final_envelope=envelope,
                        attempts=tuple(attempts),
                        events=tuple(events),
                        recovery_inputs=tuple(recovery_inputs),
                        transport_responses=tuple(transport_responses),
                        backend_changed=lease.backend_id != original_backend_id,
                    )
                except BaseException as raw_error:
                    error = self._classify(raw_error, lease)
                    self.journal.record_failure(
                        session,
                        envelope,
                        fence=side_effect_fence,
                        failure_kind=error.kind,
                        recovery_intent=error.recovery_intent,
                        retryable=error.retryable,
                        output_observed=error.output_observed,
                        reason=str(error),
                        detail=error.detail,
                    )
                    elapsed_ms = max(0.0, (now_timestamp() - started_at) * 1_000)
                    attempt = replace(
                        attempt,
                        completed_at=now_timestamp(),
                        outcome=(
                            "cancelled"
                            if error.kind is BackendFailureKind.DISPATCH_ABORTED
                            else "failed"
                        ),
                        failure_kind=error.kind,
                        recovery_intent=error.recovery_intent,
                        retryable=error.retryable,
                        output_observed=error.output_observed,
                        reason=str(error),
                        metadata={
                            **dict(attempt.metadata),
                            "elapsed_milliseconds": elapsed_ms,
                            "failure_detail": error.detail,
                        },
                    )
                    attempts[-1] = attempt
                    self.store.put_attempt(attempt)
                    if error.kind is not BackendFailureKind.PROVIDER_FAILURE:
                        self.registry.record_failure(
                            lease.backend_id,
                            kind=error.kind,
                            reason=str(error),
                        )
                    decision = self.failover.decide(
                        error=error,
                        lease=lease,
                        request=request,
                        attempts=attempts,
                        session=self.store.get_dispatch_session(session.session_id) or session,
                    )
                    failed_event = self._event(
                        (
                            "provider.dispatch.failover_required"
                            if error.kind is BackendFailureKind.PROVIDER_FAILURE
                            else "backend.dispatch.cancelled"
                            if error.kind is BackendFailureKind.DISPATCH_ABORTED
                            else "backend.dispatch.failed"
                        ),
                        request,
                        session=session,
                        lease=lease,
                        envelope=envelope,
                        attempt=attempt,
                        causation_id=attempt.attempt_id,
                        payload={
                            "failure": error.safe_dict(),
                            "decision": decision.kind.value,
                            "backend_changed": False,
                            "provider_route_changed": decision.provider_route_changed,
                        },
                    )
                    events.append(failed_event)
                    self.store.append_event(failed_event)
                    next_lease: BackendLease | None = None
                    if decision.kind in {
                        BackendFailoverDecisionKind.CHANGE_BACKEND,
                        BackendFailoverDecisionKind.REBUILD_WORKSPACE,
                    }:
                        if decision.kind is BackendFailoverDecisionKind.REBUILD_WORKSPACE:
                            self.failover.quarantine_workspace(
                                lease=lease,
                                worker_id=request.runtime_worker,
                                reason=str(error),
                            )
                        if decision.delay_seconds:
                            # A per-attempt timeout token is already cancelled at
                            # this point.  Retry delay is governed by durable
                            # session control, not by the expired attempt token.
                            self._session_delay(decision.delay_seconds, session.session_id)
                        previous_lease = lease
                        previous_envelope_id = envelope.envelope_id
                        next_lease = self.failover.acquire_next(
                            decision=decision,
                            request=request,
                            previous_lease=previous_lease,
                        )
                        self.registry.release(
                            previous_lease.lease_id,
                            reason=f"failover:{error.kind.value}",
                        )
                        lease = next_lease
                        recovery = self.failover.recovery_input(
                            decision=decision,
                            request=request,
                            session=session,
                            attempt=attempt,
                            previous_lease=previous_lease,
                            next_lease=next_lease,
                        )
                        self.store.put_recovery_input(recovery)
                        recovery_inputs.append(recovery)
                        self.journal.append(
                            session,
                            DispatchJournalKind.RECOVERY_EMITTED,
                            {
                                "recovery_input_id": recovery.recovery_input_id,
                                "recovery_kind": recovery.kind.value,
                                "previous_backend_id": previous_lease.backend_id,
                                "next_backend_id": next_lease.backend_id,
                                "provider_route_changed": False,
                            },
                            envelope=envelope,
                        )
                        session = transition_session(
                            self.store,
                            session,
                            phase=BackendDispatchPhase.REQUEUED,
                            backend_id=lease.backend_id,
                            lease_id=lease.lease_id,
                            failover_count=session.failover_count + 1,
                            output_observed=error.output_observed,
                            metadata={"last_recovery_input_id": recovery.recovery_input_id},
                        )
                        failover_event = self._event(
                            "backend.failover.committed",
                            request,
                            session=session,
                            lease=lease,
                            envelope=None,
                            attempt=attempt,
                            causation_id=failed_event.event_id,
                            payload={
                                "previous_lease_id": previous_lease.lease_id,
                                "previous_backend_id": previous_lease.backend_id,
                                "backend_id": lease.backend_id,
                                "provider_route_id": lease.provider_route_id,
                                "provider_route_changed": False,
                                "backend_changed": True,
                                "recovery_input_id": recovery.recovery_input_id,
                            },
                        )
                        events.append(failover_event)
                        self.store.append_event(failover_event)
                        self.journal.append(
                            session,
                            DispatchJournalKind.BACKEND_CHANGED,
                            {
                                "previous_backend_id": previous_lease.backend_id,
                                "next_backend_id": next_lease.backend_id,
                                "previous_envelope_id": previous_envelope_id,
                                "recovery_input_id": recovery.recovery_input_id,
                                "provider_route_changed": False,
                            },
                            backend_id=next_lease.backend_id,
                            provider_route_id=next_lease.provider_route_id,
                        )
                        continue
                    recovery = self.failover.recovery_input(
                        decision=decision,
                        request=request,
                        session=session,
                        attempt=attempt,
                        previous_lease=lease,
                        next_lease=None,
                    )
                    self.store.put_recovery_input(recovery)
                    recovery_inputs.append(recovery)
                    terminal_phase = self._terminal_phase(decision)
                    session = transition_session(
                        self.store,
                        session,
                        phase=terminal_phase,
                        output_observed=error.output_observed,
                        terminal_reason=decision.reason,
                        metadata={"last_recovery_input_id": recovery.recovery_input_id},
                    )
                    recovery_event = self._event(
                        "backend.recovery.input.created",
                        request,
                        session=session,
                        lease=lease,
                        envelope=envelope,
                        attempt=attempt,
                        causation_id=failed_event.event_id,
                        payload={
                            "recovery_input_id": recovery.recovery_input_id,
                            "recovery_kind": recovery.kind.value,
                            "replay_safe": recovery.replay_safe,
                            "requires_reconcile": recovery.requires_reconcile,
                        },
                    )
                    events.append(recovery_event)
                    self.store.append_event(recovery_event)
                    self.journal.append(
                        session,
                        (
                            DispatchJournalKind.PROVIDER_CHANGE_REQUIRED
                            if error.kind is BackendFailureKind.PROVIDER_FAILURE
                            else DispatchJournalKind.SESSION_TERMINATED
                        ),
                        {
                            "phase": terminal_phase.value,
                            "failure_kind": error.kind.value,
                            "recovery_intent": error.recovery_intent.value,
                            "recovery_input_id": recovery.recovery_input_id,
                            "output_observed": error.output_observed,
                            "reason": str(error),
                        },
                        envelope=envelope,
                    )
                    self.journal.enqueue(
                        session,
                        topic="backend.recovery.input.created",
                        payload=recovery_event.to_dict(),
                    )
                    raise error from raw_error
                finally:
                    self.timeouts.complete(envelope.envelope_id)
                    if active_handle is not None:
                        self.active.unregister(
                            session.session_id,
                            fence_epoch=active_handle.fence_epoch,
                        )
                        active_handle = None
        finally:
            self.registry.release(lease.lease_id, reason="dispatch_terminal")

    @staticmethod
    def _validate_entry(
        request: BackendSelectionRequest,
        operation_name: str,
        idempotency_key: str,
    ) -> None:
        if not operation_name.strip():
            raise ValueError("operation_name is required")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        required = {
            "provider_route_id": request.provider_route_id,
            "provider_route_checksum": request.provider_route_checksum,
            "provider_credential_fingerprint": request.provider_credential_fingerprint,
            "provider_transport_id": request.provider_transport_id,
            "m0_execution_ref": request.m0_execution_ref,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if request.provider_catalog_revision <= 0:
            missing.append("provider_catalog_revision")
        if request.provider_credential_version <= 0:
            missing.append("provider_credential_version")
        if missing:
            raise BackendDispatchError(
                BackendFailureKind.BACKEND_PROTOCOL,
                "dispatch envelope reference preflight failed",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
                detail={"missing_refs": sorted(set(missing))},
            )

    def _create_session(
        self,
        request: BackendSelectionRequest,
        *,
        idempotency_key: str,
    ) -> BackendDispatchSession:
        created_at = now_timestamp()
        session = BackendDispatchSession(
            session_id=new_backend_id("backend_session"),
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            turn_id=request.turn_id,
            runtime_worker=request.runtime_worker,
            phase=BackendDispatchPhase.CREATED,
            current_backend_id=None,
            current_lease_id=None,
            current_envelope_id=None,
            provider_route_id=request.provider_route_id or "",
            provider_route_checksum=request.provider_route_checksum,
            provider_catalog_revision=request.provider_catalog_revision,
            provider_credential_version=request.provider_credential_version,
            provider_credential_fingerprint=request.provider_credential_fingerprint,
            provider_transport_id=request.provider_transport_id,
            m0_execution_ref=request.m0_execution_ref,
            physical_worker_lease_ref=None,
            idempotency_key=idempotency_key,
            attempt_count=0,
            failover_count=0,
            output_observed=False,
            cancel_requested=False,
            cancel_reason="",
            deadline_at=created_at + max(
                definition.limits.turn_timeout_seconds
                for definition in self.registry.definitions(runtime_worker=request.runtime_worker)
            ),
            created_at=created_at,
            updated_at=created_at,
            revision=1,
            terminal_reason="",
            metadata={
                **dict(request.metadata),
                "workspace_root": request.workspace_root,
                "artifact_root": request.artifact_root,
            },
        )
        return self.store.put_dispatch_session(session, expected_revision=0)

    def _envelope(
        self,
        lease: BackendLease,
        *,
        definition: Any,
        idempotency_key: str,
        attempt: int,
        previous_envelope_id: str | None,
    ) -> BackendDispatchEnvelope:
        from .runtime import build_backend_envelope

        return build_backend_envelope(
            lease,
            definition_kind=definition.kind,
            definition_location=definition.location,
            idempotency_key=idempotency_key,
            attempt=attempt,
            previous_envelope_id=previous_envelope_id,
            timeout_seconds=definition.limits.turn_timeout_seconds,
        )

    @staticmethod
    def _classify(error: BaseException, lease: BackendLease) -> BackendDispatchError:
        from .runtime import classify_dispatch_error

        return classify_dispatch_error(error, lease)

    @staticmethod
    def _terminal_phase(decision: BackendFailoverDecision) -> BackendDispatchPhase:
        if decision.kind is BackendFailoverDecisionKind.CANCEL:
            return BackendDispatchPhase.CANCELLED
        if decision.kind is BackendFailoverDecisionKind.RECONCILE:
            return BackendDispatchPhase.RECONCILE_REQUIRED
        if decision.kind is BackendFailoverDecisionKind.CHANGE_PROVIDER_ROUTE:
            return BackendDispatchPhase.REQUEUED
        return BackendDispatchPhase.FAILED

    @staticmethod
    def _interruptible_delay(seconds: float, cancellation: Any) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            cancellation.throw_if_cancelled()
            time.sleep(min(0.02, remaining))

    def _session_delay(self, seconds: float, session_id: str) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            session = self.store.get_dispatch_session(session_id)
            if session is None or session.phase in {
                BackendDispatchPhase.CANCELLING,
                BackendDispatchPhase.CANCELLED,
                BackendDispatchPhase.FAILED,
            }:
                raise BackendTransportError(
                    BackendFailureKind.DISPATCH_ABORTED,
                    "backend failover delay interrupted by durable control state",
                    retryable=False,
                    recovery_intent=BackendRecoveryIntent.STOP,
                    detail={"session_id": session_id},
                )
            time.sleep(min(0.02, remaining))

    @staticmethod
    def _event(
        event_type: str,
        request: BackendSelectionRequest,
        *,
        session: BackendDispatchSession,
        lease: BackendLease,
        envelope: BackendDispatchEnvelope | None,
        attempt: BackendDispatchAttempt,
        causation_id: str | None,
        payload: Mapping[str, Any],
    ) -> BackendControlEvent:
        body = {
            **dict(payload),
            "dispatch_session_id": session.session_id,
            "dispatch_phase": session.phase.value,
            "dispatch_session_revision": session.revision,
            "attempt_id": attempt.attempt_id,
            "attempt": attempt.attempt,
            "backend_id": lease.backend_id,
            "backend_lease_id": lease.lease_id,
            "provider_route_id": lease.provider_route_id,
            "provider_route_checksum": lease.provider_route_checksum,
            "provider_catalog_revision": lease.provider_catalog_revision,
            "provider_credential_version": lease.provider_credential_version,
            "provider_credential_fingerprint": lease.provider_credential_fingerprint,
            "provider_transport_id": lease.provider_transport_id,
            "m0_execution_ref": lease.m0_execution_ref,
            "physical_worker_lease_ref": lease.physical_worker_lease_ref,
        }
        return BackendControlEvent(
            event_id=new_backend_id("backend_event"),
            event_type=event_type,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            lease_id=lease.lease_id,
            envelope_id=None if envelope is None else envelope.envelope_id,
            backend_id=lease.backend_id,
            provider_route_id=lease.provider_route_id,
            causation_id=causation_id,
            correlation_id=request.turn_id,
            created_at=now_timestamp(),
            payload=body,
            payload_digest=checksum(body),
        )


__all__ = ["WorkerDispatchResult", "WorkerDispatchRouter"]
