from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import now_iso

from .curator_models import (
    CandidateKind,
    CommitDisposition,
    CuratorRunResult,
    DecisionCode,
    DecisionStatus,
    MemoryCandidate,
    MemoryCommitReceipt,
    MemoryDecision,
    canonical_json,
    mapping,
    parse_timestamp,
    stable_digest,
    stable_id,
    unique_strings,
)


CURATOR_INPUT_PROTOCOL = "zyra.memory-curator-input/v1"
CURATOR_OUTCOME_PROTOCOL = "zyra.memory-curator-outcome/v1"
CURATOR_FAILURE_PROTOCOL = "zyra.memory-curator-failure/v1"
CURATOR_DELIVERY_PROTOCOL = "zyra.memory-curator-delivery/v1"
CURATOR_CONTEXT_PROTOCOL = "zyra.memory-curator-context-proof/v1"
CURATOR_INTEGRATION_PROTOCOL = "zyra.memory-curator-integration/v1"
CURATOR_AUDIT_PROTOCOL = "zyra.memory-curator-audit/v1"


class CuratorIntegrationContractError(ValueError):
    pass


class CuratorProjectionConflictError(RuntimeError):
    pass


class CuratorDeliveryConflictError(RuntimeError):
    pass


class CuratorContextVerificationError(RuntimeError):
    pass


class RuntimeTraceKind(StrEnum):
    EVENT = "event"
    TOOL_RESULT = "tool_result"
    BROWSER_TRACE = "browser_trace"
    CODE_TRACE = "code_trace"
    ARTIFACT = "artifact"
    COMPACT = "compact"
    SESSION = "session"
    CONTROL = "control"
    FAILURE = "failure"
    RECOVERY = "recovery"


class CuratorInputSource(StrEnum):
    RUNTIME_EVENT_SPINE = "runtime_event_spine"
    LEGACY_EVENT_PROJECTION = "legacy_event_projection"
    TASK_CHECKPOINT = "task_checkpoint"
    ARTIFACT_STORE = "artifact_store"


class CuratorOutcomeKind(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMMITTED = "committed"
    INDEX_PUBLISHED = "index_published"
    SKILL_CANDIDATE = "skill_candidate"
    FAILURE_PATTERN = "failure_pattern"
    DISCARDED = "discarded"
    COMPRESSED = "compressed"
    SUPERSEDED = "superseded"
    MERGE_REQUIRED = "merge_required"
    NOOP = "noop"


class CuratorOutcomeState(StrEnum):
    PROJECTED = "projected"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class CuratorConsumer(StrEnum):
    RETRIEVAL_CONTEXT = "retrieval_context"
    SKILL_MEMORY = "skill_memory"
    FAULT_OBSERVER = "fault_observer"
    RECOVERY_PLANNER = "recovery_planner"
    COMPACT_RUNTIME = "compact_runtime"
    AUDIT = "audit"


class CuratorDeliveryState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    ACKNOWLEDGED = "acknowledged"
    RETRY_WAIT = "retry_wait"
    DEAD = "dead"
    CANCELLED = "cancelled"


class CuratorIntegrationRunState(StrEnum):
    STARTED = "started"
    CURATOR_SUCCEEDED = "curator_succeeded"
    PROJECTING = "projecting"
    PUBLISHED = "published"
    CONTEXT_VERIFIED = "context_verified"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ContextEffect(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    STALE_REJECTED = "stale_rejected"
    NOT_APPLICABLE = "not_applicable"


class FailureSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class FailureDisposition(StrEnum):
    RETRY = "retry"
    REPLAN = "replan"
    QUARANTINE = "quarantine"
    REJECT = "reject"
    OPERATOR = "operator"


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CuratorIntegrationContractError(f"{name} is required")
    return text


def _optional(value: object) -> str:
    return str(value or "").strip()


def _positive(value: object, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise CuratorIntegrationContractError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise CuratorIntegrationContractError(f"{name} must be an integer") from error
    minimum = 0 if allow_zero else 1
    if number < minimum:
        raise CuratorIntegrationContractError(f"{name} must be >= {minimum}")
    return number


def _finite_float(value: object, name: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise CuratorIntegrationContractError(f"{name} must be numeric") from error
    if number != number or number in {float("inf"), float("-inf")}:
        raise CuratorIntegrationContractError(f"{name} must be finite")
    if number < minimum:
        raise CuratorIntegrationContractError(f"{name} must be >= {minimum}")
    return number


def _timestamp(value: object, name: str) -> str:
    text = _required(value, name)
    try:
        parse_timestamp(text)
    except (TypeError, ValueError) as error:
        raise CuratorIntegrationContractError(f"{name} is not an ISO timestamp") from error
    return text


def _digest(value: object, name: str) -> str:
    text = _required(value, name)
    normalized = text.removeprefix("sha256:")
    if len(normalized) != 64:
        raise CuratorIntegrationContractError(f"{name} must be a sha256 digest")
    try:
        int(normalized, 16)
    except ValueError as error:
        raise CuratorIntegrationContractError(f"{name} must be hexadecimal") from error
    return normalized


def _json_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _string_tuple(values: Iterable[object]) -> tuple[str, ...]:
    return unique_strings(tuple(values))


def _mapping_tuple(values: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    return tuple(dict(item) for item in values if isinstance(item, Mapping))


@dataclass(frozen=True, slots=True)
class RuntimeTraceRef:
    ref_id: str
    run_id: str
    task_id: str
    source: CuratorInputSource
    kind: RuntimeTraceKind
    source_id: str
    source_sequence: int
    content_digest: str
    occurred_at: str
    event_type: str = ""
    aggregate_id: str = ""
    causation_id: str = ""
    correlation_id: str = ""
    producer: str = ""
    summary: str = ""
    artifact_ids: tuple[str, ...] = ()
    trusted_runtime: bool = False
    canonical: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> RuntimeTraceRef:
        _required(self.ref_id, "ref_id")
        _required(self.run_id, "run_id")
        _required(self.task_id, "task_id")
        _required(self.source_id, "source_id")
        _positive(self.source_sequence, "source_sequence", allow_zero=True)
        _digest(self.content_digest, "content_digest")
        _timestamp(self.occurred_at, "occurred_at")
        if self.source is CuratorInputSource.RUNTIME_EVENT_SPINE and not self.canonical:
            raise CuratorIntegrationContractError(
                "runtime event spine refs must identify canonical envelopes"
            )
        if self.trusted_runtime and self.source not in {
            CuratorInputSource.RUNTIME_EVENT_SPINE,
            CuratorInputSource.LEGACY_EVENT_PROJECTION,
            CuratorInputSource.TASK_CHECKPOINT,
        }:
            raise CuratorIntegrationContractError(
                "artifact-only input cannot claim verified runtime trust"
            )
        return self

    @property
    def identity_digest(self) -> str:
        return stable_digest(self.identity_projection())

    def identity_projection(self) -> Mapping[str, Any]:
        return {
            "ref_id": self.ref_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "source": self.source.value,
            "kind": self.kind.value,
            "source_id": self.source_id,
            "source_sequence": self.source_sequence,
            "content_digest": self.content_digest,
            "event_type": self.event_type,
            "aggregate_id": self.aggregate_id,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "artifact_ids": list(self.artifact_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_INPUT_PROTOCOL,
            **self.identity_projection(),
            "occurred_at": self.occurred_at,
            "producer": self.producer,
            "summary": self.summary,
            "trusted_runtime": self.trusted_runtime,
            "canonical": self.canonical,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        source: CuratorInputSource,
        kind: RuntimeTraceKind,
        source_id: str,
        source_sequence: int,
        content_digest: str,
        occurred_at: str,
        event_type: str = "",
        aggregate_id: str = "",
        causation_id: str = "",
        correlation_id: str = "",
        producer: str = "",
        summary: str = "",
        artifact_ids: Sequence[str] = (),
        trusted_runtime: bool = False,
        canonical: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeTraceRef:
        ref_id = stable_id(
            "curator_trace",
            run_id,
            task_id,
            source.value,
            source_id,
            source_sequence,
            content_digest,
        )
        return cls(
            ref_id=ref_id,
            run_id=run_id,
            task_id=task_id,
            source=source,
            kind=kind,
            source_id=source_id,
            source_sequence=source_sequence,
            content_digest=content_digest.removeprefix("sha256:"),
            occurred_at=occurred_at,
            event_type=event_type,
            aggregate_id=aggregate_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            producer=producer,
            summary=summary,
            artifact_ids=_string_tuple(artifact_ids),
            trusted_runtime=trusted_runtime,
            canonical=canonical,
            metadata=dict(metadata or {}),
        ).validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeTraceRef:
        return cls(
            ref_id=_optional(value.get("ref_id")),
            run_id=_optional(value.get("run_id")),
            task_id=_optional(value.get("task_id")),
            source=CuratorInputSource(
                str(value.get("source") or CuratorInputSource.LEGACY_EVENT_PROJECTION.value)
            ),
            kind=RuntimeTraceKind(str(value.get("kind") or RuntimeTraceKind.EVENT.value)),
            source_id=_optional(value.get("source_id")),
            source_sequence=int(value.get("source_sequence", 0)),
            content_digest=_optional(value.get("content_digest")),
            occurred_at=_optional(value.get("occurred_at")) or now_iso(),
            event_type=_optional(value.get("event_type")),
            aggregate_id=_optional(value.get("aggregate_id")),
            causation_id=_optional(value.get("causation_id")),
            correlation_id=_optional(value.get("correlation_id")),
            producer=_optional(value.get("producer")),
            summary=_optional(value.get("summary")),
            artifact_ids=_string_tuple(value.get("artifact_ids") or ()),
            trusted_runtime=bool(value.get("trusted_runtime", False)),
            canonical=bool(value.get("canonical", False)),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class CuratorInputBatch:
    batch_id: str
    run_id: str
    task_id: str
    refs: tuple[RuntimeTraceRef, ...]
    source_watermark: int
    next_watermark: int
    high_watermark: int
    batch_digest: str
    has_more: bool
    created_at: str
    legacy_event_count: int = 0
    runtime_event_count: int = 0
    artifact_count: int = 0
    duplicate_source_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> CuratorInputBatch:
        _required(self.batch_id, "batch_id")
        _required(self.run_id, "run_id")
        _required(self.task_id, "task_id")
        _positive(self.source_watermark, "source_watermark", allow_zero=True)
        _positive(self.next_watermark, "next_watermark", allow_zero=True)
        _positive(self.high_watermark, "high_watermark", allow_zero=True)
        if self.next_watermark < self.source_watermark:
            raise CuratorIntegrationContractError("next_watermark regressed")
        if self.high_watermark < self.next_watermark:
            raise CuratorIntegrationContractError("high_watermark precedes next_watermark")
        _digest(self.batch_digest, "batch_digest")
        _timestamp(self.created_at, "created_at")
        if any(ref.task_id != self.task_id or ref.run_id != self.run_id for ref in self.refs):
            raise CuratorIntegrationContractError("input batch contains a foreign run/task ref")
        if len({ref.ref_id for ref in self.refs}) != len(self.refs):
            raise CuratorIntegrationContractError("input batch ref identities must be unique")
        expected = stable_digest([ref.identity_projection() for ref in self.refs])
        if expected != self.batch_digest:
            raise CuratorIntegrationContractError("input batch digest does not match refs")
        return self

    @property
    def empty(self) -> bool:
        return not self.refs

    @property
    def trusted_count(self) -> int:
        return sum(1 for ref in self.refs if ref.trusted_runtime)

    @property
    def evidence_kinds(self) -> tuple[str, ...]:
        return tuple(sorted({ref.kind.value for ref in self.refs}))

    def to_dict(self, *, include_refs: bool = True) -> dict[str, Any]:
        output = {
            "protocol": CURATOR_INPUT_PROTOCOL,
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "source_watermark": self.source_watermark,
            "next_watermark": self.next_watermark,
            "high_watermark": self.high_watermark,
            "batch_digest": self.batch_digest,
            "has_more": self.has_more,
            "created_at": self.created_at,
            "ref_count": len(self.refs),
            "trusted_count": self.trusted_count,
            "evidence_kinds": list(self.evidence_kinds),
            "legacy_event_count": self.legacy_event_count,
            "runtime_event_count": self.runtime_event_count,
            "artifact_count": self.artifact_count,
            "duplicate_source_ids": list(self.duplicate_source_ids),
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }
        if include_refs:
            output["refs"] = [ref.to_dict() for ref in self.refs]
        return output

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        refs: Sequence[RuntimeTraceRef],
        source_watermark: int,
        next_watermark: int,
        high_watermark: int,
        has_more: bool,
        legacy_event_count: int = 0,
        runtime_event_count: int = 0,
        artifact_count: int = 0,
        duplicate_source_ids: Sequence[str] = (),
        warnings: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> CuratorInputBatch:
        ordered = tuple(
            sorted(
                refs,
                key=lambda ref: (
                    ref.source_sequence,
                    ref.source.value,
                    ref.source_id,
                    ref.ref_id,
                ),
            )
        )
        digest = stable_digest([ref.identity_projection() for ref in ordered])
        batch_id = stable_id(
            "curator_input",
            run_id,
            task_id,
            source_watermark,
            next_watermark,
            digest,
        )
        return cls(
            batch_id=batch_id,
            run_id=run_id,
            task_id=task_id,
            refs=ordered,
            source_watermark=source_watermark,
            next_watermark=next_watermark,
            high_watermark=high_watermark,
            batch_digest=digest,
            has_more=has_more,
            created_at=created_at or now_iso(),
            legacy_event_count=legacy_event_count,
            runtime_event_count=runtime_event_count,
            artifact_count=artifact_count,
            duplicate_source_ids=_string_tuple(duplicate_source_ids),
            warnings=_string_tuple(warnings),
            metadata=dict(metadata or {}),
        ).validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CuratorInputBatch:
        return cls(
            batch_id=_optional(value.get("batch_id")),
            run_id=_optional(value.get("run_id")),
            task_id=_optional(value.get("task_id")),
            refs=tuple(
                RuntimeTraceRef.from_dict(item)
                for item in _mapping_tuple(value.get("refs"))
            ),
            source_watermark=int(value.get("source_watermark", 0)),
            next_watermark=int(value.get("next_watermark", 0)),
            high_watermark=int(value.get("high_watermark", 0)),
            batch_digest=_optional(value.get("batch_digest")),
            has_more=bool(value.get("has_more", False)),
            created_at=_optional(value.get("created_at")) or now_iso(),
            legacy_event_count=int(value.get("legacy_event_count", 0)),
            runtime_event_count=int(value.get("runtime_event_count", 0)),
            artifact_count=int(value.get("artifact_count", 0)),
            duplicate_source_ids=_string_tuple(value.get("duplicate_source_ids") or ()),
            warnings=_string_tuple(value.get("warnings") or ()),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class CuratorOutcome:
    outcome_id: str
    run_id: str
    task_id: str
    curator_job_id: str
    curator_request_id: str
    kind: CuratorOutcomeKind
    state: CuratorOutcomeState
    candidate_id: str
    decision_id: str
    evidence_bundle_id: str
    evidence_digest: str
    subject: str
    summary: str
    payload: Mapping[str, Any]
    target_consumers: tuple[CuratorConsumer, ...]
    outcome_digest: str
    created_at: str
    memory_id: str = ""
    memory_revision: int = 0
    commit_receipt_id: str = ""
    causation_id: str = ""
    model_assisted: bool = False
    deterministic_validation: bool = True
    canonical_memory_changed: bool = False
    index_published: bool = False
    supersedes_outcome_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> CuratorOutcome:
        _required(self.outcome_id, "outcome_id")
        _required(self.run_id, "run_id")
        _required(self.task_id, "task_id")
        _required(self.curator_job_id, "curator_job_id")
        _required(self.curator_request_id, "curator_request_id")
        _required(self.candidate_id, "candidate_id")
        _required(self.evidence_bundle_id, "evidence_bundle_id")
        _digest(self.evidence_digest, "evidence_digest")
        _required(self.subject, "subject")
        _timestamp(self.created_at, "created_at")
        if self.memory_revision < 0:
            raise CuratorIntegrationContractError("memory_revision cannot be negative")
        if self.canonical_memory_changed:
            _required(self.decision_id, "decision_id")
            _required(self.memory_id, "memory_id")
            _required(self.commit_receipt_id, "commit_receipt_id")
            if self.memory_revision <= 0:
                raise CuratorIntegrationContractError(
                    "canonical mutation requires a positive memory_revision"
                )
            if not self.deterministic_validation:
                raise CuratorIntegrationContractError(
                    "canonical mutation cannot bypass deterministic validation"
                )
        if self.kind is CuratorOutcomeKind.INDEX_PUBLISHED and not self.index_published:
            raise CuratorIntegrationContractError(
                "index_published outcome must record publication"
            )
        expected = stable_digest(self.semantic_projection())
        if expected != self.outcome_digest:
            raise CuratorIntegrationContractError("outcome digest mismatch")
        return self

    def semantic_projection(self) -> Mapping[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "curator_job_id": self.curator_job_id,
            "curator_request_id": self.curator_request_id,
            "kind": self.kind.value,
            "candidate_id": self.candidate_id,
            "decision_id": self.decision_id,
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_digest": self.evidence_digest,
            "subject": self.subject,
            "summary": self.summary,
            "payload": dict(self.payload),
            "target_consumers": [consumer.value for consumer in self.target_consumers],
            "memory_id": self.memory_id,
            "memory_revision": self.memory_revision,
            "commit_receipt_id": self.commit_receipt_id,
            "model_assisted": self.model_assisted,
            "deterministic_validation": self.deterministic_validation,
            "canonical_memory_changed": self.canonical_memory_changed,
            "index_published": self.index_published,
            "supersedes_outcome_id": self.supersedes_outcome_id,
        }

    @property
    def terminal(self) -> bool:
        return self.state in {
            CuratorOutcomeState.PUBLISHED,
            CuratorOutcomeState.SUPERSEDED,
            CuratorOutcomeState.REVOKED,
        }

    @property
    def recallable(self) -> bool:
        return self.canonical_memory_changed and self.kind in {
            CuratorOutcomeKind.COMMITTED,
            CuratorOutcomeKind.INDEX_PUBLISHED,
            CuratorOutcomeKind.SKILL_CANDIDATE,
            CuratorOutcomeKind.FAILURE_PATTERN,
            CuratorOutcomeKind.COMPRESSED,
        }

    def publish(self) -> CuratorOutcome:
        if self.state is CuratorOutcomeState.PUBLISHED:
            return self
        if self.state is not CuratorOutcomeState.PROJECTED:
            raise CuratorProjectionConflictError(
                f"cannot publish outcome in state {self.state.value}"
            )
        return replace(self, state=CuratorOutcomeState.PUBLISHED).validated()

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_OUTCOME_PROTOCOL,
            "outcome_id": self.outcome_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "curator_job_id": self.curator_job_id,
            "curator_request_id": self.curator_request_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "candidate_id": self.candidate_id,
            "decision_id": self.decision_id,
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_digest": self.evidence_digest,
            "subject": self.subject,
            "summary": self.summary,
            "payload": dict(self.payload),
            "target_consumers": [consumer.value for consumer in self.target_consumers],
            "outcome_digest": self.outcome_digest,
            "created_at": self.created_at,
            "memory_id": self.memory_id,
            "memory_revision": self.memory_revision,
            "commit_receipt_id": self.commit_receipt_id,
            "causation_id": self.causation_id,
            "model_assisted": self.model_assisted,
            "deterministic_validation": self.deterministic_validation,
            "canonical_memory_changed": self.canonical_memory_changed,
            "index_published": self.index_published,
            "supersedes_outcome_id": self.supersedes_outcome_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def build(
        cls,
        *,
        result: CuratorRunResult,
        candidate: MemoryCandidate,
        kind: CuratorOutcomeKind,
        decision: MemoryDecision | None = None,
        receipt: MemoryCommitReceipt | None = None,
        payload: Mapping[str, Any] | None = None,
        target_consumers: Sequence[CuratorConsumer] = (),
        state: CuratorOutcomeState = CuratorOutcomeState.PROJECTED,
        index_published: bool = False,
        canonical_memory_changed: bool | None = None,
        supersedes_outcome_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> CuratorOutcome:
        committed = bool(
            receipt is not None
            and receipt.disposition
            in {CommitDisposition.COMMITTED, CommitDisposition.ALREADY_COMMITTED}
            and receipt.memory_id
            and receipt.memory_revision > 0
        )
        values = dict(payload or {})
        consumers = tuple(dict.fromkeys(target_consumers))
        memory_changed = (
            committed
            if canonical_memory_changed is None
            else bool(canonical_memory_changed)
        )
        if memory_changed and not committed:
            raise CuratorIntegrationContractError(
                "outcome cannot claim memory change without a committed receipt"
            )
        semantic = {
            "run_id": result.run_id,
            "task_id": result.task_id,
            "curator_job_id": result.job_id,
            "curator_request_id": result.request_id,
            "kind": kind.value,
            "candidate_id": candidate.candidate_id,
            "decision_id": decision.decision_id if decision else "",
            "evidence_bundle_id": candidate.evidence_bundle_id,
            "evidence_digest": candidate.evidence_digest,
            "subject": candidate.subject,
            "summary": (
                decision.validated_summary
                if decision and decision.validated_summary
                else candidate.summary
            ),
            "payload": values,
            "target_consumers": [consumer.value for consumer in consumers],
            "memory_id": receipt.memory_id if receipt else "",
            "memory_revision": receipt.memory_revision if receipt else 0,
            "commit_receipt_id": receipt.receipt_id if receipt else "",
            "model_assisted": candidate.model_assisted,
            "deterministic_validation": decision is not None,
            "canonical_memory_changed": memory_changed,
            "index_published": index_published,
            "supersedes_outcome_id": supersedes_outcome_id,
        }
        digest = stable_digest(semantic)
        return cls(
            outcome_id=stable_id(
                "curator_outcome",
                result.job_id,
                candidate.candidate_id,
                kind.value,
                digest,
            ),
            run_id=result.run_id,
            task_id=result.task_id,
            curator_job_id=result.job_id,
            curator_request_id=result.request_id,
            kind=kind,
            state=state,
            candidate_id=candidate.candidate_id,
            decision_id=decision.decision_id if decision else "",
            evidence_bundle_id=candidate.evidence_bundle_id,
            evidence_digest=candidate.evidence_digest,
            subject=candidate.subject,
            summary=semantic["summary"],
            payload=values,
            target_consumers=consumers,
            outcome_digest=digest,
            created_at=result.finished_at,
            memory_id=receipt.memory_id if receipt else "",
            memory_revision=receipt.memory_revision if receipt else 0,
            commit_receipt_id=receipt.receipt_id if receipt else "",
            causation_id=(
                receipt.receipt_id
                if receipt
                else decision.decision_id
                if decision
                else candidate.candidate_id
            ),
            model_assisted=candidate.model_assisted,
            deterministic_validation=decision is not None,
            canonical_memory_changed=memory_changed,
            index_published=index_published,
            supersedes_outcome_id=supersedes_outcome_id,
            metadata=dict(metadata or {}),
        ).validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CuratorOutcome:
        return cls(
            outcome_id=_optional(value.get("outcome_id")),
            run_id=_optional(value.get("run_id")),
            task_id=_optional(value.get("task_id")),
            curator_job_id=_optional(value.get("curator_job_id")),
            curator_request_id=_optional(value.get("curator_request_id")),
            kind=CuratorOutcomeKind(str(value.get("kind") or CuratorOutcomeKind.CANDIDATE.value)),
            state=CuratorOutcomeState(
                str(value.get("state") or CuratorOutcomeState.PROJECTED.value)
            ),
            candidate_id=_optional(value.get("candidate_id")),
            decision_id=_optional(value.get("decision_id")),
            evidence_bundle_id=_optional(value.get("evidence_bundle_id")),
            evidence_digest=_optional(value.get("evidence_digest")),
            subject=_optional(value.get("subject")),
            summary=_optional(value.get("summary")),
            payload=mapping(value.get("payload")),
            target_consumers=tuple(
                CuratorConsumer(str(item))
                for item in value.get("target_consumers") or ()
            ),
            outcome_digest=_optional(value.get("outcome_digest")),
            created_at=_optional(value.get("created_at")) or now_iso(),
            memory_id=_optional(value.get("memory_id")),
            memory_revision=int(value.get("memory_revision", 0)),
            commit_receipt_id=_optional(value.get("commit_receipt_id")),
            causation_id=_optional(value.get("causation_id")),
            model_assisted=bool(value.get("model_assisted", False)),
            deterministic_validation=bool(value.get("deterministic_validation", True)),
            canonical_memory_changed=bool(value.get("canonical_memory_changed", False)),
            index_published=bool(value.get("index_published", False)),
            supersedes_outcome_id=_optional(value.get("supersedes_outcome_id")),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class CuratorFailureContract:
    failure_id: str
    run_id: str
    task_id: str
    curator_job_id: str
    phase: str
    code: str
    message: str
    severity: FailureSeverity
    disposition: FailureDisposition
    retryable: bool
    attempt: int
    causation_id: str
    created_at: str
    candidate_id: str = ""
    decision_id: str = ""
    outcome_id: str = ""
    evidence_ids: tuple[str, ...] = ()
    consumer_hints: tuple[CuratorConsumer, ...] = ()
    canonical_memory_changed: bool = False
    operator_action_required: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> CuratorFailureContract:
        _required(self.failure_id, "failure_id")
        _required(self.run_id, "run_id")
        _required(self.task_id, "task_id")
        _required(self.curator_job_id, "curator_job_id")
        _required(self.phase, "phase")
        _required(self.code, "code")
        _required(self.message, "message")
        _positive(self.attempt, "attempt", allow_zero=True)
        _required(self.causation_id, "causation_id")
        _timestamp(self.created_at, "created_at")
        if self.operator_action_required and self.disposition is not FailureDisposition.OPERATOR:
            raise CuratorIntegrationContractError(
                "operator_action_required needs operator disposition"
            )
        if self.canonical_memory_changed:
            raise CuratorIntegrationContractError(
                "failure contracts cannot claim a canonical memory mutation"
            )
        return self

    @property
    def blocks_publication(self) -> bool:
        return self.severity in {FailureSeverity.ERROR, FailureSeverity.CRITICAL}

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_FAILURE_PROTOCOL,
            "failure_id": self.failure_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "curator_job_id": self.curator_job_id,
            "phase": self.phase,
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "disposition": self.disposition.value,
            "retryable": self.retryable,
            "attempt": self.attempt,
            "causation_id": self.causation_id,
            "created_at": self.created_at,
            "candidate_id": self.candidate_id,
            "decision_id": self.decision_id,
            "outcome_id": self.outcome_id,
            "evidence_ids": list(self.evidence_ids),
            "consumer_hints": [consumer.value for consumer in self.consumer_hints],
            "canonical_memory_changed": self.canonical_memory_changed,
            "operator_action_required": self.operator_action_required,
            "details": dict(self.details),
        }

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        curator_job_id: str,
        phase: str,
        code: str,
        message: str,
        severity: FailureSeverity,
        disposition: FailureDisposition,
        retryable: bool,
        attempt: int,
        causation_id: str,
        candidate_id: str = "",
        decision_id: str = "",
        outcome_id: str = "",
        evidence_ids: Sequence[str] = (),
        consumer_hints: Sequence[CuratorConsumer] = (),
        operator_action_required: bool = False,
        details: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> CuratorFailureContract:
        identity = stable_digest(
            {
                "run_id": run_id,
                "task_id": task_id,
                "curator_job_id": curator_job_id,
                "phase": phase,
                "code": code,
                "attempt": attempt,
                "candidate_id": candidate_id,
                "decision_id": decision_id,
                "outcome_id": outcome_id,
                "causation_id": causation_id,
            }
        )
        return cls(
            failure_id=stable_id("curator_failure", identity),
            run_id=run_id,
            task_id=task_id,
            curator_job_id=curator_job_id,
            phase=phase,
            code=code,
            message=message[:2000],
            severity=severity,
            disposition=disposition,
            retryable=retryable,
            attempt=attempt,
            causation_id=causation_id,
            created_at=created_at or now_iso(),
            candidate_id=candidate_id,
            decision_id=decision_id,
            outcome_id=outcome_id,
            evidence_ids=_string_tuple(evidence_ids),
            consumer_hints=tuple(dict.fromkeys(consumer_hints)),
            operator_action_required=operator_action_required,
            details=dict(details or {}),
        ).validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CuratorFailureContract:
        return cls(
            failure_id=_optional(value.get("failure_id")),
            run_id=_optional(value.get("run_id")),
            task_id=_optional(value.get("task_id")),
            curator_job_id=_optional(value.get("curator_job_id")),
            phase=_optional(value.get("phase")),
            code=_optional(value.get("code")),
            message=_optional(value.get("message")),
            severity=FailureSeverity(
                str(value.get("severity") or FailureSeverity.ERROR.value)
            ),
            disposition=FailureDisposition(
                str(value.get("disposition") or FailureDisposition.REJECT.value)
            ),
            retryable=bool(value.get("retryable", False)),
            attempt=int(value.get("attempt", 0)),
            causation_id=_optional(value.get("causation_id")),
            created_at=_optional(value.get("created_at")) or now_iso(),
            candidate_id=_optional(value.get("candidate_id")),
            decision_id=_optional(value.get("decision_id")),
            outcome_id=_optional(value.get("outcome_id")),
            evidence_ids=_string_tuple(value.get("evidence_ids") or ()),
            consumer_hints=tuple(
                CuratorConsumer(str(item))
                for item in value.get("consumer_hints") or ()
            ),
            canonical_memory_changed=bool(value.get("canonical_memory_changed", False)),
            operator_action_required=bool(value.get("operator_action_required", False)),
            details=mapping(value.get("details")),
        ).validated()


@dataclass(frozen=True, slots=True)
class CuratorDeliveryLease:
    delivery_id: str
    consumer: CuratorConsumer
    worker_id: str
    claim_token: str
    lease_epoch: int
    attempt: int
    expires_at: float

    def validated(self) -> CuratorDeliveryLease:
        _required(self.delivery_id, "delivery_id")
        _required(self.worker_id, "worker_id")
        _required(self.claim_token, "claim_token")
        _positive(self.lease_epoch, "lease_epoch")
        _positive(self.attempt, "attempt")
        _finite_float(self.expires_at, "expires_at", minimum=0.000001)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "consumer": self.consumer.value,
            "worker_id": self.worker_id,
            "claim_token": self.claim_token,
            "lease_epoch": self.lease_epoch,
            "attempt": self.attempt,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class CuratorDownstreamDelivery:
    delivery_id: str
    outcome_id: str
    run_id: str
    task_id: str
    consumer: CuratorConsumer
    state: CuratorDeliveryState
    payload_digest: str
    payload: Mapping[str, Any]
    available_at: float
    created_at: float
    updated_at: float
    attempt: int = 0
    retry_remaining: int = 5
    claimed_by: str = ""
    claim_token: str = ""
    lease_epoch: int = 0
    claim_expires_at: float | None = None
    acknowledged_at: float | None = None
    error_code: str = ""
    error_message: str = ""
    receipt: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> CuratorDownstreamDelivery:
        _required(self.delivery_id, "delivery_id")
        _required(self.outcome_id, "outcome_id")
        _required(self.run_id, "run_id")
        _required(self.task_id, "task_id")
        _digest(self.payload_digest, "payload_digest")
        _finite_float(self.available_at, "available_at")
        _finite_float(self.created_at, "created_at")
        _finite_float(self.updated_at, "updated_at")
        _positive(self.attempt, "attempt", allow_zero=True)
        _positive(self.retry_remaining, "retry_remaining", allow_zero=True)
        _positive(self.lease_epoch, "lease_epoch", allow_zero=True)
        if self.payload_digest != stable_digest(self.payload):
            raise CuratorIntegrationContractError("delivery payload digest mismatch")
        if self.state is CuratorDeliveryState.CLAIMED:
            _required(self.claimed_by, "claimed_by")
            _required(self.claim_token, "claim_token")
            if self.claim_expires_at is None:
                raise CuratorIntegrationContractError("claimed delivery lacks expiry")
        if self.state is CuratorDeliveryState.ACKNOWLEDGED and self.acknowledged_at is None:
            raise CuratorIntegrationContractError("acknowledged delivery lacks timestamp")
        return self

    @property
    def terminal(self) -> bool:
        return self.state in {
            CuratorDeliveryState.ACKNOWLEDGED,
            CuratorDeliveryState.DEAD,
            CuratorDeliveryState.CANCELLED,
        }

    @property
    def claimable(self) -> bool:
        return self.state in {
            CuratorDeliveryState.PENDING,
            CuratorDeliveryState.RETRY_WAIT,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_DELIVERY_PROTOCOL,
            "delivery_id": self.delivery_id,
            "outcome_id": self.outcome_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "consumer": self.consumer.value,
            "state": self.state.value,
            "payload_digest": self.payload_digest,
            "payload": dict(self.payload),
            "available_at": self.available_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempt": self.attempt,
            "retry_remaining": self.retry_remaining,
            "claimed_by": self.claimed_by,
            "claim_token": self.claim_token,
            "lease_epoch": self.lease_epoch,
            "claim_expires_at": self.claim_expires_at,
            "acknowledged_at": self.acknowledged_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "receipt": dict(self.receipt),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CuratorDownstreamDelivery:
        return cls(
            delivery_id=_optional(value.get("delivery_id")),
            outcome_id=_optional(value.get("outcome_id")),
            run_id=_optional(value.get("run_id")),
            task_id=_optional(value.get("task_id")),
            consumer=CuratorConsumer(str(value.get("consumer") or CuratorConsumer.AUDIT.value)),
            state=CuratorDeliveryState(
                str(value.get("state") or CuratorDeliveryState.PENDING.value)
            ),
            payload_digest=_optional(value.get("payload_digest")),
            payload=mapping(value.get("payload")),
            available_at=float(value.get("available_at", 0.0)),
            created_at=float(value.get("created_at", 0.0)),
            updated_at=float(value.get("updated_at", 0.0)),
            attempt=int(value.get("attempt", 0)),
            retry_remaining=int(value.get("retry_remaining", 5)),
            claimed_by=_optional(value.get("claimed_by")),
            claim_token=_optional(value.get("claim_token")),
            lease_epoch=int(value.get("lease_epoch", 0)),
            claim_expires_at=(
                float(value["claim_expires_at"])
                if value.get("claim_expires_at") is not None
                else None
            ),
            acknowledged_at=(
                float(value["acknowledged_at"])
                if value.get("acknowledged_at") is not None
                else None
            ),
            error_code=_optional(value.get("error_code")),
            error_message=_optional(value.get("error_message")),
            receipt=mapping(value.get("receipt")),
        ).validated()


@dataclass(frozen=True, slots=True)
class CuratorContextProof:
    proof_id: str
    run_id: str
    task_id: str
    outcome_id: str
    memory_id: str
    memory_revision: int
    query_id: str
    query_digest: str
    index_scope: str
    index_generation: int
    index_revision: str
    retrieved_memory_ids: tuple[str, ...]
    context_digest: str
    effect: ContextEffect
    consumer: CuratorConsumer
    created_at: str
    worker_request_id: str = ""
    session_id: str = ""
    message_count: int = 0
    total_chars: int = 0
    contains_index_dump: bool = False
    canonical_owner: str = "SQLiteStore.memory_records"
    index_owner: str = "MemoryIndexRuntime"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> CuratorContextProof:
        _required(self.proof_id, "proof_id")
        _required(self.run_id, "run_id")
        _required(self.task_id, "task_id")
        _required(self.outcome_id, "outcome_id")
        _required(self.memory_id, "memory_id")
        _positive(self.memory_revision, "memory_revision")
        _required(self.query_id, "query_id")
        _digest(self.query_digest, "query_digest")
        _required(self.index_scope, "index_scope")
        _positive(self.index_generation, "index_generation")
        _required(self.index_revision, "index_revision")
        _digest(self.context_digest, "context_digest")
        _timestamp(self.created_at, "created_at")
        _positive(self.message_count, "message_count", allow_zero=True)
        _positive(self.total_chars, "total_chars", allow_zero=True)
        if self.effect is ContextEffect.PRESENT and self.memory_id not in self.retrieved_memory_ids:
            raise CuratorIntegrationContractError(
                "present context effect must include the committed memory id"
            )
        if self.contains_index_dump:
            raise CuratorIntegrationContractError(
                "worker context proof cannot contain an index dump"
            )
        if self.canonical_owner != "SQLiteStore.memory_records":
            raise CuratorIntegrationContractError("context proof changed canonical memory owner")
        return self

    @property
    def verified(self) -> bool:
        return self.effect is ContextEffect.PRESENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_CONTEXT_PROTOCOL,
            "proof_id": self.proof_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "outcome_id": self.outcome_id,
            "memory_id": self.memory_id,
            "memory_revision": self.memory_revision,
            "query_id": self.query_id,
            "query_digest": self.query_digest,
            "index_scope": self.index_scope,
            "index_generation": self.index_generation,
            "index_revision": self.index_revision,
            "retrieved_memory_ids": list(self.retrieved_memory_ids),
            "context_digest": self.context_digest,
            "effect": self.effect.value,
            "consumer": self.consumer.value,
            "created_at": self.created_at,
            "worker_request_id": self.worker_request_id,
            "session_id": self.session_id,
            "message_count": self.message_count,
            "total_chars": self.total_chars,
            "contains_index_dump": self.contains_index_dump,
            "canonical_owner": self.canonical_owner,
            "index_owner": self.index_owner,
            "verified": self.verified,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def build(
        cls,
        *,
        outcome: CuratorOutcome,
        query_id: str,
        query_text: str,
        index_scope: str,
        index_generation: int,
        index_revision: str,
        retrieved_memory_ids: Sequence[str],
        context_payload: object,
        consumer: CuratorConsumer = CuratorConsumer.RETRIEVAL_CONTEXT,
        worker_request_id: str = "",
        session_id: str = "",
        message_count: int = 0,
        total_chars: int = 0,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> CuratorContextProof:
        memory_ids = _string_tuple(retrieved_memory_ids)
        effect = (
            ContextEffect.PRESENT
            if outcome.memory_id in memory_ids
            else ContextEffect.ABSENT
        )
        query_digest = stable_digest(query_text)
        context_digest = stable_digest(context_payload)
        proof_id = stable_id(
            "curator_context",
            outcome.outcome_id,
            query_id,
            index_revision,
            context_digest,
        )
        return cls(
            proof_id=proof_id,
            run_id=outcome.run_id,
            task_id=outcome.task_id,
            outcome_id=outcome.outcome_id,
            memory_id=outcome.memory_id,
            memory_revision=outcome.memory_revision,
            query_id=query_id,
            query_digest=query_digest,
            index_scope=index_scope,
            index_generation=index_generation,
            index_revision=index_revision,
            retrieved_memory_ids=memory_ids,
            context_digest=context_digest,
            effect=effect,
            consumer=consumer,
            created_at=created_at or now_iso(),
            worker_request_id=worker_request_id,
            session_id=session_id,
            message_count=message_count,
            total_chars=total_chars,
            metadata=dict(metadata or {}),
        ).validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CuratorContextProof:
        return cls(
            proof_id=_optional(value.get("proof_id")),
            run_id=_optional(value.get("run_id")),
            task_id=_optional(value.get("task_id")),
            outcome_id=_optional(value.get("outcome_id")),
            memory_id=_optional(value.get("memory_id")),
            memory_revision=int(value.get("memory_revision", 0)),
            query_id=_optional(value.get("query_id")),
            query_digest=_optional(value.get("query_digest")),
            index_scope=_optional(value.get("index_scope")),
            index_generation=int(value.get("index_generation", 0)),
            index_revision=_optional(value.get("index_revision")),
            retrieved_memory_ids=_string_tuple(value.get("retrieved_memory_ids") or ()),
            context_digest=_optional(value.get("context_digest")),
            effect=ContextEffect(str(value.get("effect") or ContextEffect.ABSENT.value)),
            consumer=CuratorConsumer(
                str(value.get("consumer") or CuratorConsumer.RETRIEVAL_CONTEXT.value)
            ),
            created_at=_optional(value.get("created_at")) or now_iso(),
            worker_request_id=_optional(value.get("worker_request_id")),
            session_id=_optional(value.get("session_id")),
            message_count=int(value.get("message_count", 0)),
            total_chars=int(value.get("total_chars", 0)),
            contains_index_dump=bool(value.get("contains_index_dump", False)),
            canonical_owner=_optional(value.get("canonical_owner"))
            or "SQLiteStore.memory_records",
            index_owner=_optional(value.get("index_owner")) or "MemoryIndexRuntime",
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class CuratorIntegrationRun:
    integration_run_id: str
    run_id: str
    task_id: str
    curator_job_id: str
    curator_request_id: str
    state: CuratorIntegrationRunState
    input_batch_id: str
    input_digest: str
    started_at: str
    updated_at: str
    outcome_ids: tuple[str, ...] = ()
    failure_ids: tuple[str, ...] = ()
    delivery_ids: tuple[str, ...] = ()
    context_proof_ids: tuple[str, ...] = ()
    model_status: str = ""
    canonical_commit_count: int = 0
    index_publication_count: int = 0
    error_code: str = ""
    error_message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> CuratorIntegrationRun:
        _required(self.integration_run_id, "integration_run_id")
        _required(self.run_id, "run_id")
        _required(self.task_id, "task_id")
        _required(self.curator_job_id, "curator_job_id")
        _required(self.curator_request_id, "curator_request_id")
        _required(self.input_batch_id, "input_batch_id")
        _digest(self.input_digest, "input_digest")
        _timestamp(self.started_at, "started_at")
        _timestamp(self.updated_at, "updated_at")
        _positive(self.canonical_commit_count, "canonical_commit_count", allow_zero=True)
        _positive(self.index_publication_count, "index_publication_count", allow_zero=True)
        if self.state is CuratorIntegrationRunState.FAILED and not self.error_code:
            raise CuratorIntegrationContractError("failed integration run lacks error_code")
        if self.state is CuratorIntegrationRunState.SUCCEEDED and self.error_code:
            raise CuratorIntegrationContractError("successful integration run retains error_code")
        return self

    @property
    def terminal(self) -> bool:
        return self.state in {
            CuratorIntegrationRunState.SUCCEEDED,
            CuratorIntegrationRunState.FAILED,
        }

    def transition(
        self,
        state: CuratorIntegrationRunState,
        *,
        outcome_ids: Sequence[str] | None = None,
        failure_ids: Sequence[str] | None = None,
        delivery_ids: Sequence[str] | None = None,
        context_proof_ids: Sequence[str] | None = None,
        model_status: str | None = None,
        canonical_commit_count: int | None = None,
        index_publication_count: int | None = None,
        error_code: str = "",
        error_message: str = "",
        metadata: Mapping[str, Any] | None = None,
        updated_at: str | None = None,
    ) -> CuratorIntegrationRun:
        allowed = {
            CuratorIntegrationRunState.STARTED: {
                CuratorIntegrationRunState.CURATOR_SUCCEEDED,
                CuratorIntegrationRunState.FAILED,
            },
            CuratorIntegrationRunState.CURATOR_SUCCEEDED: {
                CuratorIntegrationRunState.PROJECTING,
                CuratorIntegrationRunState.FAILED,
            },
            CuratorIntegrationRunState.PROJECTING: {
                CuratorIntegrationRunState.PUBLISHED,
                CuratorIntegrationRunState.FAILED,
            },
            CuratorIntegrationRunState.PUBLISHED: {
                CuratorIntegrationRunState.CONTEXT_VERIFIED,
                CuratorIntegrationRunState.SUCCEEDED,
                CuratorIntegrationRunState.FAILED,
            },
            CuratorIntegrationRunState.CONTEXT_VERIFIED: {
                CuratorIntegrationRunState.SUCCEEDED,
                CuratorIntegrationRunState.FAILED,
            },
            CuratorIntegrationRunState.SUCCEEDED: set(),
            CuratorIntegrationRunState.FAILED: set(),
        }
        if state is self.state:
            return self
        if state not in allowed[self.state]:
            raise CuratorProjectionConflictError(
                f"invalid integration transition {self.state.value} -> {state.value}"
            )
        merged_metadata = {**dict(self.metadata), **dict(metadata or {})}
        return replace(
            self,
            state=state,
            outcome_ids=(
                _string_tuple(outcome_ids)
                if outcome_ids is not None
                else self.outcome_ids
            ),
            failure_ids=(
                _string_tuple(failure_ids)
                if failure_ids is not None
                else self.failure_ids
            ),
            delivery_ids=(
                _string_tuple(delivery_ids)
                if delivery_ids is not None
                else self.delivery_ids
            ),
            context_proof_ids=(
                _string_tuple(context_proof_ids)
                if context_proof_ids is not None
                else self.context_proof_ids
            ),
            model_status=model_status if model_status is not None else self.model_status,
            canonical_commit_count=(
                canonical_commit_count
                if canonical_commit_count is not None
                else self.canonical_commit_count
            ),
            index_publication_count=(
                index_publication_count
                if index_publication_count is not None
                else self.index_publication_count
            ),
            error_code=error_code,
            error_message=error_message[:2000],
            metadata=merged_metadata,
            updated_at=updated_at or now_iso(),
        ).validated()

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_INTEGRATION_PROTOCOL,
            "integration_run_id": self.integration_run_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "curator_job_id": self.curator_job_id,
            "curator_request_id": self.curator_request_id,
            "state": self.state.value,
            "input_batch_id": self.input_batch_id,
            "input_digest": self.input_digest,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "outcome_ids": list(self.outcome_ids),
            "failure_ids": list(self.failure_ids),
            "delivery_ids": list(self.delivery_ids),
            "context_proof_ids": list(self.context_proof_ids),
            "model_status": self.model_status,
            "canonical_commit_count": self.canonical_commit_count,
            "index_publication_count": self.index_publication_count,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "terminal": self.terminal,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def start(
        cls,
        *,
        result: CuratorRunResult,
        input_batch: CuratorInputBatch,
        metadata: Mapping[str, Any] | None = None,
        started_at: str | None = None,
    ) -> CuratorIntegrationRun:
        identity = stable_id(
            "curator_integration",
            result.job_id,
            result.request_id,
            input_batch.batch_id,
            input_batch.batch_digest,
        )
        timestamp = started_at or result.started_at or now_iso()
        return cls(
            integration_run_id=identity,
            run_id=result.run_id,
            task_id=result.task_id,
            curator_job_id=result.job_id,
            curator_request_id=result.request_id,
            state=CuratorIntegrationRunState.STARTED,
            input_batch_id=input_batch.batch_id,
            input_digest=input_batch.batch_digest,
            started_at=timestamp,
            updated_at=timestamp,
            model_status=result.model_status,
            metadata=dict(metadata or {}),
        ).validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CuratorIntegrationRun:
        return cls(
            integration_run_id=_optional(value.get("integration_run_id")),
            run_id=_optional(value.get("run_id")),
            task_id=_optional(value.get("task_id")),
            curator_job_id=_optional(value.get("curator_job_id")),
            curator_request_id=_optional(value.get("curator_request_id")),
            state=CuratorIntegrationRunState(
                str(value.get("state") or CuratorIntegrationRunState.STARTED.value)
            ),
            input_batch_id=_optional(value.get("input_batch_id")),
            input_digest=_optional(value.get("input_digest")),
            started_at=_optional(value.get("started_at")) or now_iso(),
            updated_at=_optional(value.get("updated_at")) or now_iso(),
            outcome_ids=_string_tuple(value.get("outcome_ids") or ()),
            failure_ids=_string_tuple(value.get("failure_ids") or ()),
            delivery_ids=_string_tuple(value.get("delivery_ids") or ()),
            context_proof_ids=_string_tuple(value.get("context_proof_ids") or ()),
            model_status=_optional(value.get("model_status")),
            canonical_commit_count=int(value.get("canonical_commit_count", 0)),
            index_publication_count=int(value.get("index_publication_count", 0)),
            error_code=_optional(value.get("error_code")),
            error_message=_optional(value.get("error_message")),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class CuratorProjectionReport:
    integration_run: CuratorIntegrationRun
    outcomes: tuple[CuratorOutcome, ...]
    failures: tuple[CuratorFailureContract, ...]
    deliveries: tuple[CuratorDownstreamDelivery, ...]
    context_proofs: tuple[CuratorContextProof, ...] = ()
    duplicate_outcome_ids: tuple[str, ...] = ()
    replayed_delivery_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def validated(self) -> CuratorProjectionReport:
        run = self.integration_run.validated()
        if any(item.task_id != run.task_id or item.run_id != run.run_id for item in self.outcomes):
            raise CuratorIntegrationContractError("projection report contains foreign outcome")
        if any(item.task_id != run.task_id or item.run_id != run.run_id for item in self.failures):
            raise CuratorIntegrationContractError("projection report contains foreign failure")
        if any(item.task_id != run.task_id or item.run_id != run.run_id for item in self.deliveries):
            raise CuratorIntegrationContractError("projection report contains foreign delivery")
        if any(item.task_id != run.task_id or item.run_id != run.run_id for item in self.context_proofs):
            raise CuratorIntegrationContractError("projection report contains foreign context proof")
        return self

    @property
    def canonical_commit_count(self) -> int:
        return sum(1 for item in self.outcomes if item.canonical_memory_changed)

    @property
    def index_publication_count(self) -> int:
        return sum(1 for item in self.outcomes if item.index_published)

    @property
    def pending_delivery_count(self) -> int:
        return sum(1 for item in self.deliveries if not item.terminal)

    @property
    def ok(self) -> bool:
        return self.integration_run.state is CuratorIntegrationRunState.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_INTEGRATION_PROTOCOL,
            "ok": self.ok,
            "integration_run": self.integration_run.to_dict(),
            "outcomes": [item.to_dict() for item in self.outcomes],
            "failures": [item.to_dict() for item in self.failures],
            "deliveries": [item.to_dict() for item in self.deliveries],
            "context_proofs": [item.to_dict() for item in self.context_proofs],
            "canonical_commit_count": self.canonical_commit_count,
            "index_publication_count": self.index_publication_count,
            "pending_delivery_count": self.pending_delivery_count,
            "duplicate_outcome_ids": list(self.duplicate_outcome_ids),
            "replayed_delivery_ids": list(self.replayed_delivery_ids),
            "warnings": list(self.warnings),
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "model_can_write": False,
        }


def consumers_for_outcome(
    kind: CuratorOutcomeKind,
    *,
    canonical_memory_changed: bool,
) -> tuple[CuratorConsumer, ...]:
    values: list[CuratorConsumer] = [CuratorConsumer.AUDIT]
    if canonical_memory_changed:
        values.extend(
            [
                CuratorConsumer.RETRIEVAL_CONTEXT,
                CuratorConsumer.COMPACT_RUNTIME,
            ]
        )
    if kind is CuratorOutcomeKind.SKILL_CANDIDATE:
        values.append(CuratorConsumer.SKILL_MEMORY)
    if kind is CuratorOutcomeKind.FAILURE_PATTERN:
        values.extend(
            [
                CuratorConsumer.FAULT_OBSERVER,
                CuratorConsumer.RECOVERY_PLANNER,
            ]
        )
    if kind in {
        CuratorOutcomeKind.REJECTED,
        CuratorOutcomeKind.MERGE_REQUIRED,
        CuratorOutcomeKind.SUPERSEDED,
    }:
        values.append(CuratorConsumer.FAULT_OBSERVER)
    return tuple(dict.fromkeys(values))


def outcome_kind_for_candidate(
    candidate: MemoryCandidate,
    decision: MemoryDecision | None,
    receipt: MemoryCommitReceipt | None,
) -> CuratorOutcomeKind:
    if decision is None:
        return CuratorOutcomeKind.CANDIDATE
    if decision.status is DecisionStatus.REJECT:
        return CuratorOutcomeKind.REJECTED
    if decision.status is DecisionStatus.MERGE_REQUIRED:
        return CuratorOutcomeKind.MERGE_REQUIRED
    if decision.status is DecisionStatus.SUPERSEDE:
        return CuratorOutcomeKind.SUPERSEDED
    if decision.status in {DecisionStatus.DISCARD, DecisionStatus.NOOP}:
        return (
            CuratorOutcomeKind.DISCARDED
            if candidate.kind is CandidateKind.DISCARD
            else CuratorOutcomeKind.NOOP
        )
    if receipt is not None and receipt.disposition in {
        CommitDisposition.COMMITTED,
        CommitDisposition.ALREADY_COMMITTED,
    }:
        if candidate.kind is CandidateKind.SKILL_CANDIDATE:
            return CuratorOutcomeKind.SKILL_CANDIDATE
        if candidate.kind is CandidateKind.FAILURE_PATTERN:
            return CuratorOutcomeKind.FAILURE_PATTERN
        if candidate.kind is CandidateKind.COMPRESS:
            return CuratorOutcomeKind.COMPRESSED
        if candidate.kind is CandidateKind.DISCARD:
            return CuratorOutcomeKind.DISCARDED
        return CuratorOutcomeKind.COMMITTED
    return CuratorOutcomeKind.ACCEPTED


def failure_from_decision(
    *,
    result: CuratorRunResult,
    candidate: MemoryCandidate,
    decision: MemoryDecision,
    outcome_id: str,
) -> CuratorFailureContract | None:
    issues = tuple(issue for issue in decision.issues if issue.code is not DecisionCode.OK)
    if not issues:
        return None
    primary = issues[0]
    security_codes = {
        DecisionCode.SECRET_DETECTED,
        DecisionCode.RULE_MUTATION_FORBIDDEN,
        DecisionCode.EVIDENCE_FORGED,
    }
    conflict_codes = {
        DecisionCode.CONTRADICTION,
        DecisionCode.REVISION_CONFLICT,
        DecisionCode.DUPLICATE,
    }
    severity = (
        FailureSeverity.CRITICAL
        if primary.code in security_codes
        else FailureSeverity.WARNING
        if primary.code in conflict_codes
        else FailureSeverity.ERROR
    )
    disposition = (
        FailureDisposition.QUARANTINE
        if primary.code in security_codes
        else FailureDisposition.REPLAN
        if primary.code in conflict_codes
        else FailureDisposition.RETRY
        if primary.retryable
        else FailureDisposition.REJECT
    )
    return CuratorFailureContract.build(
        run_id=result.run_id,
        task_id=result.task_id,
        curator_job_id=result.job_id,
        phase="deterministic_validation",
        code=primary.code.value,
        message=primary.message,
        severity=severity,
        disposition=disposition,
        retryable=primary.retryable,
        attempt=0,
        causation_id=decision.decision_id,
        candidate_id=candidate.candidate_id,
        decision_id=decision.decision_id,
        outcome_id=outcome_id,
        evidence_ids=candidate.evidence_ids,
        consumer_hints=(
            CuratorConsumer.FAULT_OBSERVER,
            CuratorConsumer.RECOVERY_PLANNER,
        ),
        details={
            "issues": [issue.to_dict() for issue in issues],
            "candidate_kind": candidate.kind.value,
            "canonical_memory_changed": False,
            "deterministic_validator": True,
        },
    )


__all__ = [
    "CURATOR_AUDIT_PROTOCOL",
    "CURATOR_CONTEXT_PROTOCOL",
    "CURATOR_DELIVERY_PROTOCOL",
    "CURATOR_FAILURE_PROTOCOL",
    "CURATOR_INPUT_PROTOCOL",
    "CURATOR_INTEGRATION_PROTOCOL",
    "CURATOR_OUTCOME_PROTOCOL",
    "ContextEffect",
    "CuratorConsumer",
    "CuratorContextProof",
    "CuratorContextVerificationError",
    "CuratorDeliveryConflictError",
    "CuratorDeliveryLease",
    "CuratorDeliveryState",
    "CuratorDownstreamDelivery",
    "CuratorFailureContract",
    "CuratorInputBatch",
    "CuratorInputSource",
    "CuratorIntegrationContractError",
    "CuratorIntegrationRun",
    "CuratorIntegrationRunState",
    "CuratorOutcome",
    "CuratorOutcomeKind",
    "CuratorOutcomeState",
    "CuratorProjectionConflictError",
    "CuratorProjectionReport",
    "FailureDisposition",
    "FailureSeverity",
    "RuntimeTraceKind",
    "RuntimeTraceRef",
    "consumers_for_outcome",
    "failure_from_decision",
    "outcome_kind_for_candidate",
]
