from __future__ import annotations

"""Fail-closed MCP event commit adapter for an existing EventStore.

No event data is durably owned here.  The committer validates one strict
run/task partition, canonicalizes and deduplicates event identities, divides
work into bounded batches, and delegates atomic append to an injected store
port.  Retry is allowed only when the port explicitly classifies a failure as
retryable and can prove that the same idempotency key is safe to repeat.
"""

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from zyra_core import EventRecord, now_iso

from .causality import McpCausalityReport, McpCausalityValidator
from .models import JsonValue, redact_value, stable_digest, to_json_value


class McpEventCommitError(RuntimeError):
    pass


class McpEventCommitDisabled(McpEventCommitError):
    pass


class McpEventPartitionError(McpEventCommitError):
    pass


class McpEventDuplicateConflict(McpEventCommitError):
    pass


class McpEventCommitRejected(McpEventCommitError):
    pass


class McpEventCommitExhausted(McpEventCommitError):
    def __init__(self, report: "McpEventCommitReport") -> None:
        super().__init__("MCP event commit retries exhausted")
        self.report = report


class McpEventCommitStatus(StrEnum):
    COMMITTED = "committed"
    ALREADY_COMMITTED = "already_committed"
    EMPTY = "empty"
    REJECTED = "rejected"
    FAILED = "failed"


class McpEventAttemptStatus(StrEnum):
    COMMITTED = "committed"
    RETRYABLE = "retryable"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    FAILED = "failed"


class McpEventDedupeDisposition(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE_IDENTICAL = "duplicate_identical"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    ALREADY_PERSISTED = "already_persisted"


@dataclass(frozen=True, slots=True)
class McpEventPartition:
    run_id: str
    task_id: str

    def __post_init__(self) -> None:
        if not self.run_id or not self.task_id:
            raise ValueError("run_id and task_id are required")

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.task_id}"

    @property
    def digest(self) -> str:
        return stable_digest({"run_id": self.run_id, "task_id": self.task_id})

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "key": self.key,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class McpEventCommitPolicy:
    max_batch_events: int = 128
    max_batch_bytes: int = 2 * 1024 * 1024
    max_attempts: int = 3
    reject_empty_event_id: bool = True
    reject_cross_partition: bool = True
    validate_causality: bool = True
    require_atomic_batch: bool = True
    require_idempotency: bool = True
    fail_on_duplicate_conflict: bool = True
    allow_identical_duplicates: bool = True

    def __post_init__(self) -> None:
        if self.max_batch_events <= 0:
            raise ValueError("max_batch_events must be positive")
        if self.max_batch_bytes <= 0:
            raise ValueError("max_batch_bytes must be positive")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "max_batch_events": self.max_batch_events,
            "max_batch_bytes": self.max_batch_bytes,
            "max_attempts": self.max_attempts,
            "reject_empty_event_id": self.reject_empty_event_id,
            "reject_cross_partition": self.reject_cross_partition,
            "validate_causality": self.validate_causality,
            "require_atomic_batch": self.require_atomic_batch,
            "require_idempotency": self.require_idempotency,
            "fail_on_duplicate_conflict": self.fail_on_duplicate_conflict,
            "allow_identical_duplicates": self.allow_identical_duplicates,
            "fail_closed": True,
        }


@dataclass(frozen=True, slots=True)
class McpEventStoreCommitResult:
    committed: bool
    partition_revision: int
    committed_event_ids: tuple[str, ...] = ()
    already_committed_event_ids: tuple[str, ...] = ()
    retryable: bool = False
    conflict: bool = False
    error_code: str = ""
    error_message: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.partition_revision < 0:
            raise ValueError("partition_revision cannot be negative")
        object.__setattr__(self, "committed_event_ids", tuple(self.committed_event_ids))
        object.__setattr__(self, "already_committed_event_ids", tuple(self.already_committed_event_ids))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "committed": self.committed,
            "partition_revision": self.partition_revision,
            "committed_event_ids": list(self.committed_event_ids),
            "already_committed_event_ids": list(self.already_committed_event_ids),
            "retryable": self.retryable,
            "conflict": self.conflict,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": redact_value(self.metadata),
        }


class McpEventStorePort(Protocol):
    """Existing canonical EventStore boundary required by the committer."""

    def partition_revision(self, run_id: str, task_id: str) -> int: ...

    def event_fingerprints(
        self,
        run_id: str,
        task_id: str,
        event_ids: Sequence[str],
    ) -> Mapping[str, str]: ...

    def commit_events(
        self,
        run_id: str,
        task_id: str,
        events: Sequence[EventRecord],
        *,
        expected_revision: int,
        idempotency_key: str,
        atomic: bool,
    ) -> McpEventStoreCommitResult | Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class McpEventEnvelope:
    event: EventRecord
    event_id: str
    fingerprint: str
    partition: McpEventPartition
    encoded_bytes: int
    sequence: int

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": self.event_id,
            "fingerprint": self.fingerprint,
            "partition": self.partition.safe_dict(),
            "encoded_bytes": self.encoded_bytes,
            "sequence": self.sequence,
            "event_type": str(self.event.event_type),
            "node_id": self.event.node_id,
        }


@dataclass(frozen=True, slots=True)
class McpEventDedupeDecision:
    event_id: str
    disposition: McpEventDedupeDisposition
    fingerprint: str
    persisted_fingerprint: str = ""
    reason: str = ""

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": self.event_id,
            "disposition": str(self.disposition),
            "fingerprint": self.fingerprint,
            "persisted_fingerprint": self.persisted_fingerprint,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class McpEventCommitBatch:
    batch_id: str
    partition: McpEventPartition
    envelopes: tuple[McpEventEnvelope, ...]
    expected_revision: int
    idempotency_key: str
    batch_index: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "envelopes", tuple(self.envelopes))
        if self.expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")

    @property
    def events(self) -> tuple[EventRecord, ...]:
        return tuple(item.event for item in self.envelopes)

    @property
    def event_ids(self) -> tuple[str, ...]:
        return tuple(item.event_id for item in self.envelopes)

    @property
    def encoded_bytes(self) -> int:
        return sum(item.encoded_bytes for item in self.envelopes)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "batch_id": self.batch_id,
            "partition": self.partition.safe_dict(),
            "envelopes": [item.safe_dict() for item in self.envelopes],
            "event_ids": list(self.event_ids),
            "expected_revision": self.expected_revision,
            "idempotency_key_digest": stable_digest(self.idempotency_key),
            "batch_index": self.batch_index,
            "encoded_bytes": self.encoded_bytes,
        }


@dataclass(frozen=True, slots=True)
class McpEventCommitAttempt:
    batch_id: str
    attempt: int
    status: McpEventAttemptStatus
    expected_revision: int
    resulting_revision: int
    committed_event_ids: tuple[str, ...] = ()
    retryable: bool = False
    error_code: str = ""
    error_message: str = ""
    started_at: str = field(default_factory=now_iso)
    completed_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "committed_event_ids", tuple(self.committed_event_ids))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "batch_id": self.batch_id,
            "attempt": self.attempt,
            "status": str(self.status),
            "expected_revision": self.expected_revision,
            "resulting_revision": self.resulting_revision,
            "committed_event_ids": list(self.committed_event_ids),
            "retryable": self.retryable,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class McpEventBatchReceipt:
    batch: McpEventCommitBatch
    status: McpEventCommitStatus
    attempts: tuple[McpEventCommitAttempt, ...]
    resulting_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempts", tuple(self.attempts))

    @property
    def ok(self) -> bool:
        return self.status in {McpEventCommitStatus.COMMITTED, McpEventCommitStatus.ALREADY_COMMITTED, McpEventCommitStatus.EMPTY}

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "batch": self.batch.safe_dict(),
            "status": str(self.status),
            "ok": self.ok,
            "attempts": [item.safe_dict() for item in self.attempts],
            "resulting_revision": self.resulting_revision,
        }


@dataclass(frozen=True, slots=True)
class McpEventCommitReport:
    partition: McpEventPartition
    status: McpEventCommitStatus
    receipts: tuple[McpEventBatchReceipt, ...]
    dedupe: tuple[McpEventDedupeDecision, ...]
    causality: McpCausalityReport | None
    input_event_count: int
    committed_event_count: int
    initial_revision: int
    final_revision: int
    started_at: str
    completed_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status in {McpEventCommitStatus.COMMITTED, McpEventCommitStatus.ALREADY_COMMITTED, McpEventCommitStatus.EMPTY} and all(item.ok for item in self.receipts)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-event-commit-report.v1",
            "partition": self.partition.safe_dict(),
            "status": str(self.status),
            "ok": self.ok,
            "receipts": [item.safe_dict() for item in self.receipts],
            "dedupe": [item.safe_dict() for item in self.dedupe],
            "causality": self.causality.safe_dict() if self.causality else None,
            "input_event_count": self.input_event_count,
            "committed_event_count": self.committed_event_count,
            "initial_revision": self.initial_revision,
            "final_revision": self.final_revision,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "owns_event_store": False,
            "fail_closed": True,
        }


class McpEventCommitter:
    def __init__(
        self,
        store: McpEventStorePort,
        *,
        policy: McpEventCommitPolicy | None = None,
        causality_validator: McpCausalityValidator | None = None,
        retry_observer: Callable[[McpEventCommitAttempt], None] | None = None,
        disabled: bool = False,
    ) -> None:
        self.store = store
        self.policy = policy or McpEventCommitPolicy()
        self.causality_validator = causality_validator or McpCausalityValidator()
        self.retry_observer = retry_observer
        self.disabled = disabled
        self._lock = threading.RLock()

    def commit(
        self,
        events: Sequence[EventRecord],
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str,
        raise_on_failure: bool = True,
    ) -> McpEventCommitReport:
        self._assert_enabled()
        started_at = now_iso()
        partition = _select_partition(events, run_id=run_id, task_id=task_id)
        envelopes = self._envelopes(events, partition)
        causality = None
        if self.policy.validate_causality and events:
            causality = self.causality_validator.validate_event_chain(events)
            if not causality.ok:
                report = McpEventCommitReport(partition, McpEventCommitStatus.REJECTED, (), (), causality, len(events), 0, 0, 0, started_at)
                if raise_on_failure:
                    raise McpEventCommitRejected("MCP event causality validation failed")
                return report
        with self._lock:
            initial_revision = self.store.partition_revision(partition.run_id, partition.task_id)
            if expected_revision is not None and expected_revision != initial_revision:
                report = McpEventCommitReport(partition, McpEventCommitStatus.REJECTED, (), (), causality, len(events), 0, initial_revision, initial_revision, started_at)
                if raise_on_failure:
                    raise McpEventCommitRejected("EventStore partition revision mismatch")
                return report
            selected, dedupe = self._dedupe(envelopes, partition)
            if not selected:
                status = McpEventCommitStatus.ALREADY_COMMITTED if events else McpEventCommitStatus.EMPTY
                return McpEventCommitReport(partition, status, (), tuple(dedupe), causality, len(events), 0, initial_revision, initial_revision, started_at)
            batches = self._batches(selected, partition, initial_revision, idempotency_key)
            receipts: list[McpEventBatchReceipt] = []
            revision = initial_revision
            committed_count = 0
            for batch in batches:
                if batch.expected_revision != revision:
                    batch = McpEventCommitBatch(batch.batch_id, batch.partition, batch.envelopes, revision, batch.idempotency_key, batch.batch_index)
                receipt = self._commit_batch(batch)
                receipts.append(receipt)
                if not receipt.ok:
                    report = McpEventCommitReport(partition, McpEventCommitStatus.FAILED, tuple(receipts), tuple(dedupe), causality, len(events), committed_count, initial_revision, receipt.resulting_revision, started_at)
                    if raise_on_failure:
                        raise McpEventCommitExhausted(report)
                    return report
                revision = receipt.resulting_revision
                committed_count += len(batch.envelopes)
            report = McpEventCommitReport(partition, McpEventCommitStatus.COMMITTED, tuple(receipts), tuple(dedupe), causality, len(events), committed_count, initial_revision, revision, started_at)
            return report

    def _envelopes(self, events: Sequence[EventRecord], partition: McpEventPartition) -> tuple[McpEventEnvelope, ...]:
        values: list[McpEventEnvelope] = []
        for sequence, event in enumerate(events):
            if (event.run_id, event.task_id) != (partition.run_id, partition.task_id):
                if self.policy.reject_cross_partition:
                    raise McpEventPartitionError("MCP event batch crosses run/task partition")
                continue
            event_id = str(getattr(event, "event_id", "") or "")
            if not event_id and self.policy.reject_empty_event_id:
                raise McpEventCommitRejected("MCP event requires canonical event_id")
            serialized = _event_projection(event)
            encoded = _canonical_bytes(serialized)
            if len(encoded) > self.policy.max_batch_bytes:
                raise McpEventCommitRejected("single MCP event exceeds maximum batch bytes")
            values.append(McpEventEnvelope(event, event_id, stable_digest(serialized), partition, len(encoded), sequence))
        return tuple(values)

    def _dedupe(self, envelopes: Sequence[McpEventEnvelope], partition: McpEventPartition) -> tuple[tuple[McpEventEnvelope, ...], list[McpEventDedupeDecision]]:
        by_id: dict[str, McpEventEnvelope] = {}
        decisions: list[McpEventDedupeDecision] = []
        for envelope in envelopes:
            current = by_id.get(envelope.event_id)
            if current is None:
                by_id[envelope.event_id] = envelope
                continue
            if current.fingerprint != envelope.fingerprint:
                decision = McpEventDedupeDecision(envelope.event_id, McpEventDedupeDisposition.DUPLICATE_CONFLICT, envelope.fingerprint, current.fingerprint, "same event_id has different canonical payload")
                decisions.append(decision)
                if self.policy.fail_on_duplicate_conflict:
                    raise McpEventDuplicateConflict(decision.reason)
            else:
                decisions.append(McpEventDedupeDecision(envelope.event_id, McpEventDedupeDisposition.DUPLICATE_IDENTICAL, envelope.fingerprint, current.fingerprint, "identical duplicate removed"))
                if not self.policy.allow_identical_duplicates:
                    raise McpEventDuplicateConflict("identical duplicates are disabled")
        persisted = self.store.event_fingerprints(partition.run_id, partition.task_id, tuple(by_id))
        selected: list[McpEventEnvelope] = []
        for event_id, envelope in by_id.items():
            fingerprint = str(persisted.get(event_id) or "")
            if not fingerprint:
                decisions.append(McpEventDedupeDecision(event_id, McpEventDedupeDisposition.ACCEPTED, envelope.fingerprint, reason="event is new to canonical store"))
                selected.append(envelope)
            elif fingerprint == envelope.fingerprint:
                decisions.append(McpEventDedupeDecision(event_id, McpEventDedupeDisposition.ALREADY_PERSISTED, envelope.fingerprint, fingerprint, "canonical store already contains identical event"))
            else:
                decision = McpEventDedupeDecision(event_id, McpEventDedupeDisposition.DUPLICATE_CONFLICT, envelope.fingerprint, fingerprint, "canonical store contains different payload for event_id")
                decisions.append(decision)
                raise McpEventDuplicateConflict(decision.reason)
        selected.sort(key=lambda item: item.sequence)
        return tuple(selected), decisions

    def _batches(self, envelopes: Sequence[McpEventEnvelope], partition: McpEventPartition, revision: int, idempotency_key: str) -> tuple[McpEventCommitBatch, ...]:
        if self.policy.require_idempotency and not idempotency_key:
            raise McpEventCommitRejected("idempotency_key is required")
        batches: list[McpEventCommitBatch] = []
        current: list[McpEventEnvelope] = []
        current_bytes = 0
        for envelope in envelopes:
            would_overflow = current and (len(current) >= self.policy.max_batch_events or current_bytes + envelope.encoded_bytes > self.policy.max_batch_bytes)
            if would_overflow:
                batches.append(_batch(partition, tuple(current), revision + len(batches), idempotency_key, len(batches)))
                current = []
                current_bytes = 0
            current.append(envelope)
            current_bytes += envelope.encoded_bytes
        if current:
            batches.append(_batch(partition, tuple(current), revision + len(batches), idempotency_key, len(batches)))
        return tuple(batches)

    def _commit_batch(self, batch: McpEventCommitBatch) -> McpEventBatchReceipt:
        attempts: list[McpEventCommitAttempt] = []
        revision = batch.expected_revision
        for attempt_number in range(1, self.policy.max_attempts + 1):
            started = now_iso()
            try:
                raw = self.store.commit_events(batch.partition.run_id, batch.partition.task_id, batch.events, expected_revision=revision, idempotency_key=batch.idempotency_key, atomic=self.policy.require_atomic_batch)
                result = _store_result(raw, revision)
            except Exception as error:  # noqa: BLE001 - unknown failures are fail-closed.
                attempt = McpEventCommitAttempt(batch.batch_id, attempt_number, McpEventAttemptStatus.FAILED, revision, revision, error_code=type(error).__name__, error_message=str(error)[:1000], started_at=started)
                attempts.append(attempt)
                self._observe(attempt)
                break
            status = _attempt_status(result)
            attempt = McpEventCommitAttempt(batch.batch_id, attempt_number, status, revision, result.partition_revision, result.committed_event_ids, result.retryable, result.error_code, result.error_message, started)
            attempts.append(attempt)
            self._observe(attempt)
            if result.committed:
                return McpEventBatchReceipt(batch, McpEventCommitStatus.COMMITTED, tuple(attempts), result.partition_revision)
            if result.already_committed_event_ids and set(result.already_committed_event_ids) == set(batch.event_ids):
                return McpEventBatchReceipt(batch, McpEventCommitStatus.ALREADY_COMMITTED, tuple(attempts), result.partition_revision)
            if not result.retryable or result.conflict:
                break
            revision = self.store.partition_revision(batch.partition.run_id, batch.partition.task_id)
        return McpEventBatchReceipt(batch, McpEventCommitStatus.FAILED, tuple(attempts), revision)

    def _observe(self, attempt: McpEventCommitAttempt) -> None:
        if self.retry_observer is not None:
            self.retry_observer(attempt)

    def _assert_enabled(self) -> None:
        if self.disabled:
            raise McpEventCommitDisabled("McpEventCommitter is disabled")


class CallableMcpEventStorePort:
    """Callable adapter; all canonical behavior remains in injected callbacks."""

    def __init__(self, *, partition_revision: Callable[[str, str], int], event_fingerprints: Callable[[str, str, Sequence[str]], Mapping[str, str]], commit_events: Callable[..., McpEventStoreCommitResult | Mapping[str, Any]]) -> None:
        self._partition_revision = partition_revision
        self._event_fingerprints = event_fingerprints
        self._commit_events = commit_events

    def partition_revision(self, run_id: str, task_id: str) -> int:
        value = int(self._partition_revision(run_id, task_id))
        if value < 0:
            raise McpEventCommitRejected("EventStore returned negative revision")
        return value

    def event_fingerprints(self, run_id: str, task_id: str, event_ids: Sequence[str]) -> Mapping[str, str]:
        value = self._event_fingerprints(run_id, task_id, event_ids)
        if not isinstance(value, Mapping):
            raise McpEventCommitRejected("EventStore fingerprint callback returned invalid shape")
        return {str(key): str(item) for key, item in value.items()}

    def commit_events(self, run_id: str, task_id: str, events: Sequence[EventRecord], *, expected_revision: int, idempotency_key: str, atomic: bool) -> McpEventStoreCommitResult | Mapping[str, Any]:
        return self._commit_events(run_id, task_id, events, expected_revision=expected_revision, idempotency_key=idempotency_key, atomic=atomic)


class CallableMcpEventSink:
    """Immediate callable sink backed by ``McpEventCommitter``.

    The sink keeps no queue and no dedupe database.  Every call is committed to
    the injected canonical store or raises; callers therefore cannot mistake a
    process-local buffer for durable evidence.
    """

    def __init__(self, committer: McpEventCommitter, *, idempotency_key_factory: Callable[[EventRecord], str] | None = None) -> None:
        self.committer = committer
        self.idempotency_key_factory = idempotency_key_factory or _default_event_idempotency_key

    def __call__(self, event: EventRecord) -> McpEventCommitReport:
        return self.append(event)

    def append(self, event: EventRecord) -> McpEventCommitReport:
        key = self.idempotency_key_factory(event)
        if not key:
            raise McpEventCommitRejected("event sink idempotency key is empty")
        return self.committer.commit((event,), run_id=event.run_id, task_id=event.task_id, idempotency_key=key, raise_on_failure=True)

    def append_batch(self, events: Sequence[EventRecord], *, idempotency_key: str) -> McpEventCommitReport:
        return self.committer.commit(events, idempotency_key=idempotency_key, raise_on_failure=True)


def _select_partition(events: Sequence[EventRecord], *, run_id: str | None, task_id: str | None) -> McpEventPartition:
    selected_run = str(run_id or (events[0].run_id if events else ""))
    selected_task = str(task_id or (events[0].task_id if events else ""))
    return McpEventPartition(selected_run, selected_task)


def _event_projection(event: EventRecord) -> dict[str, Any]:
    payload = to_json_value(event.payload)
    return {"event_id": str(getattr(event, "event_id", "")), "run_id": event.run_id, "task_id": event.task_id, "node_id": event.node_id, "event_type": str(event.event_type), "payload": payload, "timestamp": str(getattr(event, "timestamp", ""))}


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _batch(partition: McpEventPartition, envelopes: tuple[McpEventEnvelope, ...], revision: int, root_key: str, index: int) -> McpEventCommitBatch:
    event_digest = stable_digest([item.fingerprint for item in envelopes])
    key = f"{root_key}:batch:{index}:{event_digest}"
    return McpEventCommitBatch("mcpbatch_" + event_digest[:24], partition, envelopes, revision, key, index)


def _store_result(value: McpEventStoreCommitResult | Mapping[str, Any], default_revision: int) -> McpEventStoreCommitResult:
    if isinstance(value, McpEventStoreCommitResult):
        return value
    if not isinstance(value, Mapping):
        raise McpEventCommitRejected("EventStore commit returned invalid shape")
    return McpEventStoreCommitResult(committed=bool(value.get("committed", False)), partition_revision=int(value.get("partition_revision", default_revision)), committed_event_ids=tuple(value.get("committed_event_ids") or ()), already_committed_event_ids=tuple(value.get("already_committed_event_ids") or ()), retryable=bool(value.get("retryable", False)), conflict=bool(value.get("conflict", False)), error_code=str(value.get("error_code") or ""), error_message=str(value.get("error_message") or "")[:1000], metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {})


def _attempt_status(result: McpEventStoreCommitResult) -> McpEventAttemptStatus:
    if result.committed:
        return McpEventAttemptStatus.COMMITTED
    if result.conflict:
        return McpEventAttemptStatus.CONFLICT
    if result.retryable:
        return McpEventAttemptStatus.RETRYABLE
    if result.error_code:
        return McpEventAttemptStatus.REJECTED
    return McpEventAttemptStatus.FAILED


def _default_event_idempotency_key(event: EventRecord) -> str:
    event_id = str(getattr(event, "event_id", "") or "")
    if not event_id:
        return ""
    return f"mcp-event:{event.run_id}:{event.task_id}:{event_id}"


__all__ = [
    "CallableMcpEventSink",
    "CallableMcpEventStorePort",
    "McpEventBatchReceipt",
    "McpEventCommitAttempt",
    "McpEventCommitBatch",
    "McpEventCommitDisabled",
    "McpEventCommitError",
    "McpEventCommitExhausted",
    "McpEventCommitPolicy",
    "McpEventCommitRejected",
    "McpEventCommitReport",
    "McpEventCommitStatus",
    "McpEventCommitter",
    "McpEventDedupeDecision",
    "McpEventDedupeDisposition",
    "McpEventDuplicateConflict",
    "McpEventEnvelope",
    "McpEventPartition",
    "McpEventPartitionError",
    "McpEventStoreCommitResult",
    "McpEventStorePort",
]
