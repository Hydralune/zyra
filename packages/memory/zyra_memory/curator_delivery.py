from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .curator_context import (
    CuratorRecallRequest,
    CuratorRecallResult,
    CuratorRecallVerifier,
)
from .curator_integration_models import (
    CURATOR_DELIVERY_PROTOCOL,
    CURATOR_FAILURE_PROTOCOL,
    CURATOR_OUTCOME_PROTOCOL,
    CuratorConsumer,
    CuratorDeliveryLease,
    CuratorDeliveryState,
    CuratorDownstreamDelivery,
    CuratorOutcome,
    CuratorOutcomeKind,
)
from .curator_integration_store import CuratorIntegrationStore
from .curator_models import stable_digest


class CuratorConsumerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code).strip() or "consumer_error"
        self.retryable = bool(retryable)


class CuratorConsumerPort(Protocol):
    consumer: CuratorConsumer

    def consume(
        self,
        delivery: CuratorDownstreamDelivery,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CuratorDeliveryDispatchResult:
    delivery_id: str
    outcome_id: str
    consumer: CuratorConsumer
    status: str
    attempt: int
    acknowledged: bool
    retryable: bool
    receipt: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "outcome_id": self.outcome_id,
            "consumer": self.consumer.value,
            "status": self.status,
            "attempt": self.attempt,
            "acknowledged": self.acknowledged,
            "retryable": self.retryable,
            "receipt": dict(self.receipt),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class CuratorDeliveryDrainReport:
    consumer: CuratorConsumer
    processed: tuple[CuratorDeliveryDispatchResult, ...]
    recovered_delivery_ids: tuple[str, ...]
    pending_count: int
    retry_wait_count: int
    dead_count: int
    acknowledged_count: int
    handler_configured: bool

    @property
    def ok(self) -> bool:
        return self.dead_count == 0 and all(
            item.acknowledged or item.retryable for item in self.processed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "consumer": self.consumer.value,
            "processed": [item.to_dict() for item in self.processed],
            "recovered_delivery_ids": list(self.recovered_delivery_ids),
            "pending_count": self.pending_count,
            "retry_wait_count": self.retry_wait_count,
            "dead_count": self.dead_count,
            "acknowledged_count": self.acknowledged_count,
            "handler_configured": self.handler_configured,
        }


class CuratorDeliveryContract:
    """Validate the language-neutral consumer envelope before side effects."""

    @staticmethod
    def outcome(delivery: CuratorDownstreamDelivery) -> CuratorOutcome:
        delivery.validated()
        payload = delivery.payload
        if str(payload.get("protocol") or "") != CURATOR_DELIVERY_PROTOCOL:
            raise CuratorConsumerError(
                "delivery_protocol_invalid",
                "downstream delivery has an unsupported protocol",
            )
        if str(payload.get("consumer") or "") != delivery.consumer.value:
            raise CuratorConsumerError(
                "delivery_consumer_mismatch",
                "delivery payload consumer differs from claimed consumer",
            )
        raw_outcome = payload.get("outcome")
        if not isinstance(raw_outcome, Mapping):
            raise CuratorConsumerError(
                "outcome_missing",
                "downstream delivery lacks a curator outcome",
            )
        if str(raw_outcome.get("protocol") or "") != CURATOR_OUTCOME_PROTOCOL:
            raise CuratorConsumerError(
                "outcome_protocol_invalid",
                "downstream delivery outcome has an unsupported protocol",
            )
        outcome = CuratorOutcome.from_dict(raw_outcome)
        if outcome.outcome_id != delivery.outcome_id:
            raise CuratorConsumerError(
                "outcome_identity_mismatch",
                "delivery outcome identity differs from durable row",
            )
        if delivery.consumer not in outcome.target_consumers:
            raise CuratorConsumerError(
                "consumer_not_targeted",
                "outcome does not target the claimed consumer",
            )
        if outcome.task_id != delivery.task_id or outcome.run_id != delivery.run_id:
            raise CuratorConsumerError(
                "outcome_scope_mismatch",
                "delivery outcome belongs to another run or task",
            )
        return outcome

    @staticmethod
    def require_canonical(outcome: CuratorOutcome) -> None:
        if not outcome.canonical_memory_changed:
            raise CuratorConsumerError(
                "canonical_commit_required",
                "consumer requires a canonical memory mutation",
            )
        if not outcome.deterministic_validation:
            raise CuratorConsumerError(
                "deterministic_validation_required",
                "consumer refuses an unvalidated memory outcome",
            )
        if not outcome.memory_id or outcome.memory_revision <= 0:
            raise CuratorConsumerError(
                "memory_identity_missing",
                "consumer requires a committed memory identity and revision",
            )

    @staticmethod
    def require_kind(
        outcome: CuratorOutcome,
        kinds: Sequence[CuratorOutcomeKind],
    ) -> None:
        if outcome.kind not in set(kinds):
            expected = ",".join(kind.value for kind in kinds)
            raise CuratorConsumerError(
                "outcome_kind_unsupported",
                f"consumer expects one of [{expected}], got {outcome.kind.value}",
            )


class RetrievalContextConsumer:
    consumer = CuratorConsumer.RETRIEVAL_CONTEXT

    def __init__(self, verifier: CuratorRecallVerifier) -> None:
        self.verifier = verifier

    def consume(self, delivery: CuratorDownstreamDelivery) -> Mapping[str, Any]:
        outcome = CuratorDeliveryContract.outcome(delivery)
        CuratorDeliveryContract.require_canonical(outcome)
        result = self.verifier.verify(
            CuratorRecallRequest(
                outcome_id=outcome.outcome_id,
                require_present=True,
                worker_request_id=f"curator-delivery:{delivery.delivery_id}",
                session_id=f"curator-recall:{outcome.task_id}",
                metadata={
                    "delivery_id": delivery.delivery_id,
                    "delivery_attempt": delivery.attempt,
                    "consumer": self.consumer.value,
                },
            )
        )
        return {
            "protocol": "zyra.memory-curator-consumer-receipt/v1",
            "consumer": self.consumer.value,
            "delivery_id": delivery.delivery_id,
            "outcome_id": outcome.outcome_id,
            "proof_id": result.proof.proof_id,
            "query_id": result.proof.query_id,
            "memory_id": result.proof.memory_id,
            "memory_revision": result.proof.memory_revision,
            "effect": result.proof.effect.value,
            "index_scope": result.proof.index_scope,
            "index_generation": result.proof.index_generation,
            "index_revision": result.proof.index_revision,
            "worker_context_changed": result.verified,
            "contains_index_dump": False,
            "canonical_memory_owner": "SQLiteStore.memory_records",
        }


class SkillMemoryContractConsumer:
    consumer = CuratorConsumer.SKILL_MEMORY

    def consume(self, delivery: CuratorDownstreamDelivery) -> Mapping[str, Any]:
        outcome = CuratorDeliveryContract.outcome(delivery)
        CuratorDeliveryContract.require_kind(
            outcome,
            (CuratorOutcomeKind.SKILL_CANDIDATE,),
        )
        CuratorDeliveryContract.require_canonical(outcome)
        candidate = outcome.payload.get("receipt")
        if not isinstance(candidate, Mapping):
            raise CuratorConsumerError(
                "skill_receipt_missing",
                "skill-memory outcome lacks a commit receipt",
            )
        return {
            "protocol": "zyra.skill-memory-candidate-admission/v1",
            "consumer": self.consumer.value,
            "delivery_id": delivery.delivery_id,
            "outcome_id": outcome.outcome_id,
            "candidate_id": outcome.candidate_id,
            "decision_id": outcome.decision_id,
            "memory_id": outcome.memory_id,
            "memory_revision": outcome.memory_revision,
            "subject": outcome.subject,
            "summary": outcome.summary,
            "evidence_bundle_id": outcome.evidence_bundle_id,
            "evidence_digest": outcome.evidence_digest,
            "publication_state": "candidate_admitted",
            "publishes_skill_registry": False,
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "next_owner": "M1-06C SkillMemoryRuntime",
        }


class FaultObserverContractConsumer:
    consumer = CuratorConsumer.FAULT_OBSERVER

    def consume(self, delivery: CuratorDownstreamDelivery) -> Mapping[str, Any]:
        outcome = CuratorDeliveryContract.outcome(delivery)
        supported = {
            CuratorOutcomeKind.FAILURE_PATTERN,
            CuratorOutcomeKind.REJECTED,
            CuratorOutcomeKind.MERGE_REQUIRED,
            CuratorOutcomeKind.SUPERSEDED,
        }
        if outcome.kind not in supported:
            raise CuratorConsumerError(
                "fault_outcome_unsupported",
                f"fault observer cannot consume {outcome.kind.value}",
            )
        if outcome.kind is CuratorOutcomeKind.FAILURE_PATTERN:
            CuratorDeliveryContract.require_canonical(outcome)
        return {
            "protocol": CURATOR_FAILURE_PROTOCOL,
            "consumer": self.consumer.value,
            "delivery_id": delivery.delivery_id,
            "outcome_id": outcome.outcome_id,
            "candidate_id": outcome.candidate_id,
            "decision_id": outcome.decision_id,
            "kind": outcome.kind.value,
            "subject": outcome.subject,
            "summary": outcome.summary,
            "memory_id": outcome.memory_id,
            "memory_revision": outcome.memory_revision,
            "evidence_bundle_id": outcome.evidence_bundle_id,
            "evidence_digest": outcome.evidence_digest,
            "observation_state": "admitted",
            "next_owner": "M1-07B FaultObserverRuntime",
            "canonical_memory_changed": outcome.canonical_memory_changed,
        }


class RecoveryPlannerContractConsumer:
    consumer = CuratorConsumer.RECOVERY_PLANNER

    def consume(self, delivery: CuratorDownstreamDelivery) -> Mapping[str, Any]:
        outcome = CuratorDeliveryContract.outcome(delivery)
        CuratorDeliveryContract.require_kind(
            outcome,
            (CuratorOutcomeKind.FAILURE_PATTERN,),
        )
        CuratorDeliveryContract.require_canonical(outcome)
        return {
            "protocol": "zyra.recovery-memory-signal/v1",
            "consumer": self.consumer.value,
            "delivery_id": delivery.delivery_id,
            "outcome_id": outcome.outcome_id,
            "failure_memory_id": outcome.memory_id,
            "failure_memory_revision": outcome.memory_revision,
            "subject": outcome.subject,
            "summary": outcome.summary,
            "evidence_bundle_id": outcome.evidence_bundle_id,
            "evidence_digest": outcome.evidence_digest,
            "recovery_signal_state": "admitted",
            "strategy_selection_deferred": True,
            "next_owner": "M1-07C RecoveryPlannerRuntime",
            "canonical_memory_owner": "SQLiteStore.memory_records",
        }


class CompactRuntimeContractConsumer:
    consumer = CuratorConsumer.COMPACT_RUNTIME

    def consume(self, delivery: CuratorDownstreamDelivery) -> Mapping[str, Any]:
        outcome = CuratorDeliveryContract.outcome(delivery)
        CuratorDeliveryContract.require_canonical(outcome)
        return {
            "protocol": "zyra.compact-memory-admission/v1",
            "consumer": self.consumer.value,
            "delivery_id": delivery.delivery_id,
            "outcome_id": outcome.outcome_id,
            "memory_id": outcome.memory_id,
            "memory_revision": outcome.memory_revision,
            "subject": outcome.subject,
            "summary": outcome.summary,
            "compact_input_state": "available",
            "changes_compact_policy": False,
            "canonical_memory_owner": "SQLiteStore.memory_records",
        }


class AuditContractConsumer:
    consumer = CuratorConsumer.AUDIT

    def consume(self, delivery: CuratorDownstreamDelivery) -> Mapping[str, Any]:
        outcome = CuratorDeliveryContract.outcome(delivery)
        return {
            "protocol": "zyra.memory-curator-audit-receipt/v1",
            "consumer": self.consumer.value,
            "delivery_id": delivery.delivery_id,
            "outcome_id": outcome.outcome_id,
            "outcome_digest": outcome.outcome_digest,
            "kind": outcome.kind.value,
            "candidate_id": outcome.candidate_id,
            "decision_id": outcome.decision_id,
            "commit_receipt_id": outcome.commit_receipt_id,
            "memory_id": outcome.memory_id,
            "memory_revision": outcome.memory_revision,
            "evidence_bundle_id": outcome.evidence_bundle_id,
            "evidence_digest": outcome.evidence_digest,
            "canonical_memory_changed": outcome.canonical_memory_changed,
            "deterministic_validation": outcome.deterministic_validation,
            "audit_state": "observed",
        }


class CuratorDownstreamDispatcher:
    """Lease-fenced delivery of versioned 06B outcomes to downstream ports."""

    def __init__(
        self,
        *,
        store: CuratorIntegrationStore,
        handlers: Sequence[CuratorConsumerPort] = (),
        worker_id: str = "memory-curator-downstream",
        lease_seconds: float = 30.0,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.worker_id = str(worker_id).strip() or "memory-curator-downstream"
        self.lease_seconds = max(0.1, float(lease_seconds))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.handlers: dict[CuratorConsumer, CuratorConsumerPort] = {}
        for handler in handlers:
            self.register(handler)

    def register(self, handler: CuratorConsumerPort) -> None:
        consumer = handler.consumer
        if consumer in self.handlers and self.handlers[consumer] is not handler:
            raise ValueError(f"consumer handler already registered: {consumer.value}")
        self.handlers[consumer] = handler

    def unregister(self, consumer: CuratorConsumer) -> bool:
        return self.handlers.pop(consumer, None) is not None

    def process_one(
        self,
        consumer: CuratorConsumer,
        *,
        task_id: str = "",
        delivery_id: str = "",
    ) -> CuratorDeliveryDispatchResult | None:
        handler = self.handlers.get(consumer)
        if handler is None:
            return None
        claimed = self.store.claim_delivery(
            consumer=consumer,
            worker_id=f"{self.worker_id}:{consumer.value}",
            lease_seconds=self.lease_seconds,
            task_id=task_id,
            delivery_id=delivery_id,
        )
        if claimed is None:
            return None
        delivery, lease = claimed
        try:
            receipt = dict(handler.consume(delivery))
            self._validate_receipt(delivery, receipt)
        except CuratorConsumerError as error:
            settled = self.store.fail_delivery(
                lease,
                error_code=error.code,
                error_message=str(error),
                retryable=error.retryable,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            return CuratorDeliveryDispatchResult(
                delivery_id=delivery.delivery_id,
                outcome_id=delivery.outcome_id,
                consumer=consumer,
                status=settled.state.value,
                attempt=delivery.attempt,
                acknowledged=False,
                retryable=error.retryable,
                error_code=error.code,
                error_message=str(error),
            )
        except BaseException as error:
            settled = self.store.fail_delivery(
                lease,
                error_code=type(error).__name__.casefold(),
                error_message=str(error),
                retryable=True,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            return CuratorDeliveryDispatchResult(
                delivery_id=delivery.delivery_id,
                outcome_id=delivery.outcome_id,
                consumer=consumer,
                status=settled.state.value,
                attempt=delivery.attempt,
                acknowledged=False,
                retryable=True,
                error_code=type(error).__name__.casefold(),
                error_message=str(error),
            )
        acknowledged = self.store.acknowledge_delivery(
            lease,
            receipt=receipt,
            partition_key=delivery.task_id,
        )
        return CuratorDeliveryDispatchResult(
            delivery_id=delivery.delivery_id,
            outcome_id=delivery.outcome_id,
            consumer=consumer,
            status=acknowledged.state.value,
            attempt=delivery.attempt,
            acknowledged=True,
            retryable=False,
            receipt=receipt,
        )

    def drain(
        self,
        consumer: CuratorConsumer,
        *,
        task_id: str = "",
        limit: int = 100,
        recover_expired: bool = True,
    ) -> CuratorDeliveryDrainReport:
        handler = self.handlers.get(consumer)
        recovered = (
            self.store.sweep_expired_deliveries(
                consumer=consumer,
                limit=max(0, int(limit)),
                retry_delay_seconds=self.retry_delay_seconds,
            )
            if recover_expired
            else ()
        )
        processed: list[CuratorDeliveryDispatchResult] = []
        if handler is not None:
            for _ in range(max(0, int(limit))):
                result = self.process_one(consumer, task_id=task_id)
                if result is None:
                    break
                processed.append(result)
        deliveries = self.store.deliveries(
            task_id=task_id,
            consumer=consumer,
            limit=100_000,
        )
        return CuratorDeliveryDrainReport(
            consumer=consumer,
            processed=tuple(processed),
            recovered_delivery_ids=tuple(recovered),
            pending_count=sum(
                1 for item in deliveries if item.state is CuratorDeliveryState.PENDING
            ),
            retry_wait_count=sum(
                1 for item in deliveries if item.state is CuratorDeliveryState.RETRY_WAIT
            ),
            dead_count=sum(
                1 for item in deliveries if item.state is CuratorDeliveryState.DEAD
            ),
            acknowledged_count=sum(
                1 for item in deliveries if item.state is CuratorDeliveryState.ACKNOWLEDGED
            ),
            handler_configured=handler is not None,
        )

    def drain_configured(
        self,
        *,
        task_id: str = "",
        limit_per_consumer: int = 100,
    ) -> Mapping[str, Any]:
        reports = {
            consumer.value: self.drain(
                consumer,
                task_id=task_id,
                limit=limit_per_consumer,
            ).to_dict()
            for consumer in sorted(self.handlers, key=lambda item: item.value)
        }
        return {
            "ok": all(bool(report["ok"]) for report in reports.values()),
            "task_id": task_id,
            "reports": reports,
            "configured_consumers": sorted(consumer.value for consumer in self.handlers),
        }

    def recover(
        self,
        *,
        limit: int = 10_000,
    ) -> Mapping[str, Any]:
        expired = self.store.sweep_expired_deliveries(limit=limit)
        reports = self.drain_configured(limit_per_consumer=limit)
        return {
            "expired_delivery_ids": list(expired),
            "dispatch": reports,
            "recovery_is_idempotent": True,
        }

    def health(self, *, task_id: str = "") -> Mapping[str, Any]:
        deliveries = self.store.deliveries(task_id=task_id, limit=100_000)
        by_consumer: dict[str, dict[str, int]] = {}
        for delivery in deliveries:
            values = by_consumer.setdefault(delivery.consumer.value, {})
            values[delivery.state.value] = values.get(delivery.state.value, 0) + 1
        return {
            "ok": not any(
                item.state is CuratorDeliveryState.DEAD for item in deliveries
            ),
            "task_id": task_id,
            "worker_id": self.worker_id,
            "lease_seconds": self.lease_seconds,
            "retry_delay_seconds": self.retry_delay_seconds,
            "configured_consumers": sorted(consumer.value for consumer in self.handlers),
            "delivery_counts": by_consumer,
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "delivery_owner": "CuratorIntegrationStore",
        }

    @staticmethod
    def _validate_receipt(
        delivery: CuratorDownstreamDelivery,
        receipt: Mapping[str, Any],
    ) -> None:
        if str(receipt.get("delivery_id") or "") != delivery.delivery_id:
            raise CuratorConsumerError(
                "receipt_delivery_mismatch",
                "consumer receipt does not identify the claimed delivery",
            )
        if str(receipt.get("outcome_id") or "") != delivery.outcome_id:
            raise CuratorConsumerError(
                "receipt_outcome_mismatch",
                "consumer receipt does not identify the delivered outcome",
            )
        if str(receipt.get("consumer") or "") != delivery.consumer.value:
            raise CuratorConsumerError(
                "receipt_consumer_mismatch",
                "consumer receipt does not identify the claimed consumer",
            )
        if not stable_digest(receipt):
            raise CuratorConsumerError(
                "receipt_digest_failed",
                "consumer receipt could not be digested",
            )


class CuratorDeliveryController:
    """API/control-command facade for downstream runtimes implemented later."""

    def __init__(self, store: CuratorIntegrationStore) -> None:
        self.store = store

    def claim(
        self,
        *,
        consumer: CuratorConsumer,
        worker_id: str,
        task_id: str = "",
        delivery_id: str = "",
        lease_seconds: float = 30.0,
    ) -> Mapping[str, Any]:
        claimed = self.store.claim_delivery(
            consumer=consumer,
            worker_id=worker_id,
            task_id=task_id,
            delivery_id=delivery_id,
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            return {
                "status": "idle",
                "consumer": consumer.value,
                "delivery": None,
                "lease": None,
            }
        delivery, lease = claimed
        return {
            "status": "claimed",
            "consumer": consumer.value,
            "delivery": delivery.to_dict(),
            "lease": lease.to_dict(),
        }

    def acknowledge(
        self,
        lease: CuratorDeliveryLease,
        *,
        receipt: Mapping[str, Any],
        partition_key: str = "default",
    ) -> Mapping[str, Any]:
        delivery = self.store.acknowledge_delivery(
            lease,
            receipt=receipt,
            partition_key=partition_key,
        )
        return {
            "status": delivery.state.value,
            "delivery": delivery.to_dict(),
            "checkpoint": self.store.consumer_checkpoint(
                lease.consumer,
                partition_key=partition_key,
            ),
        }

    def fail(
        self,
        lease: CuratorDeliveryLease,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: float = 1.0,
    ) -> Mapping[str, Any]:
        delivery = self.store.fail_delivery(
            lease,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
        )
        return {
            "status": delivery.state.value,
            "delivery": delivery.to_dict(),
        }


__all__ = [
    "AuditContractConsumer",
    "CompactRuntimeContractConsumer",
    "CuratorConsumerError",
    "CuratorConsumerPort",
    "CuratorDeliveryContract",
    "CuratorDeliveryController",
    "CuratorDeliveryDispatchResult",
    "CuratorDeliveryDrainReport",
    "CuratorDownstreamDispatcher",
    "FaultObserverContractConsumer",
    "RecoveryPlannerContractConsumer",
    "RetrievalContextConsumer",
    "SkillMemoryContractConsumer",
]
