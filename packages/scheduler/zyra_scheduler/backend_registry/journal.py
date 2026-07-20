from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import (
    BackendDispatchEnvelope,
    BackendDispatchSession,
    BackendFailureKind,
    BackendRecoveryIntent,
    canonical_json,
    checksum,
)
from .store import BackendRegistryStore
from .transport import BackendTransportResponse


class DispatchJournalKind(str, Enum):
    SESSION_CREATED = "session_created"
    ENVELOPE_PINNED = "envelope_pinned"
    TRANSPORT_STARTED = "transport_started"
    FRAME_OBSERVED = "frame_observed"
    TRANSPORT_SUCCEEDED = "transport_succeeded"
    TRANSPORT_FAILED = "transport_failed"
    BACKEND_CHANGED = "backend_changed"
    PROVIDER_CHANGE_REQUIRED = "provider_change_required"
    CONTROL_APPLIED = "control_applied"
    RECOVERY_EMITTED = "recovery_emitted"
    SESSION_TERMINATED = "session_terminated"


class SideEffectFenceStatus(str, Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    ABORTED = "aborted"
    RECONCILE_REQUIRED = "reconcile_required"


@dataclass(frozen=True, slots=True)
class DispatchJournalRecord:
    journal_id: str
    session_id: str
    sequence: int
    record_kind: DispatchJournalKind
    envelope_id: str | None
    backend_id: str | None
    provider_route_id: str | None
    previous_digest: str
    created_at: float
    payload: Mapping[str, Any]
    record_digest: str

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        sequence: int,
        kind: DispatchJournalKind,
        previous_digest: str,
        payload: Mapping[str, Any],
        envelope_id: str | None = None,
        backend_id: str | None = None,
        provider_route_id: str | None = None,
        created_at: float | None = None,
    ) -> DispatchJournalRecord:
        body = {
            "journal_id": f"backend_journal_{uuid.uuid4().hex}",
            "session_id": session_id,
            "sequence": sequence,
            "record_kind": kind.value,
            "envelope_id": envelope_id,
            "backend_id": backend_id,
            "provider_route_id": provider_route_id,
            "previous_digest": previous_digest,
            "created_at": time.time() if created_at is None else created_at,
            "payload": dict(payload),
        }
        return cls(
            journal_id=body["journal_id"],
            session_id=session_id,
            sequence=sequence,
            record_kind=kind,
            envelope_id=envelope_id,
            backend_id=backend_id,
            provider_route_id=provider_route_id,
            previous_digest=previous_digest,
            created_at=float(body["created_at"]),
            payload=dict(payload),
            record_digest=checksum(body),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DispatchJournalRecord:
        body = dict(value)
        supplied = str(body.pop("record_digest"))
        if checksum(body) != supplied:
            raise RuntimeError("dispatch journal record checksum mismatch")
        return cls(
            journal_id=str(body["journal_id"]),
            session_id=str(body["session_id"]),
            sequence=int(body["sequence"]),
            record_kind=DispatchJournalKind(str(body["record_kind"])),
            envelope_id=(str(body["envelope_id"]) if body.get("envelope_id") else None),
            backend_id=(str(body["backend_id"]) if body.get("backend_id") else None),
            provider_route_id=(
                str(body["provider_route_id"]) if body.get("provider_route_id") else None
            ),
            previous_digest=str(body["previous_digest"]),
            created_at=float(body["created_at"]),
            payload=dict(body.get("payload") or {}),
            record_digest=supplied,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "journal_id": self.journal_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "record_kind": self.record_kind.value,
            "envelope_id": self.envelope_id,
            "backend_id": self.backend_id,
            "provider_route_id": self.provider_route_id,
            "previous_digest": self.previous_digest,
            "created_at": self.created_at,
            "payload": dict(self.payload),
            "record_digest": self.record_digest,
        }


@dataclass(frozen=True, slots=True)
class DispatchMaterialization:
    session_id: str
    envelope_id: str
    transport_receipt_id: str
    result_digest: str
    status: str
    completed_at: float
    result: Mapping[str, Any]
    provider_route_id: str
    provider_route_checksum: str
    backend_id: str
    m0_execution_ref: str
    frame_count: int
    output_observed: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(
        cls,
        session: BackendDispatchSession,
        envelope: BackendDispatchEnvelope,
        response: BackendTransportResponse,
    ) -> DispatchMaterialization:
        return cls(
            session_id=session.session_id,
            envelope_id=envelope.envelope_id,
            transport_receipt_id=response.transport_receipt_id,
            result_digest=response.result_digest,
            status=response.status,
            completed_at=response.completed_at,
            result=dict(response.result),
            provider_route_id=envelope.provider_route_id or "",
            provider_route_checksum=envelope.provider_route_checksum,
            backend_id=envelope.backend_id,
            m0_execution_ref=envelope.m0_execution_ref,
            frame_count=len(response.frames),
            output_observed=response.output_observed,
            metadata={
                "input_digest": response.input_digest,
                "request_bytes": response.request_bytes,
                "response_bytes": response.response_bytes,
                "transport": response.metadata.get("transport"),
            },
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DispatchMaterialization:
        return cls(
            session_id=str(value["session_id"]),
            envelope_id=str(value["envelope_id"]),
            transport_receipt_id=str(value["transport_receipt_id"]),
            result_digest=str(value["result_digest"]),
            status=str(value["status"]),
            completed_at=float(value["completed_at"]),
            result=dict(value.get("result") or {}),
            provider_route_id=str(value["provider_route_id"]),
            provider_route_checksum=str(value["provider_route_checksum"]),
            backend_id=str(value["backend_id"]),
            m0_execution_ref=str(value["m0_execution_ref"]),
            frame_count=int(value["frame_count"]),
            output_observed=bool(value["output_observed"]),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "envelope_id": self.envelope_id,
            "transport_receipt_id": self.transport_receipt_id,
            "result_digest": self.result_digest,
            "status": self.status,
            "completed_at": self.completed_at,
            "result": dict(self.result),
            "provider_route_id": self.provider_route_id,
            "provider_route_checksum": self.provider_route_checksum,
            "backend_id": self.backend_id,
            "m0_execution_ref": self.m0_execution_ref,
            "frame_count": self.frame_count,
            "output_observed": self.output_observed,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SideEffectFence:
    fence_key: str
    session_id: str
    envelope_id: str
    owner_epoch: int
    status: SideEffectFenceStatus
    acquired_at: float
    completed_at: float | None
    result_digest: str | None
    provider_route_id: str
    backend_id: str
    m0_execution_ref: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SideEffectFence:
        return cls(
            fence_key=str(value["fence_key"]),
            session_id=str(value["session_id"]),
            envelope_id=str(value["envelope_id"]),
            owner_epoch=int(value["owner_epoch"]),
            status=SideEffectFenceStatus(str(value["status"])),
            acquired_at=float(value["acquired_at"]),
            completed_at=(float(value["completed_at"]) if value.get("completed_at") else None),
            result_digest=(str(value["result_digest"]) if value.get("result_digest") else None),
            provider_route_id=str(value["provider_route_id"]),
            backend_id=str(value["backend_id"]),
            m0_execution_ref=str(value["m0_execution_ref"]),
            idempotency_key=str(value["idempotency_key"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fence_key": self.fence_key,
            "session_id": self.session_id,
            "envelope_id": self.envelope_id,
            "owner_epoch": self.owner_epoch,
            "status": self.status.value,
            "acquired_at": self.acquired_at,
            "completed_at": self.completed_at,
            "result_digest": self.result_digest,
            "provider_route_id": self.provider_route_id,
            "backend_id": self.backend_id,
            "m0_execution_ref": self.m0_execution_ref,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class DispatchReplayPlan:
    session: BackendDispatchSession
    records: tuple[DispatchJournalRecord, ...]
    materialization: DispatchMaterialization | None
    original_provider_route_id: str
    final_provider_route_id: str
    original_backend_id: str
    final_backend_id: str
    m0_execution_ref: str
    envelope_ids: tuple[str, ...]
    transport_receipt_ids: tuple[str, ...]
    failure_kinds: tuple[str, ...]
    recovery_input_ids: tuple[str, ...]
    chain_head: str
    replay_safe: bool
    replay_block_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict(),
            "records": [record.to_dict() for record in self.records],
            "materialization": None if self.materialization is None else self.materialization.to_dict(),
            "route_refs": {
                "provider_before": self.original_provider_route_id,
                "provider_after": self.final_provider_route_id,
                "backend_before": self.original_backend_id,
                "backend_after": self.final_backend_id,
                "m0_execution_ref": self.m0_execution_ref,
            },
            "envelope_ids": list(self.envelope_ids),
            "transport_receipt_ids": list(self.transport_receipt_ids),
            "failure_kinds": list(self.failure_kinds),
            "recovery_input_ids": list(self.recovery_input_ids),
            "chain_head": self.chain_head,
            "replay_safe": self.replay_safe,
            "replay_block_reason": self.replay_block_reason,
        }


@dataclass(frozen=True, slots=True)
class DispatchOutboxRecord:
    outbox_id: str
    session_id: str
    topic: str
    partition_key: str
    sequence: int
    available_at: float
    delivered_at: float | None
    delivery_attempts: int
    last_error: str | None
    payload: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DispatchOutboxRecord:
        return cls(
            outbox_id=str(value["outbox_id"]),
            session_id=str(value["session_id"]),
            topic=str(value["topic"]),
            partition_key=str(value["partition_key"]),
            sequence=int(value["sequence"]),
            available_at=float(value["available_at"]),
            delivered_at=(float(value["delivered_at"]) if value.get("delivered_at") else None),
            delivery_attempts=int(value["delivery_attempts"]),
            last_error=(str(value["last_error"]) if value.get("last_error") else None),
            payload=dict(value.get("payload") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "session_id": self.session_id,
            "topic": self.topic,
            "partition_key": self.partition_key,
            "sequence": self.sequence,
            "available_at": self.available_at,
            "delivered_at": self.delivered_at,
            "delivery_attempts": self.delivery_attempts,
            "last_error": self.last_error,
            "payload": dict(self.payload),
        }


class BackendDispatchJournal:
    def __init__(self, store: BackendRegistryStore) -> None:
        self.store = store

    def append(
        self,
        session: BackendDispatchSession,
        kind: DispatchJournalKind,
        payload: Mapping[str, Any],
        *,
        envelope: BackendDispatchEnvelope | None = None,
        backend_id: str | None = None,
        provider_route_id: str | None = None,
    ) -> DispatchJournalRecord:
        records = self.records(session.session_id)
        record = DispatchJournalRecord.create(
            session_id=session.session_id,
            sequence=len(records) + 1,
            kind=kind,
            previous_digest=records[-1].record_digest if records else "genesis",
            payload=payload,
            envelope_id=(envelope.envelope_id if envelope else None),
            backend_id=(envelope.backend_id if envelope else backend_id),
            provider_route_id=(
                envelope.provider_route_id if envelope else provider_route_id
            ),
        )
        stored = self.store.append_journal_record(record.to_dict())
        return DispatchJournalRecord.from_dict(stored)

    def records(self, session_id: str) -> tuple[DispatchJournalRecord, ...]:
        return tuple(
            DispatchJournalRecord.from_dict(value)
            for value in self.store.journal_records(session_id)
        )

    def pin_envelope(
        self,
        session: BackendDispatchSession,
        envelope: BackendDispatchEnvelope,
        *,
        operation: str,
        input_digest: str,
    ) -> tuple[DispatchJournalRecord, SideEffectFence]:
        self._assert_route_identity(session, envelope)
        record = self.append(
            session,
            DispatchJournalKind.ENVELOPE_PINNED,
            {
                "envelope": envelope.to_dict(),
                "operation": operation,
                "input_digest": input_digest,
                "backend_changed": bool(envelope.previous_envelope_id),
                "provider_route_changed": False,
            },
            envelope=envelope,
        )
        fence = SideEffectFence(
            fence_key=f"dispatch:{envelope.idempotency_key}",
            session_id=session.session_id,
            envelope_id=envelope.envelope_id,
            owner_epoch=envelope.attempt,
            status=SideEffectFenceStatus.ACTIVE,
            acquired_at=time.time(),
            completed_at=None,
            result_digest=None,
            provider_route_id=envelope.provider_route_id or "",
            backend_id=envelope.backend_id,
            m0_execution_ref=envelope.m0_execution_ref,
            idempotency_key=envelope.idempotency_key,
        )
        stored = SideEffectFence.from_dict(
            self.store.acquire_side_effect_fence(fence.to_dict())
        )
        if stored.session_id != session.session_id:
            raise RuntimeError("idempotency fence is owned by another dispatch session")
        if stored.status is SideEffectFenceStatus.COMMITTED:
            raise RuntimeError("dispatch side effect is already committed")
        if stored.owner_epoch > envelope.attempt:
            raise RuntimeError("dispatch attempt is stale relative to side-effect fence")
        return record, stored

    def materialize_success(
        self,
        session: BackendDispatchSession,
        envelope: BackendDispatchEnvelope,
        response: BackendTransportResponse,
        *,
        fence: SideEffectFence,
    ) -> DispatchMaterialization:
        self._assert_route_identity(session, envelope)
        if response.input_digest == "" or response.result_digest == "":
            raise ValueError("transport receipt digests are required")
        value = DispatchMaterialization.from_response(session, envelope, response)
        stored = DispatchMaterialization.from_dict(
            self.store.put_dispatch_materialization(value.to_dict())
        )
        self.store.complete_side_effect_fence(
            fence.fence_key,
            owner_epoch=fence.owner_epoch,
            status=SideEffectFenceStatus.COMMITTED.value,
            completed_at=response.completed_at,
            result_digest=response.result_digest,
        )
        self.append(
            session,
            DispatchJournalKind.TRANSPORT_SUCCEEDED,
            {
                "transport_receipt_id": response.transport_receipt_id,
                "input_digest": response.input_digest,
                "result_digest": response.result_digest,
                "result": dict(response.result),
                "frame_count": len(response.frames),
                "output_observed": response.output_observed,
                "request_bytes": response.request_bytes,
                "response_bytes": response.response_bytes,
            },
            envelope=envelope,
        )
        return stored

    def record_failure(
        self,
        session: BackendDispatchSession,
        envelope: BackendDispatchEnvelope,
        *,
        fence: SideEffectFence,
        failure_kind: BackendFailureKind,
        recovery_intent: BackendRecoveryIntent,
        retryable: bool,
        output_observed: bool,
        reason: str,
        detail: Mapping[str, Any],
    ) -> DispatchJournalRecord:
        terminal_fence = (
            SideEffectFenceStatus.RECONCILE_REQUIRED
            if output_observed
            else SideEffectFenceStatus.ABORTED
        )
        self.store.complete_side_effect_fence(
            fence.fence_key,
            owner_epoch=fence.owner_epoch,
            status=terminal_fence.value,
            completed_at=time.time(),
            result_digest=None,
        )
        return self.append(
            session,
            DispatchJournalKind.TRANSPORT_FAILED,
            {
                "failure_kind": failure_kind.value,
                "recovery_intent": recovery_intent.value,
                "retryable": retryable,
                "output_observed": output_observed,
                "reason": reason,
                "detail": dict(detail),
                "fence_status": terminal_fence.value,
            },
            envelope=envelope,
        )

    def materialization(self, session_id: str) -> DispatchMaterialization | None:
        value = self.store.get_dispatch_materialization(session_id)
        return None if value is None else DispatchMaterialization.from_dict(value)

    def replay_plan(self, session_id: str) -> DispatchReplayPlan:
        session = self.store.get_dispatch_session(session_id)
        if session is None:
            raise KeyError(f"backend dispatch session not found: {session_id}")
        records = self.records(session_id)
        if not records:
            raise RuntimeError(f"backend dispatch journal is empty: {session_id}")
        materialization = self.materialization(session_id)
        envelope_ids: list[str] = []
        backend_ids: list[str] = []
        provider_ids: list[str] = []
        receipts: list[str] = []
        failures: list[str] = []
        recovery_ids: list[str] = []
        for record in records:
            if record.envelope_id and record.envelope_id not in envelope_ids:
                envelope_ids.append(record.envelope_id)
            if record.backend_id and record.backend_id not in backend_ids:
                backend_ids.append(record.backend_id)
            if record.provider_route_id and record.provider_route_id not in provider_ids:
                provider_ids.append(record.provider_route_id)
            receipt = str(record.payload.get("transport_receipt_id") or "")
            if receipt and receipt not in receipts:
                receipts.append(receipt)
            failure = str(record.payload.get("failure_kind") or "")
            if failure:
                failures.append(failure)
            recovery = str(record.payload.get("recovery_input_id") or "")
            if recovery:
                recovery_ids.append(recovery)
        output_observed_without_result = any(
            bool(record.payload.get("output_observed"))
            for record in records
            if record.record_kind is DispatchJournalKind.TRANSPORT_FAILED
        ) and materialization is None
        replay_safe = materialization is not None or not output_observed_without_result
        block_reason = (
            ""
            if replay_safe
            else "observable output exists without a committed materialization"
        )
        return DispatchReplayPlan(
            session=session,
            records=records,
            materialization=materialization,
            original_provider_route_id=provider_ids[0] if provider_ids else session.provider_route_id,
            final_provider_route_id=provider_ids[-1] if provider_ids else session.provider_route_id,
            original_backend_id=backend_ids[0] if backend_ids else "",
            final_backend_id=backend_ids[-1] if backend_ids else session.current_backend_id or "",
            m0_execution_ref=session.m0_execution_ref,
            envelope_ids=tuple(envelope_ids),
            transport_receipt_ids=tuple(receipts),
            failure_kinds=tuple(failures),
            recovery_input_ids=tuple(recovery_ids),
            chain_head=records[-1].record_digest,
            replay_safe=replay_safe,
            replay_block_reason=block_reason,
        )

    def enqueue(
        self,
        session: BackendDispatchSession,
        *,
        topic: str,
        payload: Mapping[str, Any],
        available_at: float | None = None,
    ) -> DispatchOutboxRecord:
        if not topic.strip():
            raise ValueError("dispatch outbox topic is required")
        sequence = len(self.records(session.session_id))
        record = DispatchOutboxRecord(
            outbox_id=f"backend_outbox_{uuid.uuid4().hex}",
            session_id=session.session_id,
            topic=topic,
            partition_key=f"{session.run_id}:{session.task_id}",
            sequence=sequence,
            available_at=time.time() if available_at is None else available_at,
            delivered_at=None,
            delivery_attempts=0,
            last_error=None,
            payload=dict(payload),
        )
        return DispatchOutboxRecord.from_dict(
            self.store.enqueue_dispatch_outbox(record.to_dict())
        )

    def drain_outbox(
        self,
        publisher: Callable[[str, str, Mapping[str, Any]], None],
        *,
        limit: int = 100,
        now: float | None = None,
        retry_delay_seconds: float = 1.0,
    ) -> tuple[DispatchOutboxRecord, ...]:
        current = time.time() if now is None else now
        delivered: list[DispatchOutboxRecord] = []
        for raw in self.store.ready_dispatch_outbox(at=current, limit=limit):
            record = DispatchOutboxRecord.from_dict(raw)
            try:
                publisher(record.topic, record.partition_key, record.payload)
            except Exception as error:
                updated = self.store.complete_dispatch_outbox(
                    record.outbox_id,
                    delivered_at=None,
                    next_available_at=current + max(0.01, retry_delay_seconds),
                    error=f"{type(error).__name__}: {error}"[:2048],
                )
            else:
                updated = self.store.complete_dispatch_outbox(
                    record.outbox_id,
                    delivered_at=current,
                    next_available_at=current,
                    error=None,
                )
            delivered.append(DispatchOutboxRecord.from_dict(updated))
        return tuple(delivered)

    def verify(self, session_id: str) -> dict[str, Any]:
        records = self.records(session_id)
        previous = "genesis"
        provider_routes: set[str] = set()
        backends: set[str] = set()
        for sequence, record in enumerate(records, start=1):
            if record.sequence != sequence:
                raise RuntimeError("dispatch journal sequence is not contiguous")
            if record.previous_digest != previous:
                raise RuntimeError("dispatch journal predecessor does not match")
            body = record.to_dict()
            supplied = str(body.pop("record_digest"))
            if checksum(body) != supplied:
                raise RuntimeError("dispatch journal record digest does not match")
            previous = supplied
            if record.provider_route_id:
                provider_routes.add(record.provider_route_id)
            if record.backend_id:
                backends.add(record.backend_id)
        return {
            "session_id": session_id,
            "valid": True,
            "record_count": len(records),
            "chain_head": previous,
            "provider_route_count": len(provider_routes),
            "backend_count": len(backends),
            "provider_route_changed": len(provider_routes) > 1,
            "backend_changed": len(backends) > 1,
        }

    @staticmethod
    def _assert_route_identity(
        session: BackendDispatchSession,
        envelope: BackendDispatchEnvelope,
    ) -> None:
        if session.provider_route_id != (envelope.provider_route_id or ""):
            raise RuntimeError("backend envelope changed provider route within a dispatch session")
        if session.provider_route_checksum != envelope.provider_route_checksum:
            raise RuntimeError("backend envelope provider route checksum drift")
        if session.provider_catalog_revision != envelope.provider_catalog_revision:
            raise RuntimeError("backend envelope provider catalog revision drift")
        if session.provider_credential_version != envelope.provider_credential_version:
            raise RuntimeError("backend envelope provider credential version drift")
        if session.provider_credential_fingerprint != envelope.provider_credential_fingerprint:
            raise RuntimeError("backend envelope provider credential fingerprint drift")
        if session.provider_transport_id != envelope.provider_transport_id:
            raise RuntimeError("backend envelope provider transport drift")
        if session.m0_execution_ref != envelope.m0_execution_ref:
            raise RuntimeError("backend envelope M0 execution reference drift")


__all__ = [
    "BackendDispatchJournal",
    "DispatchJournalKind",
    "DispatchJournalRecord",
    "DispatchMaterialization",
    "DispatchOutboxRecord",
    "DispatchReplayPlan",
    "SideEffectFence",
    "SideEffectFenceStatus",
]
