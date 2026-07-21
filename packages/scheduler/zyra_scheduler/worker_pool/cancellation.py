from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .inbox import WorkerInboxRuntime
from .leases import WorkerLeaseManager
from .models import (
    AttemptState,
    CancellationReceipt,
    CancellationRequest,
    LeaseState,
)
from .store import WorkerPoolStore


class LogicalTaskCancellationPort(Protocol):
    def cancel_logical_task(self, task_id: str, *, reason: str, actor_id: str) -> bool: ...


class BackendCancellationPort(Protocol):
    def cancel_dispatch(self, dispatch_id: str, *, reason: str) -> bool: ...


class GatewayCancellationPort(Protocol):
    def cancel_command(self, command_id: str, *, reason: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class CancellationPorts:
    logical_task: LogicalTaskCancellationPort | None = None
    backend: BackendCancellationPort | None = None
    gateway: GatewayCancellationPort | None = None


class WorkerCancellationRuntime:
    """Coordinates real cancellation across logical and physical state owners."""

    def __init__(
        self,
        store: WorkerPoolStore,
        leases: WorkerLeaseManager,
        inbox: WorkerInboxRuntime,
        *,
        ports: CancellationPorts | None = None,
    ) -> None:
        self.store = store
        self.leases = leases
        self.inbox = inbox
        self.ports = ports or CancellationPorts()

    def cancel(self, request: CancellationRequest) -> CancellationReceipt:
        existing = self.store.cancellation_receipt(request.request_id)
        if existing is not None:
            return existing
        attempts = self.store.list_attempts(task_id=request.task_id)
        if request.attempt_id:
            attempts = tuple(item for item in attempts if item.attempt_id == request.attempt_id)
        leases = self.store.list_leases(task_id=request.task_id)
        if request.lease_id:
            leases = tuple(item for item in leases if item.lease_id == request.lease_id)
        active_lease_by_attempt = {
            lease.attempt_id: lease
            for lease in leases
            if lease.state in {LeaseState.ACTIVE, LeaseState.DRAINING}
        }
        cancelled_attempts: list[str] = []
        cancelled_leases: list[str] = []
        backend_cancelled: list[str] = []
        gateway_cancelled: list[str] = []
        failed: dict[str, str] = {}
        logical_changed = False
        if self.ports.logical_task is not None:
            try:
                logical_changed = bool(
                    self.ports.logical_task.cancel_logical_task(
                        request.task_id,
                        reason=request.reason,
                        actor_id=request.actor_id,
                    )
                )
            except Exception as error:
                failed["logical_task"] = f"{type(error).__name__}: {error}"
        for attempt in attempts:
            if attempt.backend_dispatch_id and self.ports.backend is not None:
                try:
                    if self.ports.backend.cancel_dispatch(
                        attempt.backend_dispatch_id,
                        reason=request.reason,
                    ):
                        backend_cancelled.append(attempt.backend_dispatch_id)
                except Exception as error:
                    failed[f"backend:{attempt.backend_dispatch_id}"] = f"{type(error).__name__}: {error}"
            command_id = str(attempt.metadata.get("gateway_command_id") or "")
            if command_id and self.ports.gateway is not None:
                try:
                    if self.ports.gateway.cancel_command(command_id, reason=request.reason):
                        gateway_cancelled.append(command_id)
                except Exception as error:
                    failed[f"gateway:{command_id}"] = f"{type(error).__name__}: {error}"
            lease = active_lease_by_attempt.get(attempt.attempt_id)
            if lease is not None:
                try:
                    _, cancelled_attempt = self.leases.cancel(lease.lease_id, reason=request.reason)
                    cancelled_leases.append(lease.lease_id)
                    cancelled_attempts.append(cancelled_attempt.attempt_id)
                except Exception as error:
                    failed[f"lease:{lease.lease_id}"] = f"{type(error).__name__}: {error}"
            elif attempt.state in {AttemptState.PENDING, AttemptState.LEASED, AttemptState.RUNNING}:
                failed[f"attempt:{attempt.attempt_id}"] = "active attempt has no cancellable lease"
        inbox_cancelled = self.inbox.cancel_task_messages(request.task_id, reason=request.reason)
        changed = bool(
            logical_changed
            or cancelled_attempts
            or cancelled_leases
            or backend_cancelled
            or gateway_cancelled
            or inbox_cancelled
        )
        receipt = CancellationReceipt(
            request_id=request.request_id,
            task_id=request.task_id,
            accepted=not failed or changed,
            changed=changed,
            cancelled_attempt_ids=tuple(cancelled_attempts),
            cancelled_lease_ids=tuple(cancelled_leases),
            backend_cancelled=tuple(backend_cancelled),
            gateway_cancelled=tuple(gateway_cancelled),
            failed_targets=failed,
            metadata={
                "logical_task_changed": logical_changed,
                "inbox_cancelled": [item.envelope_id for item in inbox_cancelled],
                "cancellation_owner": "WorkerCancellationRuntime",
            },
        )
        return self.store.append_cancellation_receipt(receipt)
