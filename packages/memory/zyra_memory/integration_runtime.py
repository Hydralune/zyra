from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from zyra_core import EventRecord, EventType

from .incremental_index import ChangeDisposition, IncrementalIndexUpdater
from .integration_store import AdmissionState, RetrievalIntegrationStore, SourceAdmission
from .memory_index import HydratedMemoryResult, MemoryIndexRuntime, MemoryIndexSyncResult
from .models import MemoryLayer, MemoryRecord
from .process_supervisor import (
    IndexWorkerProcessReceipt,
    MemoryIndexWorkerProcessSupervisor,
)
from .query_contract import (
    IndexCheckpointRef,
    MemoryFilterQuery,
    RecoveryIndexReference,
    RetrievalConsumer,
    RetrievalMode,
    RetrievalSnapshotRef,
)
from .retrieval_context import MemoryContextBlock, MemoryContextBridge
from .retrieval_models import (
    IndexJobState,
    RetrievalFilter,
    stable_digest,
)
from .skill_memory_index import SkillExperience, SkillMemoryIndex


class RetrievalIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CanonicalAdmissionResult:
    admission: SourceAdmission
    disposition: str
    source_revision: str
    job_id: str = ""
    generation: int = 0
    sync: MemoryIndexSyncResult | None = None
    worker_receipt: IndexWorkerProcessReceipt | None = None
    published: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "admission": self.admission.to_dict(),
            "disposition": self.disposition,
            "source_revision": self.source_revision,
            "job_id": self.job_id,
            "generation": self.generation,
            "sync": self.sync.to_dict() if self.sync else None,
            "worker_receipt": self.worker_receipt.to_dict() if self.worker_receipt else None,
            "published": self.published,
            "canonical_owner": "MemoryRecordStore",
            "derived_owner": "MemoryIndexRuntime",
        }


@dataclass(frozen=True, slots=True)
class RetrievalExecution:
    request: MemoryFilterQuery
    hydrated: HydratedMemoryResult
    snapshot: RetrievalSnapshotRef
    source_revision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(include_text=False),
            "result": self.hydrated.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "source_revision": self.source_revision,
            "canonical_hydration": True,
        }


@dataclass(frozen=True, slots=True)
class RetrievalComparisonArm:
    mode: RetrievalMode
    memory_ids: tuple[str, ...]
    scores: tuple[float, ...]
    elapsed_ms: float
    vector_status: str
    result_digest: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "memory_ids": list(self.memory_ids),
            "scores": list(self.scores),
            "elapsed_ms": self.elapsed_ms,
            "vector_status": self.vector_status,
            "result_digest": self.result_digest,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class RetrievalComparison:
    task_id: str
    query_digest: str
    arms: tuple[RetrievalComparisonArm, ...]
    ordering_changed: bool
    coverage_changed: bool
    vector_available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "query_digest": self.query_digest,
            "arms": [arm.to_dict() for arm in self.arms],
            "ordering_changed": self.ordering_changed,
            "coverage_changed": self.coverage_changed,
            "vector_available": self.vector_available,
            "canonical_record_comparison": True,
            "mnemopi_database_used": False,
        }


@dataclass(frozen=True, slots=True)
class RecoveryRecallResult:
    failures: RetrievalExecution
    procedures: RetrievalExecution
    reference: RecoveryIndexReference

    def to_dict(self) -> dict[str, Any]:
        return {
            "failures": self.failures.to_dict(),
            "procedures": self.procedures.to_dict(),
            "reference": self.reference.to_dict(),
        }


class RetrievalIndexAdapter:
    """Typed integration port over the foundation ``MemoryIndexRuntime``.

    The port exists to keep CodeWorker/compact/recovery consumers from reaching
    into SQLite details.  It is not a second store and does not allow alternate
    canonical memory implementations to bypass MemoryRecordStore hydration.
    """

    def __init__(self, runtime: MemoryIndexRuntime) -> None:
        self.runtime = runtime

    def retrieve(
        self,
        request: MemoryFilterQuery,
        *,
        mode: RetrievalMode = RetrievalMode.POLYPHONIC,
    ) -> HydratedMemoryResult:
        value = request.validated()
        if mode is RetrievalMode.LEXICAL_ONLY:
            raise RetrievalIntegrationError(
                "lexical_baseline_not_production",
                "lexical-only retrieval is an evaluation arm, not a production adapter",
            )
        return self.runtime.retrieve(
            value.task_id,
            value.text,
            filters=value.filters,
            budget=value.budget,
            vector_enabled=(value.vector_enabled and mode is RetrievalMode.POLYPHONIC),
            synchronize=False,
            query_id=value.query_id,
        )

    def status(self, task_id: str = "") -> Mapping[str, Any]:
        return self.runtime.status(task_id)


class RetrievalIntegrationRuntime:
    """Canonical admission -> durable worker -> query snapshot integration.

    AgentScope's durable RAG job lifecycle remains the primary implementation
    shape.  The OMP-derived intent/MMR/polyphonic mechanisms stay inside the
    foundation ranking code and never own a database or event stream here.
    """

    def __init__(
        self,
        runtime: MemoryIndexRuntime,
        *,
        worker_supervisor: MemoryIndexWorkerProcessSupervisor | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        enabled: bool = True,
        worker_enabled: bool = True,
        sweeper_enabled: bool = True,
        allow_inline_worker_for_tests: bool = False,
    ) -> None:
        self.runtime = runtime
        self.adapter = RetrievalIndexAdapter(runtime)
        self.updater = IncrementalIndexUpdater(runtime)
        self.skill_index = SkillMemoryIndex(runtime)
        self.context_bridge = MemoryContextBridge(runtime)
        self.store = RetrievalIntegrationStore(runtime.index)
        self.worker_supervisor = worker_supervisor
        self.event_sink = event_sink
        self.enabled = bool(enabled)
        self.worker_enabled = bool(worker_enabled)
        self.sweeper_enabled = bool(sweeper_enabled)
        self.allow_inline_worker_for_tests = bool(allow_inline_worker_for_tests)

    def admit_canonical_records(
        self,
        task_id: str,
        records: Sequence[MemoryRecord] | None = None,
        *,
        event_id: str = "",
        causation_id: str = "",
        process_worker: bool = True,
        force: bool = False,
    ) -> CanonicalAdmissionResult:
        self._require_enabled()
        canonical = tuple(
            records
            if records is not None
            else self.runtime.canonical_store.task_memory_records(task_id)
        )
        if any(record.task_id != task_id for record in canonical):
            raise RetrievalIntegrationError(
                "foreign_task_memory",
                "canonical admission received memory records from another task",
            )
        source_revision = self.runtime.source_revision(canonical)
        content_digest = stable_digest(
            [self.runtime._record_projection(record) for record in canonical]
        )
        admission, inserted = self.store.admit_source(
            task_id=task_id,
            source_kind="memory_record_set",
            source_id=task_id,
            source_revision=source_revision,
            content_digest=content_digest,
            event_id=event_id,
            causation_id=causation_id,
            metadata={
                "record_count": len(canonical),
                "canonical_owner": type(self.runtime.canonical_store).__name__,
                "index_owner": "MemoryIndexRuntime",
            },
        )
        scope = self.runtime.index.scope_state(self.runtime.task_scope(task_id))
        already_published = (
            str(scope.get("published_revision") or "") == source_revision
            and int(scope.get("published_generation") or 0) > 0
        )
        if already_published and not force:
            if admission.state is not AdmissionState.PUBLISHED:
                if admission.job_id:
                    admissions = self.store.mark_published(
                        admission.job_id,
                        generation=int(scope["published_generation"]),
                    )
                    admission = next(
                        (value for value in admissions if value.admission_id == admission.admission_id),
                        admission,
                    )
            self._checkpoint(task_id, source_revision)
            return CanonicalAdmissionResult(
                admission=admission,
                disposition="already_published",
                source_revision=source_revision,
                job_id=admission.job_id,
                generation=int(scope["published_generation"]),
                published=True,
            )

        update = self.updater.from_memory_records(
            task_id,
            canonical,
            process=False,
            causation_id=causation_id or event_id or admission.admission_id,
        )
        sync = update.sync
        if sync is None or (force and not sync.changed):
            sync = self.runtime.synchronize_task(
                task_id,
                records=canonical,
                process=False,
                causation_id=causation_id or event_id or admission.admission_id,
                force=force or not already_published,
            )
        if sync.job_id:
            admission = self.store.bind_job(
                admission.admission_id,
                job_id=sync.job_id,
                generation=sync.generation,
            )
        self._emit(
            task_id=task_id,
            run_id=canonical[0].run_id if canonical else "",
            phase="source_admitted",
            payload={
                "admission_id": admission.admission_id,
                "event_id": event_id,
                "source_revision": source_revision,
                "job_id": sync.job_id,
                "generation": sync.generation,
                "disposition": update.disposition.value,
            },
        )
        worker_receipt: IndexWorkerProcessReceipt | None = None
        if process_worker and sync.changed:
            worker_receipt = self._process_job(task_id)
        scope = self.runtime.index.scope_state(self.runtime.task_scope(task_id))
        published = (
            str(scope.get("published_revision") or "") == source_revision
            and int(scope.get("published_generation") or 0) >= sync.generation
        )
        if process_worker and not published:
            raise RetrievalIntegrationError(
                "index_publication_not_observed",
                "durable memory index worker returned without publishing the canonical revision",
            )
        if published and sync.job_id:
            admissions = self.store.mark_published(
                sync.job_id,
                generation=int(scope["published_generation"]),
            )
            admission = next(
                (value for value in admissions if value.admission_id == admission.admission_id),
                admission,
            )
        self._checkpoint(task_id, source_revision)
        self._emit(
            task_id=task_id,
            run_id=canonical[0].run_id if canonical else "",
            phase="source_published" if published else "source_queued",
            payload={
                "admission_id": admission.admission_id,
                "job_id": sync.job_id,
                "generation": int(scope.get("published_generation") or sync.generation),
                "source_revision": source_revision,
                "worker_process": worker_receipt.to_dict() if worker_receipt else None,
            },
        )
        disposition = (
            "published"
            if published
            else "queued"
            if update.disposition is ChangeDisposition.ENQUEUED
            else update.disposition.value
        )
        return CanonicalAdmissionResult(
            admission=admission,
            disposition=disposition if inserted else f"duplicate_{disposition}",
            source_revision=source_revision,
            job_id=sync.job_id,
            generation=int(scope.get("published_generation") or sync.generation),
            sync=sync,
            worker_receipt=worker_receipt,
            published=published,
        )

    def consume_canonical_event(
        self,
        event: Mapping[str, Any],
        *,
        process_worker: bool = False,
    ) -> CanonicalAdmissionResult:
        self._require_enabled()
        task_id = str(event.get("task_id") or "").strip()
        event_id = str(event.get("event_id") or "").strip()
        if not task_id or not event_id:
            raise RetrievalIntegrationError(
                "invalid_canonical_event",
                "canonical event requires task_id and event_id",
            )
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        causation_id = str(payload.get("causation_id") or event_id)
        # This call is not allowed to turn event payload into memory.  The
        # MemoryFabric/MemoryRecordStore projection must already exist.
        records = self.runtime.canonical_store.task_memory_records(task_id)
        result = self.admit_canonical_records(
            task_id,
            records,
            event_id=event_id,
            causation_id=causation_id,
            process_worker=process_worker,
        )
        return result

    def execute(
        self,
        request: MemoryFilterQuery,
        *,
        mode: RetrievalMode = RetrievalMode.POLYPHONIC,
    ) -> RetrievalExecution:
        self._require_enabled()
        value = request.validated()
        canonical = tuple(self.runtime.canonical_store.task_memory_records(value.task_id))
        source_revision = self.runtime.source_revision(canonical)
        scope = self.runtime.index.scope_state(self.runtime.task_scope(value.task_id))
        published_revision = str(scope.get("published_revision") or "")
        if published_revision != source_revision:
            if value.synchronize:
                self.admit_canonical_records(
                    value.task_id,
                    canonical,
                    event_id=value.request_id,
                    causation_id=value.causation_id,
                    process_worker=True,
                )
                scope = self.runtime.index.scope_state(self.runtime.task_scope(value.task_id))
                published_revision = str(scope.get("published_revision") or "")
            if published_revision != source_revision:
                raise RetrievalIntegrationError(
                    "index_revision_not_current",
                    "retrieval index does not cover the current canonical memory revision",
                )
        hydrated = self.adapter.retrieve(value, mode=mode)
        snapshot = RetrievalSnapshotRef.from_result(
            value,
            hydrated.retrieval,
            source_revision=source_revision,
            mode=mode,
        )
        self.store.record_query(
            snapshot,
            request_id=value.request_id,
            metadata={
                "causation_id": value.causation_id,
                "stale_hit_count": len(hydrated.stale_document_ids),
                "missing_hit_count": len(hydrated.missing_document_ids),
                "canonical_hydrated_count": len(hydrated.records),
            },
        )
        run_id = value.run_id or (canonical[0].run_id if canonical else "")
        self._emit(
            task_id=value.task_id,
            run_id=run_id,
            phase="query_snapshot",
            payload={
                "query_id": snapshot.query_id,
                "consumer": value.consumer.value,
                "query_fingerprint": value.fingerprint,
                "index_scope": snapshot.index_scope,
                "index_generation": snapshot.index_generation,
                "index_revision": snapshot.index_revision,
                "result_digest": snapshot.result_digest,
                "source_refs": [item.to_dict() for item in snapshot.source_refs],
                "budget": value.to_dict(include_text=False)["budget"],
                "vector_status": snapshot.vector_status,
            },
        )
        self._checkpoint(value.task_id, source_revision)
        return RetrievalExecution(
            request=value,
            hydrated=hydrated,
            snapshot=snapshot,
            source_revision=source_revision,
        )

    def context(self, request: MemoryFilterQuery) -> tuple[RetrievalExecution, MemoryContextBlock]:
        value = request.validated()
        execution = self.execute(value, mode=RetrievalMode.POLYPHONIC)
        entries, total_chars, truncated = MemoryContextBridge._entries(
            execution.hydrated,
            maximum_entries=value.maximum_results,
            maximum_chars=value.maximum_chars,
        )
        diagnostics = execution.hydrated.retrieval.diagnostics
        warnings = list(diagnostics.warnings)
        if execution.hydrated.stale_document_ids:
            warnings.append("stale_hits_rejected")
        if execution.hydrated.missing_document_ids:
            warnings.append("missing_hits_rejected")
        block = MemoryContextBlock(
            task_id=value.task_id,
            query=value.text,
            entries=entries,
            query_id=execution.snapshot.query_id,
            query_digest=value.fingerprint,
            index_scope=execution.snapshot.index_scope,
            index_generation=execution.snapshot.index_generation,
            vector_status=execution.snapshot.vector_status,
            total_chars=total_chars,
            truncated=truncated or execution.snapshot.truncated,
            warnings=tuple(dict.fromkeys(warnings)),
        )
        return execution, block

    def skill_experiences(
        self,
        request: MemoryFilterQuery,
    ) -> tuple[RetrievalExecution, tuple[SkillExperience, ...]]:
        value = request.validated()
        if value.consumer is not RetrievalConsumer.SKILL_MEMORY:
            value = replace_query_consumer(value, RetrievalConsumer.SKILL_MEMORY)
        if not value.layers:
            value = replace_query_layers(value, (MemoryLayer.SKILL,))
        execution = self.execute(value)
        experiences = tuple(
            SkillExperience.from_record(record)
            for record in execution.hydrated.records
            if record.layer is MemoryLayer.SKILL
        )
        return execution, experiences

    def recovery_recall(
        self,
        *,
        task_id: str,
        query: str,
        run_id: str = "",
        session_id: str = "",
        failure_kinds: Sequence[str] = (),
        request_id: str = "",
        limit: int = 12,
    ) -> RecoveryRecallResult:
        failures = self.execute(
            MemoryFilterQuery(
                task_id=task_id,
                text=query,
                consumer=RetrievalConsumer.FAILURE_RECOVERY,
                run_id=run_id,
                session_id=session_id,
                failure_kinds=tuple(failure_kinds),
                maximum_results=limit,
                request_id=request_id,
            )
        )
        procedures = self.execute(
            MemoryFilterQuery(
                task_id=task_id,
                text=query,
                consumer=RetrievalConsumer.PROCEDURE_RECALL,
                run_id=run_id,
                session_id=session_id,
                layers=(MemoryLayer.SKILL, MemoryLayer.SEMANTIC),
                source_types=("skill_invocation", "procedure", "workflow"),
                maximum_results=limit,
                request_id=f"{request_id}:procedure" if request_id else "",
            )
        )
        refs = tuple(dict.fromkeys((*failures.snapshot.source_refs, *procedures.snapshot.source_refs)))
        reference = RecoveryIndexReference(
            task_id=task_id,
            query_id=failures.snapshot.query_id,
            index_scope=failures.snapshot.index_scope,
            index_generation=max(
                failures.snapshot.index_generation,
                procedures.snapshot.index_generation,
            ),
            index_revision=failures.source_revision,
            source_refs=refs,
            failure_kinds=tuple(sorted(set(str(value) for value in failure_kinds if str(value)))),
            procedure_memory_ids=tuple(
                record.memory_id for record in procedures.hydrated.records
            ),
        )
        return RecoveryRecallResult(
            failures=failures,
            procedures=procedures,
            reference=reference,
        )

    def compare(
        self,
        request: MemoryFilterQuery,
    ) -> RetrievalComparison:
        self._require_enabled()
        value = request.validated()
        canonical = tuple(self.runtime.canonical_store.task_memory_records(value.task_id))
        lexical_started = time.perf_counter()
        lexical = self._lexical_baseline(canonical, value)
        lexical_arm = RetrievalComparisonArm(
            mode=RetrievalMode.LEXICAL_ONLY,
            memory_ids=tuple(record.memory_id for record, _ in lexical),
            scores=tuple(score for _, score in lexical),
            elapsed_ms=(time.perf_counter() - lexical_started) * 1000.0,
            vector_status="disabled",
            result_digest=stable_digest([(record.memory_id, score) for record, score in lexical]),
        )
        fts_started = time.perf_counter()
        fts = self.execute(value, mode=RetrievalMode.FTS_MMR)
        fts_arm = RetrievalComparisonArm(
            mode=RetrievalMode.FTS_MMR,
            memory_ids=tuple(record.memory_id for record in fts.hydrated.records),
            scores=tuple(hit.score for hit in fts.hydrated.retrieval.hits),
            elapsed_ms=(time.perf_counter() - fts_started) * 1000.0,
            vector_status=fts.snapshot.vector_status,
            result_digest=fts.snapshot.result_digest,
            warnings=fts.snapshot.warnings,
        )
        poly_started = time.perf_counter()
        poly = self.execute(value, mode=RetrievalMode.POLYPHONIC)
        poly_arm = RetrievalComparisonArm(
            mode=RetrievalMode.POLYPHONIC,
            memory_ids=tuple(record.memory_id for record in poly.hydrated.records),
            scores=tuple(hit.score for hit in poly.hydrated.retrieval.hits),
            elapsed_ms=(time.perf_counter() - poly_started) * 1000.0,
            vector_status=poly.snapshot.vector_status,
            result_digest=poly.snapshot.result_digest,
            warnings=poly.snapshot.warnings,
        )
        arms = (lexical_arm, fts_arm, poly_arm)
        orderings = {arm.memory_ids for arm in arms}
        coverages = {frozenset(arm.memory_ids) for arm in arms}
        comparison = RetrievalComparison(
            task_id=value.task_id,
            query_digest=stable_digest(value.text),
            arms=arms,
            ordering_changed=len(orderings) > 1,
            coverage_changed=len(coverages) > 1,
            vector_available=poly_arm.vector_status == "available",
        )
        self._emit(
            task_id=value.task_id,
            run_id=value.run_id or (canonical[0].run_id if canonical else ""),
            phase="retrieval_comparison",
            payload=comparison.to_dict(),
        )
        return comparison

    def sweep_and_recover(self, *, task_id: str = "", maximum_jobs: int = 64) -> Mapping[str, Any]:
        self._require_enabled()
        if not self.sweeper_enabled:
            return {
                "status": "disabled",
                "sweeper_enabled": False,
                "stale_leases_recovered": False,
                "fallback": False,
            }
        if self.worker_supervisor is not None:
            scope = self.runtime.task_scope(task_id) if task_id else ""
            sweep, drain = self.worker_supervisor.recover_and_drain(
                maximum_jobs=maximum_jobs,
                scope_key=scope,
            )
            return {"sweep": sweep.to_dict(), "drain": drain.to_dict()}
        if not self.allow_inline_worker_for_tests:
            raise RetrievalIntegrationError(
                "index_worker_process_missing",
                "sweeper recovery requires the productized index worker process",
            )
        sweep = self.runtime.sweeper.sweep_once()
        outcomes = self.runtime.worker.drain(
            maximum_jobs=maximum_jobs,
            scope_key=self.runtime.task_scope(task_id) if task_id else "",
        )
        return {
            "sweep": sweep.to_dict(),
            "outcomes": [outcome.to_dict() for outcome in outcomes],
            "test_inline_worker": True,
        }

    def checkpoint_ref(self, task_id: str) -> IndexCheckpointRef:
        canonical = tuple(self.runtime.canonical_store.task_memory_records(task_id))
        source_revision = self.runtime.source_revision(canonical)
        scope_key = self.runtime.task_scope(task_id)
        scope = self.runtime.index.scope_state(scope_key)
        pending = tuple(
            job.job_id
            for state in (
                IndexJobState.QUEUED,
                IndexJobState.LEASED,
                IndexJobState.BUILDING,
                IndexJobState.PUBLISHING,
            )
            for job in self.runtime.queue.list(scope_key=scope_key, states=(state,))
        )
        queries = self.store.query_records(task_id=task_id, limit=16)
        admissions = self.store.source_admissions(task_id=task_id, limit=32)
        return IndexCheckpointRef(
            task_id=task_id,
            source_revision=source_revision,
            desired_generation=int(scope.get("desired_generation") or 0),
            published_generation=int(scope.get("published_generation") or 0),
            pending_job_ids=tuple(dict.fromkeys(pending)),
            last_query_ids=tuple(record.query_id for record in queries),
            cursor_refs=tuple(
                {
                    "admission_id": admission.admission_id,
                    "source_kind": admission.source_kind,
                    "source_id": admission.source_id,
                    "source_revision": admission.source_revision,
                    "job_id": admission.job_id,
                    "generation": admission.generation,
                    "state": admission.state.value,
                }
                for admission in admissions
            ),
        ).validated()

    def disable_probes(self, task_id: str, query: str) -> Mapping[str, Any]:
        enabled_hits: tuple[str, ...] = ()
        try:
            result = self.execute(
                MemoryFilterQuery(
                    task_id=task_id,
                    text=query,
                    consumer=RetrievalConsumer.EVALUATION,
                )
            )
            enabled_hits = tuple(record.memory_id for record in result.hydrated.records)
        except RetrievalIntegrationError:
            enabled_hits = ()
        queued = self.runtime.queue.list(
            scope_key=self.runtime.task_scope(task_id),
            states=(IndexJobState.QUEUED,),
        )
        return {
            "retrieval_disabled": {
                "result_count": 0,
                "legacy_scan": False,
                "fallback": False,
                "behavior_changed": bool(enabled_hits),
            },
            "worker_disabled": {
                "queued_job_ids": [job.job_id for job in queued],
                "queue_stalled": bool(queued) or not self.worker_enabled,
                "inline_rebuild": False,
            },
            "sweeper_disabled": {
                "expired_lease_requeued": False,
                "automatic_recovery": False,
                "fallback": False,
            },
            "enabled_memory_ids": list(enabled_hits),
        }

    def status(self, task_id: str = "") -> Mapping[str, Any]:
        return {
            "enabled": self.enabled,
            "worker_enabled": self.worker_enabled,
            "sweeper_enabled": self.sweeper_enabled,
            "adapter": "RetrievalIndexAdapter",
            "runtime": self.runtime.status(task_id),
            "integration": self.store.health(),
            "canonical_owner": type(self.runtime.canonical_store).__name__,
            "derived_state": True,
            "external_source_dependency": False,
            "legacy_substring_fallback": False,
        }

    def _process_job(self, task_id: str) -> IndexWorkerProcessReceipt | None:
        if not self.worker_enabled:
            raise RetrievalIntegrationError(
                "index_worker_disabled",
                "memory index job is queued but the index worker is disabled",
            )
        scope_key = self.runtime.task_scope(task_id)
        if self.worker_supervisor is not None:
            receipt = self.worker_supervisor.drain(maximum_jobs=64, scope_key=scope_key)
            if not receipt.ok:
                raise RetrievalIntegrationError(
                    "index_worker_failed",
                    "productized memory index worker returned a failure receipt",
                )
            return receipt
        if not self.allow_inline_worker_for_tests:
            raise RetrievalIntegrationError(
                "index_worker_process_missing",
                "production retrieval integration requires the index worker process port",
            )
        outcomes = self.runtime.worker.drain(maximum_jobs=64, scope_key=scope_key)
        if any(outcome.status not in {"ready", "idle", "fenced"} for outcome in outcomes):
            raise RetrievalIntegrationError(
                "inline_test_worker_failed",
                "test-only inline index worker did not reach a terminal publication state",
            )
        return None

    def _checkpoint(self, task_id: str, source_revision: str) -> None:
        checkpoint = self.checkpoint_ref(task_id)
        if checkpoint.source_revision != source_revision:
            raise RetrievalIntegrationError(
                "checkpoint_source_revision_mismatch",
                "checkpoint reference diverged from canonical source revision",
            )
        self.store.save_checkpoint(checkpoint)

    def _emit(
        self,
        *,
        task_id: str,
        run_id: str,
        phase: str,
        payload: Mapping[str, Any],
    ) -> None:
        if self.event_sink is None or not run_id or not task_id:
            return
        self.event_sink(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                event_type=EventType.SYSTEM_NOTICE,
                payload={
                    "retrieval_index": {
                        "schema": "zyra.retrieval-index-event.v1",
                        "phase": phase,
                        "state_owner": "MemoryIndexRuntime",
                        "canonical_owner": "MemoryRecordStore",
                        **dict(payload),
                    }
                },
            )
        )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RetrievalIntegrationError(
                "retrieval_index_disabled",
                "retrieval integration is disabled and has no substring fallback",
            )

    @staticmethod
    def _lexical_baseline(
        records: Sequence[MemoryRecord],
        request: MemoryFilterQuery,
    ) -> tuple[tuple[MemoryRecord, float], ...]:
        filters = request.filters
        tokens = tuple(
            dict.fromkeys(
                token.casefold()
                for token in re.findall(r"[\w.-]+", request.text, flags=re.UNICODE)
                if len(token) > 1
            )
        )
        if not tokens:
            return ()
        ranked: list[tuple[MemoryRecord, float]] = []
        for record in records:
            if not _record_matches_filters(record, filters):
                continue
            content = " ".join(
                (
                    record.summary,
                    json.dumps(record.content, ensure_ascii=False, sort_keys=True, default=str),
                    " ".join(record.keywords),
                )
            ).casefold()
            matched = sum(1 for token in tokens if token in content)
            if not matched:
                continue
            coverage = matched / len(tokens)
            score = coverage * 0.85 + max(0.0, min(float(record.score), 1.0)) * 0.15
            if math.isfinite(score):
                ranked.append((record, round(score, 8)))
        ranked.sort(key=lambda item: (-item[1], item[0].memory_id))
        return tuple(ranked[: request.maximum_results])


def _record_matches_filters(record: MemoryRecord, filters: RetrievalFilter) -> bool:
    if filters.task_ids and record.task_id not in filters.task_ids:
        return False
    if filters.run_ids and record.run_id not in filters.run_ids:
        return False
    if filters.layers and record.layer not in filters.layers:
        return False
    if filters.source_types and record.source_type not in filters.source_types:
        return False
    if filters.source_ids and record.source_id not in filters.source_ids:
        return False
    if filters.node_ids and (record.node_id or "") not in filters.node_ids:
        return False
    metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
    if filters.session_ids and str(metadata.get("session_id") or "") not in filters.session_ids:
        return False
    if filters.skill_names:
        skill_name = str(metadata.get("skill_name") or record.source_id)
        if skill_name not in filters.skill_names:
            return False
    if filters.failure_kinds:
        failure_kind = str(metadata.get("failure_kind") or "")
        if failure_kind not in filters.failure_kinds:
            return False
    if filters.artifact_ids and not set(filters.artifact_ids).intersection(record.artifact_ids):
        return False
    return True


def replace_query_consumer(
    request: MemoryFilterQuery,
    consumer: RetrievalConsumer,
) -> MemoryFilterQuery:
    from dataclasses import replace

    return replace(request, consumer=consumer)


def replace_query_layers(
    request: MemoryFilterQuery,
    layers: Sequence[MemoryLayer],
) -> MemoryFilterQuery:
    from dataclasses import replace

    return replace(request, layers=tuple(layers))


__all__ = [
    "CanonicalAdmissionResult",
    "RecoveryRecallResult",
    "RetrievalComparison",
    "RetrievalComparisonArm",
    "RetrievalExecution",
    "RetrievalIndexAdapter",
    "RetrievalIntegrationError",
    "RetrievalIntegrationRuntime",
]
