from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .index_jobs import IndexBuildQueue, IndexLeaseStore
from .index_worker import IndexWorkerRuntime, StaleLeaseSweeper, WorkerOutcome
from .models import MemoryLayer, MemoryRecord
from .retrieval_models import (
    IndexCursor,
    IndexDocument,
    IndexJob,
    IndexOperation,
    IndexSourceKind,
    RetrievalBudget,
    RetrievalFilter,
    RetrievalQuery,
    RetrievalResult,
    VectorAvailability,
    canonical_json,
    stable_digest,
)
from .retrieval_query import combine_retrieval_hits, enrich_query, filter_hit_in_memory
from .retrieval_store import SQLiteRetrievalIndex
from .vector_adapter import UnavailableVectorAdapter, VectorSearchAdapter


@dataclass(frozen=True, slots=True)
class MemoryIndexSyncResult:
    task_id: str
    scope_key: str
    source_revision: str
    changed: bool
    job_id: str = ""
    generation: int = 0
    outcome: WorkerOutcome | None = None
    canonical_record_count: int = 0
    index_document_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "scope_key": self.scope_key,
            "source_revision": self.source_revision,
            "changed": self.changed,
            "job_id": self.job_id,
            "generation": self.generation,
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "canonical_record_count": self.canonical_record_count,
            "index_document_count": self.index_document_count,
        }


@dataclass(frozen=True, slots=True)
class HydratedMemoryResult:
    retrieval: RetrievalResult
    records: tuple[MemoryRecord, ...]
    stale_document_ids: tuple[str, ...] = ()
    missing_document_ids: tuple[str, ...] = ()
    repair_job_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval": self.retrieval.to_dict(),
            "memory_ids": [record.memory_id for record in self.records],
            "stale_document_ids": list(self.stale_document_ids),
            "missing_document_ids": list(self.missing_document_ids),
            "repair_job_id": self.repair_job_id,
        }


class MemoryIndexRuntime:
    """Zyra-owned retrieval facade over canonical MemoryRecordStore.

    AgentScope supplies the retained RAG/job lifecycle shape; the bounded
    oh-my-pi mechanisms are query-intent weights, deterministic RRF/MMR, and
    interrupted derived-index rebuild recovery.  The implementation does not
    adopt either upstream database, environment discovery, embedding owner, or
    service process.
    """

    def __init__(
        self,
        *,
        canonical_store: Any,
        index_path: str | Path,
        artifact_store: Any | None = None,
        vector_adapter: VectorSearchAdapter | None = None,
        worker_id: str = "memory-index-inline",
        lease_ttl_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
        clock: Any | None = None,
        event_sink: Any | None = None,
    ) -> None:
        self.canonical_store = canonical_store
        self.artifact_store = artifact_store
        self.index = SQLiteRetrievalIndex(index_path, clock=clock)
        self.queue = IndexBuildQueue(self.index)
        self.leases = IndexLeaseStore(self.queue)
        self.vector_adapter = vector_adapter or UnavailableVectorAdapter()
        self.worker = IndexWorkerRuntime(
            worker_id=worker_id,
            queue=self.queue,
            leases=self.leases,
            index=self.index,
            loader=self._load_job_documents,
            lease_ttl_seconds=lease_ttl_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            event_sink=event_sink,
        )
        self.sweeper = StaleLeaseSweeper(self.leases, event_sink=event_sink)
        self._guard = threading.RLock()

    @staticmethod
    def task_scope(task_id: str) -> str:
        value = str(task_id).strip()
        if not value:
            raise ValueError("task_id is required")
        return f"memory:{value}"

    @staticmethod
    def task_id_from_scope(scope_key: str) -> str:
        prefix = "memory:"
        if not scope_key.startswith(prefix) or len(scope_key) == len(prefix):
            raise ValueError(f"not a memory task scope: {scope_key!r}")
        return scope_key[len(prefix) :]

    def source_revision(self, records: Sequence[MemoryRecord]) -> str:
        projections = [self._record_projection(record) for record in records]
        projections.sort(key=lambda item: str(item["memory_id"]))
        task_ids = sorted({record.task_id for record in records})
        return f"memory:{','.join(task_ids)}:{stable_digest(projections)}"

    def synchronize_task(
        self,
        task_id: str,
        *,
        records: Sequence[MemoryRecord] | None = None,
        process: bool = True,
        causation_id: str = "",
        force: bool = False,
    ) -> MemoryIndexSyncResult:
        scope_key = self.task_scope(task_id)
        canonical = tuple(records if records is not None else self.canonical_store.task_memory_records(task_id))
        if any(record.task_id != task_id for record in canonical):
            raise ValueError("synchronize_task received records from another task")
        source_revision = self.source_revision(canonical)
        scope = self.index.scope_state(scope_key)
        unchanged = (
            not force
            and str(scope.get("published_revision") or "") == source_revision
            and int(scope.get("published_generation") or 0) > 0
        )
        if unchanged:
            return MemoryIndexSyncResult(
                task_id=task_id,
                scope_key=scope_key,
                source_revision=source_revision,
                changed=False,
                generation=int(scope["published_generation"]),
                canonical_record_count=len(canonical),
                index_document_count=int(scope.get("document_count") or 0),
            )
        with self._guard:
            scope = self.index.scope_state(scope_key)
            if (
                not force
                and str(scope.get("published_revision") or "") == source_revision
                and int(scope.get("published_generation") or 0) > 0
            ):
                return MemoryIndexSyncResult(
                    task_id=task_id,
                    scope_key=scope_key,
                    source_revision=source_revision,
                    changed=False,
                    generation=int(scope["published_generation"]),
                    canonical_record_count=len(canonical),
                    index_document_count=int(scope.get("document_count") or 0),
                )
            idempotency_key = f"memory-sync:{scope_key}:{source_revision}" if not force else ""
            job = self.queue.enqueue(
                scope_key=scope_key,
                source_revision=source_revision,
                operation=IndexOperation.REBUILD,
                payload={
                    "domain": "memory",
                    "task_id": task_id,
                    "canonical_owner": type(self.canonical_store).__name__,
                    "record_count_hint": len(canonical),
                    "source_revision": source_revision,
                },
                idempotency_key=idempotency_key,
                causation_id=causation_id,
            )
            self.queue.cancel_superseded_queued(scope_key)
            outcome = self.worker.process_one(scope_key=scope_key) if process else None
            if outcome is not None and outcome.status == "ready":
                live = self.index.list_scope_documents(scope_key)
                self.vector_adapter.replace_scope(scope_key, outcome.generation, live)
                for record in canonical:
                    document = self._record_to_document(record, scope_key, outcome.generation)
                    self.index.put_cursor(
                        IndexCursor(
                            source_kind=document.source_kind,
                            source_id=document.source_id,
                            source_revision=document.source_revision,
                            content_digest=document.content_digest,
                            scope_key=scope_key,
                            generation=outcome.generation,
                        )
                    )
            generation = outcome.generation if outcome else job.generation
            count = outcome.document_count if outcome else 0
            return MemoryIndexSyncResult(
                task_id=task_id,
                scope_key=scope_key,
                source_revision=source_revision,
                changed=True,
                job_id=job.job_id,
                generation=generation,
                outcome=outcome,
                canonical_record_count=len(canonical),
                index_document_count=count,
            )

    def request_rebuild(
        self,
        task_id: str,
        *,
        causation_id: str = "",
        process: bool = False,
    ) -> MemoryIndexSyncResult:
        return self.synchronize_task(
            task_id,
            process=process,
            causation_id=causation_id,
            force=True,
        )

    def retrieve(
        self,
        task_id: str,
        text: str,
        *,
        filters: RetrievalFilter | None = None,
        budget: RetrievalBudget | None = None,
        vector_enabled: bool = True,
        synchronize: bool = False,
    ) -> HydratedMemoryResult:
        if synchronize:
            self.synchronize_task(task_id)
        scope_key = self.task_scope(task_id)
        scope = self.index.scope_state(scope_key)
        generation = int(scope.get("published_generation") or 0)
        query_filter = filters or RetrievalFilter(task_ids=(task_id,))
        if not query_filter.task_ids:
            query_filter = replace(query_filter, task_ids=(task_id,))
        query = enrich_query(
            RetrievalQuery(
                text=text,
                filters=query_filter,
                budget=budget or RetrievalBudget(),
                vector_enabled=vector_enabled,
            )
        )
        started = time.perf_counter()
        fts_hits = self.index.search_fts(query, scope_key=scope_key) if generation else ()
        vector_hits = ()
        vector_status = self.vector_adapter.availability
        vector_reason = self.vector_adapter.reason
        if vector_enabled and generation:
            vector_hits = tuple(
                hit
                for hit in self.vector_adapter.search(
                    scope_key,
                    generation,
                    query.text,
                    limit=query.budget.candidate_limit,
                )
                if filter_hit_in_memory(hit, query.filters)
            )
            vector_status = self.vector_adapter.availability
            vector_reason = self.vector_adapter.reason
        elif not vector_enabled:
            vector_status = VectorAvailability.DISABLED
            vector_reason = "vector voice disabled by retrieval request"
        warnings: list[str] = []
        if generation == 0:
            warnings.append("index_not_ready")
        retrieval = combine_retrieval_hits(
            query,
            fts_hits=fts_hits,
            vector_hits=vector_hits,
            vector_status=vector_status,
            vector_reason=vector_reason,
            index_scope=scope_key,
            index_generation=generation,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            warnings=warnings,
        )
        self.index.record_query(retrieval)
        return self._hydrate(task_id, retrieval)

    def retrieve_layers(
        self,
        task_id: str,
        text: str,
        *,
        layers: Sequence[MemoryLayer],
        limit: int = 10,
    ) -> HydratedMemoryResult:
        return self.retrieve(
            task_id,
            text,
            filters=RetrievalFilter(task_ids=(task_id,), layers=tuple(layers)),
            budget=RetrievalBudget(limit=limit, candidate_limit=max(limit * 8, limit)),
        )

    def rebuild_from_canonical(self, task_id: str) -> MemoryIndexSyncResult:
        return self.request_rebuild(task_id, causation_id="clean-rebuild", process=True)

    def disable_effect(self, task_id: str, query: str) -> Mapping[str, Any]:
        enabled = self.retrieve(task_id, query)
        return {
            "enabled_hit_ids": [hit.document_id for hit in enabled.retrieval.hits],
            "disabled_contract": {
                "status": "disabled",
                "fallback": False,
                "legacy_substring_scan": False,
                "result_count": 0,
            },
            "behavior_changed": bool(enabled.retrieval.hits),
        }

    def status(self, task_id: str = "") -> Mapping[str, Any]:
        value: dict[str, Any] = {
            "health": self.index.health().to_dict(),
            "vector": (
                self.vector_adapter.status().to_dict()
                if hasattr(self.vector_adapter, "status")
                else {
                    "availability": self.vector_adapter.availability.value,
                    "reason": self.vector_adapter.reason,
                }
            ),
            "canonical_owner": type(self.canonical_store).__name__,
            "derived_state": True,
            "rebuildable": True,
        }
        if task_id:
            value["scope"] = self.index.scope_state(self.task_scope(task_id))
        return value

    def _load_job_documents(self, job: IndexJob) -> Sequence[IndexDocument]:
        task_id = str(job.payload.get("task_id") or self.task_id_from_scope(job.scope_key))
        records = self.canonical_store.task_memory_records(task_id)
        actual_revision = self.source_revision(records)
        if actual_revision != job.source_revision:
            raise RuntimeError(
                "canonical memory changed after enqueue; this generation must fail and be superseded"
            )
        return tuple(
            self._record_to_document(record, job.scope_key, job.generation)
            for record in records
        )

    def _record_to_document(
        self,
        record: MemoryRecord,
        scope_key: str,
        generation: int,
    ) -> IndexDocument:
        artifact_texts: list[str] = []
        embedded_preview = record.content.get("preview") if isinstance(record.content, Mapping) else None
        if embedded_preview:
            artifact_texts.append(str(embedded_preview))
        return IndexDocument.from_memory_record(
            record,
            scope_key=scope_key,
            generation=generation,
            artifact_text="\n".join(artifact_texts),
        )

    def _hydrate(self, task_id: str, retrieval: RetrievalResult) -> HydratedMemoryResult:
        canonical = {
            record.memory_id: record
            for record in self.canonical_store.task_memory_records(task_id)
        }
        records: list[MemoryRecord] = []
        stale: list[str] = []
        missing: list[str] = []
        for hit in retrieval.hits:
            record = canonical.get(hit.document_id)
            if record is None:
                missing.append(hit.document_id)
                continue
            expected = self._record_to_document(record, hit.metadata.get("scope_key", self.task_scope(task_id)), max(1, hit.generation))
            if expected.content_digest != hit.content_digest:
                stale.append(hit.document_id)
                continue
            records.append(record)
        repair_job_id = ""
        if stale or missing:
            repair = self.synchronize_task(task_id, process=False, force=True, causation_id="retrieval-hydration-mismatch")
            repair_job_id = repair.job_id
        return HydratedMemoryResult(
            retrieval=retrieval,
            records=tuple(records),
            stale_document_ids=tuple(stale),
            missing_document_ids=tuple(missing),
            repair_job_id=repair_job_id,
        )

    @staticmethod
    def _record_projection(record: MemoryRecord) -> Mapping[str, Any]:
        return {
            "memory_id": record.memory_id,
            "run_id": record.run_id,
            "task_id": record.task_id,
            "layer": str(record.layer),
            "source_type": record.source_type,
            "source_id": record.source_id,
            "node_id": record.node_id or "",
            "summary": record.summary,
            "content": record.content,
            "keywords": sorted(record.keywords),
            "artifact_ids": sorted(record.artifact_ids),
            "evidence_ids": sorted(record.evidence_ids),
            "score": record.score,
            "metadata": record.metadata,
        }
