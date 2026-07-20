from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .models import (
    BackendControlAction,
    BackendControlRequest,
    BackendDispatchPhase,
    BackendDispatchSession,
    BackendRecoveryInput,
    RecoveryInputKind,
    new_backend_id,
    now_timestamp,
)
from .store import BackendRegistryStore
from .transport import DispatchCancellation


@dataclass(frozen=True, slots=True)
class BackendControlReceipt:
    control_id: str
    action: BackendControlAction
    run_id: str
    task_id: str
    matched_session_ids: tuple[str, ...]
    interrupted_session_ids: tuple[str, ...]
    persisted: bool
    effective: bool
    reason: str
    created_at: float
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "action": self.action.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "matched_session_ids": list(self.matched_session_ids),
            "interrupted_session_ids": list(self.interrupted_session_ids),
            "persisted": self.persisted,
            "effective": self.effective,
            "reason": self.reason,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ActiveDispatchHandle:
    session_id: str
    run_id: str
    task_id: str
    turn_id: str
    backend_id: str
    cancellation: DispatchCancellation
    registered_at: float
    fence_epoch: int
    late_result_handler: Callable[[str, Any], None] | None = None


class ActiveDispatchRegistry:
    """Process-live handle registry; durable intent remains in BackendRegistryStore."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handles: dict[str, ActiveDispatchHandle] = {}
        self._fence_epochs: dict[str, int] = {}

    def register(
        self,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        turn_id: str,
        backend_id: str,
        cancellation: DispatchCancellation,
        late_result_handler: Callable[[str, Any], None] | None = None,
    ) -> ActiveDispatchHandle:
        with self._lock:
            if session_id in self._handles:
                raise RuntimeError(f"dispatch session already active: {session_id}")
            epoch = self._fence_epochs.get(session_id, 0) + 1
            self._fence_epochs[session_id] = epoch
            handle = ActiveDispatchHandle(
                session_id=session_id,
                run_id=run_id,
                task_id=task_id,
                turn_id=turn_id,
                backend_id=backend_id,
                cancellation=cancellation,
                registered_at=time.time(),
                fence_epoch=epoch,
                late_result_handler=late_result_handler,
            )
            self._handles[session_id] = handle
            return handle

    def update_backend(self, session_id: str, backend_id: str) -> ActiveDispatchHandle:
        with self._lock:
            handle = self.require(session_id)
            handle.backend_id = backend_id
            return handle

    def unregister(self, session_id: str, *, fence_epoch: int) -> bool:
        with self._lock:
            handle = self._handles.get(session_id)
            if handle is None or handle.fence_epoch != fence_epoch:
                return False
            del self._handles[session_id]
            return True

    def require(self, session_id: str) -> ActiveDispatchHandle:
        with self._lock:
            handle = self._handles.get(session_id)
            if handle is None:
                raise KeyError(f"active dispatch session not found: {session_id}")
            return handle

    def find(
        self,
        *,
        run_id: str,
        task_id: str,
        turn_id: str | None = None,
        session_id: str | None = None,
        backend_id: str | None = None,
    ) -> tuple[ActiveDispatchHandle, ...]:
        with self._lock:
            values = tuple(self._handles.values())
        return tuple(
            handle
            for handle in values
            if handle.run_id == run_id
            and handle.task_id == task_id
            and (turn_id is None or handle.turn_id == turn_id)
            and (session_id is None or handle.session_id == session_id)
            and (backend_id is None or handle.backend_id == backend_id)
        )

    def cancel(self, session_id: str, reason: str) -> bool:
        try:
            handle = self.require(session_id)
        except KeyError:
            return False
        return handle.cancellation.cancel(reason)

    def accepts_result(self, session_id: str, fence_epoch: int) -> bool:
        with self._lock:
            return self._fence_epochs.get(session_id) == fence_epoch and not (
                self._handles.get(session_id) is not None
                and self._handles[session_id].cancellation.cancelled
            )

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            values = tuple(self._handles.values())
        return tuple(
            {
                "session_id": item.session_id,
                "run_id": item.run_id,
                "task_id": item.task_id,
                "turn_id": item.turn_id,
                "backend_id": item.backend_id,
                "registered_at": item.registered_at,
                "fence_epoch": item.fence_epoch,
                "cancelled": item.cancellation.cancelled,
                "cancel_reason": item.cancellation.reason,
                "deadline_at": item.cancellation.deadline_at,
            }
            for item in values
        )


ACTIVE_DISPATCHES = ActiveDispatchRegistry()


class BackendControlRuntime:
    def __init__(
        self,
        store: BackendRegistryStore,
        *,
        active: ActiveDispatchRegistry | None = None,
    ) -> None:
        self.store = store
        self.active = active or ACTIVE_DISPATCHES
        self._lock = threading.RLock()

    def submit(
        self,
        *,
        action: BackendControlAction,
        run_id: str,
        task_id: str,
        reason: str,
        requested_by: str,
        idempotency_key: str,
        turn_id: str | None = None,
        dispatch_session_id: str | None = None,
        backend_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> BackendControlReceipt:
        for name, value in {
            "run_id": run_id,
            "task_id": task_id,
            "reason": reason,
            "requested_by": requested_by,
            "idempotency_key": idempotency_key,
        }.items():
            if not str(value).strip():
                raise ValueError(f"{name} is required")
        request = BackendControlRequest(
            control_id=new_backend_id("backend_control"),
            action=action,
            run_id=run_id,
            task_id=task_id,
            turn_id=turn_id,
            dispatch_session_id=dispatch_session_id,
            backend_id=backend_id,
            reason=reason,
            requested_at=now_timestamp(),
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            metadata=dict(metadata or {}),
        )
        stored = self.store.put_control_request(request)
        # Repeated idempotent calls use the canonical stored identity.
        request = stored
        if action is BackendControlAction.CANCEL:
            return self._cancel(request)
        if action is BackendControlAction.REQUEUE:
            return self._requeue(request)
        if action is BackendControlAction.QUARANTINE:
            return self._quarantine(request)
        if action is BackendControlAction.RELEASE_QUARANTINE:
            return self._release_quarantine(request)
        if action is BackendControlAction.DRAIN:
            return self._drain(request)
        if action is BackendControlAction.RESUME:
            return self._resume(request)
        raise ValueError(f"unsupported backend control action: {action.value}")

    def cancel_task(
        self,
        *,
        run_id: str,
        task_id: str,
        reason: str,
        requested_by: str = "task-control-api",
        idempotency_key: str | None = None,
    ) -> BackendControlReceipt:
        return self.submit(
            action=BackendControlAction.CANCEL,
            run_id=run_id,
            task_id=task_id,
            reason=reason,
            requested_by=requested_by,
            idempotency_key=idempotency_key or f"cancel:{run_id}:{task_id}:{reason}",
        )

    def _cancel(self, request: BackendControlRequest) -> BackendControlReceipt:
        sessions = self._matching_sessions(request, active_only=True)
        interrupted: list[str] = []
        for session in sessions:
            updated = self._transition_cancelling(session, request.reason)
            if self.active.cancel(session.session_id, request.reason):
                interrupted.append(session.session_id)
            self._ensure_cancel_recovery_input(updated, request)
        return self._receipt(request, sessions, interrupted, effective=bool(sessions))

    def _requeue(self, request: BackendControlRequest) -> BackendControlReceipt:
        sessions = self._matching_sessions(request, active_only=True)
        interrupted: list[str] = []
        for session in sessions:
            if session.output_observed:
                updated = self._transition(
                    session,
                    phase=BackendDispatchPhase.RECONCILE_REQUIRED,
                    terminal_reason="requeue rejected after observable output",
                    metadata={"control_id": request.control_id, "requeue_rejected": True},
                )
            else:
                updated = self._transition(
                    session,
                    phase=BackendDispatchPhase.REQUEUED,
                    terminal_reason=request.reason,
                    metadata={"control_id": request.control_id, "requeue_requested": True},
                )
                if self.active.cancel(session.session_id, f"requeue:{request.reason}"):
                    interrupted.append(session.session_id)
            self._ensure_requeue_recovery_input(updated, request)
        return self._receipt(request, sessions, interrupted, effective=bool(sessions))

    def _quarantine(self, request: BackendControlRequest) -> BackendControlReceipt:
        if request.backend_id is None:
            raise ValueError("backend_id is required for quarantine control")
        sessions = self._matching_sessions(request, active_only=True)
        interrupted: list[str] = []
        for session in sessions:
            if self.active.cancel(session.session_id, f"backend quarantined:{request.reason}"):
                interrupted.append(session.session_id)
            self._transition_cancelling(session, request.reason)
        return self._receipt(request, sessions, interrupted, effective=True)

    def _release_quarantine(self, request: BackendControlRequest) -> BackendControlReceipt:
        workspace_root = str(request.metadata.get("workspace_root") or "")
        if not workspace_root:
            raise ValueError("workspace_root metadata is required to release quarantine")
        count = self.store.release_workspace_quarantine(
            workspace_root,
            backend_id=request.backend_id or "",
            released_at=now_timestamp(),
        )
        return self._receipt(request, (), (), effective=count > 0, metadata={"released": count})

    def _drain(self, request: BackendControlRequest) -> BackendControlReceipt:
        if request.backend_id is None:
            raise ValueError("backend_id is required for drain control")
        sessions = self._matching_sessions(request, active_only=True)
        interrupted: list[str] = []
        for session in sessions:
            if session.output_observed:
                continue
            if self.active.cancel(session.session_id, f"backend drain:{request.reason}"):
                interrupted.append(session.session_id)
            updated = self._transition(
                session,
                phase=BackendDispatchPhase.REQUEUED,
                terminal_reason=request.reason,
                metadata={"control_id": request.control_id, "drained_backend": request.backend_id},
            )
            self._ensure_requeue_recovery_input(updated, request)
        return self._receipt(request, sessions, interrupted, effective=bool(sessions))

    def _resume(self, request: BackendControlRequest) -> BackendControlReceipt:
        sessions = self._matching_sessions(request, active_only=False)
        resumed: list[BackendDispatchSession] = []
        for session in sessions:
            if session.phase is not BackendDispatchPhase.REQUEUED:
                continue
            resumed.append(
                self._transition(
                    session,
                    phase=BackendDispatchPhase.CREATED,
                    terminal_reason="",
                    metadata={"control_id": request.control_id, "resume_requested": True},
                )
            )
        return self._receipt(request, resumed, (), effective=bool(resumed))

    def recover_stranded_sessions(self, *, now: float | None = None) -> tuple[BackendRecoveryInput, ...]:
        current = now_timestamp() if now is None else now
        outputs: list[BackendRecoveryInput] = []
        for session in self.store.dispatch_sessions(active_only=True):
            if session.session_id in {item["session_id"] for item in self.active.snapshot()}:
                continue
            if session.phase not in {
                BackendDispatchPhase.CONNECTING,
                BackendDispatchPhase.RUNNING,
                BackendDispatchPhase.CANCELLING,
                BackendDispatchPhase.ROUTED,
            }:
                continue
            requires_reconcile = session.output_observed or session.phase is BackendDispatchPhase.CANCELLING
            next_phase = (
                BackendDispatchPhase.RECONCILE_REQUIRED
                if requires_reconcile
                else BackendDispatchPhase.REQUEUED
            )
            updated = self._transition(
                session,
                phase=next_phase,
                terminal_reason="dispatch process restarted without active transport handle",
                metadata={"restart_recovered_at": current},
            )
            recovery = BackendRecoveryInput(
                recovery_input_id=new_backend_id("backend_recovery"),
                kind=(
                    RecoveryInputKind.RECONCILE_PARTIAL_OUTPUT
                    if requires_reconcile
                    else RecoveryInputKind.BACKEND_FAILOVER
                ),
                run_id=updated.run_id,
                task_id=updated.task_id,
                node_id=updated.node_id,
                turn_id=updated.turn_id,
                dispatch_session_id=updated.session_id,
                attempt_id=None,
                previous_backend_id=updated.current_backend_id,
                next_backend_id=None,
                previous_provider_route_id=updated.provider_route_id,
                next_provider_route_id=updated.provider_route_id,
                m0_execution_ref=updated.m0_execution_ref,
                workspace_root=str(updated.metadata.get("workspace_root") or ""),
                reason_code="dispatch_restart_recovery",
                reason=updated.terminal_reason,
                replay_safe=not requires_reconcile,
                requires_reconcile=requires_reconcile,
                consumed=False,
                created_at=current,
                metadata={
                    "provider_route_checksum": updated.provider_route_checksum,
                    "provider_catalog_revision": updated.provider_catalog_revision,
                    "provider_credential_version": updated.provider_credential_version,
                    "provider_credential_fingerprint": updated.provider_credential_fingerprint,
                    "provider_transport_id": updated.provider_transport_id,
                },
            )
            outputs.append(self.store.put_recovery_input(recovery))
        return tuple(outputs)

    def _matching_sessions(
        self,
        request: BackendControlRequest,
        *,
        active_only: bool,
    ) -> tuple[BackendDispatchSession, ...]:
        return tuple(
            session
            for session in self.store.dispatch_sessions(
                run_id=request.run_id,
                task_id=request.task_id,
                active_only=active_only,
            )
            if (request.turn_id is None or session.turn_id == request.turn_id)
            and (
                request.dispatch_session_id is None
                or session.session_id == request.dispatch_session_id
            )
            and (
                request.backend_id is None
                or session.current_backend_id == request.backend_id
            )
        )

    def _transition_cancelling(
        self,
        session: BackendDispatchSession,
        reason: str,
    ) -> BackendDispatchSession:
        if session.terminal:
            return session
        return self._transition(
            session,
            phase=BackendDispatchPhase.CANCELLING,
            cancel_requested=True,
            cancel_reason=reason,
            terminal_reason=reason,
        )

    def _transition(
        self,
        session: BackendDispatchSession,
        *,
        phase: BackendDispatchPhase,
        cancel_requested: bool | None = None,
        cancel_reason: str | None = None,
        terminal_reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> BackendDispatchSession:
        current = self.store.get_dispatch_session(session.session_id)
        if current is None:
            raise KeyError(f"dispatch session not found: {session.session_id}")
        merged_metadata = dict(current.metadata)
        merged_metadata.update(dict(metadata or {}))
        updated = replace(
            current,
            phase=phase,
            cancel_requested=(
                current.cancel_requested
                if cancel_requested is None
                else cancel_requested
            ),
            cancel_reason=(
                current.cancel_reason if cancel_reason is None else cancel_reason
            ),
            terminal_reason=(
                current.terminal_reason
                if terminal_reason is None
                else terminal_reason
            ),
            updated_at=now_timestamp(),
            revision=current.revision + 1,
            metadata=merged_metadata,
        )
        return self.store.put_dispatch_session(updated, expected_revision=current.revision)

    def _ensure_cancel_recovery_input(
        self,
        session: BackendDispatchSession,
        request: BackendControlRequest,
    ) -> BackendRecoveryInput:
        existing = [
            item
            for item in self.store.recovery_inputs(run_id=session.run_id, task_id=session.task_id)
            if item.dispatch_session_id == session.session_id
            and item.kind is RecoveryInputKind.CONTROL_CANCEL
            and item.metadata.get("control_id") == request.control_id
        ]
        if existing:
            return existing[-1]
        recovery = BackendRecoveryInput(
            recovery_input_id=new_backend_id("backend_recovery"),
            kind=RecoveryInputKind.CONTROL_CANCEL,
            run_id=session.run_id,
            task_id=session.task_id,
            node_id=session.node_id,
            turn_id=session.turn_id,
            dispatch_session_id=session.session_id,
            attempt_id=None,
            previous_backend_id=session.current_backend_id,
            next_backend_id=None,
            previous_provider_route_id=session.provider_route_id,
            next_provider_route_id=session.provider_route_id,
            m0_execution_ref=session.m0_execution_ref,
            workspace_root=str(session.metadata.get("workspace_root") or ""),
            reason_code="control_cancel",
            reason=request.reason,
            replay_safe=False,
            requires_reconcile=session.output_observed,
            consumed=False,
            created_at=now_timestamp(),
            metadata={"control_id": request.control_id, "requested_by": request.requested_by},
        )
        return self.store.put_recovery_input(recovery)

    def _ensure_requeue_recovery_input(
        self,
        session: BackendDispatchSession,
        request: BackendControlRequest,
    ) -> BackendRecoveryInput:
        kind = (
            RecoveryInputKind.RECONCILE_PARTIAL_OUTPUT
            if session.output_observed
            else RecoveryInputKind.BACKEND_FAILOVER
        )
        recovery = BackendRecoveryInput(
            recovery_input_id=new_backend_id("backend_recovery"),
            kind=kind,
            run_id=session.run_id,
            task_id=session.task_id,
            node_id=session.node_id,
            turn_id=session.turn_id,
            dispatch_session_id=session.session_id,
            attempt_id=None,
            previous_backend_id=session.current_backend_id,
            next_backend_id=None,
            previous_provider_route_id=session.provider_route_id,
            next_provider_route_id=session.provider_route_id,
            m0_execution_ref=session.m0_execution_ref,
            workspace_root=str(session.metadata.get("workspace_root") or ""),
            reason_code="control_requeue",
            reason=request.reason,
            replay_safe=not session.output_observed,
            requires_reconcile=session.output_observed,
            consumed=False,
            created_at=now_timestamp(),
            metadata={"control_id": request.control_id, "requested_by": request.requested_by},
        )
        return self.store.put_recovery_input(recovery)

    @staticmethod
    def _receipt(
        request: BackendControlRequest,
        sessions: tuple[BackendDispatchSession, ...] | list[BackendDispatchSession],
        interrupted: tuple[str, ...] | list[str],
        *,
        effective: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> BackendControlReceipt:
        return BackendControlReceipt(
            control_id=request.control_id,
            action=request.action,
            run_id=request.run_id,
            task_id=request.task_id,
            matched_session_ids=tuple(item.session_id for item in sessions),
            interrupted_session_ids=tuple(interrupted),
            persisted=True,
            effective=effective,
            reason=request.reason,
            created_at=now_timestamp(),
            metadata={"requested_by": request.requested_by, **dict(metadata or {})},
        )


def cancel_pending_dispatches(
    *,
    store_path: str | Path,
    run_id: str,
    task_id: str,
    reason: str,
    requested_by: str = "task-control-api",
    idempotency_key: str | None = None,
) -> BackendControlReceipt:
    store = BackendRegistryStore(store_path)
    try:
        return BackendControlRuntime(store).cancel_task(
            run_id=run_id,
            task_id=task_id,
            reason=reason,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
        )
    finally:
        store.close()


__all__ = [
    "ACTIVE_DISPATCHES",
    "ActiveDispatchHandle",
    "ActiveDispatchRegistry",
    "BackendControlReceipt",
    "BackendControlRuntime",
    "cancel_pending_dispatches",
]
