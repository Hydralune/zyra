from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from .models import MemoryLayer, MemoryRecord


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_digest(*values: Any) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def stable_identifier(prefix: str, *values: Any) -> str:
    return f"{prefix}_{stable_digest(*values)[:32]}"


class IndexSourceKind(StrEnum):
    MEMORY = "memory"
    EVENT = "event"
    ARTIFACT = "artifact"
    TOOL_TRACE = "tool_trace"
    BROWSER_TRACE = "browser_trace"
    CODE_FILE = "code_file"
    CODE_SYMBOL = "code_symbol"
    SKILL = "skill"
    FAILURE = "failure"


class IndexOperation(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"
    REBUILD = "rebuild"
    INVALIDATE = "invalidate"


class IndexJobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    BUILDING = "building"
    PUBLISHING = "publishing"
    READY = "ready"
    FAILED = "failed"
    STALE = "stale"

    @property
    def terminal(self) -> bool:
        return self in {IndexJobState.READY, IndexJobState.FAILED, IndexJobState.STALE}


class VectorAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    DEGRADED = "degraded"


class RetrievalVoice(StrEnum):
    FTS = "fts"
    VECTOR = "vector"
    RECENCY = "recency"
    IMPORTANCE = "importance"
    EXACT = "exact"


class QueryIntentCategory(StrEnum):
    TEMPORAL = "temporal"
    FACTUAL = "factual"
    ENTITY = "entity"
    PREFERENCE = "preference"
    PROCEDURAL = "procedural"
    GENERAL = "general"


class IndexInvariantError(RuntimeError):
    pass


class LeaseUnavailableError(IndexInvariantError):
    pass


class LeaseLostError(IndexInvariantError):
    pass


class PublicationFencedError(IndexInvariantError):
    pass


class IndexUnavailableError(IndexInvariantError):
    pass


class QueryBudgetError(IndexInvariantError):
    pass


@dataclass(frozen=True, slots=True)
class TemporalConstraint:
    start_at: str = ""
    end_at: str = ""
    tags: tuple[str, ...] = ()
    precision: str = "unknown"
    source_text: str = ""

    def active(self) -> bool:
        return bool(self.start_at or self.end_at or self.tags)


@dataclass(frozen=True, slots=True)
class QueryIntent:
    category: QueryIntentCategory = QueryIntentCategory.GENERAL
    confidence: float = 0.0
    signals: tuple[QueryIntentCategory, ...] = ()
    vector_bias: float = 1.0
    fts_bias: float = 1.0
    importance_bias: float = 1.0

    def normalized_weights(
        self,
        *,
        vector: float = 0.35,
        fts: float = 0.45,
        importance: float = 0.20,
    ) -> tuple[float, float, float]:
        weighted = (
            max(0.0, vector * self.vector_bias),
            max(0.0, fts * self.fts_bias),
            max(0.0, importance * self.importance_bias),
        )
        total = sum(weighted)
        if total <= 0:
            return 0.0, 1.0, 0.0
        return tuple(value / total for value in weighted)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class RetrievalFilter:
    run_ids: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    failure_kinds: tuple[str, ...] = ()
    layers: tuple[MemoryLayer, ...] = ()
    node_ids: tuple[str, ...] = ()
    workspace_ids: tuple[str, ...] = ()
    temporal: TemporalConstraint = field(default_factory=TemporalConstraint)
    include_deleted: bool = False

    def normalized(self) -> "RetrievalFilter":
        def values(items: Iterable[Any]) -> tuple[str, ...]:
            return tuple(sorted({str(item).strip() for item in items if str(item).strip()}))

        return RetrievalFilter(
            run_ids=values(self.run_ids),
            task_ids=values(self.task_ids),
            session_ids=values(self.session_ids),
            artifact_ids=values(self.artifact_ids),
            source_types=values(self.source_types),
            source_ids=values(self.source_ids),
            skill_names=values(self.skill_names),
            failure_kinds=values(self.failure_kinds),
            layers=tuple(sorted(set(self.layers), key=str)),
            node_ids=values(self.node_ids),
            workspace_ids=values(self.workspace_ids),
            temporal=self.temporal,
            include_deleted=bool(self.include_deleted),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_ids": list(self.run_ids),
            "task_ids": list(self.task_ids),
            "session_ids": list(self.session_ids),
            "artifact_ids": list(self.artifact_ids),
            "source_types": list(self.source_types),
            "source_ids": list(self.source_ids),
            "skill_names": list(self.skill_names),
            "failure_kinds": list(self.failure_kinds),
            "layers": [str(item) for item in self.layers],
            "node_ids": list(self.node_ids),
            "workspace_ids": list(self.workspace_ids),
            "temporal": {
                "start_at": self.temporal.start_at,
                "end_at": self.temporal.end_at,
                "tags": list(self.temporal.tags),
                "precision": self.temporal.precision,
                "source_text": self.temporal.source_text,
            },
            "include_deleted": self.include_deleted,
        }


@dataclass(frozen=True, slots=True)
class RetrievalBudget:
    limit: int = 10
    candidate_limit: int = 80
    max_query_chars: int = 4096
    max_output_chars: int = 24_000
    max_document_chars: int = 8_000
    max_explanation_chars: int = 1_000
    mmr_lambda: float = 0.72
    timeout_ms: int = 5_000

    def validated(self) -> "RetrievalBudget":
        if self.limit < 0 or self.limit > 1_000:
            raise QueryBudgetError("limit must be between 0 and 1000")
        if self.candidate_limit < self.limit or self.candidate_limit > 10_000:
            raise QueryBudgetError("candidate_limit must cover limit and remain <= 10000")
        if self.max_query_chars < 1 or self.max_query_chars > 100_000:
            raise QueryBudgetError("max_query_chars must be between 1 and 100000")
        if self.max_output_chars < 0 or self.max_document_chars < 0:
            raise QueryBudgetError("output budgets must be non-negative")
        if not 0.0 <= self.mmr_lambda <= 1.0:
            raise QueryBudgetError("mmr_lambda must be between 0 and 1")
        if self.timeout_ms <= 0:
            raise QueryBudgetError("timeout_ms must be positive")
        return self


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    text: str
    filters: RetrievalFilter = field(default_factory=RetrievalFilter)
    budget: RetrievalBudget = field(default_factory=RetrievalBudget)
    intent: QueryIntent | None = None
    query_id: str = ""
    request_time: str = field(default_factory=isoformat)
    vector_enabled: bool = True
    explain: bool = True

    def validated(self) -> "RetrievalQuery":
        budget = self.budget.validated()
        text = self.text.strip()
        if not text:
            raise QueryBudgetError("retrieval query cannot be empty")
        if len(text) > budget.max_query_chars:
            raise QueryBudgetError("retrieval query exceeds max_query_chars")
        query_id = self.query_id or stable_identifier(
            "query",
            text,
            self.filters.normalized().to_dict(),
            self.request_time,
        )
        return replace(
            self,
            text=text,
            filters=self.filters.normalized(),
            query_id=query_id,
        )


@dataclass(frozen=True, slots=True)
class IndexDocument:
    document_id: str
    scope_key: str
    generation: int
    source_kind: IndexSourceKind
    source_id: str
    title: str
    body: str
    run_id: str = ""
    task_id: str = ""
    session_id: str = ""
    layer: str = ""
    node_id: str = ""
    workspace_id: str = ""
    skill_name: str = ""
    failure_kind: str = ""
    artifact_ids: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    event_at: str = ""
    importance: float = 0.0
    source_revision: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    deleted: bool = False

    def __post_init__(self) -> None:
        if not self.document_id:
            raise ValueError("document_id is required")
        if not self.scope_key:
            raise ValueError("scope_key is required")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if not math.isfinite(float(self.importance)):
            raise ValueError("importance must be finite")

    @property
    def artifact_key(self) -> str:
        return "\n".join(sorted(set(self.artifact_ids)))

    @property
    def keyword_text(self) -> str:
        return " ".join(sorted(set(self.keywords)))

    @property
    def content_digest(self) -> str:
        return stable_digest(
            self.source_kind,
            self.source_id,
            self.title,
            self.body,
            self.run_id,
            self.task_id,
            self.session_id,
            self.layer,
            self.node_id,
            self.workspace_id,
            self.skill_name,
            self.failure_kind,
            self.artifact_ids,
            self.keywords,
            self.event_at,
            self.importance,
            self.source_revision,
            self.metadata,
            self.deleted,
        )

    def with_generation(self, generation: int) -> "IndexDocument":
        return replace(self, generation=generation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "scope_key": self.scope_key,
            "generation": self.generation,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "title": self.title,
            "body": self.body,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "layer": self.layer,
            "node_id": self.node_id,
            "workspace_id": self.workspace_id,
            "skill_name": self.skill_name,
            "failure_kind": self.failure_kind,
            "artifact_ids": list(self.artifact_ids),
            "keywords": list(self.keywords),
            "event_at": self.event_at,
            "importance": self.importance,
            "source_revision": self.source_revision,
            "metadata": dict(self.metadata),
            "deleted": self.deleted,
            "content_digest": self.content_digest,
        }

    @classmethod
    def from_memory_record(
        cls,
        record: MemoryRecord,
        *,
        scope_key: str,
        generation: int,
        artifact_text: str = "",
    ) -> "IndexDocument":
        source_kind = IndexSourceKind.MEMORY
        failure_kind = ""
        if record.layer is MemoryLayer.SKILL:
            source_kind = IndexSourceKind.SKILL
        elif "failure" in record.source_type.casefold():
            source_kind = IndexSourceKind.FAILURE
            failure_kind = str(record.metadata.get("failure_kind") or record.source_type)
        body_parts = [record.summary]
        if record.content:
            body_parts.append(canonical_json(record.content))
        if artifact_text:
            body_parts.append(artifact_text)
        session_id = str(record.metadata.get("session_id") or "")
        skill_name = ""
        if record.layer is MemoryLayer.SKILL:
            skill_name = str(record.metadata.get("skill_name") or record.source_id)
        return cls(
            document_id=record.memory_id,
            scope_key=scope_key,
            generation=generation,
            source_kind=source_kind,
            source_id=record.source_id,
            title=record.summary[:300],
            body="\n".join(part for part in body_parts if part),
            run_id=record.run_id,
            task_id=record.task_id,
            session_id=session_id,
            layer=str(record.layer),
            node_id=record.node_id or "",
            skill_name=skill_name,
            failure_kind=failure_kind,
            artifact_ids=tuple(record.artifact_ids),
            keywords=tuple(record.keywords),
            event_at=record.updated_at or record.created_at,
            importance=max(0.0, float(record.score)),
            source_revision=stable_digest(
                record.memory_id,
                record.run_id,
                record.task_id,
                str(record.layer),
                record.source_type,
                record.source_id,
                record.node_id or "",
                record.summary,
                record.content,
                sorted(record.keywords),
                sorted(record.artifact_ids),
                sorted(record.evidence_ids),
                record.score,
                record.metadata,
            ),
            metadata=dict(record.metadata),
        )


@dataclass(frozen=True, slots=True)
class VoiceScore:
    voice: RetrievalVoice
    score: float
    rank: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice": self.voice.value,
            "score": self.score,
            "rank": self.rank,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    document_id: str
    source_kind: IndexSourceKind
    source_id: str
    title: str
    content: str
    score: float
    rank: int = 0
    run_id: str = ""
    task_id: str = ""
    session_id: str = ""
    layer: str = ""
    node_id: str = ""
    workspace_id: str = ""
    skill_name: str = ""
    failure_kind: str = ""
    artifact_ids: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    event_at: str = ""
    importance: float = 0.0
    generation: int = 0
    content_digest: str = ""
    voices: tuple[VoiceScore, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def with_rank(self, rank: int) -> "RetrievalHit":
        return replace(self, rank=rank)

    def with_score(self, score: float, voices: Sequence[VoiceScore] | None = None) -> "RetrievalHit":
        return replace(self, score=float(score), voices=tuple(voices or self.voices))

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "title": self.title,
            "content": self.content,
            "score": self.score,
            "rank": self.rank,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "layer": self.layer,
            "node_id": self.node_id,
            "workspace_id": self.workspace_id,
            "skill_name": self.skill_name,
            "failure_kind": self.failure_kind,
            "artifact_ids": list(self.artifact_ids),
            "keywords": list(self.keywords),
            "event_at": self.event_at,
            "importance": self.importance,
            "generation": self.generation,
            "content_digest": self.content_digest,
            "voices": [item.to_dict() for item in self.voices],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RetrievalDiagnostics:
    query_id: str
    fts_used: bool = False
    vector_status: VectorAvailability = VectorAvailability.DISABLED
    vector_reason: str = ""
    candidate_count: int = 0
    returned_count: int = 0
    filtered_count: int = 0
    truncated: bool = False
    index_generation: int = 0
    index_scope: str = ""
    elapsed_ms: float = 0.0
    intent: QueryIntent = field(default_factory=QueryIntent)
    temporal: TemporalConstraint = field(default_factory=TemporalConstraint)
    warnings: tuple[str, ...] = ()
    source_roles: Mapping[str, str] = field(
        default_factory=lambda: {
            "agentscope": "primary_rag_and_index_job_lifecycle",
            "oh-my-pi": "supplementary_retrieval_and_rebuild_recovery",
            "langgraph": "conformance_only_checkpoint_reference",
            "hermes-agent": "reference_only_memory_search",
            "opencode": "reference_only_code_search_contract",
            "claude-code-best": "reference_only_code_navigation_patterns",
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "fts_used": self.fts_used,
            "vector_status": self.vector_status.value,
            "vector_reason": self.vector_reason,
            "candidate_count": self.candidate_count,
            "returned_count": self.returned_count,
            "filtered_count": self.filtered_count,
            "truncated": self.truncated,
            "index_generation": self.index_generation,
            "index_scope": self.index_scope,
            "elapsed_ms": self.elapsed_ms,
            "intent": {
                "category": self.intent.category.value,
                "confidence": self.intent.confidence,
                "signals": [item.value for item in self.intent.signals],
                "vector_bias": self.intent.vector_bias,
                "fts_bias": self.intent.fts_bias,
                "importance_bias": self.intent.importance_bias,
            },
            "temporal": {
                "start_at": self.temporal.start_at,
                "end_at": self.temporal.end_at,
                "tags": list(self.temporal.tags),
                "precision": self.temporal.precision,
                "source_text": self.temporal.source_text,
            },
            "warnings": list(self.warnings),
            "source_roles": dict(self.source_roles),
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    query: RetrievalQuery
    hits: tuple[RetrievalHit, ...]
    diagnostics: RetrievalDiagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": {
                "query_id": self.query.query_id,
                "text": self.query.text,
                "filters": self.query.filters.to_dict(),
                "limit": self.query.budget.limit,
                "candidate_limit": self.query.budget.candidate_limit,
            },
            "hits": [hit.to_dict() for hit in self.hits],
            "diagnostics": self.diagnostics.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class IndexJob:
    job_id: str
    scope_key: str
    operation: IndexOperation
    state: IndexJobState
    generation: int
    source_revision: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    lease_owner: str = ""
    lease_token: str = ""
    lease_epoch: int = 0
    lease_expires_at: str = ""
    attempt: int = 0
    created_at: str = field(default_factory=isoformat)
    updated_at: str = field(default_factory=isoformat)
    started_at: str = ""
    published_at: str = ""
    error_code: str = ""
    error_message: str = ""
    causation_id: str = ""
    idempotency_key: str = ""

    @property
    def fence(self) -> tuple[str, int, str]:
        return self.job_id, self.lease_epoch, self.lease_token

    def to_dict(self, *, include_lease_token: bool = False) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "scope_key": self.scope_key,
            "operation": self.operation.value,
            "state": self.state.value,
            "generation": self.generation,
            "source_revision": self.source_revision,
            "payload": dict(self.payload),
            "lease_owner": self.lease_owner,
            "lease_token": self.lease_token if include_lease_token else ("redacted" if self.lease_token else ""),
            "lease_epoch": self.lease_epoch,
            "lease_expires_at": self.lease_expires_at,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "published_at": self.published_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class IndexLease:
    job_id: str
    scope_key: str
    worker_id: str
    token: str
    epoch: int
    generation: int
    expires_at: str

    @property
    def fence(self) -> tuple[str, int, str]:
        return self.job_id, self.epoch, self.token


@dataclass(frozen=True, slots=True)
class IndexPublication:
    job_id: str
    scope_key: str
    generation: int
    document_count: int
    content_digest: str
    published_at: str = field(default_factory=isoformat)
    previous_generation: int = 0
    source_revision: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "scope_key": self.scope_key,
            "generation": self.generation,
            "document_count": self.document_count,
            "content_digest": self.content_digest,
            "published_at": self.published_at,
            "previous_generation": self.previous_generation,
            "source_revision": self.source_revision,
        }


@dataclass(frozen=True, slots=True)
class IndexCursor:
    source_kind: IndexSourceKind
    source_id: str
    source_revision: str
    content_digest: str
    scope_key: str
    generation: int
    updated_at: str = field(default_factory=isoformat)


@dataclass(frozen=True, slots=True)
class IndexHealth:
    database_path: str
    fts_available: bool
    queued: int
    leased: int
    building: int
    publishing: int
    ready: int
    failed: int
    stale: int
    live_documents: int
    staged_documents: int
    oldest_lease_expiry: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "database_path": self.database_path,
            "fts_available": self.fts_available,
            "jobs": {
                "queued": self.queued,
                "leased": self.leased,
                "building": self.building,
                "publishing": self.publishing,
                "ready": self.ready,
                "failed": self.failed,
                "stale": self.stale,
            },
            "live_documents": self.live_documents,
            "staged_documents": self.staged_documents,
            "oldest_lease_expiry": self.oldest_lease_expiry,
            "warnings": list(self.warnings),
        }
