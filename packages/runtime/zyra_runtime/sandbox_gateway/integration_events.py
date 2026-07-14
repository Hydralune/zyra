from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .redaction import redact_for_event
from .event_port import GatewayEventPort
from .models import GatewayEventKind, GatewaySessionRecord
from .state_store import GatewayStateStore
from .integration_models import (
    FailureClass,
    GatewayBoundarySnapshot,
    GatewayExecutionReceipt,
    GatewayFailureSignal,
    RecoveryAction,
    WorkerGatewayIdentity,
    canonical_json,
    canonical_value,
    content_digest,
    stable_identifier,
)


@dataclass(frozen=True, slots=True)
class GatewayProjectedEvent:
    event_id: str
    event_type: str
    run_id: str
    task_id: str
    node_id: str
    session_id: str
    worker_id: str
    payload: Mapping[str, Any]
    causation_id: str = ""
    correlation_id: str = ""
    created_at: float = field(default_factory=time.time)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "session_id": self.session_id,
            "worker_id": self.worker_id,
            "payload": canonical_value(self.payload),
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
        }


class GatewayBackendSignalEmitter:
    """Creates same-run recovery inputs from gateway/backend failures."""

    def __init__(
        self,
        state_store: GatewayStateStore,
        event_port: GatewayEventPort,
        *,
        maximum_signals_per_session: int = 256,
        sink: Callable[[GatewayFailureSignal], None] | None = None,
    ) -> None:
        if maximum_signals_per_session <= 0:
            raise ValueError("maximum_signals_per_session must be positive")
        self.state_store = state_store
        self.event_port = event_port
        self.maximum_signals_per_session = maximum_signals_per_session
        self.sink = sink
        self._lock = threading.RLock()
        self._signals: dict[str, deque[GatewayFailureSignal]] = defaultdict(
            lambda: deque(maxlen=maximum_signals_per_session)
        )

    def emit(
        self,
        identity: WorkerGatewayIdentity,
        *,
        invocation_id: str,
        failure_class: FailureClass,
        code: str,
        reason: str,
        retryable: bool,
        recovery_actions: Sequence[RecoveryAction | str],
        backend_signal: str = "",
        artifact_refs: Sequence[str] = (),
        causation_id: str = "",
        attempt: int = 1,
        metadata: Mapping[str, Any] | None = None,
    ) -> GatewayFailureSignal:
        signal = GatewayFailureSignal(
            signal_id=stable_identifier(
                "gateway-failure",
                identity.binding_digest,
                invocation_id,
                code,
                attempt,
                time.time_ns(),
            ),
            identity=identity,
            invocation_id=invocation_id,
            failure_class=FailureClass(failure_class),
            code=code,
            reason=reason,
            retryable=retryable,
            recovery_actions=tuple(RecoveryAction(item) for item in recovery_actions),
            attempt=attempt,
            backend_signal=backend_signal,
            artifact_refs=tuple(artifact_refs),
            causation_id=causation_id,
            metadata=metadata or {},
        )
        with self._lock:
            self._signals[identity.session_id].append(signal)
        try:
            record = self.state_store.require_session(identity.session_id)
        except Exception:  # noqa: BLE001 - signal remains available even if session recovery is needed.
            record = None
        if record is not None:
            self.event_port.emit_recovery(
                record,
                reason=reason,
                error_code=code,
                alternatives=[item.value for item in signal.recovery_actions],
                causation_id=causation_id,
            )
        if self.sink is not None:
            self.sink(signal)
        return signal

    def backend_unavailable(
        self,
        identity: WorkerGatewayIdentity,
        *,
        invocation_id: str,
        reason: str,
        causation_id: str = "",
    ) -> GatewayFailureSignal:
        return self.emit(
            identity,
            invocation_id=invocation_id,
            failure_class=FailureClass.BACKEND,
            code="sandbox_backend_unavailable",
            reason=reason,
            retryable=True,
            recovery_actions=(RecoveryAction.REPLACE_BACKEND, RecoveryAction.RETRY),
            backend_signal="unavailable",
            causation_id=causation_id,
        )

    def workspace_corrupt(
        self,
        identity: WorkerGatewayIdentity,
        *,
        invocation_id: str,
        reason: str,
        causation_id: str = "",
    ) -> GatewayFailureSignal:
        return self.emit(
            identity,
            invocation_id=invocation_id,
            failure_class=FailureClass.WORKSPACE,
            code="workspace_integrity_failed",
            reason=reason,
            retryable=True,
            recovery_actions=(RecoveryAction.REBIND_WORKSPACE, RecoveryAction.REPLAN),
            backend_signal="workspace_corrupt",
            causation_id=causation_id,
        )

    def permission_blocked(
        self,
        identity: WorkerGatewayIdentity,
        *,
        invocation_id: str,
        reason: str,
        sealed: bool,
        causation_id: str = "",
    ) -> GatewayFailureSignal:
        return self.emit(
            identity,
            invocation_id=invocation_id,
            failure_class=FailureClass.PERMISSION,
            code="permission_denied" if sealed else "permission_pending",
            reason=reason,
            retryable=not sealed,
            recovery_actions=(
                (RecoveryAction.REPLAN, RecoveryAction.REDUCE_SCOPE)
                if sealed
                else (RecoveryAction.REQUEST_PERMISSION, RecoveryAction.REDUCE_SCOPE)
            ),
            backend_signal="permission_blocked",
            causation_id=causation_id,
        )

    def cancelled(
        self,
        identity: WorkerGatewayIdentity,
        *,
        invocation_id: str,
        reason: str,
        causation_id: str = "",
    ) -> GatewayFailureSignal:
        return self.emit(
            identity,
            invocation_id=invocation_id,
            failure_class=FailureClass.CANCEL,
            code="gateway_execution_cancelled",
            reason=reason,
            retryable=False,
            recovery_actions=(RecoveryAction.REPLAN,),
            backend_signal="cancelled",
            causation_id=causation_id,
        )

    def list(self, session_id: str) -> tuple[GatewayFailureSignal, ...]:
        with self._lock:
            return tuple(self._signals.get(session_id, ()))

    def consume(self, session_id: str, signal_id: str) -> GatewayFailureSignal:
        with self._lock:
            queue = self._signals.get(session_id)
            if queue is None:
                raise KeyError(signal_id)
            values = list(queue)
            for index, signal in enumerate(values):
                if signal.signal_id == signal_id:
                    del values[index]
                    queue.clear()
                    queue.extend(values)
                    return signal
        raise KeyError(signal_id)

    def recovery_input(self, session_id: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            {
                "kind": "sandbox_gateway_failure",
                "signal": signal.safe_dict(),
                "state_mutation": "recovery_input_available",
            }
            for signal in self.list(session_id)
        )

    def descriptor(self) -> Mapping[str, Any]:
        with self._lock:
            sessions = len(self._signals)
            signals = sum(len(items) for items in self._signals.values())
        return {
            "runtime": "GatewayBackendSignalEmitter",
            "sessions": sessions,
            "signals": signals,
            "maximum_signals_per_session": self.maximum_signals_per_session,
            "same_run_recovery_input": True,
        }


class GatewayReceiptJournal:
    """Durable append-only receipt projection; GatewayStateStore remains owner."""

    def __init__(
        self,
        root: str | Path,
        *,
        maximum_receipts: int = 100_000,
    ) -> None:
        if maximum_receipts <= 0:
            raise ValueError("maximum_receipts must be positive")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "integration-receipts.jsonl"
        self.maximum_receipts = maximum_receipts
        self._lock = threading.RLock()
        self._by_id: dict[str, Mapping[str, Any]] = {}
        self._by_idempotency: dict[str, str] = {}
        self._load()

    def append(
        self,
        receipt: GatewayExecutionReceipt,
        *,
        idempotency_key: str = "",
    ) -> GatewayExecutionReceipt:
        value = receipt.safe_dict()
        with self._lock:
            existing = self._by_id.get(receipt.receipt_id)
            if existing is not None:
                if canonical_json(existing) != canonical_json(value):
                    raise ValueError("gateway receipt id collision")
                return receipt
            if idempotency_key:
                previous_id = self._by_idempotency.get(idempotency_key)
                if previous_id and previous_id != receipt.receipt_id:
                    raise ValueError("gateway idempotency key collision")
            if len(self._by_id) >= self.maximum_receipts:
                raise RuntimeError("gateway receipt journal reached its configured limit")
            line = json.dumps(
                {
                    "record_type": "gateway_execution_receipt",
                    "idempotency_key_digest": content_digest(idempotency_key) if idempotency_key else "",
                    "receipt": value,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.write("\n")
                handle.flush()
            self._by_id[receipt.receipt_id] = value
            if idempotency_key:
                self._by_idempotency[idempotency_key] = receipt.receipt_id
        return receipt

    def get(self, receipt_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            return self._by_id.get(receipt_id)

    def find_idempotent(self, idempotency_key: str) -> Mapping[str, Any] | None:
        with self._lock:
            receipt_id = self._by_idempotency.get(idempotency_key)
            return self._by_id.get(receipt_id) if receipt_id else None

    def digests(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(content_digest(item) for item in self._by_id.values())

    def descriptor(self) -> Mapping[str, Any]:
        with self._lock:
            count = len(self._by_id)
        return {
            "journal_path_digest": content_digest(str(self.path)),
            "receipt_count": count,
            "maximum_receipts": self.maximum_receipts,
            "raw_secret_persistence": False,
        }

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                value = record.get("receipt")
                if not isinstance(value, Mapping):
                    raise ValueError("gateway receipt journal contains an invalid record")
                receipt_id = str(value.get("receipt_id") or "")
                if not receipt_id:
                    raise ValueError("gateway receipt journal record has no receipt id")
                existing = self._by_id.get(receipt_id)
                if existing is not None and canonical_json(existing) != canonical_json(value):
                    raise ValueError("gateway receipt journal contains a collision")
                self._by_id[receipt_id] = value


class GatewayEventProjector:
    def __init__(self, *, known_secrets: Iterable[str] = ()) -> None:
        self.known_secrets = tuple(str(item) for item in known_secrets if str(item))

    def project_receipt(
        self,
        receipt: GatewayExecutionReceipt,
        *,
        node_id: str = "",
    ) -> GatewayProjectedEvent:
        identity = receipt.invocation.identity
        payload = redact_for_event(
            {
                "sandbox_gateway": {
                    "kind": "execution_receipt",
                    "receipt": receipt.safe_dict(),
                    "state_mutation": "gateway_execution_settled",
                }
            },
            known_secrets=self.known_secrets,
        )
        return GatewayProjectedEvent(
            event_id=stable_identifier("gateway-event", receipt.receipt_id, "settled"),
            event_type="gateway_execution_settled",
            run_id=identity.run_id,
            task_id=identity.task_id,
            node_id=node_id or identity.node_id,
            session_id=identity.session_id,
            worker_id=identity.worker_id,
            payload=payload,
            causation_id=receipt.invocation.causation_id,
            correlation_id=receipt.invocation.correlation_id,
            created_at=receipt.finished_at,
        )

    def project_failure(self, signal: GatewayFailureSignal) -> GatewayProjectedEvent:
        identity = signal.identity
        payload = redact_for_event(
            {
                "sandbox_gateway": {
                    "kind": "failure_signal",
                    "failure": signal.safe_dict(),
                    "state_mutation": "gateway_recovery_input_created",
                }
            },
            known_secrets=self.known_secrets,
        )
        return GatewayProjectedEvent(
            event_id=stable_identifier("gateway-event", signal.signal_id, "failure"),
            event_type="gateway_failure_signal",
            run_id=identity.run_id,
            task_id=identity.task_id,
            node_id=identity.node_id,
            session_id=identity.session_id,
            worker_id=identity.worker_id,
            payload=payload,
            causation_id=signal.causation_id,
            created_at=signal.occurred_at,
        )

    def tool_metadata(
        self,
        receipt: GatewayExecutionReceipt,
        signals: Sequence[GatewayFailureSignal] = (),
    ) -> dict[str, str]:
        return {
            "sandbox_gateway_routed": "true",
            "sandbox_gateway_receipt_id": receipt.receipt_id,
            "sandbox_gateway_receipt_digest": receipt.receipt_digest,
            "sandbox_gateway_outcome": receipt.outcome.value,
            "sandbox_gateway_policy_digest": receipt.invocation.policy_digest,
            "sandbox_gateway_permission_consumption_id": receipt.permission_consumption_id,
            "sandbox_gateway_command_receipt_id": receipt.command_receipt_id,
            "sandbox_gateway_patch_receipt_id": receipt.patch_receipt_id,
            "sandbox_gateway_failure_signal_count": str(len(signals)),
            "sandbox_gateway_recovery_required": str(bool(signals)).lower(),
        }


def boundary_snapshot(
    *,
    state_store: GatewayStateStore,
    signal_emitter: GatewayBackendSignalEmitter,
    receipt_journal: GatewayReceiptJournal,
    backend_descriptors: Sequence[Mapping[str, Any]],
    pending_permissions: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> GatewayBoundarySnapshot:
    sessions = state_store.list_sessions()
    active_commands = tuple(
        item.active_command_id for item in sessions if item.active_command_id
    )
    signals = tuple(
        signal
        for session in sessions
        for signal in signal_emitter.list(session.session_id)
    )
    return GatewayBoundarySnapshot(
        snapshot_id=stable_identifier(
            "gateway-snapshot",
            [item.session_id for item in sessions],
            active_commands,
            receipt_journal.digests(),
            time.time_ns(),
        ),
        sessions=tuple(_session_projection(item) for item in sessions),
        active_commands=active_commands,
        pending_permissions=tuple(pending_permissions),
        failure_signals=signals,
        receipt_digests=receipt_journal.digests(),
        backend_descriptors=tuple(backend_descriptors),
        metadata=metadata or {},
    )


def _session_projection(record: GatewaySessionRecord) -> Mapping[str, Any]:
    return {
        "session_id": record.session_id,
        "run_id": record.run_id,
        "task_id": record.task_id,
        "workspace_id": record.workspace_id,
        "worker_id": record.worker_id,
        "state": record.state.value,
        "owner_epoch": record.owner_epoch,
        "generation": record.generation,
        "backend_id": record.backend_id,
        "active_command_id": record.active_command_id,
        "failure_code": record.failure_code,
        "recovery_count": record.recovery_count,
    }


__all__ = [
    "GatewayBackendSignalEmitter",
    "GatewayEventProjector",
    "GatewayProjectedEvent",
    "GatewayReceiptJournal",
    "boundary_snapshot",
]
