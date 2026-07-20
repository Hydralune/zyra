from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Generic, Mapping, TypeVar

from .models import (
    BackendControlEvent,
    BackendDispatchAttempt,
    BackendDispatchEnvelope,
    BackendDispatchError,
    BackendFailureKind,
    BackendLease,
    BackendRecoveryIntent,
    BackendSelectionRequest,
    checksum,
    new_backend_id,
    now_timestamp,
)
from .registry import BackendRegistry


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BackendDispatchOutcome(Generic[T]):
    value: T
    final_lease: BackendLease
    final_envelope: BackendDispatchEnvelope
    attempts: tuple[BackendDispatchAttempt, ...]
    events: tuple[BackendControlEvent, ...]
    backend_changed: bool
    session: Any | None = None
    recovery_inputs: tuple[Any, ...] = ()
    transport_responses: tuple[Any, ...] = ()


class BackendDispatchRuntime:
    """Executes a worker call under a pinned backend lease with bounded failover."""

    def __init__(self, registry: BackendRegistry) -> None:
        self.registry = registry

    def dispatch_callable(
        self,
        request: BackendSelectionRequest,
        operation: Callable[[BackendDispatchEnvelope], T],
        *,
        idempotency_key: str,
        interruptible: bool = False,
    ) -> BackendDispatchOutcome[T]:
        from .router import WorkerDispatchRouter

        routed = WorkerDispatchRouter(self.registry).dispatch_callable(
            request,
            operation,
            idempotency_key=idempotency_key,
            interruptible=interruptible,
        )
        return BackendDispatchOutcome(
            value=routed.value,
            final_lease=routed.final_lease,
            final_envelope=routed.final_envelope,
            attempts=routed.attempts,
            events=routed.events,
            backend_changed=routed.backend_changed,
            session=routed.session,
            recovery_inputs=routed.recovery_inputs,
            transport_responses=routed.transport_responses,
        )

    def _dispatch_callable_foundation(
        self,
        request: BackendSelectionRequest,
        operation: Callable[[BackendDispatchEnvelope], T],
        *,
        idempotency_key: str,
        interruptible: bool = False,
    ) -> BackendDispatchOutcome[T]:
        """05D-01 reference path retained for conformance, never the default."""
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        attempts: list[BackendDispatchAttempt] = []
        events: list[BackendControlEvent] = []
        lease = self.registry.acquire(request)
        original_backend_id = lease.backend_id
        previous_envelope_id: str | None = None
        try:
            while True:
                definition = self.registry.definition(lease.backend_id)
                attempt_number = len(attempts) + 1
                envelope = build_backend_envelope(
                    lease,
                    definition_kind=definition.kind,
                    definition_location=definition.location,
                    idempotency_key=idempotency_key,
                    attempt=attempt_number,
                    previous_envelope_id=previous_envelope_id,
                    timeout_seconds=definition.limits.turn_timeout_seconds,
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
                    reason="dispatch started",
                    metadata={
                        "backend_kind": definition.kind.value,
                        "backend_location": definition.location.value,
                        "registry_revision": lease.registry_revision,
                        "health_revision": lease.health_revision,
                    },
                )
                self.registry.store.put_attempt(attempt)
                attempts.append(attempt)
                events.append(
                    self._event(
                        "backend.dispatch.started",
                        request,
                        lease=lease,
                        envelope=envelope,
                        payload={
                            "attempt_id": attempt.attempt_id,
                            "attempt": attempt_number,
                            "backend_kind": definition.kind.value,
                            "backend_location": definition.location.value,
                            "provider_route_id": lease.provider_route_id,
                        },
                        causation_id=(previous_envelope_id or lease.previous_lease_id),
                    )
                )
                try:
                    value = self._invoke(
                        operation,
                        envelope,
                        timeout_seconds=definition.limits.turn_timeout_seconds,
                        interruptible=interruptible,
                    )
                    elapsed_ms = max(0.0, (now_timestamp() - started_at) * 1_000.0)
                    attempt = replace(
                        attempt,
                        completed_at=now_timestamp(),
                        outcome="succeeded",
                        reason="worker callable completed",
                        metadata={**dict(attempt.metadata), "elapsed_milliseconds": elapsed_ms},
                    )
                    attempts[-1] = attempt
                    self.registry.store.put_attempt(attempt)
                    self.registry.record_success(lease.backend_id, latency_milliseconds=elapsed_ms)
                    completed = self._event(
                        "backend.dispatch.completed",
                        request,
                        lease=lease,
                        envelope=envelope,
                        payload={
                            "attempt_id": attempt.attempt_id,
                            "attempt": attempt_number,
                            "elapsed_milliseconds": elapsed_ms,
                            "backend_changed": lease.backend_id != original_backend_id,
                            "provider_route_changed": False,
                        },
                        causation_id=attempt.attempt_id,
                    )
                    events.append(completed)
                    return BackendDispatchOutcome(
                        value=value,
                        final_lease=lease,
                        final_envelope=envelope,
                        attempts=tuple(attempts),
                        events=tuple(events),
                        backend_changed=lease.backend_id != original_backend_id,
                    )
                except BaseException as raw_error:
                    error = classify_dispatch_error(raw_error, lease)
                    elapsed_ms = max(0.0, (now_timestamp() - started_at) * 1_000.0)
                    attempt = replace(
                        attempt,
                        completed_at=now_timestamp(),
                        outcome="failed",
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
                    self.registry.store.put_attempt(attempt)
                    if error.kind is not BackendFailureKind.PROVIDER_FAILURE:
                        self.registry.record_failure(
                            lease.backend_id,
                            kind=error.kind,
                            reason=str(error),
                        )
                    events.append(
                        self._event(
                            "provider.dispatch.failover_required"
                            if error.kind is BackendFailureKind.PROVIDER_FAILURE
                            else "backend.dispatch.failed",
                            request,
                            lease=lease,
                            envelope=envelope,
                            payload={
                                "attempt_id": attempt.attempt_id,
                                "failure": error.safe_dict(),
                                "backend_changed": False,
                                "provider_route_changed": False,
                            },
                            causation_id=attempt.attempt_id,
                        )
                    )
                    if not self._can_failover(error, lease, attempts):
                        raise error from raw_error
                    previous_envelope_id = envelope.envelope_id
                    previous_lease_id = lease.lease_id
                    next_lease = self.registry.acquire(
                        request,
                        previous_lease_id=previous_lease_id,
                    )
                    self.registry.release(
                        previous_lease_id,
                        reason=f"failover:{error.kind.value}",
                    )
                    lease = next_lease
                    events.append(
                        self._event(
                            "backend.failover.committed",
                            request,
                            lease=lease,
                            envelope=None,
                            payload={
                                "previous_lease_id": previous_lease_id,
                                "previous_backend_id": attempt.backend_id,
                                "backend_id": lease.backend_id,
                                "reason": error.kind.value,
                                "provider_route_id": lease.provider_route_id,
                                "provider_route_changed": False,
                                "backend_changed": True,
                            },
                            causation_id=attempt.attempt_id,
                        )
                    )
        finally:
            self.registry.release(lease.lease_id, reason="dispatch_terminal")
            for event in events:
                # Events are appended only once, after their causal payload is complete.
                if not any(
                    existing.event_id == event.event_id
                    for existing in self.registry.store.events(
                        run_id=event.run_id,
                        task_id=event.task_id,
                    )
                ):
                    self.registry.store.append_event(event)

    @staticmethod
    def _invoke(
        operation: Callable[[BackendDispatchEnvelope], T],
        envelope: BackendDispatchEnvelope,
        *,
        timeout_seconds: float,
        interruptible: bool,
    ) -> T:
        if not interruptible:
            started = time.monotonic()
            value = operation(envelope)
            elapsed = time.monotonic() - started
            if elapsed > timeout_seconds:
                # Output has already been observed; never replay a potentially
                # side-effecting in-process operation after a late timeout.
                raise BackendDispatchError(
                    BackendFailureKind.TURN_TIMEOUT,
                    f"worker turn exceeded {timeout_seconds:.3f}s after completion",
                    retryable=False,
                    recovery_intent=BackendRecoveryIntent.RECONCILE,
                    backend_id=envelope.backend_id,
                    lease_id=envelope.backend_lease_id,
                    provider_route_id=envelope.provider_route_id or "",
                    output_observed=True,
                )
            return value

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"zyra-{envelope.backend_id}",
        )
        future = executor.submit(operation, envelope)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise BackendDispatchError(
                BackendFailureKind.TURN_TIMEOUT,
                f"worker turn timed out after {timeout_seconds:.3f}s",
                retryable=True,
                recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                backend_id=envelope.backend_id,
                lease_id=envelope.backend_lease_id,
                provider_route_id=envelope.provider_route_id or "",
            ) from error
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _can_failover(
        error: BackendDispatchError,
        lease: BackendLease,
        attempts: list[BackendDispatchAttempt],
    ) -> bool:
        if error.kind is BackendFailureKind.PROVIDER_FAILURE:
            return False
        if error.output_observed or not error.retryable:
            return False
        if error.recovery_intent not in {
            BackendRecoveryIntent.CHANGE_BACKEND,
            BackendRecoveryIntent.REBUILD_WORKSPACE,
        }:
            return False
        return len(attempts) < lease.attempt_limit

    @staticmethod
    def _event(
        event_type: str,
        request: BackendSelectionRequest,
        *,
        lease: BackendLease,
        envelope: BackendDispatchEnvelope | None,
        payload: Mapping[str, Any],
        causation_id: str | None,
    ) -> BackendControlEvent:
        event_payload = dict(payload)
        return BackendControlEvent(
            event_id=new_backend_id("backend_event"),
            event_type=event_type,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            lease_id=lease.lease_id,
            envelope_id=(None if envelope is None else envelope.envelope_id),
            backend_id=lease.backend_id,
            provider_route_id=lease.provider_route_id,
            causation_id=causation_id,
            correlation_id=request.turn_id,
            created_at=now_timestamp(),
            payload=event_payload,
            payload_digest=checksum(event_payload),
        )


def build_backend_envelope(
    lease: BackendLease,
    *,
    definition_kind: Any,
    definition_location: Any,
    idempotency_key: str,
    attempt: int,
    previous_envelope_id: str | None,
    timeout_seconds: float,
) -> BackendDispatchEnvelope:
    created_at = now_timestamp()
    body = {
        "schema": "zyra.backend-dispatch-envelope/v2",
        "envelope_id": new_backend_id("backend_dispatch"),
        "run_id": lease.run_id,
        "task_id": lease.task_id,
        "node_id": lease.node_id,
        "turn_id": lease.turn_id,
        "runtime_worker": lease.runtime_worker,
        "backend_lease_id": lease.lease_id,
        "backend_id": lease.backend_id,
        "backend_kind": definition_kind,
        "backend_location": definition_location,
        "workspace_root": lease.workspace_root,
        "artifact_root": lease.artifact_root,
        "provider_route_id": lease.provider_route_id,
        "provider_route_checksum": lease.provider_route_checksum,
        "provider_catalog_revision": lease.provider_catalog_revision,
        "provider_credential_version": lease.provider_credential_version,
        "provider_credential_fingerprint": lease.provider_credential_fingerprint,
        "provider_transport_id": lease.provider_transport_id,
        "m0_execution_ref": lease.m0_execution_ref,
        "physical_worker_lease_ref": lease.physical_worker_lease_ref,
        "idempotency_key": idempotency_key,
        "deadline_at": created_at + timeout_seconds,
        "attempt": attempt,
        "previous_envelope_id": previous_envelope_id,
        "created_at": created_at,
        "metadata": {
            "registry_revision": lease.registry_revision,
            "health_revision": lease.health_revision,
            "provider_state_embedded": False,
            "provider_route_is_opaque_reference": True,
            "provider_route_checksum": lease.provider_route_checksum,
            "provider_catalog_revision": lease.provider_catalog_revision,
            "provider_credential_version": lease.provider_credential_version,
            "provider_credential_fingerprint": lease.provider_credential_fingerprint,
            "provider_transport_id": lease.provider_transport_id,
            "m0_execution_ref": lease.m0_execution_ref,
            "physical_worker_lease_ref": lease.physical_worker_lease_ref,
        },
    }
    digest_body = {
        **body,
        "backend_kind": definition_kind.value,
        "backend_location": definition_location.value,
    }
    return BackendDispatchEnvelope(**body, checksum=checksum(digest_body))


def classify_dispatch_error(
    error: BaseException,
    lease: BackendLease,
) -> BackendDispatchError:
    if isinstance(error, BackendDispatchError):
        return error
    code = str(getattr(error, "code", "") or "")
    provider_codes = {
        "authentication_failed",
        "authorization_failed",
        "rate_limited",
        "usage_limited",
        "provider_unavailable",
        "provider_timeout",
        "stream_timeout",
        "context_overflow",
        "request_too_large",
        "response_protocol_error",
        "partial_response_observed",
        "credential_expired",
        "credential_revoked",
        "credential_blocked",
    }
    if code in provider_codes:
        detail = dict(getattr(error, "detail", {}) or {})
        output_observed = bool(detail.get("outputObserved", False))
        return BackendDispatchError(
            BackendFailureKind.PROVIDER_FAILURE,
            str(error),
            retryable=False,
            recovery_intent=(
                BackendRecoveryIntent.RECONCILE
                if output_observed
                else BackendRecoveryIntent.CHANGE_PROVIDER_ROUTE
            ),
            backend_id=lease.backend_id,
            lease_id=lease.lease_id,
            provider_route_id=lease.provider_route_id or "",
            output_observed=output_observed,
            detail={"provider_failure_code": code, **detail},
        )
    if isinstance(error, (FileNotFoundError, ConnectionError, BrokenPipeError)):
        return BackendDispatchError(
            BackendFailureKind.BACKEND_UNAVAILABLE,
            str(error),
            retryable=True,
            recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
            backend_id=lease.backend_id,
            lease_id=lease.lease_id,
            provider_route_id=lease.provider_route_id or "",
        )
    return BackendDispatchError(
        BackendFailureKind.EXECUTION_FAILED,
        f"{type(error).__name__}: {error}",
        retryable=False,
        recovery_intent=BackendRecoveryIntent.NONE,
        backend_id=lease.backend_id,
        lease_id=lease.lease_id,
        provider_route_id=lease.provider_route_id or "",
    )
