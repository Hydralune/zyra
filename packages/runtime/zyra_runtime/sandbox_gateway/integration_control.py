from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from .models import GatewayLifecycleState
from .integration_events import boundary_snapshot
from .integration_models import (
    FailureClass,
    GatewayAction,
    GatewayBoundarySnapshot,
    GatewayControlRequest,
    GatewayControlResult,
    GatewayOutcome,
    RecoveryAction,
    WorkerGatewayIdentity,
    stable_identifier,
)

if TYPE_CHECKING:
    from .integration_factory import GatewayRuntimeBundle


@dataclass(frozen=True, slots=True)
class GatewayControlAuditRecord:
    audit_id: str
    request: GatewayControlRequest
    result: GatewayControlResult
    request_digest: str
    created_at: float = field(default_factory=time.time)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "request": self.request.safe_dict(),
            "result": self.result.safe_dict(),
            "request_digest": self.request_digest,
            "created_at": self.created_at,
        }


class GatewayControlPlane:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bundles: dict[str, "GatewayRuntimeBundle"] = {}
        self._audit: list[GatewayControlAuditRecord] = []

    def register(self, bundle: "GatewayRuntimeBundle") -> None:
        with self._lock:
            for session in bundle.state_store.list_sessions():
                owner = self._bundles.get(session.session_id)
                if owner is not None and owner is not bundle:
                    raise ValueError("gateway session already belongs to another runtime bundle")
                self._bundles[session.session_id] = bundle

    def register_session(self, session_id: str, bundle: "GatewayRuntimeBundle") -> None:
        bundle.state_store.require_session(session_id)
        with self._lock:
            owner = self._bundles.get(session_id)
            if owner is not None and owner is not bundle:
                raise ValueError("gateway session owner collision")
            self._bundles[session_id] = bundle

    def execute(self, request: GatewayControlRequest) -> GatewayControlResult:
        bundle = self._require_bundle(request.session_id)
        try:
            record = bundle.state_store.require_session(request.session_id)
            self._assert_expectations(record, request)
            if request.action == GatewayAction.BACKEND_CANCEL:
                result = self._cancel(bundle, record, request)
            elif request.action == GatewayAction.SESSION_CLOSE:
                result = self._close(bundle, record, request)
            else:
                result = GatewayControlResult(
                    request_id=request.request_id,
                    accepted=False,
                    outcome=GatewayOutcome.DENIED,
                    session_id=request.session_id,
                    command_id=request.command_id,
                    generation=record.generation,
                    state=record.state.value,
                    failure_code="unsupported_gateway_control_action",
                )
        except Exception as error:  # noqa: BLE001
            result = GatewayControlResult(
                request_id=request.request_id,
                accepted=False,
                outcome=GatewayOutcome.FAILED,
                session_id=request.session_id,
                command_id=request.command_id,
                failure_code=str(getattr(getattr(error, "code", None), "value", "") or type(error).__name__),
                metadata={"reason": str(error)},
            )
        audit = GatewayControlAuditRecord(
            audit_id=stable_identifier("gateway-control-audit", request.request_id, result.safe_dict()),
            request=request,
            result=result,
            request_digest=request.request_digest,
        )
        with self._lock:
            self._audit.append(audit)
        return result

    def cancel(
        self,
        *,
        session_id: str,
        command_id: str,
        reason: str,
        expected_generation: int = 0,
        actor_id: str = "runtime-control",
        causation_id: str = "",
    ) -> GatewayControlResult:
        return self.execute(
            GatewayControlRequest(
                request_id=stable_identifier("gateway-control", session_id, command_id, "cancel", time.time_ns()),
                action=GatewayAction.BACKEND_CANCEL,
                session_id=session_id,
                command_id=command_id,
                reason=reason,
                expected_generation=expected_generation,
                actor_id=actor_id,
                causation_id=causation_id,
            )
        )

    def close(
        self,
        *,
        session_id: str,
        reason: str,
        expected_generation: int = 0,
        actor_id: str = "runtime-control",
        causation_id: str = "",
    ) -> GatewayControlResult:
        return self.execute(
            GatewayControlRequest(
                request_id=stable_identifier("gateway-control", session_id, "close", time.time_ns()),
                action=GatewayAction.SESSION_CLOSE,
                session_id=session_id,
                reason=reason,
                expected_generation=expected_generation,
                actor_id=actor_id,
                causation_id=causation_id,
            )
        )

    def recover_startup(self) -> tuple[Mapping[str, Any], ...]:
        outcomes: list[Mapping[str, Any]] = []
        bundles = self._unique_bundles()
        for bundle in bundles:
            recovered = bundle.runtime.recover_on_startup()
            for record in recovered:
                self.register_session(record.session_id, bundle)
                identity = WorkerGatewayIdentity(
                    run_id=record.run_id,
                    task_id=record.task_id,
                    worker_id=record.worker_id,
                    session_id=record.session_id,
                    workspace_id=record.workspace_id,
                    owner_epoch=record.owner_epoch,
                    backend_id=record.backend_id,
                    generation=record.generation,
                )
                signal = bundle.signal_emitter.emit(
                    identity,
                    invocation_id=record.active_command_id,
                    failure_class=FailureClass.BACKEND,
                    code="gateway_interrupted_recovered",
                    reason="gateway recovered an interrupted session during startup",
                    retryable=True,
                    recovery_actions=(RecoveryAction.RETRY, RecoveryAction.REPLAN),
                    backend_signal="startup_recovery",
                )
                outcomes.append(
                    {
                        "session_id": record.session_id,
                        "state": record.state.value,
                        "generation": record.generation,
                        "signal": signal.safe_dict(),
                    }
                )
        return tuple(outcomes)

    def snapshot(self) -> tuple[GatewayBoundarySnapshot, ...]:
        snapshots: list[GatewayBoundarySnapshot] = []
        for bundle in self._unique_bundles():
            snapshots.append(
                boundary_snapshot(
                    state_store=bundle.state_store,
                    signal_emitter=bundle.signal_emitter,
                    receipt_journal=bundle.receipt_journal,
                    backend_descriptors=(bundle.backend.descriptor(),),
                    metadata={"worker_id": bundle.worker_id},
                )
            )
        return tuple(snapshots)

    def audit_records(self) -> tuple[GatewayControlAuditRecord, ...]:
        with self._lock:
            return tuple(self._audit)

    def descriptor(self) -> Mapping[str, Any]:
        with self._lock:
            sessions = len(self._bundles)
            audits = len(self._audit)
        return {
            "runtime": "GatewayControlPlane",
            "registered_sessions": sessions,
            "control_audit_records": audits,
            "supports_cancel": True,
            "supports_close": True,
            "supports_startup_recovery": True,
            "permission_owner_changed": False,
        }

    def _cancel(
        self,
        bundle: "GatewayRuntimeBundle",
        record: Any,
        request: GatewayControlRequest,
    ) -> GatewayControlResult:
        command_id = request.command_id or record.active_command_id
        if not command_id:
            return GatewayControlResult(
                request_id=request.request_id,
                accepted=False,
                outcome=GatewayOutcome.DENIED,
                session_id=request.session_id,
                generation=record.generation,
                state=record.state.value,
                failure_code="gateway_command_not_active",
            )
        cancelled = bundle.runtime.cancel(
            request.session_id,
            command_id,
            reason=request.reason or "control cancellation",
        )
        latest = bundle.state_store.require_session(request.session_id)
        if cancelled:
            identity = WorkerGatewayIdentity(
                run_id=latest.run_id,
                task_id=latest.task_id,
                worker_id=latest.worker_id,
                session_id=latest.session_id,
                workspace_id=latest.workspace_id,
                owner_epoch=latest.owner_epoch,
                backend_id=latest.backend_id,
                generation=latest.generation,
            )
            signal = bundle.signal_emitter.cancelled(
                identity,
                invocation_id=command_id,
                reason=request.reason or "control cancellation",
                causation_id=request.causation_id,
            )
            event_refs = (signal.signal_id,)
        else:
            event_refs = ()
        return GatewayControlResult(
            request_id=request.request_id,
            accepted=cancelled,
            outcome=GatewayOutcome.CANCELLED if cancelled else GatewayOutcome.DENIED,
            session_id=request.session_id,
            command_id=command_id,
            generation=latest.generation,
            state=latest.state.value,
            failure_code="" if cancelled else "gateway_cancel_not_delivered",
            event_refs=event_refs,
        )

    @staticmethod
    def _close(
        bundle: "GatewayRuntimeBundle",
        record: Any,
        request: GatewayControlRequest,
    ) -> GatewayControlResult:
        if record.state == GatewayLifecycleState.BUSY:
            return GatewayControlResult(
                request_id=request.request_id,
                accepted=False,
                outcome=GatewayOutcome.DENIED,
                session_id=request.session_id,
                command_id=record.active_command_id,
                generation=record.generation,
                state=record.state.value,
                failure_code="gateway_session_busy",
            )
        closed = bundle.runtime.close_session(request.session_id)
        return GatewayControlResult(
            request_id=request.request_id,
            accepted=True,
            outcome=GatewayOutcome.COMMITTED,
            session_id=request.session_id,
            generation=closed.generation,
            state=closed.state.value,
        )

    @staticmethod
    def _assert_expectations(record: Any, request: GatewayControlRequest) -> None:
        if request.expected_generation and record.generation != request.expected_generation:
            raise ValueError("gateway session generation changed before control command")
        if request.expected_owner_epoch and record.owner_epoch != request.expected_owner_epoch:
            raise ValueError("gateway workspace owner epoch changed before control command")

    def _require_bundle(self, session_id: str) -> "GatewayRuntimeBundle":
        with self._lock:
            bundle = self._bundles.get(session_id)
        if bundle is not None:
            return bundle
        for candidate in self._unique_bundles():
            try:
                candidate.state_store.require_session(session_id)
            except Exception:  # noqa: BLE001
                continue
            self.register_session(session_id, candidate)
            return candidate
        raise KeyError(session_id)

    def _unique_bundles(self) -> tuple["GatewayRuntimeBundle", ...]:
        with self._lock:
            values = tuple(self._bundles.values())
        unique: list["GatewayRuntimeBundle"] = []
        seen: set[int] = set()
        for value in values:
            if id(value) not in seen:
                unique.append(value)
                seen.add(id(value))
        return tuple(unique)


__all__ = [
    "GatewayControlAuditRecord",
    "GatewayControlPlane",
]
