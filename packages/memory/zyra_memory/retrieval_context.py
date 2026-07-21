from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .memory_index import HydratedMemoryResult, MemoryIndexRuntime
from .models import MemoryRecord
from .retrieval_models import RetrievalBudget, RetrievalFilter, stable_digest


@dataclass(frozen=True, slots=True)
class MemoryContextEntry:
    memory_id: str
    layer: str
    title: str
    content: str
    source_type: str
    source_id: str
    artifact_ids: tuple[str, ...]
    score: float
    rank: int
    source_revision: str
    index_generation: int
    voices: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "layer": self.layer,
            "title": self.title,
            "content": self.content,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "artifact_ids": list(self.artifact_ids),
            "score": self.score,
            "rank": self.rank,
            "source_revision": self.source_revision,
            "index_generation": self.index_generation,
            "voices": [dict(item) for item in self.voices],
        }


@dataclass(frozen=True, slots=True)
class MemoryContextBlock:
    task_id: str
    query: str
    entries: tuple[MemoryContextEntry, ...]
    query_id: str
    query_digest: str
    index_scope: str
    index_generation: int
    vector_status: str
    total_chars: int
    truncated: bool
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "query": self.query,
            "entries": [item.to_dict() for item in self.entries],
            "query_id": self.query_id,
            "query_digest": self.query_digest,
            "index_scope": self.index_scope,
            "index_generation": self.index_generation,
            "vector_status": self.vector_status,
            "total_chars": self.total_chars,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "retrieval_scope": "task_memory",
            "code_index_source": False,
            "canonical_owner": "MemoryRecordStore",
        }

    def as_worker_constraints(self) -> Mapping[str, Any]:
        return {
            "memory_hints": [item.to_dict() for item in self.entries],
            "memory_retrieval": {
                "query_id": self.query_id,
                "query_digest": self.query_digest,
                "retrieval_scope": "task_memory",
                "index_scope": self.index_scope,
                "index_revision": self.index_generation,
                "vector_status": self.vector_status,
                "source_refs": [
                    {
                        "memory_id": item.memory_id,
                        "source_type": item.source_type,
                        "source_id": item.source_id,
                        "source_revision": item.source_revision,
                    }
                    for item in self.entries
                ],
            },
        }


class MemoryContextBridge:
    """Budgeted retrieval-to-worker context adapter.

    The bridge consumes hydrated canonical records.  It never serializes the
    derived index database or treats a stale index body as a memory fact.
    """

    def __init__(self, runtime: MemoryIndexRuntime) -> None:
        self.runtime = runtime

    def build(
        self,
        task_id: str,
        query: str,
        *,
        filters: RetrievalFilter | None = None,
        maximum_entries: int = 12,
        maximum_chars: int = 24_000,
        synchronize: bool = False,
    ) -> MemoryContextBlock:
        if maximum_entries < 0 or maximum_chars < 0:
            raise ValueError("context budgets must be non-negative")
        hydrated = self.runtime.retrieve(
            task_id,
            query,
            filters=filters,
            budget=RetrievalBudget(
                limit=maximum_entries,
                candidate_limit=max(maximum_entries * 8, maximum_entries),
                max_output_chars=maximum_chars,
                max_document_chars=min(8_000, maximum_chars),
            ),
            synchronize=synchronize,
        )
        entries, chars, truncated = self._entries(
            hydrated,
            maximum_entries=maximum_entries,
            maximum_chars=maximum_chars,
        )
        diagnostics = hydrated.retrieval.diagnostics
        warnings = list(diagnostics.warnings)
        if hydrated.stale_document_ids:
            warnings.append("stale_hits_rejected")
        if hydrated.missing_document_ids:
            warnings.append("missing_hits_rejected")
        return MemoryContextBlock(
            task_id=task_id,
            query=query,
            entries=entries,
            query_id=diagnostics.query_id,
            query_digest=stable_digest(query, filters.to_dict() if filters else {}),
            index_scope=diagnostics.index_scope,
            index_generation=diagnostics.index_generation,
            vector_status=diagnostics.vector_status.value,
            total_chars=chars,
            truncated=truncated or diagnostics.truncated,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _entries(
        hydrated: HydratedMemoryResult,
        *,
        maximum_entries: int,
        maximum_chars: int,
    ) -> tuple[tuple[MemoryContextEntry, ...], int, bool]:
        records = {record.memory_id: record for record in hydrated.records}
        entries: list[MemoryContextEntry] = []
        chars = 0
        truncated = False
        for hit in hydrated.retrieval.hits:
            record = records.get(hit.document_id)
            if record is None:
                continue
            content = MemoryContextBridge._canonical_text(record)
            available = max(0, maximum_chars - chars)
            if not available or len(entries) >= maximum_entries:
                truncated = True
                break
            if len(content) > available:
                content = content[:available]
                truncated = True
            entry = MemoryContextEntry(
                memory_id=record.memory_id,
                layer=str(record.layer),
                title=record.summary,
                content=content,
                source_type=record.source_type,
                source_id=record.source_id,
                artifact_ids=tuple(record.artifact_ids),
                score=hit.score,
                rank=hit.rank,
                source_revision=record.updated_at,
                index_generation=hit.generation,
                voices=tuple(item.to_dict() for item in hit.voices),
            )
            entries.append(entry)
            chars += len(content) + len(record.summary)
        return tuple(entries), chars, truncated

    @staticmethod
    def _canonical_text(record: MemoryRecord) -> str:
        import json

        body = json.dumps(record.content, ensure_ascii=False, sort_keys=True, default=str)
        return f"{record.summary}\n{body}"
