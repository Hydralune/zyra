from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import now_iso


CURATOR_PROTOCOL = "zyra.memory-curator.v1"
EVIDENCE_PROTOCOL = "zyra.memory-evidence.v1"
DECISION_PROTOCOL = "zyra.memory-decision.v1"
COMMIT_PROTOCOL = "zyra.memory-commit.v1"
EXTRACTOR_VERSION = "event-artifact-trace-extractor/1"


class EvidenceKind(StrEnum):
    EVENT = "event"
    ARTIFACT = "artifact"
    TOOL_RESULT = "tool_result"
    BROWSER_TRACE = "browser_trace"
    CODE_TRACE = "code_trace"
    CHECKPOINT = "checkpoint"


class TrustTier(StrEnum):
    SYSTEM = "system"
    VERIFIED_RUNTIME = "verified_runtime"
    USER = "user"
    TOOL = "tool"
    EXTERNAL = "external"
    MODEL_PROPOSAL = "model_proposal"
    UNKNOWN = "unknown"


class CandidateKind(StrEnum):
    PROMOTE = "promote"
    DISCARD = "discard"
    COMPRESS = "compress"
    SKILL_CANDIDATE = "skill_candidate"
    FAILURE_PATTERN = "failure_pattern"


class CandidateState(StrEnum):
    PROPOSED = "proposed"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMMITTED = "committed"
    SUPERSEDED = "superseded"
    MERGED = "merged"


class MemoryScope(StrEnum):
    TASK = "task"
    RUN = "run"
    PROJECT = "project"
    GLOBAL = "global"


class CandidateRelationKind(StrEnum):
    DUPLICATE_OF = "duplicate_of"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    MERGED_INTO = "merged_into"
    SUPPORTS = "supports"


class DecisionStatus(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    MERGE_REQUIRED = "merge_required"
    SUPERSEDE = "supersede"
    DISCARD = "discard"
    NOOP = "noop"


class DecisionCode(StrEnum):
    OK = "ok"
    SCHEMA_INVALID = "schema_invalid"
    SCOPE_INVALID = "scope_invalid"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_FORGED = "evidence_forged"
    EVIDENCE_RANGE_INVALID = "evidence_range_invalid"
    PROVENANCE_INVALID = "provenance_invalid"
    TRUST_INSUFFICIENT = "trust_insufficient"
    SECRET_DETECTED = "secret_detected"
    RULE_MUTATION_FORBIDDEN = "rule_mutation_forbidden"
    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"
    REVISION_CONFLICT = "revision_conflict"
    TTL_INVALID = "ttl_invalid"
    RETENTION_DENIED = "retention_denied"
    EMPTY_CONTENT = "empty_content"
    LOW_CONFIDENCE = "low_confidence"
    CANDIDATE_SUPERSEDED = "candidate_superseded"
    MODEL_UNAVAILABLE = "model_unavailable"
    LEASE_LOST = "lease_lost"
    INTERNAL_ERROR = "internal_error"


class CuratorTrigger(StrEnum):
    MANUAL = "manual"
    TASK_END = "task_end"
    RECOVERY = "recovery"


class CuratorJobState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    EXTRACTING = "extracting"
    DECIDING = "deciding"
    VALIDATING = "validating"
    COMMITTING = "committing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_WAIT = "retry_wait"
    STALE = "stale"
    CANCELLED = "cancelled"


class OutboxKind(StrEnum):
    EVENT = "event"
    INDEX_SYNC = "index_sync"


class OutboxState(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    RETRY_WAIT = "retry_wait"
    DEAD = "dead"


class CommitDisposition(StrEnum):
    COMMITTED = "committed"
    ALREADY_COMMITTED = "already_committed"
    REJECTED = "rejected"
    MERGE_REQUIRED = "merge_required"
    SUPERSEDED = "superseded"
    NOOP = "noop"


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def stable_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    digest = stable_digest([str(part) for part in parts])[:24]
    return f"{prefix}_{digest}"


def parse_timestamp(value: str) -> datetime:
    raw = str(value).strip()
    if not raw:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def unique_strings(values: Sequence[object]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return tuple(output)


def mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class EvidenceRange:
    start_sequence: int
    end_sequence: int
    start_event_id: str = ""
    end_event_id: str = ""

    def validated(self) -> EvidenceRange:
        if self.start_sequence < 0:
            raise ValueError("evidence range start must be non-negative")
        if self.end_sequence < self.start_sequence:
            raise ValueError("evidence range end precedes start")
        return self

    @property
    def length(self) -> int:
        return self.end_sequence - self.start_sequence + 1

    def contains(self, sequence: int) -> bool:
        return self.start_sequence <= int(sequence) <= self.end_sequence

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "start_event_id": self.start_event_id,
            "end_event_id": self.end_event_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceRange:
        return cls(
            start_sequence=int(value.get("start_sequence", 0)),
            end_sequence=int(value.get("end_sequence", 0)),
            start_event_id=str(value.get("start_event_id") or ""),
            end_event_id=str(value.get("end_event_id") or ""),
        ).validated()


@dataclass(frozen=True, slots=True)
class CuratorEvidenceRef:
    evidence_id: str
    kind: EvidenceKind
    run_id: str
    task_id: str
    source_id: str
    source_revision: str
    content_digest: str
    trust: TrustTier
    sequence: int
    created_at: str
    artifact_id: str = ""
    event_id: str = ""
    node_id: str = ""
    producer: str = ""
    media_type: str = "application/json"
    char_count: int = 0
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def validated(self) -> CuratorEvidenceRef:
        if not self.evidence_id.strip():
            raise ValueError("evidence_id is required")
        if not self.run_id.strip() or not self.task_id.strip():
            raise ValueError("evidence requires run/task identity")
        if not self.source_id.strip():
            raise ValueError("evidence source_id is required")
        if len(self.content_digest) != 64:
            raise ValueError("evidence content_digest must be sha256")
        if self.sequence < 0:
            raise ValueError("evidence sequence must be non-negative")
        if self.char_count < 0:
            raise ValueError("evidence char_count must be non-negative")
        parse_timestamp(self.created_at)
        return self

    def identity_projection(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "content_digest": self.content_digest,
            "trust": self.trust.value,
            "sequence": self.sequence,
            "artifact_id": self.artifact_id,
            "event_id": self.event_id,
            "node_id": self.node_id,
            "producer": self.producer,
            "media_type": self.media_type,
            "char_count": self.char_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_projection(),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CuratorEvidenceRef:
        return cls(
            evidence_id=str(value.get("evidence_id") or ""),
            kind=EvidenceKind(str(value.get("kind") or EvidenceKind.EVENT.value)),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            source_id=str(value.get("source_id") or ""),
            source_revision=str(value.get("source_revision") or ""),
            content_digest=str(value.get("content_digest") or ""),
            trust=TrustTier(str(value.get("trust") or TrustTier.UNKNOWN.value)),
            sequence=int(value.get("sequence", 0)),
            created_at=str(value.get("created_at") or now_iso()),
            artifact_id=str(value.get("artifact_id") or ""),
            event_id=str(value.get("event_id") or ""),
            node_id=str(value.get("node_id") or ""),
            producer=str(value.get("producer") or ""),
            media_type=str(value.get("media_type") or "application/json"),
            char_count=int(value.get("char_count", 0)),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class EvidenceDocument:
    ref: CuratorEvidenceRef
    title: str
    text: str
    normalized: Mapping[str, Any]
    redacted: bool = False
    secret_fingerprints: tuple[str, ...] = ()

    def validated(self) -> EvidenceDocument:
        self.ref.validated()
        if not self.title.strip():
            raise ValueError("evidence title is required")
        expected = stable_digest(self.normalized)
        if expected != self.ref.content_digest:
            raise ValueError("evidence document digest does not match ref")
        return self

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        output = {
            "ref": self.ref.to_dict(),
            "title": self.title,
            "normalized": dict(self.normalized),
            "redacted": self.redacted,
            "secret_fingerprints": list(self.secret_fingerprints),
        }
        if include_text:
            output["text"] = self.text
        return output

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceDocument:
        return cls(
            ref=CuratorEvidenceRef.from_dict(mapping(value.get("ref"))),
            title=str(value.get("title") or ""),
            text=str(value.get("text") or ""),
            normalized=mapping(value.get("normalized")),
            redacted=bool(value.get("redacted", False)),
            secret_fingerprints=unique_strings(value.get("secret_fingerprints") or ()),
        ).validated()


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    bundle_id: str
    run_id: str
    task_id: str
    evidence_range: EvidenceRange
    evidence_refs: tuple[CuratorEvidenceRef, ...]
    bundle_digest: str
    extractor_version: str = EXTRACTOR_VERSION
    created_at: str = dataclass_field(default_factory=now_iso)
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def projection(self) -> dict[str, Any]:
        return {
            "protocol": EVIDENCE_PROTOCOL,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "evidence_range": self.evidence_range.to_dict(),
            "evidence_refs": [item.identity_projection() for item in self.evidence_refs],
            "extractor_version": self.extractor_version,
        }

    def validated(self) -> EvidenceBundle:
        self.evidence_range.validated()
        if not self.bundle_id.strip():
            raise ValueError("bundle_id is required")
        if not self.run_id.strip() or not self.task_id.strip():
            raise ValueError("bundle run/task identity is required")
        if not self.evidence_refs:
            raise ValueError("evidence bundle cannot be empty")
        for ref in self.evidence_refs:
            ref.validated()
            if ref.run_id != self.run_id or ref.task_id != self.task_id:
                raise ValueError("cross-run/task evidence is forbidden")
            if not self.evidence_range.contains(ref.sequence):
                raise ValueError("evidence ref escapes declared range")
        if stable_digest(self.projection()) != self.bundle_digest:
            raise ValueError("bundle digest mismatch")
        parse_timestamp(self.created_at)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            **self.projection(),
            "bundle_digest": self.bundle_digest,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        evidence_refs: Sequence[CuratorEvidenceRef],
        extractor_version: str = EXTRACTOR_VERSION,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvidenceBundle:
        refs = tuple(sorted(evidence_refs, key=lambda item: (item.sequence, item.evidence_id)))
        if not refs:
            raise ValueError("evidence bundle cannot be empty")
        evidence_range = EvidenceRange(
            start_sequence=refs[0].sequence,
            end_sequence=refs[-1].sequence,
            start_event_id=next((item.event_id for item in refs if item.event_id), ""),
            end_event_id=next((item.event_id for item in reversed(refs) if item.event_id), ""),
        )
        draft = cls(
            bundle_id="pending",
            run_id=run_id,
            task_id=task_id,
            evidence_range=evidence_range,
            evidence_refs=refs,
            bundle_digest="",
            extractor_version=extractor_version,
            metadata=dict(metadata or {}),
        )
        digest = stable_digest(draft.projection())
        result = replace(draft, bundle_id=stable_id("bundle", run_id, task_id, digest), bundle_digest=digest)
        return result.validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceBundle:
        return cls(
            bundle_id=str(value.get("bundle_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            evidence_range=EvidenceRange.from_dict(mapping(value.get("evidence_range"))),
            evidence_refs=tuple(
                CuratorEvidenceRef.from_dict(mapping(item))
                for item in value.get("evidence_refs") or ()
                if isinstance(item, Mapping)
            ),
            bundle_digest=str(value.get("bundle_digest") or ""),
            extractor_version=str(value.get("extractor_version") or EXTRACTOR_VERSION),
            created_at=str(value.get("created_at") or now_iso()),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    candidate_id: str
    run_id: str
    task_id: str
    kind: CandidateKind
    proposed_layer: str
    scope: MemoryScope
    subject: str
    summary: str
    content: Mapping[str, Any]
    evidence_bundle_id: str
    evidence_digest: str
    evidence_range: EvidenceRange
    evidence_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    confidence: float
    ttl_seconds: int | None
    extractor_version: str
    idempotency_key: str
    state: CandidateState = CandidateState.PROPOSED
    expected_memory_id: str = ""
    expected_revision: int | None = None
    model_assisted: bool = False
    proposer: str = "deterministic"
    created_at: str = dataclass_field(default_factory=now_iso)
    updated_at: str = dataclass_field(default_factory=now_iso)
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def validated(self) -> MemoryCandidate:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id is required")
        if not self.run_id.strip() or not self.task_id.strip():
            raise ValueError("candidate run/task identity is required")
        if not self.subject.strip():
            raise ValueError("candidate subject is required")
        if not self.summary.strip() and self.kind is not CandidateKind.DISCARD:
            raise ValueError("candidate summary is required")
        if not self.evidence_bundle_id.strip() or len(self.evidence_digest) != 64:
            raise ValueError("candidate evidence identity is invalid")
        self.evidence_range.validated()
        if not self.evidence_ids:
            raise ValueError("candidate evidence_ids cannot be empty")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("candidate confidence must be between zero and one")
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ValueError("candidate ttl_seconds must be positive")
        if not self.extractor_version.strip() or not self.idempotency_key.strip():
            raise ValueError("candidate extractor/idempotency identity is required")
        if self.expected_revision is not None and self.expected_revision < 0:
            raise ValueError("candidate expected_revision must be non-negative")
        parse_timestamp(self.created_at)
        parse_timestamp(self.updated_at)
        return self

    def semantic_projection(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "kind": self.kind.value,
            "proposed_layer": self.proposed_layer,
            "scope": self.scope.value,
            "subject": self.subject,
            "summary": self.summary,
            "content": dict(self.content),
            "evidence_digest": self.evidence_digest,
            "evidence_ids": list(self.evidence_ids),
            "artifact_ids": list(self.artifact_ids),
            "confidence": round(float(self.confidence), 6),
            "ttl_seconds": self.ttl_seconds,
            "expected_memory_id": self.expected_memory_id,
            "expected_revision": self.expected_revision,
        }

    @property
    def semantic_digest(self) -> str:
        return stable_digest(self.semantic_projection())

    def with_state(self, state: CandidateState, *, at: str | None = None) -> MemoryCandidate:
        return replace(self, state=state, updated_at=at or now_iso()).validated()

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_PROTOCOL,
            "candidate_id": self.candidate_id,
            **self.semantic_projection(),
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_range": self.evidence_range.to_dict(),
            "extractor_version": self.extractor_version,
            "idempotency_key": self.idempotency_key,
            "state": self.state.value,
            "model_assisted": self.model_assisted,
            "proposer": self.proposer,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        kind: CandidateKind,
        proposed_layer: str,
        scope: MemoryScope,
        subject: str,
        summary: str,
        content: Mapping[str, Any],
        bundle: EvidenceBundle,
        confidence: float,
        ttl_seconds: int | None = None,
        artifact_ids: Sequence[str] = (),
        expected_memory_id: str = "",
        expected_revision: int | None = None,
        model_assisted: bool = False,
        proposer: str = "deterministic",
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryCandidate:
        evidence_ids = unique_strings([item.evidence_id for item in bundle.evidence_refs])
        artifacts = unique_strings(artifact_ids)
        semantic = {
            "run_id": run_id,
            "task_id": task_id,
            "kind": kind.value,
            "proposed_layer": proposed_layer,
            "scope": scope.value,
            "subject": subject.strip(),
            "summary": summary.strip(),
            "content": dict(content),
            "evidence_digest": bundle.bundle_digest,
            "evidence_ids": list(evidence_ids),
            "artifact_ids": list(artifacts),
            "confidence": round(float(confidence), 6),
            "ttl_seconds": ttl_seconds,
            "expected_memory_id": expected_memory_id,
            "expected_revision": expected_revision,
        }
        semantic_digest = stable_digest(semantic)
        candidate_id = stable_id("candidate", run_id, task_id, kind.value, semantic_digest)
        return cls(
            candidate_id=candidate_id,
            run_id=run_id,
            task_id=task_id,
            kind=kind,
            proposed_layer=proposed_layer,
            scope=scope,
            subject=subject.strip(),
            summary=summary.strip(),
            content=dict(content),
            evidence_bundle_id=bundle.bundle_id,
            evidence_digest=bundle.bundle_digest,
            evidence_range=bundle.evidence_range,
            evidence_ids=evidence_ids,
            artifact_ids=artifacts,
            confidence=float(confidence),
            ttl_seconds=ttl_seconds,
            extractor_version=bundle.extractor_version,
            idempotency_key=f"memory-candidate:{candidate_id}",
            expected_memory_id=expected_memory_id,
            expected_revision=expected_revision,
            model_assisted=model_assisted,
            proposer=proposer,
            metadata=dict(metadata or {}),
        ).validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MemoryCandidate:
        return cls(
            candidate_id=str(value.get("candidate_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            kind=CandidateKind(str(value.get("kind") or CandidateKind.PROMOTE.value)),
            proposed_layer=str(value.get("proposed_layer") or "semantic"),
            scope=MemoryScope(str(value.get("scope") or MemoryScope.TASK.value)),
            subject=str(value.get("subject") or ""),
            summary=str(value.get("summary") or ""),
            content=mapping(value.get("content")),
            evidence_bundle_id=str(value.get("evidence_bundle_id") or ""),
            evidence_digest=str(value.get("evidence_digest") or ""),
            evidence_range=EvidenceRange.from_dict(mapping(value.get("evidence_range"))),
            evidence_ids=unique_strings(value.get("evidence_ids") or ()),
            artifact_ids=unique_strings(value.get("artifact_ids") or ()),
            confidence=float(value.get("confidence", 0.0)),
            ttl_seconds=(int(value["ttl_seconds"]) if value.get("ttl_seconds") is not None else None),
            extractor_version=str(value.get("extractor_version") or EXTRACTOR_VERSION),
            idempotency_key=str(value.get("idempotency_key") or ""),
            state=CandidateState(str(value.get("state") or CandidateState.PROPOSED.value)),
            expected_memory_id=str(value.get("expected_memory_id") or ""),
            expected_revision=(
                int(value["expected_revision"])
                if value.get("expected_revision") is not None
                else None
            ),
            model_assisted=bool(value.get("model_assisted", False)),
            proposer=str(value.get("proposer") or "deterministic"),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class CandidateRelation:
    relation_id: str
    source_candidate_id: str
    target_candidate_id: str
    kind: CandidateRelationKind
    reason: str
    evidence_digest: str
    created_at: str = dataclass_field(default_factory=now_iso)
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def validated(self) -> CandidateRelation:
        if not self.relation_id or not self.source_candidate_id or not self.target_candidate_id:
            raise ValueError("candidate relation identity is required")
        if self.source_candidate_id == self.target_candidate_id:
            raise ValueError("candidate relation cannot self-reference")
        if not self.reason.strip():
            raise ValueError("candidate relation reason is required")
        if len(self.evidence_digest) != 64:
            raise ValueError("candidate relation evidence_digest must be sha256")
        return self

    @classmethod
    def build(
        cls,
        *,
        source_candidate_id: str,
        target_candidate_id: str,
        kind: CandidateRelationKind,
        reason: str,
        evidence_digest: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> CandidateRelation:
        relation_id = stable_id(
            "relation",
            source_candidate_id,
            target_candidate_id,
            kind.value,
            evidence_digest,
        )
        return cls(
            relation_id=relation_id,
            source_candidate_id=source_candidate_id,
            target_candidate_id=target_candidate_id,
            kind=kind,
            reason=reason,
            evidence_digest=evidence_digest,
            metadata=dict(metadata or {}),
        ).validated()

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "source_candidate_id": self.source_candidate_id,
            "target_candidate_id": self.target_candidate_id,
            "kind": self.kind.value,
            "reason": self.reason,
            "evidence_digest": self.evidence_digest,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DecisionIssue:
    code: DecisionCode
    message: str
    field: str = ""
    evidence_id: str = ""
    retryable: bool = False
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "field": self.field,
            "evidence_id": self.evidence_id,
            "retryable": self.retryable,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DecisionIssue:
        return cls(
            code=DecisionCode(str(value.get("code") or DecisionCode.INTERNAL_ERROR.value)),
            message=str(value.get("message") or ""),
            field=str(value.get("field") or ""),
            evidence_id=str(value.get("evidence_id") or ""),
            retryable=bool(value.get("retryable", False)),
            metadata=mapping(value.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class MemoryDecision:
    decision_id: str
    candidate_id: str
    status: DecisionStatus
    candidate_digest: str
    evidence_digest: str
    policy_digest: str
    validated_summary: str
    validated_content: Mapping[str, Any]
    issues: tuple[DecisionIssue, ...]
    target_memory_id: str = ""
    target_revision: int | None = None
    relation_id: str = ""
    created_at: str = dataclass_field(default_factory=now_iso)
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def validated(self) -> MemoryDecision:
        if not self.decision_id or not self.candidate_id:
            raise ValueError("decision identity is required")
        for digest in (self.candidate_digest, self.evidence_digest, self.policy_digest):
            if len(digest) != 64:
                raise ValueError("decision digest must be sha256")
        if self.status is DecisionStatus.ACCEPT and self.issues:
            blocking = [issue for issue in self.issues if issue.code is not DecisionCode.OK]
            if blocking:
                raise ValueError("accepted decision cannot contain blocking issues")
        if self.target_revision is not None and self.target_revision < 0:
            raise ValueError("decision target_revision must be non-negative")
        return self

    @property
    def accepted(self) -> bool:
        return self.status in {DecisionStatus.ACCEPT, DecisionStatus.SUPERSEDE}

    @property
    def retryable(self) -> bool:
        return any(issue.retryable for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": DECISION_PROTOCOL,
            "decision_id": self.decision_id,
            "candidate_id": self.candidate_id,
            "status": self.status.value,
            "candidate_digest": self.candidate_digest,
            "evidence_digest": self.evidence_digest,
            "policy_digest": self.policy_digest,
            "validated_summary": self.validated_summary,
            "validated_content": dict(self.validated_content),
            "issues": [issue.to_dict() for issue in self.issues],
            "target_memory_id": self.target_memory_id,
            "target_revision": self.target_revision,
            "relation_id": self.relation_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def build(
        cls,
        *,
        candidate: MemoryCandidate,
        status: DecisionStatus,
        policy_digest: str,
        validated_summary: str,
        validated_content: Mapping[str, Any],
        issues: Sequence[DecisionIssue] = (),
        target_memory_id: str = "",
        target_revision: int | None = None,
        relation_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> MemoryDecision:
        issue_values = tuple(issues)
        identity = stable_digest(
            {
                "candidate_id": candidate.candidate_id,
                "candidate_digest": candidate.semantic_digest,
                "evidence_digest": candidate.evidence_digest,
                "policy_digest": policy_digest,
                "status": status.value,
                "issues": [issue.to_dict() for issue in issue_values],
                "target_memory_id": target_memory_id,
                "target_revision": target_revision,
                "relation_id": relation_id,
            }
        )
        return cls(
            decision_id=stable_id("decision", candidate.candidate_id, identity),
            candidate_id=candidate.candidate_id,
            status=status,
            candidate_digest=candidate.semantic_digest,
            evidence_digest=candidate.evidence_digest,
            policy_digest=policy_digest,
            validated_summary=validated_summary,
            validated_content=dict(validated_content),
            issues=issue_values,
            target_memory_id=target_memory_id,
            target_revision=target_revision,
            relation_id=relation_id,
            created_at=created_at or candidate.updated_at,
            metadata=dict(metadata or {}),
        ).validated()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MemoryDecision:
        return cls(
            decision_id=str(value.get("decision_id") or ""),
            candidate_id=str(value.get("candidate_id") or ""),
            status=DecisionStatus(str(value.get("status") or DecisionStatus.REJECT.value)),
            candidate_digest=str(value.get("candidate_digest") or ""),
            evidence_digest=str(value.get("evidence_digest") or ""),
            policy_digest=str(value.get("policy_digest") or ""),
            validated_summary=str(value.get("validated_summary") or ""),
            validated_content=mapping(value.get("validated_content")),
            issues=tuple(
                DecisionIssue.from_dict(mapping(item))
                for item in value.get("issues") or ()
                if isinstance(item, Mapping)
            ),
            target_memory_id=str(value.get("target_memory_id") or ""),
            target_revision=(
                int(value["target_revision"])
                if value.get("target_revision") is not None
                else None
            ),
            relation_id=str(value.get("relation_id") or ""),
            created_at=str(value.get("created_at") or now_iso()),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class CuratorRunRequest:
    request_id: str
    run_id: str
    task_id: str
    trigger: CuratorTrigger
    requested_by: str
    input_watermark: int
    evidence_start: int = 0
    evidence_end: int | None = None
    allow_model_assist: bool = True
    max_candidates: int = 64
    lease_seconds: float = 30.0
    idempotency_key: str = ""
    created_at: str = dataclass_field(default_factory=now_iso)
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def validated(self) -> CuratorRunRequest:
        if not self.request_id or not self.run_id or not self.task_id:
            raise ValueError("curator request identity is required")
        if not self.requested_by.strip():
            raise ValueError("curator requested_by is required")
        if self.input_watermark < 0 or self.evidence_start < 0:
            raise ValueError("curator watermarks must be non-negative")
        if self.evidence_end is not None and self.evidence_end < self.evidence_start:
            raise ValueError("curator evidence_end precedes evidence_start")
        if not 1 <= self.max_candidates <= 1000:
            raise ValueError("curator max_candidates is out of range")
        if self.lease_seconds <= 0:
            raise ValueError("curator lease_seconds must be positive")
        if not self.idempotency_key:
            raise ValueError("curator idempotency_key is required")
        return self

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        trigger: CuratorTrigger,
        requested_by: str,
        input_watermark: int,
        evidence_start: int = 0,
        evidence_end: int | None = None,
        allow_model_assist: bool = True,
        max_candidates: int = 64,
        lease_seconds: float = 30.0,
        idempotency_key: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> CuratorRunRequest:
        key = idempotency_key or (
            f"curator:{task_id}:{trigger.value}:{input_watermark}:{evidence_start}:{evidence_end}"
        )
        request_id = stable_id("curator_request", run_id, task_id, key)
        return cls(
            request_id=request_id,
            run_id=run_id,
            task_id=task_id,
            trigger=trigger,
            requested_by=requested_by,
            input_watermark=input_watermark,
            evidence_start=evidence_start,
            evidence_end=evidence_end,
            allow_model_assist=allow_model_assist,
            max_candidates=max_candidates,
            lease_seconds=lease_seconds,
            idempotency_key=key,
            metadata=dict(metadata or {}),
        ).validated()

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_PROTOCOL,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "trigger": self.trigger.value,
            "requested_by": self.requested_by,
            "input_watermark": self.input_watermark,
            "evidence_start": self.evidence_start,
            "evidence_end": self.evidence_end,
            "allow_model_assist": self.allow_model_assist,
            "max_candidates": self.max_candidates,
            "lease_seconds": self.lease_seconds,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CuratorJobLease:
    job_id: str
    task_id: str
    worker_id: str
    ownership_token: str
    lease_epoch: int
    input_watermark: int
    expires_at: float
    attempt: int

    def validated(self) -> CuratorJobLease:
        if not self.job_id or not self.task_id or not self.worker_id or not self.ownership_token:
            raise ValueError("curator lease identity is required")
        if self.lease_epoch <= 0 or self.attempt <= 0:
            raise ValueError("curator lease epoch/attempt must be positive")
        if self.input_watermark < 0 or self.expires_at <= 0:
            raise ValueError("curator lease watermark/expiry is invalid")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "ownership_token": self.ownership_token,
            "lease_epoch": self.lease_epoch,
            "input_watermark": self.input_watermark,
            "expires_at": self.expires_at,
            "attempt": self.attempt,
        }


@dataclass(frozen=True, slots=True)
class CuratorJob:
    job_id: str
    request: CuratorRunRequest
    state: CuratorJobState
    input_watermark: int
    last_success_watermark: int
    lease_owner: str = ""
    ownership_token: str = ""
    lease_epoch: int = 0
    lease_expires_at: float | None = None
    attempt: int = 0
    retry_remaining: int = 3
    retry_at: float | None = None
    error_code: str = ""
    error_message: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    candidate_count: int = 0
    committed_count: int = 0

    @property
    def dirty(self) -> bool:
        return self.input_watermark > self.last_success_watermark

    @property
    def terminal(self) -> bool:
        return self.state in {
            CuratorJobState.SUCCEEDED,
            CuratorJobState.FAILED,
            CuratorJobState.STALE,
            CuratorJobState.CANCELLED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "request": self.request.to_dict(),
            "state": self.state.value,
            "input_watermark": self.input_watermark,
            "last_success_watermark": self.last_success_watermark,
            "lease_owner": self.lease_owner,
            "ownership_token": self.ownership_token,
            "lease_epoch": self.lease_epoch,
            "lease_expires_at": self.lease_expires_at,
            "attempt": self.attempt,
            "retry_remaining": self.retry_remaining,
            "retry_at": self.retry_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "candidate_count": self.candidate_count,
            "committed_count": self.committed_count,
            "dirty": self.dirty,
            "terminal": self.terminal,
        }


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    message_id: str
    kind: OutboxKind
    aggregate_id: str
    task_id: str
    run_id: str
    payload: Mapping[str, Any]
    state: OutboxState
    attempt: int
    available_at: float
    claimed_by: str = ""
    claim_token: str = ""
    claim_expires_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    delivered_at: float | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "kind": self.kind.value,
            "aggregate_id": self.aggregate_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "payload": dict(self.payload),
            "state": self.state.value,
            "attempt": self.attempt,
            "available_at": self.available_at,
            "claimed_by": self.claimed_by,
            "claim_token": self.claim_token,
            "claim_expires_at": self.claim_expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "delivered_at": self.delivered_at,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class MemoryCommitReceipt:
    receipt_id: str
    candidate_id: str
    decision_id: str
    disposition: CommitDisposition
    memory_id: str
    memory_revision: int
    outbox_message_ids: tuple[str, ...]
    committed_at: str
    idempotent_replay: bool = False
    reason: str = ""
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": COMMIT_PROTOCOL,
            "receipt_id": self.receipt_id,
            "candidate_id": self.candidate_id,
            "decision_id": self.decision_id,
            "disposition": self.disposition.value,
            "memory_id": self.memory_id,
            "memory_revision": self.memory_revision,
            "outbox_message_ids": list(self.outbox_message_ids),
            "committed_at": self.committed_at,
            "idempotent_replay": self.idempotent_replay,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MemoryCommitReceipt:
        return cls(
            receipt_id=str(value.get("receipt_id") or ""),
            candidate_id=str(value.get("candidate_id") or ""),
            decision_id=str(value.get("decision_id") or ""),
            disposition=CommitDisposition(
                str(value.get("disposition") or CommitDisposition.REJECTED.value)
            ),
            memory_id=str(value.get("memory_id") or ""),
            memory_revision=int(value.get("memory_revision", 0)),
            outbox_message_ids=unique_strings(value.get("outbox_message_ids") or ()),
            committed_at=str(value.get("committed_at") or now_iso()),
            idempotent_replay=bool(value.get("idempotent_replay", False)),
            reason=str(value.get("reason") or ""),
            metadata=mapping(value.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class CuratorRunResult:
    request_id: str
    job_id: str
    run_id: str
    task_id: str
    status: str
    input_watermark: int
    success_watermark: int
    evidence_bundle_id: str
    candidate_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    receipts: tuple[MemoryCommitReceipt, ...]
    rejected_candidate_ids: tuple[str, ...]
    model_status: str
    outbox_pending: int
    started_at: str
    finished_at: str
    diagnostics: Mapping[str, Any] = dataclass_field(default_factory=dict)

    @property
    def committed_count(self) -> int:
        return sum(
            1
            for receipt in self.receipts
            if receipt.disposition
            in {CommitDisposition.COMMITTED, CommitDisposition.ALREADY_COMMITTED}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": CURATOR_PROTOCOL,
            "request_id": self.request_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "input_watermark": self.input_watermark,
            "success_watermark": self.success_watermark,
            "evidence_bundle_id": self.evidence_bundle_id,
            "candidate_ids": list(self.candidate_ids),
            "decision_ids": list(self.decision_ids),
            "receipts": [receipt.to_dict() for receipt in self.receipts],
            "rejected_candidate_ids": list(self.rejected_candidate_ids),
            "model_status": self.model_status,
            "outbox_pending": self.outbox_pending,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "committed_count": self.committed_count,
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CuratorRunResult:
        return cls(
            request_id=str(value.get("request_id") or ""),
            job_id=str(value.get("job_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            status=str(value.get("status") or ""),
            input_watermark=int(value.get("input_watermark", 0)),
            success_watermark=int(value.get("success_watermark", 0)),
            evidence_bundle_id=str(value.get("evidence_bundle_id") or ""),
            candidate_ids=unique_strings(value.get("candidate_ids") or ()),
            decision_ids=unique_strings(value.get("decision_ids") or ()),
            receipts=tuple(
                MemoryCommitReceipt.from_dict(mapping(item))
                for item in value.get("receipts") or ()
                if isinstance(item, Mapping)
            ),
            rejected_candidate_ids=unique_strings(
                value.get("rejected_candidate_ids") or ()
            ),
            model_status=str(value.get("model_status") or ""),
            outbox_pending=int(value.get("outbox_pending", 0)),
            started_at=str(value.get("started_at") or now_iso()),
            finished_at=str(value.get("finished_at") or now_iso()),
            diagnostics=mapping(value.get("diagnostics")),
        )


__all__ = [
    "COMMIT_PROTOCOL",
    "CURATOR_PROTOCOL",
    "DECISION_PROTOCOL",
    "EVIDENCE_PROTOCOL",
    "EXTRACTOR_VERSION",
    "CandidateKind",
    "CandidateRelation",
    "CandidateRelationKind",
    "CandidateState",
    "CommitDisposition",
    "CuratorEvidenceRef",
    "CuratorJob",
    "CuratorJobLease",
    "CuratorJobState",
    "CuratorRunRequest",
    "CuratorRunResult",
    "CuratorTrigger",
    "DecisionCode",
    "DecisionIssue",
    "DecisionStatus",
    "EvidenceBundle",
    "EvidenceDocument",
    "EvidenceKind",
    "EvidenceRange",
    "MemoryCandidate",
    "MemoryCommitReceipt",
    "MemoryDecision",
    "MemoryScope",
    "OutboxKind",
    "OutboxMessage",
    "OutboxState",
    "TrustTier",
    "canonical_json",
    "mapping",
    "parse_timestamp",
    "stable_digest",
    "stable_id",
    "unique_strings",
]
