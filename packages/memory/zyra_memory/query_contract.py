from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .models import MemoryLayer
from .retrieval_models import (
    RetrievalBudget,
    RetrievalFilter,
    RetrievalResult,
    stable_digest,
    stable_identifier,
)


RETRIEVAL_QUERY_CONTRACT = "zyra.retrieval-query.v2"
RETRIEVAL_SNAPSHOT_CONTRACT = "zyra.retrieval-snapshot.v2"
RECOVERY_INDEX_REFERENCE_CONTRACT = "zyra.recovery-index-reference.v1"
INDEX_CHECKPOINT_CONTRACT = "zyra.index-checkpoint-ref.v1"


class RetrievalConsumer(StrEnum):
    CODE_WORKER_CONTEXT = "code_worker_context"
    COMPACT_RESTORE = "compact_restore"
    SKILL_MEMORY = "skill_memory"
    FAILURE_RECOVERY = "failure_recovery"
    PROCEDURE_RECALL = "procedure_recall"
    SCHEDULER_HINT = "scheduler_hint"
    API = "api"
    EVALUATION = "evaluation"


class RetrievalMode(StrEnum):
    LEXICAL_ONLY = "lexical_only"
    FTS_MMR = "fts_mmr"
    POLYPHONIC = "polyphonic"


@dataclass(frozen=True, slots=True)
class MemoryFilterQuery:
    """Versioned query contract shared by worker, compact and recovery paths.

    This is intentionally a consumer contract over ``RetrievalFilter`` rather
    than another query engine.  It gives downstream runtimes one place to bind
    task/run/session/artifact/source constraints and to preserve the exact
    budget used for a decision.
    """

    task_id: str
    text: str
    consumer: RetrievalConsumer
    run_id: str = ""
    session_id: str = ""
    artifact_ids: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    failure_kinds: tuple[str, ...] = ()
    layers: tuple[MemoryLayer, ...] = ()
    node_ids: tuple[str, ...] = ()
    maximum_results: int = 12
    candidate_limit: int = 96
    maximum_chars: int = 24_000
    maximum_document_chars: int = 8_000
    timeout_ms: int = 5_000
    vector_enabled: bool = True
    synchronize: bool = False
    request_id: str = ""
    causation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> "MemoryFilterQuery":
        task_id = self.task_id.strip()
        text = self.text.strip()
        if not task_id:
            raise ValueError("task_id is required")
        if not text:
            raise ValueError("retrieval text is required")
        if self.maximum_results < 0 or self.maximum_results > 1_000:
            raise ValueError("maximum_results must be between 0 and 1000")
        candidate_limit = max(self.maximum_results, self.candidate_limit)
        if candidate_limit > 10_000:
            raise ValueError("candidate_limit must be <= 10000")
        if self.maximum_chars < 0 or self.maximum_document_chars < 0:
            raise ValueError("character budgets must be non-negative")
        if self.timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")

        def normalized(values: Sequence[Any]) -> tuple[str, ...]:
            return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))

        return replace(
            self,
            task_id=task_id,
            text=text,
            run_id=self.run_id.strip(),
            session_id=self.session_id.strip(),
            artifact_ids=normalized(self.artifact_ids),
            source_types=normalized(self.source_types),
            source_ids=normalized(self.source_ids),
            skill_names=normalized(self.skill_names),
            failure_kinds=normalized(self.failure_kinds),
            layers=tuple(sorted(set(self.layers), key=str)),
            node_ids=normalized(self.node_ids),
            candidate_limit=candidate_limit,
            request_id=self.request_id.strip(),
            causation_id=self.causation_id.strip(),
            metadata=dict(self.metadata),
        )

    @property
    def query_id(self) -> str:
        value = self.validated()
        return stable_identifier(
            "retrieval",
            RETRIEVAL_QUERY_CONTRACT,
            value.task_id,
            value.text,
            value.consumer.value,
            value.filters.to_dict(),
            value.budget,
            value.request_id,
        )

    @property
    def filters(self) -> RetrievalFilter:
        value = self.validated()
        return RetrievalFilter(
            run_ids=(value.run_id,) if value.run_id else (),
            task_ids=(value.task_id,),
            session_ids=(value.session_id,) if value.session_id else (),
            artifact_ids=value.artifact_ids,
            source_types=value.source_types,
            source_ids=value.source_ids,
            skill_names=value.skill_names,
            failure_kinds=value.failure_kinds,
            layers=value.layers,
            node_ids=value.node_ids,
        ).normalized()

    @property
    def budget(self) -> RetrievalBudget:
        value = self.validated()
        return RetrievalBudget(
            limit=value.maximum_results,
            candidate_limit=value.candidate_limit,
            max_output_chars=value.maximum_chars,
            max_document_chars=value.maximum_document_chars,
            timeout_ms=value.timeout_ms,
        )

    @property
    def fingerprint(self) -> str:
        return stable_digest(self.to_dict(include_text=False))

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        value = self.validated()
        body: dict[str, Any] = {
            "schema": RETRIEVAL_QUERY_CONTRACT,
            "query_id": value.query_id,
            "task_id": value.task_id,
            "consumer": value.consumer.value,
            "filters": value.filters.to_dict(),
            "budget": {
                "limit": value.maximum_results,
                "candidate_limit": value.candidate_limit,
                "maximum_chars": value.maximum_chars,
                "maximum_document_chars": value.maximum_document_chars,
                "timeout_ms": value.timeout_ms,
            },
            "vector_enabled": value.vector_enabled,
            "synchronize": value.synchronize,
            "request_id": value.request_id,
            "causation_id": value.causation_id,
            "metadata": dict(value.metadata),
        }
        if include_text:
            body["text"] = value.text
        else:
            body["text_digest"] = stable_digest(value.text)
        return body


@dataclass(frozen=True, slots=True)
class RetrievalSourceRef:
    document_id: str
    source_kind: str
    source_id: str
    source_revision: str
    content_digest: str
    generation: int
    rank: int
    score: float
    artifact_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "content_digest": self.content_digest,
            "generation": self.generation,
            "rank": self.rank,
            "score": self.score,
            "artifact_ids": list(self.artifact_ids),
        }


@dataclass(frozen=True, slots=True)
class RetrievalSnapshotRef:
    query_id: str
    query_fingerprint: str
    task_id: str
    consumer: RetrievalConsumer
    index_scope: str
    index_generation: int
    index_revision: str
    mode: RetrievalMode
    source_refs: tuple[RetrievalSourceRef, ...]
    returned_count: int
    candidate_count: int
    vector_status: str
    result_digest: str
    truncated: bool = False
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_result(
        cls,
        query: MemoryFilterQuery,
        result: RetrievalResult,
        *,
        source_revision: str,
        mode: RetrievalMode,
    ) -> "RetrievalSnapshotRef":
        source_refs = tuple(
            RetrievalSourceRef(
                document_id=hit.document_id,
                source_kind=hit.source_kind.value,
                source_id=hit.source_id,
                source_revision=str(hit.metadata.get("source_revision") or ""),
                content_digest=hit.content_digest,
                generation=hit.generation,
                rank=hit.rank,
                score=hit.score,
                artifact_ids=tuple(hit.artifact_ids),
            )
            for hit in result.hits
        )
        diagnostics = result.diagnostics
        return cls(
            query_id=diagnostics.query_id,
            query_fingerprint=query.fingerprint,
            task_id=query.task_id,
            consumer=query.consumer,
            index_scope=diagnostics.index_scope,
            index_generation=diagnostics.index_generation,
            index_revision=source_revision,
            mode=mode,
            source_refs=source_refs,
            returned_count=diagnostics.returned_count,
            candidate_count=diagnostics.candidate_count,
            vector_status=diagnostics.vector_status.value,
            result_digest=stable_digest([item.to_dict() for item in source_refs]),
            truncated=diagnostics.truncated,
            warnings=tuple(diagnostics.warnings),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RETRIEVAL_SNAPSHOT_CONTRACT,
            "query_id": self.query_id,
            "query_fingerprint": self.query_fingerprint,
            "task_id": self.task_id,
            "consumer": self.consumer.value,
            "index_scope": self.index_scope,
            "index_generation": self.index_generation,
            "index_revision": self.index_revision,
            "mode": self.mode.value,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "returned_count": self.returned_count,
            "candidate_count": self.candidate_count,
            "vector_status": self.vector_status,
            "result_digest": self.result_digest,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class RecoveryIndexReference:
    task_id: str
    query_id: str
    index_scope: str
    index_generation: int
    index_revision: str
    source_refs: tuple[RetrievalSourceRef, ...]
    failure_kinds: tuple[str, ...] = ()
    procedure_memory_ids: tuple[str, ...] = ()
    code_index_refs: tuple[Mapping[str, Any], ...] = ()
    reason: str = "retrieval_evidence"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECOVERY_INDEX_REFERENCE_CONTRACT,
            "task_id": self.task_id,
            "query_id": self.query_id,
            "index_scope": self.index_scope,
            "index_generation": self.index_generation,
            "index_revision": self.index_revision,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "failure_kinds": list(self.failure_kinds),
            "procedure_memory_ids": list(self.procedure_memory_ids),
            "code_index_refs": [dict(item) for item in self.code_index_refs],
            "reason": self.reason,
            "canonical_memory_embedded": False,
            "derived_index_dump_embedded": False,
        }


@dataclass(frozen=True, slots=True)
class IndexCheckpointRef:
    task_id: str
    source_revision: str
    desired_generation: int
    published_generation: int
    pending_job_ids: tuple[str, ...] = ()
    last_query_ids: tuple[str, ...] = ()
    cursor_refs: tuple[Mapping[str, Any], ...] = ()

    def validated(self) -> "IndexCheckpointRef":
        if not self.task_id or not self.source_revision:
            raise ValueError("checkpoint requires task_id and source_revision")
        if self.desired_generation < 0 or self.published_generation < 0:
            raise ValueError("checkpoint generations must be non-negative")
        forbidden = {"documents", "embeddings", "index_rows", "fts_rows", "memory_records"}
        for cursor in self.cursor_refs:
            if forbidden.intersection(cursor):
                raise ValueError("checkpoint cursor embeds derived or canonical state")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validated()
        return {
            "schema": INDEX_CHECKPOINT_CONTRACT,
            "task_id": self.task_id,
            "source_revision": self.source_revision,
            "desired_generation": self.desired_generation,
            "published_generation": self.published_generation,
            "pending_job_ids": list(self.pending_job_ids),
            "last_query_ids": list(self.last_query_ids),
            "cursor_refs": [dict(item) for item in self.cursor_refs],
            "contains_index_dump": False,
            "contains_canonical_records": False,
        }


__all__ = [
    "INDEX_CHECKPOINT_CONTRACT",
    "RECOVERY_INDEX_REFERENCE_CONTRACT",
    "RETRIEVAL_QUERY_CONTRACT",
    "RETRIEVAL_SNAPSHOT_CONTRACT",
    "IndexCheckpointRef",
    "MemoryFilterQuery",
    "RecoveryIndexReference",
    "RetrievalConsumer",
    "RetrievalMode",
    "RetrievalSnapshotRef",
    "RetrievalSourceRef",
]
